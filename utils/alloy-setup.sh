#!/bin/bash
# Installs Grafana Alloy natively and configures it for Grafana Cloud.
# Required env vars:
#   GCLOUD_HOSTED_LOGS_URL, GCLOUD_HOSTED_LOGS_ID
#   GCLOUD_HOSTED_METRICS_URL, GCLOUD_HOSTED_METRICS_ID
#   GCLOUD_RW_API_KEY, GCLOUD_SCRAPE_INTERVAL
#   ALLOY_NODE_LABEL
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get install -y -qq gpg wget curl

# Grafana apt repository
mkdir -p /etc/apt/keyrings/
wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor > /etc/apt/keyrings/grafana.gpg
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
  > /etc/apt/sources.list.d/grafana.list
apt-get update -y -qq
apt-get install -y -qq alloy

mkdir -p /etc/alloy

cat > /etc/alloy/config.alloy << ALLOYEOF
// ── Node metrics (Grafana Cloud node_exporter integration) ────────────────────

prometheus.exporter.unix "integrations_node_exporter" {
  disable_collectors = ["ipvs", "btrfs", "infiniband", "xfs", "zfs"]

  filesystem {
    fs_types_exclude     = "^(autofs|binfmt_misc|bpf|cgroup2?|configfs|debugfs|devpts|devtmpfs|tmpfs|fusectl|hugetlbfs|iso9660|mqueue|nsfs|overlay|proc|procfs|pstore|rpc_pipefs|securityfs|selinuxfs|squashfs|sysfs|tracefs)\$"
    mount_points_exclude = "^/(dev|proc|run/credentials/.+|sys|var/lib/docker/.+)(\$|/)"
    mount_timeout        = "5s"
  }

  netclass {
    ignored_devices = "^(veth.*|cali.*|[a-f0-9]{15})\$"
  }

  netdev {
    device_exclude = "^(veth.*|cali.*|[a-f0-9]{15})\$"
  }
}

discovery.relabel "integrations_node_exporter" {
  targets = prometheus.exporter.unix.integrations_node_exporter.targets

  rule {
    target_label = "instance"
    replacement  = "$ALLOY_NODE_LABEL"
  }

  rule {
    target_label = "job"
    replacement  = "integrations/node_exporter"
  }
}

prometheus.scrape "integrations_node_exporter" {
  targets         = discovery.relabel.integrations_node_exporter.output
  forward_to      = [prometheus.relabel.integrations_node_exporter.receiver]
  scrape_interval = "$GCLOUD_SCRAPE_INTERVAL"
}

prometheus.relabel "integrations_node_exporter" {
  forward_to = [prometheus.remote_write.metrics_service.receiver]

  rule {
    source_labels = ["__name__"]
    regex         = "node_scrape_collector_.+"
    action        = "drop"
  }
}

// ── System journal logs ───────────────────────────────────────────────────────

loki.source.journal "system" {
  max_age       = "24h0m0s"
  relabel_rules = discovery.relabel.journal_labels.rules
  forward_to    = [loki.write.grafana_cloud_loki.receiver]
}

discovery.relabel "journal_labels" {
  targets = []

  rule {
    source_labels = ["__journal__systemd_unit"]
    target_label  = "unit"
  }

  rule {
    source_labels = ["__journal__boot_id"]
    target_label  = "boot_id"
  }

  rule {
    source_labels = ["__journal__transport"]
    target_label  = "transport"
  }

  rule {
    source_labels = ["__journal_priority_keyword"]
    target_label  = "level"
  }
}

// ── /var/log files ────────────────────────────────────────────────────────────

local.file_match "var_logs" {
  path_targets = [{
    __address__ = "localhost",
    __path__    = "/var/log/{syslog,messages,*.log}",
    instance    = "$ALLOY_NODE_LABEL",
    job         = "integrations/node_exporter",
  }]
}

loki.source.file "var_logs" {
  targets    = local.file_match.var_logs.targets
  forward_to = [loki.write.grafana_cloud_loki.receiver]
}

// ── Remote write / push ───────────────────────────────────────────────────────

prometheus.remote_write "metrics_service" {
  endpoint {
    url = "$GCLOUD_HOSTED_METRICS_URL"

    basic_auth {
      username = "$GCLOUD_HOSTED_METRICS_ID"
      password = "$GCLOUD_RW_API_KEY"
    }
  }
}

loki.write "grafana_cloud_loki" {
  endpoint {
    url = "$GCLOUD_HOSTED_LOGS_URL"

    basic_auth {
      username = "$GCLOUD_HOSTED_LOGS_ID"
      password = "$GCLOUD_RW_API_KEY"
    }
  }

  external_labels = {
    cluster = "homelab",
    host    = "$ALLOY_NODE_LABEL",
  }
}
ALLOYEOF

# Append Docker container log collection if Docker is available on this node
if [ -S /var/run/docker.sock ]; then
  cat >> /etc/alloy/config.alloy << 'DOCKEREOF'

// ── Docker container logs ─────────────────────────────────────────────────────

discovery.docker "containers" {
  host = "unix:///var/run/docker.sock"
}

discovery.relabel "docker_meta" {
  targets = discovery.docker.containers.targets

  rule {
    source_labels = ["__meta_docker_container_name"]
    regex         = "/(.*)"
    target_label  = "container"
  }

  rule {
    source_labels = ["__meta_docker_container_log_stream"]
    target_label  = "stream"
  }

  rule {
    target_label = "job"
    replacement  = "docker"
  }
}

loki.source.docker "containers" {
  host       = "unix:///var/run/docker.sock"
  targets    = discovery.relabel.docker_meta.output
  forward_to = [loki.write.grafana_cloud_loki.receiver]
}
DOCKEREOF
  echo "Docker log collection enabled"
fi

usermod -aG docker alloy 2>/dev/null || true

systemctl enable alloy
systemctl restart alloy
echo "Alloy started on $ALLOY_NODE_LABEL"
