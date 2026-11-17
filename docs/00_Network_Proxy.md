# Network Proxy

## Contents

- [Network Proxy](#network-proxy)
  - [Contents](#contents)
  - [Why a proxy](#why-a-proxy)
  - [The proxies on the supplementary services VM](#the-proxies-on-the-supplementary-services-vm)
  - [The HA entry](#the-ha-entry)
  - [Admin entries](#admin-entries)
    - [Admin accounts](#admin-accounts)
    - [Classify a new line](#classify-a-new-line)
  - [Accounts](#accounts)
    - [Manage accounts](#manage-accounts)
    - [Client settings](#client-settings)
    - [Docker daemon with an account](#docker-daemon-with-an-account)
    - [Usage](#usage)
    - [Accounts on the lines](#accounts-on-the-lines)
    - [Changes from the portal](#changes-from-the-portal)
  - [Proxy availability](#proxy-availability)
  - [Bootstrap: a temporary proxy on one machine](#bootstrap-a-temporary-proxy-on-one-machine)
  - [Verify a proxy](#verify-a-proxy)
  - [Use the proxy](#use-the-proxy)
    - [Docker daemon](#docker-daemon)
    - [Environment variables](#environment-variables)
    - [Proxychains](#proxychains)
    - [pip and git](#pip-and-git)

## Why a proxy

From the campus network, Docker Hub is blocked and GitHub is unreliable (`git` often times out; `*.github.io` is often blocked), while PyPI and the Chinese mirrors work. Pulling images, installing the NVIDIA container toolkit, downloading GitHub releases (e.g. [our Determined fork](03_Setup_DeterminedAI.md#installation)) and fetching this repository therefore go through an HTTP or SOCKS5 proxy. Set it up before everything else: chapters [01](01_First-time_Setup_of_Cluster_Nodes.md) and [04](04_Setup_Supplementary_Services.md) use it.

## The proxies on the supplementary services VM

In production, the proxies run as Docker Compose services on the supplementary services VM (`10.0.1.68` on the campus network, `192.168.233.8` on the private network). Each `xray-<name>` service in [`services/docker-compose.yml`](../services/docker-compose.yml) forwards to one upstream server and publishes an HTTP and a SOCKS5 port on the VM. In the configurations that our tools generate, private addresses and destinations in China go out directly. A `xray-<name>-exporter` next to each one feeds the v2ray dashboard in Grafana.

The default proxy of the cluster is `xray-usca5-bwh-sla-1tb`:

| Protocol | Private network | Campus network |
| :--- | :--- | :--- |
| HTTP | `http://192.168.233.8:59889` | `http://10.0.1.68:59889` |
| SOCKS5 | `192.168.233.8:59880` | `10.0.1.68:59880` |

Four lines are public, open to every user: `xray-usca5-bwh-sla-1tb`, `xray-usca1-bwh-megaboxpro-2tb`, `xray-usca6-bwh-1tb` and `xray-us-west-racknerd-12tb`. They work the same way, on other ports. The other lines are private, for admins only ([Admin entries](#admin-entries)). To list the services, on the VM:

```sh
cd ~/ws/cluster-setup/services
docker compose ps --format '{{.Service}}\t{{.Ports}}' | grep -v exporter | grep xray
```

Each service looks like this (the full file is [`services/docker-compose.yml`](../services/docker-compose.yml)); its `config.json` is not in git:

```yaml
services:
  xray-usca5-bwh-sla-1tb:
    image: teddysun/xray:latest
    restart: unless-stopped
    environment:
      TZ: Asia/Shanghai
    networks:
      - grafana_monitor
    ports:
      - 59880:1089     # SOCKS5
      - 59889:8889     # HTTP
    volumes:
      - ./xray/usca5-bwh-sla-1tb/config:/etc/xray
      - ./xray/usca5-bwh-sla-1tb/log:/var/log/xray
    expose:
      - 10085          # stats API, read by the exporter

  xray-usca5-bwh-sla-1tb-exporter:
    image: wi1dcard/v2ray-exporter:master
    environment:
      TZ: Asia/Shanghai
    networks:
      - grafana_monitor
    restart: unless-stopped
    command: 'v2ray-exporter --v2ray-endpoint "xray-usca5-bwh-sla-1tb:10085" --listen ":9550"'
    expose:
      - 9550
```

Add a new proxy from a share link (`vmess://`, `vless://`, `trojan://`, `ss://`) with [`create_xray_service.py`](../services/xray/scripts/README.md): it writes the `config.json`, adds both services to `docker-compose.yml` and the exporter to Prometheus. It requires `--public` or `--private` ([Classify a new line](#classify-a-new-line)). See the [Xray notes](../services/xray/README.md) for the configuration and [XTLS/Xray-examples](https://github.com/XTLS/Xray-examples) for reference configurations.

## The HA entry

`xray-ha` is one more entry on the same VM that does not depend on a single line:

| Protocol | Private network | Campus network |
| :--- | :--- | :--- |
| HTTP | `http://192.168.233.8:30889` | `http://10.0.1.68:30889` |
| SOCKS5 | `192.168.233.8:30089` | `10.0.1.68:30089` |

Both ports require a proxy [account](#accounts). It forwards each new connection over SOCKS5 to one line of its pool `ha-general`, the four public lines: `xray-usca5-bwh-sla-1tb`, `xray-usca1-bwh-megaboxpro-2tb`, `xray-usca6-bwh-1tb` and `xray-us-west-racknerd-12tb`. Every 15 seconds Xray fetches `https://www.google.com/generate_204` through each line (its observatory). A new connection goes to the line with the lowest latency among those whose last fetch succeeded (the `leastPing` balancer). The private lines are not in the pool; admins have a second entry over every line, [`ha-all`](#admin-entries).

What it covers and what it does not:

- **A failed line**, whether its server, its route abroad or its `xray-<name>` service: new connections go to another line once the next fetch through it has failed, so within about one probe interval. Requests that start in between can fail.
- **No fixed order.** The pool has no primary and backup: the line, and with it the exit address, can change from one connection to the next. To pin a line, use its own entry (the table above and `docker compose ps`).
- **Open connections stay on their line.** A download through a line that fails breaks, and the client has to retry.
- **Fails closed.** When no line of the pool passes, the entry refuses every request, also for destinations in China, instead of going out directly. The entry has no route of its own: private and Chinese destinations go out directly at the line, as through the line's own entry.
- **Starts closed.** After `xray-ha` starts, it refuses requests until its first fetches through the lines are done, a few seconds.
- **Not the VM.** The entry, its lines and the monitoring all run on the supplementary services VM, so a failure of the VM or of the management server stops them together.

The configuration is [`services/xray-ha/config.example.json`](../services/xray-ha/config.example.json). The service reads `services/xray-ha/config/config.json`, which holds the accounts and is not in git: [`proxy_accounts.py`](#manage-accounts) renders it from the example. To set the entry up:

```sh
cd ~/ws/cluster-setup/services
python3 xray/scripts/proxy_accounts.py add svc-monitor
python3 xray/scripts/proxy_accounts.py add svc-ha
python3 xray/scripts/proxy_accounts.py render   # before the first start, or Docker creates the folders as root
docker compose up -d xray-ha xray-ha-exporter blackbox-exporter
docker compose kill -s SIGHUP prometheus   # reload the probe job
docker compose restart grafana             # load the new dashboard providers
```

The pool is explicit, and [`create_xray_service.py`](../services/xray/scripts/README.md) does not add new lines to it. To add a public line, give it a SOCKS outbound whose tag starts with `pool-`, as the others have, and set the `pool` label of its probe targets to `ha-general` in [`prometheus.yml`](../services/prometheus/prometheus.yml). `render` refuses a `pool-` outbound to a line that is not public.

Move clients to the HA entry only after its ports work from where they are used: run `curl -x http://<user>:<password>@192.168.233.8:30889 https://www.google.com/generate_204 -w '%{http_code}\n'` on a node and the same with `10.0.1.68` on the login node, then the same with `socks5h://` and port `30089`. Then move one kind of client at a time: environment variables, `pip` and `git` first, then the [Docker daemons](#docker-daemon-with-an-account), one drained node at a time, keeping `NO_PROXY` as it is. While the lines keep their own entries without accounts, moving a client back is changing the URL back.

## Admin entries

These entries accept only admin accounts. Regular users use `ha-general` and the public lines.

| Entry | Lines | Who may log in | HTTP | SOCKS5 |
| :--- | :--- | :--- | :--- | :--- |
| `ha-general` | the four public lines | every enabled account | `30889` | `30089` |
| `ha-all` | all nine lines, public and private | enabled admins | `31889` | `31089` |
| a public line's own entry | that line | everyone; with [raw auth](#accounts-on-the-lines), every enabled account | its own | its own |
| a private line's own entry | that line | enabled admins, always | its own | its own |

The ports are on `192.168.233.8` and `10.0.1.68`, as for `ha-general`. The private lines are `xray-jpty1-bwh-2tb` and the four `xray-miyaip-*` relays; `docker compose ps` on the VM shows their ports. Client settings are those of [`ha-general`](#client-settings) with the `ha-all` ports.

- **`ha-all`** runs in the same `xray-ha` service as `ha-general`, on two more ports, and balances over all nine lines in the same way (`leastPing`, the same observatory, failing closed when no line passes). `ha-general` keeps its own pool and users, and `render` checks that it can reach no private line.
- **Private lines** always require an account, whatever raw auth says. Besides the admins, they accept `svc-ha` (the HA entry's connections) and `svc-monitor` (the probes).
- **A disabled admin** is refused everywhere accounts are required, like any disabled account. While raw auth is off, the public lines' own entries still accept them.

### Admin accounts

An admin is an account with the admin flag. Service accounts cannot be admins.

```sh
cd ~/ws/cluster-setup/services
python3 xray/scripts/proxy_accounts.py admin <user> on    # or off
python3 xray/scripts/proxy_accounts.py render
docker compose restart xray-ha xray-jpty1-bwh-2tb $(docker compose config --services | grep '^xray-miyaip-' | grep -v exporter)
```

`render` lists the files it wrote; restart the services they belong to. A change to an admin (on, off, a new password, disable, enable, remove) changes the configs of `xray-ha` and every private line. The [portal](#changes-from-the-portal) does the same with `admin-on` and `admin-off` requests. `list` marks the admins and `status` shows the flag of each account and the access of each line.

To set `ha-all` up on a VM that runs `ha-general`: stop the [applier](#changes-from-the-portal) (`sudo systemctl stop proxy-apply.path proxy-apply.timer`), since it verifies through `ha-all` and would roll back every request until the new ports are published; `render`, then `docker compose up -d xray-ha` (a restart does not publish new ports), restart the private lines, and `docker compose kill -s SIGHUP blackbox-exporter prometheus`. From the private lines' restart on, their own entries refuse every client without an admin account: move such clients to `ha-general` first. Then start the applier again (`sudo systemctl start proxy-apply.path proxy-apply.timer`).

### Classify a new line

[`services/xray/public-lines.txt`](../services/xray/public-lines.txt) lists the public lines; every line not listed is private. A line nobody classified is therefore admin-only, never open to everyone. [`create_xray_service.py`](../services/xray/scripts/README.md) requires `--public` (it adds the line to the list) or `--private`. A private line requires accounts from its creation.

- **Public line:** listed; for `ha-general` and `ha-all`, a `pool-<line>` outbound in [`config.example.json`](../services/xray-ha/config.example.json) and the probe targets with `pool: ha-general`, `access: public`.
- **Private line:** not listed; for `ha-all`, a `private-<line>` outbound and the probe targets with `pool: ha-all`, `access: admin-only`. Its probe modules in `blackbox.example.yml` carry `svc-monitor:PASSWORD@`.

To reclassify a line, edit the list, move its outbound between `pool-` and `private-`, update its probe labels, then `render` and restart `xray-ha` and the line.

## Accounts

Every cluster user has one proxy account, named after their cluster username; an admin creates it and hands out the password. Accounts whose names start with `svc-` are service accounts: `svc-docker-<node>` (`<node>` is the node's hostname) for the Docker daemon of a node, `svc-monitor` for the [probes](#proxy-availability) and `svc-ha` for the HA entry's connections to the lines.

### Manage accounts

The accounts live in one file on the VM, `services/proxy-accounts/accounts.json` (mode `0600`, not in git). Admins change it with [`proxy_accounts.py`](../services/xray/scripts/README.md#代理账号-proxy_accountspy), and `render` writes every configuration that holds passwords from it: the HA entry's, the probes', the private lines' and, once the public lines require accounts, theirs. On the VM, nothing else holds a copy. Outside it, users keep their own passwords and each node keeps its Docker daemon's in `proxy.env`: after `rotate svc-docker-<node>`, update that file and restart Docker on the drained node ([Docker daemon with an account](#docker-daemon-with-an-account)).

```sh
cd ~/ws/cluster-setup/services
python3 xray/scripts/proxy_accounts.py add <user> --show      # prints the new password
python3 xray/scripts/proxy_accounts.py rotate <user> --show   # new password; the old one stops working
python3 xray/scripts/proxy_accounts.py remove <user>
python3 xray/scripts/proxy_accounts.py disable <user>         # keeps the account and its password
python3 xray/scripts/proxy_accounts.py enable <user>
python3 xray/scripts/proxy_accounts.py admin <user> on|off    # admin entries
python3 xray/scripts/proxy_accounts.py list                   # names, admins marked
python3 xray/scripts/proxy_accounts.py status                 # accounts and lines; --json for the portal
python3 xray/scripts/proxy_accounts.py render                 # lists the files it wrote
docker compose restart xray-ha                                # and the private lines when render wrote theirs
docker compose kill -s SIGHUP blackbox-exporter
```

Passwords are random and are printed only by `add` and `rotate` with `--show`; give them to the user privately. A disabled account is left out of every rendered configuration, so every entry that requires accounts refuses it. While the [public lines require no accounts](#accounts-on-the-lines) (the default), that is the HA entries and the private lines: a disabled user can still use a public line's own entry. `svc-ha` and `svc-monitor` cannot be disabled or removed. The accounts file (format version 3) records for each account whether it is enabled and whether it is an admin; an older file is read with no admins (version 2), or with every account enabled and no admins (version 1), and is rewritten as version 3 at the next change. Xray reads accounts only when it starts, so each change takes effect with the restart of `xray-ha`, which breaks the connections open through it and resets its traffic counters. While the [lines require accounts](#accounts-on-the-lines), restart them as well.

### Client settings

| Protocol | Proxy URL |
| :--- | :--- |
| HTTP | `http://<user>:<password>@192.168.233.8:30889` |
| SOCKS5 | `socks5h://<user>:<password>@192.168.233.8:30089` |

From the campus network (the login node), use `10.0.1.68`. `socks5h` lets the proxy resolve names. The passwords contain only URL-safe characters, so they go into the URL as they are. For example:

```bash
export http_proxy=http://<user>:<password>@192.168.233.8:30889
export https_proxy=$http_proxy HTTP_PROXY=$http_proxy HTTPS_PROXY=$http_proxy
```

- **Keep the password to yourself.** Set it in your own shell profile or task environment, never in images, Dockerfiles, shared task templates, notebooks or repositories: whoever has it uses the proxy as you. If it leaks, ask an admin to rotate it.
- **Not encryption.** The password goes to the proxy in clear text (HTTP basic authentication, SOCKS5 username and password) over the private and campus networks. An account decides who may use the entry and whose usage it is; it does not protect the traffic. HTTPS stays encrypted end to end as usual.

### Docker daemon with an account

The Docker daemon of a node uses the account `svc-docker-<node>`, for example `svc-docker-cvgl-node01`. Keep its URL in a file only root can read, not in the drop-in itself: every user of the node can see a drop-in's `Environment=` lines with `systemctl show docker`.

```sh
sudo install -D -m 600 /dev/null /etc/systemd/system/docker.service.d/proxy.env
sudoedit /etc/systemd/system/docker.service.d/proxy.env
```

```conf
HTTP_PROXY=http://svc-docker-cvgl-node01:<password>@192.168.233.8:30889
HTTPS_PROXY=http://svc-docker-cvgl-node01:<password>@192.168.233.8:30889
NO_PROXY=localhost,127.0.0.1,nvcr.io,aliyuncs.com,cvgl.lab,harbor.cvgl.lab,10.0.1.68,192.168.233.8
```

and replace the contents of `/etc/systemd/system/docker.service.d/proxy.conf`, including any `Environment=` proxy lines, with:

```conf
[Service]
EnvironmentFile=/etc/systemd/system/docker.service.d/proxy.env
```

Then reload and restart Docker on the drained node as in steps 3 and 4 of [Docker daemon](#docker-daemon).

### Usage

The **Proxy Usage** dashboard in Grafana shows the traffic per user, per service account and per line, to everyone who can open Grafana.

- **Users** are counted where they enter the proxies: at the HA entry (`ha-general` and `ha-all` share one counter per user, `proxy_role="entry"`), and at a line's own entry when they use it directly and it requires accounts. The HA entry reaches the lines as `svc-ha` where they require accounts, so traffic it relays is counted once for the user, at the HA entry, never twice. User figures include traffic to private and Chinese destinations; traffic through a line's own entry without an account is counted under no user. There is no per-user breakdown by line.
- **Service accounts** are shown apart from users.
- **Lines** show each line's traffic to its upstream server, whichever entry it came through. Traffic that goes out directly (private and Chinese destinations) is not included.
- **HA entry throughput** is all traffic of the entry's ports, by entry: `ha-general` and `ha-all`.

The totals are Prometheus `increase()` over the selected range, extrapolated to the range edges, so a total built from only a few samples (a short range, or a counter that lived briefly between restarts) reads high. Xray's counters restart from zero when Xray restarts, as after every account change, and an account's counter appears only at its first use after that. Not counted: traffic after the last scrape before a restart, and an account's traffic up to its first scrape when the range holds no earlier sample of it (a new account, or a range that starts after a restart). Prometheus keeps 30 days of samples (`--storage.tsdb.retention.time` in `docker-compose.yml`), which bounds the range.

### Accounts on the lines

By default the public lines' own entries (the *raw* entries, hence `raw-auth`) require no account; the private lines always do. Turn accounts on there only once every client of a line sends one, because a client without one is refused:

1. Move the clients to the HA entry with their accounts, and the Docker daemons to their `svc-docker-<node>` accounts, one drained node at a time. A client that keeps using a line directly can add its account to the line's URL beforehand: a line without accounts ignores it.
2. Turn it on and render:

    ```sh
    python3 xray/scripts/proxy_accounts.py raw-auth on
    python3 xray/scripts/proxy_accounts.py render
    ```

    `render` then requires every account on the HTTP and SOCKS5 entries of every public line, and has the HA entry log in to them as `svc-ha` and the probes as `svc-monitor`, as it always does for the private lines.
3. Restart the lines and `xray-ha` together, and reload the probes:

    ```sh
    docker compose restart xray-ha $(docker compose config --services | grep '^xray-' | grep -v -e exporter -e '^xray-ha$')
    docker compose kill -s SIGHUP blackbox-exporter
    ```

    The HA entry and the lines must agree: the HA entry logging in to a line without accounts, or not logging in to a line with them, fails, and the HA entry fails closed until both sides have restarted.

While accounts are on, restart the lines together with `xray-ha` after every account change. A line made with `create_xray_service.py` requires the accounts from its creation. `raw-auth off` followed by the same steps turns them off.

### Changes from the portal

The proxy portal does not change accounts itself. It writes a request per change into a spool folder, and the applier, a root systemd service on the VM ([`services/proxy-portal/applier`](../services/proxy-portal/applier/README.md)), applies them. The portal never gets the Docker socket or the accounts file. The accounts file stays the only record of the accounts and whether they are enabled.

- **Install:** the units `proxy-apply.service`, `.path` and `.timer` and the settings file `/etc/default/proxy-apply` are in [`services/system-configurations/etc`](../services/system-configurations/etc). Create the portal's user, install the files, set `PROXY_APPLY_SERVICES_DIR`, run `proxy_apply.py init` and enable the `.path` and `.timer` units; the [applier README](../services/proxy-portal/applier/README.md#install) has the commands.
- **Spool and permissions:** `/var/lib/proxy-portal/spool`. The portal's user owns `requests/` and `results/` (`0700`); root owns `status/` and `status.json`. A request is accepted only as a regular file owned by the portal's user, at most 4096 bytes. Write each request under a name not ending in `.json`, then rename it to `<id>.json`; every `*.json` entry is read once and removed, valid or not.
- **Requests:** `{"id": "<id>", "op": "add|rotate|enable|disable|remove|admin-on|admin-off", "username": "<account>"}` in `requests/<id>.json`, nothing else; `<id>` is 8 to 64 lowercase letters, digits and `-` (a UUID fits), new for every request. `svc-ha` and `svc-monitor` are refused; admins change them only with `proxy_accounts.py` ([Manage accounts](#manage-accounts)). `admin-on` for a service account fails. For `add` and `rotate`, the applier generates the password and writes it once to `results/<id>.json` (mode `0400`, owned by the portal's user), which the portal deletes after showing it.
- **Statuses** in `status/<id>.json`: `applying` while its batch runs, then `applied` (live and verified), `failed` (not applied; the previous files and services are restored, `restored: false` if the rollback did not finish: the [applier README](../services/proxy-portal/applier/README.md#statuses) has the recovery), or `unconfirmed` (live and every refused login gets 407, but the logins that must pass got no 204 in time, e.g. while the lines are down).
- **How:** the applier holds the accounts lock and applies the pending requests in batches, a second request for the same account going into the next batch: it renders in memory, tests every changed Xray config with `xray run -test`, replaces the files atomically, restarts the services and verifies through `ha-general` and `ha-all` that `svc-monitor` and every newly accepted login pass (204), and that every disabled or removed account, replaced password, admin turned off, a non-admin on `ha-all` and a wrong password are refused (407). A wrong answer rolls everything back.
- **Restart impact:** every change restarts `xray-ha`, a change to an admin also the private lines, and with raw auth on every line. Established connections through them drop, and new ones fail for a few seconds. Requests that arrive together share one restart; each further request for the same account adds one.

While raw auth is off, turning a user's access off in the portal stops it only at the HA entries (and the private lines); the public lines' own entries stay open to them.

## Proxy availability

The **Proxy Availability** dashboard in Grafana (linked from Flare) shows whether requests get through each entry. Every 30 seconds `blackbox-exporter` fetches `https://www.google.com/generate_204` through both HA entries and through every line's own HTTP and SOCKS5 ports, logging in as `svc-monitor` where the entry requires accounts ([`services/blackbox/blackbox.example.yml`](../services/blackbox/blackbox.example.yml), rendered with the account by `proxy_accounts.py`; the `proxy-probe` job in [`prometheus.yml`](../services/prometheus/prometheus.yml)). A probe passes on a 204 within 8 seconds, without following redirects, with the proxy resolving the name. The lines send `www.google.com` abroad (unlike `www.gstatic.com`, which the `geosite:cn` rule sends out directly), and Google cannot be reached directly from the campus network, so a passing probe shows that the line's way abroad works. A line is always probed through its own entry, never through the HA entry, so a failed line shows as failed while the HA entry carries on.

- **Top rows**, one for `ha-general` and one for `ha-all` (admins only): the entry's HTTP and SOCKS5 status; how many of its lines pass on SOCKS5, the protocol the HA entry uses to reach them, out of how many are configured (the four public lines; all nine); the probe duration; the age of the last result; and the probe success rate and coverage over 24 hours.
- **Lines:** one row per line, with its access (Public or Admins only), its pool, its HTTP and SOCKS5 status, durations, last result, 24-hour probe success rates and coverage, and the status and traffic from its exporter. The exporter only shows that Xray runs, not that requests get through.
- **History:** probe results over time (a gap is a time without results), durations, the hourly probe success rate, the results per 5 minutes (10 expected; fewer means the exporter or Prometheus did not run) and the HA entry's traffic through each line, from its own counters (with Xray's small probe fetches through every line).

A status is **Passing**, **Failing** or **Unknown**. Unknown means no result in the last 90 seconds; it is never the last known status. The probe success rate counts only the probes that ran, so read it together with the coverage. Reading the statuses together:

| HA entry | Pool lines | Meaning |
| :--- | :--- | :--- |
| Passing | 2 or more passing | Working, with a spare line |
| Passing | 1 passing | Working, without a spare |
| Failing | some passing | Look at `xray-ha` and its ports first |
| Failing | none passing | Nothing gets out from the VM to the probe URL: all lines are down, or the probe URL is |

The probes run on the VM. They do not cover the way from a node or the login node to the VM's ports; check that with `curl` as above. Pool lines that pass are not necessarily independent: several lines can share an upstream provider.

A new line needs its two modules in `blackbox.example.yml` and its two targets in the `proxy-probe` job, with the `pool` and `access` labels of its class ([Classify a new line](#classify-a-new-line)), by hand; `create_xray_service.py` adds only its exporter. The modules take effect after `proxy_accounts.py render` and `docker compose kill -s SIGHUP blackbox-exporter`.

## Bootstrap: a temporary proxy on one machine

Before the supplementary services VM exists (first-time setup, or a rebuild), run Xray by hand on the machine that needs the proxy, and stop it once the proxies on the VM are up:

1) Download and extract the latest `Xray-linux-64.zip` from the [Xray-core releases](https://github.com/XTLS/Xray-core/releases).

2) Create a client configuration `config.json` from a share link with [`vless_to_config.py`](../services/xray/README.md#tools) (for `vless://` links; for other links take a configuration from [XTLS/Xray-examples](https://github.com/XTLS/Xray-examples)). Its default output opens a SOCKS5 proxy on port `1089` and an HTTP proxy on port `8889`.

3) Run it:

    ```sh
    ./xray run -c ./config.json
    ```

   The machine then uses `http://127.0.0.1:8889` (HTTP) or `127.0.0.1:1089` (SOCKS5) wherever this chapter says `192.168.233.8:59889` or `:59880`.

## Verify a proxy

```bash
export https_proxy=http://192.168.233.8:59889
curl https://google.com.hk
```

If the output is an HTTP response like:

```html
<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">
<TITLE>301 Moved</TITLE></HEAD><BODY>
<H1>301 Moved</H1>
The document has moved
<A HREF="https://www.google.com.hk/">here</A>.
</BODY></HTML>
```

the proxy works.

## Use the proxy

The examples below use a line's own entry, which needs no account while the lines do not require accounts. Through the HA entry, use the URL with your account from [Client settings](#client-settings) instead.

### Docker daemon

Docker pulls images through the proxy set in a systemd drop-in. Through the HA entry, follow [Docker daemon with an account](#docker-daemon-with-an-account). Through a line's own entry, follow the steps below (the [reference file](../services/system-configurations/etc/systemd/system/docker.service.d/proxy.conf) has the values used on the supplementary services VM).

1) Create the folder:

    ```sh
    sudo mkdir -p /etc/systemd/system/docker.service.d
    ```

2) Write `/etc/systemd/system/docker.service.d/proxy.conf`:

    ```conf
    [Service]
    Environment="HTTP_PROXY=http://192.168.233.8:59889"
    Environment="HTTPS_PROXY=http://192.168.233.8:59889"
    Environment="NO_PROXY=localhost,127.0.0.1,nvcr.io,aliyuncs.com,cvgl.lab,harbor.cvgl.lab,10.0.1.68,192.168.233.8"
    ```

    `NO_PROXY` keeps the cluster's own registry (`harbor.cvgl.lab`) and the mirrors that work directly out of the proxy. Note that `http` is intentionally used in `HTTPS_PROXY`: this is how most HTTP proxies work.

3) Reload and restart Docker:

    ```sh
    sudo systemctl daemon-reload
    sudo systemctl restart docker
    ```

    Restarting Docker stops the running containers (including Determined tasks), so drain the node first.

4) Check that `docker info` shows the proxy.

### Environment variables

Most programs, including those written in Python or Go, honour these variables:

```bash
export http_proxy=http://192.168.233.8:59889
export https_proxy=http://192.168.233.8:59889
export HTTP_PROXY=$http_proxy HTTPS_PROXY=$https_proxy
export no_proxy=localhost,127.0.0.1,cvgl.lab,harbor.cvgl.lab,192.168.233.0/24,10.0.1.64/27
curl google.com
```

### Proxychains

[Proxychains](https://github.com/rofl0r/proxychains-ng) redirects the connections of dynamically linked programs that ignore the environment variables through a SOCKS5 or HTTP proxy:

1. Install it:

    ```bash
    sudo apt install proxychains4
    ```

2. In `/etc/proxychains4.conf`, change the last line into (on the login node, which reaches the VM through the campus network):

    ```text
    socks5 10.0.1.68 59880
    ```

3. Check it:

    ```text
    cvgladmin@cvgl-loginnode:~$ proxychains curl google.com
    [proxychains] config file found: /etc/proxychains4.conf
    [proxychains] preloading /usr/lib/x86_64-linux-gnu/libproxychains.so.4
    [proxychains] DLL init: proxychains-ng 4.14
    [proxychains] Strict chain  ...  10.0.1.68:59880  ...  google.com:80  ...  OK
    <HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">
    <TITLE>301 Moved</TITLE></HEAD><BODY>
    <H1>301 Moved</H1>
    ...
    ```

### pip and git

- `pip`: add `--proxy http://192.168.233.8:59889` for packages from GitHub (e.g. the [Determined wheel](01_First-time_Setup_of_Cluster_Nodes.md#install-determined-ai-systemwide)); PyPI itself works without it, and so does the cluster's [PyPI cache](04_Setup_Supplementary_Services.md#pypi-cache), pip's default index in Determined tasks.
- `git`: fetch GitHub over HTTPS through the proxy, for example on the supplementary services VM:

    ```sh
    git -c http.proxy=http://192.168.233.8:59889 fetch https://github.com/WU-CVGL/cluster-setup.git '+refs/heads/*:refs/remotes/origin/*'
    ```

    This updates `origin/*` like a plain `git fetch` would (the checkout there has an SSH `origin` URL, which does not use `http.proxy`). To use the proxy for every HTTPS fetch from GitHub of a user: `git config --global http.https://github.com/.proxy http://192.168.233.8:59889`.
