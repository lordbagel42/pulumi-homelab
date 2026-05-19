job "demo" {
  datacenters = ["dc1"]
  type        = "service"

  group "demo" {
    count = 1

    network {
      port "http" { to = 80 }
    }

    task "whoami" {
      driver = "docker"

      config {
        image = "traefik/whoami"
        ports = ["http"]
      }

      service {
        name = "demo"
        port = "http"
        tags = [
          "traefik.enable=true",
          "traefik.http.routers.demo.rule=Host(`demo.bagelindustries.com`)",
          "traefik.http.routers.demo.entrypoints=web",
          "traefik.http.routers.demo.middlewares=authentik@consulcatalog",
        ]
      }

      resources {
        cpu    = 100
        memory = 64
      }
    }
  }
}
