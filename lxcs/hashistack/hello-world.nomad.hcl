job "hello-world" {
  datacenters = ["dc1"]
  type        = "service"

  group "web" {
    count = 1

    network {
      mode = "bridge"

      port "http" {
        to = 80
      }
    }

    service {
      name = "hello-world"
      port = "http"

      tags = [
        "traefik.enable=true",
        "traefik.http.routers.hello-world.rule=Host(`hellonomad.raygen.dev`)",
        "traefik.http.routers.hello-world.entrypoints=web",
      ]

      check {
        type     = "tcp"
        interval = "10s"
        timeout  = "2s"
      }
    }

    task "nginx" {
      driver = "docker"

      config {
        image = "nginx:alpine"
        ports = ["http"]
      }

      resources {
        cpu    = 100
        memory = 64
      }
    }
  }
}
