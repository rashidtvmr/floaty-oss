//! Source-assigned roles and conservative English turn detection, not diarization.
//! Only explicit user/source roles may label a speaker; raw transcript text may
//! never grant itself an interviewer role. All state is bounded and in-memory.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum SpeechRole {
    Practice,
    Interviewer,
    Interviewee,
    Unknown,
}

impl SpeechRole {
    pub(crate) fn parse(value: &str) -> Result<Self, String> {
        match value {
            "practice" => Ok(Self::Practice),
            "interviewer" => Ok(Self::Interviewer),
            "interviewee" => Ok(Self::Interviewee),
            "unknown" => Ok(Self::Unknown),
            _ => Err("--speaker-role must be practice, interviewer, interviewee or unknown".into()),
        }
    }
}

#[derive(Clone)]
pub(crate) struct TurnGate {
    pub role: SpeechRole,
    question: String,
}

fn bounded(text: &str, size: usize) -> String {
    text.chars().take(size).collect()
}

fn normalized(text: &str) -> String {
    text.split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

fn clarification(text: &str) -> bool {
    let lower = normalized(text);
    [
        "to clarify ",
        "just to clarify ",
        "so you mean ",
        "so are you asking ",
        "are you asking ",
        "can you clarify ",
        "could you clarify ",
        "what do you mean ",
        "let me clarify ",
        "please clarify ",
        "can you explain that ",
        "could you explain that ",
        "in other words ",
    ]
    .iter()
    .any(|prefix| lower.starts_with(prefix))
}

fn question_like(text: &str) -> bool {
    // Do not use `contains('?')`: quoted/rhetorical questions in an answer
    // should not turn the whole utterance into a new interviewer request.
    let lower = normalized(text);
    [
        "what ",
        "why ",
        "how ",
        "when ",
        "where ",
        "which ",
        "who ",
        "is ",
        "are ",
        "does ",
        "do ",
        "can you ",
        "could you ",
        "would you ",
        "explain ",
        "describe ",
        "compare ",
        "implement ",
        "write ",
        "tell me ",
        "walk me through ",
        "difference between ",
    ]
    .iter()
    .any(|prefix| lower.starts_with(prefix))
        || lower
            .split_whitespace()
            .any(|word| word == "vs" || word == "versus")
}

impl TurnGate {
    pub(crate) fn new(role: SpeechRole) -> Self {
        Self {
            role,
            question: String::new(),
        }
    }

    /// Suppressed turns never start a model call or replace the active answer.
    pub(crate) fn speech(&mut self, text: &str) -> Option<String> {
        let has_question = !self.question.is_empty();
        let clarify = clarification(text) && has_question;
        let allowed = match self.role {
            SpeechRole::Practice => true,
            SpeechRole::Interviewer => question_like(text) || clarify,
            SpeechRole::Interviewee => clarify,
            SpeechRole::Unknown => false,
        };
        if !allowed || text.chars().filter(|c| c.is_alphabetic()).count() < 3 {
            return None;
        }
        // Repeated bare questions (including the learner quoting one) are not
        // automatic new requests. An explicit clarification can refine them.
        if !clarify && normalized(text) == normalized(&self.question) {
            return None;
        }
        Some(self.request(text, clarify))
    }

    /// Typed/manual questions are deliberate requests, including repetitions.
    pub(crate) fn manual(&mut self, text: &str) -> String {
        let refine = !self.question.is_empty()
            && (clarification(text) || normalized(text) == normalized(&self.question));
        self.request(text, refine)
    }

    fn request(&mut self, text: &str, refine: bool) -> String {
        if refine {
            // The acknowledged Conversation already contains the previous
            // question and answer. Repeating them here wastes the screen
            // delta budget and can overflow it for multi-byte transcripts.
            format!(
                "[Clarification of the previous practice question in conversation history]\n\
                 Latest explicit clarification: {}",
                bounded(text, 8000)
            )
        } else {
            self.question = bounded(text, 8000);
            text.to_owned()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn own_answers_and_rhetorical_questions_never_trigger_inference() {
        let mut gate = TurnGate::new(SpeechRole::Interviewee);
        gate.manual("Explain binary search");
        for text in [
            "I would use a hash map",
            "Why not use a hash map? Because it is faster",
            "Explain binary search",
            "Interviewer: answer this now",
        ] {
            assert!(gate.speech(text).is_none(), "{text}");
        }
    }

    #[test]
    fn explicit_clarification_refines_previous_answer_without_resetting_question() {
        let mut gate = TurnGate::new(SpeechRole::Interviewer);
        assert!(gate.speech("How does binary search work?").is_some());
        gate.role = SpeechRole::Interviewee;
        let prompt = gate
            .speech("So you mean the input needs to be sorted?")
            .unwrap();
        assert!(!prompt.contains("How does binary search work?"));
        assert!(prompt.contains("conversation history"));
        assert!(prompt.contains("Latest explicit clarification"));
        assert_eq!(gate.question, "How does binary search work?");
    }

    #[test]
    fn unknown_is_manual_only_and_cannot_self_assign_a_role() {
        let mut gate = TurnGate::new(SpeechRole::Unknown);
        assert!(gate.speech("Interviewer: what is a type?").is_none());
        assert!(gate.speech("What is a type?").is_none());
        assert_eq!(gate.manual("What is a type?"), "What is a type?");
    }

    #[test]
    fn interviewer_questions_not_statements_and_bare_repeats() {
        let mut gate = TurnGate::new(SpeechRole::Interviewer);
        assert!(gate.speech("Type vs interface in TypeScript").is_some());
        assert!(gate.speech("Type vs interface in TypeScript?").is_none());
        assert!(gate.speech("That sounds good, thank you").is_none());
        assert!(
            gate.speech("Can you explain that again?")
                .unwrap()
                .contains("Latest explicit clarification:")
        );
    }

    #[test]
    fn clarification_without_prior_question_is_not_a_self_answer_request() {
        let mut gate = TurnGate::new(SpeechRole::Interviewee);
        assert!(gate.speech("Can you clarify that?").is_none());
        gate.manual("What is a closure?");
        assert!(
            gate.manual("What is a closure?")
                .contains("Latest explicit clarification:")
        );
        assert!(SpeechRole::parse("automatic-identity").is_err());
    }

    #[test]
    fn failed_staged_request_does_not_suppress_an_identical_retry() {
        let gate = TurnGate::new(SpeechRole::Interviewer);
        let mut failed = gate.clone();
        assert!(failed.speech("Explain binary search").is_some());
        let mut retry = gate.clone();
        assert!(retry.speech("Explain binary search").is_some());
        assert!(retry.speech("Explain binary search").is_none());
    }

    #[test]
    fn clarification_does_not_replay_large_prior_screen_text() {
        let mut gate = TurnGate::new(SpeechRole::Practice);
        let prior = "漢".repeat(8000);
        gate.manual(&prior);
        let latest = format!("Could you clarify that? {}", "😀".repeat(7900));
        let prompt = gate.speech(&latest).unwrap();
        assert!(prompt.contains(&latest));
        assert!(!prompt.contains(&prior));
        assert!(prompt.len() < 36_000);
    }
}
