# macOS build and verification

**Target:** macOS 13 or newer on Apple silicon or Intel. Development and the
verification below were done on macOS 27 with Swift 6.4.2.

SocketStream on macOS is a self-contained AppKit app plus the existing Rust CLI.
The app reads accessibility text first and uses Apple Vision OCR only when the
user enables the fallback and the accessibility text is insufficient.

## 1. Status: verified vs. still unverified

Verified on the installed bundle (`~/Applications/SocketStream.app`,
`dev.socket_stream.app`):

- The main window, provider selection, permission status, consent alert,
  observation start/stop, and the live answer panel.
- Accessibility text-tree reading from Terminal and Google Chrome, rooted at
  the focused window and skipping the global menu bar.
- Live question/answer updates from `observe-live` JSONL events with the mock
  provider.
- Panel controls: Compact / Expand, Pause / Resume, Copy, Close, Main,
  Stop practice.
- The panel stays visible when the main window is minimized and when another
  app becomes focused.
- The panel floats above a fullscreen app (Google Chrome) while keeping its
  `.statusBar` level and `.fullScreenAuxiliary` behavior.
- `NSWindow.sharingType = .none` hides the panel from `screencapture`; toggling
  stealth off with the Shortcuts menu makes it appear in the same capture.
- `socket_stream overlay --provider mock --stealth` launches the AppKit panel in
  `--panel-only` mode; Stop exits the app.
- `socket_stream_gui --self-test` replays the schema-1 JSONL contract.

Not yet verified:

- Exclusion from a **real remote screen share** (RustDesk, Meet, Zoom, Teams).
  `screencapture` honours `sharingType = .none`; that is evidence, not a
  universal guarantee. macOS documents the property as a hint and some capture
  paths may still include the window. Test the actual share product before
  claiming invisibility.
- A Developer ID signature and notarization. The bundle is signed with a local
  self-signed identity so permission grants survive rebuilds, but it is not
  notarized and not distributable.
- System-audio capture and microphone capture from the installed bundle.
- Intel Macs (the local build is arm64).

## 2. Requirements

- macOS 13+ with Xcode Command Line Tools (`swiftc`, `codesign`, `plutil`).
- Rust toolchain with Cargo.
- Python 3 is not required for the macOS panel; it is still used by the Linux
  overlay and tests.
- Optional for real answers: Ollama, an OpenAI-compatible endpoint and key, or
  the Codex CLI.
- Optional for audio practice: `ffmpeg` and a local Whisper `.bin` model.

## 3. Build and install

```sh
./scripts/build-macos-app.sh              # release build, installs to ~/Applications
./scripts/build-macos-app.sh --no-install # builds dist/SocketStream.app only
./scripts/build-macos-app.sh --debug      # debug CLI profile
```

The bundle layout is:

```
SocketStream.app/Contents/
  MacOS/
    SocketStream        # AppKit shell
    socket_stream        # Rust core
  Resources/
    target/native-helpers/
      socket_stream_a11y
      socket_stream_ocr
      socket_stream_system_audio
    scripts/           # Swift helper sources (fallback)
    models/            # Whisper model when present
  Info.plist
```

The script signs the bundle with a stable local code-signing identity when one
exists (`scripts/create-macos-signing-cert.sh`), and falls back to ad-hoc
signing with a warning otherwise. It also signs the helpers under
`Contents/Resources/target/native-helpers/` explicitly, because
`codesign --deep` only signs code in the standard nested-code locations. There
is no Developer ID identity on this machine. A future signed/notarized release
should replace the local identity without changing the layout.

Open the installed app with:

```sh
open -n "$HOME/Applications/SocketStream.app"
```

`./scripts/start-macos.sh` opens the installed bundle by default. Use
`./scripts/start-macos.sh --dev` to compile and run the development GUI instead.

## 4. Permissions

macOS grants permissions to a specific signed app/path, not to the project
directory. The development binary and the installed bundle are separate TCC
identities, so granting one does not grant the other.

1. Launch SocketStream.
2. The main window shows two status labels:
   - **Accessibility (text trees)** — required for the primary reading path.
   - **Screen Recording (OCR fallback)** — required only for the screenshot +
     Vision OCR fallback, and for `screencapture`-based tests.
3. Click **Request permissions**. The app calls the documented
   `AXIsProcessTrustedWithOptions` and `CGRequestScreenCaptureAccess` APIs; it
   never edits the TCC database and never bypasses a prompt.
4. Grant the toggles in System Settings. On macOS 27 the consolidated
   **Device Control and Data Access** pane may list SocketStream; the dedicated
   **Privacy & Security > Screen & System Audio Recording** pane is where a
   screenshot-based app appears. Use the pane's **+** button to add the app if
   the automatic prompt did not add it.
5. Quit and reopen SocketStream after changing Screen Recording. The pane offers
   **Quit & Reopen** for this.
6. Confirm both labels read **granted**.

`NSMicrophoneUsageDescription` and `NSAudioCaptureUsageDescription` are in the
bundle's `Info.plist`. macOS asks at first use when an audio practice session is
started; audio permissions are not requested at launch.

If macOS resets permissions after a rebuild, the label returns to **not
granted**. Use **Repair permissions** in the app, or
`scripts/reset-macos-permissions.sh`, to clear the stale entry, then grant
again. Building with a stable signing identity makes the grant survive rebuilds.

### Why "enabled" in System Settings can still mean "not granted"

macOS TCC does not store "this app is allowed". It stores a *code signing
requirement* (`csreq`) and re-evaluates it on every permission check. With
ad-hoc signing (`codesign --sign -`) the stored requirement is
`cdhash H"..."`, a hash of the built binary. Every rebuild changes the cdhash,
so the stored requirement stops matching the running app: TCC silently denies
and the app reports **not granted**, while the System Settings toggle still
shows as enabled because the old row remains.

You can see the mismatch directly:

```sh
# what TCC stored at grant time (hex blob; the last 20 bytes are the cdhash)
sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" \
  "select service, client, auth_value, hex(csreq) from access where client='dev.socket_stream.app';"

# what the current build actually is
codesign -d -r- "$HOME/Applications/SocketStream.app"
```

Ad-hoc builds show `designated => cdhash H"..."`; builds signed with the local
identity show `designated => identifier "dev.socket_stream.app" and certificate
root = H"..."`, which is stable across rebuilds.

The fix is a stable local signing identity. It does not need an Apple Developer
account; TCC only compares the certificate hash embedded in the signature:

```sh
./scripts/create-macos-signing-cert.sh   # once
./scripts/build-macos-app.sh             # now signs with it
./scripts/reset-macos-permissions.sh     # clear any stale rows
```

Remove the identity later with
`security delete-identity -c "SocketStream Local Signing"`.

Running the app executable manually from Terminal is not definitive proof of
the installed app's TCC identity: macOS attributes that process to the
responsible process (the terminal), which may already hold Accessibility.
Likewise, `agent-desktop permissions` reports permissions for the process that
launched `agent-desktop`; it does not report SocketStream's grants. Use
`agent-desktop snapshot --app "SocketStream"` only to inspect the app's exposed
UI tree, not as evidence that SocketStream itself can read another app.

## 5. Running a practice session

1. Select a provider (`mock` is offline and makes no AI call).
2. Set the interval (2–3600 seconds; 4 is the default).
3. Leave **Local OCR fallback** off unless you explicitly want a screenshot
   fallback. Text-tree reading does not require it.
4. Click **Start observation** and confirm the consent alert.
5. Focus the application you want to read (Terminal, a browser, an editor).
   The panel shows the detected question and the provider answer.
6. Use **Stop practice** to end observation. Closing the main window does not
   stop observation; closing the panel hides only the panel.

### Panel-only mode

```sh
"$HOME/Applications/SocketStream.app/Contents/MacOS/socket_stream" \
  overlay --provider mock --stealth
```

This launches the AppKit app with `--panel-only`, starts an explicitly
authorized observation, and exits when you press **Stop practice**. The same
flags as the Linux overlay are accepted where they make sense:
`--provider`, `--interval`, `--allow-ocr`, `--stealth`, `--no-stealth`,
`--compact`, `--opacity`, `--cli`. Linux-only flags such as `--portal` are not
invented for the macOS panel.

### JSONL producer

The panel consumes the same schema-1 events as the Linux panel:

```sh
"$HOME/Applications/SocketStream.app/Contents/MacOS/socket_stream" \
  observe-live --authorized --interval 4 --provider mock
```

`socket_stream` reads from stdin for `pause`, `resume`, and `stop`. The app keeps
those control lines internal; they are not typed into the practice window.

## 6. Panel behavior

- `NSPanel` at the `.statusBar` level with `.fullScreenAuxiliary`,
  `.canJoinAllSpaces`, and `.nonactivatingPanel`.
- `hidesOnDeactivate = false`; focus changes never hide it.
- `sharingType = .none` while stealth is on; toggling stealth off sets
  `.readOnly` for comparison.
- `alphaValue = 0.94` by default (`--opacity` accepts 0.85–1.0), with a
  near-opaque content backing so answers stay readable over busy windows.
- The panel is movable by its background and its position is saved between
  launches (`SocketStreamAnswerPanel`). If the saved position is off-screen it
  is centered again.
- Only **Close** / **Hide answer panel** hides it. The observation session
  keeps running and **Show answer panel** restores it.
- **Main** restores the main window even when it is minimized.
- **Copy** writes the current answer to the pasteboard.

## 7. Capture-exclusion verification

A repeatable local check:

1. Leave **stealth on** (the default) and make the panel show a unique canary
   line, for example by running the mock provider.
2. Capture the screen:

   ```sh
   screencapture -x /tmp/socket_stream_stealth-on.png
   ```

   The panel should be absent from the image while it is visible on the
   display.
3. Toggle stealth off from the app's **Shortcuts** menu
   (**Toggle stealth (capture exclusion)**).
4. Capture again:

   ```sh
   screencapture -x /tmp/socket_stream_stealth-off.png
   ```

   The panel should now appear.
5. Inspect the window level and alpha without relying on pixels:

   ```sh
   swift - <<'SWIFT'
   import CoreGraphics
   let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                         kCGNullWindowID) as? [[String: Any]] ?? []
   for window in list
   where (window[kCGWindowOwnerName as String] as? String ?? "").contains("SocketStream") {
       let name = window[kCGWindowName as String] as? String ?? ""
       let layer = window[kCGWindowLayer as String] as? Int ?? -1
       let alpha = window[kCGWindowAlpha as String] as? Double ?? -1
       print("\(name) layer=\(layer) alpha=\(alpha)")
   }
   SWIFT
   ```

   The answer panel reports layer 25 and alpha 0.94.

This proves exclusion from `screencapture` (and by extension capture paths that
use the same window-sharing rules). It does **not** prove exclusion from every
screen-share product. Record the actual product and result in
`docs/STEALTH-OVERLAY.md` before claiming a real-share guarantee.

## 8. End-to-end checklist

- [ ] `./scripts/build-macos-app.sh` succeeds and installs the bundle.
- [ ] `"$HOME/Applications/SocketStream.app/Contents/MacOS/SocketStream" --self-test` prints `macOS panel self-test: ok`.
- [ ] Both permission labels read **granted** after a restart.
- [ ] `socket_stream observe-live --authorized --interval 4 --provider mock` emits schema-1 JSONL and exits on `stop`.
- [ ] Starting observation opens the panel and shows the detected question.
- [ ] Focus changes do not hide the panel.
- [ ] Minimizing the main window does not hide the panel.
- [ ] The panel remains visible over a fullscreen app.
- [ ] Compact / Expand, Pause / Resume, Copy, Close, Main, Stop practice all work.
- [ ] `screencapture` omits the panel with stealth on and includes it with stealth off.
- [ ] The panel position survives a relaunch.
- [ ] `socket_stream overlay --provider mock --stealth` launches panel-only mode and Stop exits cleanly.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Accessibility: not granted` | The bundle has its own TCC identity. Grant it in Privacy & Security > Accessibility (or Device Control and Data Access), then restart. |
| `Screen Recording: not granted` | Add the app with **+** in Screen & System Audio Recording, then use **Quit & Reopen**. `screencapture` tests need this; text-tree reading does not. |
| `socket_stream not found` | The app resolves `socket_stream` next to its executable, then under `Contents/Resources`. Rebuild with `scripts/build-macos-app.sh`; do not mix an installed app with a deleted `dist/`. |
| Accessibility helper returns only the menu bar | Fixed by rooting the helper at the focused/main window and filtering `AXMenuBar`. Update the helper with `scripts/build-macos-helpers.sh`. |
| Browser tree shows only the toolbar, never the page | Two causes, both fixed: pointer-identity dedup pruned the `AXWebArea` subtree (now `CFEqual`), and the old depth-8 budget stopped above the page content, which sits at depth 13-14. The defaults are now depth 20 / 800 nodes / 32k chars / 4000 tokens and the default mode is `tree`. Rebuild with `scripts/build-macos-helpers.sh` and `scripts/build-macos-app.sh`. |
| `No accessible question` on a page that clearly has one | The default a11y mode was `auto`, which renders only the focused subtree (a browser's address bar or toolbar) and never reaches the page body. The default is now `tree`; override with `--a11y-mode` or `a11y.mode` in the config. |
| Chrome’s tree is empty from a Terminal-run helper | TCC attributes the helper to the responsible process. Run the app itself for the real permission identity; grant Accessibility to the app. |
| Panel is visible in a screen share | Verify the capture path. `sharingType` is a documented hint; some products do not honour it. Do not claim exclusion without a real test. |
| Permissions reset after every build | The grant is pinned to the build's signature. Run `scripts/create-macos-signing-cert.sh` once, rebuild, then `scripts/reset-macos-permissions.sh` and grant again. |
| Toggle shows enabled but app says `not granted` | Stale TCC row from an earlier signature. Use **Repair permissions** or `scripts/reset-macos-permissions.sh`, then grant again. |

## 10. Quality gates

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test --workspace
python3 -m pytest tests -q
./scripts/build-macos-helpers.sh
target/native-helpers/socket_stream_gui --self-test
```

The Swift self-test replays the event schema without AppKit and regresses the
live-output pipe handoff: a short event must be delivered while the writer
stays open (POSIX readiness read, not a fill-the-buffer read). The Rust tests
cover the CLI, accessibility renderer, OCR policy, and overlay argument
forwarding.
