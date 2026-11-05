# s8030 exporters

s8030 is behind a separate NAT gateway. Its node-exporter, cAdvisor and DCGM-Exporter run from
[`../node-exporter/`](../node-exporter/docker-compose.yaml). The supplementary services VM scrapes
them through this proxy on core, which reaches the node over WireGuard:

| Exporter | Prometheus target | WireGuard destination |
| --- | --- | --- |
| node-exporter | `10.0.1.66:19100` | `172.30.233.2:9100` |
| cAdvisor | `10.0.1.66:19080` | `172.30.233.2:9080` |
| DCGM-Exporter | `10.0.1.66:19400` | `172.30.233.2:9400` |

Only the supplementary services VM (`10.0.1.68`) can read `GET /metrics`. Other sources are
denied, and no task or management port is proxied. Prometheus keeps `node="s8030"` and the
exporter's original port in its `instance` label, so existing dashboard variables discover it.

Run this compose project on core after the WireGuard peer and node exporters are available:

```sh
docker compose run --rm --no-deps exporters-proxy nginx -t
docker compose up -d
```

Validate all three targets with `promtool check config` before reloading Prometheus. From the
supplementary services VM, check the proxy's `/metrics` endpoints and confirm all targets are up.
The GPU-missing alert expects five GPUs on s8030; exporter availability alone does not confirm
that every GPU is visible.
