#!/bin/sh
set -eu
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
python3 -m py_compile scripts/linux-accessibility.py scripts/linux-portal-capture.py scripts/task_queue.py scripts/dev_agent_runner.py apps/context-desktop/ui/linux-gtk.py
python3 -m unittest discover -s tests -v
if [ "$(uname -s)" = "Darwin" ]; then
  swiftc -typecheck scripts/macos-accessibility.swift
  swiftc -typecheck scripts/macos-ocr.swift
  swiftc -typecheck apps/context-desktop/ui/mac-app.swift
  swiftc -parse-as-library -typecheck scripts/macos-system-audio.swift
fi
cargo run --quiet -p context-cli -- doctor
cargo run --quiet -p context-cli -- analyze-file --file examples/sample-question.txt --provider mock
