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
      - [1. Gitea](#1-gitea)
      - [2. Harbor](#2-harbor)
      - [3. Xray](#3-xray)
      - [4. Grafana and Prometheus](#4-grafana-and-prometheus)
      - [5. System-configurations](#5-system-configurations)
      - [6. All-in-one services (except Harbor and node-exporter)](#6-all-in-one-services-except-harbor-and-node-exporter)
      - [7. Set up endpoints for Node-exporter and other monitoring services](#7-set-up-endpoints-for-node-exporter-and-other-monitoring-services)
        - [7.1. Introduction](#71-introduction)
        - [7.2. Run](#72-run)
        - [7.3. Prometheus authentication for Determined AI (Bearer token)](#73-prometheus-authentication-for-determined-ai-bearer-token)
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
- frp

## HOW-TO

### Requirements

Install the [Compose plugin](https://docs.docker.com/compose/install/linux/#install-using-the-repository)
to enable [GPU support](https://docs.docker.com/compose/gpu-support/) instead of using the older version of `docker-compose` in Ubuntu (20.04).

```bash
sudo apt install docker-compose-plugin
```

### First-time configurations

#### 1. Gitea

Check the notes [to configure env variables for Gitea](gitea/README.md).

#### 2. Harbor

Check the [notes to install Harbor](../docs/04_Setup_Supplementary_Services.md#harbor).

P.S. The Harbor service is not in the all-in-one file, thus needs to be launched separately.

#### 3. Xray

Check the [note to add the configuration files](xray/README.md)

#### 4. Grafana and Prometheus

Fix the ACL permissions:

```bash
sudo chown -R 472:0 grafana/*

sudo chown -R 1000:1000 prometheus/*
```

#### 5. System-configurations

Contains some [key configurations](system-configurations/etc) in `/etc`

#### 6. All-in-one services (except Harbor and node-exporter)

To launch the all-in-one services, simply run the command on the management node:

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

To force recreate all services:

```bash
docker compose up -d --force-recreate --remove-orphans
```

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

On every node that needs to be monitored:

Copy [`docker-compose.yaml` in `node-exporter`](./node-exporter/docker-compose.yaml) to every node, then run

```bash
# Using `docker compose` instead of `docker-compose`
docker compose up -d --force-recreate --remove-orphans
```

to collect data from every machine.

Update `static_configs[targets]` in `prometheus/config/prometheus.yml` if any new nodes are added to the cluster.

##### 7.3. Prometheus authentication for Determined AI (Bearer token)

Scraping Determined-AI-master's metrics (`/prom/det-state-metrics`) with Determined-AI API needs a `bearer_token`. You can get this token by:

```bash
curl -s "http://10.0.1.66:8080/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  --data-binary '{"username":"admin","password":"********"}'
```

Then you can use this token in `prometheus.yaml`.

Reference: 
> [Determined AI Docs - Configure Determined with Prometheus and Grafana](https://docs.determined.ai/latest/integrations/prometheus/prometheus.html)
>
> [Determined AI Docs - REST API - Authentication](https://docs.determined.ai/latest/reference/rest-api.html?highlight=api%20login#authentication)

## Notes

Although Determined-AI's [det-state-metrics](https://gpu.cvgl.lab/prom/det-state-metrics) (to view it in your browser you need to log in to https://gpu.cvgl.lab first) provides enough information about tasks and containers, the [official document](https://docs.determined.ai/latest/integrations/prometheus/prometheus.html) and [repo](https://github.com/determined-ai/works-with-determined) did not provide a Grafana dashboard that integrates these data with `cAdvisor` and `dcgm-exporter` to provide usage statistics by individual users or tasks. Further development is required for more precise cluster management.

For example, in `https://gpu.cvgl.lab/prom/det-state-metrics`, each job will have an `allocation_id`. With this `allocation_id`, you can get the corresponding `container_id` in `det_container_id_allocation_id`.

With this `container_id`, you can:

- Get `container_runtime_id` in `det_container_id_runtime_container_id`
- Get `gpu_uuid` in `det_gpu_uuid_container_id`

With `container_runtime_id`, you can get container stats of this job with `cAdvisor`;`

With `gpu_uuid`, you can get GPU stats of this job with `dcgm-exporter`.

TODOs:

- A Grafana dashboard that integrates and visualizes these data
- A management watchdog that utilizes these data and kills tasks

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

https://github.com/fatedier/frp

https://github.com/snowdreamtech/frp
