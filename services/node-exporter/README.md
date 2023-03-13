# Node Exporter and other monitoring services

## Introduction

This `docker-compose.yaml` starts monitoring tools similar to the [Determined AI Docs - Configure Determined with Prometheus and Grafana](https://docs.determined.ai/latest/integrations/prometheus/prometheus.html), except that in [configure cAdvisor and dcgm-exporter](https://docs.determined.ai/latest/integrations/prometheus/prometheus.html#configure-cadvisor-and-dcgm-exporter), the official document uses `provider: startup_script: |` that only works with GCP and Azure provider, while we use our own on-premise cluster. Instead of using that start-up script, we need to manually launch this `docker-compose.yaml` on each agent node. (Maybe we can use Ansible in the future)

Monitoring tools:

- [node-exporter](https://github.com/prometheus/node_exporter)
- [cAdvisor](https://github.com/google/cadvisor)
- [dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter)

These tools will run on the cluster agents to be monitored.

## Requirements

Install the [Compose plugin](https://docs.docker.com/compose/install/linux/#install-using-the-repository)
to enable [GPU support](https://docs.docker.com/compose/gpu-support/) instead of using the older version of `docker-compose` in Ubuntu (20.04).

```bash
sudo apt install docker-compose-plugin
```

## Run

```bash
# Using `docker compose` instead of `docker-compose`
docker compose up -d --force-recreate --remove-orphans
```

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
