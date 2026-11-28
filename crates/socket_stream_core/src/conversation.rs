//! Bounded, in-memory conversation history for one authorized live session.
//!
//! A pending turn is a transaction: preparing, cancelling or failing a model
//! request does not change history. Only a completed answer may be committed.
//! History is never silently evicted because a screen patch depends on its
//! earlier snapshot. The caller explicitly rebases with a fresh snapshot and
//! a bounded dialogue recap when [`Conversation::needs_checkpoint`] is true.

/// Leaves room for the fixed system instructions and transport framing within
/// the provider's 64 KB text budget. Screenshots have a separate byte limit.
pub const DEFAULT_CONTEXT_BYTES: usize = 48_000;
pub const DEFAULT_MAX_TURNS: usize = 16;
pub const DEFAULT_RESPONSE_BYTES: usize = 4_000;
const TRUNCATED_RESPONSE: &str = "\n[Earlier answer truncated for context]";

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ConversationRole {
    User,
    Assistant,
}

impl ConversationRole {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::User => "user",
            Self::Assistant => "assistant",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct ConversationMessage {
    pub role: ConversationRole,
    pub content: String,
}

#[derive(Clone, Debug)]
pub struct PendingTurn {
    session: std::sync::Arc<()>,
    revision: u64,
    prompt: String,
}

impl PendingTurn {
    pub fn prompt(&self) -> &str {
        &self.prompt
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum ConversationError {
    #[error("question must contain readable text")]
    EmptyPrompt,
    #[error("question exceeds the session context budget")]
    PromptTooLarge,
    #[error("session requires a full screen checkpoint before the next request")]
    NeedsCheckpoint,
    #[error("answer belongs to an obsolete session turn")]
    StaleTurn,
    #[error("an empty answer cannot be committed")]
    EmptyAnswer,
}

#[derive(Clone, Debug)]
pub struct Conversation {
    session: std::sync::Arc<()>,
    messages: Vec<ConversationMessage>,
    bytes: usize,
    revision: u64,
    max_context_bytes: usize,
    max_turns: usize,
    max_response_bytes: usize,
}

impl Default for Conversation {
    fn default() -> Self {
        Self::with_limits(
            DEFAULT_CONTEXT_BYTES,
            DEFAULT_MAX_TURNS,
            DEFAULT_RESPONSE_BYTES,
        )
    }
}

impl Conversation {
    /// Custom bounds are useful for small-context providers and deterministic
    /// checkpoint tests. Zero bounds reject input rather than becoming unbounded.
    pub fn with_limits(bytes: usize, turns: usize, response_bytes: usize) -> Self {
        Self {
            session: std::sync::Arc::new(()),
            messages: Vec::new(),
            bytes: 0,
            revision: 0,
            max_context_bytes: bytes,
            max_turns: turns,
            max_response_bytes: response_bytes.min(bytes),
        }
    }

    pub fn messages(&self) -> &[ConversationMessage] {
        &self.messages
    }

    pub fn byte_len(&self) -> usize {
        self.bytes
    }

    pub fn is_empty(&self) -> bool {
        self.messages.is_empty()
    }

    /// The caller must include a fresh screen snapshot after a reset; sending
    /// only the next patch would leave the provider with an orphaned delta.
    pub fn needs_checkpoint(&self, prompt: &str) -> bool {
        self.messages.len() / 2 >= self.max_turns
            || self
                .bytes
                .checked_add(prompt.trim().len())
                .and_then(|bytes| bytes.checked_add(self.max_response_bytes))
                .is_none_or(|bytes| bytes > self.max_context_bytes)
            // CLI providers replay previous roles as JSON. Escaped quotes,
            // backslashes and control characters can make valid raw history
            // exceed the transport limit before the raw-memory bound does.
            // Check the actual replay representation rather than pessimistically
            // multiplying every ordinary-text request by a worst-case factor.
            || (!self.messages.is_empty()
                && serde_json::to_string(&self.messages)
                    .expect("conversation messages are serializable")
                    .len()
                    .checked_add(prompt.trim().len())
                    .is_none_or(|bytes| bytes > self.max_context_bytes))
    }

    pub fn prepare(&self, prompt: &str) -> Result<PendingTurn, ConversationError> {
        let prompt = prompt.trim();
        if prompt.is_empty() {
            return Err(ConversationError::EmptyPrompt);
        }
        if prompt
            .len()
            .checked_add(self.max_response_bytes)
            .is_none_or(|bytes| bytes > self.max_context_bytes)
            || self.max_turns == 0
            || self.max_response_bytes == 0
        {
            return Err(ConversationError::PromptTooLarge);
        }
        if self.needs_checkpoint(prompt) {
            return Err(ConversationError::NeedsCheckpoint);
        }
        Ok(PendingTurn {
            session: self.session.clone(),
            revision: self.revision,
            prompt: prompt.to_owned(),
        })
    }

    pub fn commit(&mut self, turn: PendingTurn, answer: &str) -> Result<(), ConversationError> {
        if !std::sync::Arc::ptr_eq(&turn.session, &self.session) || turn.revision != self.revision {
            return Err(ConversationError::StaleTurn);
        }
        if answer.trim().is_empty() {
            return Err(ConversationError::EmptyAnswer);
        }
        // PendingTurn fields are private; prepare already reserved this space.
        let answer = bounded_answer(answer, self.max_response_bytes);
        self.bytes += turn.prompt.len() + answer.len();
        self.messages.push(ConversationMessage {
            role: ConversationRole::User,
            content: turn.prompt,
        });
        self.messages.push(ConversationMessage {
            role: ConversationRole::Assistant,
            content: answer,
        });
        self.revision = self.revision.wrapping_add(1);
        // Two clones may acknowledge different turns and reach equal numeric
        // revisions. Rotate the baseline identity to reject cross-branch turns.
        self.session = std::sync::Arc::new(());
        Ok(())
    }

    pub fn reset(&mut self) {
        self.messages.clear();
        self.bytes = 0;
        self.revision = self.revision.wrapping_add(1);
        self.session = std::sync::Arc::new(());
    }
}

fn bounded_answer(answer: &str, limit: usize) -> String {
    if answer.len() <= limit {
        return answer.to_owned();
    }
    let marker = if limit >= TRUNCATED_RESPONSE.len() {
        TRUNCATED_RESPONSE
    } else {
        ""
    };
    let mut end = limit - marker.len();
    while !answer.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}{marker}", &answer[..end])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn successful_turns_keep_roles_and_exact_untrusted_data() {
        let mut conversation = Conversation::default();
        let first = conversation.prepare("  snapshot: <data>  ").unwrap();
        assert!(conversation.is_empty(), "prepare must not change history");
        assert_eq!(first.prompt(), "snapshot: <data>");
        conversation.commit(first, "First answer").unwrap();
        let second = conversation.prepare("patch: \"changed\"").unwrap();
        conversation.commit(second, "Follow-up answer").unwrap();
        assert_eq!(conversation.messages().len(), 4);
        let messages = conversation.messages();
        assert_eq!(messages[0].role.as_str(), "user");
        assert_eq!(messages[1].role.as_str(), "assistant");
        assert_eq!(messages[2].content, "patch: \"changed\"");
        assert_eq!(messages[3].content, "Follow-up answer");
        assert_eq!(
            conversation.byte_len(),
            messages.iter().map(|m| m.content.len()).sum::<usize>()
        );
        assert_eq!(serde_json::to_value(&messages[0]).unwrap()["role"], "user");
    }

    #[test]
    fn abandoned_requests_and_staged_clones_do_not_mutate_the_session() {
        let original = Conversation::default();
        let pending = original.prepare("snapshot").unwrap();
        let mut staged = original.clone();
        staged.commit(pending, "answer").unwrap();
        assert!(original.is_empty());
        assert!(!staged.is_empty());
        let abandoned = staged.prepare("failed/cancelled patch").unwrap();
        drop(abandoned);
        assert_eq!(staged.messages().len(), 2);
    }

    #[test]
    fn reset_and_competing_commits_reject_stale_pending_turns() {
        let mut conversation = Conversation::default();
        let stale = conversation.prepare("old snapshot").unwrap();
        let competing = stale.clone();
        conversation.commit(competing, "first answer").unwrap();
        assert_eq!(
            conversation.commit(stale, "late answer"),
            Err(ConversationError::StaleTurn)
        );
        let pending = conversation.prepare("patch").unwrap();
        conversation.reset();
        assert_eq!(conversation.byte_len(), 0);
        assert!(conversation.is_empty());
        assert_eq!(
            conversation.commit(pending, "late answer"),
            Err(ConversationError::StaleTurn)
        );
        let fresh = conversation.prepare("fresh snapshot").unwrap();
        conversation.commit(fresh, "new answer").unwrap();
        assert_eq!(conversation.messages()[0].content, "fresh snapshot");
    }

    #[test]
    fn invalid_prompts_and_empty_answers_do_not_advance_revision() {
        let mut conversation = Conversation::with_limits(100, 2, 10);
        assert_eq!(
            conversation.prepare(" \n ").unwrap_err(),
            ConversationError::EmptyPrompt
        );
        assert_eq!(
            conversation.prepare(&"x".repeat(91)).unwrap_err(),
            ConversationError::PromptTooLarge
        );
        let turn = conversation.prepare("user").unwrap();
        assert_eq!(
            conversation.commit(turn.clone(), " \t"),
            Err(ConversationError::EmptyAnswer)
        );
        assert!(conversation.is_empty());
        conversation.commit(turn, "valid").unwrap();
        for session in [
            Conversation::with_limits(0, 1, 1),
            Conversation::with_limits(100, 0, 10),
            Conversation::with_limits(100, 1, 0),
        ] {
            assert_eq!(
                session.prepare("user").unwrap_err(),
                ConversationError::PromptTooLarge
            );
        }
    }

    #[test]
    fn checkpoints_are_explicit_and_never_drop_snapshot_dependencies() {
        let mut conversation = Conversation::with_limits(40, 3, 10);
        let turn = conversation.prepare("snapshot-with-19chrs").unwrap();
        conversation.commit(turn, "answer").unwrap();
        let before = conversation.messages().to_vec();
        assert!(conversation.needs_checkpoint("new-patch"));
        assert_eq!(
            conversation.prepare("new-patch").unwrap_err(),
            ConversationError::NeedsCheckpoint
        );
        assert_eq!(conversation.messages(), before);
        conversation.reset();
        assert!(!conversation.needs_checkpoint("new-snapshot"));
        let fresh = conversation.prepare("new-snapshot").unwrap();
        conversation.commit(fresh, "done").unwrap();
        assert_eq!(conversation.messages().len(), 2);
    }

    #[test]
    fn turn_limit_also_requires_checkpoint() {
        let mut conversation = Conversation::with_limits(1000, 1, 100);
        let turn = conversation.prepare("snapshot").unwrap();
        conversation.commit(turn, "answer").unwrap();
        assert!(conversation.needs_checkpoint("tiny"));
        assert_eq!(
            conversation.prepare("tiny").unwrap_err(),
            ConversationError::NeedsCheckpoint
        );
    }

    #[test]
    fn answer_retention_is_bounded_and_utf8_safe() {
        let mut conversation = Conversation::with_limits(200, 3, 60);
        let turn = conversation.prepare("question").unwrap();
        conversation.commit(turn, &"😀".repeat(100)).unwrap();
        let saved = &conversation.messages()[1].content;
        assert!(saved.len() <= 60);
        assert!(saved.ends_with(TRUNCATED_RESPONSE));
        assert!(saved.starts_with('😀'));
        assert_eq!(bounded_answer("😀😀", 5), "😀");
        assert_eq!(bounded_answer("😀", 1), "");
        assert_eq!(bounded_answer("x", 1), "x");
        assert_eq!(bounded_answer("😀", 0), "");
        assert_eq!(conversation.byte_len(), "question".len() + saved.len());
    }

    #[test]
    fn extreme_limits_cannot_overflow_checkpoint_math() {
        let conversation = Conversation::with_limits(usize::MAX, 5, usize::MAX);
        assert_eq!(
            conversation.prepare("one").unwrap_err(),
            ConversationError::PromptTooLarge
        );
        assert!(conversation.needs_checkpoint("one"));
    }

    #[test]
    fn pending_turns_cannot_cross_independent_sessions() {
        let a = Conversation::default();
        let mut b = Conversation::default();
        let pending = a.prepare("session A secret").unwrap();
        assert_eq!(
            b.commit(pending, "leaked answer"),
            Err(ConversationError::StaleTurn)
        );
        assert!(b.is_empty());
    }

    #[test]
    fn equally_numbered_divergent_clones_cannot_cross_commit() {
        let mut left = Conversation::default();
        let mut right = left.clone();
        let turn = left.prepare("Snapshot A").unwrap();
        left.commit(turn, "Answer A").unwrap();
        let turn = right.prepare("Snapshot B").unwrap();
        right.commit(turn, "Answer B").unwrap();
        let before = left.messages().to_vec();
        let foreign = right.prepare("Patch for B").unwrap();
        assert_eq!(
            left.commit(foreign, "Wrong baseline"),
            Err(ConversationError::StaleTurn)
        );
        assert_eq!(left.messages(), before);

        let mut reset_branch = left.clone();
        left.reset();
        reset_branch.reset();
        let foreign = reset_branch.prepare("Independent reset snapshot").unwrap();
        assert_eq!(
            left.commit(foreign, "Wrong session"),
            Err(ConversationError::StaleTurn)
        );
        assert!(left.is_empty());
    }

    #[test]
    fn escaped_history_requires_checkpoint_before_transport_replay_overflows() {
        let mut conversation = Conversation::with_limits(300, 10, 10);
        let prompt = "\"\\\u{0001}é".repeat(15);
        let first = conversation.prepare(&prompt).unwrap();
        conversation.commit(first, "Answer").unwrap();
        assert!(
            conversation.byte_len() + prompt.len() + 10 < 300,
            "raw history alone would incorrectly permit this request"
        );
        let serialized = serde_json::to_string(conversation.messages()).unwrap();
        assert!(serialized.len() + prompt.len() > 300);
        assert!(conversation.needs_checkpoint(&prompt));
        assert_eq!(
            conversation.prepare(&prompt).unwrap_err(),
            ConversationError::NeedsCheckpoint
        );
        conversation.reset();
        assert!(conversation.prepare(&prompt).is_ok());
    }

    #[test]
    fn ordinary_text_does_not_checkpoint_early_and_maximum_first_prompt_still_fits() {
        let mut conversation = Conversation::default();
        for _ in 0..DEFAULT_MAX_TURNS {
            assert!(!conversation.needs_checkpoint("Explain binary search"));
            let turn = conversation.prepare("Explain binary search").unwrap();
            conversation
                .commit(turn, "It repeatedly halves a sorted search range.")
                .unwrap();
        }
        assert!(conversation.needs_checkpoint("Follow-up"));
        conversation.reset();
        let maximum = "x".repeat(DEFAULT_CONTEXT_BYTES - DEFAULT_RESPONSE_BYTES);
        assert!(conversation.prepare(&maximum).is_ok());
    }
}
