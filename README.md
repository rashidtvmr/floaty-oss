# SocketStream installers

These packages are built from [source commit `da55a1a`](https://github.com/rashidtvmr/floaty-oss/commit/da55a1af5ba7e39366fa55ae1b4df337879b8487). The installer branch does not change `main`. Installers are split to stay under GitHub's per-file size limit.

Download [this branch as a ZIP](https://github.com/rashidtvmr/floaty-oss/archive/refs/heads/installers.zip) and extract it, or clone it:

```sh
git clone --depth 1 --single-branch --branch installers https://github.com/rashidtvmr/floaty-oss.git socketstream-installers
cd socketstream-installers
```

### macOS (Apple Silicon)

Run `sh assemble.sh macos` from the extracted folder or checkout, then double-click the reconstructed ZIP and move `SocketStream.app` to `Applications`. The app bundle passed local signature verification and the panel self-test. It is not notarized for public distribution.

`assemble.sh` verifies the full installer SHA-256 before creating the final file. Windows has no supported desktop installer.
