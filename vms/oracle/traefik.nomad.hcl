job "oracle-traefik" {
  datacenters = ["oracle"]
  type        = "service"

  group "traefik" {
    count = 1

    task "traefik" {
      driver = "docker"

      config {
        image        = "traefik:__TRAEFIK_VERSION__"
        network_mode = "host"
        args = [
          "--api.dashboard=true",
          "--api.insecure=true",
          "--entrypoints.web.address=:80",
          # cloudflared runs on this host and terminates TLS, so its
          # X-Forwarded-Proto must be trusted or Pelican (BEHIND_PROXY=true)
          # builds http:// URLs and redirect-loops behind the tunnel.
          "--entrypoints.web.forwardedHeaders.insecure=true",
          "--entrypoints.websecure.address=:443",
          "--entrypoints.traefik.address=:8082",
          "--providers.consulcatalog.prefix=traefik",
          "--providers.consulcatalog.exposedByDefault=false",
          "--providers.consulcatalog.constraints=Tag(`oracle`)",
          "--providers.consulcatalog.endpoint.address=127.0.0.1:8500",
          "--log.level=INFO",
          "--accessLog=true",
        ]
      }

      resources {
        cpu    = 100
        memory = 128
      }
    }
  }
}
