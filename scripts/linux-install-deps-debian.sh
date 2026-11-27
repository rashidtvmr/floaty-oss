#!/bin/sh
# Manual apt installer for Ubuntu/Debian; never runs automatically.
set -eu
command -v apt-get >/dev/null 2>&1 || { echo "Only apt-based distros supported" >&2; exit 2; }
if [ "$#" -eq 0 ] || [ "$1" != "--yes" ]; then
  echo "Review this script and add --yes to explicitly install Linux dependencies."
  exit 2
fi
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
$SUDO apt-get update
$SUDO apt-get install -y python3 python3-gi python3-dbus gir1.2-gtk-3.0 gir1.2-atspi-2.0 at-spi2-core libglib2.0-bin \
  gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-pipewire xdg-desktop-portal pipewire tesseract-ocr ffmpeg pulseaudio-utils \
  imagemagick git cmake make g++
echo "Dependencies installed. Re-run check-dependencies.sh."
