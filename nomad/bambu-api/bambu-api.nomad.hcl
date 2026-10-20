variable "image" {
  type = string
}

variable "environment_revision" {
  type    = string
  default = ""
}

job "bambu-api" {
  datacenters = ["homelab"]
  type        = "service"

  group "app" {
    # One writer for the SQLite database.
    count = 1

    update {
      max_parallel      = 1
      canary            = 0
      min_healthy_time  = "10s"
      healthy_deadline  = "3m"
      progress_deadline = "5m"
      auto_revert       = false
    }

    network {
      mode = "bridge"
      port "http" {
        to = 8080
      }
    }

    volume "data" {
      type      = "host"
      source    = "bambu_api_data"
      read_only = false
    }

    volume "secrets" {
      type      = "host"
      source    = "bambu_api_secrets"
      read_only = true
    }

    task "server" {
      driver       = "docker"
      user         = "1000:1000"
      kill_signal  = "SIGTERM"
      kill_timeout = "15s"

      config {
        image           = var.image
        ports           = ["http"]
        readonly_rootfs = true
        cap_drop        = ["ALL"]
        security_opt    = ["no-new-privileges"]
      }

      env {
        BAMBU_API_ADDR       = ":8080"
        BAMBU_DB             = "/data/bambu-api.sqlite"
        BAMBU_ENV_FILE       = "/run/secrets/bambu-api.env"
        BAMBU_PUBLIC_URL     = "https://bambuapi.raygen.dev"
        BAMBU_CORS_ORIGINS   = "https://bambu.raygen.dev"
        # The camera helper's closed plugin writes its own log directories
        # into its working directory; the task dir is the writable place on a
        # read-only rootfs.
        TMPDIR               = "$${NOMAD_TASK_DIR}"
        ENVIRONMENT_REVISION = var.environment_revision
      }

      volume_mount {
        volume      = "data"
        destination = "/data"
        read_only   = false
      }

      volume_mount {
        volume      = "secrets"
        destination = "/run/secrets"
        read_only   = true
      }

      service {
        name = "bambu-api"
        port = "http"
        tags = [
          "traefik.enable=true",
          "traefik.http.routers.bambu-api.rule=Host(`bambuapi.raygen.dev`)",
          "traefik.http.routers.bambu-api.entrypoints=web",
          # Only the tunnel may reach the service, so CF-Connecting-IP is trusted below.
          "traefik.http.routers.bambu-api.middlewares=bambu-api-tunnel",
          "traefik.http.middlewares.bambu-api-tunnel.ipallowlist.sourcerange=192.168.0.204/32",
          # Login routes call Bambu for anonymous callers; limit them per client IP.
          # Generous, because a server-side proxy puts all its users behind one IP.
          "traefik.http.routers.bambu-api-auth.rule=Host(`bambuapi.raygen.dev`) && PathPrefix(`/v1/auth/`)",
          "traefik.http.routers.bambu-api-auth.entrypoints=web",
          "traefik.http.routers.bambu-api-auth.middlewares=bambu-api-tunnel,bambu-api-auth-limit",
          "traefik.http.middlewares.bambu-api-auth-limit.ratelimit.average=300",
          "traefik.http.middlewares.bambu-api-auth-limit.ratelimit.period=1m",
          "traefik.http.middlewares.bambu-api-auth-limit.ratelimit.burst=50",
          "traefik.http.middlewares.bambu-api-auth-limit.ratelimit.sourcecriterion.requestheadername=CF-Connecting-IP",
        ]

        check {
          name     = "bambu-api-health"
          type     = "http"
          path     = "/v1/healthz"
          interval = "10s"
          timeout  = "3s"
        }
      }

      resources {
        cpu    = 300
        # Each printer being watched runs a bambu-tunnel helper that maps the
        # Studio network plugin (and the 30 MB Agora SDK) alongside the API.
        memory = 512
      }
    }
  }
}
