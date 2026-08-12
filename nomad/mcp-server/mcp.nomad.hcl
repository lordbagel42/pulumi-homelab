job "mcp-server" {
  datacenters = ["dc1"]
  type        = "service"

  group "mcp" {
    count = 1

    network {
      port "http" {
        to = 8080
      }
    }

    service {
      name = "mcp-server"
      port = "http"

      check {
        type     = "http"
        path     = "/health"
        interval = "10s"
        timeout  = "2s"
      }
    }

    task "server" {
      driver = "docker"

      config {
        image = "ghcr.io/lordbagel42/mcp-server:latest"
        ports = ["http"]
      }

      resources {
        cpu    = 200
        memory = 256
      }
    }
  }
}
