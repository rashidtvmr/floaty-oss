//! Normalize and deduplicate screen text before any model call.
use std::collections::VecDeque;
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

pub struct Deduplicator {
    seen: VecDeque<u64>,
    capacity: usize,
}

impl Deduplicator {
    pub fn new(capacity: usize) -> Self {
        Self {
            seen: VecDeque::new(),
            capacity: capacity.max(1),
        }
    }

    /// Return true only for genuinely new content.
    pub fn should_process(&mut self, text: &str) -> bool {
        if text.trim().is_empty() {
            return false;
        }
        let key = fingerprint(text);
        if self.seen.contains(&key) {
            return false;
        }
        self.seen.push_back(key);
        if self.seen.len() > self.capacity {
            self.seen.pop_front();
        }
        true
    }
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
}
