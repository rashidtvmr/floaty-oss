# Capture-filter verification

## Scope and result

The opt-in patch for upstream Mutter **50.1**, commit
`8e9bb806da4a6a4fc87aa4a481b228dba6735cbd`, built successfully on Ubuntu
26.04.1 x86_64 (GCC 15.2.0, Meson 1.10.1, PipeWire 1.6.2).
These are headless compositor tests, **not deployed GNOME Shell or Meet QA**.

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

## Source digests (SHA-256)

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

## Remaining limits

- No live GNOME Shell/Chrome Meet entire-screen validation or deployment of the
  patched libraries into the user's desktop has been performed.
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
