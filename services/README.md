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
- Prometheus proxy for the Determined master ([`native-task-resources-prometheus-proxy/`](native-task-resources-prometheus-proxy/compose.yaml), a compose project of its own)
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

Services built from this repo (`nginx`, `watchdog`) are not rebuilt by `up`: after a pull that changes `nginx/build/` or `determined-watchdog/build/`, run `docker compose build nginx watchdog` (or `docker compose up -d --build <service>`) before recreating them; otherwise the old image runs with the new configuration.

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

After a change to `grafana/.env`, recreate Grafana and the image renderer in one command, `docker compose up -d --force-recreate grafana grafana-renderer`: they must switch to the new renderer token together.

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

Older versions of the watchdog wrote the token into the last line of `prometheus.yml` instead; such a token stays in the public git history. A token that reaches a tracked file must be revoked: see [Determined scrape token](prometheus/README.md#determined-scrape-token).

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

Prometheus joins the exporters' samples to Determined tasks and allocations with the recording rules in [`prometheus/rules/`](prometheus/rules/determined-task-resources.yml). The master's native **Resources** pages and its `GET /api/v1/tasks/{task_id}/resources` API (enabled by `integrations.task_resources` in [`master.yaml`](system-configurations/etc/determined/master.yaml)) and the Grafana dashboard `det-task-resources` read them. The master reads Prometheus at `http://10.0.1.68:19090`, through [its own proxy](native-task-resources-prometheus-proxy/compose.yaml) that lets only the master's host in (see [the cluster's path](prometheus/README.md#connecting-the-determined-master)). How the pieces connect, the names the fork's master relies on, connecting the master, end-to-end checks and troubleshooting: [Determined task resources](prometheus/README.md).

## Notes

Determined-AI's [det-state-metrics](https://gpu.cvgl.lab/prom/det-state-metrics) (to view it in your browser you need to log in to https://gpu.cvgl.lab first) relates tasks to containers and GPUs, but the [official document](https://docs.determined.ai/latest/integrations/prometheus/prometheus.html) and [repo](https://github.com/determined-ai/works-with-determined) do not join it with `cAdvisor` and `dcgm-exporter`. Our [recording rules](prometheus/rules/determined-task-resources.yml) do, for the master's native Resources pages and the Grafana dashboard `det-task-resources` (see [7.5](#75-determined-task-resources)).

From fork 0.41.0 a task's allocation, container, runtime container and GPU mappings appear there `observability.task_mapping_delay` (default 5 minutes) after its allocation starts and are removed when it stops; shorter allocations never appear. See [Determined task resources](prometheus/README.md#task-dashboard-and-permissions).

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
