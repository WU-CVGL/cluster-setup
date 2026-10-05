# GPU Topology

How the GPUs of each node connect to each other and to the CPUs. This matters for jobs whose GPUs talk to each other (DDP, NCCL collectives, peer copies): GPUs on the same CPU socket communicate faster than GPUs on different sockets. For the P2P driver and the measured bandwidths, see [GPU P2P on GeForce](05_GPU_P2P_GeForce.md).

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

A close class does not mean peer-to-peer works: check `nvidia-smi topo -p2p r` (`OK`; `GNS` means the GPU or driver does not support it). The GPU index of `nvidia-smi` is not the Determined slot ID; match GPUs by UUID or PCI bus ID.

## GPU nodes 01-08

All eight nodes have the same layout:

- Two CPU sockets, each its own NUMA node, with four GPUs per socket.
- Every GPU sits behind its own PCIe host bridge: GPUs on the same socket are `NODE`, GPUs on different sockets are `SYS`. No two GPUs share a PCIe switch, and there is no NVLink.
- Peer-to-peer reads are `OK` for every pair (patched open driver on nodes 01-07, see [GPU P2P on GeForce](05_GPU_P2P_GeForce.md); stock driver on node 08).

| Socket (NUMA node) | PCI bus IDs of the GPUs |
| :--- | :--- |
| 0 | `01:00.0`, `21:00.0` (nodes 06-08) / `23:00.0` (node 05) / `25:00.0` (nodes 01-04), `41:00.0`, `61:00.0` |
| 1 | `81:00.0`, `A1:00.0`, `C1:00.0`, `E1:00.0` |

- **Node 01:** its agent leaves out the GPU at `81:00.0` (see [Leaving out a faulty GPU](../services/determined/README.md#leaving-out-a-faulty-gpu)), so Determined sees four GPUs on socket 0 and three on socket 1.
- **PCIe link width:** a GPU that trained at x8 caps every ring that includes it, whatever the topology; some nodes have one or two. See [PCIe link width](05_GPU_P2P_GeForce.md#pcie-link-width) for how to check it.
- **100GbE/IB NIC:** `NIC0` in `nvidia-smi topo -m` shows which socket the ConnectX card is on (`PHB` to the GPU that shares its host bridge).

## g292

g292 is a single-socket node with a different layout:

- One AMD EPYC 7J13 (one NUMA node) and eight NVIDIA CMP 170HX.
- The GPUs come in four pairs, each pair behind one PCIe switch (`PIX`); pairs on different switches are `NODE`.

| Pair (`PIX`) | PCI bus IDs |
| :--- | :--- |
| GPU 0, 1 | `04:00.0`, `05:00.0` |
| GPU 2, 3 | `43:00.0`, `44:00.0` |
| GPU 4, 5 | `87:00.0`, `88:00.0` |
| GPU 6, 7 | `C3:00.0`, `C4:00.0` |

- With the stock driver the CMP 170HX does no peer-to-peer (`GNS` for every pair), so traffic between its GPUs goes through host memory, and the two GPUs of a pair share their switch's link to the CPU. A patched open driver can enable peer-to-peer, as on nodes 01-07; recheck `nvidia-smi topo -p2p r` after a driver change.

## Choosing GPUs for a job

- **Nodes 01-08:** a job of up to four GPUs that communicate should stay on one socket. With P2P, four GPUs on one socket reach a higher all-reduce bandwidth than any set that crosses the sockets; see the results table in [GPU P2P on GeForce](05_GPU_P2P_GeForce.md#results-summary). An eight-GPU job always spans both sockets.
- **g292:** a two-GPU job is closest on one pair; larger jobs always cross switches. Without peer-to-peer, the pair has no advantage and shares one uplink.
- Jobs whose GPUs work independently (no collectives) do not benefit from any of this.
