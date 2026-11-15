job "socket_stream_linux-desktop-diagnostic" {
  datacenters = ["dc1"]
  type = "batch"

  constraint {
    attribute = "${meta.os_family}"
    value     = "linux"
  }

  group "inspect" {
    task "session" {
      driver = "raw_exec"
      config {
        command = "/usr/bin/bash"
        args = ["-lc", "hostname; uname -sr; cat /etc/os-release | head -8; loginctl list-sessions --no-legend; for session in $(loginctl list-sessions --no-legend | awk '{print $1}'); do loginctl show-session \"$session\" -p Type -p Desktop -p State -p Active -p Remote -p Display; done; ps -eo comm | grep -E 'gnome-shell|kwin_wayland|Hyprland|niri' || :; for command in python3 git gnome-shell kwin_wayland niri Hyprland; do command -v \"$command\" || :; done"]
      }
      resources {
        cpu = 100
        memory = 64
      }
    }
  }
}
