# Setup DeterminedAI

## Contents

- [Setup DeterminedAI](#setup-determinedai)
  - [Contents](#contents)
  - [Deploy a Determined AI Single-Node Cluster](#deploy-a-determined-ai-single-node-cluster)
  - [Scale to multi-node: configure NFS export \& NFS client](#scale-to-multi-node-configure-nfs-export--nfs-client)
  - [Scale to multi-node: configure Determined AI](#scale-to-multi-node-configure-determined-ai)
  - [TL;DR](#tldr)
    - [Installation](#installation)
    - [Launch master \& agents](#launch-master--agents)
  - [Conduct an experiment with `Determined AI`](#conduct-an-experiment-with-determined-ai)
    - [`ssh` to a remote server](#ssh-to-a-remote-server)
    - [Log in `Determined AI`](#log-in-determined-ai)
    - [Upload code and data to the server](#upload-code-and-data-to-the-server)
    - [Configure your virtual environment](#configure-your-virtual-environment)
  - [Maintainance](#maintainance)
    - [Upgrade Determined](#upgrade-determined)
    - [Add a resource pool](#add-a-resource-pool)

The cluster runs our fork of Determined, [WU-CVGL/determined](https://github.com/WU-CVGL/determined) (currently `0.40.1`): the `det` CLI, the master image `ghcr.io/wu-cvgl/determined-master` and the agent image `ghcr.io/wu-cvgl/determined-agent` all come from its releases. The upstream documentation linked below still describes the concepts and the configuration.

## Deploy a Determined AI Single-Node Cluster

Introduction

> https://docs.determined.ai/latest/introduction.html

Basic setup

> https://docs.determined.ai/latest/cluster-setup-guide/basic.html

Deploy a single node cluster

> https://docs.determined.ai/latest/cluster-setup-guide/deploy-cluster/sysadmin-deploy-on-prem/deploy.html#deploy-a-single-node-cluster

After a successful deployment, you can test the task submiting workflow:

![Diagram of submitting task](images/03_task-diagram.svg)

Interactive jupyter notebook task

> https://docs.determined.ai/latest/interfaces/notebooks.html
> https://docs.determined.ai/latest/reference/reference-interface/job-config-reference.html

Shell task for VSCode and PyCharm, etc.

> https://docs.determined.ai/latest/interfaces/commands-and-shells.html

## Scale to multi-node: configure NFS export & NFS client

The storage model:

![Storage Model](images/03_storage-model.svg)

(Temporary) Example of NFS configuration:

```sh
# On Node01 as NFS server
sudo su
apt install nfs-kernel-server
systemctl enable nfs-kernel-server --now
vim /etc/exports
################################################
/data   192.168.233.0/24(rw,sync,no_subtree_check,no_root_squash)
#################################################
exportfs -ar

mkdir -p /shared-data
echo "/data /shared-data nfs defaults,noatime,hard,nointr,rsize=32768,wsize=32768,_netdev 0 2" >> /etc/fstab
mount -a

# On Node02 as NFS client
sudo su
apt install nfs-common
mkdir -p /shared-data
echo "192.168.233.162:/data /shared-data nfs defaults,noatime,hard,nointr,rsize=32768,wsize=32768,_netdev 0 2" >> /etc/fstab
mount -a
```

Notes:

1) In the server configuration, we did not expose our NFS service to the campus network (`10.0.1.64/27`) to comply with security rules.
We only expose the NFS service to the private 10GbE network `192.168.233.0/24`.
2) In the client configuration, `192.168.233.233` is the IP of the `NFS Server`.
You can first check the availability of the NFS service on the client using the command `showmount -e 192.168.233.233`.

## Scale to multi-node: configure Determined AI

> https://docs.determined.ai/latest/cluster-setup-guide/deploy-cluster/sysadmin-deploy-on-prem/deploy.html#deploy-a-standalone-master

## TL;DR

### Installation

Install the CLI of our fork, in the version of the master (`0.40.1`); see [Install Determined AI Systemwide](./01_First-time_Setup_of_Cluster_Nodes.md#install-determined-ai-systemwide) for the nodes:

```bash
pip install -U "https://github.com/WU-CVGL/determined/releases/download/0.40.1/determined-0.40.1-py3-none-any.whl"
```

Not `pip install determined`: that is the upstream package.

### Launch master & agents

See [notes](../services/determined/README.md) and the [master configuration file](../services/system-configurations/etc/determined/master.yaml).

## Conduct an experiment with `Determined AI`

`Determined AI` provides a solution for creating a virtual environment with some computing resources (e.g., GPUs, memory, and CPUs).

### `ssh` to a remote server

```sh
ssh user_name@host_name
```

then, input `password` to complete `ssh`. You can ask the system admin for the `user_name`, `host_name` and `password`.

### Log in `Determined AI`

```sh
det user login user_name
```

then, input `password` to log in.

### Upload code and data to the server

Upload your code and data to a path `/workspace/xxx/` on the server, where `xxx` is your username (your NFS workspace).

### Configure your virtual environment

Write a configuration document `config.yaml` under the path `/workspace/xxx/`, in which:

```yaml
description: your_task_name
resources:
    slots: number_of_GPUs
bind_mounts:
  - host_path: /workspace/xxx/
    container_path: /run/determined/workdir/xxx/
environment:
    image: determinedai/environments:cuda-11.3-pytorch-1.10-tf-2.8-gpu-0.19.4
```

These parameters configure the virtual environment, where

- `description`: Tag your task.
- `resources`: The number of GPUs (i.e., `slots`) used to run the task.
- `bind_mounts`: Mount your data and code into the docker container. `host_path` is the path of the physical machine, `container_path` is the path inside the container.
- `environment`: The environment configuration of the docker container, in which `image` is `docker-image` that you uses.

Above all, start the virtual environment:

```sh
det shell start --config-file config.yaml
```

Then, `cd` to `/run/determined/workdir/xxx/` inside the container and run your code.

## Maintainance

See [Maintainance: Upgrade APT packages & `Determined AI`](./01_First-time_Setup_of_Cluster_Nodes.md#maintainance-upgrade-apt-packages--determined-ai).

### Upgrade Determined

Upgrade to a [release of our fork](https://github.com/WU-CVGL/determined/releases), following its [installation and deployment guide](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/distribution.md). Keep the CLI, the master and all agents on the same version. In short:

1. Disable the agents: `det agent disable --all --drain` lets the running tasks finish first, plain `det agent disable --all` stops them now (announce either). Once nothing runs, back up the PostgreSQL database.
2. On the master node, install the new CLI (see [Installation](#installation)) and start the new master: `det deploy local master-down`, then the `master-up` command of the [notes](../services/determined/README.md) with the new `--det-version`. Check that the database migration finished and that you can log in.
3. On every agent node, install the new CLI and restart the agent with the `agent-down`/`agent-up` commands of the [notes](../services/determined/README.md) and the new `--det-version`; enable the agents again (`det agent enable --all`).
4. Check tasks, metrics and checkpoints.

Rollback: stop the agents and the master, restore the database backup, and start the previous version again. Switching back to the old images alone does not undo the database migration.

### Add a resource pool

Add pools at runtime as dynamic pools of our fork ([guide](https://github.com/WU-CVGL/determined/blob/main/docs/maintenance/dynamic-pools.md)); do not edit `master.yaml` for that. A dynamic pool cannot be renamed, updated or deleted, so choose its name and settings carefully, and never list it in `master.yaml` afterwards (the master would refuse to start).

1. Write the pool as its own file in [`services/determined/resource-pools/`](../services/determined/resource-pools/) (the pool object itself, not a `resource_pools:` list). Use the settings of the other pools (`agent_reconnect_wait: 10m`, `max_aux_containers_per_agent: 100`, `agent_reattach_enabled: false`; see the [reference `master.yaml`](../services/system-configurations/etc/determined/master.yaml)). The scheduler and the task container defaults are copied from the master when the pool is created and do not follow later `master.yaml` changes.
2. As an administrator, create it with a fixed idempotency key (safe to repeat with the same file and key), and check that it becomes `Ready`:

    ```bash
    det resource-pool create <pool>.yaml --idempotency-key create-<pool>-<date>
    det resource-pool list-dynamic      # Pending -> Ready; Failed shows the error
    ```

    `--cluster-name` is not needed: the cluster has a single agent resource manager. If it ends `Failed`, fix the cause and run `det resource-pool retry <pool>` (it reuses the saved configuration, not the file).
3. Start the agents of the pool with `--agent-resource-pool=<pool>` (see the [notes](../services/determined/README.md)). Creating a pool does not move agents; move a busy agent only after disabling it with `--drain` and waiting for its tasks.
4. Check it with a small task: `det command run --config resources.resource_pool=<pool> --config resources.slots=1 nvidia-smi`.

Warning: Do not upgrade when the cluster is in use! Upgrading packages especially those related to the kernel, DKMS, GPU drivers and containers will kill running tasks.
