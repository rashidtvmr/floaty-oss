# Authentication and endpoint update

## Ready

Custom OpenAI-compatible endpoints now accept API base URLs or complete chat-completions URLs, without duplicating the suffix. Gateway prefixes are preserved. An origin-only URL defaults to `/v1`.

Remote endpoints require HTTPS; HTTP is allowed only for loopback hosts. URL credentials, queries, fragments and control characters are rejected. Redirects are refused, JSON responses are limited to 2 MiB, and provider error bodies are not logged.

Both desktop shells have **List provider models** and **Test AI response** controls. Model discovery sends a GET request with no captured content. The inference test uses one fixed prompt and requires explicit confirmation because it may consume quota.

    ./socket_stream provider-models --provider openai-compatible
    ./socket_stream provider-models --provider ollama
    ./socket_stream provider-test --provider openai-compatible --confirm-inference

## Codex/ChatGPT device authorization

The native device-authorization flow is now implemented, so sign-in no longer requires the official Codex CLI. It uses only the official OpenAI endpoints (`https://auth.openai.com`), with redirects disabled, a 20 s request timeout, bounded response bodies and bounded polling.

Credentials are stored in SocketStream's own config directory (`$XDG_CONFIG_HOME/socket_stream/`, macOS `~/Library/Application Support/socket_stream/`) as `0600` on Unix. The flow never reads the Codex CLI's files and never logs tokens or user codes; `Debug` output redacts them.

    ./socket_stream codex-login --method device --native
    ./socket_stream codex-status --native
    ./socket_stream codex-logout --native --confirm

The existing CLI-backed path still works and is unchanged when `--native` is absent. A real-account sign-in and an independent security review are still outstanding, and signing in never guarantees remaining Codex credits.

## Explicit exclusions and remaining checks

No screen-share invisibility, assessment-monitoring evasion, system-process disguise, persistence, or permission bypass was added.

Endpoint regression tests and real local Ollama model discovery/inference passed. Linux GNOME/KDE UI and portal behavior still require testing on an actual Linux desktop. Sign-in alone never guarantees remaining Codex credits.
