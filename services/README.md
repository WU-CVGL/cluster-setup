# Supplementary Services

These are container-based supplementary services.

- [Supplementary Services](#supplementary-services)
  - [Services](#services)
    - [Core service](#core-service)
    - [Web service](#web-service)
    - [Background services](#background-services)
  - [HOW-TO](#how-to)
    - [Requirements](#requirements)
    - [First-time configurations](#first-time-configurations)
      - [2. Harbor](#2-harbor)
      - [3. Xray](#3-xray)
      - [4. Grafana, Prometheus and Wandb](#4-grafana-prometheus-and-wandb)
      - [5. System-configurations](#5-system-configurations)
      - [6. All-in-one services (except Harbor and node-exporter)](#6-all-in-one-services-except-harbor-and-node-exporter)
        - [6.1. Secrets and env files](#61-secrets-and-env-files)
      - [7. Set up endpoints for Node-exporter and other monitoring services](#7-set-up-endpoints-for-node-exporter-and-other-monitoring-services)
        - [7.1. Introduction](#71-introduction)
        - [7.2. Run](#72-run)
        - [7.3. Prometheus authentication for Determined AI (Bearer token)](#73-prometheus-authentication-for-determined-ai-bearer-token)
          - [Update from the hand-deployed state of 2026-09-28](#update-from-the-hand-deployed-state-of-2026-09-28)
          - [Rollback](#rollback)
        - [7.4. GPU health alerts (Grafana)](#74-gpu-health-alerts-grafana)
        - [7.5. Determined task resources](#75-determined-task-resources)
  - [Notes](#notes)
  - [Acknowledgments](#acknowledgments)

## Services

We use NGINX as our reverse proxy, which forwards users' HTTPS requests from their web browsers to our various backend services.

We are currently offering these web services:

### Core service

- Determined AI Master
  - [Notes](determined/README.md)

### Web service

- Homepage
  - https://cvgl.lab
- Nextcloud
  - https://pan.cvgl.lab
- Determined AI
  - https://gpu.cvgl.lab
- Harbor
  - https://harbor.cvgl.lab
- Grafana
  - https://grafana.cvgl.lab
- Weights & Biases (wandb)
  - https://wandb.cvgl.lab
- Portainer
  - https://portainer.cvgl.lab
- frp dashboard
  - https://frp.cvgl.lab
- Peng's Synology NAS (proxied to 10.0.1.69)
  - https://peng.cvgl.lab

### Background services

- NGINX
- Prometheus ([Determined task resources](prometheus/README.md))
- Grafana image renderer (`grafana-renderer`)
- [Determined watchdog](determined-watchdog/README.md) (kills idle GPU shells and JupyterLab notebooks; renews the Determined token for Prometheus)
- V2Ray Exporter
- frp (server `frps`, host network)
- RustDesk server (`hbbs`, `hbbr`, host network)
- On every node, from [`node-exporter/`](node-exporter/docker-compose.yaml) (see [7.](#7-set-up-endpoints-for-node-exporter-and-other-monitoring-services)):
  - node-exporter
  - cAdvisor (GPU nodes only)
  - DCGM-Exporter (GPU nodes only)

## HOW-TO

### Requirements

Install the [Compose plugin](https://docs.docker.com/compose/install/linux/#install-using-the-repository)
to enable [GPU support](https://docs.docker.com/compose/gpu-support/) instead of using the older version of `docker-compose` in Ubuntu (20.04).

```bash
sudo apt install docker-compose-plugin
```

### First-time configurations

(Section 1, Gitea, was removed: Gitea is no longer deployed. The numbering is kept so that links to the sections below stay valid.)

#### 2. Harbor

Check the [notes to install Harbor](../docs/04_Setup_Supplementary_Services.md#harbor).

P.S. The Harbor service is not in the all-in-one file, thus needs to be launched separately.

#### 3. Xray

Check the [note to add the configuration files](xray/README.md) and the [scripts that create a new Xray service](xray/scripts/README.md). What the proxies are for and how the machines use them: [docs/00](../docs/00_Network_Proxy.md).

#### 4. Grafana, Prometheus and Wandb

Fix the ACL permissions:

```bash
# Grafana runs as 472:0 and writes only grafana/data (gitignored).
# Do not chown the tracked files in grafana/ (custom.ini, provisioning/, .env.example),
# or a later `git pull` cannot update them.
mkdir -p grafana/data && sudo chown -R 472:0 grafana/data

# Prometheus and the watchdog run as uid 1000, the owner of the checkout (cvgladmin),
# so prometheus/ needs no chown. Their host directories are set in .env (see 6.1):
# PROMETHEUS_TSDB_DIR (the TSDB; required, on a local disk, not NFS) and
# DET_METRICS_SECRETS_DIR (the Determined scrape token; default prometheus/secrets/,
# gitignored). `git pull` creates neither: create both for uid 1000 before the first
# `docker compose up` (this also fixes a directory Docker already created as root).
# The paths below are the live ones from .env.example.
# See prometheus/README.md (Determined scrape token).
sudo install -d -o 1000 -g 1000 /home/cvgladmin/.local/share/cluster-setup-monitoring/prometheus
sudo install -d -m 0700 -o 1000 -g 1000 /home/cvgladmin/.local/share/cluster-setup-monitoring/secrets

# wandb stores its data on NFS (/srv/nfs/var/wandb/vol, see docker-compose.yml);
# chown over NFS only works if the TrueNAS share maps root (maproot).
sudo chown -R 999:0 /srv/nfs/var/wandb/vol
```

#### 5. System-configurations

Contains some [key configurations](system-configurations/etc) in `/etc`

#### 6. All-in-one services (except Harbor and node-exporter)

To launch the all-in-one services, create the env files first (see [6.1](#61-secrets-and-env-files)), then run the command in `~/ws/cluster-setup/services` on the supplementary services VM (`cvglsuppvm`, 10.0.1.68 / 192.168.233.8):

```bash
docker compose up -d
```

To rebuild one service, for example, the NGINX reverse proxy, run

```bash
docker compose build nginx
```

To force recreate some services (when changing some configurations), run

```bash
docker compose up -d --force-recreate --remove-orphans [service1 service2 ...]
```

To force recreate all services (this also recreates the watchdog: not during minute 0 of an hour, see [the watchdog README](determined-watchdog/README.md#what-it-does)):

```bash
docker compose up -d --force-recreate --remove-orphans
```

Services built from this repo (`nginx`, `watchdog`) are not rebuilt by `up`: after a pull that changes `nginx/build/` or `determined-watchdog/build/`, run `docker compose build nginx watchdog` (or `docker compose up -d --build <service>`) before recreating them; otherwise the old image runs with the new configuration. For the one-time update from the hand-deployed state of 2026-09-28, follow [7.3](#update-from-the-hand-deployed-state-of-2026-09-28).

##### 6.1. Secrets and env files

Secrets and host-specific paths are not in git. `docker-compose.yml` reads them from these files, which are all gitignored and only exist on the supplementary services VM. Create them before the first `docker compose up` (or before a `git pull` that adds a new one):

| File | Used by | How to create |
| :--- | :--- | :--- |
| `.env` (in `services/`) | Docker Compose itself, to fill in `${...}` in `docker-compose.yml`: `PROMETHEUS_TSDB_DIR` (required), `DET_METRICS_SECRETS_DIR` and, optionally, the Slack webhooks of the GPU health alerts, which only `grafana` receives (see [7.4](#74-gpu-health-alerts-grafana)). The file itself is not passed into any container | copy [`.env.example`](.env.example), which holds the live paths, and check them (owner uid 1000, TSDB on a local disk); `chmod 600` once it holds the webhooks |
| `determined-watchdog/.env` | `watchdog` | see [the watchdog README](determined-watchdog/README.md#configuration) |
| `wandb/.env` | `wandb` | wandb local server settings |
| `frp/.env` | `frp` (`frps.ini` reads `FRP_TOKEN`, `FRP_DASHBOARD_USER`, `FRP_DASHBOARD_PWD`) | copy [`frp/.env.example`](frp/.env.example) and set all three values |
| `grafana/.env` | `grafana`, `grafana-renderer` (`GF_RENDERING_RENDERER_TOKEN` and `AUTH_TOKEN`, same value) | copy [`grafana/.env.example`](grafana/.env.example) |
| `nextcloud/db.env`, `nextcloud/nextcloud.env` | `db`, `nextcloud-app` | see [the Nextcloud README](nextcloud/README.md) |

`.env` sets where Prometheus and the watchdog keep their data on the host, and where Grafana sends the GPU health alerts:

- `PROMETHEUS_TSDB_DIR`: the Prometheus TSDB, mounted at `/prometheus`. Live: `/home/cvgladmin/.local/share/cluster-setup-monitoring/prometheus` (local xfs). It has no default on purpose: while `.env` is missing, under the server's Compose 2.21 every `docker compose` command run in `services/` (also `ps`, `logs`, `exec`, `stop`, `start`, `restart`, `rm` and `down`) stops with `required variable PROMETHEUS_TSDB_DIR is missing a value: ...`. The TSDB used to be on NFS (`/srv/nfs/var/prometheus`); that directory is kept unchanged only as a rollback copy, and a default must never start Prometheus on it again.
- `DET_METRICS_SECRETS_DIR`: the directory of the Determined token file, mounted at `/run/determined-metrics` (read-write in `watchdog`, read-only in `prometheus`). Live: `/home/cvgladmin/.local/share/cluster-setup-monitoring/secrets`. Default when unset or empty: `prometheus/secrets/` (gitignored).
- `SLACK_GPU_CRITICAL_WEBHOOK_URL`, `SLACK_GPU_APP_WEBHOOK_URL`: the Slack incoming webhooks of Grafana's GPU health alerts, passed to the `grafana` service only. Optional: unset or empty, Grafana uses a dead local address and delivers no GPU alert. How to fill them in from `determined-watchdog/.env`: [7.4](#74-gpu-health-alerts-grafana).

Compose reads `.env` only from the project directory, so run `docker compose` in `services/`. A variable exported in your shell overrides `.env`. Compose 2.21 also aborts every command, not only `up`, `build` and `config`, while one of the `env_file`s is missing (newer Compose versions still run `ps`, `stop` and `rm` then). To stop or inspect a service anyway, use `docker stop services-<svc>-1` and `docker logs services-<svc>-1`, or `docker compose -p services stop <svc>` (with `-p` and without `-f`, Compose does not load `docker-compose.yml`).

Before the pull that adds an `.example` file, read it with `git show origin/main:services/<path>/.env.example` (for `.env`: `git show origin/main:services/.env.example`).

Use plain `[A-Za-z0-9]` values in the secret env files: Compose expands `$` in them, and a variable missing from `frp/.env` is rendered by frps as the literal text `<no value>`. Keep them private (`chmod 600`).

After a `git pull` that changes `frp/frps.ini` or `frp/.env` (including the first pull with the templated `frps.ini`), recreate frp right away, in the same session, with `docker compose up -d --force-recreate frp`, and check `docker compose logs --tail 20 frp`. Never use `docker restart` (or Portainer's restart) for it: a plain restart re-reads the new `frps.ini` but keeps the container's old environment, so the `FRP_*` values become `<no value>`.

The Determined bearer token that Prometheus uses is not an env file: it is the file `token` in `DET_METRICS_SECRETS_DIR` (live: `/home/cvgladmin/.local/share/cluster-setup-monitoring/secrets/token`; default `prometheus/secrets/token`). `git pull` does not create that directory. Create it private to uid 1000 before the first `docker compose up`, e.g. `sudo install -d -m 0700 -o 1000 -g 1000 prometheus/secrets` for the default; otherwise Docker creates it owned by root and the watchdog cannot write the token. See [7.3](#73-prometheus-authentication-for-determined-ai-bearer-token) and [Determined scrape token](prometheus/README.md#determined-scrape-token).

[`frp/frpc.ini`](frp/frpc.ini) is the client template handed to users. Its token is a placeholder (`REPLACE_WITH_FRP_TOKEN`): give users the real `FRP_TOKEN` out-of-band, and tell them when it is rotated.

#### 7. Set up endpoints for Node-exporter and other monitoring services

##### 7.1. Introduction

This `docker-compose.yaml` starts monitoring tools similar to the [Determined AI Docs - Configure Determined with Prometheus and Grafana](https://docs.determined.ai/latest/integrations/prometheus/prometheus.html), except that in [configure cAdvisor and dcgm-exporter](https://docs.determined.ai/latest/integrations/prometheus/prometheus.html#configure-cadvisor-and-dcgm-exporter), the official document uses `provider: startup_script: |` that only works with GCP and Azure provider, while we use our own on-premise cluster.

Instead of using that start-up script, we need to manually launch this `docker-compose.yaml` on each agent node (Maybe we can use Ansible in the future).

Monitoring tools:

- [node-exporter](https://github.com/prometheus/node_exporter)
- [cAdvisor](https://github.com/google/cadvisor)
- [dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter)

These tools will run on the cluster agents to be monitored.

##### 7.2. Run

On every node that needs to be monitored, copy the whole [`node-exporter`](./node-exporter/docker-compose.yaml) folder (`docker-compose.yaml` and `default-counters.csv`, which the `dcgm-exporter` service bind-mounts) to `~/ws/node-exporter`, replacing any older compose file there (an old `docker-compose.yml` next to `docker-compose.yaml` makes Compose warn, and starting the wrong one brings back an old configuration). Then run in that folder

```bash
# Using `docker compose` instead of `docker-compose`
docker compose up -d --force-recreate --remove-orphans
```

On VMs without a GPU, start only `docker compose up -d node-exporter` (Prometheus scrapes cAdvisor and DCGM-Exporter only on the GPU nodes).

- **Images:** all three images come from `harbor.cvgl.lab`, so every machine that runs them, VMs included, must trust the Harbor certificate first ([docs/04](../docs/04_Setup_Supplementary_Services.md#post-installation)). Harbor holds the same images as the upstream registries (`library/prom/node-exporter` is `prom/node-exporter`, `nvidia/k8s/dcgm-exporter` is `nvcr.io/nvidia/k8s/dcgm-exporter`), so no machine needs Docker Hub, `nvcr.io` or the outbound proxy to pull them.
- **Versions:** images are pinned, never `latest` (a `latest` is whatever a node or Harbor cached when it was pulled, which can be years old): node-exporter `v1.12.1`, cAdvisor `v0.60.6` (from `ghcr.io/google/cadvisor`), DCGM-Exporter `4.6.1-4.8.4` (DCGM 4.6.1; upstream publishes only the distroless image from this release on). To update one, pull the new upstream tag on a node, tag and push it to Harbor under the same pinned tag, run it next to the deployed exporter on a spare port, compare the metrics, then change the tag here and redeploy.
- **DCGM counters:** `default-counters.csv` is the list of DCGM fields exported. DCGM 4 removed the PCIe throughput fields (`DCGM_FI_DEV_PCIE_TX/RX_THROUGHPUT`), and the exporter refuses to start while the file lists a removed field. Their replacements, the `DCGM_FI_PROF_*` profiling fields, work only on cards with profiling support: the RTX 6000 Ada exports them, the GeForce cards (RTX 3090, RTX 4090) skip them with a warning at startup. `DCGM_FI_DEV_XID_ERRORS` appears only after a GPU reports an XID error, labelled with its code and message, and keeps the last code. The file also enables the exporter's own event counters: `DCGM_EXP_XID_ERRORS_COUNT` (XIDs per GPU in the last 5 minutes, labelled `xid`; a 0-valued series without `xid` when there were none), `DCGM_EXP_XID_ERRORS_TOTAL` and `DCGM_EXP_CLOCK_EVENTS_TOTAL` (both from exporter start). The throttling (`*_VIOLATION`) counters count nanoseconds.
- **SYS_ADMIN:** `dcgm-exporter` runs with `cap_add: SYS_ADMIN`, which DCGM needs to watch profiling fields. On a card that offers profiling it exits at startup without it.
- **cAdvisor and cgroup v2:** the nodes use cgroup v2. cAdvisor reports per-container RSS there (`container_memory_rss`, from the cgroup's anonymous memory); older versions such as v0.38 report it as 0.
- **Restarts:** every service has `restart: unless-stopped`, so the exporters come back after a reboot. A container without a restart policy stays stopped after a reboot, and its metrics stop.
- **Check** on each GPU node that DCGM-Exporter reports every GPU: `curl -s localhost:9400/metrics | grep -c '^DCGM_FI_DEV_GPU_UTIL'` prints the number of GPUs (`nvidia-smi -L | wc -l`).

Every service in the file has `restart: unless-stopped`, so the exporters come back after a reboot. Check it on each node:

```bash
docker inspect -f '{{.Name}} {{.HostConfig.RestartPolicy.Name}}' $(docker compose ps -aq)
```

A service without a policy (`no`), e.g. in an older copy of the file on a node, stays stopped after every reboot and Prometheus loses its metrics: copy this folder again and run the `up` command above.

Update `static_configs[targets]` in `prometheus/prometheus.yml` if any new nodes are added to the cluster.

##### 7.3. Prometheus authentication for Determined AI (Bearer token)

Prometheus reads its Determined bearer token from a private runtime file (see
[Determined scrape token](prometheus/README.md#determined-scrape-token)). Do not put tokens in YAML.

The `det-master` job in `prometheus/prometheus.yml` reads the token with `authorization.credentials_file: /run/determined-metrics/token`. On the host that is the file `token` in the directory `DET_METRICS_SECRETS_DIR` from `.env` (see [6.1](#61-secrets-and-env-files); live: `/home/cvgladmin/.local/share/cluster-setup-monitoring/secrets/token`, default: `prometheus/secrets/token`): Prometheus mounts that directory read-only at `/run/determined-metrics`, and the [Determined watchdog](determined-watchdog/README.md#determined-token-shared-with-prometheus) mounts the same directory read-write. At start and every hour it logs in to Determined and writes a new token if the file is missing, unreadable or empty, if the token's expiry cannot be decoded (so a hand-placed token that is not a Determined session token is replaced), if it expires in less than 48 hours, or if Determined answers `GET /api/v1/me` with HTTP 401 for it (details in [the watchdog README](determined-watchdog/README.md#determined-token-shared-with-prometheus)). The start-up check only logs; the hourly checks also post the outcome of a renewal to Slack. Prometheus re-reads the file on every scrape, so a new token needs no reload or restart. If the `det-master` target is down with "unable to read authorization credentials", check the watchdog logs (`docker compose logs watchdog`) and that the token directory is owned by uid 1000. If it is down with "server returned HTTP status 401 Unauthorized" (the session was revoked before its expiry), the watchdog renews the token at its next hourly check; `docker compose restart watchdog` renews it at once (silently: look for `Obtained new Determined token` in its log; never during minute 0 of an hour, see [the watchdog README](determined-watchdog/README.md#what-it-does)). To provision or replace the token by hand, follow [Determined scrape token](prometheus/README.md#determined-scrape-token).

Older versions of the watchdog wrote the token into the last line of `prometheus.yml` instead. The token tracked there is in the public git history: revoke it (step 13 below).

###### Update from the hand-deployed state of 2026-09-28

On 2026-09-28 the task-resource monitoring (PRs #3 and #4) was deployed on `cvglsuppvm` by hand, without a commit: the checkout stayed at `f71d24c` with local changes, the Prometheus TSDB moved from NFS to `/home/cvgladmin/.local/share/cluster-setup-monitoring/prometheus` (the NFS directory `/srv/nfs/var/prometheus` is kept unchanged as a rollback copy), the token directory is `/home/cvgladmin/.local/share/cluster-setup-monitoring/secrets`, and the running watchdog is PR #4's image `determined-watchdog:metrics-20260928`, which already writes only the token file. The steps below bring the checkout to `origin/main` without losing any of that: `.env` now carries the two host paths that were hard-coded in the local `docker-compose.yml`. They also replace the watchdog with the refactored image, switch frp and the Grafana image renderer to their env files, pin the images and apply the two NGINX configurations. The TSDB and the token file are used in place; nothing is copied or moved.

Effects: frp tunnels drop for a few seconds (step 8, announce it), Prometheus records no samples while it restarts and replays its WAL (step 10), and Grafana and the web vhosts restart briefly (steps 11 and 12). The watchdog posts nothing to Slack at its start.

Rules for the whole update:

- Run each block from the directory its first line `cd`s into. Stop at the first check that does not print what is described, and do not improvise around it.
- Never commit on the server (the local files may hold passwords, and older `prometheus.yml` versions held live tokens), never `git stash` or `git stash pop` (a popped `prometheus.yml` conflicts with the new one), and use `git merge --ff-only origin/main` after the checks in step 3 instead of `git pull` (a pull fetches again and could deploy something you did not check).
- After step 7, do not run a plain `docker compose up -d` (for all services) before step 9 has built the new watchdog image. The running watchdog is PR #4's (image tag `determined-watchdog:metrics-20260928`, which only writes the token file), but the updated `docker-compose.yml` refers to the untagged `determined-watchdog` image, i.e. `:latest`, and on this server `:latest` is still the pre-PR #3 watchdog, which rewrites `prometheus.yml` and restarts Prometheus through Portainer. A plain `up -d` would replace PR #4's container with that one.
- Do not start, restart or recreate the watchdog during minute 0 of an hour (while the clock shows hh:00; wait until hh:01). The new watchdog skips an alert check that comes less than 30 minutes after the previous saved one, so it cannot kill shells that the previous container warned seconds earlier, but waiting avoids even that skipped check (see [the watchdog README](determined-watchdog/README.md#what-it-does)). PR #4's watchdog (the rollback) has no such skip: started during minute 0, it runs that hour's check at once and warns again the shells warned seconds earlier. This applies to steps 9 and 13 and to the rollback.
- Several checks pass when they print nothing or `0` (`grep` then exits with status 1). Judge every check by what it prints, not by its exit status.

**Step 1.** Check the starting point (read-only). If `HEAD` or the list differs, something changed after 2026-09-29: this procedure does not cover it, so handle that change by hand first.

```sh
cd ~/ws/cluster-setup
git rev-parse --short HEAD      # must print f71d24c
git status --porcelain          # must print exactly the 10 lines below
```

```text
 M scripts/create_user.py
 M services/determined-watchdog/build/alert_config.py
 M services/determined-watchdog/build/alert_response_handler_v02.py
 M services/docker-compose.yml
 M services/flare/app/config.yml
 M services/grafana/provisioning/dashboards/dashboard.yaml
 M services/prometheus/prometheus.yml
?? services/determined-watchdog/build/metrics_token.py
?? services/grafana/provisioning/dashboards/json/determined-task-resources.json
?? services/prometheus/rules/
```

```sh
cd ~/ws/cluster-setup/services
docker compose config -q && echo compose-ok     # must print compose-ok
docker inspect -f '{{.Name}} {{.Config.Image}}{{range .Mounts}}{{println}}  {{.Source}} -> {{.Destination}} rw={{.RW}}{{end}}' services-prometheus-1 services-watchdog-1
```

`config` reads only the env files of the current (local) `docker-compose.yml` (`determined-watchdog/.env`, `wandb/.env`, `nextcloud/db.env`, `nextcloud/nextcloud.env`). If it reports "permission denied" on one of them, fix its owner (`sudo chown 1000:1000 <file>`, keep mode 600; see [the Nextcloud README](nextcloud/README.md#notes)): every later `docker compose` command runs without sudo. The `inspect` must show the image `determined-watchdog:metrics-20260928` for the watchdog and these mounts (in any order):

```text
/services-prometheus-1 prom/prometheus
  /home/cvgladmin/.local/share/cluster-setup-monitoring/prometheus -> /prometheus rw=true
  /home/cvgladmin/ws/cluster-setup/services/prometheus -> /etc/prometheus rw=false
  /home/cvgladmin/.local/share/cluster-setup-monitoring/secrets -> /run/determined-metrics rw=false
/services-watchdog-1 determined-watchdog:metrics-20260928
  /home/cvgladmin/ws/cluster-setup/services/determined-watchdog/data -> /app/data rw=true
  /home/cvgladmin/.local/share/cluster-setup-monitoring/secrets -> /run/determined-metrics rw=true
```

**Step 2.** Back up everything the update discards, and write the mount check used in steps 5 and 8. The backup directory is private (`tracked.diff` and the archive may contain passwords from `scripts/create_user.py`). If `mkdir` fails because the directory exists (an earlier attempt), rename the old one (e.g. to `refactor-update.1`) and run the block again.

```sh
cd ~/ws/cluster-setup
B=~/.cache/determined-rollout/refactor-update
mkdir -p ~/.cache/determined-rollout && (umask 077 && mkdir "$B") && echo backup-dir-ok   # must print backup-dir-ok
git status --porcelain > "$B/git-status.txt"
git diff > "$B/tracked.diff"
git ls-files -z -m -o --exclude-standard | tar --null -T - -czf "$B/local-changes.tgz"
tar -tzf "$B/local-changes.tgz" | wc -l          # must print 10
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' > "$B/containers.txt"
docker image ls > "$B/images.txt"
cat > "$B/check_mounts.py" <<'EOF'
# Compares the bind mounts Compose would give prometheus and watchdog with the running containers.
# Usage: python3 check_mounts.py [compose options, e.g. --project-directory . -f FILE]
import json, subprocess, sys
cfg = json.loads(subprocess.check_output(['docker', 'compose'] + sys.argv[1:] + ['config', '--format', 'json']))
rc = 0
for svc in ('prometheus', 'watchdog'):
    want = sorted((v['source'], v['target'], not v.get('read_only', False)) for v in cfg['services'][svc]['volumes'])
    mounts = json.loads(subprocess.check_output(['docker', 'inspect', 'services-%s-1' % svc]))[0]['Mounts']
    have = sorted((m['Source'], m['Destination'], m['RW']) for m in mounts if m['Type'] == 'bind')
    print(svc, 'SAME' if want == have else 'DIFFERENT')
    if want != have:
        rc = 1
        for label, rows in (('compose', want), ('running', have)):
            for row in rows:
                print('  %-8s %s -> %s rw=%s' % ((label,) + row))
sys.exit(rc)
EOF
```

**Step 3.** Fetch, check that `origin/main` is the update and a fast-forward, and pull the pinned images while the old containers still run.

```sh
cd ~/ws/cluster-setup
git fetch origin && git log --oneline -1 origin/main
git merge-base --is-ancestor HEAD origin/main && echo fast-forward-ok   # must print fast-forward-ok
for f in services/.env.example services/frp/.env.example services/grafana/.env.example services/determined-watchdog/build/alert_TokenManager.py; do git cat-file -e "origin/main:$f" 2>/dev/null || echo "MISSING $f"; done   # must print nothing
for i in snowdreamtech/frps:0.51.3 grafana/grafana:13.0.1-security-01 prom/prometheus:v3.11.3; do docker pull -q "$i" || echo "PULL FAILED $i"; done
for p in frp=snowdreamtech/frps:0.51.3 grafana=grafana/grafana:13.0.1-security-01 prometheus=prom/prometheus:v3.11.3; do
  if [ "$(docker inspect -f '{{.Image}}' "services-${p%%=*}-1")" = "$(docker image inspect -f '{{.Id}}' "${p#*=}")" ]; then echo "same image  ${p#*=}"; else echo "DIFFERENT   ${p#*=}"; fi
done
```

`MISSING` means that `origin/main` does not contain the update yet. The last loop must print `same image` three times: the running containers already use exactly these images, which the update only pins by tag. `DIFFERENT` means the tag now points to another build, i.e. an upgrade: stop and decide about it separately.

**Step 4.** Check that every local change is either already in `origin/main` or deliberately replaced by it (read-only).

```sh
cd ~/ws/cluster-setup
git ls-files -o --exclude-standard | while read -r f; do
  if [ "$(git hash-object "$f")" = "$(git rev-parse -q --verify "origin/main:$f")" ]; then echo "same      $f"; else echo "DIFFERENT $f"; fi
done
for f in services/determined-watchdog/build/alert_config.py services/determined-watchdog/build/alert_response_handler_v02.py; do
  if [ "$(git hash-object "$f")" = "$(git rev-parse -q --verify "444af63:$f")" ]; then echo "same      $f"; else echo "DIFFERENT $f"; fi
done
python3 - <<'EOF'
import subprocess, yaml
def new(f):
    return yaml.safe_load(subprocess.check_output(['git', 'show', 'origin/main:' + f]))
dead = ('cvglsuppvm.lan:9080', 'cvglloginnode.lan:9080')   # cAdvisor targets that origin/main drops (nothing listens there)
for f in ('services/prometheus/prometheus.yml', 'services/grafana/provisioning/dashboards/dashboard.yaml', 'services/flare/app/config.yml'):
    local = yaml.safe_load(open(f))
    if f.endswith('prometheus.yml'):
        for job in local['scrape_configs']:
            if job['job_name'] == 'cadvisor':
                for sc in job.get('static_configs', []):
                    sc['targets'] = [t for t in sc['targets'] if t not in dead]
    print('same     ' if local == new(f) else 'DIFFERENT', f)
live = yaml.safe_load(open('services/docker-compose.yml'))['services']
comp = new('services/docker-compose.yml')['services']
print('services only in the local file:', sorted(set(live) - set(comp)), '/ only in origin/main:', sorted(set(comp) - set(live)))
print('settings that differ:', ' '.join(sorted('%s.%s' % (s, k) for s in set(live) & set(comp) for k in set(live[s]) | set(comp[s]) if live[s].get(k) != comp[s].get(k))))
EOF
```

It must print exactly (the Python part needs PyYAML, which `cvglsuppvm` has; elsewhere `sudo apt install python3-yaml`):

```text
same      services/determined-watchdog/build/metrics_token.py
same      services/grafana/provisioning/dashboards/json/determined-task-resources.json
same      services/prometheus/rules/determined-task-resources.yml
same      services/determined-watchdog/build/alert_config.py
same      services/determined-watchdog/build/alert_response_handler_v02.py
same      services/prometheus/prometheus.yml
same      services/grafana/provisioning/dashboards/dashboard.yaml
same      services/flare/app/config.yml
services only in the local file: [] / only in origin/main: []
settings that differ: frp.env_file frp.image grafana-renderer.env_file grafana-renderer.environment grafana-renderer.expose grafana-renderer.ports grafana-renderer.restart grafana.env_file grafana.environment grafana.image prometheus.image prometheus.volumes watchdog.image watchdog.volumes
```

What this establishes: the three untracked files are byte-identical to `origin/main`; the two watchdog files are PR #4's code (`444af63`), which the refactored watchdog replaces; `prometheus.yml` (a YAML re-dump), `dashboard.yaml` (quoting only) and the Flare config (re-saved by Flare's editor, which wraps a long line) have the same content as `origin/main` (for `prometheus.yml` apart from the two cAdvisor targets `cvglsuppvm.lan:9080` and `cvglloginnode.lan:9080`, where nothing listens: `origin/main` drops them, and the check leaves them out of the local copy); and every difference in `docker-compose.yml` is one this update makes on purpose (image pins, env files, renderer port, and the host paths that step 5 moves into `.env` and checks). Any other `DIFFERENT` or setting is a local change that `origin/main` does not have: stop, and get it into the repository first (never by committing on the server).

Then read the local change to `scripts/create_user.py` yourself (it may contain passwords: do not paste it anywhere):

```sh
cd ~/ws/cluster-setup
git diff scripts/create_user.py
```

The new `create_user.py` no longer has a user list in the code. If the diff lists users that still have to be created, create them after the update from `scripts/new_users.csv` (step 14); the diff is saved in `$B/tracked.diff`.

**Step 5.** Create the three new env files now, before the merge: afterwards every `docker compose` command stops while one is missing. `set -C` refuses to overwrite an existing file; if an earlier attempt left an empty (0-byte) one, delete it and run the command again.

```sh
cd ~/ws/cluster-setup/services
(set -C; git show origin/main:services/.env.example > .env) && grep -E '^[A-Z_]+=' .env
for v in PROMETHEUS_TSDB_DIR DET_METRICS_SECRETS_DIR; do d=$(sed -n "s/^$v=//p" .env); stat -c '%u:%g %a %n' "$d"; findmnt -n -o FSTYPE -T "$d"; done
```

`grep` must print the two live paths, `PROMETHEUS_TSDB_DIR=/home/cvgladmin/.local/share/cluster-setup-monitoring/prometheus` and `DET_METRICS_SECRETS_DIR=/home/cvgladmin/.local/share/cluster-setup-monitoring/secrets`; both directories must be owned by `1000:1000` (the secrets directory with mode `700`), on a local filesystem (`xfs`), never `nfs`. If `.env` already existed, compare it with `git show origin/main:services/.env.example` and fix it by hand.

`frp/.env` gets the CURRENT values of the old `frp/frps.ini` (still the tracked file with literal values until the merge), so that frp clients keep working; rotate them later, in a separate, announced step. `grafana/.env` gets one new random renderer token for both variables. Neither command prints a secret. `grafana/.env` is written into `services/grafana`, so check that directory's owner first:

```sh
cd ~/ws/cluster-setup/services
stat -c '%u %n' grafana         # must print 1000 grafana
```

If it shows another uid (e.g. `472`, see step 6), run `sudo chown 1000:1000 grafana` (no `-R`: `grafana/data` stays 472; step 6 checks this directory again) and run `stat` again. Then:

```sh
cd ~/ws/cluster-setup/services
(umask 077; set -C
 awk '{ k = $0; sub(/ *=.*/, "", k); v = $0; sub(/^[^=]*= */, "", v) }
      k == "token" { print "FRP_TOKEN=" v } k == "dashboard_user" { print "FRP_DASHBOARD_USER=" v } k == "dashboard_pwd" { print "FRP_DASHBOARD_PWD=" v }' frp/frps.ini > frp/.env
 t=$(openssl rand -hex 24) && printf 'GF_RENDERING_RENDERER_TOKEN=%s\nAUTH_TOKEN=%s\n' "$t" "$t" > grafana/.env)
grep -cE '^FRP_(TOKEN|DASHBOARD_USER|DASHBOARD_PWD)=[A-Za-z0-9]+$' frp/.env        # must print 3
grep -cE '^(GF_RENDERING_RENDERER_TOKEN|AUTH_TOKEN)=[A-Za-z0-9]+$' grafana/.env    # must print 2
[ -s grafana/.env ] && [ "$(sed -n 's/^GF_RENDERING_RENDERER_TOKEN=//p' grafana/.env)" = "$(sed -n 's/^AUTH_TOKEN=//p' grafana/.env)" ] && echo same   # must print same
grep -l change-me frp/.env grafana/.env                                          # must print nothing
```

Then check the new `docker-compose.yml` with these files against the running containers:

```sh
cd ~/ws/cluster-setup/services
B=~/.cache/determined-rollout/refactor-update
git show origin/main:services/docker-compose.yml > "$B/docker-compose.new.yml"
docker compose --project-directory . -f "$B/docker-compose.new.yml" config -q && echo new-compose-ok   # must print new-compose-ok
python3 "$B/check_mounts.py" --project-directory . -f "$B/docker-compose.new.yml"                         # must print: prometheus SAME, watchdog SAME
```

`SAME` for both means that Prometheus and the watchdog get exactly the directories they use today. `DIFFERENT` lists both sides: fix `.env` and run the check again.

**Step 6.** Check that `git` can write every directory the merge touches, and fix the Grafana files. Older versions of [section 4](#4-grafana-prometheus-and-wandb) ran `chown -R 472:0 grafana/*`, so `grafana/custom.ini` and `grafana/provisioning/` belong to uid 472 and the merge (which writes into `grafana/provisioning/dashboards/`) would fail halfway.

```sh
cd ~/ws/cluster-setup
{ git diff --name-only HEAD origin/main; git ls-files -m -o --exclude-standard; } | while read -r f; do
  d=$(dirname "$f"); while [ ! -d "$d" ]; do d=$(dirname "$d"); done
  [ -w "$d" ] || echo "not writable: $d"
done | sort -u
```

Expected on `cvglsuppvm`: `services/grafana/provisioning/dashboards` and `services/grafana/provisioning/dashboards/json`. Fix them, then run the check again: it must print nothing. Grafana (uid 472) only reads these files, and they stay readable for it. Anything else listed: stop and fix its owner by hand.

```sh
cd ~/ws/cluster-setup
stat -c '%u:%g %a %n' services/grafana services/grafana/custom.ini services/grafana/provisioning services/grafana/provisioning/dashboards services/grafana/provisioning/dashboards/json
sudo chown -R 1000:1000 services/grafana/custom.ini services/grafana/provisioning   # never chown services/grafana/data
```

**Step 7.** Discard the checked local changes, remove the three untracked copies, and fast-forward, in one command so that the gap is about a second (Grafana's file provisioner rescans every few seconds and would drop the task dashboard while its file is missing; Prometheus reads `prometheus.yml` only at start or reload):

```sh
cd ~/ws/cluster-setup
[ "$(git rev-parse HEAD)" = "$(git rev-parse f71d24c)" ] \
  && git checkout -- scripts/create_user.py services/determined-watchdog/build/alert_config.py \
    services/determined-watchdog/build/alert_response_handler_v02.py services/docker-compose.yml \
    services/flare/app/config.yml services/grafana/provisioning/dashboards/dashboard.yaml \
    services/prometheus/prometheus.yml \
  && rm -f services/determined-watchdog/build/metrics_token.py \
    services/grafana/provisioning/dashboards/json/determined-task-resources.json \
    services/prometheus/rules/determined-task-resources.yml \
  && { [ ! -d services/prometheus/rules ] || rmdir services/prometheus/rules; } \
  && git merge --ff-only origin/main \
  && git status --porcelain && git log --oneline -1
```

It must end with `git log` showing the commit of `origin/main` from step 3, and `git status` must print nothing. If it stops earlier, the running containers are not affected, and what to do depends on whether git printed a line `Updating f71d24c..<commit>`:

- No `Updating` line (the error came from `git checkout --`, e.g. `unable to unlink old '...': Permission denied`, see step 6, or from `rm`/`rmdir`): nothing has been merged and `HEAD` is still `f71d24c`, but `git checkout --` may already have reset some of the 7 local files to `f71d24c`. That includes `docker-compose.yml`, which then mounts `/srv/nfs/var/prometheus` and uses the pre-PR #3 tag `determined-watchdog`, and `prometheus.yml`. Run no `docker compose` command until either the cause is fixed and the same block has run through (it is safe to repeat until the merge has happened; afterwards its first test stops it), or the hand-deployed files are restored: to go back to them, run `tar -xzf ~/.cache/determined-rollout/refactor-update/local-changes.tgz` in `~/ws/cluster-setup`. If the cause is not fixed yet, `tar` reports `Cannot open: File exists` for the files in the directories it cannot write (the Grafana ones of step 6); `git checkout --` could not change those files either. Afterwards `git status --porcelain` must print the 10 lines of step 1 again.
- `Updating f71d24c..<commit>` and then an error such as `unable to create file` or `cannot create directory`: the working tree is partly `origin/main`, including the templated `frp/frps.ini` and the new `docker-compose.yml`, while `HEAD` and the index are still `f71d24c`. Do not run the block again: its `git checkout --` would reset the listed files to `f71d24c` once more (they are backed up) and its merge is refused anyway (`... would be overwritten by merge`). Do not restart or recreate frp or any other container (and never `docker restart` frp). Fix the cause, then in `~/ws/cluster-setup` run `git reset --hard origin/main`: this step already discarded the local changes, step 2 backed them up, and ignored files such as `.env`, `frp/.env` and `grafana/.env` are kept. `git status --porcelain` must then print nothing and `git log --oneline -1` must show the commit from step 3; continue at once with step 8. To go back instead: `git reset --hard f71d24c && tar -xzf ~/.cache/determined-rollout/refactor-update/local-changes.tgz`, which restores the literal `frps.ini` and the hand-deployed `docker-compose.yml`. The files that exist only in `origin/main` stay behind as untracked (`??`) files; they do not affect the containers, but a later attempt to go forward must repeat the checks of steps 4 and 6 (step 4 then also lists those files, each of them `same`) and then use `git reset --hard origin/main` instead of this block (its merge would be refused), followed by step 8.

**Step 8.** Right after the merge, in the same session: check the Compose project, then recreate frp ([6.1](#61-secrets-and-env-files)): the merge replaced `frp/frps.ini` with the template, which the running frps would read with no `FRP_*` values at its next restart. Never plain `docker restart` (or Portainer-restart) frp.

```sh
cd ~/ws/cluster-setup/services
B=~/.cache/determined-rollout/refactor-update
docker compose config -q && echo compose-ok      # must print compose-ok
python3 "$B/check_mounts.py"                      # must print: prometheus SAME, watchdog SAME
docker compose up -d --force-recreate frp
docker compose logs --tail 20 frp                 # "frps started successfully"
sleep 60
docker compose logs --since 3m frp | grep -c 'new proxy .* success'   # more than 0: clients are back
docker compose logs --since 3m frp | grep -c "doesn't match"          # must print 0
```

Also confirm with a known frpc client (or the dashboard https://frp.cvgl.lab, with the unchanged user and password) that it reconnected. If clients are rejected ("token in login doesn't match"), `frp/.env` is wrong: compare it with `git show f71d24c:services/frp/frps.ini`, fix it and recreate frp again.

**Step 9.** Replace the watchdog with the refactored image. `up` does not rebuild it, so build first; tag the build so that a later rollback can come back to it. The first block only builds and checks the new image; it starts nothing:

```sh
cd ~/ws/cluster-setup/services
grep -iE '^WATCHDOG_DEBUG=' determined-watchdog/.env     # must be 0 (production) or 1 (debug)
stat -c '%u:%g %a %n' determined-watchdog/data determined-watchdog/data/localData   # both 1000:1000
docker compose build watchdog \
  && [ "$(docker image inspect -f '{{json .Config.Cmd}}' determined-watchdog:latest)" = '["python","alert_response_handler_v02.py"]' ] \
  && docker tag determined-watchdog:latest "determined-watchdog:refactor-$(date +%Y%m%d)" && echo build-ok   # must print build-ok
```

The `Cmd` test tells the new image apart from both old ones without starting a container: the pre-PR #3 image and PR #4's `metrics-20260928` have the shell form `["/bin/sh","-c","python alert_response_handler_v02.py"]`. If it does not print `build-ok`, stop: PR #4's watchdog keeps running untouched. Do not run the next block, because `determined-watchdog:latest` may still be the pre-PR #3 image.

Only after `build-ok`, replace the container. The clock is checked only after the old container is gone: PR #4's container ignores SIGTERM and needs about 10 s to stop, so a guard checked before that could still let the new watchdog start at hh:00, right after the old one ran that hour's check.

```sh
cd ~/ws/cluster-setup/services
T=$(sed -n 's/^DET_METRICS_SECRETS_DIR=//p' .env)/token; stat -c '%U %a %y' "$T"
docker compose rm -sf watchdog                # stops PR #4's watchdog; it ignores SIGTERM, so this takes ~10 s
[ "$(date +%M)" != 00 ] || sleep 60           # checked after the old watchdog is gone: never start it during minute 0
docker compose up -d watchdog
sleep 30; docker compose logs watchdog | grep -E 'Determined token|is_debug|base_path'
stat -c '%U %a %y' "$T"
```

Expected: `Determined token OK (expires at ...)`, `is_debug: False`, `base_path: /app/data`, and the token file unchanged (`cvgladmin 600`, same time). The start-up check is silent, so nothing appears in Slack. `Obtained new Determined token` is also fine: the token was renewed (e.g. it expired within 48 hours), and the file has a new time. `Determined token renewal FAILED (...) [start-up check: not posted to Slack]` means the old file is kept and Prometheus keeps using it: fix the cause (see [the watchdog README](determined-watchdog/README.md#determined-token-shared-with-prometheus)); the next hourly check retries and posts the result to Slack. See [the watchdog README](determined-watchdog/README.md#deploy--update) for `WATCHDOG_DEBUG` values and the data directory owner.

**Step 10.** Check the Prometheus configuration with the new mounts, then recreate Prometheus (it now runs the pinned tag of the same image, with the same directories).

```sh
cd ~/ws/cluster-setup/services
docker compose run --rm --no-deps --user 1000:1000 --entrypoint promtool prometheus check config /etc/prometheus/prometheus.yml
```

It must print `SUCCESS` for the configuration and for `rules/determined-task-resources.yml` (27 rules); do not recreate Prometheus otherwise. `--user 1000:1000` (also the service's user) is needed because the image's default user cannot enter the 0700 token directory: "permission denied" there is not a configuration error, and the directory mode must not be loosened.

```sh
cd ~/ws/cluster-setup/services
docker compose up -d --force-recreate prometheus
for i in $(seq 60); do docker compose exec -T prometheus wget -qO- http://localhost:9090/-/ready 2>/dev/null && break; sleep 5; done
docker compose exec -T prometheus wget -qO- http://localhost:9090/metrics | awk '/^prometheus_tsdb_lowest_timestamp_seconds /{printf "%.0f\n", $2}' | xargs -I{} date -d @{}
sleep 30; docker compose exec -T prometheus wget -qO- 'http://localhost:9090/api/v1/targets?scrapePool=det-master' | grep -oE '"(health|lastError)":"[^"]*"'
```

The loop must print `Prometheus Server is Ready.` (replaying the WAL can take a few minutes; if it does not within 5 minutes, read `docker compose logs --tail 50 prometheus`). The date must be about 30 days back (the retention), not today: today means that Prometheus started on an empty TSDB, so stop it at once (`docker compose stop prometheus`) and check `PROMETHEUS_TSDB_DIR`. The target check must print `"health":"up"` and `"lastError":""`.

**Step 11.** Recreate Grafana and the image renderer in ONE command: they must switch to the new renderer token together, and Grafana reads the dashboard provider only at start.

```sh
cd ~/ws/cluster-setup/services
docker compose up -d --force-recreate grafana grafana-renderer
sleep 20; curl -s localhost:10080/api/health     # "database": "ok"
docker compose ps grafana-renderer               # PORTS: 8081/tcp only, no published host port
```

Then check in Grafana (Dashboards) that "Determined Task Resources" (uid `det-task-resources`) is listed.

**Step 12.** Apply the two NGINX configurations: the reverse proxy's `nginx.conf` is built into its image, and `nextcloud-nginx` bind-mounts `nextcloud/nginx.conf` as a single file, so the running container keeps the old file until it is recreated. Tag the running proxy image first, for a rollback.

```sh
cd ~/ws/cluster-setup/services
docker tag "$(docker inspect -f '{{.Image}}' services-nginx-1)" reverseproxy:pre-refactor
docker compose build nginx && docker compose up -d --no-deps nginx
docker compose up -d --force-recreate --no-deps nextcloud-nginx
```

Then check that https://grafana.cvgl.lab (Grafana Live websocket), https://portainer.cvgl.lab (container console), https://pan.cvgl.lab and https://gpu.cvgl.lab still work.

**Step 13.** Revoke the token that was tracked in `prometheus.yml` (it is in the public git history; tokens last 7 days, so it has probably expired already). Determined's logout endpoint ends the session of the token it receives; the command never prints the token:

```sh
cd ~/ws/cluster-setup/services
M=$(sed -n 's/^DET_WEB_URL=\([^[:space:]]*\).*/\1/p' determined-watchdog/.env | tr -d "\"'"); M=${M%/}
git show f71d24c:services/prometheus/prometheus.yml | sed -n 's/^ *bearer_token: *//p' | tr -d "\"'" | {
  read -r t; curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $t" "$M/api/v1/auth/logout"; }
```

`200`: the session was still open and is now ended. `401`: it had already expired or been revoked. Anything else (e.g. `000`, `404` or a `3xx`): the request did not reach the endpoint; check `DET_WEB_URL` in `determined-watchdog/.env` (e.g. a space after `=`, which Compose ignores but this command does not) before assuming anything about the token. Then run the target check of step 10 again: `det-master` must still be UP. If it is down with HTTP 401, the revocation also ended the watchdog's session: the next hourly check renews it, or run `docker compose restart watchdog` (not during minute 0 of an hour).

**Step 14.** Final checks and follow-ups:

```sh
cd ~/ws/cluster-setup
git status --porcelain                                                     # must print nothing
docker ps -a --filter name=services- --format '{{.Names}}\t{{.Status}}'   # all Up, none Restarting; compare with ~/.cache/determined-rollout/refactor-update/containers.txt
```

- Users from the old `create_user.py` diff (step 4) that still have to be created: in `~/ws/cluster-setup`, `(umask 077; set -C; cp scripts/new_users.example.csv scripts/new_users.csv)`, one row per user, then follow [docs/02](../docs/02_User_Management.md#create-users-with-create_userpy-recommended).
- Gitea is gone: remove its git remote (it points at the removed Gitea) and, if it exists, the leftover, git-ignored `services/gitea/gitea.env` (it holds the old Gitea database credentials). Removing the remote also unsets the upstream of `main` if that was `gitea`, so set it to `origin/main` again:

  ```sh
  cd ~/ws/cluster-setup
  git remote remove gitea && git branch -q -u origin/main main && git rev-parse --abbrev-ref 'main@{upstream}'   # must print origin/main
  rm -f services/gitea/gitea.env && { [ ! -d services/gitea ] || rmdir services/gitea; } && echo gitea-gone   # must print gitea-gone
  ```

  If `rmdir` reports `Directory not empty`, `services/gitea` holds other ignored files: look at them before deleting them by hand.
- Rotate the frp credentials later, in a separate, announced step: new values in `frp/.env`, `docker compose up -d --force-recreate frp`, and hand the new `FRP_TOKEN` to the users of [`frp/frpc.ini`](frp/frpc.ini).
- Once no rollback is wanted any more (e.g. after a week): delete the `PORTAINER_*` and `PROMETHEUS_*` lines from `determined-watchdog/.env` (the PR #4 image needs `PORTAINER_WEB_URL` and `PORTAINER_API_TOKEN`), remove the images `determined-watchdog:metrics-20260928` and `reverseproxy:pre-refactor`, and delete `~/.cache/determined-rollout/refactor-update` (it may hold passwords; this also ends the "Everything" rollback below, while the image-tag rollbacks of the watchdog and NGINX stay possible) and, when no longer needed, the deployer's backup `~/.cache/determined-rollout/20260928-monitoring/backup` (it holds credentials).

###### Rollback

- Watchdog only (the one component whose code changes; not during minute 0 of an hour): go back to PR #4's image with `docker tag determined-watchdog:metrics-20260928 determined-watchdog:latest && docker compose rm -sf watchdog && { [ "$(date +%M)" != 00 ] || sleep 60; } && docker compose up -d --no-build watchdog`, and forward again with `docker tag determined-watchdog:refactor-<date> determined-watchdog:latest && docker compose rm -sf watchdog && { [ "$(date +%M)" != 00 ] || sleep 60; } && docker compose up -d --no-build watchdog` (the tag from step 9; as in step 9, the clock is checked only after PR #4's container, which takes about 10 s to stop, is gone). PR #4's code needs `PORTAINER_WEB_URL` and `PORTAINER_API_TOKEN` in `determined-watchdog/.env` (it exits at start without them) and brings back the weekly Thursday login and Slack message; it uses the same token directory and data format, but it re-initializes `data/file_info.json` at every start, so shells that are already warned are warned again and killed an hour later than planned. Never run the pre-PR #3 image that `determined-watchdog:latest` pointed to before step 9 (it is untagged now): it rewrites `prometheus.yml` and restarts Prometheus.
- NGINX: `docker tag reverseproxy:pre-refactor reverseproxy:latest && docker compose up -d --no-build --no-deps nginx`.
- Everything, back to the hand-deployed state (needs the archive from step 2): check the archive, check out `f71d24c`, restore the hand-deployed files, and check the restored `docker-compose.yml`:

  ```sh
  cd ~/ws/cluster-setup
  A=~/.cache/determined-rollout/refactor-update/local-changes.tgz
  tar -tzf "$A" >/dev/null && git checkout --detach f71d24c && tar -xzf "$A" && echo restored   # must print restored
  cd services
  grep -c 'cluster-setup-monitoring/prometheus:/prometheus' docker-compose.yml   # must print 1
  grep -c /srv/nfs/var/prometheus docker-compose.yml                             # must print 0
  ```

  If `restored` is missing or a count differs, run no `docker compose` command: `f71d24c`'s own `docker-compose.yml` would start Prometheus on the NFS rollback copy `/srv/nfs/var/prometheus` and give the watchdog the pre-PR #3 setup. A missing or unreadable archive stops the line before the checkout (`HEAD` stays on `main`, nothing changed). While `~/.cache/determined-rollout/refactor-update` exists, `python3 ~/.cache/determined-rollout/refactor-update/check_mounts.py` (in `services/`) must also print `prometheus SAME` and `watchdog SAME`. Only then, in `services/`: `docker compose up -d --force-recreate frp grafana grafana-renderer prometheus watchdog`, the NGINX line above and `docker compose up -d --force-recreate --no-deps nextcloud-nginx`. The old `docker-compose.yml` has the absolute paths and the `metrics-20260928` tag, uses no variable from `.env`, and has the old renderer token and literal frp credentials, so those services match again. To go forward again later: step 7 (it discards the restored files and fast-forwards the detached `HEAD`), `git checkout main`, then steps 8 to 12.
- The deployer's backup of 2026-09-28, `/home/cvgladmin/.cache/determined-rollout/20260928-monitoring/backup`, holds the configuration and container metadata from before the hand deployment (with credentials).
- Both directions use the TSDB and the token file in place. The NFS copy `/srv/nfs/var/prometheus` is not touched by this update: never point `PROMETHEUS_TSDB_DIR` (or a compose file) at it without an explicit decision about the samples written since 2026-09-28, and never run two Prometheus processes on one TSDB.

##### 7.4. GPU health alerts (Grafana)

Grafana provisions the GPU health alerts from [`grafana/provisioning/alerting/gpu-health.yaml`](grafana/provisioning/alerting/gpu-health.yaml): the folder **GPU health** with the rule group `gpu-health` (evaluated every minute) and two Slack contact points, `slack-gpu-critical` and `slack-gpu-app`. The **GPU health** row at the top of the "NVIDIA DCGM Exporter Dashboard" (uid `Oxed_c6Wz`) shows the same signals, plus throttling, PCIe replays, row remapping and temperatures. The XID rules and panels need the DCGM-Exporter counters `DCGM_EXP_XID_ERRORS_COUNT`, `DCGM_EXP_XID_ERRORS_TOTAL` and `DCGM_EXP_CLOCK_EVENTS_TOTAL` from [`node-exporter/default-counters.csv`](node-exporter/default-counters.csv) (see [7.2](#72-run)); until a node exports them, its XID rules see no data, which counts as normal.

| Rule (alert name) | Fires when | Pending period | Slack |
| :--- | :--- | :--- | :--- |
| GPU XID error (hardware or driver) | a GPU reported an XID other than 13, 31, 43 and 45 in DCGM-Exporter's 5-minute window; unknown codes included | none | `slack-gpu-critical` |
| GPU XID error (application) | a GPU reported XID 13, 31, 43 or 45 (usually caused by the user's job: illegal memory access, page fault, killed job) | none | `slack-gpu-app` |
| DCGM-Exporter down | Prometheus cannot scrape DCGM-Exporter on a node | 5 min | `slack-gpu-critical` |
| GPUs missing from DCGM | fewer than 8 GPUs (distinct `gpu_uuid`) report on a node, or none while its exporter is up | 5 min | `slack-gpu-critical` |

- **Messages:** one Slack message per rule and node. Labels: `node`, `gpu`, `gpu_uuid`, `modelName`, `xid` (where they apply) and `severity` (`critical`, or `info` for application XIDs). Annotations: a summary with a short meaning of the common XID codes, what to check on the node, the NVIDIA XID catalog and a dashboard link with the node preselected. A firing alert is repeated every 4 hours (application XIDs: 12 hours), and a resolved message follows when it clears. An XID alert clears 5 to 10 minutes after the last XID of that code, when it leaves the exporter's window.
- **XID source:** the rules read `DCGM_EXP_XID_ERRORS_COUNT` (XIDs per GPU and code in the last 5 minutes). `DCGM_FI_DEV_XID_ERRORS` (the last code, kept until another code arrives or the exporter restarts) is shown on the dashboard only: an alert on it would never resolve.
- **Not covered:** XIDs that happen while DCGM-Exporter is down or restarting are never seen. When Prometheus cannot be queried, the rules keep their last state (`KeepLast`) and send nothing, so a Prometheus outage does not alert here. No data (no XID, or no exporter counters yet) is normal (`OK`).
- **Expected GPU count:** 8, the GPU count of every GPU node, is the threshold of `GPUs missing from DCGM` in the alerting file. For a node that runs with fewer GPUs on purpose, or a node in maintenance (it also raises `DCGM-Exporter down`), add a silence in Grafana (Alerting > Silences) with the matcher `node=cvgl-nodeXX.lan`.
- **Watchdog and IdleKillAlert:** the watchdog acts only on the alert named `GRAFANA_ALERT_NAME` (`IdleKillAlert`, kept in Grafana's database, folder `test`), so these alerts never warn or kill anything. The file routes each rule with its own contact point (`notification_settings`) and has no `policies` section, so the default notification policy and IdleKillAlert are left as they are.
- **Editing:** provisioned rules and contact points are read-only in the UI; change the file. Grafana replaces `$NAME` in the file with environment variables, except in annotations and queries (rules in the file header). Anonymous viewers can read rules and annotations: never put a secret there.

**Slack webhooks.** Compose passes `SLACK_GPU_CRITICAL_WEBHOOK_URL` and `SLACK_GPU_APP_WEBHOOK_URL` from `.env` (see [6.1](#61-secrets-and-env-files)) to the `grafana` service only, not to `grafana-renderer`. Unset or empty, each defaults to a dead local address (`http://127.0.0.1:9/...`): Grafana starts and evaluates the rules, the alerts show in Alerting, and every delivery fails with `Failed to send Slack message ... connection refused` in `docker compose logs grafana`. Never hand Grafana an empty URL by other means (e.g. `docker run -e SLACK_GPU_CRITICAL_WEBHOOK_URL=`): it validates the Slack URL at start and exits, and `restart: unless-stopped` then restarts it in a loop. Grafana stores the URLs as secure settings (shown as `[REDACTED]`), but a failed delivery logs the full URL: do not share Grafana logs. To use the watchdog's channels (critical alerts to `SLACK_WEBHOOK_URL`, application XIDs to `SLACK_WEBHOOK_URL_DEBUG`), copy them without printing them:

```sh
cd ~/ws/cluster-setup/services
grep -c '^SLACK_GPU_' .env    # must print 0; otherwise the lines exist already: edit them instead
c=$(sed -n 's/^SLACK_WEBHOOK_URL=//p' determined-watchdog/.env | tr -d "\"' ")
a=$(sed -n 's/^SLACK_WEBHOOK_URL_DEBUG=//p' determined-watchdog/.env | tr -d "\"' ")
printf 'SLACK_GPU_CRITICAL_WEBHOOK_URL=%s\nSLACK_GPU_APP_WEBHOOK_URL=%s\n' "$c" "$a" >> .env; unset c a
chmod 600 .env
grep -cE '^SLACK_GPU_(CRITICAL|APP)_WEBHOOK_URL=https://hooks\.slack\.com/' .env   # must print 2
docker compose config grafana | grep -c 'SLACK_GPU_.*hooks\.slack\.com'           # must print 2
docker compose config grafana-renderer | grep -c SLACK_GPU                       # must print 0
```

A new or changed value reaches Grafana only when the container is recreated: `docker compose up -d --no-deps grafana`. A plain `docker compose restart grafana` keeps the old environment.

**Deploy** (on `cvglsuppvm`, in `~/ws/cluster-setup/services`; not during minute 0 of an hour, when the watchdog asks Grafana for alerts):

1. Optional: set the webhooks in `.env` (above). Without them, everything below works and no GPU alert is delivered.
2. `stat -c '%u %n' grafana/provisioning` must print `1000 grafana/provisioning`: the update creates `grafana/provisioning/alerting/` in it. If it prints `472`, run `sudo chown 1000:1000 grafana/provisioning` (never `grafana/data`; see [4](#4-grafana-prometheus-and-wandb)).
3. Update the checkout (`git pull`, or `git merge --ff-only` after a `git fetch`). Grafana picks up the dashboard change within about 10 seconds; the alerting file waits for step 4.
4. Recreate Grafana, which loads the alerting file and the new environment: `docker compose up -d --no-deps grafana`. Grafana is unavailable for about 20 seconds.
5. Check:

   ```sh
   sleep 20; docker compose ps grafana                    # Up, not Restarting
   docker compose logs grafana | grep -E 'provision(ing)? alerting|Failed to provision'   # "finished to provision alerting", no "Failed"
   curl -s localhost:10080/api/health                      # "database": "ok"
   ```

   In Grafana, Alerting > Alert rules lists the folder **GPU health** with the 4 rules, and IdleKillAlert is unchanged in folder `test`. To send a test message through a contact point (Grafana admin password; the long name is the contact point name in unpadded base64url, `c2xhY2stZ3B1LWFwcA` for `slack-gpu-app`):

   ```sh
   curl -s -u admin -X POST -H 'Content-Type: application/json' \
     localhost:10080/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers/c2xhY2stZ3B1LWNyaXRpY2Fs/test \
     -d '{"integration":{"uid":"slack-gpu-critical","type":"slack","version":"v1","settings":{},"secureFields":{"url":true}}}'
   ```

   `{"status":"success",...}` means Slack accepted it.

Later changes to the alerting file need no restart: after the pull, `curl -s -u admin -X POST localhost:10080/api/admin/provisioning/alerting/reload` (Grafana server admin only). A file that fails to load returns HTTP 500 and Grafana keeps running with the previous rules; the same file at a restart or recreate stops Grafana. Test a changed file on a scratch Grafana first, from `services/` on any Docker host:

```sh
docker run -d --name grafana-dryrun -p 127.0.0.1:13000:3000 \
  -e SLACK_GPU_CRITICAL_WEBHOOK_URL=http://127.0.0.1:9/dryrun -e SLACK_GPU_APP_WEBHOOK_URL=http://127.0.0.1:9/dryrun \
  -v "$PWD/grafana/provisioning:/etc/grafana/provisioning:ro" grafana/grafana:13.0.1-security-01
sleep 20; docker logs grafana-dryrun 2>&1 | grep -E 'provision(ing)? alerting|Failed to provision'   # "finished to provision alerting"
curl -s -u admin:admin localhost:13000/api/v1/provisioning/alert-rules | grep -o '"title":"[^"]*"'
docker rm -f grafana-dryrun
```

**Rollback:**

- Grafana restarts in a loop after step 4 (`Failed to provision alerting` in its log): take the file out of the provisioning directory (only `.yaml`, `.yml` and `.json` files are read) and recreate: `mv grafana/provisioning/alerting/gpu-health.yaml grafana/provisioning/alerting/gpu-health.yaml.off && docker compose up -d --no-deps grafana`. Fix the cause, move the file back (`git status` must be clean again) and recreate.
- Removing the file does not remove what it provisioned: the rules and contact points stay in Grafana's database. To delete them, replace the file's content with:

  ```yaml
  apiVersion: 1
  deleteRules:
    - orgId: 1
      uid: gpu-xid-critical
    - orgId: 1
      uid: gpu-xid-app
    - orgId: 1
      uid: dcgm-exporter-down
    - orgId: 1
      uid: gpu-missing
  deleteContactPoints:
    - orgId: 1
      uid: slack-gpu-critical
    - orgId: 1
      uid: slack-gpu-app
  ```

  and reload or recreate. The empty folder **GPU health** can then be deleted in the UI.
- Dashboard: check out the previous `grafana/provisioning/dashboards/json/dcgm-exporter-dashboard.json`; Grafana applies it within about 10 seconds.

##### 7.5. Determined task resources

Prometheus joins the exporters' samples to Determined tasks and allocations with the recording rules in [`prometheus/rules/`](prometheus/rules/determined-task-resources.yml). The master's native **Resources** pages and its `GET /api/v1/tasks/{task_id}/resources` API (enabled by `integrations.task_resources` in [`master.yaml`](system-configurations/etc/determined/master.yaml)) and the Grafana dashboard `det-task-resources` read them. How the pieces connect, the names the fork's master relies on, connecting the master, end-to-end checks and troubleshooting: [Determined task resources](prometheus/README.md).

## Notes

Determined-AI's [det-state-metrics](https://gpu.cvgl.lab/prom/det-state-metrics) (to view it in your browser you need to log in to https://gpu.cvgl.lab first) relates tasks to containers and GPUs, but the [official document](https://docs.determined.ai/latest/integrations/prometheus/prometheus.html) and [repo](https://github.com/determined-ai/works-with-determined) do not join it with `cAdvisor` and `dcgm-exporter`. Our [recording rules](prometheus/rules/determined-task-resources.yml) do, for the master's native Resources pages and the Grafana dashboard `det-task-resources` (see [7.5](#75-determined-task-resources)).

They follow this chain. In `https://gpu.cvgl.lab/prom/det-state-metrics`, each job will have an `allocation_id`. With this `allocation_id`, you can get the corresponding `container_id` in `det_container_id_allocation_id`.

With this `container_id`, you can:

- Get `container_runtime_id` in `det_container_id_runtime_container_id`
- Get `gpu_uuid` in `det_gpu_uuid_container_id`

With `container_runtime_id`, you can get container stats of this job with `cAdvisor`.

With `gpu_uuid`, you can get GPU stats of this job with `dcgm-exporter`.

TODOs:

- A management watchdog that utilizes these data and kills tasks (the existing
  [determined-watchdog](determined-watchdog/README.md) only kills idle shells and JupyterLab
  notebooks; it acts on a Grafana alert and does not use the task-resource recording rules)

## Acknowledgments

https://github.com/stefan0us/xray-traefik

https://github.com/nginx/nginx

https://github.com/determined-ai/determined

https://github.com/WU-CVGL/determined (our fork, which this cluster runs)

https://github.com/nextcloud/server

https://github.com/goharbor/harbor

https://github.com/XTLS/Xray-core

https://github.com/grafana/grafana

https://github.com/prometheus/prometheus

https://github.com/prometheus/node_exporter

https://github.com/google/cadvisor

https://github.com/NVIDIA/dcgm-exporter

https://github.com/wi1dcard/v2ray-exporter

https://github.com/soulteary/docker-flare

https://github.com/fatedier/frp

https://github.com/snowdreamtech/frp
