job "oracle-pbs-backup" {
  datacenters = ["oracle"]
  type        = "batch"

  periodic {
    crons            = ["0 3 * * *"]
    prohibit_overlap = true
  }

  group "backup" {
    restart {
      attempts = 2
      interval = "30m"
      delay    = "15s"
      mode     = "fail"
    }

    task "pbs-backup" {
      driver = "docker"

      config {
        image = "ghcr.io/aterfax/pbs-client-docker:latest"
        volumes = [
          "/app/pelican/data:/backup/pelican-data:ro",
          "/app/pelican/mysql:/backup/pelican-mysql:ro",
          "/app/pelican/logs:/backup/pelican-logs:ro",
        ]
      }

      env {
        PBS_REPOSITORY  = "root@pam@__PBS_IP__:main"
        PBS_PASSWORD    = "__PBS_PASSWORD__"
        PBS_FINGERPRINT = "__PBS_FINGERPRINT__"
        BACKUP_ID       = "oracle-gameserver"
        BACKUP_SOURCES  = "pelican-data.pxar:/backup/pelican-data pelican-mysql.pxar:/backup/pelican-mysql pelican-logs.pxar:/backup/pelican-logs"
      }

      resources {
        cpu    = 500
        memory = 256
      }
    }
  }
}
