# Network Proxy

## Contents

- [Network Proxy](#network-proxy)
  - [Contents](#contents)
  - [Why a proxy](#why-a-proxy)
  - [The proxies on the supplementary services VM](#the-proxies-on-the-supplementary-services-vm)
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

The other services work the same way, on other ports. To list them, on the VM:

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

Add a new proxy from a share link (`vmess://`, `vless://`, `trojan://`, `ss://`) with [`create_xray_service.py`](../services/xray/scripts/README.md): it writes the `config.json`, adds both services to `docker-compose.yml` and the exporter to Prometheus. See the [Xray notes](../services/xray/README.md) for the configuration and [XTLS/Xray-examples](https://github.com/XTLS/Xray-examples) for reference configurations.

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

### Docker daemon

Docker pulls images through the proxy set in a systemd drop-in (the [reference file](../services/system-configurations/etc/systemd/system/docker.service.d/proxy.conf) has the values used on the supplementary services VM):

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

- `pip`: add `--proxy http://192.168.233.8:59889` for packages from GitHub (e.g. the [Determined wheel](01_First-time_Setup_of_Cluster_Nodes.md#install-determined-ai-systemwide)); PyPI itself works without it.
- `git`: fetch GitHub over HTTPS through the proxy, for example on the supplementary services VM:

    ```sh
    git -c http.proxy=http://192.168.233.8:59889 fetch https://github.com/WU-CVGL/cluster-setup.git '+refs/heads/*:refs/remotes/origin/*'
    ```

    This updates `origin/*` like a plain `git fetch` would (the checkout there has an SSH `origin` URL, which does not use `http.proxy`). To use the proxy for every HTTPS fetch from GitHub of a user: `git config --global http.https://github.com/.proxy http://192.168.233.8:59889`.
