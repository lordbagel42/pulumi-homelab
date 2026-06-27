job "oracle-traefik" {
  datacenters = ["oracle"]
  type        = "service"

  group "traefik" {
    count = 1

    task "traefik" {
      driver = "docker"

      config {
        image        = "traefik:v__TRAEFIK_VERSION__"
        network_mode = "host"
        args = [
          "--api.dashboard=true",
          "--api.insecure=true",
          "--entrypoints.web.address=:80",
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
