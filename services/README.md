# CVGL Docker-based Services

## Services

### NGINX web service

- homepage
  - https://cvgl.lab
- nextcloud
  - https://pan.cvgl.lab
- gpu
  - https://gpu.cvgl.lab
- gitea
  - https://git.cvgl.lab
- harbor
  - https://harbor.cvgl.lab
- portainer
  - https://portainer.cvgl.lab
- grafana
  - https://grafana.cvgl.lab

### monitoring service

- prometheus
- node-exporter
- cAdvisor
- dcgm-exporter
- v2ray-exporter

## HOW-TO

### Most supplementary services

On management node:

```bash
sudo chown -R 472:0 grafana/*

sudo chown -R 1000:1000 prometheus/*

docker-compose up -d
```

### Node-exporter endpoint

On every node:

Copy `docker-compose.yaml` in `node-exporter` to every node, run

```bash
docker-compose up -d
```

to collect data from every machine.

Update `static_configs[targets]` in `prometheus/config/prometheus.yml` if any new nodes are added to the cluster.

## Acknowledgements

https://github.com/stefan0us/xray-traefik

https://github.com/nginx/nginx

https://github.com/determined-ai/determined

https://github.com/nextcloud/server

https://github.com/portainer/portainer

https://github.com/go-gitea/gitea

https://github.com/goharbor/harbor

https://github.com/traefik/traefik

https://github.com/XTLS/Xray-core

https://github.com/grafana/grafana

https://github.com/prometheus/prometheus

https://github.com/prometheus/node_exporter

https://github.com/google/cadvisor

https://github.com/NVIDIA/dcgm-exporter

https://github.com/wi1dcard/v2ray-exporter

https://github.com/soulteary/docker-flare
