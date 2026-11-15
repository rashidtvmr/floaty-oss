job "socket_stream_linux-test-runner" {
  datacenters = ["dc1"]
  type = "batch"
  constraint {
    attribute = "${meta.os_family}"
    value = "linux"
  }
  group "test" {
    task "runner" {
      driver = "raw_exec"
      config {
        command = "/usr/bin/bash"
        args = ["-lc", "sleep 3600"]
      }
      resources {
        cpu = 100
        memory = 128
      }
    }
  }
}
