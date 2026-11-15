# Capture-filter verification

## Scope and result

The opt-in patch for upstream Mutter **50.1**, commit
`8e9bb806da4a6a4fc87aa4a481b228dba6735cbd`, built successfully on Ubuntu
26.04.1 x86_64 (GCC 15.2.0, Meson 1.10.1, PipeWire 1.6.2).
The original evidence below is headless compositor QA. Subsequent Ubuntu
full-Shell startup, real-systemd guard and live desktop activation evidence is
recorded separately below. **The user confirmed the live sharing result works**;
an automated live received-frame comparison has not been retained.

| Check | Result | Evidence |
| --- | --- | --- |
| Full patched Mutter build | Passed | Build completed; native logs under `evidence/mutter-build/meson-logs/` |
| Native capture pixels, actor clone, offscreen redirect, native screencast | 4 executables passed | Same native logs |
| XWayland/native Wayland × opt-in off/on × monitor/area/panel-window | 12 cases passed | `evidence/capture-qa-final/*/results.json` and PNGs |
| Animated obscured background, XWayland and native Wayland | 60 frames each; no magenta panel pixels | `evidence/capture-qa-final/*-dynamic/` |
| Strengthened snapshot-only native regression | Passed after rebuilding test | `evidence/snapshot-regression/results.json` |
| Transactional patcher | 5 tests passed with isolated upstream fixtures | `tests/test_mutter_patch.py` |
| Compositor configuration helpers | 35 tests passed, including KDE regressions | `tests/test_capture_rules.py` |

Disabled controls contain 54,000 magenta panel pixels in each static capture;
enabled captures contain none. Enabled monitor/area output exposes the green
window behind the panel. Enabled panel-window captures are empty. Both animated
runs contain green and blue background frames and zero magenta pixels.

The native regression checks cached ancestor effects across capture/local modes
and consecutive capture paints with changed predicates. Its final revision hides
the original red actor before checking snapshot-only local red pixels, waits for
layout/presentation, and disables snapshot exclusion as a negative control that
must expose red in capture. That revision was rebuilt and passed separately;
the production patch was unchanged from the full capture run.

## Ubuntu private runtime and safer startup (2026-11-15)

The exact installed Ubuntu Mutter source **50.1-0ubuntu2.5**, including downstream
patches, was built and privately installed on `rashid-lenovo`. Its four native
test executables, 12 static capture cases and 60 animated frames per backend
passed. GNOME Shell is pinned to **50.1-0ubuntu1.5**.

The first desktop activation failed during GI/JavaScript startup with duplicate
`ClutterActor`/`ClutterAnimatable` registration. The failure was reproduced in an
isolated full Shell. Copying Shell with the private lib/typelib RUNPATH, together
with each DSO's local RPATH, resolved that failure in the production startup
probe. Static `ldd` success alone had not exercised those late GI loads.

Finalized immutable runtime:

```text
/opt/socket_stream-mutter/50.1-0ubuntu2.5.socket-stream.3b4e8e8a98dcb8a3
34db0d1aa96e0ee6c3c5bd906b14fb9ed243e0ed3ff3c01f16c0952f5907dad7  source.dsc
e4daf6e0d5b02499b8373aece210db9db9f3ef2c7d1b765abbd02fe8b922cdd3  capture.patch
```

| Check | Observed result | Retained evidence |
| --- | --- | --- |
| Public production startup probe | Actual private Shell PID **77340**, starttime **634583**, authenticated READY, **3.021 s** stable liveness, four exact private mappings, shutdown **0**, complete cleanup | `evidence/production-startup-20261115-212245/result.json` |
| Real-systemd lifecycle QA | **8/8 passed**, UID 1000, actual notify readiness and `/proc` identity; all QA units and scratch paths gone afterward | `evidence/guard-lifecycle-20261115-213117/summary.json` |
| Desktop after QA | Stock `/usr/bin/gnome-shell`, PID **11136** unchanged from the prior stock observation, four stock mappings, no managed drop-in | `evidence/guard-lifecycle-20261115-213117/live-unit-integrity.json` |
| Full Python suite | **500 tests, six skips, no failures** on macOS; platform/source-fixture skips retained | `python3 -m unittest discover -s tests -p 'test_*.py' -q` |
| Linux capture/startup Python suites | **343 tests, four source-fixture skips, no failures**, run as desktop UID 1000 on the Lenovo | Remote QA root: `linux-python-20261115-214152.txt` |
| Rust gates | Formatting, Clippy with warnings denied and **144 tests** passed earlier; Rust source unchanged by this startup work | Required workspace gates in `docs/ACCEPTANCE.md` |

The auxiliary before/after unit snapshot initially targeted inactive `@wayland`;
the retained integrity file labels it as such. The subsequent live check targets
the actual **`org.gnome.Shell@ubuntu.service`** and records the unchanged stock PID.

### Actual systemd finish tuples

The harness captures these directly from `ExecStopPost`, rather than inferring
them from `systemd-run`'s return code:

| Case | `(SERVICE_RESULT, EXIT_CODE, EXIT_STATUS)` | Guard afterward |
| --- | --- | --- |
| Ready, exit 0 | `(success, exited, 0)` | `armed / clean-stop` |
| Exit 1 before readiness | `(protocol, exited, 1)` | `blocked / pending-failed` |
| Ready, SIGSEGV | `(core-dump, dumped, SEGV)` | `blocked / unclean-stop` |
| Ready, exit 1 with `SuccessExitStatus=1` | `(success, exited, 1)` | `blocked / unclean-stop` |
| Disarm while ready, then exit 0 | `(success, exited, 0)` | `blocked / disarmed` |
| Next invocation after each of the three failures | `(success, exited, 0)` from the stock fixture | Original blocked record retained; neither hook rearms it |

These services use a synthetic executable and four mapped fixture files with the
production guard. They prove systemd lifecycle/identity semantics, not Mutter
loading or exclusion pixels. The real-Shell probe supplies separate startup
evidence. The subsequent live transition recorded the **stock** desktop's normal
logout tuple as `(success, exited, 0)`; the custom desktop's normal logout/rearm
remains unmeasured. Strict exit-0 rearming is retained. The probe's cleanup
assumes its descendants remain in the disposable unit's cgroup; its private bus
has no service activation, and systemd also applies `KillMode=control-group`.

## Installed application and live activation (2026-11-15)

Application commit **`4c4eba9fe7769ea2e808438abf70bf53b02f90b1`** was installed at
`/opt/socket_stream`. The release checksum and eleven packaged source/page files
were verified. The packaged Moonshine helper emitted `ready` and exited 0 as the
desktop user. The actual installed GTK app's **Replay demo** and **Set up
exclusion** actions were exercised through AT-SPI.

After the user-approved normal logout, the actual `org.gnome.Shell@ubuntu.service`
transitioned from stock PID **11136** to private Shell PID **125880**. The old
stock invocation's finish tuple was `(success, exited, 0)`. The new invocation
`cd7151d47e314851b5539adf64b8d42a` loaded all four matching private libraries,
carried the opt-in, and reached `ready`. The installed backend returned
`config_validated=true`, `runtime_active=true`, `restart_required=false`, and
`verified=false`.

The user then reported **“its working finally”** and requested the method be
committed and documented. This is a user-reported live result. Chrome opened the
exact reference page but closed before the scripted receiver attached, so this
run supplies no new automated encoded-frame comparison. Two XTest drag attempts
observed no movement and do not establish drag behavior for this run. The custom
desktop and installed app were left running.

The [live evidence index](evidence/live-desktop-20261115/README.md) links the
installation, UI, state and read-only systemd lifecycle records. The
[technical document](../../docs/GNOME-CAPTURE-EXCLUSION.md) explains the rendering,
loader, activation and recovery method in detail.

## Original headless source digests (SHA-256)

```text
7a5db3b3b5f9fe165a02a64dd9ce7aff48b8aaf60028b7bc39390ab89e1e294e  patch_mutter.py
73aa242f4e4c9761ee711c269291bd72018b8ad8dba221538e2711e1999112f8  socket_stream_capture.c
7514931631baf6cac3aa4f008269682b67843e870e9d20edd4ee3f2e4840963f  run_headless_qa.sh
```

## Independent review disposition

Read-only review found KConfig file immutability, double-layer comma escaping,
and legacy migration defects. Fixes refuse unsupported/immutable input before
backup/write/reload, round-trip authentic comma IDs, and refuse migration that
would activate or delete retired user groups. Follow-up found a managed legacy
orphan status false-positive; effective IDs now remain legacy-listed until
explicit installation repairs the orphan. A regression covers both legacy and
modern behavior. Review also identified the weak snapshot assertion; the
strengthened test and negative control passed as described above.

The safer-startup work received independent read-only reviews of the launcher,
validator, stager, probe, guard, app transactions and status reporting. Follow-up
closed the identified rollback-disclosure and permission defects, required all
four mappings before readiness, and verified same-PID fallback. The final
diagnostic review closed the smaller residuals; its claimed vanished-file
`IndexError` was retracted after tracing the unconditional removal bookkeeping.
No P0/P1 issue remained in the reviewed paths. Real login subsequently succeeded
as recorded above; automated live received-pixel QA remains incomplete.

## Remaining limits

- The finalized runtime is active on the live desktop and the user confirmed the
  sharing result. Automated Chrome/Meet received-frame pixels and a subsequent
  normal logout/rearm of that custom desktop remain unverified.
- KDE live output/region sharing remains unverified; explicit panel-window
  capture is not covered by the upstream KDE rule.
- Real Shell animation transitions, hardware DMA-BUF/direct scanout,
  fractional/multi-monitor scaling and performance remain unverified.
- Generic Shell screenshot consumers and `clutter_stage_capture_view_into()`
  remain unfiltered. Third-party effects and direct X11/hardware capture need
  separate analysis. There is no universal all-app capture-exclusion guarantee.
- Activation requires patched compositor libraries and
  `SOCKET_STREAM_CAPTURE_EXCLUDE=1` in the compositor's environment, not merely in
  SocketStream. See `README.md` for deployment boundaries.
