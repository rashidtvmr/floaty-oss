# Remio-inspired desktop styling

The [official Remio download page](https://remio.net/download) and its published
Mac host/client, Windows and iPhone screenshots are the visual references. The
Mac host shows a dark indigo workspace with a clear status row and separated
rounded cards. The Mac client instead uses a spacious device carousel. SocketStream
borrows the host's hierarchy and palette direction, not the carousel, brand assets,
pairing workflow or remote-control behavior.

## REA evidence and limits

| Artifact | Observation |
| --- | --- |
| macOS Host v3.8.9 | REA package inventory `ev_6891378d4182955f269e255d48fb8083bc6257337410483713ebf2a2fb437656`; read-only bundle resource session after expansion. `pkgutil` reported a notarized installer. No install scripts ran. |
| Android Host v3.8.4 | REA archive inventory `ev_e0749b60d70d02a489dc1d72e0e1ac61b61d4d544a6f30bcfab5400559ab8191`. |
| Windows client and host installers | REA PE resource inventories `ev_7296ba919c4fe4b6537873d0627b542d55b6d0ccd952e53ff889c983a9f8b015` and `ev_cd85341143d13af94373c6ccbca386c7b1bc6c44d267ab8fbcd79bd756e78fa4`. These describe installer resources, not runtime screens. |
| Linux x86-64 AppImage, Raspberry Pi arm64 DEB, FreeBSD PKG | Downloaded for reference; runtime UI not verified. |
| macOS and iOS clients, Android client, tvOS and Vision Pro | Store-distributed; no client package downloaded from those stores. |

The reference screenshots and static inventories don't establish Remio's actual
animation timings. SocketStream uses its own 160 ms entry fade, with the native
Reduce Motion setting honored on macOS and GTK's animation setting plus
SocketStream's Reduce Motion option honored on Linux. There are no perpetual
background animations. This is a theme pass on the existing AppKit and GTK3
surfaces, not a new Windows or mobile port.

The native macOS self-test covers the panel surface and entry animation's
Reduce Motion behavior. The Linux release workflow exercises the GTK palette
with Xvfb. Visual QA on an actual Linux desktop remains outstanding.
