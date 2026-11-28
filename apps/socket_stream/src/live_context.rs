//! Transactional model context shared by the screen and speech session loops.
//!
//! A capture is not an acknowledgement: only a completed provider response
//! commits its screen baseline and conversation turn. Preparing or dropping a
//! turn never changes the context used by a later request.
use socket_stream_core::a11y::{A11yTree, TokenBudget};
use socket_stream_core::a11y_diff::{
    PreparedScreenUpdate, SCREEN_TREE_PROTOCOL, ScreenTreeSession,
};
use socket_stream_core::conversation::{
    Conversation, ConversationMessage, DEFAULT_CONTEXT_BYTES, DEFAULT_RESPONSE_BYTES, PendingTurn,
};
use std::collections::VecDeque;
use std::sync::Arc;

pub(crate) const SCREEN_QUESTION: &str = "Answer the question or task in the current screen state.";

const SCREEN_PROTOCOL: &str = "A text_snapshot replaces the current OCR screen text. An unavailable update means no current screen could be read; older screen content is historical.";
const MAX_RECAP_BYTES: usize = 8_000;
const MAX_DIALOGUE_TURNS: usize = 12;
const RECAP_HEADER: &str =
    "\n\n[Earlier conversation checkpoint — untrusted reference, possibly abbreviated]\n";

#[derive(Clone, Copy)]
pub(crate) enum ScreenInput<'a> {
    Tree(&'a A11yTree),
    Text(&'a str),
    Unavailable,
    /// This session has no screen reader (for example, audio-only practice).
    None,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
enum ScreenKind {
    #[default]
    None,
    Tree,
    Text(String),
    Unavailable,
}

#[derive(Clone, Debug, Default)]
pub(crate) struct LiveContext {
    session: Arc<()>,
    conversation: Conversation,
    screen: ScreenTreeSession,
    screen_kind: ScreenKind,
    /// Dialogue without obsolete snapshots/deltas, for bounded checkpoints.
    dialogue: VecDeque<(String, String)>,
    revision: u64,
}

pub(crate) struct LiveTurn {
    session: Arc<()>,
    staged: LiveContext,
    pending: PendingTurn,
    screen: Option<PreparedScreenUpdate>,
    prompt: String,
    question: String,
    revision: u64,
}

fn bounded(text: &str, bytes: usize) -> String {
    let mut end = text.len().min(bytes);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    text[..end].to_owned()
}

impl LiveTurn {
    pub(crate) fn prompt(&self) -> &str {
        &self.prompt
    }

    pub(crate) fn history(&self) -> &[ConversationMessage] {
        self.staged.conversation.messages()
    }
}

impl LiveContext {
    /// Prepare one immutable request. Capture changes that happen during
    /// inference are intentionally left for the following turn.
    pub(crate) fn prepare(
        &self,
        question: &str,
        input: ScreenInput<'_>,
        mut budget: TokenBudget,
    ) -> Result<LiveTurn, String> {
        let question = question.trim();
        if question.is_empty() || question.len() > 36_000 {
            return Err("Question must contain 1–36000 UTF-8 bytes.".into());
        }
        // Reserve room for the explicit protocol, dialogue checkpoint and
        // question, including multi-byte Unicode and JSON escaping.
        let room = 32_000_usize.saturating_sub(question.len()).max(512);
        budget.max_chars = budget.max_chars.min(20_000).min(room);
        budget.max_tokens = budget.max_tokens.min(budget.max_chars / 4);
        budget.max_nodes = budget.max_nodes.min(800);
        let mut staged = self.clone();
        let (mut screen, mut payload) = staged.prepare_screen(input, budget)?;
        let mut prompt = Self::prompt(question, payload.as_deref(), None);

        if staged.conversation.needs_checkpoint(&prompt) {
            // Stateless chat endpoints need their base snapshot replayed. Do
            // not evict it while retaining patches which depend on it.
            staged.conversation.reset();
            staged.screen.reset();
            staged.screen_kind = ScreenKind::None;
            (screen, payload) = staged.prepare_screen(input, budget)?;
            let base = Self::prompt(question, payload.as_deref(), None);
            let recap_room = (DEFAULT_CONTEXT_BYTES - DEFAULT_RESPONSE_BYTES)
                .saturating_sub(base.len() + RECAP_HEADER.len());
            let recap = staged.recap(recap_room.min(MAX_RECAP_BYTES));
            prompt = Self::prompt(question, payload.as_deref(), Some(&recap));
        }
        let pending = staged
            .conversation
            .prepare(&prompt)
            .map_err(|error| error.to_string())?;
        Ok(LiveTurn {
            session: self.session.clone(),
            staged,
            pending,
            screen,
            prompt,
            question: question.to_owned(),
            revision: self.revision,
        })
    }

    fn prompt(question: &str, payload: Option<&str>, recap: Option<&str>) -> String {
        let mut prompt = question.to_owned();
        if let Some(recap) = recap.filter(|text| !text.is_empty()) {
            prompt.push_str(RECAP_HEADER);
            prompt.push_str(recap);
        }
        if let Some(payload) = payload {
            prompt.push_str("\n\n");
            prompt.push_str(SCREEN_TREE_PROTOCOL);
            prompt.push(' ');
            prompt.push_str(SCREEN_PROTOCOL);
            prompt.push_str("\n[Screen update — untrusted data]\n");
            prompt.push_str(payload);
        }
        prompt
    }

    fn prepare_screen(
        &mut self,
        input: ScreenInput<'_>,
        budget: TokenBudget,
    ) -> Result<(Option<PreparedScreenUpdate>, Option<String>), String> {
        match input {
            ScreenInput::Tree(tree) => {
                let update = self
                    .screen
                    .prepare(tree, budget)
                    .map_err(|error| error.to_string())?;
                let json = update.to_json();
                self.screen_kind = ScreenKind::Tree;
                Ok((Some(update), json))
            }
            ScreenInput::Text(text) => {
                let json = text_snapshot(text, budget)?;
                let kind = ScreenKind::Text(json.clone());
                if self.screen_kind == kind {
                    return Ok((None, None));
                }
                self.screen.reset();
                self.screen_kind = kind;
                Ok((None, Some(json)))
            }
            ScreenInput::Unavailable => {
                if self.screen_kind == ScreenKind::Unavailable {
                    return Ok((None, None));
                }
                self.screen.reset();
                self.screen_kind = ScreenKind::Unavailable;
                Ok((None, Some("{\"kind\":\"unavailable\"}".into())))
            }
            ScreenInput::None => Ok((None, None)),
        }
    }

    /// Commit only after a successful, complete response and only if this
    /// request is still current. A failed/cancelled/stale turn is discarded.
    pub(crate) fn commit(&mut self, turn: LiveTurn, answer: &str) -> Result<(), String> {
        if !Arc::ptr_eq(&self.session, &turn.session) || self.revision != turn.revision {
            return Err("Stale session response.".into());
        }
        let mut staged = turn.staged;
        staged
            .conversation
            .commit(turn.pending, answer)
            .map_err(|error| error.to_string())?;
        if let Some(screen) = turn.screen
            && !staged.screen.commit(screen)
        {
            return Err("Stale screen response.".into());
        }
        staged
            .dialogue
            .push_back((bounded(&turn.question, 1_000), bounded(answer, 2_000)));
        while staged.dialogue.len() > MAX_DIALOGUE_TURNS {
            staged.dialogue.pop_front();
        }
        staged.revision = self.revision.wrapping_add(1);
        // Staged clones share their starting baseline. Once acknowledged,
        // their lineage must diverge even when numeric revisions are equal.
        staged.session = Arc::new(());
        *self = staged;
        Ok(())
    }

    fn recap(&self, bytes: usize) -> String {
        let mut remaining = bytes;
        let mut turns = Vec::new();
        for (question, answer) in self.dialogue.iter().rev() {
            let text = format!("User: {question}\nAssistant: {answer}");
            let needed = text.len() + if turns.is_empty() { 0 } else { 2 };
            if needed > remaining {
                break;
            }
            remaining -= needed;
            turns.push(text);
        }
        turns.reverse();
        turns.join("\n\n")
    }
}

fn text_snapshot(text: &str, budget: TokenBudget) -> Result<String, String> {
    let max_bytes = budget.max_chars.min(budget.max_tokens.saturating_mul(4));
    let make = |text: &str, truncated| {
        serde_json::json!({
            "kind": "text_snapshot", "source": "ocr", "text": text,
            "truncated": truncated,
        })
        .to_string()
    };
    let full = make(text, false);
    if full.len() <= max_bytes {
        return Ok(full);
    }
    if make("", true).len() > max_bytes {
        return Err("Screen context budget is too small for an update.".into());
    }
    // Binary search character boundaries against the serialized size, not
    // source characters: quotes, control characters and Unicode all count.
    let mut boundaries: Vec<usize> = text.char_indices().map(|(index, _)| index).collect();
    boundaries.push(text.len());
    let (mut low, mut high) = (0, boundaries.len() - 1);
    while low < high {
        let mid = low + (high - low).div_ceil(2);
        if make(&text[..boundaries[mid]], true).len() <= max_bytes {
            low = mid;
        } else {
            high = mid - 1;
        }
    }
    Ok(make(&text[..boundaries[low]], true))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn tree(question: &str) -> A11yTree {
        A11yTree::parse(&json!({
            "schema": 1, "source": "atspi", "app": "Synthetic practice window",
            "pid": 42, "window_id": "window:42", "nodes": [
                {"id":1,"stable_id":"frame:42","parent":null,"role":"frame"},
                {"id":2,"stable_id":"question:42","parent":1,"role":"paragraph","text":question},
                {"id":3,"stable_id":"hint:42","parent":1,"role":"paragraph","text":"Retained unchanged hint"}
            ]
        }).to_string()).unwrap()
    }

    fn screen_json(turn: &LiveTurn) -> Value {
        let data = turn
            .prompt()
            .split("[Screen update — untrusted data]\n")
            .nth(1)
            .unwrap();
        serde_json::from_str(data).unwrap()
    }

    fn prepare(context: &LiveContext, tree: &A11yTree) -> LiveTurn {
        context
            .prepare(
                SCREEN_QUESTION,
                ScreenInput::Tree(tree),
                TokenBudget::default(),
            )
            .unwrap()
    }

    #[test]
    fn initial_snapshot_then_changed_node_only_with_prior_conversation() {
        let mut context = LiveContext::default();
        let first = prepare(&context, &tree("Explain binary search"));
        assert!(first.history().is_empty());
        assert_eq!(screen_json(&first)["kind"], "snapshot");
        context
            .commit(first, "Binary search halves the search range.")
            .unwrap();
        let next = prepare(&context, &tree("Explain interpolation search"));
        assert_eq!(next.history().len(), 2);
        assert!(next.history()[0].content.contains("Explain binary search"));
        assert_eq!(
            next.history()[1].content,
            "Binary search halves the search range."
        );
        let update = screen_json(&next);
        assert_eq!(update["kind"], "patch");
        assert_eq!(update["updated"].as_array().unwrap().len(), 1);
        assert!(next.prompt().contains("Explain interpolation search"));
        assert!(!next.prompt().contains("Retained unchanged hint"));
    }

    #[test]
    fn unchanged_screen_adds_no_payload_to_a_followup() {
        let mut context = LiveContext::default();
        let screen = tree("Explain binary search");
        let turn = prepare(&context, &screen);
        context.commit(turn, "It halves the range.").unwrap();
        let next = context
            .prepare(
                "What if it is unsorted?",
                ScreenInput::Tree(&screen),
                TokenBudget::default(),
            )
            .unwrap();
        assert_eq!(next.prompt(), "What if it is unsorted?");
        assert_eq!(next.history().len(), 2);
        context
            .commit(next, "Sorting or another approach is needed.")
            .unwrap();
        assert_eq!(context.conversation.messages().len(), 4);
    }

    #[test]
    fn failed_or_cancelled_preparations_leave_the_acknowledged_baseline_unchanged() {
        let mut context = LiveContext::default();
        let first_screen = tree("First question");
        let first = prepare(&context, &first_screen);
        let original = first.prompt().to_owned();
        drop(first); // cancellation before a successful response
        let retry = prepare(&context, &first_screen);
        assert_eq!(retry.prompt(), original);
        context.commit(retry, "First answer").unwrap();
        let changed = tree("Second question");
        let failed = prepare(&context, &changed);
        assert!(context.commit(failed, "  ").is_err());
        let retry = prepare(&context, &changed);
        assert_eq!(screen_json(&retry)["base_revision"], 1);
        assert_eq!(retry.history().len(), 2);
    }

    #[test]
    fn a_capture_during_inference_is_compared_to_the_completed_request() {
        let mut context = LiveContext::default();
        let screen_a = tree("Question A");
        let screen_b = tree("Question B");
        let in_flight = prepare(&context, &screen_a);
        // The reader can already see B, but A is what the model received.
        context.commit(in_flight, "Answer A").unwrap();
        let next = prepare(&context, &screen_b);
        assert_eq!(screen_json(&next)["updated"][0]["text"], "Question B");
        assert!(next.history()[0].content.contains("Question A"));
    }

    #[test]
    fn independent_and_stale_sessions_cannot_receive_each_others_turns() {
        let mut first = LiveContext::default();
        let mut second = LiveContext::default();
        let screen = tree("Question A");
        let foreign = prepare(&first, &screen);
        assert!(second.commit(foreign, "Answer").is_err());
        assert!(second.conversation.is_empty());
        let stale = prepare(&first, &screen);
        let current = prepare(&first, &screen);
        first.commit(current, "Answer").unwrap();
        assert!(first.commit(stale, "Late answer").is_err());
        assert_eq!(first.conversation.messages().len(), 2);
    }

    #[test]
    fn divergent_staged_clones_cannot_commit_equal_revision_turns_to_each_other() {
        let mut first = LiveContext::default();
        let mut second = first.clone();
        let first_turn = prepare(&first, &tree("Question A"));
        first.commit(first_turn, "Answer A").unwrap();
        let second_turn = prepare(&second, &tree("Question B"));
        second.commit(second_turn, "Answer B").unwrap();
        let cross_branch = prepare(&second, &tree("Question C"));
        assert!(first.commit(cross_branch, "Answer C").is_err());
        assert!(
            first.conversation.messages()[0]
                .content
                .contains("Question A")
        );
        let next = prepare(&first, &tree("Question A"));
        assert_eq!(
            next.prompt(),
            SCREEN_QUESTION,
            "the acknowledged tree must remain A"
        );
    }

    #[tokio::test]
    async fn serialized_history_checkpoints_before_local_provider_budget_failure() {
        use socket_stream_core::ai::{Provider, stream_answer_with_history};
        let mut context = LiveContext::default();
        let question = format!("Explain {}", "\"x\"".repeat(3_300));
        let mut saw_checkpoint = false;
        for index in 0..6 {
            let turn = context
                .prepare(&question, ScreenInput::None, TokenBudget::default())
                .unwrap();
            if index > 0 && turn.history().is_empty() {
                saw_checkpoint = true;
                assert!(turn.prompt().contains("Earlier conversation checkpoint"));
            }
            let replay_bytes =
                serde_json::to_string(turn.history()).unwrap().len() + turn.prompt().len();
            assert!(replay_bytes <= DEFAULT_CONTEXT_BYTES);
            let (output, mut received) = tokio::sync::mpsc::channel(4);
            stream_answer_with_history(
                &Provider::Mock,
                turn.prompt(),
                turn.history(),
                None,
                &output,
                None,
            )
            .await
            .expect("a prepared checkpoint must fit the provider replay budget");
            assert!(!received.try_recv().unwrap().is_empty());
            context.commit(turn, "Concise answer.").unwrap();
        }
        assert!(saw_checkpoint);
    }

    #[test]
    fn window_switch_rebases_without_losing_dialogue() {
        let mut context = LiveContext::default();
        let first = prepare(&context, &tree("Question in first window"));
        context.commit(first, "First answer").unwrap();
        let mut other = tree("Question in second window");
        other.window_id = Some("window:43".into());
        let next = prepare(&context, &other);
        assert_eq!(screen_json(&next)["kind"], "snapshot");
        assert_eq!(screen_json(&next)["reason"], "window");
        assert_eq!(next.history().len(), 2);
    }

    #[test]
    fn unavailable_then_restored_screen_gets_a_fresh_snapshot() {
        let mut context = LiveContext::default();
        let screen = tree("Question in current window");
        let turn = prepare(&context, &screen);
        context.commit(turn, "Answer").unwrap();
        let unavailable = context
            .prepare(
                "What changed?",
                ScreenInput::Unavailable,
                TokenBudget::default(),
            )
            .unwrap();
        assert_eq!(screen_json(&unavailable)["kind"], "unavailable");
        context
            .commit(unavailable, "A fresh read is needed.")
            .unwrap();
        let unchanged = context
            .prepare(
                "Please explain the earlier answer.",
                ScreenInput::Unavailable,
                TokenBudget::default(),
            )
            .unwrap();
        assert!(!unchanged.prompt().contains("[Screen update"));
        let restored = prepare(&context, &screen);
        assert_eq!(screen_json(&restored)["kind"], "snapshot");
    }

    #[test]
    fn ocr_snapshots_are_deduplicated_and_switch_back_to_tree_safely() {
        let mut context = LiveContext::default();
        let budget = TokenBudget::default();
        let first = context
            .prepare("Read it", ScreenInput::Text("OCR question"), budget)
            .unwrap();
        assert_eq!(screen_json(&first)["text"], "OCR question");
        assert_eq!(screen_json(&first)["truncated"], false);
        context.commit(first, "Answer").unwrap();
        let next = context
            .prepare("Explain more", ScreenInput::Text("OCR question"), budget)
            .unwrap();
        assert_eq!(next.prompt(), "Explain more");
        context.commit(next, "More detail").unwrap();
        let restored = prepare(&context, &tree("Native question"));
        assert_eq!(screen_json(&restored)["kind"], "snapshot");
    }

    #[test]
    fn ocr_bounds_count_json_escaping_utf8_and_truncation_metadata() {
        let budget = TokenBudget {
            max_chars: 180,
            max_tokens: 45,
            ..TokenBudget::default()
        };
        for text in ["\u{0001}\n\"\\".repeat(200), "你好🪴".repeat(200)] {
            let json = text_snapshot(&text, budget).unwrap();
            assert!(json.len() <= 180);
            let value: Value = serde_json::from_str(&json).unwrap();
            assert_eq!(value["truncated"], true);
            assert!(text.starts_with(value["text"].as_str().unwrap()));
        }
        let mut context = LiveContext::default();
        let original = "x".repeat(500);
        let first = context
            .prepare("Read it", ScreenInput::Text(&original), budget)
            .unwrap();
        let prefix = screen_json(&first)["text"].as_str().unwrap().to_owned();
        context.commit(first, "Answer").unwrap();
        let shortened = context
            .prepare(
                "Read it",
                ScreenInput::Text(&prefix),
                TokenBudget::default(),
            )
            .unwrap();
        assert_eq!(screen_json(&shortened)["truncated"], false);
    }

    #[test]
    fn checkpoint_preserves_recent_dialogue_and_replaces_orphaned_patches() {
        let mut context = LiveContext {
            conversation: Conversation::with_limits(48_000, 2, 4_000),
            ..LiveContext::default()
        };
        for (question, answer) in [
            ("First question", "First explanation"),
            ("Second question", "Second explanation"),
        ] {
            let screen = tree(question);
            let turn = context
                .prepare(question, ScreenInput::Tree(&screen), TokenBudget::default())
                .unwrap();
            context.commit(turn, answer).unwrap();
        }
        let screen = tree("Third question");
        let checkpoint = context
            .prepare(
                "How does this compare?",
                ScreenInput::Tree(&screen),
                TokenBudget::default(),
            )
            .unwrap();
        assert!(checkpoint.history().is_empty());
        assert_eq!(screen_json(&checkpoint)["kind"], "snapshot");
        assert!(checkpoint.prompt().contains("First explanation"));
        assert!(checkpoint.prompt().contains("Second explanation"));
        assert!(!checkpoint.prompt().contains("\"base_revision\""));
        // Cancellation of compaction leaves the old acknowledged baseline.
        drop(checkpoint);
        assert_eq!(context.conversation.messages().len(), 4);
        let retry = prepare(&context, &screen);
        context.commit(retry, "Comparison").unwrap();
        assert_eq!(context.conversation.messages().len(), 2);
    }

    #[test]
    fn long_session_and_checkpoint_recap_stay_within_provider_bounds() {
        let mut context = LiveContext::default();
        for index in 0..80 {
            let question = format!("Question {index}: {}", "🪴".repeat(220));
            let screen = tree(&question);
            let turn = context
                .prepare(
                    &question,
                    ScreenInput::Tree(&screen),
                    TokenBudget::default(),
                )
                .unwrap();
            let request_bytes = turn.prompt().len()
                + turn
                    .history()
                    .iter()
                    .map(|message| message.content.len())
                    .sum::<usize>();
            assert!(request_bytes <= 44_000);
            context
                .commit(turn, &"Long explanation 🪴 ".repeat(250))
                .unwrap();
            assert!(context.conversation.byte_len() <= DEFAULT_CONTEXT_BYTES);
            assert!(context.dialogue.len() <= MAX_DIALOGUE_TURNS);
        }
        assert!(context.recap(MAX_RECAP_BYTES).len() <= MAX_RECAP_BYTES);
        assert!(context.recap(0).is_empty());
    }

    #[test]
    fn large_manual_question_reduces_checkpoint_recap_to_fit() {
        let mut context = LiveContext {
            conversation: Conversation::with_limits(48_000, 1, 4_000),
            ..LiveContext::default()
        };
        let first = context
            .prepare(
                "Earlier question",
                ScreenInput::None,
                TokenBudget::default(),
            )
            .unwrap();
        context
            .commit(first, &"Earlier detailed explanation. ".repeat(100))
            .unwrap();
        let question = "x".repeat(36_000);
        let next = context
            .prepare(
                &question,
                ScreenInput::Text("Current context"),
                TokenBudget::default(),
            )
            .unwrap();
        assert!(next.prompt().len() <= 44_000);
        assert_eq!(screen_json(&next)["text"], "Current context");
    }

    #[test]
    fn audio_only_dialogue_never_invents_a_screen_read() {
        let mut context = LiveContext::default();
        let first = context
            .prepare(
                "Explain closures",
                ScreenInput::None,
                TokenBudget::default(),
            )
            .unwrap();
        assert_eq!(first.prompt(), "Explain closures");
        context
            .commit(first, "A closure retains its lexical environment.")
            .unwrap();
        let followup = context
            .prepare("Give an example", ScreenInput::None, TokenBudget::default())
            .unwrap();
        assert_eq!(followup.prompt(), "Give an example");
        assert!(
            followup.history()[1]
                .content
                .contains("lexical environment")
        );
    }

    #[test]
    fn invalid_input_and_impossible_budgets_fail_without_changing_context() {
        let context = LiveContext::default();
        let normal = TokenBudget::default();
        assert!(context.prepare(" \n ", ScreenInput::None, normal).is_err());
        assert!(
            context
                .prepare(&"x".repeat(36_001), ScreenInput::None, normal)
                .is_err()
        );
        let tiny = TokenBudget {
            max_chars: 1,
            max_tokens: 1,
            ..normal
        };
        assert!(
            context
                .prepare("Read it", ScreenInput::Text("data"), tiny)
                .is_err()
        );
        assert!(
            context
                .prepare("Read it", ScreenInput::Tree(&tree("data")), tiny)
                .is_err()
        );
        let mut malformed = tree("data");
        malformed.nodes[0].parent = Some(999);
        assert!(
            context
                .prepare("Read it", ScreenInput::Tree(&malformed), normal)
                .is_err()
        );
        assert!(context.conversation.is_empty());
    }

    #[test]
    fn dialogue_boundary_clipping_is_utf8_safe_and_explicitly_abbreviated() {
        assert_eq!(bounded("🪴éx", 5), "🪴");
        assert_eq!(bounded("🪴", 0), "");
        let mut context = LiveContext {
            conversation: Conversation::with_limits(48_000, 1, 4_000),
            ..LiveContext::default()
        };
        let turn = context
            .prepare(&"🪴".repeat(400), ScreenInput::None, TokenBudget::default())
            .unwrap();
        context.commit(turn, &"é".repeat(2_000)).unwrap();
        assert!(context.dialogue[0].0.len() <= 1_000);
        assert!(context.dialogue[0].1.len() <= 2_000);
        let next = context
            .prepare("Continue", ScreenInput::None, TokenBudget::default())
            .unwrap();
        assert!(next.prompt().contains("possibly abbreviated"));
        assert!(!next.prompt().contains("[Screen update"));
    }
}
