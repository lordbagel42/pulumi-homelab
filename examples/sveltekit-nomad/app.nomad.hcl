variable "image" {
  type    = string
  default = "sveltekit-nomad:latest"
}

job "sveltekit-nomad" {
  datacenters = ["homelab"]
  type        = "service"

  group "web" {
    count = 1

    network {
      port "http" { to = 3000 }
    }

    task "app" {
      driver = "docker"

      config {
        image = var.image
        ports = ["http"]
      }

      service {
        name = "sveltekit-nomad"
        port = "http"

        tags = [
          "traefik.enable=true",
          "traefik.http.routers.sveltekit.rule=Host(`sveltekit.bagelindustries.com`)",
          "traefik.http.routers.sveltekit.entrypoints=web",
          "traefik.http.routers.sveltekit.middlewares=authentik@consulcatalog",
          "traefik.http.services.sveltekit.loadbalancer.server.port=3000",
        ]

        check {
          type     = "http"
          path     = "/"
          interval = "10s"
          timeout  = "2s"
        }
      }

      resources {
        cpu    = 200
        memory = 256
      }

      env {
        PORT    = "3000"
        HOST    = "0.0.0.0"
      }
    }
  }
}
