job "oracle-pelican" {
  datacenters = ["dc1"]
  type        = "service"

  constraint {
    attribute = "${node.class}"
    value     = "oracle"
  }

  group "pelican" {
    count = 1

    network {
      mode = "bridge"
      port "http" {
        static = 8088
        to     = 80
      }
    }

    task "mysql" {
      driver = "docker"

      lifecycle {
        hook    = "prestart"
        sidecar = true
      }

      config {
        image   = "mariadb:10.11"
        volumes = ["/app/pelican/mysql:/var/lib/mysql"]
      }

      env {
        MYSQL_ROOT_PASSWORD = "__DB_ROOT_PASSWORD__"
        MYSQL_DATABASE      = "panel"
        MYSQL_USER          = "pelican"
        MYSQL_PASSWORD      = "__DB_PASSWORD__"
      }

      resources {
        cpu    = 300
        memory = 512
      }
    }

    task "redis" {
      driver = "docker"

      lifecycle {
        hook    = "prestart"
        sidecar = true
      }

      config {
        image = "redis:alpine"
        args  = ["--save", ""]
      }

      resources {
        cpu    = 50
        memory = 64
      }
    }

    task "panel" {
      driver = "docker"

      config {
        image = "ghcr.io/pelican-dev/panel:latest"
        volumes = [
          "/app/pelican/data:/pelican-data",
          "/app/pelican/logs:/var/www/html/storage/logs",
        ]
      }

      env {
        APP_URL      = "https://panel.bagelindustries.com"
        APP_ENV      = "production"
        APP_KEY      = "__APP_KEY__"
        APP_DEBUG    = "false"
        BEHIND_PROXY = "true"
        XDG_DATA_HOME    = "/pelican-data"
        DB_CONNECTION    = "mysql"
        DB_HOST          = "127.0.0.1"
        DB_PORT          = "3306"
        DB_DATABASE      = "panel"
        DB_USERNAME      = "pelican"
        DB_PASSWORD      = "__DB_PASSWORD__"
        CACHE_STORE      = "redis"
        SESSION_DRIVER   = "redis"
        QUEUE_CONNECTION = "redis"
        REDIS_HOST       = "127.0.0.1"
        REDIS_PORT       = "6379"
      }

      service {
        name     = "pelican"
        port     = "http"
        provider = "consul"

        tags = [
          "oracle",
          "traefik.enable=true",
          "traefik.http.routers.pelican.rule=Host(`panel.bagelindustries.com`)",
          "traefik.http.routers.pelican.entrypoints=web",
          "traefik.http.services.pelican.loadbalancer.server.port=8088",
        ]
      }

      resources {
        cpu    = 500
        memory = 512
      }
    }
  }
}
