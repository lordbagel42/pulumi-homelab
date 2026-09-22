#!/usr/bin/env python3
"""Pin VM 233's public SSH host key, read through the authenticated Proxmox API."""
import argparse
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


def config(key):
    for _ in range(3):
        result = subprocess.run(['pulumi', 'config', 'get', key], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    raise RuntimeError(f'Cannot read Pulumi config {key}')


class Proxmox:
    def __init__(self):
        endpoint = 'PROXMOX_NETBIRD_ENDPOINT' if os.environ.get('HOSTED') == 'true' else 'PROXMOX_ENDPOINT'
        self.base = config(endpoint).rstrip('/') + '/api2/json/'
        self.context = (ssl._create_unverified_context() if config('PROXMOX_INSECURE').lower() == 'true'
                        else ssl.create_default_context())
        self.headers = {}
        ticket = self.request('access/ticket', {'username': config('PROXMOX_USERNAME'),
                                               'password': config('PROXMOX_PASSWORD')})
        self.headers = {'Cookie': 'PVEAuthCookie=' + ticket['ticket'],
                        'CSRFPreventionToken': ticket['CSRFPreventionToken']}

    def request(self, endpoint, data=None):
        body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
        request = urllib.request.Request(self.base + endpoint, data=body, headers=self.headers)
        with urllib.request.urlopen(request, context=self.context, timeout=15) as response:
            return json.load(response)['data']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    api = Proxmox()
    deadline = time.monotonic() + args.timeout
    print('Waiting for VM 233 guest agent to expose its public SSH host key...', flush=True)
    while True:
        try:
            result = api.request('nodes/optiplex/qemu/233/agent/file-read?' +
                                 urllib.parse.urlencode({'file': '/etc/ssh/ssh_host_ed25519_key.pub'}))
            host_key = result['content'].strip()
            if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?', host_key):
                raise RuntimeError('Unexpected guest SSH host-key format')
            subprocess.run(['pulumi', 'config', 'set', 'huddle-phone:sshHostKey'],
                           input=host_key, text=True, check=True, stdout=subprocess.DEVNULL)
            print('Pinned the Proxmox-verified SSH host key in huddle-phone:sshHostKey.')
            return
        except urllib.error.HTTPError as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(f'Guest agent did not become ready (HTTP {error.code})') from None
            time.sleep(3)


if __name__ == '__main__':
    main()
