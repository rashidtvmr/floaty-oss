//! Structured accessibility tree reading.
//! See docs/A11Y-TREE-SHORTCUTS.md section 1.3 for the frozen interface.
//!
//! Rendering is pure: it never touches the filesystem, the environment or the
//! network, so it can be unit-tested deterministically and reused by any caller.
use std::collections::{HashMap, HashSet};

/// Version of the helper JSON contract understood by this module.
pub const A11Y_SCHEMA: u32 = 1;

/// Roles that carry no user-visible content on their own.
const STRUCTURAL_ROLES: [&str; 5] = ["panel", "filler", "separator", "scroll pane", "unknown"];

/// Upper bound on the dedup set so a hostile tree cannot grow memory without limit.
const DEDUP_CAPACITY: usize = 512;

#[derive(Debug, Clone, PartialEq, Eq, serde::Deserialize)]
pub struct A11yNode {
    pub id: u64,
    /// Native object identity, stable across helper invocations when available.
    /// Numeric `id` remains the per-read traversal identifier for schema 1.
    #[serde(default)]
    pub stable_id: Option<String>,
    pub parent: Option<u64>,
    pub role: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub focused: bool,
    #[serde(default)]
    pub bounds: Option<[i64; 4]>,
    #[serde(default)]
    pub image: bool,
    #[serde(default)]
    pub protected: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Deserialize)]
pub struct A11yTree {
    pub schema: u32,
    pub source: String,
    /// Native selected-window identity; titles are not stable window keys.
    #[serde(default)]
    pub window_id: Option<String>,
    #[serde(default)]
    pub app: String,
    #[serde(default)]
    pub pid: Option<u32>,
    #[serde(default)]
    pub truncated: bool,
    #[serde(default)]
    pub nodes: Vec<A11yNode>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadMode {
    /// Focused subtree only; cheapest and the default.
    Focused,
    /// Whole tree as an indented outline.
    Tree,
    /// Flat text of text-bearing nodes (legacy behaviour).
    Text,
    /// Focused subtree if one exists, otherwise the whole tree.
    Auto,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenBudget {
    pub max_tokens: usize,
    pub max_nodes: usize,
    pub max_depth: usize,
    pub max_chars: usize,
}

impl Default for TokenBudget {
    fn default() -> Self {
        Self {
            // Sized for real windows, not just native toolbars: a browser nests
            // page content (AXWebArea) about eight levels below the window and
            // the question several levels below that. On a measured Chrome
            // practice-test page the question sat at depth 13, the window held
            // ~355 nodes, and the rendered outline was ~12k characters. The
            // defaults keep that content while still bounding hostile trees.
            max_tokens: 4000,
            max_nodes: 800,
            max_depth: 20,
            max_chars: 32_000,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct A11yReading {
    /// Token-budgeted rendering handed to the model.
    pub text: String,
    /// Role-free rendering for the practice panel. Equals `text` unless the
    /// rendered form carries accessibility role labels worth stripping.
    pub display: String,
    /// Image/graphic nodes worth OCR, largest first, bounded by the budget.
    pub image_regions: Vec<ImageRegion>,
    /// The role of the focused node, when known.
    pub focused_role: Option<String>,
    pub truncated: bool,
    pub source: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ImageRegion {
    pub x: i64,
    pub y: i64,
    pub width: i64,
    pub height: i64,
}

impl ImageRegion {
    /// Pixel area, saturating and never negative so bad bounds cannot panic.
    pub fn area(&self) -> i64 {
        self.width.max(0).saturating_mul(self.height.max(0))
    }

    pub fn is_worth_ocr(&self, min_pixels: i64) -> bool {
        self.width > 0 && self.height > 0 && self.area() >= min_pixels.max(1)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum A11yError {
    #[error("malformed accessibility tree JSON: {0}")]
    Malformed(String),
    #[error("unsupported accessibility tree schema: {0}")]
    UnsupportedSchema(u32),
}

/// Rough token estimate: four characters per token, rounded up.
pub fn estimate_tokens(text: &str) -> usize {
    text.chars().count().div_ceil(4)
}

/// The text a node contributes: its `text`, falling back to its `name`.
fn label_of(node: &A11yNode) -> &str {
    if node.text.is_empty() {
        &node.name
    } else {
        &node.text
    }
}

fn is_structural(role: &str) -> bool {
    let role = role.trim().to_ascii_lowercase();
    STRUCTURAL_ROLES.contains(&role.as_str())
}

/// Accumulates rendered lines while enforcing every budget bound.
struct Renderer {
    budget: TokenBudget,
    used_nodes: usize,
    used_chars: usize,
    used_tokens: usize,
    truncated: bool,
    seen: HashSet<(String, String)>,
}

impl Renderer {
    fn new(budget: TokenBudget) -> Self {
        Self {
            budget,
            used_nodes: 0,
            used_chars: 0,
            used_tokens: 0,
            truncated: false,
            seen: HashSet::new(),
        }
    }

    /// Append one line. Returns `false` when iteration must stop because a
    /// non-depth bound was reached. Duplicate `(role, label)` pairs are skipped.
    fn push_line(
        &mut self,
        dedup_role: &str,
        prefix: &str,
        label: &str,
        out: &mut Vec<String>,
    ) -> bool {
        let key = (dedup_role.to_owned(), label.to_owned());
        if self.seen.contains(&key) {
            return true;
        }
        let line = format!("{prefix}{label}");
        let line_chars = line.chars().count();
        let line_tokens = estimate_tokens(&line);
        if self.used_nodes >= self.budget.max_nodes
            || self.used_chars + line_chars > self.budget.max_chars
            || self.used_tokens + line_tokens > self.budget.max_tokens
        {
            self.truncated = true;
            return false;
        }
        if self.seen.len() < DEDUP_CAPACITY {
            self.seen.insert(key);
        }
        self.used_nodes += 1;
        self.used_chars += line_chars + 1;
        self.used_tokens += line_tokens;
        out.push(line);
        true
    }
}

impl A11yTree {
    /// Parse the helper's JSON. Returns Err on schema mismatch or malformed JSON.
    pub fn parse(json: &str) -> Result<Self, A11yError> {
        let tree: A11yTree =
            serde_json::from_str(json).map_err(|e| A11yError::Malformed(e.to_string()))?;
        if tree.schema != A11Y_SCHEMA {
            return Err(A11yError::UnsupportedSchema(tree.schema));
        }
        Ok(tree)
    }

    /// Assemble question context before spending the model's token budget.
    /// Remove explicitly marked application chrome and protected subtrees,
    /// keeping document order, code whitespace, examples and answer choices.
    /// Unknown layouts remain intact: text length or English keywords never
    /// decide which question paragraphs survive.
    pub fn question_tree(&self) -> Self {
        let index = self.node_index();
        let is_chrome = |role: &str| {
            matches!(
                role.to_ascii_lowercase()
                    .replace([' ', '-', '_'], "")
                    .as_str(),
                "toolbar" | "menubar" | "menu" | "navigation" | "banner" | "statusbar"
            )
        };
        let mut scoped = self.clone();
        scoped.nodes.retain(|node| {
            let mut current = Some(node.id);
            let mut visited = HashSet::new();
            while let Some(id) = current {
                if !visited.insert(id) {
                    return false;
                }
                let Some(&position) = index.get(&id) else {
                    break;
                };
                let ancestor = &self.nodes[position];
                if ancestor.protected || is_chrome(&ancestor.role) {
                    return false;
                }
                current = ancestor.parent;
            }
            true
        });
        for node in &mut scoped.nodes {
            if matches!(node.role.as_str(), "application" | "window" | "frame") {
                // The window title is source metadata, not part of a question.
                node.name.clear();
                node.text.clear();
            }
        }
        scoped
    }

    /// Apply the same reading scope to structured model updates, display text
    /// and region OCR. Focused reads contain the focus target, its ancestors
    /// and direct children; they never expand to unrelated sibling content.
    pub fn question_tree_for_mode(&self, mode: ReadMode) -> Self {
        let mut scoped = self.question_tree();
        let narrow = match mode {
            ReadMode::Focused => true,
            ReadMode::Auto => scoped
                .nodes
                .iter()
                .rposition(|node| node.focused)
                .is_some_and(|index| {
                    !matches!(
                        scoped.nodes[index].role.as_str(),
                        "window" | "frame" | "application"
                    )
                }),
            ReadMode::Tree | ReadMode::Text => false,
        };
        if narrow && let Some(indices) = scoped.focused_indices(&scoped.node_index()) {
            let included: HashSet<u64> = indices
                .into_iter()
                .map(|index| scoped.nodes[index].id)
                .collect();
            scoped.nodes.retain(|node| included.contains(&node.id));
        }
        scoped
    }

    /// Render the same scoped tree used by incremental screen updates.
    pub fn render_question(&self, mode: ReadMode, budget: TokenBudget) -> A11yReading {
        self.question_tree_for_mode(mode).render(mode, budget)
    }

    /// Render under a budget. Pure, deterministic, no I/O.
    pub fn render(&self, mode: ReadMode, budget: TokenBudget) -> A11yReading {
        let (text, truncated) = match mode {
            ReadMode::Focused => self.render_focused(budget),
            ReadMode::Tree => self.render_tree(budget),
            ReadMode::Text => self.render_flat(budget),
            ReadMode::Auto => {
                // A focused window/frame is not a content target: rendering its
                // direct children drops the nested page text. Only narrow to a
                // focused subtree when the focus is an actual content element.
                match self.nodes.iter().rposition(|node| node.focused) {
                    Some(idx)
                        if !matches!(
                            self.nodes[idx].role.as_str(),
                            "window" | "frame" | "application"
                        ) =>
                    {
                        self.render_focused(budget)
                    }
                    _ => self.render_tree(budget),
                }
            }
        };
        // Flat text already carries no role labels, so the display rendering is
        // identical; the structured modes need a separate role-free rendering.
        let display = match mode {
            ReadMode::Text => text.clone(),
            _ => self.render_display(budget),
        };
        A11yReading {
            text,
            display,
            image_regions: self.image_regions(budget),
            focused_role: self
                .nodes
                .iter()
                .rposition(|node| node.focused)
                .map(|idx| self.nodes[idx].role.clone()),
            truncated,
            source: self.source.clone(),
        }
    }

    /// Role-free, deduplicated text for the practice panel: every text-bearing
    /// node in document order, with no role labels or indentation. Role labels
    /// only exist in the structured rendering, so the panel shows readable
    /// question text rather than the shape of the accessibility tree.
    fn render_display(&self, budget: TokenBudget) -> String {
        self.render_flat(budget).0
    }

    /// Image/graphic nodes worth OCR, largest first, bounded by `max_nodes`.
    /// Protected nodes are never offered for OCR.
    pub fn image_regions(&self, budget: TokenBudget) -> Vec<ImageRegion> {
        let mut regions: Vec<(u64, ImageRegion)> = self
            .nodes
            .iter()
            .filter(|node| node.image && !node.protected)
            .filter_map(|node| {
                node.bounds.map(|b| {
                    (
                        node.id,
                        ImageRegion {
                            x: b[0],
                            y: b[1],
                            width: b[2],
                            height: b[3],
                        },
                    )
                })
            })
            .collect();
        regions.sort_by(|a, b| b.1.area().cmp(&a.1.area()).then(a.0.cmp(&b.0)));
        regions
            .into_iter()
            .take(budget.max_nodes)
            .map(|(_, region)| region)
            .collect()
    }

    /// Flat text of text-bearing nodes (legacy path).
    pub fn flat_text(&self, budget: TokenBudget) -> String {
        self.render_flat(budget).0
    }

    fn node_index(&self) -> HashMap<u64, usize> {
        self.nodes
            .iter()
            .enumerate()
            .map(|(idx, node)| (node.id, idx))
            .collect()
    }

    /// Depth of a node (root = 0), walking parents with cycle protection.
    fn depth_of(&self, idx: usize, index: &HashMap<u64, usize>) -> usize {
        let mut depth = 0usize;
        let mut guard = HashSet::new();
        guard.insert(self.nodes[idx].id);
        let mut current = self.nodes[idx].parent;
        while let Some(parent_id) = current {
            if !guard.insert(parent_id) {
                break;
            }
            match index.get(&parent_id) {
                Some(&parent_idx) => {
                    depth += 1;
                    current = self.nodes[parent_idx].parent;
                }
                None => break,
            }
        }
        depth
    }

    fn render_tree(&self, budget: TokenBudget) -> (String, bool) {
        let index = self.node_index();
        let mut renderer = Renderer::new(budget);
        let mut out = Vec::new();
        for (idx, node) in self.nodes.iter().enumerate() {
            if node.protected {
                continue;
            }
            let depth = self.depth_of(idx, &index);
            if depth > budget.max_depth {
                renderer.truncated = true;
                continue;
            }
            let label = label_of(node);
            if is_structural(&node.role) && node.text.is_empty() {
                continue;
            }
            let indent = "  ".repeat(depth);
            let prefix = if label.is_empty() {
                format!("{indent}{}", node.role)
            } else {
                format!("{indent}{} ", node.role)
            };
            if !renderer.push_line(&node.role, &prefix, label, &mut out) {
                break;
            }
        }
        (out.join("\n"), renderer.truncated || self.truncated)
    }

    fn focused_indices(&self, index: &HashMap<u64, usize>) -> Option<Vec<usize>> {
        // Pre-order traversal marks ancestors before descendants; the last
        // focused node is the actual focus target.
        let focus_idx = self.nodes.iter().rposition(|node| node.focused)?;
        // Ancestor chain, root first.
        let mut chain = Vec::new();
        let mut guard = HashSet::new();
        let mut current = Some(focus_idx);
        while let Some(idx) = current {
            if !guard.insert(idx) {
                break;
            }
            chain.push(idx);
            current = self.nodes[idx]
                .parent
                .and_then(|pid| index.get(&pid).copied());
        }
        chain.reverse();
        // Direct children of the focused node, one level deep.
        let focused_id = self.nodes[focus_idx].id;
        let children = self
            .nodes
            .iter()
            .enumerate()
            .filter(|(_, node)| node.parent == Some(focused_id))
            .map(|(idx, _)| idx);
        chain.extend(children);
        Some(chain)
    }

    fn render_focused(&self, budget: TokenBudget) -> (String, bool) {
        let index = self.node_index();
        let Some(indices) = self.focused_indices(&index) else {
            return self.render_tree(budget);
        };
        let mut renderer = Renderer::new(budget);
        let mut out = Vec::new();
        for idx in indices {
            let node = &self.nodes[idx];
            if node.protected {
                continue;
            }
            let depth = self.depth_of(idx, &index);
            if depth > budget.max_depth {
                renderer.truncated = true;
                continue;
            }
            let label = label_of(node);
            let indent = "  ".repeat(depth);
            let prefix = if label.is_empty() {
                format!("{indent}{}", node.role)
            } else {
                format!("{indent}{} ", node.role)
            };
            if !renderer.push_line(&node.role, &prefix, label, &mut out) {
                break;
            }
        }
        (out.join("\n"), renderer.truncated || self.truncated)
    }

    fn render_flat(&self, budget: TokenBudget) -> (String, bool) {
        let mut renderer = Renderer::new(budget);
        let mut out = Vec::new();
        for node in &self.nodes {
            if node.protected {
                continue;
            }
            let label = label_of(node);
            if label.is_empty() {
                continue;
            }
            if !renderer.push_line("", "", label, &mut out) {
                break;
            }
        }
        (out.join("\n"), renderer.truncated || self.truncated)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"{
        "schema": 1,
        "source": "atspi",
        "app": "Firefox",
        "pid": 4321,
        "truncated": false,
        "node_count": 5,
        "nodes": [
            {"id": 1, "parent": null, "role": "frame", "name": "Root", "text": "root text"},
            {"id": 2, "parent": 1, "role": "panel", "text": ""},
            {"id": 3, "parent": 2, "role": "paragraph", "text": "Find two numbers that sum"},
            {"id": 4, "parent": 1, "role": "image", "text": "", "bounds": [10, 20, 100, 50], "image": true},
            {"id": 5, "parent": 1, "role": "entry", "text": "", "focused": true}
        ]
    }"#;

    fn sample() -> A11yTree {
        A11yTree::parse(SAMPLE).expect("sample parses")
    }

    #[test]
    fn parse_round_trip() {
        let tree = sample();
        assert_eq!(tree.schema, 1);
        assert_eq!(tree.source, "atspi");
        assert_eq!(tree.app, "Firefox");
        assert_eq!(tree.pid, Some(4321));
        assert_eq!(tree.nodes.len(), 5);
        assert_eq!(tree.nodes[0].id, 1);
        assert_eq!(tree.nodes[0].parent, None);
        assert_eq!(tree.nodes[2].role, "paragraph");
        assert_eq!(tree.nodes[3].bounds, Some([10, 20, 100, 50]));
        assert!(tree.nodes[3].image);
        assert!(tree.nodes[4].focused);
    }

    #[test]
    fn unsupported_schema_is_rejected() {
        let json = r#"{"schema": 2, "source": "atspi", "nodes": []}"#;
        assert!(matches!(
            A11yTree::parse(json),
            Err(A11yError::UnsupportedSchema(2))
        ));
    }

    #[test]
    fn malformed_json_is_rejected() {
        assert!(matches!(
            A11yTree::parse("{not json"),
            Err(A11yError::Malformed(_))
        ));
    }

    #[test]
    fn missing_source_is_rejected() {
        assert!(matches!(
            A11yTree::parse(r#"{"schema": 1, "nodes": []}"#),
            Err(A11yError::Malformed(_))
        ));
    }

    #[test]
    fn focused_mode_selects_focused_subtree_only() {
        let json = r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"root"},
            {"id":2,"parent":1,"role":"paragraph","text":"focused here","focused":true},
            {"id":3,"parent":2,"role":"label","text":"child of focused"},
            {"id":4,"parent":1,"role":"paragraph","text":"unrelated sibling"}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let reading = tree.render(ReadMode::Focused, TokenBudget::default());
        assert!(reading.text.contains("focused here"));
        assert!(reading.text.contains("child of focused"));
        assert!(!reading.text.contains("unrelated sibling"));
        assert_eq!(reading.focused_role.as_deref(), Some("paragraph"));
    }

    #[test]
    fn focused_without_focus_falls_back_to_tree() {
        let json = r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"root"},
            {"id":2,"parent":1,"role":"label","text":"leaf"}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let focused = tree.render(ReadMode::Focused, TokenBudget::default());
        let tree_mode = tree.render(ReadMode::Tree, TokenBudget::default());
        assert_eq!(focused.text, tree_mode.text);
        assert!(focused.focused_role.is_none());
    }

    #[test]
    fn auto_uses_focused_when_present_else_tree() {
        let with_focus = sample();
        let reading = with_focus.render(ReadMode::Auto, TokenBudget::default());
        assert!(reading.text.contains("entry"));

        let json = r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"root"}
        ]}"#;
        let no_focus = A11yTree::parse(json).unwrap();
        let reading = no_focus.render(ReadMode::Auto, TokenBudget::default());
        assert_eq!(reading.text, "frame root");
    }

    #[test]
    fn auto_falls_back_to_tree_when_only_a_window_is_focused() {
        // macOS exposes the window itself as the focused element for several
        // apps; narrowing to its direct children drops the page text.
        let json = r#"{"schema":1,"source":"ax","nodes":[
            {"id":1,"parent":null,"role":"window","text":"Editor","focused":true},
            {"id":2,"parent":1,"role":"splitgroup","text":""},
            {"id":3,"parent":2,"role":"textarea","text":"Find two numbers that sum to 9"},
            {"id":4,"parent":1,"role":"button","text":"Close"}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let reading = tree.render(ReadMode::Auto, TokenBudget::default());
        assert!(reading.text.contains("Find two numbers that sum to 9"));
        assert_eq!(reading.focused_role.as_deref(), Some("window"));
    }

    #[test]
    fn deepest_focused_node_wins_over_focused_ancestors() {
        let json = r#"{"schema":1,"source":"ax","nodes":[
            {"id":1,"parent":null,"role":"window","text":"Editor","focused":true},
            {"id":2,"parent":1,"role":"group","text":"","focused":true},
            {"id":3,"parent":2,"role":"textarea","text":"focused question","focused":true}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let reading = tree.render(ReadMode::Auto, TokenBudget::default());
        assert!(reading.text.contains("focused question"));
        assert_eq!(reading.focused_role.as_deref(), Some("textarea"));
    }

    #[test]
    fn tree_mode_indents_two_spaces_per_depth() {
        let json = r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"root"},
            {"id":2,"parent":1,"role":"label","text":"child"},
            {"id":3,"parent":2,"role":"label","text":"grandchild"}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let text = tree.render(ReadMode::Tree, TokenBudget::default()).text;
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines[0], "frame root");
        assert_eq!(lines[1], "  label child");
        assert_eq!(lines[2], "    label grandchild");
    }

    #[test]
    fn tree_mode_skips_structural_roles_without_text() {
        let json = r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"root"},
            {"id":2,"parent":1,"role":"panel","text":""},
            {"id":3,"parent":2,"role":"separator","text":""},
            {"id":4,"parent":2,"role":"paragraph","text":"kept"},
            {"id":5,"parent":1,"role":"scroll pane","text":"has text"}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let text = tree.render(ReadMode::Tree, TokenBudget::default()).text;
        assert!(!text.contains("panel"));
        assert!(!text.contains("separator"));
        assert!(text.contains("paragraph kept"));
        assert!(text.contains("scroll pane has text"));
    }

    #[test]
    fn text_mode_is_flat_and_deduplicated() {
        let json = r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"alpha"},
            {"id":2,"parent":1,"role":"label","text":"beta"},
            {"id":3,"parent":1,"role":"label","text":"alpha"},
            {"id":4,"parent":1,"role":"label","name":"named only","text":""}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let text = tree.flat_text(TokenBudget::default());
        assert_eq!(text, "alpha\nbeta\nnamed only");
    }

    #[test]
    fn protected_nodes_never_render_text() {
        let json = r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"root"},
            {"id":2,"parent":1,"role":"password text","text":"hunter2","protected":true},
            {"id":3,"parent":1,"role":"label","text":"visible"}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let tree_text = tree.render(ReadMode::Tree, TokenBudget::default()).text;
        let flat = tree.flat_text(TokenBudget::default());
        assert!(!tree_text.contains("hunter2"));
        assert!(!flat.contains("hunter2"));
        assert!(tree_text.contains("visible"));
    }

    #[test]
    fn duplicate_role_text_pairs_are_emitted_once() {
        let json = r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"root"},
            {"id":2,"parent":1,"role":"label","text":"same"},
            {"id":3,"parent":1,"role":"label","text":"same"}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let text = tree.render(ReadMode::Tree, TokenBudget::default()).text;
        assert_eq!(text.matches("label same").count(), 1);
    }

    #[test]
    fn max_nodes_truncates_and_flags() {
        let tree = sample();
        let budget = TokenBudget {
            max_nodes: 1,
            ..TokenBudget::default()
        };
        let reading = tree.render(ReadMode::Tree, budget);
        assert_eq!(reading.text.lines().count(), 1);
        assert!(reading.truncated);
    }

    #[test]
    fn max_chars_truncates_and_flags() {
        let tree = sample();
        let budget = TokenBudget {
            max_chars: 5,
            ..TokenBudget::default()
        };
        let reading = tree.render(ReadMode::Tree, budget);
        assert!(reading.truncated);
        assert!(reading.text.chars().count() <= 5);
    }

    #[test]
    fn max_tokens_truncates_and_flags() {
        let tree = sample();
        let budget = TokenBudget {
            max_tokens: 1,
            ..TokenBudget::default()
        };
        let reading = tree.render(ReadMode::Tree, budget);
        assert!(reading.truncated);
    }

    #[test]
    fn max_depth_skips_deeper_nodes_but_keeps_siblings() {
        let json = r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"root"},
            {"id":2,"parent":1,"role":"label","text":"deep"},
            {"id":3,"parent":2,"role":"label","text":"deeper"},
            {"id":4,"parent":1,"role":"label","text":"sibling"}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let budget = TokenBudget {
            max_depth: 1,
            ..TokenBudget::default()
        };
        let reading = tree.render(ReadMode::Tree, budget);
        assert!(reading.text.contains("deep"));
        assert!(!reading.text.contains("deeper"));
        assert!(reading.text.contains("sibling"));
        assert!(reading.truncated);
    }

    #[test]
    fn tree_truncated_flag_propagates() {
        let json = r#"{"schema":1,"source":"atspi","truncated":true,"nodes":[
            {"id":1,"parent":null,"role":"frame","text":"root"}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        assert!(
            tree.render(ReadMode::Tree, TokenBudget::default())
                .truncated
        );
    }

    #[test]
    fn image_regions_are_largest_first_and_bounded() {
        let json = r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"root"},
            {"id":2,"parent":1,"role":"image","image":true,"bounds":[0,0,10,10]},
            {"id":3,"parent":1,"role":"canvas","image":true,"bounds":[0,0,100,100]},
            {"id":4,"parent":1,"role":"icon","image":true,"bounds":null},
            {"id":5,"parent":1,"role":"label","image":false,"bounds":[0,0,999,999]},
            {"id":6,"parent":1,"role":"password","image":true,"protected":true,"bounds":[0,0,500,500]}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let regions = tree.image_regions(TokenBudget::default());
        assert_eq!(regions.len(), 2);
        assert_eq!(regions[0].width, 100);
        assert_eq!(regions[1].width, 10);

        let bounded = tree.image_regions(TokenBudget {
            max_nodes: 1,
            ..TokenBudget::default()
        });
        assert_eq!(bounded.len(), 1);
        assert_eq!(bounded[0].width, 100);
    }

    #[test]
    fn image_region_area_and_worth_ocr() {
        let region = ImageRegion {
            x: 0,
            y: 0,
            width: 40,
            height: 30,
        };
        assert_eq!(region.area(), 1200);
        assert!(region.is_worth_ocr(1000));
        assert!(!region.is_worth_ocr(2000));

        let negative = ImageRegion {
            x: 0,
            y: 0,
            width: -5,
            height: 30,
        };
        assert_eq!(negative.area(), 0);
        assert!(!negative.is_worth_ocr(1));
        assert!(!negative.is_worth_ocr(0));
    }

    #[test]
    fn estimate_tokens_rounds_up_by_four_chars() {
        assert_eq!(estimate_tokens(""), 0);
        assert_eq!(estimate_tokens("abcd"), 1);
        assert_eq!(estimate_tokens("abcde"), 2);
        assert_eq!(estimate_tokens("hello world"), 3);
    }

    #[test]
    fn reading_reports_source_and_regions() {
        let tree = sample();
        let reading = tree.render(ReadMode::Tree, TokenBudget::default());
        assert_eq!(reading.source, "atspi");
        assert_eq!(reading.image_regions.len(), 1);
        assert_eq!(reading.image_regions[0].width, 100);
    }

    #[test]
    fn display_text_is_role_free_and_deduplicated() {
        let json = r#"{"schema":1,"source":"ax","nodes":[
            {"id":1,"parent":null,"role":"window","text":"LeetCode","focused":true},
            {"id":2,"parent":1,"role":"group","text":""},
            {"id":3,"parent":2,"role":"textarea","text":"Two Sum: find indices"},
            {"id":4,"parent":3,"role":"text","text":"Two Sum: find indices"},
            {"id":5,"parent":1,"role":"button","text":"Submit"}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let reading = tree.render(ReadMode::Auto, TokenBudget::default());
        // The model still receives the structured rendering.
        assert!(reading.text.contains("textarea"));
        // The panel sees text only, with no role labels and no duplicated node.
        assert_eq!(reading.display, "LeetCode\nTwo Sum: find indices\nSubmit");
        assert!(!reading.display.contains("textarea"));
        assert_eq!(reading.display.matches("Two Sum: find indices").count(), 1);
    }

    #[test]
    fn display_text_never_leaks_protected_text() {
        let json = r#"{"schema":1,"source":"ax","nodes":[
            {"id":1,"parent":null,"role":"window","text":"Sign in"},
            {"id":2,"parent":1,"role":"secure text","text":"hunter2","protected":true}
        ]}"#;
        let tree = A11yTree::parse(json).unwrap();
        let reading = tree.render(ReadMode::Auto, TokenBudget::default());
        assert!(!reading.display.contains("hunter2"));
        assert!(reading.display.contains("Sign in"));
    }

    #[test]
    fn text_mode_display_matches_text() {
        let tree = sample();
        let reading = tree.render(ReadMode::Text, TokenBudget::default());
        assert_eq!(reading.display, reading.text);
    }

    #[test]
    fn question_context_preserves_code_choices_and_graphics_after_long_navigation() {
        let tree = A11yTree::parse(r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"frame","text":"Browser title"},
            {"id":2,"parent":1,"role":"navigation","text":""},
            {"id":3,"parent":2,"role":"link","text":"Home Profile Account Settings Documentation Help Community"},
            {"id":4,"parent":1,"role":"document web","text":""},
            {"id":5,"parent":4,"role":"paragraph","text":"What does this function return?"},
            {"id":6,"parent":4,"role":"text","text":"def solve(x):\n    return x + 1"},
            {"id":7,"parent":4,"role":"radio button","text":"A. 2"},
            {"id":8,"parent":4,"role":"radio button","text":"B. 3"},
            {"id":9,"parent":4,"role":"paragraph","text":"Constraints: 0 <= x <= 100"},
            {"id":10,"parent":4,"role":"canvas","image":true,"bounds":[10,10,200,100]}
        ]}"#).unwrap();
        let reading = tree.render_question(ReadMode::Tree, TokenBudget::default());
        assert!(!reading.text.contains("Documentation"));
        assert!(!reading.display.contains("Browser title"));
        assert!(reading.display.contains("def solve(x):\n    return x + 1"));
        assert!(reading.display.contains("A. 2\nB. 3"));
        assert!(reading.display.contains("Constraints: 0 <= x <= 100"));
        assert_eq!(reading.image_regions.len(), 1);
        assert!(
            estimate_tokens(&reading.text)
                < estimate_tokens(&tree.render(ReadMode::Tree, TokenBudget::default()).text)
        );
    }

    #[test]
    fn question_context_does_not_mistake_toolbar_for_readable_content() {
        let tree = A11yTree::parse(
            r#"{"schema":1,"source":"ax","nodes":[
            {"id":1,"parent":null,"role":"window","text":"A very long application window title"},
            {"id":2,"parent":1,"role":"toolbar","text":""},
            {"id":3,"parent":2,"role":"button","text":"Open Save Edit View Help Settings"},
            {"id":4,"parent":1,"role":"canvas","image":true,"bounds":[0,0,800,600]}
        ]}"#,
        )
        .unwrap();
        let reading = tree.render_question(ReadMode::Tree, TokenBudget::default());
        assert!(reading.display.is_empty());
        assert_eq!(reading.image_regions.len(), 1);
    }

    #[test]
    fn question_context_excludes_protected_descendants_and_handles_cycles() {
        let tree = A11yTree::parse(
            r#"{"schema":1,"source":"atspi","nodes":[
            {"id":1,"parent":null,"role":"group","protected":true},
            {"id":2,"parent":1,"role":"text","text":"secret"},
            {"id":3,"parent":1,"role":"image","image":true,"bounds":[0,0,100,100]},
            {"id":4,"parent":5,"role":"text","text":"cycle"},
            {"id":5,"parent":4,"role":"group"},
            {"id":6,"parent":null,"role":"text","text":"Explain binary search."}
        ]}"#,
        )
        .unwrap();
        let reading = tree.render_question(ReadMode::Text, TokenBudget::default());
        assert_eq!(reading.text, "Explain binary search.");
        assert!(reading.image_regions.is_empty());
    }
}
