# Determined Configuration Files

The cluster runs our fork, [WU-CVGL/determined](https://github.com/WU-CVGL/determined), version `0.41.0`, with its images `ghcr.io/wu-cvgl/determined-master` and `ghcr.io/wu-cvgl/determined-agent`. Install the fork's `det` CLI first (see [docs/01](../../docs/01_First-time_Setup_of_Cluster_Nodes.md#install-determined-ai-systemwide)). Keep the CLI, the master and all agents on the same version. The fork has no version gate, so mixed versions work during a rolling agent upgrade; the fork's [hot upgrade guide](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/hot-upgrade.md) says which mixes were tested.

- [Configuration file](../system-configurations/etc/determined/master.yaml): a copy of the master's `master.yaml`, without secrets.

## Master

The master runs on the core VM (`cvglcorevm`) as a container started with `docker run`. Do not use `det deploy local master-up`: it stops and recreates the database container. The database is the container `determined_determined-db_1` (`postgres:10.14`, volume `determined_determined-db-volume`, network alias `determined-db`) from the original `det deploy local`; leave it as it is.

Each master version has its own deploy directory `~/determined-deploy/<version>-<yyyymmdd>/` (mode 700, files mode 600) with:

- `master.yaml`: the configuration (the [copy in this repo](../system-configurations/etc/determined/master.yaml) has no secrets).
- `master.env`: `DET_MASTER_HTTP_PORT`, `DET_DB_PASSWORD` and `DET_LOG_INFO`.

Start the master from it, with the image pinned by its digest (`docker images --digests ghcr.io/wu-cvgl/determined-master` after the pull):

```bash
D=~/determined-deploy/<version>-<yyyymmdd>
docker run -d --name determined-master-<version> --restart unless-stopped \
    --network determined_default -p 8080:8080 --env-file $D/master.env \
    -v /shared/determined_shared_fs:/determined_shared_fs \
    -v $D/master.yaml:/etc/determined/master.yaml:ro \
    ghcr.io/wu-cvgl/determined-master@<digest> --config-file /etc/determined/master.yaml
```

The WebUI, https://gpu.cvgl.lab, goes through [NGINX](../nginx/build/nginx.conf). The master's session cookie is HttpOnly; NGINX must forward the `Host` header unchanged (`proxy_set_header Host $host`).

### Changing master.yaml

`master.yaml` is bind-mounted as a single file. Edit it in place: `cp` the new version onto it, or use an editor that writes in place. Never replace it with `mv`: the running container keeps reading the old file. Then restart the master and check what it reads:

```bash
docker restart determined-master-<version>
docker exec determined-master-<version> cat /etc/determined/master.yaml
```

A restart is a short master outage; running tasks continue (see the time limits in [Upgrade](#upgrade)).

### Backups

Back up the database before an upgrade, into `~/determined-deploy/backups/` (mode 700), and check that the dump can be read:

```bash
B=~/determined-deploy/backups
docker exec determined_determined-db_1 pg_dump -U postgres -Fc -Z 6 determined > $B/determined-<old version>-<yyyymmdd>.dump
docker exec determined_determined-db_1 pg_dumpall -U postgres --globals-only > $B/globals-<old version>-<yyyymmdd>.sql
docker exec -i determined_determined-db_1 pg_restore -l < $B/determined-<old version>-<yyyymmdd>.dump | head
```

The old version's deploy directory keeps its `master.yaml` and `master.env`. Delete the dumps once the upgrade is verified.

### Upgrade

The master and the agents can be replaced while tasks run (hot upgrade) when the release allows it: see the fork's [hot upgrade guide](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/hot-upgrade.md) and the release notes. In short:

1. Install the new CLI ([docs/01](../../docs/01_First-time_Setup_of_Cluster_Nodes.md#install-determined-ai-systemwide)). Pull the new images on core (`determined-master`) and on every GPU node (`determined-agent`); pulling does not touch running containers.
2. Back up the database ([Backups](#backups)).
3. Create the new deploy directory with copies of the old `master.yaml` and `master.env`:

    ```bash
    install -d -m 700 ~/determined-deploy/<version>-<yyyymmdd>
    cp -p ~/determined-deploy/<old version>-<yyyymmdd>/{master.yaml,master.env} ~/determined-deploy/<version>-<yyyymmdd>/
    ```

4. Stop the old master and keep its container for a rollback:

    ```bash
    docker update --restart=no determined-master-<old version>
    docker stop determined-master-<old version>
    ```

5. Start the new master ([Master](#master)) and check its log for the new version and the database migration:

    ```bash
    docker logs determined-master-<version> 2>&1 | grep -E 'Determined master|migrated from|no migrations|views'
    ```

6. Check `det master info` (the new version), `det agent list` (every agent back), `det resource-pool list-dynamic` (every pool `Ready`) and the running tasks.
7. Replace the agents, one node at a time ([Agents](#agents)).

Time without a master: agents exit after about 145 s and Docker restarts them; once the master is back they reconnect and reattach their running task containers. Tasks that write output are killed after about 11 minutes without a master. Keep every master outage, also a restart after a configuration change, well under ten minutes.

### Rollback

Back up the database, then stop the new master and start the old one again (a binary swap):

```bash
docker update --restart=no determined-master-<version>
docker stop determined-master-<version>
docker update --restart=unless-stopped determined-master-<old version>
docker start determined-master-<old version>
```

This works only when the old master starts against the migrated database with the `master.yaml` of its own deploy directory. The preconditions are in the fork's [hot upgrade](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/hot-upgrade.md) and [dynamic resource pools](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/dynamic-pools.md) guides. In particular, the old `master.yaml` must not define a pool that is now a dynamic pool (after a pool conversion, remove the pools from it too, in place), and must not have settings that only the new version knows. Otherwise roll back cold: stop the agents and the master, restore the backup, and start the previous version.

## Agents

Every GPU node runs one agent container, `det-agent-<hostname>`, started with `docker run`. Its hostname (`cvgl-node01` to `cvgl-node08`) is the agent ID. `<master ip>` is the master's address (`DET_MASTER` in the login node's `/etc/environment`), `<pool>` the node's pool from the table below:

```bash
docker run -d --name det-agent-<hostname> --hostname <hostname> --network host --restart unless-stopped --init \
    --gpus 'all,"capabilities=gpu,utility"' --label ai.determined.type=agent \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e DET_MASTER_HOST=<master ip> -e DET_MASTER_PORT=8080 -e DET_RESOURCE_POOL=<pool> \
    ghcr.io/wu-cvgl/determined-agent:<version> run
```

`--gpus 'all,"capabilities=gpu,utility"'` passes all GPUs; to leave one out of the slots see [Leaving out a faulty GPU](#leaving-out-a-faulty-gpu). Docker rejects `--gpus '"all,capabilities=gpu,utility"'` (`unexpected key 'all,capabilities'`), and `--gpus '"capabilities=gpu,utility"'` (no `all`, no `device=`) gives the container only one GPU. Check the slots with `det agent list` or `docker logs det-agent-<hostname>` ("detected compute devices").

`det deploy local agent-up <master ip> --agent-resource-pool=<pool>`, with the CLI of the same version, starts the same container with all GPUs; it cannot take a GPU list.

To replace an agent, e.g. with a new version, pull the new image first, then on its node:

```bash
docker stop det-agent-<hostname>
docker update --restart=no det-agent-<hostname>
docker rename det-agent-<hostname> det-agent-<hostname>-<old version>   # kept for a rollback
```

Start the new container with the same settings (`docker run` above, with the old container's arguments after the image, e.g. `run --exclude-gpus <UUID>`: `docker inspect -f '{{json .Args}}' det-agent-<hostname>-<old version>` shows them) and check that `det agent list` shows the agent with the same slot count. With the same GPUs and pool, the new agent reattaches the running task containers (see [Restarting or replacing an agent](#restarting-or-replacing-an-agent)). Remove the old container once the new version has proven itself.

`<pool>` is one of these pools:

| Pool | Hardware | Nodes |
| :--- | :--- | :--- |
| `32c64t_256_3090` | 2x AMD Epyc 7302, 256 GiB RAM, RTX 3090 | node01 |
| `48c96t_512_3090` | 2x AMD Epyc 7402, 512 GiB RAM, RTX 3090 | node03, node04 |
| `48c96t_512_4090` | 2x AMD Epyc 7402, 512 GB RAM, RTX 4090 | node02 |
| `64c128t_512_4090` | 2x AMD Epyc 7543, 512 GB RAM, RTX 4090 | node05 |
| `128c256t_1536_4090` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 4090 | node06 |
| `128c256t_1536_4090_48` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 4090 48G | node07 |
| `128c256t_1536_6000Ada` | 2x AMD Epyc 9554, 1536 GB RAM, RTX 6000 Ada | node08 |
| `64c128t_1024_170hx_64` | AMD Epyc 7J13, 1024 GB RAM, CMP 170HX | none; restricted to administrators and lzzhao |
| `temp` | none | none (for temporary use) |

All of them are dynamic pools ([Dynamic resource pools](#dynamic-resource-pools)). The master also has the built-in `default` pool, which is static and empty: an agent without `DET_RESOURCE_POOL` joins it. Tasks that name no pool run in the default pools set in `master.yaml`: `48c96t_512_3090` with GPUs, `128c256t_1536_4090` without.

Pick the pool that matches the node's hardware (see the hardware tables in the [top-level README](../../README.md#hardware-information)) and check the current assignment with `det agent list` before restarting an agent.

## Agent operations

### Restarting or replacing an agent

The master keeps a disconnected agent for `agent_reconnect_wait` (10 minutes on all named pools) so that it can reconnect without losing its tasks. Within that window the master treats a new agent with the same ID (the node's hostname) as the old one coming back:

- Same GPUs and pool: the agent is restored and its running task containers are reattached. Restarting the agent container (`docker restart`) is safe this way.
- Different slots (another GPU count, GPU list or exclude list) or resource pool: the master stops the agent and fails every task that ran on it, also on GPUs that did not change; the restarted agent kills task containers it is not told to reattach. This is by design: the master accepts a reconnect only when every slot ID still has the same GPU.

Rules:

1. Never change an agent's GPU set, exclude list or pool while tasks run on it: `det agent disable --drain <agent>` and wait until nothing runs on it first.
2. After stopping or removing an agent container, wait until `det agent list` no longer lists the agent (up to `agent_reconnect_wait`) before starting one with a different GPU set, exclude list or pool. Masters without the fork fix [WU-CVGL/determined#24](https://github.com/WU-CVGL/determined/pull/24) otherwise drop the new agent one `agent_reconnect_wait` later: it stays connected, but `det agent list` no longer shows it and the API answers `agent '<agent>' not found`. There is no API or CLI command to remove an agent from the master.

- `det deploy local agent-down` stops and removes the container named `--agent-name` (default `det-agent-<hostname>`, the name of the nodes' agent containers); `--all` removes every container labelled `ai.determined.type=agent`, including old agent containers kept stopped for a rollback. A container with another name is not found: stop it with `docker rm -f` before `agent-up`.
- Never run two agent containers with the same agent ID on a node: the master accepts only one connection per ID, and the other one restarts in a loop with `websocket already connected` (`docker ps` shows `Restarting`).

### Leaving out a faulty GPU

Give the agent all GPUs and name the faulty one with `--exclude-gpus` after `run`. The agent reports the GPU, so `det agent describe` and the resource pool page show it as excluded, but never offers it as a slot: no task gets it, and this survives reboots and reconnects. The other slots keep their `nvidia-smi` index, so the slot IDs have a gap. Name the GPU by its UUID (`nvidia-smi --query-gpu=pci.bus_id,uuid --format=csv,noheader`), never by index; an entry that matches no GPU stops the agent. There is no environment variable for it. The slots change, so follow the rules above:

```bash
docker run -d --name det-agent-<hostname> --hostname <hostname> --network host --restart unless-stopped --init \
    --gpus 'all,"capabilities=gpu,utility"' --label ai.determined.type=agent \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e DET_MASTER_HOST=<master ip> -e DET_MASTER_PORT=8080 -e DET_RESOURCE_POOL=<pool> \
    ghcr.io/wu-cvgl/determined-agent:<version> run --exclude-gpus <faulty GPU UUID>
```

node01 runs this way and leaves out its GPU at `81:00.0`, so its slots are 0-3 and 5-7. Check the slots with `det slot list` or `docker logs det-agent-<hostname>` ("excluded by exclude_gpus, not a slot").

Agents before fork 0.41.0 refuse `--exclude-gpus`. Before rolling an agent back to such a version, hide the GPU from its container instead. That agent does not see the GPU at all, so the GPU is missing from the topology, and the slots after it move down by one: drain first, as for any slot change.

```bash
GPUS=$(nvidia-smi --query-gpu=pci.bus_id,uuid --format=csv,noheader | grep -v '^<faulty bus id>' | cut -d' ' -f2 | paste -sd,)
docker run -d --name det-agent-<hostname> --hostname <hostname> --network host --restart unless-stopped --init \
    --gpus "\"device=$GPUS\",\"capabilities=gpu,utility\"" --label ai.determined.type=agent \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e DET_MASTER_HOST=<master ip> -e DET_MASTER_PORT=8080 -e DET_RESOURCE_POOL=<pool> \
    ghcr.io/wu-cvgl/determined-agent:<version> run
```

`det slot disable <agent> <slot>` only keeps the scheduler off a slot until the next `det agent enable`/`disable`, reconnect or agent restart, which all reset it. Anything outside Determined (other containers, monitoring, `nvtop`) still reaches an excluded or hidden GPU; if the GPU's fault affects the host, also leave it out of such tools.

## Dynamic resource pools

Every named pool is a dynamic pool of our fork, stored in the master's database: see the fork's [dynamic resource pools guide](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/dynamic-pools.md). The pools that were in `master.yaml` were adopted (`det resource-pool adopt`, with the entry copied verbatim) and then removed from it. `master.yaml` has no `resource_pools` key, so the master adds the built-in, empty `default` pool; it stays static, which keeps a rollback to 0.40.1 a binary swap. A pool without its own `scheduler` or `task_container_defaults` uses those of `master.yaml`, like a `master.yaml` pool.

- List the pools: `det resource-pool list-dynamic` shows each pool's `State`, `Revision`, `Active` (the revision the master runs) and `Pending restart`; `--json` adds the saved specs.
- Change a pool: edit its file below and run `det resource-pool update <pool> <pool>.yaml`. The file replaces the whole spec (a key left out falls back to its default). The change takes effect at the next master restart ([Changing master.yaml](#changing-masteryaml) shows how to restart).
- Add a pool: `det resource-pool create`, see [Add a resource pool](../../docs/03_Setup_DeterminedAI.md#add-a-resource-pool).
- Pools cannot be renamed or deleted.
- Never add a pool to `master.yaml`: the master refuses to start when a pool there has the name of a dynamic pool, unless it is the identical entry of an adopted pool.

The spec of each pool is kept in [`resource-pools/`](resource-pools/); the database holds the live one:

- [`32c64t_256_3090`](resource-pools/32c64t_256_3090.yaml)
- [`48c96t_512_3090`](resource-pools/48c96t_512_3090.yaml)
- [`48c96t_512_4090`](resource-pools/48c96t_512_4090.yaml)
- [`64c128t_512_4090`](resource-pools/64c128t_512_4090.yaml)
- [`128c256t_1536_4090`](resource-pools/128c256t_1536_4090.yaml)
- [`128c256t_1536_4090_48`](resource-pools/128c256t_1536_4090_48.yaml)
- [`128c256t_1536_6000Ada`](resource-pools/128c256t_1536_6000Ada.yaml)
- [`64c128t_1024_170hx_64`](resource-pools/64c128t_1024_170hx_64.yaml)
- [`temp`](resource-pools/temp.yaml)
