# Determined Configuration Files

The cluster runs our fork, [WU-CVGL/determined](https://github.com/WU-CVGL/determined), currently version `0.40.1`, with its images `ghcr.io/wu-cvgl/determined-master` and `ghcr.io/wu-cvgl/determined-agent`. Install the fork's `det` CLI first (see [docs/01](../../docs/01_First-time_Setup_of_Cluster_Nodes.md#install-determined-ai-systemwide)). Without `--image-repo-prefix ghcr.io/wu-cvgl`, `det deploy local` starts the upstream `determinedai/` images; `--det-version` defaults to the CLI's version. Keep the CLI, the master and all agents on the same version.

- [Configuration file](../system-configurations/etc/determined/master.yaml) location: /etc/determined/master.yaml
- Live master (2026-09-30): on the core VM (192.168.233.6), the container `determined-master-0401` was started with `docker run` (restart policy `unless-stopped`), not with `master-up`. Its configuration is `/home/cvgladmin/determined-deploy/0.40.1-20260929/master-040.yaml`, mounted read-only; `/etc/determined/master.yaml` there is not used. A configuration change needs `docker restart determined-master-0401`; running tasks have survived such restarts because the agents reconnected within seconds (0.38.1 agents give up after about 25 s, see the fork's [task continuity notes](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/task-continuity.md)). Edit the file in place (`cp`, not `mv`): the bind mount keeps the old file otherwise.

## Master-up command

```bash
det deploy local master-up --image-repo-prefix ghcr.io/wu-cvgl --det-version 0.40.1 --master-config-path /etc/determined/master.yaml
```

## Agent-up command

```bash
det deploy local agent-up $DET_MASTER --image-repo-prefix ghcr.io/wu-cvgl --det-version 0.40.1 --agent-resource-pool=<pool>
```

`<pool>` must be a resource pool of the master: a static pool of `master.yaml` or a dynamic pool (`det resource-pool list-dynamic`):

| Pool | Hardware | Nodes (2026-09-30) |
| :--- | :--- | :--- |
| `32c64t_256_3090` | 2x AMD Epyc 7302, 256 GiB RAM, RTX 3090 | node01 |
| `48c96t_512_3090` | 2x AMD Epyc 7402, 512 GiB RAM, RTX 3090 | node03, node04 |
| `48c96t_512_4090` | 2x AMD Epyc 7402, 512 GB RAM, RTX 4090 | node02 |
| `64c128t_512_4090` | 2x AMD Epyc 7543, 512 GB RAM, RTX 4090 | node05 |
| `128c256t_1536_4090` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 4090 | node06 |
| `128c256t_1536_4090_48` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 4090 48G | node07 (dynamic pool, see below) |
| `128c256t_1536_6000Ada` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 6000 Ada | node08 |
| `temp` | none | none (for temporary use) |

All pools except `128c256t_1536_4090_48` are static pools in `master.yaml`.

Pick the pool that matches the node's hardware (see the hardware tables in the [top-level README](../../README.md#hardware-information)) and check the current assignment with `det agent list` before restarting an agent.

## Dynamic resource pools

Our fork (0.40.1 and later) adds resource pools at runtime, without restarting the master: see the fork's [dynamic resource pools guide](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/dynamic-pools.md) and [Add a resource pool](../../docs/03_Setup_DeterminedAI.md#add-a-resource-pool). A dynamic pool is stored in the master's database and cannot be renamed, updated or deleted. Never add it to `master.yaml`: the master refuses to start when a static pool has the name of a dynamic one. The file of each created pool is kept in [`resource-pools/`](resource-pools/):

- [`128c256t_1536_4090_48`](resource-pools/128c256t_1536_4090_48.yaml) (created 2026-09-30, `agent_reconnect_wait: 10m`): node07 with 8x RTX 4090 48G.

Start node07's agent in it (on node07; `agent-down` first if an agent container still exists there):

```bash
det deploy local agent-down
det deploy local agent-up 192.168.233.6 --image-repo-prefix ghcr.io/wu-cvgl --det-version 0.40.1 --agent-resource-pool=128c256t_1536_4090_48
```

