job "oracle-cloudflared" {
  datacenters = ["oracle"]
  type        = "service"

  group "cloudflared" {
    count = 1

    task "cloudflared" {
      driver = "docker"

      config {
        image        = "cloudflare/cloudflared:latest"
        network_mode = "host"
        args = [
          "tunnel",
          "--no-autoupdate",
          "run",
          "--token",
          "__CF_TOKEN__",
        ]
      }

      resources {
        cpu    = 50
        memory = 64
      }
    }
  }
}
