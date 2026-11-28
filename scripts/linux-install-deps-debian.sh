#!/bin/sh
# Manual apt installer for Ubuntu/Debian; never runs automatically.
set -eu
command -v apt-get >/dev/null 2>&1 || { echo "Only apt-based distros supported" >&2; exit 2; }
confirmed=0
desktop=
for argument in "$@"; do
  case "$argument" in
    --yes) confirmed=1 ;;
    --desktop=gnome|--desktop=kde)
      choice=${argument#--desktop=}
      if [ -n "$desktop" ] && [ "$desktop" != "$choice" ]; then
        echo "Choose exactly one desktop portal backend." >&2; exit 2
      fi
      desktop=$choice ;;
    *) echo "Usage: $0 --yes [--desktop=gnome|--desktop=kde]" >&2; exit 2 ;;
  esac
done
if [ "$confirmed" -ne 1 ]; then
  echo "Review this script and add --yes to explicitly install Linux dependencies."
  exit 2
fi
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
set -- python3 python3-venv python3-gi python3-cairo python3-gi-cairo python3-dbus gir1.2-gtk-3.0 gir1.2-atspi-2.0 at-spi2-core libglib2.0-bin \
  gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-pipewire xdg-desktop-portal pipewire tesseract-ocr ffmpeg pulseaudio-utils \
  imagemagick git cmake make g++
case "$desktop" in
  gnome) set -- "$@" xdg-desktop-portal-gnome ;;
  kde) set -- "$@" xdg-desktop-portal-kde ;;
esac
# pactl works with the desktop's existing PulseAudio-compatible server. Do not
# select a replacement pulseaudio/pipewire-pulse daemon or restart user services.
$SUDO apt-get update
$SUDO apt-get install -y "$@"
echo "Dependencies installed. Re-run check-dependencies.sh."
if [ -z "$desktop" ]; then
  echo "GNOME/KDE portal backend: add --desktop=gnome or --desktop=kde if it is missing."
fi
echo "Speech also needs the bundled Moonshine engine/model; source checkouts build it with python3 scripts/build-moonshine.py."
