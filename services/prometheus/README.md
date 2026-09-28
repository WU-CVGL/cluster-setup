# Determined task resources

This stack uses the existing node exporters and one Prometheus instance. The
`det-task-resources` Grafana dashboard joins hardware samples to Determined task
and allocation identities; training code needs no metrics SDK changes.

## Configuration contract

- `det_cluster=cvgl` is a **target label** on `det-master`, `cadvisor`, `dcgm`, and
  `node`. Use a different value for a second master or test deployment.
- `node` is the target hostname without its exporter port. The login-node aliases
  are normalized to `login.cvgl.lab`; check these against your deployment.
- Static targets are the only discovery source. cAdvisor is exposed on `9080`,
  DCGM on `9400`, node-exporter on `9100`. Adding a logical resource pool does not
  add a physical scrape target.
- cAdvisor recognizes full 64-character Docker IDs in `/docker/<id>` and
  `docker-<id>.scope` cgroup paths. Other paths remain unassociated. DCGM's `UUID`
  becomes `gpu_uuid`. Confirm both with actual exporter samples before rollout.
- Hardware and master state use a 15-second interval and 5-second timeout.
  This is a starting configuration, not a guarantee of observed target health.

## Scrape credential migration

The old tracked bearer token must be revoked and replaced; deleting it from the
current YAML does not remove it from Git history. Do not reuse that token.

From `services/`, provision the private directory for the existing Prometheus and
watchdog UID (both are 1000):

```sh
sudo install -d -m 0700 -o 1000 -g 1000 prometheus/secrets
```

Place a newly issued Determined metrics credential in
`prometheus/secrets/token`, owned by UID 1000 with mode `0600`. Use your normal
secret provisioning mechanism; do not paste credentials into YAML or shell
history. The directory is Git-ignored. Prometheus mounts it read-only at
`/run/determined-metrics`; watchdog mounts the same directory read/write.

Rebuild/recreate **only watchdog and Prometheus** when applying the new mounts.
The updated watchdog validates login success, writes the token atomically, and
does not rewrite Prometheus YAML or restart Prometheus during later refreshes.
The directory mount is necessary: an individual-file bind mount would retain the
old inode after atomic replacement. `DETERMINED_METRICS_TOKEN_FILE` overrides the
watchdog path if you also update the Prometheus credential path/mounts.

Provision the initial token before starting Prometheus, or expect the Determined
target to be down until the watchdog successfully writes it. Token renewal still
uses the deployment's existing watchdog schedule. Monitor target authentication
failures; this change does not create a new independent credential-renewal service.

## Task dashboard and permissions

Grafana provisions the dashboard from
`grafana/provisioning/dashboards/json/determined-task-resources.json`.
Its link contract is:

```text
/d/det-task-resources/task-resources?var-cluster=cvgl&var-task_id=<task>&var-allocation_id=%24__all&from=<start-ms>&to=now
```

The Determined companion change supplies a configurable external Resources link.
Task identity is primary; Generic Tasks do not require an experiment mapping.
Select the time range before choosing completed tasks or historical allocations.
CPU is measured in logical cores, memory in bytes, and GPU panels show **allocated
device** observations, not exclusive per-process attribution. Allocation labels
remain distinct across pause/resume. Parent task usage excludes child tasks.
On the observed cAdvisor targets, RSS currently reports zero for every sample
while working set is nonzero. That does not establish that task RSS is truly
zero; validate the exporter and cgroup support before interpreting the RSS panel.

The mapping rules normalize positive relationship values to one and exclude
ambiguous ownership before joining. Conflict/missing-data panels explain gaps.
Missing mappings and unavailable device metrics are not filled with zero, and
recording rules cannot backfill periods when association was unavailable.
GPU sharing/MPS and out-of-container processes invalidate exclusive attribution.

This first-phase dashboard is for a shared administrative monitoring audience.
Grafana variables are filters, **not task authorization**. Use Grafana's own
access controls. Strict per-task user isolation requires a future Determined
authorized query API and removal of unrestricted data-source access for those
users. These recording rules do not feed watchdog termination decisions.

## Focused validation and rollout

Run `promtool check config` on the deployed configuration with its credential
file mounted, `promtool check rules` on the new rule file, and the included rule
fixtures with `promtool test rules`:

```sh
cd services/prometheus/tests
promtool test rules determined-task-resources.test.yml
```

Keep fixtures outside `rules/` so the production wildcard never loads test YAML.
Run the small credential writer regression:

```sh
cd services/determined-watchdog/build
python3 -m unittest test_metrics_token.py
```

Before applying to lab services, inspect `/api/v1/targets` and a few real cAdvisor
and DCGM samples. Check unique Docker/GPU identities, cluster/node labels, and
unsupported GPU sentinel values. Confirm the same task's `.1` and `.2` allocation
curves remain separate after pause/resume. A CPU fixture or synthetic GPU samples
do not establish single- or multi-GPU hardware acceptance.

Check `findmnt -T /srv/nfs/var/prometheus` on the Prometheus host. If its filesystem
is NFS, plan a backed-up migration to local storage before using it as the active
TSDB. The path name alone does not establish filesystem type; this PR does not
move or delete existing time-series data. For a migration, provision the local
filesystem and copy the TSDB once for preparation, then **stop Prometheus and
make a final copy while it is stopped** before changing the bind mount. Check
ownership for UID 1000, start Prometheus against the local copy, and verify
targets and historical queries. Retain the old NFS directory unchanged as a
rollback copy; do not run two Prometheus processes against one TSDB. Any rollback
after new writes needs an explicit decision about the intervening samples.

References: [Prometheus configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/),
[rule tests](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/),
[Grafana URL variables](https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/create-dashboard-url-variables/).
