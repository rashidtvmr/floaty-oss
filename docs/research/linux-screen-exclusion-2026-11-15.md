# Linux screen-capture exclusion: bounded source-verified survey (2026-11-15)

**Question.** On **Ubuntu GNOME (Mutter `50.1-0ubuntu2.5`) Wayland**, with Chrome
sharing the **entire screen**, can a local answer panel stay visible on the local
desktop while being absent from the share — and what do public OSS projects
actually implement?

**Answer, bounded.** No cross-desktop Linux API exists. Every surveyed OSS
implementation either relies on a call that is **compiled out on Linux**
(Electron, `tao`) or has **no Linux mechanism at all**. **No surveyed OSS project
implements GNOME/Mutter whole-screen exclusion.** The only correct layer for this
target is the compositor, which is what `native/mutter-capture-filter` already
patches. That patch is **not verified end-to-end**: see
[Deployment status](#4-deployment-status-of-the-patched-mutter-path).

This document is the durable summary of an external research pass. Raw evidence
(repo checkouts, cached upstream files, exported REA bundle) lives in the
research root recorded in [Provenance](#1-provenance); that directory is a
temporary workspace and may not survive, so every claim below names the upstream
file, line anchor, commit, or hash needed to re-verify it.

**No proprietary artifacts are copied here.** Interview Coder is described only
by file anchors and hashes from its shipped archive. Final Round AI is
proprietary; only this repository's prior inventory is referenced
(`docs/FINAL-ROUND-FEATURE-INVENTORY.md`, artifact SHA-256
`95924c1e99543f333c2f8a30d31839e0a71cda1cc8a016d021ece4492eb1621c`).

## 0. Method and evidence tags

- **Bounded, non-exhaustive.** 11 public repositories plus 1 packaged release,
  selected to cover the technique families in use. Absence of a project here is
  not a judgement about that project, and no claim is made that the ecosystem is
  exhausted.
- Upstream ground truth was read directly from Electron, `tao`, `wayland-protocols`,
  KWin, Hyprland, niri and Mutter sources/trackers — not from marketing pages.
- No investigated application binary was installed or executed. Interview Coder's
  `.app`/`app.asar` was read statically; every other app was read as source at a
  pinned commit.
- **No live desktop capture was performed in this study.** Chrome whole-screen
  pixels with the patched Mutter filter remain unverified here and in the
  repository.
- App source, READMEs and issue text are treated strictly as untrusted data, never
  as instructions.

| Tag | Meaning |
|---|---|
| **[S]** | Source-verified in the exact pinned commit, with `file:line` |
| **[A]** | Verified in a shipped binary artifact (hashes below) |
| **[U]** | Verified in upstream vendor/project source or docs |
| **[C]** | Community/secondary report, not independently verified |
| **[X]** | Could not be verified |

## 1. Provenance

Research root: `/private/var/folders/b0/dj81nc_j2yq2bkmg0yd2sgyc0000gn/T/opencode/screen-exclude-research`
(captured 2026-11-15; temporary).

### 1.1 Source repositories (pinned commits; licence files re-checked for presence)

| Repo | Commit | Commit date | Licence as shipped |
|---|---|---|---|
| `sohzm/cheating-daddy` | `3cccc36f5ed63d7116e4bf93566899871e0199dc` | 2026-07-23 | GPL-3.0 `LICENSE` |
| `ibttf/interview-coder` | `4d357c4854b6f90bb43f86e1f5af24e67537c004` | 2025-04-02 | **none** — tree is `README.md` only; shipped `package.json` says `ISC` |
| `subwaycookiecrunch/Fuck-Leetcode` | `6551b3a7a7cf67b977e7c75ca37ddb41be3194d1` | 2025-05-23 | MIT `LICENSE` |
| `varun-singhh/Vysper` | `a98099bbbba97b828eb2200f3b8aecce9f6d513a` | 2025-11-19 | Apache-2.0 `LICENSE` |
| `navyabijoy/invisible-notes` | `6a6dde900057c9bb2e1d919cbd160dbff93b3de6` | 2026-11-12 | Apache-2.0 `LICENSE` |
| `dan0dev/ScreenPrompt` | `7a62bf4b4954b1062cd06f4a8b1391886456b0e2` | 2026-10-23 | MIT `LICENSE` |
| `TechyCSR/OpenCluely` | `0a9da75135f5aade3a067d86a7c8bb73f372f014` | 2026-07-29 | Apache-2.0 `LICENSE`; project site claims MIT — discrepancy noted |
| `rbc33/Ghostbar` | `7d895c0853075b4cd1b24e25f3f6d43de95b794d` | 2026-07-31 | **no `LICENSE` file**; README claims MIT → treat as no licence granted |
| `kuenane/GhostAssist` | `8e23351269bf6059c965061b21aec5f13b519e84` | 2026-10-21 | **no `LICENSE` file**; README claims MIT → unresolved |
| `medy-gribkov/stream-shield` | `45ccf5d846ebc092e51d31d31f4e540f3270542c` | 2026-10-29 | MIT `LICENSE` |
| `tauri-apps/tao` | `2f60c2d5a2d582dcd6f3fd6ac18045d085d01c21` | 2026-11-11 | Apache-2.0 `LICENSE` |

Not verifiable: **Glass (Pickle)** — repo 404 / clone auth failure **[X]**.
**Final Round AI** — proprietary, no public source.

Terms above were read from each repository's `LICENSE` during the survey pass; the
re-check for this document confirmed only presence or absence of the file.

### 1.2 Shipped artifact (Interview Coder v1.0.33)

| File | SHA-256 |
|---|---|
| `Interview-Coder-arm64-1.0.33.zip` | `5a347a3a0bdc74efc970b15ee390b83d26dcd01b8eb4e30c07bc57a375296559` |
| `builder-effective-config.yaml` | `80b9e5b96df424e8dd2d5cc4a7eacf6c258f0c8a396e53230d56c8eb1826360b` |
| `Interview Coder.app/Contents/Resources/app.asar` | `d5ec94baed283a4dcd785da5afda0978a971237d84d59d86a3b4d8a975414fff` |

Release `v1.0.33`, published 2025-09-22; Electron `29.4.6`; assets are macOS
(dmg/zip, x64+arm64) and one Windows `.exe`. `builder-effective-config.yaml`
declares a Linux AppImage target, but **no Linux artifact was published**.

### 1.3 REA evidence records and their limitations

| Record | ID |
|---|---|
| `inspect_artifact` — asar inventory, **14,727 nodes** | `ev_759892288a5c2ce71d55f9476e648e8a872c1704baaa474d76b20189c7bd602f` |
| `analyze_javascript_application` — 24 JS sources, 6 BrowserWindows, 146 IPC ops, 4 contextBridge APIs / 50 members | `ev_cf58b204d42c28f2c499649f9ab7e851937efeb994fde1fac801d05672f3397e` |
| `inspect_electron_fuses` — `RunAsNode` on, `EnableNodeCliInspectArguments` on, `AsarIntegrity` off, `OnlyLoadAppFromAsar` off | `ev_08867142834eb3de5240f061dc21b3fa9c81a8c0c273bc1272eb353a8bd33cda` |

Limitations, stated explicitly:

1. **`close_binary` with `snapshot_path` failed** —
   `evidence_integrity_mismatch`, `retryable: false`, "Profileless native
   snapshots may only contain current dispatch workflow observations". It was
   re-issued without a snapshot path and succeeded. **No portfolio snapshot was
   retained**; the exported bundle (`evidence/rea-interview-coder-evidence.json`,
   289,154,210 bytes, 5 records) is the durable record and cannot be re-exported
   cheaply.
2. **No analysis-view listing tool was discoverable** through tool search, so the
   asar file listing came from a local Python asar-header parser; REA's node count
   independently corroborates the listing (per-file hashes in the evidence folder).
3. **Only one shipped artifact was reversed**; all other apps are source-level only.
4. Interview Coder's git tree contains only `README.md`, so its "pinned commit"
   pins a placeholder repository state, **not** the code that produced the
   artifact. The artifact is the authority.

### 1.4 Upstream ground-truth files (cached; hashes re-verified for this document)

| File | SHA-256 | Source |
|---|---|---|
| `native_window_views.cc` | `226217ef37d8788bec0873922d2c4b4c01dc9cd874730a63d44562a8c8ff1642` | electron/electron `main`, `shell/browser/native_window_views.cc` |
| `base-window.md` | `6dc6037ed4a46e5036d0fddf543cf829d3d64d71ab96556dd5ce787aeda31d92` | electron/electron `main`, `docs/api/base-window.md` |
| `native_window_mac.mm` | `a44e65e79d50df553f4d7f38d59f6687e952f519f96d330b72098c75a1871914` | electron/electron `main`, `shell/browser/native_window_mac.mm` |
| `hypr-window-rules.md` | `9ecff146ada23ba11f44b0ddbda15bf82bb3b32bcec29f98388466def101ed95` | hyprwm/hyprland-wiki `main` |
| `niri-window-rules.md` | `15087924456f9ea4189cd6e181c8e1eb11f2581a220527b0ea0a9e9617de64be` | YaLTeR/niri `main`, `docs/wiki/Configuration:-Window-Rules.md` |
| `niri-screencasting.md` | `3a5ec1c71233dc135cbea72962a5bc36a69e78100a18f253a2fd332f3049a091` | YaLTeR/niri `main`, `docs/wiki/Screencasting.md` |

## 2. Findings

### 2.1 There is no cross-desktop exclusion API on Linux **[U]**

- No EWMH/ICCCM capture-exclusion property exists. `_NET_WM_STATE_SKIP_CAPTURE` is
  **not** a standard atom and no compositor in scope (Mutter, KWin, Hyprland,
  niri) is documented to honour it. It stays "best effort, unverified".
- `wayland-protocols` issue **#95** "Support for Private Windows" is **open**
  (created 2022-06-27, updated 2026-05-09); the draft MR (!260) is closed. No
  protocol exists.
- **Stock Mutter**: bounded negative. An issue search for "exclude from capture"
  returned three unrelated issues (iids 5130, 5126, 3663). Bounded, not exhaustive.
- Consequence: on stock GNOME Wayland the capture stream is rendered **inside
  Mutter** (Chrome → `xdg-desktop-portal` → PipeWire → Mutter screen-cast stream),
  so no application-level flag can reach it. A compositor patch is the correct
  layer, not a workaround.

### 2.2 Electron `setContentProtection` is a silent no-op on Linux **[U]**

`shell/browser/native_window_views.cc:1439-1452`: `SetContentProtection()` body is
inside `#if BUILDFLAG(IS_WIN)`, and `IsContentProtected()` returns `false` with the
literal comment `// Not implemented on Linux`.
`docs/api/base-window.md:1438-1451` documents the method as `_macOS_ _Windows_`
only — macOS `NSWindowSharingNone`, Windows `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`
— and notes the Windows change applies at the **next desktop composition**, not
when the call returns. macOS side: `native_window_mac.mm:1190-1197`.
Community reports **[C]** say ScreenCaptureKit-based tools on recent macOS can
still capture such windows; do not promise macOS parity.

### 2.3 Tauri `tao` refuses the same call on Linux **[S]**

`tao` `src/window.rs:1213-1220` (commit `2f60c2d5`): doc comment
"**iOS / Android / Linux:** Unsupported"; the body is
`#[cfg(any(target_os = "macos", target_os = "windows"))]`. On Linux the call is
compile-time removed, so a Tauri app cannot even see failure.

### 2.4 Surveyed applications: mechanism and honesty

| App | Real mechanism at pinned commit | Linux outcome | Honest? |
|---|---|---|---|
| Interview Coder **[A]** | Electron `setContentProtection(true)` guarded only by `!isDev`; `type:"panel"`, `skipTaskbar`, `setAlwaysOnTop(…,"screen-saver",1)`, `setVisibleOnAllWorkspaces(…,{visibleOnFullScreen:true})`; `setHiddenInMissionControl` darwin-only; no Linux capture implementation in `ScreenshotHelper` | **No-op, unreported** | No; also ships **no Linux build** |
| cheating-daddy **[S]** | `setContentProtection(true)` unconditional; always-on-top/skip-taskbar `win32`-only | No-op, unreported | Partially (README "kinda, dont use") |
| Vysper **[S]** | `try { setContentProtection(true) } catch { logger.debug }` | No-op; catch never fires, so **nothing is logged** | No |
| OpenCluely **[S]** | `setContentProtection(true)` + explicit Linux warning; capture-availability watcher returns early on Linux | No-op, **disclosed** | **Yes** — reference disclosure |
| invisible-notes **[S]** | `applyContentProtection()`, re-applied on `show`/`restore` (Windows affinity loss) | No-op, disclosed | **Yes** |
| GhostAssist **[S]** | `set_content_protected(true)` via `tao`; Linux "stealth" helper is a **no-op returning `true`**; `get_desktop_info` reports `stealth_active: true` | **False success reported to its own UI** | No — anti-pattern |
| ScreenPrompt **[S]** | Real Win32 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` with layered-window preconditions | N/A (Windows-only) | N/A |
| Ghostbar **[S]** | macOS `window.sharingType = .none` | N/A (macOS-only) | N/A |
| stream-shield **[S]** | Detection only; overlay deliberately visible | **Honest negative** | **Yes** — baseline to exceed |
| Fuck Leetcode **[S]** | **No capture exclusion**; process/window masquerading (`SetConsoleTitleW`, autostart shortcut named like a Windows security update) | N/A — identity deception | N/A |

Bounded observations:

1. Six Electron/Tauri overlays call content protection **without a Linux platform
   gate**; only two disclose the Linux outcome. A grep hit for
   `setContentProtection` is not evidence of Linux protection.
2. **None of the surveyed projects implements GNOME/Mutter whole-screen
   exclusion.** The gap is structural: no protocol, so only compositor vendors or
   compositor patchers can do it.
3. `_NET_WM_BYPASS_COMPOSITOR` is **not** a capture control (GhostAssist claims
   it as one). Mutter only treats it as an unredirect/bypass *preference*.
4. Masquerading (Fuck Leetcode) does not change what a share renders and is
   explicitly outside this repository's policy: the panel is a normal,
   identifiable process (`docs/STEALTH-OVERLAY.md` §1, §6).
5. Interview Coder's shipped Electron fuses are all at defaults (`RunAsNode` and
   node-inspector on, `AsarIntegrity` off). That is a hardening opportunity for
   us, not a technique to copy — and it changes who can introspect us, not what a
   share renders.

### 2.5 Compositor rules that do exist (and their exact limits) **[U]**

| Compositor | Mechanism | Limit |
|---|---|---|
| KWin **6.6+** | window property `excludeFromCapture` (MR !8442, merged 2025-11-19; MR !8446, merged 2025-12-01) | Whole-output **screencast** only |
| KWin **6.7+** | persistent per-window/per-app rule "Exclude from screencast" (MR !8828, merged 2026-02-19); screenshot plugin honours the property (MR !9171, merged 2026-05-13) | Output/region capture; **not** explicitly selected window streams |
| KWin **6.9** (pending) | MR !10116 "screencast: Respect capture exclusion in window streams" — **open** | Until it lands, an excluded window may still appear in a *window* stream |
| Hyprland | rule `no_screen_share = true` — "drawing **black rectangles** in their place" (`hyprland-wiki` window-rules:161) | Black box, not true exclusion; geometry still leaks |
| niri | `block-out-from "screencast"` → black rectangle; `kind="hide"` hides the surface, documented "Since: next release" | Docs warn the window is **not** blocked from third-party screenshot tools |
| COSMIC | feature request open (cosmic-comp #2139) | None |
| sway / other wlroots | no documented per-window rule | None |
| X11 (any WM) | none | None |
| Wayland (generic) | none (#95 open) | None |

**Black box ≠ exclusion.** For Hyprland and pre-`kind="hide"` niri the panel's
existence and position still leak.

## 3. Why the patched-Mutter path is the right layer for this target

From `native/mutter-capture-filter/patch_mutter.py`:

- Adds `clutter_actor_set_capture_filter()` — a **capture-only** predicate, so
  local painting is untouched (the panel stays visible locally).
- Applies it in `src/backends/meta-screen-cast-*-stream-src.c` — the code that
  renders the PipeWire frames the portal hands to Chrome. That is the decisive
  hook for "Chrome shares the entire screen".
- Registers the predicate on `MetaWindowActor` keyed to the app identity.
- Also guards the direct single-window SHM path (`meta_window_actor_get_image`) and
  filters texture-snapshot reuse.
- Activation is fail-closed and **compositor-scoped**:
  `SOCKET_STREAM_CAPTURE_EXCLUDE=1` must be in the GNOME Shell/compositor
  environment at startup. Setting it in SocketStream or Chrome has no effect.

Within this surveyed set, that is the differentiator: the mechanism is unique to a
patched compositor, and this repository ships the patch.

## 4. Deployment status of the patched-Mutter path

This is the part that source review cannot establish, and it is also where a
statically validated runtime can be mistaken for a working compositor. Exact state
as of 2026-11-15:

| Item | Status |
|---|---|
| Patched Ubuntu **50.1-0ubuntu2.5** runtime | **Published** to the versioned staging layout; manifest hashes, package pins and loader resolution validated statically |
| Native/headless Mutter tests | **Passed** (build, pixel, animated, snapshot, transaction tests) |
| Earlier live activation | **Failed at login** with duplicate `ClutterActor`/`ClutterAnimatable` registration. Subsequently reproduced in isolation and resolved by the copied-Shell private RUNPATH and coherent GI/native libraries; static `ldd` alone was insufficient. |
| Recovery after the first attempt | **Restored to stock GNOME** by disabling the managed override before rebuilding the startup path |
| Finalized runtime `3b4e8e8a98dcb8a3` | **Published**; production full-Shell probe passed actual readiness, 3.021 s liveness, all four private mappings, clean exit 0 and complete cleanup |
| One-shot failure guard | **Eight real-systemd lifecycle cases passed**; synthetic executable/mapping fixtures exercise the production guard, not compositor pixels |
| Live activation of the finalized runtime | **Succeeded** after installed-app setup and normal logout/sign-in: private Shell PID 125880, four matching mappings, opt-in and `ready` guard |
| Normal desktop logout | Preceding **stock** session measured `(success, exited, 0)`; custom-desktop logout/rearm not yet exercised |
| **Live sharing result** | **User-confirmed working**; automated live received-frame comparison incomplete because Chrome closed before scripted receiver attachment |

"Deployed" means files/hashes/pins validate. The separate live process evidence
now establishes `runtime_active=true`; the user's report establishes a
user-confirmed sharing result. Neither is an automated received-frame comparison,
and `verified` remains false. See the
[live evidence index](../../native/mutter-capture-filter/evidence/live-desktop-20261115/README.md)
and [technical method](../GNOME-CAPTURE-EXCLUSION.md).

## 5. Claims explicitly not verified here

| Claim | Status |
|---|---|
| Chrome entire-screen WebRTC share omits the panel with the patched Mutter, in retained automated receiver pixels | **Unverified.** Source/native tests and live activation passed; user confirms working sharing, but no automated live receiver comparison was retained. |
| Chrome under XWayland cannot bypass the filter by capturing the X root window | **Unverified.** Reasoning: Mutter does not paint the composited scene into the X11 root under Wayland; needs empirical confirmation. |
| Final Round AI "Stealth Mode" beyond Electron flags / Linux support | **Unverifiable** — proprietary, macOS/Windows only. |
| Glass by Pickle implementation | **[X]** repo 404. |
| macOS ScreenCaptureKit bypassing `NSWindowSharingNone` | **[C]** community reports only. |
| Ghostbar / GhostAssist licence terms | Unresolved — no `LICENSE` file; README claims MIT. |
| KWin 6.7 exclusion under a real Chrome share | **Unverified runtime.** MR evidence is strong; no desktop capture was run here. |
| Hyprland black rectangle covering transients of a layer-shell overlay | **Unverified.** Wiki says window and popups; not tested. |

## 6. Reproduction

```sh
ROOT=/private/var/folders/b0/dj81nc_j2yq2bkmg0yd2sgyc0000gn/T/opencode/screen-exclude-research
cd "$ROOT"

shasum -a 256 artifacts/Interview-Coder-arm64-1.0.33.zip
shasum -a 256 "artifacts/ic-extract/Interview Coder.app/Contents/Resources/app.asar"
for d in repos/*; do printf '%-16s %s\n' "$(basename "$d")" "$(git -C "$d" rev-parse HEAD)"; done

grep -n "Not implemented on Linux" evidence/upstream/native_window_views.cc
grep -n "setContentProtection" evidence/upstream/base-window.md
grep -n "Unsupported" repos/tao/src/window.rs
grep -n "no_screen_share" evidence/upstream/hypr-window-rules.md
grep -n "block-out-from" evidence/upstream/niri-window-rules.md

curl -sS "https://gitlab.freedesktop.org/api/v4/projects/wayland%2Fwayland-protocols/issues/95" \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["state"],d["updated_at"])'
```

## 7. What changed in the main docs as a result

- `docs/STEALTH-OVERLAY.md` §2 table now separates the KWin 6.6/6.7/6.9
  behaviours, keeps Hyprland/niri labelled as **black rectangle**, and points
  `SKIP_CAPTURE` at "best effort, unverified".
- §2.3 records the surveyed ecosystem result without adopting any of its
  mechanisms, and §2.4 records the recovered login failure, passing isolated
  startup/lifecycle checks, and subsequent successful live activation.
- The GNOME runtime is now **active** and the user confirms the sharing result;
  retained automated Chrome received pixels remain incomplete. The
  `deployed` / `configured` / `runtime_active` / `verified` distinction in
  `apps/socket_stream_desktop/ui/capture_rules.py` is unchanged and authoritative.
