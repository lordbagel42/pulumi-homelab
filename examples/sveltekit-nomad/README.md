# SvelteKit on Nomad

A minimal SvelteKit app deployed to Nomad via SST. The image is built locally,
transferred directly to the Nomad client over SSH (no external registry needed),
and submitted as a Nomad job that registers with Consul for Traefik routing and
Authentik forward-auth.

## How it works

```
sst deploy
  └─ docker build           (local)
  └─ docker save | ssh load (→ Nomad client 192.168.0.230)
  └─ nomad job run          (→ Nomad server 192.168.0.202)
        └─ Consul registers service with Traefik tags
              └─ Traefik routes sveltekit.bagelindustries.com → container
                    └─ Authentik middleware enforces login
```

SST wraps Pulumi, so `sst.config.ts` is a standard Pulumi program. The
`command` provider runs local shell commands as Pulumi resources, giving you
diffs and state tracking for free.

## Prerequisites

- Docker running locally
- SSH key with access to `192.168.0.230` (Nomad client) and `192.168.0.202` (Nomad server)
- SST CLI: `npm install -g sst` or `pnpm add -g sst`
- Homelab running (hashistack deployed)

## Local development

```bash
npm install
npm run dev
```

Runs at `http://localhost:5173`.

## Deploying

```bash
npm install
sst deploy
```

On first run, SST initialises its local state in `.sst/`. Subsequent runs diff
against that state and only rebuild/redeploy when the git hash changes.

To deploy to a named stage (maps to the image tag):

```bash
sst deploy --stage production
```

### SSH key path

By default SST looks for `~/.ssh/id_ed25519`. Override with:

```bash
SSH_KEY_PATH=~/.ssh/my_key sst deploy
```

## Authentik

The Nomad job registers with the `authentik@consulcatalog` middleware, so the
app is protected by Authentik login. To make it public, remove that middleware
tag from `app.nomad.hcl`.

To have Authentik show the app in its app list, add it to the bootstrap list in
`lxcs/authentik/authentik-setup.sh`:

```python
protected = [
    ("SvelteKit Nomad", "sveltekit-nomad", "http://sveltekit.bagelindustries.com"),
    # ...
]
```

## Customising the domain

Edit the `traefik.http.routers.sveltekit.rule` tag in `app.nomad.hcl`.
