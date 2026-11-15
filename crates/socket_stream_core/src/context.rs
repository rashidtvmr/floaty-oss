//! Normalize and deduplicate screen text before any model call.
use std::collections::{HashMap, HashSet, VecDeque};
use std::hash::{Hash, Hasher};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScreenContext {
    pub text: String,
    pub origin: String,
    pub confidence: Option<u8>,
}

impl ScreenContext {
    pub fn new(text: impl Into<String>, origin: impl Into<String>) -> Self {
        Self {
            text: normalize_text(&text.into()),
            origin: origin.into(),
            confidence: None,
        }
    }
}

pub fn normalize_text(input: &str) -> String {
    input
        .replace("\r\n", "\n")
        .lines()
        .map(str::trim_end)
        .collect::<Vec<_>>()
        .join("\n")
        .trim_end_matches('\n')
        .to_owned()
}

pub fn fingerprint(input: &str) -> u64 {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    normalize_text(input).hash(&mut hasher);
    hasher.finish()
}

/// A rendered accessibility tree is a long, line-oriented outline. Real pages
/// keep mutating one or two lines on their own — a countdown, a rotating hint,
/// a live "N users" badge — while the question and its options stay put. Below
/// this many lines the input is treated as a short, hand-written string and
/// only exact repeats are deduplicated.
const VOLATILE_MIN_LINES: usize = 40;

/// Only lines at least this long feed the content signature. A question and its
/// options are long; timers, buttons, tab titles and rotating hints are short,
/// so this alone discards most self-updating chrome before any learning.
const SIGNATURE_MIN_LINE_CHARS: usize = 32;

/// How many recent observations are inspected when deciding whether a long
/// line is self-updating.
const VOLATILE_HISTORY: usize = 4;

/// A line index that changed in at least this many recent observations is
/// treated as self-updating chrome and excluded from the content signature. A
/// one-off change — the user moving to a new question — stays in the signature.
const VOLATILE_MIN_CHANGES: usize = 2;

pub struct Deduplicator {
    answered: VecDeque<u64>,
    capacity: usize,
    previous: Vec<String>,
    /// Line indices that changed in each of the most recent observations.
    history: VecDeque<Vec<usize>>,
    last_signature: Option<u64>,
}

impl Deduplicator {
    pub fn new(capacity: usize) -> Self {
        Self {
            answered: VecDeque::new(),
            capacity: capacity.max(1),
            previous: Vec::new(),
            history: VecDeque::new(),
            last_signature: None,
        }
    }

    /// Update the volatility history from a fresh reading and return its
    /// content signature plus whether that signature matches the previous
    /// observation (i.e. the screen has stopped animating).
    ///
    /// Callers that must keep learning while a model call is in flight can call
    /// this every tick and gate only the answer on [`Self::is_new`].
    pub fn observe(&mut self, text: &str) -> (u64, bool) {
        let normalized = normalize_text(text);
        let lines: Vec<&str> = normalized.lines().collect();
        let volatile = self.track(&lines);
        let signature = if lines.len() >= VOLATILE_MIN_LINES {
            content_signature(&lines, &volatile)
        } else {
            fingerprint(&normalized)
        };
        // Short inputs never wait for stability; they are compared exactly.
        let stable = lines.len() < VOLATILE_MIN_LINES || self.last_signature == Some(signature);
        self.last_signature = Some(signature);
        (signature, stable)
    }

    /// Whether this signature has not been answered yet.
    pub fn is_new(&self, signature: u64) -> bool {
        !self.answered.contains(&signature)
    }

    /// Record that a signature has been answered.
    pub fn remember(&mut self, signature: u64) {
        self.answered.push_back(signature);
        if self.answered.len() > self.capacity {
            self.answered.pop_front();
        }
    }

    /// Return true only for genuinely new, settled content.
    ///
    /// A reading is processed once it is new and has repeated unchanged since
    /// the previous observation. This answers a page once, even when a timer or
    /// a rotating hint keeps changing, while a genuine change to the question
    /// still produces a new signature once it settles.
    pub fn should_process(&mut self, text: &str) -> bool {
        if text.trim().is_empty() {
            return false;
        }
        let (signature, stable) = self.observe(text);
        if !stable || !self.is_new(signature) {
            return false;
        }
        self.remember(signature);
        true
    }

    /// Record which line indices changed since the previous observation and
    /// return the indices that look self-updating.
    fn track(&mut self, lines: &[&str]) -> HashSet<usize> {
        if self.previous.len() == lines.len() && !self.previous.is_empty() {
            let changed: Vec<usize> = lines
                .iter()
                .enumerate()
                .filter(|(index, line)| self.previous[*index] != **line)
                .map(|(index, _)| index)
                .collect();
            self.history.push_back(changed);
            if self.history.len() > VOLATILE_HISTORY {
                self.history.pop_front();
            }
        } else {
            // A different number of lines is a different screen, not a tick.
            self.history.clear();
        }
        self.previous = lines.iter().map(|line| (*line).to_owned()).collect();
        let mut counts: HashMap<usize, usize> = HashMap::new();
        for transition in &self.history {
            for &index in transition {
                *counts.entry(index).or_default() += 1;
            }
        }
        counts
            .into_iter()
            .filter(|(_, count)| *count >= VOLATILE_MIN_CHANGES)
            .map(|(index, _)| index)
            .collect()
    }
}

/// Hash the long lines that are not known to be self-updating. The lines are
/// sorted so a question is recognised regardless of where it sits in the tree.
/// If nothing is long enough, every non-volatile line is used instead.
fn content_signature(lines: &[&str], volatile: &HashSet<usize>) -> u64 {
    let long_lines = |minimum: usize| -> Vec<&str> {
        lines
            .iter()
            .enumerate()
            .filter(|(index, line)| {
                !volatile.contains(index) && line.trim().chars().count() >= minimum
            })
            .map(|(_, line)| *line)
            .collect()
    };
    let mut selected = long_lines(SIGNATURE_MIN_LINE_CHARS);
    if selected.is_empty() {
        selected = long_lines(1);
    }
    selected.sort_unstable();
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    for line in selected {
        line.hash(&mut hasher);
    }
    hasher.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_windows_newlines_without_removing_code_indent() {
        assert_eq!(
            normalize_text("  code()  \r\n  next() \r\n"),
            "  code()\n  next()"
        );
    }

    #[test]
    fn duplicate_text_does_not_trigger_twice() {
        let mut d = Deduplicator::new(2);
        assert!(d.should_process("foo"));
        assert!(!d.should_process("foo  "));
        assert!(!d.should_process("  "));
        assert!(d.should_process("bar"));
        assert!(d.should_process("baz"));
        assert!(d.should_process("foo"));
    }

    #[test]
    fn dedup_capacity_never_zero() {
        let mut d = Deduplicator::new(0);
        assert!(d.should_process("one"));
        assert!(d.should_process("two"));
        assert!(d.should_process("one"));
    }

    /// A 40+ line outline with one self-updating line and one question line.
    fn tree_with(volatile_line: &str, question: &str) -> String {
        let mut text = String::from("window Practice\n  group\n");
        for index in 0..50 {
            text.push_str(&format!("  statictext filler {index}\n"));
        }
        text.push_str(&format!("  statictext {volatile_line}\n"));
        text.push_str(&format!("  statictext {question}\n"));
        text
    }

    #[test]
    fn a_rotating_hint_is_answered_once_and_a_new_question_after_settling() {
        let mut d = Deduplicator::new(8);
        // The first sighting establishes the signature but is not yet settled.
        assert!(!d.should_process(&tree_with("Hint one", "What is binary search?")));
        // The hint keeps rotating, but it is short chrome and does not change
        // the signature, so the screen is now settled and answered once.
        assert!(d.should_process(&tree_with("Hint two", "What is binary search?")));
        assert!(
            !d.should_process(&tree_with("Hint three", "What is binary search?")),
            "a rotating hint must not look like a new question"
        );
        assert!(!d.should_process(&tree_with("Hint four", "What is binary search?")));
        // A genuinely different question is a new signature, answered once it
        // has been seen twice.
        assert!(!d.should_process(&tree_with("Hint five", "What is a hash map?")));
        assert!(d.should_process(&tree_with("Hint six", "What is a hash map?")));
        assert!(!d.should_process(&tree_with("Hint seven", "What is a hash map?")));
    }

    #[test]
    fn observe_reports_stability_without_consuming_the_signature() {
        let mut d = Deduplicator::new(8);
        let first = tree_with("Hint one", "What is binary search?");
        let second = tree_with("Hint two", "What is binary search?");
        let (signature, stable) = d.observe(&first);
        assert!(!stable, "first sighting is not yet settled");
        assert!(d.is_new(signature));
        let (again, stable) = d.observe(&second);
        assert_eq!(
            signature, again,
            "rotating hint must not change the signature"
        );
        assert!(stable, "second sighting is settled");
        assert!(d.is_new(again));
        d.remember(again);
        assert!(!d.is_new(again));
    }

    #[test]
    fn short_texts_are_deduplicated_exactly() {
        let mut d = Deduplicator::new(8);
        assert!(d.should_process("What is 2 + 2?"));
        assert!(!d.should_process("What is 2 + 2? "));
        assert!(d.should_process("What is 3 + 3?"));
    }
}
