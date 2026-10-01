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

## Agent operations

### Restarting or replacing an agent

The master keeps a disconnected agent for `agent_reconnect_wait` (10 minutes on all pools) so that it can reconnect without losing its tasks. Within that window the master treats a new agent with the same ID (the node's hostname) as the old one coming back:

- Same GPUs and pool: the agent is restored and its running task containers are reattached. Restarting the agent container (`docker restart`) is safe this way.
- Different GPU count or resource pool: the master stops the agent and fails every task that ran on it, also on GPUs that did not change; the restarted agent kills task containers it is not told to reattach. This is by design: the master tracks slots by device index, not by UUID.

Rules:

1. Never change an agent's GPU set or pool while tasks run on it: `det agent disable --drain <agent>` and wait until nothing runs on it first.
2. After stopping or removing an agent container, wait until `det agent list` no longer lists the agent (up to `agent_reconnect_wait`) before starting one with a different GPU set or pool. Masters without the fork fix [WU-CVGL/determined#24](https://github.com/WU-CVGL/determined/pull/24) otherwise drop the new agent one `agent_reconnect_wait` later: it stays connected, but `det agent list` no longer shows it and the API answers `agent '<agent>' not found`. There is no API or CLI command to remove an agent from the master.

### Leaving out a faulty GPU

Hide the GPU from the agent: the agent then has one slot less, which survives reboots and reconnects. Select the remaining GPUs by UUID (`nvidia-smi --query-gpu=pci.bus_id,uuid --format=csv,noheader`), so that a changed numbering cannot bring the faulty GPU back, and follow the rules above (the slot count changes):

```bash
GPUS=$(nvidia-smi --query-gpu=pci.bus_id,uuid --format=csv,noheader | grep -v '^<faulty bus id>' | cut -d' ' -f2 | paste -sd,)
docker run -d --name det-agent-<agent> --hostname <agent> --network host --restart unless-stopped --init \
    --gpus "\"device=$GPUS\",\"capabilities=gpu,utility\"" --label ai.determined.type=agent \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e DET_MASTER_HOST=<master ip> -e DET_MASTER_PORT=8080 -e DET_RESOURCE_POOL=<pool> \
    ghcr.io/wu-cvgl/determined-agent:<version> run
```

This is the container `det deploy local agent-up` starts, with the GPU list instead of all GPUs; it also works on nodes where the `det` CLI is not installed. Without a GPU list use `--gpus '"all,capabilities=gpu,utility"'`: a bare `--gpus capabilities=gpu,utility` gives the container only one GPU. Check the slots with `det agent list` or `docker logs det-agent-<agent>` ("detected compute devices").

`det slot disable <agent> <slot>` only keeps the scheduler off a slot until the next `det agent enable`/`disable`, reconnect or agent restart, which all reset it. Anything outside Determined (other containers, monitoring, `nvtop`) still reaches a hidden GPU; if the GPU's fault affects the host, also leave it out of such tools.

## Dynamic resource pools

Our fork (0.40.1 and later) adds resource pools at runtime, without restarting the master: see the fork's [dynamic resource pools guide](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/dynamic-pools.md) and [Add a resource pool](../../docs/03_Setup_DeterminedAI.md#add-a-resource-pool). A dynamic pool is stored in the master's database and cannot be renamed, updated or deleted. Never add it to `master.yaml`: the master refuses to start when a static pool has the name of a dynamic one. The file of each created pool is kept in [`resource-pools/`](resource-pools/):

- [`128c256t_1536_4090_48`](resource-pools/128c256t_1536_4090_48.yaml)

