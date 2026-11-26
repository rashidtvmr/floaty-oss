# SocketStream installers

The installer branch does not change `main`. Installers are split to stay under GitHub's per-file size limit. Source: [macOS `da55a1a`](https://github.com/rashidtvmr/floaty-oss/commit/da55a1af5ba7e39366fa55ae1b4df337879b8487), [Linux `9757fb8`](https://github.com/rashidtvmr/floaty-oss/commit/9757fb8a24370d596ac2cc9ad9d528bb5f244cc1).

Download [this branch as a ZIP](https://github.com/rashidtvmr/floaty-oss/archive/refs/heads/installers.zip) and extract it, or clone it:

```sh
git clone --depth 1 --single-branch --branch installers https://github.com/rashidtvmr/floaty-oss.git socketstream-installers
cd socketstream-installers
```

### macOS (Apple Silicon)

Run `sh assemble.sh macos` from the extracted folder or checkout, then double-click the reconstructed ZIP and move `SocketStream.app` to `Applications`. The app bundle passed local signature verification and the panel self-test. It is not notarized for public distribution.

`assemble.sh` verifies the full installer SHA-256 before creating the final file. Windows has no supported desktop installer.

### Linux (x86-64)

Run `sh assemble.sh linux`, then unpack and launch:

```sh
tar -xzf socket_stream_linux-x86_64-20261126.tar.gz
cd socket_stream_linux-x86_64-20261126
./check-dependencies.sh
./socket_stream
```

On Debian or Ubuntu, `./install-dependencies-debian.sh` can install missing dependencies. The portable launcher requires an X11 or Wayland desktop session. The package passed GTK startup under Xvfb, speech runtime initialization, and seven synthetic speech integration tests on Debian x86-64. These tests do not verify real desktop capture, microphone permissions, or audio devices.
