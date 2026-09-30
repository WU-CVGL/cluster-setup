# Determined Configuration Files

The cluster runs our fork, [WU-CVGL/determined](https://github.com/WU-CVGL/determined), currently version `0.40.1`, with its images `ghcr.io/wu-cvgl/determined-master` and `ghcr.io/wu-cvgl/determined-agent`. Install the fork's `det` CLI first (see [docs/01](../../docs/01_First-time_Setup_of_Cluster_Nodes.md#install-determined-ai-systemwide)). Without `--image-repo-prefix ghcr.io/wu-cvgl`, `det deploy local` starts the upstream `determinedai/` images; `--det-version` defaults to the CLI's version. Keep the CLI, the master and all agents on the same version.

- [Configuration file](../system-configurations/etc/determined/master.yaml) location: /etc/determined/master.yaml

## Master-up command

```bash
det deploy local master-up --image-repo-prefix ghcr.io/wu-cvgl --det-version 0.40.1 --master-config-path /etc/determined/master.yaml
```

## Agent-up command

```bash
det deploy local agent-up $DET_MASTER --image-repo-prefix ghcr.io/wu-cvgl --det-version 0.40.1 --agent-resource-pool=<pool>
```

`<pool>` must be a resource pool of the master: a static pool of `master.yaml` or a dynamic pool (`det resource-pool list-dynamic`):

| Pool | Hardware | Nodes |
| :--- | :--- | :--- |
| `32c64t_256_3090` | 2x AMD Epyc 7302, 256 GiB RAM, RTX 3090 | node01 |
| `48c96t_512_3090` | 2x AMD Epyc 7402, 512 GiB RAM, RTX 3090 | node03, node04 |
| `48c96t_512_4090` | 2x AMD Epyc 7402, 512 GB RAM, RTX 4090 | node02 |
| `64c128t_512_4090` | 2x AMD Epyc 7543, 512 GB RAM, RTX 4090 | node05 |
| `128c256t_1536_4090` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 4090 | node06 |
| `128c256t_1536_4090_48` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 4090 48G | node07 (dynamic pool) |
| `128c256t_1536_6000Ada` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 6000 Ada | node08 |
| `temp` | none | none (for temporary use) |

All pools except `128c256t_1536_4090_48` are static pools in `master.yaml`.

Pick the pool that matches the node's hardware (see the hardware tables in the [top-level README](../../README.md#hardware-information)) and check the current assignment with `det agent list` before restarting an agent.

## Dynamic resource pools

Our fork (0.40.1 and later) adds resource pools at runtime, without restarting the master: see the fork's [dynamic resource pools guide](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/dynamic-pools.md) and [Add a resource pool](../../docs/03_Setup_DeterminedAI.md#add-a-resource-pool). A dynamic pool is stored in the master's database and cannot be renamed, updated or deleted. Never add it to `master.yaml`: the master refuses to start when a static pool has the name of a dynamic one. The file of each created pool is kept in [`resource-pools/`](resource-pools/):

- [`128c256t_1536_4090_48`](resource-pools/128c256t_1536_4090_48.yaml)

