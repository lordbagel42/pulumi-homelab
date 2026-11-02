#!/usr/bin/env python3
"""Copy Infisical-injected environment values to mrow without files or argv secrets."""

import argparse
import base64
import json
import os
import re
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--context", required=True, help="explicit mrow kubectl context")
args = parser.parse_args()
kubectl = ["kubectl", "--context", args.context, "--request-timeout=15s"]


def run(arguments, payload=None):
    result = subprocess.run(
        arguments, input=payload, capture_output=True, text=True, check=False
    )
    if result.returncode:
        # kubectl diagnostics may contain submitted Secret data. Do not echo them.
        sys.exit("kubectl failed; inspect cluster connectivity/permissions separately")
    return result.stdout


server = run(
    kubectl
    + ["config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"]
)
if server != "https://192.168.10.1:6443":
    sys.exit(
        "refusing secrets sync: context must target mrow VIP https://192.168.10.1:6443"
    )

mapping = {
    "SECRET_KEY_BASE": "SURE_PI_SECRET_KEY_BASE",
    "ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY": "SURE_PI_ENCRYPTION_PRIMARY_KEY",
    "ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY": "SURE_PI_ENCRYPTION_DETERMINISTIC_KEY",
    "ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT": "SURE_PI_ENCRYPTION_SALT",
    "redis-password": "SURE_PI_REDIS_PASSWORD",
    "OPENAI_ACCESS_TOKEN": "SURE_PI_HACKCLUB_AI_KEY",
}
required = [*mapping.values(), "SURE_PI_POSTGRES_PASSWORD"]
missing = [key for key in required if not os.environ.get(key)]
if missing:
    sys.exit("missing Infisical values: " + ", ".join(missing))
# REDIS_URL uses Kubernetes env expansion rather than URI encoding.
if not re.fullmatch(r"[0-9a-fA-F]{64,}", os.environ["SURE_PI_REDIS_PASSWORD"]):
    sys.exit("SURE_PI_REDIS_PASSWORD must be at least 64 hexadecimal characters")

secrets = [
    ("sure-app", "Opaque", {key: os.environ[value] for key, value in mapping.items()}),
    (
        "sure-db-app",
        "kubernetes.io/basic-auth",
        {
            "username": "sure",
            "password": os.environ["SURE_PI_POSTGRES_PASSWORD"],
        },
    ),
]
payload = json.dumps(
    {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": "sure-pi"},
                "type": secret_type,
                "data": {
                    key: base64.b64encode(value.encode()).decode()
                    for key, value in data.items()
                },
            }
            for name, secret_type, data in secrets
        ],
    }
)
run(
    kubectl + ["apply", "--server-side", "--field-manager=sure-pi-secrets", "-f", "-"],
    payload,
)
print("Synced sure-pi secrets from Infisical; no values written to disk or stdout")
