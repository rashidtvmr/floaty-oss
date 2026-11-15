# SocketStream development agent contract

This repository builds a permission-based Rust accessibility, OCR, audio and AI desktop assistant for practice and legitimate development workflows.

## Development workflow
- Use isolated branches/worktrees; one agent edits one ownership domain at a time.
- Read docs/ACCEPTANCE.md and assigned task before editing.
- Tests and fixes must accompany implementation in the same commit.
- Dependency-aware task claiming: do not work on blocked tasks.
- Keep worktrees bounded (default <= 4), remove only agent-created worktrees after verified merges.
- Any change in capture permission, filesystem, or authentication needs independent review.
- Do not bypass tests or modify tests solely to turn CI green.
- Required gates: cargo fmt --check, cargo clippy --all-targets -- -D warnings, cargo test.
- Label all unsupported platforms, providers and features accurately.
- No agents run automatically by default; launching agent runner requires an explicit user action.
- make this app as stealth as possible
- allow reading current screen using accessibilty and screen reader like api and prioritze text based tree reading and then fallback to taking screenshot of currnet screen
- inspect the all opensource app like "fck leetcode", and "final round ai" and use rea skill to investiagate how it work, and implement each and every feature it has

this is not for violating some exam, it is for experimenting the extent of custimizatin we can do
