job "lookout" {
  datacenters = ["homelab"]
  type        = "service"

  group "lookout" {
    count = 1

    network {
      mode = "bridge"
      port "http" {
        to = 3000
      }
    }

    volume "pgdata" {
      type      = "host"
      read_only = false
      source    = "lookout_pgdata"
    }

    task "postgres" {
      driver = "docker"

      config {
        image = "postgres:16-alpine"
      }

      env {
        POSTGRES_DB       = "lookout"
        POSTGRES_USER     = "lookout"
        POSTGRES_PASSWORD = "__PG_PASSWORD__"
      }

      volume_mount {
        volume      = "pgdata"
        destination = "/var/lib/postgresql/data"
        read_only   = false
      }

      resources {
        cpu    = 300
        memory = 256
      }
    }

    task "server" {
      driver = "docker"

      config {
        image      = "ghcr.io/lordbagel42/lookout-server:latest"
        force_pull = true
        ports      = ["http"]
      }

      env {
        DATABASE_URL         = "postgresql://lookout:__PG_PASSWORD__@127.0.0.1:5432/lookout"
        PORT                 = "3000"
        BASE_URL             = "https://lookout.raygen.dev"
        ADMIN_USERNAME       = "__ADMIN_USERNAME__"
        ADMIN_PASSWORD       = "__ADMIN_PASSWORD__"
        R2_ACCOUNT_ID        = "__R2_ACCOUNT_ID__"
        R2_ACCESS_KEY_ID     = "__R2_ACCESS_KEY_ID__"
        R2_SECRET_ACCESS_KEY = "__R2_SECRET_ACCESS_KEY__"
        R2_BUCKET_NAME       = "lookout"
        R2_PUBLIC_DOMAIN     = "__R2_PUBLIC_DOMAIN__"
      }

      service {
        name = "lookout"
        port = "http"
        tags = [
          "traefik.enable=true",
          "traefik.http.routers.lookout.rule=Host(`lookout.raygen.dev`)",
          "traefik.http.routers.lookout.entrypoints=web",
          "traefik.http.routers.lookout.middlewares=lookout-cors",
          "traefik.http.middlewares.lookout-cors.headers.accessControlAllowMethods=GET,OPTIONS,PUT,POST,DELETE,PATCH",
          "traefik.http.middlewares.lookout-cors.headers.accessControlAllowHeaders=Content-Type,Authorization,X-Requested-With,X-API-Key",
          "traefik.http.middlewares.lookout-cors.headers.accessControlAllowOriginList=https://lookout.raygen.dev",
          "traefik.http.middlewares.lookout-cors.headers.accessControlAllowCredentials=true",
          "traefik.http.middlewares.lookout-cors.headers.accessControlMaxAge=100",
        ]
        check {
          type     = "http"
          path     = "/"
          interval = "30s"
          timeout  = "5s"
        }
      }

      resources {
        cpu    = 300
        memory = 512
      }
    }

    task "worker" {
      driver = "docker"

      config {
        image      = "ghcr.io/lordbagel42/lookout-worker:latest"
        force_pull = true
      }

      env {
        DATABASE_URL         = "postgresql://lookout:__PG_PASSWORD__@127.0.0.1:5432/lookout"
        R2_ACCOUNT_ID        = "__R2_ACCOUNT_ID__"
        R2_ACCESS_KEY_ID     = "__R2_ACCESS_KEY_ID__"
        R2_SECRET_ACCESS_KEY = "__R2_SECRET_ACCESS_KEY__"
        R2_BUCKET_NAME       = "lookout"
      }

      resources {
        cpu        = 2000
        memory     = 512
        memory_max = 3072
      }
    }
  }
}
