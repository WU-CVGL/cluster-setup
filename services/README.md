# Supplementary Services

These are container-based supplementary services.

- [Supplementary Services](#supplementary-services)
  - [Services](#services)
    - [Core service](#core-service)
    - [Web service](#web-service)
    - [Background services](#background-services)
  - [HOW-TO](#how-to)
    - [First-time configurations](#first-time-configurations)
      - [Gitea](#gitea)
      - [Harbor](#harbor)
      - [Xray](#xray)
      - [Grafana and Prometheus](#grafana-and-prometheus)
      - [System-configurations](#system-configurations)
    - [All-in-one services (except Harbor and node-exporter)](#all-in-one-services-except-harbor-and-node-exporter)
    - [Node-exporter endpoint](#node-exporter-endpoint)
  - [Acknowledgments](#acknowledgments)

## Services

We use NGINX as our reverse proxy, which forwards users' HTTPS requests from their web browsers to our various backend services.

We are currently offering these web services:

### Core service

- Determined AI Master
  - [Notes](determined/README.md)
  - [Master configuration](determined/master.yaml)

### Web service

- Homepage
  - https://cvgl.lab
- Nextcloud
  - https://pan.cvgl.lab
- Determined AI
  - https://gpu.cvgl.lab
- Gitea
  - https://git.cvgl.lab
- Harbor
  - https://harbor.cvgl.lab
- Grafana
  - https://grafana.cvgl.lab

### Background services

- NGINX
- Prometheus
- node-exporter
- cAdvisor
- DCGM-Exporter
- V2Ray Exporter

## HOW-TO

### First-time configurations

#### Gitea

Check the notes [to start Gitea](gitea/README.md).

#### Harbor

Check the [notes to install and start Harbor](harbor/README.md).

P.S. The Harbor service is not in the all-in-one file, thus needs to be launched separately.

#### Xray

Check the [note to add the configuration files](xray/README.md)

#### Grafana and Prometheus

Fix the ACL permissions:

```bash
sudo chown -R 472:0 grafana/*

sudo chown -R 1000:1000 prometheus/*
```

#### System-configurations

Contains some [key configurations](system-configurations/etc) in `/etc`

### All-in-one services (except Harbor and node-exporter)

To launch the all-in-one services, simply run the command on the management node:

```bash
docker compose up -d
```

To rebuild one service, for example, the NGINX reverse proxy, run

```bash
docker compose build nginx
```

To force recreate some services (when changed some configurations), run

```bash
docker compose up -d --force-recreate --remove-orphans [service1 service2 ...]
```

To force recreate all services:

```bash
docker compose up -d --force-recreate --remove-orphans
```

### Node-exporter endpoint

On every node that needs to be monitored:

Copy [`docker-compose.yaml` in `node-exporter`](./node-exporter/docker-compose.yaml) to every node, then run

```bash
docker compose up -d
```

to collect data from every machine.

Update `static_configs[targets]` in `prometheus/config/prometheus.yml` if any new nodes are added to the cluster.

## Acknowledgments

https://github.com/stefan0us/xray-traefik

https://github.com/nginx/nginx

https://github.com/determined-ai/determined

https://github.com/nextcloud/server

https://github.com/go-gitea/gitea

https://github.com/goharbor/harbor

https://github.com/XTLS/Xray-core

https://github.com/grafana/grafana

https://github.com/prometheus/prometheus

https://github.com/prometheus/node_exporter

https://github.com/google/cadvisor

https://github.com/NVIDIA/dcgm-exporter

https://github.com/wi1dcard/v2ray-exporter

https://github.com/soulteary/docker-flare
