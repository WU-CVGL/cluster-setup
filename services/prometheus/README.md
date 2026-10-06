# Determined task resources

Prometheus joins the hardware samples of the node exporters to Determined's task and allocation
identities. Two consumers read the result: the Determined master's native **Resources** pages and
REST API (our fork, [WU-CVGL/determined](https://github.com/WU-CVGL/determined)), and the Grafana
dashboard `det-task-resources`. Training code needs no change.

- [How the pieces connect](#how-the-pieces-connect)
- [Configuration contract](#configuration-contract)
- [Determined scrape token](#determined-scrape-token)
- [Connecting the Determined master](#connecting-the-determined-master)
- [Task dashboard and permissions](#task-dashboard-and-permissions)
- [Checking the chain end to end](#checking-the-chain-end-to-end)
- [Troubleshooting](#troubleshooting)
- [Checking the configuration](#checking-the-configuration)
- [TSDB](#tsdb)

## How the pieces connect

```text
 GPU nodes                                   Determined master
 cAdvisor :9080      DCGM-Exporter :9400     /prom/det-state-metrics (bearer token)
      |                     |                     |
      | job cadvisor        | job dcgm            | job det-master
      v                     v                     v
 Prometheus (prometheus.yml): adds det_cluster, node, container_runtime_id, gpu_uuid
      |
      v
 recording rules (rules/determined-task-resources.yml): det:*_task:info and diagnostics
      |                                              |
      | GET /api/v1/query_range                      | Grafana datasource "Prometheus"
      v                                              v
 Determined master: integrations.task_resources     Grafana: det-task-resources dashboard
 WebUI Resources pages,
 GET /api/v1/tasks/{task_id}/resources
```

1. **Exporters.** On every GPU node, [`../node-exporter/`](../node-exporter/docker-compose.yaml)
   runs cAdvisor (CPU and memory per container, keyed by its cgroup path `id`) and DCGM-Exporter
   (values per GPU, keyed by `UUID`); see [7.2](../README.md#72-run) for running them.
2. **Master state.** `/prom/det-state-metrics` (served while `observability.enable_prometheus` is
   true, the default) has one gauge per relation: `det_allocation_id_task_id_task_actor`
   (allocation to task), `det_container_id_allocation_id` (container to allocation),
   `det_container_id_runtime_container_id` (container to Docker container ID) and
   `det_gpu_uuid_container_id` (GPU UUID to container). A relation exists only while the
   allocation holds its resources, and the gauges live in the master's memory.
3. **Scrape jobs.** [`prometheus.yml`](prometheus.yml) scrapes the three sources as the jobs
   `cadvisor`, `dcgm` and `det-master` (and node-exporter as `node`), adds the target labels
   `det_cluster` and `node`, and derives `container_runtime_id` (cAdvisor) and `gpu_uuid` (DCGM).
4. **Recording rules.** [`rules/determined-task-resources.yml`](rules/determined-task-resources.yml)
   normalizes each positive relation to one, drops ambiguous relations (two owners for one
   allocation, container or GPU) and joins the rest into `det:allocation_task:info`,
   `det:runtime_task:info` (Docker container ID to task and allocation) and `det:gpu_task:info`
   (GPU UUID to task and allocation). Every mapping is gated by `det:master_up:info`: while the
   `det-master` scrape fails, no task is attributed. Conflicts and missing links are recorded as
   `det:*_conflict:count` and `det:*_without_*:info`.
5. **Consumers.** The master runs a fixed set of queries for one task, after checking the user's
   permission on that task: the three `det:*_task:info` rules joined with the raw cAdvisor and DCGM
   series. The Grafana dashboard runs the same joins and also shows the diagnostics. The
   [GPU health alerts](../README.md#74-gpu-health-alerts-grafana) and the watchdog's
   `IdleKillAlert` read raw metrics, not these rules.

## Configuration contract

The fork's master hard-codes these names in its queries (the dashboard UID in its Grafana link),
and the rules and the dashboard use them too. Renaming one of them breaks the chain without an
error at start: the Resources pages show no samples, or the Grafana link finds no dashboard.

| What | Name | Set by |
| :--- | :--- | :--- |
| Cluster | `det_cluster` on every sample of `node`, `cadvisor`, `dcgm` and `det-master` (`cvgl`). Must equal `det_cluster` in the master's `integrations.task_resources`. Use another value for a second master or a test deployment | target relabel in `prometheus.yml` |
| Node | `node`: the target host without its port; `cvglloginnode.lan` becomes `login.cvgl.lab` | target relabel |
| Jobs | `cadvisor` and `dcgm` (master queries), `det-master` (rules) | `job_name` |
| Docker container | `container_runtime_id` on cAdvisor series: the 64-character Docker ID from an `id` of `/docker/<id>` or `docker-<id>.scope`. Containers with other cgroup paths are not attributed | metric relabel of `cadvisor` |
| GPU | `gpu_uuid` on DCGM series, copied from `UUID`. DCGM's own `gpu` (the host's GPU index), `pci_bus_id` and `modelName` stay; from fork 0.41.0 on, the WebUI shows them on hover over a GPU legend entry | metric relabel of `dcgm` |
| cAdvisor metrics | `container_cpu_usage_seconds_total` (`cpu` is `total` or empty), `container_memory_working_set_bytes`, `container_memory_rss` | cAdvisor |
| DCGM metrics | `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_FB_USED`, `DCGM_FI_DEV_POWER_USAGE`, `DCGM_FI_DEV_GPU_TEMP` | [`default-counters.csv`](../node-exporter/default-counters.csv) |
| Task mappings | `det:allocation_task:info`, `det:runtime_task:info` (with `container_runtime_id`), `det:gpu_task:info` (with `gpu_uuid`), each with `det_cluster`, `task_id` and `allocation_id` | recording rules |
| Dashboard | UID `det-task-resources` (the master's optional Grafana link requires it) | dashboard JSON |

Targets are static: cAdvisor on `9080`, DCGM-Exporter on `9400`, node-exporter on `9100`. A new
node needs its targets in all three jobs; a new resource pool needs none. The four jobs use a
15-second interval and a 5-second timeout, and the rules are evaluated every 15 seconds. The
master's queries and the dashboard leave out exporter series that are duplicated for one
container or GPU, and GPU values outside their physical range: they show as gaps, not sums.

## Determined scrape token

`/prom/det-state-metrics` needs a token of an active Determined user; no admin role is needed.
The `det-master` job sends a bearer token read with
`authorization.credentials_file: /run/determined-metrics/token`.

- The file is `token` in `DET_METRICS_SECRETS_DIR` from `services/.env` (default
  `prometheus/secrets/`, gitignored; see [6.1](../README.md#61-secrets-and-env-files)). The
  directory is owned by uid 1000 (Prometheus and the watchdog) with mode `0700`, the file has mode
  `0600`. Create the directory before the first `docker compose up`, from `services/`:

  ```sh
  d=$(sed -n 's/^DET_METRICS_SECRETS_DIR=//p' .env); sudo install -d -m 0700 -o 1000 -g 1000 "${d:-prometheus/secrets}"
  ```

- Prometheus mounts the directory read-only at `/run/determined-metrics`, the watchdog read-write.
  The directory is mounted, not the file: a single-file bind mount would keep the old inode after
  an atomic replacement. `DETERMINED_METRICS_TOKEN_FILE` changes the watchdog's path; change the
  Prometheus `credentials_file` and mounts with it.
- The [watchdog](../determined-watchdog/README.md#determined-token-shared-with-prometheus) writes
  the token and renews it before it expires or after Determined rejects it. Prometheus re-reads
  the file on every scrape, so a new token needs no reload. Until the first token is written, the
  `det-master` target is down.
- A token placed by hand must be written the same way (owner uid 1000, mode `0600`, replaced
  atomically); the watchdog keeps it while its expiry can be decoded and is more than 48 hours
  away. Never put a token in `prometheus.yml` or any other tracked file.
- A token that reached a tracked file stays in the public git history (older versions of the
  watchdog wrote it into the last line of `prometheus.yml`): revoke it. Determined's logout ends
  the session of the token it receives; with the token in `t`,
  `curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $t" <master URL>/api/v1/auth/logout`
  prints `200` (ended) or `401` (already expired or revoked); anything else did not reach the
  endpoint.

## Connecting the Determined master

The master reads Prometheus server-side, with this block in its configuration (the cluster's is in
the [reference `master.yaml`](../system-configurations/etc/determined/master.yaml)):

```yaml
integrations:
  task_resources:
    prometheus_url: http://<prometheus host>:<port>
    det_cluster: <det_cluster label of prometheus.yml>
```

- `prometheus_url` is an HTTP(S) origin: scheme, host and port, nothing else. A path (also a
  reverse-proxy prefix such as `/prometheus/`), credentials, a query or a fragment are refused. The
  URL is never sent to the browser.
- `det_cluster` is the value of the `det_cluster` target label in `prometheus.yml`. Every query
  selects it, so another value gives empty charts, not an error.
- Set both keys or neither. The master checks the block at start and does not start when only one
  key is set or the URL is not a bare origin (the error names `task_resources`). It reads the block
  only at start: restart the master after a change.
- **Network path.** The master itself sends `GET <prometheus_url>/api/v1/query_range` (with
  `query`, `start`, `end` and `step`) and nothing else: no `Authorization` header, no HTTP proxy
  from the environment, no redirects followed, 10 seconds for all queries of one resource request.
  Prometheus must answer it from the master directly. In `docker-compose.yml` Prometheus is only
  on the `grafana_monitor` network and publishes no port, so whatever makes it reachable from the
  master (a published port, or a proxy serving it at the root path) must pass that request
  without authentication. Prometheus runs with `--web.enable-admin-api` and
  `--web.enable-lifecycle`, so anyone who reaches its port can also delete series or stop it: let
  only the master's address, or only `GET /api/v1/query_range`, through. Grafana's datasource
  proxy cannot stand in: its URL has a path.
- Keep `observability.enable_prometheus: true` (the default): it serves `/prom/det-state-metrics`
  for the `det-master` job.

With the block set, the WebUI shows **View Resources** in the action menus of tasks and
experiments and in the job queue, a **Resources** tab on trials, and a **Resources** link on task
logs; from fork 0.41.0 on, generic tasks also have the menu entry and the tab. The same data is in
the REST API: `GET /api/v1/task-resources/capability` (`{"enabled": true}`) and
`GET /api/v1/tasks/{task_id}/resources?start=<unix s>&end=<unix s>&step=<s>`, optionally with
`allocationId=<allocation>`. Both need a Determined login, and a task's series need permission to
read that task; an unknown task and a task the user may not read both get 404. The master's limits
per request (range, points, step, timeout, concurrent requests) and the response format are in the
fork's [native task resources guide](https://github.com/WU-CVGL/determined/blob/main/docs/integrations/observability/native-task-resources.rst).

`integrations.grafana_task_resources` (`dashboard_url` with `/d/det-task-resources/` and at most an
`orgId` query, and `det_cluster`) instead adds an external **Task Resources** link to the Grafana
dashboard. The WebUI shows it only while `task_resources` is not set; see the fork's
[Grafana link guide](https://github.com/WU-CVGL/determined/blob/main/docs/integrations/observability/grafana-task-resources.rst).

## Task dashboard and permissions

Grafana provisions the dashboard from
[`grafana/provisioning/dashboards/json/determined-task-resources.json`](../grafana/provisioning/dashboards/json/determined-task-resources.json).
Its link contract (the fork's Grafana link builds it):

```text
/d/det-task-resources/task-resources?var-cluster=<det_cluster>&var-task_id=<task>&var-allocation_id=<allocation or %24__all>&from=<start-ms>&to=<end-ms or now>
```

`var-allocation_id` is the selected allocation, or `$__all` (URL-encoded `%24__all`) when none is
selected; `to` is the task's end, or `now` while it runs.

Task identity is primary; generic tasks need no experiment mapping. Select the time range before
choosing completed tasks or historical allocations. CPU is measured in logical cores, memory in
bytes, and GPU panels show the **assigned device**, not exclusive per-process use: GPU sharing,
MPS and processes outside the container are included. Allocations stay distinct across pause and
resume, and a parent task excludes its child tasks. cAdvisor releases older than the pinned one
(such as v0.38) report an RSS of zero on cgroup v2 hosts; the master warns when a task's RSS is
zero throughout the range.

Missing mappings and unavailable device metrics are gaps, never zeros, and recording rules cannot
backfill periods when the association was unavailable. The conflict and missing-link panels
explain gaps cluster-wide.

Fork 0.41.0 and later export a task's mappings (`det_allocation_id_task_id_task_actor`,
`det_container_id_allocation_id`, `det_container_id_runtime_container_id`,
`det_gpu_uuid_container_id`) only after its allocation has run for
`observability.task_mapping_delay` (master config, default `5m`, counted from the allocation's
first Pulling or Running; `0s` exports from the start), and delete them when it stops. An
allocation that ends sooner is never attributed, and the first minutes of a longer one are never
attributed either: its charts start that late, and nothing is backfilled. cAdvisor still scrapes
and stores every container, short tasks included.

Grafana lets anonymous users in as Viewers ([`custom.ini`](../grafana/custom.ini)), so everyone
who reaches Grafana can read every task's dashboard and query the datasource. Dashboard variables
are filters, **not authorization**. Per-task access control is the master's: its Resources pages
and API check the user's permission on the task. These recording rules do not feed watchdog
termination decisions.

## Checking the chain end to end

Run the Prometheus queries on the supplementary services VM, in `services/`, with the `promtool`
of the Prometheus container (Prometheus publishes no port):

```sh
q() { docker compose exec -T prometheus promtool query instant http://localhost:9090 "$1"; }
```

1. **Targets up.** `q 'up{job=~"cadvisor|dcgm|det-master"} == 0'` prints nothing. Errors of
   failing targets:
   `docker compose exec -T prometheus wget -qO- 'http://localhost:9090/api/v1/targets?state=active' | grep -o '"lastError":"[^"]\+"'`.
2. **Labels.** `q 'count by (det_cluster, node) (container_memory_working_set_bytes{job="cadvisor",container_runtime_id!=""})'`
   and `q 'count by (det_cluster, node) (DCGM_FI_DEV_GPU_UTIL{job="dcgm",gpu_uuid!=""})'` list
   every GPU node with the expected `det_cluster`; the DCGM count is the node's GPU count.
3. **Mappings for a running task** (its ID from `det task list` or the WebUI):
   `q 'det:allocation_task:info{task_id="<task id>"}'`, `q 'det:runtime_task:info{task_id="<task id>"}'`
   (one per container) and, for a task with GPUs, `q 'det:gpu_task:info{task_id="<task id>"}'`
   (one per GPU). If the first is empty, check `q 'det:master_up:info'`; otherwise look for the
   task's containers and GPUs in
   `q '{__name__=~"det:.*_conflict:count|det:.*_without_.*:info"}'`.
4. **Master to Prometheus.** From the master's host,
   `curl -s --noproxy '*' -o /dev/null -w '%{http_code}\n' "<prometheus_url>/api/v1/query_range?query=up&start=$(($(date +%s)-60))&end=$(date +%s)&step=15"`
   prints `200`. `--noproxy '*'` makes curl ignore `http_proxy`, as the master does.
5. **Master API.** With a CLI logged in to the master (`det user login`), as a user who may read
   the task:

   ```sh
   det dev curl /api/v1/task-resources/capability        # "enabled": true
   END=$(date +%s)
   det dev curl "/api/v1/tasks/<task id>/resources?start=$((END-600))&end=$END&step=30"
   ```

   The second answer has series for `allocation_active`, `cpu_cores` and the memory metrics, and
   the `gpu_*` metrics for a task with GPUs.
6. **GPU legend** (fork 0.41.0 and later). On **View Resources** of a trial, notebook or shell
   with GPUs, the legend reads `GPU 0`, `GPU 1`, ... (the numbering of `nvidia-smi` inside the
   container). Hovering shows the host GPU index and UUID, which match
   `nvidia-smi --query-gpu=index,uuid --format=csv` on that node. Commands, TensorBoards and
   generic tasks record no GPU list, so their legend shows the start of the GPU UUID. Earlier
   releases label each GPU series `<allocation> · <node> · <GPU UUID>`; compare that UUID instead.

## Troubleshooting

| Symptom | Likely link |
| :--- | :--- |
| No **View Resources**; the page says "Native resource monitoring is not enabled for this cluster." | `integrations.task_resources` missing, or the master not restarted after adding it (the capability answers `"enabled": false`) |
| The master does not start; the error names `task_resources` | only one key set, or `prometheus_url` is not a bare origin |
| "Resource metrics could not be loaded. Please retry." (WebUI: HTTP 502; REST API: HTTP 503 with "task resource metrics are unavailable") | the master gets no `200` from `query_range` within 10 seconds: Prometheus down or unreachable from the master, a redirect, or authentication in front of it (check 4). "... are invalid" instead: the rules return series without the task's `task_id` or `det_cluster` |
| "Resource monitoring is busy. Please retry shortly." (WebUI and REST API: HTTP 503 with "task resource query capacity exhausted") | the master's concurrent resource requests are all in use; retry |
| "This task or its resource monitoring is unavailable." (HTTP 404) | unknown task, no permission to read it, or an allocation of another task |
| "No attributed samples in this time range." on every chart of every task | `det_cluster` differs between the master and `prometheus.yml`; the `det-master` target is down (token, see below), which stops all mappings; or the rules are not loaded (checks 1 and 3) |
| CPU and memory empty, GPU charts filled | cAdvisor target down on that node, or a cgroup path the `container_runtime_id` relabels do not match (`det:runtime_without_cadvisor:info`); or two cAdvisor series for one container (`det:cadvisor_cpu_conflict:count`, `det:cadvisor_memory_conflict:count`) |
| GPU charts empty, CPU filled | DCGM target down or `gpu_uuid` missing (`det:gpu_without_dcgm:info`), or two exporters on one GPU (`det:dcgm_gpu_conflict:count`) |
| One GPU chart empty or with gaps, the other GPU charts filled | values of that DCGM field outside their physical range. Only GPU utilization has a diagnostic (`det:dcgm_gpu_invalid:info`); for memory, power and temperature, query the raw `DCGM_FI_DEV_*` series of that GPU |
| Gaps in one task while others are complete | a conflict on its allocation, container or GPU (`det:*_conflict:count`) |
| Gaps in all tasks over the same period | the `det-master` scrape failed then (master restart or outage, token), which stops all mappings, or Prometheus itself was down |
| Warning that RSS is zero throughout the range | an older cAdvisor on that node (cgroup v2); compare its image with [`docker-compose.yaml`](../node-exporter/docker-compose.yaml) |
| Legend shows `GPU 1a2b3c4d` on a trial, notebook or shell (fork 0.41.0 and later) | the GPU lists its containers recorded at start do not add up to the allocation's slots (for example, `nvidia-smi` missed a GPU then) |
| `det-master` target down with "unable to read authorization credentials" or HTTP 401 | token file missing or not readable by uid 1000, or the session revoked: see [the watchdog](../determined-watchdog/README.md#determined-token-shared-with-prometheus) and [7.3](../README.md#73-prometheus-authentication-for-determined-ai-bearer-token) |

## Checking the configuration

Check a changed `prometheus.yml` with the service's own mounts and user before Prometheus loads it
(the token must exist; the image's default user cannot enter the token directory), from
`services/`:

```sh
docker compose run --rm --no-deps --user 1000:1000 --entrypoint promtool prometheus check config /etc/prometheus/prometheus.yml
```

Check the rules and run their fixtures (`promtool` of the same Prometheus version):

```sh
cd services/prometheus
promtool check rules rules/determined-task-resources.yml
cd tests && promtool test rules determined-task-resources.test.yml
```

Fixtures stay outside `rules/`, so the production wildcard `/etc/prometheus/rules/*.yml` never loads
them. The dashboard and the token writer have their own tests:

```sh
python3 -m unittest discover -s services/grafana/tests
cd services/determined-watchdog/build && python3 -m unittest test_metrics_token.py
```

Prometheus reads `prometheus.yml` and `rules/` again on `docker compose kill -s SIGHUP prometheus`
(or when recreated).

## TSDB

The TSDB is in `PROMETHEUS_TSDB_DIR` from `services/.env`, on a local disk, never on NFS (see
[6.1](../README.md#61-secrets-and-env-files)); retention is 30 days. To move it, stop Prometheus,
copy the directory while it is stopped, keep uid 1000 as the owner, point `PROMETHEUS_TSDB_DIR` at
the copy, start Prometheus and check the targets and a historical query. Keep the old directory
unchanged until the copy is checked, and never run two Prometheus processes on one TSDB.

References: [Prometheus configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/),
[rule tests](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/),
[Grafana URL variables](https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/create-dashboard-url-variables/),
fork guides for [native task resources](https://github.com/WU-CVGL/determined/blob/main/docs/integrations/observability/native-task-resources.rst)
and the [Grafana link](https://github.com/WU-CVGL/determined/blob/main/docs/integrations/observability/grafana-task-resources.rst).
