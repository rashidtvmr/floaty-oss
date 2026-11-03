//! Explicit session consent. Capture APIs are gated on Active, not merely on launch.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionState {
    Stopped,
    Active,
    Paused,
}

#[derive(Debug)]
pub struct PracticeSession {
    state: SessionState,
    explicitly_authorized: bool,
}

impl Default for PracticeSession {
    fn default() -> Self {
        Self::new()
    }
}

impl PracticeSession {
    pub fn new() -> Self {
        Self {
            state: SessionState::Stopped,
            explicitly_authorized: false,
        }
    }

    pub fn start(&mut self, authorized: bool) -> Result<(), &'static str> {
        if !authorized {
            return Err("screen capture requires explicit consent");
        }
        self.explicitly_authorized = true;
        self.state = SessionState::Active;
        Ok(())
    }

    pub fn state(&self) -> SessionState {
        self.state
    }

    pub fn may_capture(&self) -> bool {
        self.state == SessionState::Active && self.explicitly_authorized
    }

    pub fn pause(&mut self) {
        if self.state == SessionState::Active {
            self.state = SessionState::Paused;
        }
    }

    pub fn resume(&mut self) {
        if self.state == SessionState::Paused && self.explicitly_authorized {
            self.state = SessionState::Active;
        }
    }

    pub fn stop(&mut self) {
        self.state = SessionState::Stopped;
        self.explicitly_authorized = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn require_consent_and_revoke_after_stop() {
        let mut s = PracticeSession::new();
        assert!(!s.may_capture());
        assert!(s.start(false).is_err());
        assert!(!s.may_capture());
        s.start(true).unwrap();
        assert!(s.may_capture());
        s.pause();
        assert!(!s.may_capture());
        s.resume();
        assert!(s.may_capture());
        s.stop();
        assert!(!s.may_capture());
        s.resume();
        assert!(!s.may_capture());
    }
}
