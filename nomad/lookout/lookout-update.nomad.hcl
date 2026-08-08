job "lookout-updater" {
  datacenters = ["homelab"]
  type        = "batch"

  # Periodic scheduling disabled; retain the job definition for manual runs.

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
