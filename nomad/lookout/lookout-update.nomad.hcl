job "lookout-updater" {
  datacenters = ["homelab"]
  type        = "batch"

  periodic {
    crons            = ["0 6 * * *"]
    prohibit_overlap = true
  }

  group "update" {
    count = 1

    task "restart" {
      driver = "docker"

      config {
        image   = "hashicorp/nomad:1.9"
        command = "/bin/sh"
        args    = ["-c", "nomad job restart -address=http://192.168.0.202:4646 lookout || true"]
      }

      resources {
        cpu    = 100
        memory = 64
      }
    }
  }
}
