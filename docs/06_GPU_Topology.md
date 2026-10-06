# GPU Topology

How the GPUs of each node connect to each other and to the CPUs. This matters for jobs whose GPUs talk to each other (DDP, NCCL collectives, peer copies): on the two-socket nodes, GPUs on the same CPU socket communicate faster than GPUs on different sockets. For the P2P drivers and the measured bandwidths, see [GPU P2P on GeForce and CMP](05_GPU_P2P_GeForce_and_CMP.md).

## Contents

- [GPU Topology](#gpu-topology)
  - [Contents](#contents)
  - [Reading the topology](#reading-the-topology)
  - [GPU nodes 01-08](#gpu-nodes-01-08)
  - [g292](#g292)
  - [Choosing GPUs for a job](#choosing-gpus-for-a-job)

## Reading the topology

Run on the host (not in a task container):

```bash
nvidia-smi --query-gpu=index,uuid,pci.bus_id,name --format=csv,noheader
nvidia-smi topo -m        # how each pair of GPUs connects
nvidia-smi topo -p2p r    # whether each pair can do peer-to-peer reads
```

`nvidia-smi topo -m` classes, from closest to farthest:

| Class | Path between the two GPUs |
| :--- | :--- |
| `NV#` | NVLink (`#` bonded links) |
| `PIX` | At most one PCIe switch |
| `PXB` | Several PCIe switches, no host bridge |
| `PHB` | Through a PCIe host bridge (the CPU) |
| `NODE` | Between PCIe host bridges within one NUMA node (one CPU socket here) |
| `SYS` | Across the interconnect between NUMA nodes (CPU sockets) |

A close class does not mean peer-to-peer works: check `nvidia-smi topo -p2p r` (`OK`; `GNS` means the GPU or driver does not support it). Determined's slot IDs are the `nvidia-smi` indices the agent sees: the host's indices when the agent container gets all GPUs (also with `exclude_gpus`), not when it gets only some. Inside a task container the GPUs are numbered from 0. Match GPUs by UUID or PCI bus ID.

Determined (our fork, 0.41.0 and later) shows the topology as the agent measured it at its start: the GPU Topology and GPU Health columns of `det agent list`, `det agent describe <agent>`, and the Topology section of a resource pool's page in the WebUI, which groups each agent's GPUs by NUMA node and PCIe switch. Hover over a GPU for its details (`PCIe link`, `NVML errors`, `Collected at`); a click keeps them open. Reference: "GPU topology and health" in the fork's [agent configuration reference](https://github.com/WU-CVGL/determined/blob/main/docs/reference/deploy/agent-config-reference.rst).

## GPU nodes 01-08

All eight nodes have the same layout:

- Two CPU sockets, each its own NUMA node, with four GPUs per socket.
- Every GPU sits behind its own PCIe host bridge: GPUs on the same socket are `NODE`, GPUs on different sockets are `SYS`. No two GPUs share a PCIe switch, and there is no NVLink.
- Peer-to-peer reads are `OK` for every pair (patched open driver on nodes 01-07, see [GPU P2P on GeForce and CMP](05_GPU_P2P_GeForce_and_CMP.md); stock driver on node 08).

| Socket (NUMA node) | PCI bus IDs of the GPUs |
| :--- | :--- |
| 0 | `01:00.0`, `21:00.0` (nodes 06-08) / `23:00.0` (node 05) / `25:00.0` (nodes 01-04), `41:00.0`, `61:00.0` |
| 1 | `81:00.0`, `A1:00.0`, `C1:00.0`, `E1:00.0` |

- **Node 01:** its agent excludes the GPU at `81:00.0` with `exclude_gpus` (see [Leaving out a faulty GPU](../services/determined/README.md#leaving-out-a-faulty-gpu)). Determined shows that GPU as excluded, and the slots are 0-3 on socket 0 and 5-7 on socket 1.
- **PCIe link width:** a GPU that trained at x8 caps every ring that includes it, whatever the topology; some nodes have one or two. Determined marks a GPU whose link was below its maximum width at agent start as `link below max` (`narrow` in the CLI). See [PCIe link width](05_GPU_P2P_GeForce_and_CMP.md#pcie-link-width) for how to check it.
- **100GbE/IB NIC:** `NIC0` in `nvidia-smi topo -m` shows which socket the ConnectX card is on (`PHB` to the GPU that shares its host bridge).

## g292

g292 is a single-socket node with a different layout:

- One AMD EPYC 7J13 (one NUMA node) and eight NVIDIA CMP 170HX, every link at PCIe Gen2 x16.
- The GPUs come in four pairs, each pair behind one PCIe switch (`PIX`), each switch on its own root port; pairs on different switches are `NODE`.

| Pair (`PIX`) | PCI bus IDs |
| :--- | :--- |
| GPU 0, 1 | `04:00.0`, `05:00.0` |
| GPU 2, 3 | `43:00.0`, `44:00.0` |
| GPU 4, 5 | `87:00.0`, `88:00.0` |
| GPU 6, 7 | `C3:00.0`, `C4:00.0` |

- Peer-to-peer reads are `OK` for all 56 ordered pairs with the BAR1 P2P build of cmpunlocker ([CMP 170HX](05_GPU_P2P_GeForce_and_CMP.md#cmp-170hx)). Two host settings go with it: ACS redirect off on the bridges above the GPUs, so that the two GPUs of a switch reach each other across the switch instead of through the root port ([why](05_GPU_P2P_GeForce_and_CMP.md#acs-redirect-behind-pcie-switches)), and `NCCL_P2P_LEVEL=SYS` in `/etc/nccl.conf` ([in jobs](05_GPU_P2P_GeForce_and_CMP.md#cmp-170hx-using-p2p-in-jobs)). Then pairs, four and eight GPUs all reach 6.2-6.4 GB/s all-reduce bus bandwidth, on one switch or across: the Gen2 links set the limit, not the switch distance ([results](05_GPU_P2P_GeForce_and_CMP.md#results-summary)).
- Without peer-to-peer (`GNS` for every pair, as with a driver without the P2P patches), traffic between the GPUs goes through host memory, and the two GPUs of a pair share their switch's link to the CPU. Recheck `nvidia-smi topo -p2p r` after a driver change.

## Choosing GPUs for a job

- **Nodes 01-08:** a job of up to four GPUs that communicate should stay on one socket. With P2P, four GPUs on one socket reach a higher all-reduce bandwidth than any set that crosses the sockets; see the results table in [GPU P2P on GeForce and CMP](05_GPU_P2P_GeForce_and_CMP.md#results-summary). An eight-GPU job always spans both sockets.
- **g292:** with peer-to-peer, ACS redirect off and `NCCL_P2P_LEVEL=SYS`, pairs, four and eight GPUs run at 6.2-6.4 GB/s whichever GPUs they use. Without peer-to-peer, sharing a switch is a disadvantage: the two GPUs of a pair share one uplink, so a pair on one switch is the slowest set (all-reduce 1.70 GB/s against 3.00 for a pair on two switches, and 1.93 for four GPUs on two switches against 4.19 for one GPU per switch).
- Jobs whose GPUs work independently (no collectives) do not benefit from any of this.
