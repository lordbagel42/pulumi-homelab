job "lookout-updater" {
  datacenters = ["homelab"]
  type        = "batch"

  # Keep the periodic identity: Nomad cannot convert an existing periodic job
  # to a regular batch job. Disable its schedule without triggering a restart.
  periodic {
    enabled          = false
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
