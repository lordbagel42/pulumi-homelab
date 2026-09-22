variable "image" {
  type = string
}

variable "replicas" {
  type    = number
  default = 1
}

variable "environment_revision" {
  type    = string
  default = ""
}

variable "serving_enabled" {
  type    = bool
  default = false
}

job "ai-proxy" {
  datacenters = ["homelab"]
  type        = "service"

  group "app" {
    count = var.replicas

    # No canary may share the database or refresh the same upstream account.
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
        to = 3000
      }
    }

    volume "data" {
      type      = "host"
      source    = "ai_proxy_data"
      read_only = false
    }

    volume "secrets" {
      type      = "host"
      source    = "ai_proxy_secrets"
      read_only = true
    }

    task "server" {
      driver       = "docker"
      user         = "1000:1000"
      kill_signal  = "SIGTERM"
      kill_timeout = "45s"

      config {
        image           = var.image
        ports           = ["http"]
        readonly_rootfs = true
        cap_drop        = ["ALL"]
        security_opt    = ["no-new-privileges"]
      }

      env {
        HOST                     = "0.0.0.0"
        PORT                     = "3000"
        NODE_ENV                 = "production"
        DATABASE_PATH            = "/data/ai-proxy.sqlite"
        AI_PROXY_ENV_FILE        = "/run/secrets/ai-proxy.env"
        AI_PROXY_SERVING_ENABLED = "${var.serving_enabled}"
        ENVIRONMENT_REVISION     = var.environment_revision
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
        name = "ai-proxy"
        port = "http"
        tags = [
          "traefik.enable=true",
          "traefik.http.routers.ai-proxy.rule=Host(`relay.raygen.dev`)",
          "traefik.http.routers.ai-proxy.entrypoints=web",
          # Only the tunnel may supply the Cloudflare client-IP header.
          "traefik.http.routers.ai-proxy.middlewares=ai-proxy-tunnel",
          "traefik.http.middlewares.ai-proxy-tunnel.ipallowlist.sourcerange=192.168.0.204/32",
        ]

        check {
          name     = "ai-proxy-health"
          type     = "http"
          path     = "/health"
          interval = "10s"
          timeout  = "3s"
        }
      }

      resources {
        cpu    = 200
        memory = 512
      }
    }
  }
}
