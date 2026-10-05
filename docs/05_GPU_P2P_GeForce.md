# GPU P2P on GeForce

## Contents

- [GPU P2P on GeForce](#gpu-p2p-on-geforce)
  - [Contents](#contents)
  - [Introduction](#introduction)
  - [Results summary](#results-summary)
  - [How it works](#how-it-works)
    - [Static or dynamic: how to tell](#static-or-dynamic-how-to-tell)
    - [What our fork changes](#what-our-fork-changes)
    - [Managed memory (UVM)](#managed-memory-uvm)
    - [Other caveats](#other-caveats)
  - [Prerequisites](#prerequisites)
  - [Procedure](#procedure)
    - [1. Disable the node in Determined](#1-disable-the-node-in-determined)
    - [2. Install the matching open driver](#2-install-the-matching-open-driver)
    - [3. Build the patched modules](#3-build-the-patched-modules)
    - [4. Install the modules](#4-install-the-modules)
    - [5. Reboot and verify](#5-reboot-and-verify)
    - [6. Run the tests](#6-run-the-tests)
    - [7. Enable the node again](#7-enable-the-node-again)
  - [Using P2P in jobs](#using-p2p-in-jobs)
  - [Pitfalls](#pitfalls)
  - [Rollback](#rollback)
  - [Appendix A: Measurements](#appendix-a-measurements)
  - [Appendix B: References](#appendix-b-references)

## Introduction

GeForce drivers disable PCIe peer-to-peer (P2P) between GPUs, so CUDA peer copies and NCCL collectives are staged through host memory. A patched build of the kernel modules of NVIDIA's **open** driver re-enables P2P on the RTX 3090 (Ampere) and RTX 4090 (Ada, including the modded 48 GB card): `cudaDeviceCanAccessPeer` returns true and NCCL uses its P2P transport. Userspace (libcuda, NCCL, PyTorch) stays unchanged. This is an **unofficial community patch** (tinygrad, then aikitoria's branches per driver version, then duanyll's "Method 3" for 48 GB cards; links in [Appendix B](#appendix-b-references)), not supported by NVIDIA.

This guide covers P2P over PCIe BAR1 on the three card types of this cluster listed below; other GeForce models were not tested. "24 GB cards" means the RTX 3090 and the RTX 4090 24 GB, "48 GB cards" the modded RTX 4090 48 GB.

We deploy one branch of our fork on every node with these cards: [`610.57.04-p2p-48g`](https://github.com/LingzheZhao/open-gpu-kernel-modules/tree/610.57.04-p2p-48g). It works for all three card types; the Method 3 code stays dormant where VRAM fits into BAR1.

| Card | Architecture | VRAM | Max BAR1 | P2P path | Nodes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| RTX 3090 | Ampere (`sm_86`) | 24 GB | 32 GiB | static BAR1: all of VRAM is mapped once | 1, 3, 4 |
| RTX 4090 24 GB | Ada (`sm_89`) | 24 GB | 32 GiB | static BAR1: all of VRAM is mapped once | 2, 5, 6 |
| RTX 4090 48 GB (modded) | Ada (`sm_89`) | 48 GiB | 32 GiB | dynamic BAR1: each shared allocation is mapped on demand | 7 |

Placeholders: `<agent>` Determined agent ID, `<K>` kernel release (`uname -r`), `<N>` driver branch (e.g. `610`), `<version>` full driver version (e.g. `610.57.04`), `<revision>` Ubuntu package revision, `<gcc>` major version of the compiler the kernel was built with, `<fork>` absolute path of the built fork tree, `<image>` CUDA + PyTorch image.

## Results summary

NCCL all-reduce bus bandwidth (GB/s, 1024 MiB, one process per GPU) without and with P2P. Details per node: [Appendix A](#appendix-a-measurements).

| Platform and card | 8 GPUs | 4 GPUs, one socket | Pair, cross socket | P2P copy per pair |
| :--- | :--- | :--- | :--- | :--- |
| EPYC 7402 (Rome), RTX 4090 24 GB, all GPUs at x16 | 1.3 -> 20.6 | 3.9 -> 25.1-25.2 | 1.2-1.3 -> 19.2-19.4 | 26.4; cross socket 21.8-22.7 |
| EPYC 7402 (Rome), RTX 3090, all GPUs at x16 | 1.6 -> 20.1 | 4.0 -> 24.6-24.7 | 1.4-1.6 -> 18.3-18.4 | 26.4; cross socket 21.5-22.7 |
| EPYC 7302 (Rome), RTX 3090, 7 GPUs, 2 at x8 | 1.5-1.6 -> 12.6 (x8 GPU) | 3.3-3.6 -> 12.7 (x8 GPU) | 1.3-1.6 -> 11.9-12.0 (x8 GPU) | 26.4; cross socket 22.7; with an x8 GPU 13.2 |
| EPYC 7543 (Milan), RTX 4090 24 GB | 0.8 -> 12.9 (x8 GPU) | 4.4-4.7 -> 12.9 (x8 GPU) | 0.8 -> 19.2 | 26.3; cross socket 10-22 (asymmetric) |
| EPYC 9554 (Genoa), RTX 4090 24 GB and 48 GB, all GPUs at x16 (combined, see note) | about 16 -> about 24-25 (estimated) | 20.4-21.1 -> 25.6-25.7 | 15.9-16.4 -> 23.4-23.6 | 26.4 in both directions |

- **Host staging speed decides the gain.** Rome and Milan stage through host memory slowly, so P2P gives about 6-16x on Rome (all x16, RTX 3090 and RTX 4090 alike) and 3-24x on Milan. Genoa stages fast, so P2P gives 1.2-1.5x. Milan and Genoa were measured with the RTX 4090 only.
- **A GPU at PCIe x8 caps every ring that contains it at about 13 GB/s**, with or without P2P (the RTX 3090 and RTX 4090 are both PCIe Gen4). Nodes 1, 5, 6 and 7 each have one or two such GPUs ([PCIe link width](#pcie-link-width)).
- **The Genoa row combines two nodes with the same platform.** Node 6 (RTX 4090 24 GB) and Node 7 (RTX 4090 48 GB) gave the same results wherever neither had an x8 GPU in the set: per-pair copies 26.4 vs 26.4 GB/s, one-socket 4-GPU all-reduce 25.6 vs 25.7, cross-socket pairs 23.5-23.6 vs 23.4-23.6. Their x8 GPUs sit on different sockets, so between them every 4-GPU and 2-GPU set was measured at x16. An 8-GPU set was not: on both nodes it contains an x8 GPU (13.0 measured). The 8-GPU ring crosses the sockets, so its result follows the cross-socket pair: on Rome (Node 2, RTX 4090 24 GB, all at x16), 8 GPUs reached 20.6 with P2P against 19.2-19.4 for the cross-socket pairs and 25.1 for four GPUs on one socket, and 1.3 without P2P against 1.2-1.3; the RTX 3090 row on the same CPU shows the same relation. The same relation on Genoa gives about 24-25 GB/s with P2P (cross-socket pairs 23.4-23.6, one socket 25.6-25.7) and about 16 without (cross-socket pairs at x16 measured 15.9-16.4 over host memory). These two values are estimates.

## How it works

**Static BAR1** (24 GB cards). BAR1 is the GPU's PCIe window into its VRAM. With Resizable BAR the patched driver resizes BAR1 at load time to the largest size the card advertises (32 GiB on the RTX 3090 and RTX 4090; the stock driver leaves it at 256 MiB on our nodes), identity-maps all of VRAM into it, and lets a peer GPU address the memory as `BAR1 bus address + offset`. The peer's DMA goes straight over PCIe through the IOMMU, hence `iommu=pt`. The driver turns this on by itself when VRAM fits into BAR1.

**Dynamic BAR1** (48 GB cards, duanyll's Method 3). The modded cards' BAR1 is at most 32 GiB, smaller than their VRAM, so a static map is impossible. Method 3 maps each allocation that is shared with a peer into a BAR1 window on demand. Allocations anywhere in the 48 GiB work; only the allocations shared at the same time must fit into 32 GiB. NCCL shares only its own buffers (about 180 MB per GPU on 8 GPUs).

### Static or dynamic: how to tell

`verify.sh` prints BAR1 and VRAM: BAR1 >= VRAM means static BAR1 is possible. Whether it is actually on shows only under load: with static BAR1 on, nvidia-smi's "BAR1 Used" follows the VRAM in use. Hold test on an idle GPU:

```bash
docker run --rm -d --name bar1-hold --gpus device=0 <image> \
    python -c "import torch, time; x = torch.empty(8 << 30, dtype=torch.uint8, device='cuda'); time.sleep(120)"
sleep 60; nvidia-smi -i 0 -q -d MEMORY | grep -A 3 BAR1; docker stop bar1-hold
```

"BAR1 Used" about 8 GiB above idle: static BAR1 on. Unchanged: dynamic windows (48 GB cards) or no BAR1 P2P. `nvidia-smi topo -p2p r` showing `OK` proves nothing on its own. Never force static BAR1 globally (`RMForceStaticBar1=1` breaks driver init on GPUs whose VRAM exceeds BAR1).

### What our fork changes

[LingzheZhao/open-gpu-kernel-modules](https://github.com/LingzheZhao/open-gpu-kernel-modules) has two branches on top of aikitoria's `610.57.04-p2p-v3`: `610.57.04-p2p-fixes` (the general fixes below, a candidate for upstream) and `610.57.04-p2p-48g` (those fixes plus Method 3, deployed everywhere). Compared with aikitoria v3 and duanyll's port:

- **No false P2P.** When BAR1 P2P is not possible for a pair, the driver reports no P2P instead of advertising it and failing at the first peer mapping (NCCL crashed on the 48 GB cards).
- **Managed memory is safe.** Cross-GPU `cudaMallocManaged` no longer faults the GPUs or corrupts memory ([below](#managed-memory-uvm)).
- **Compressible allocations** shared with a peer are mapped uncompressed. Before, the peer's accesses landed in host RAM at 1 TiB + offset.
- **Stale cache lines** of peer mappings are invalidated on unmap.
- **Method 3 hardening**: windows are torn down under the right locks, and misaligned windows are refused with a `METHOD3` log line instead of being accessed at the wrong address.

Each commit message has the details.

### Managed memory (UVM)

Since driver 590.44.01 UVM uses PCIe BAR1 peers automatically, and its pre-Hopper code (RTX 3090 and RTX 4090) addresses them with an aperture it cannot encode. With plain aikitoria v3 on the RTX 4090 24 GB (Node 5), cross-GPU migration of managed memory raised Xid 31 `FAULT_UNSUPPORTED_APERTURE`, then Xid 154 on all GPUs, and only a reboot recovered. The RTX 3090 uses the same pre-Hopper UVM code, so the same fault is expected; it was tested only with the fixed driver. Remote mappings (`cudaMemAdviseSetAccessedBy`) would silently point into the wrong GPU's memory. NCCL, CUDA IPC, cuMem, `cudaMemcpyPeer` and kernel peer access are not affected.

Our fork adds the `nvidia_uvm` module parameter `uvm_bar1_p2p_managed` (set in `/etc/modprobe.d/`, takes effect after a reboot or a reload of `nvidia_uvm`):

| Value | Behaviour |
| :--- | :--- |
| `0` (default) | Managed memory moves between GPUs through host memory. Everything else keeps P2P. |
| `1` (opt-in) | Managed memory also uses P2P. Validated with the RTX 4090 24 GB on Milan (Node 5, [Appendix A](#opt-in-uvm_bar1_p2p_managed1)), not with the RTX 3090. Run the [opt-in window](../scripts/gpu-p2p/README.md#managed-memory-and-the-uvm-bar1-fix) on each new combination of platform and card type first. 48 GB cards always use host memory. |

### Other caveats

| Caveat | What to do |
| :--- | :--- |
| On the RTX 4090, peer atomics are not atomic across GPUs (`cudaDevP2PAttrNativeAtomicSupported` = 0 in the `atomics` matrix): concurrent `atomicAdd` from two GPUs on the same peer memory can lose updates. Not measured on the RTX 3090. | Do not rely on cross-GPU atomics. The `ATOMICS=1` diagnostic shows the attribute per pair. |
| The patch skips the driver's check that the chipset supports peer reads. | On a new platform check `p2ptest` integrity before service. Fine on EPYC Rome with the RTX 3090 and RTX 4090, and on Milan and Genoa with the RTX 4090. |
| The GPU DMA mask is 47 bits: every BAR1 must end below 2^47. | On a new platform check Region 1 in `lspci -vv -d 10de:`. |
| Ordering of data written to one GPU and a flag written elsewhere (host memory or a third GPU) depends on the platform. | The `ordering` test checks it; run it on every new platform. |

## Prerequisites

- [ ] **BIOS**: Above 4G Decoding and Resizable BAR on; the host bridge windows must fit a 32 GiB BAR1 per GPU (the maximum on all three card types).
- [ ] **Secure Boot** off (or sign the modules yourself).
- [ ] **`unattended-upgrades` purged** ([docs/01](01_First-time_Setup_of_Cluster_Nodes.md#disable-unattended-updates)): it installs new kernels, which then boot without the P2P modules, and driver updates that break the version match. The install script refuses and `verify.sh` fails while it is installed.
- [ ] **Driver**: Ubuntu's `nvidia-driver-<N>-open` at **exactly** the version of the fork branch (`610.57.04`). The closed driver cannot be patched.
- [ ] **Build tools**: `linux-headers-<K>`, and the C and C++ compilers of the version the kernel was built with (`cat /proc/version`), e.g. `gcc-12` and `g++-12`: part of the driver is C++.
- [ ] **One card type per node** (RTX 3090, RTX 4090 24 GB or RTX 4090 48 GB): do not mix. The tests build for GPU0's compute capability only, and P2P between an RTX 3090 and an RTX 4090 was not tested.
- [ ] **PCIe links**: every GPU at full width under load (`nvidia-smi --query-gpu=index,pcie.link.width.current,pcie.link.width.max --format=csv -lms 500` while a job runs).
- [ ] **Maintenance window**: the node reboots; its tasks must finish or be stopped.

## Procedure

Scripts: [`scripts/gpu-p2p/`](../scripts/gpu-p2p/README.md). Steps marked **root** need `sudo`.

### 1. Disable the node in Determined

```bash
det agent disable --drain <agent>   # running tasks finish; without --drain they are stopped
det agent list                      # wait until nothing runs on <agent>
```

### 2. Install the matching open driver

**root**. No reboot yet.

```bash
apt-cache policy nvidia-driver-<N>-open                         # <version>-* must be listed
sudo apt install nvidia-driver-<N>-open=<version>-<revision>
```

`apt install` can pull in extra kernels (e.g. `linux-image-*-nvidia`) that become the default boot entry and have no matching modules. Remove them (`apt-get -s remove` first to see what goes) so the kernel you build for stays the default; step 4 refuses otherwise.

### 3. Build the patched modules

Unprivileged, on the node or a machine with the same kernel and headers:

```bash
git clone --depth 1 -b <version>-p2p-48g https://github.com/LingzheZhao/open-gpu-kernel-modules.git
cd open-gpu-kernel-modules
git rev-parse HEAD > .source-commit    # recorded in updates/p2p/SOURCE by the installer
make modules -j"$(nproc)" SYSSRC=/lib/modules/<K>/build CC=x86_64-linux-gnu-gcc-<gcc> CXX=x86_64-linux-gnu-g++-<gcc>
modinfo -F vermagic kernel-open/nvidia.ko   # must start with <K>
```

The build takes about a minute.

### 4. Install the modules

**root**, from this repository's checkout:

```bash
sudo SRC=<fork>/kernel-open scripts/gpu-p2p/install-p2p-modules.sh install   # K=<K> V=<version> optional
```

The script checks the driver and kernel versions, Secure Boot, `unattended-upgrades`, apt locks and GRUB, then:

- turns UVM HMM off ([why](#hmm-breaks-host-cumem-allocations-under-iommu-passthrough)) and adds `amd_iommu=on iommu=pt` to GRUB;
- checks that the default boot entry is `<K>` with `iommu=pt`, and stops before installing anything if not;
- holds the driver packages, so an apt upgrade cannot break the version match;
- copies the modules to `/lib/modules/<K>/updates/p2p/`, makes them win over the DKMS build (depmod override), and regenerates the initramfs.

It backs up what it changes in `/var/backups/nvidia-p2p/` and never reboots. Options: `--help`.

### 5. Reboot and verify

**root**: `sudo systemctl reboot`. Check that `det agent list` still shows `<agent>` disabled. Then as a user in the `docker` group:

1. The [hold test](#static-or-dynamic-how-to-tell): static BAR1 on for 24 GB cards, off for 48 GB cards.
2. `scripts/gpu-p2p/verify.sh` must end with `RESULT: PASS`. It checks the loaded modules, BAR1 size, `iommu=pt`, HMM off, `topo -p2p` `OK` for all pairs, link widths and the kernel log. A narrow link is only a `WARN`.
3. `cat /sys/module/nvidia_uvm/parameters/uvm_bar1_p2p_managed` must print `0`, or `1` only after a passed opt-in window on this platform and card type. If the file is missing, the modules lack the managed-memory fix: rebuild from the `-p2p-48g` branch.

### 6. Run the tests

Unprivileged, on the idle node:

```bash
# 6a: managed memory on one pair first
OUT_DIR=~/p2p-logs MANAGED=1 MANAGED_ARGS="--quick --pair 0,1" STAGES=managedtest scripts/gpu-p2p/tests/run_host.sh <image> <label>-quick
# 6b: the full service run; <n> = VRAM minus a few GiB (20 on 24 GB cards, 38 on 48 GB cards)
OUT_DIR=~/p2p-logs MANAGED=1 RESIDENT_GB=<n> scripts/gpu-p2p/tests/run_host.sh <image> <label>
```

The run checks peer copies and their integrity on every pair, write ordering, stale reads, host memory on every NUMA node, NCCL all-reduce over several GPU sets, and managed memory. It stops at the first failure.

Pass: both logs end with `OVERALL: PASS` and `KERNEL LOG: PASS`, and every all-reduce is `correct=True`. Do not add `ATOMICS=1` or `COMPRESS=1` (diagnostics; see the [script README](../scripts/gpu-p2p/README.md#settings)).

### 7. Enable the node again

```bash
det agent enable <agent>
```

Check that the monitoring containers came back after the reboot (`docker ps`: `node-exporter`, `monitoring_cadvisor`, the `dcgm-exporter` service; each needs a restart policy, [check](../services/README.md#72-run)). Tell the users of the node about [the NCCL settings](#using-p2p-in-jobs).

## Using P2P in jobs

Which GPUs share a socket on each node, and which sets to prefer for a job, is in [GPU Topology](06_GPU_Topology.md).

| Setting | When | Why |
| :--- | :--- | :--- |
| `NCCL_P2P_LEVEL=SYS` | Newer NCCL (2.27 in PyTorch 2.9) | Otherwise NCCL uses P2P only for pairs and host staging for larger groups on our multi-socket nodes. NCCL 2.20 uses P2P by default. |
| `NCCL_LOCAL_REGISTER=0`, `NCCL_GRAPH_REGISTER=0` | 48 GB cards | Keeps large user buffers out of BAR1 (duanyll's recommendation). |
| One process per GPU | Always on 48 GB cards | `torchrun`/Determined do this. A process that drives several GPUs with peer access (e.g. `DataParallel`) must fit all its memory into 32 GiB per GPU. |

`NCCL_DEBUG=INFO` shows the transport: `via P2P/IPC` or `via P2P/CUMEM` is P2P, `via SHM` is host staging. In Determined, set the variables in the experiment configuration:

```yaml
environment:
  environment_variables:
    - NCCL_P2P_LEVEL=SYS
    - NCCL_LOCAL_REGISTER=0
    - NCCL_GRAPH_REGISTER=0
```

## Pitfalls

### depmod override matches file names

`override` lines in `depmod.d` match the file name with dashes (`nvidia-uvm`), not the module name. With underscores only `nvidia` is overridden. `modinfo -k <K> -n nvidia_uvm` must print a path under `updates/p2p`. Do not use aikitoria's `install.sh` either: it installs where the DKMS modules win.

### HMM breaks host cuMem allocations under IOMMU passthrough

A bug in the open driver itself, independent of the P2P patch. With UVM HMM on (the default) and `iommu=pt`, the driver restricts host allocations made through cuMem to the lowest 4 GiB of memory (ZONE_DMA32). They fail on the second NUMA node and after about 1 GiB on the first. NCCL >= 2.26 then crashes in its SHM transport (`Error while attaching to shared memory segment /dev/shm/nccl-...`, [NVIDIA/nccl#2190](https://github.com/NVIDIA/nccl/issues/2190)). Cause: HMM registers device memory near the top of the physical address space, which makes the driver believe system memory exceeds the GPU's 47-bit DMA mask. Fix: `options nvidia-uvm uvm_disable_hmm=1` (set by the install script) and a reboot. Keep it as long as `iommu=pt` is set. Cost: GPUs cannot access plain `malloc`'d memory directly, which PyTorch jobs do not use.

### ACS redirect

PCIe ACS redirect forces peer traffic through the root complex. With `iommu=pt` and one GPU per root port it did not limit P2P on our nodes. If P2P bandwidth is far below host<->GPU bandwidth, `sudo scripts/gpu-p2p/acs-redir.sh status|off|restore` clears it until the next reboot (only with `iommu=pt`), and `pci=disable_acs_redir=pci:<vendor>:<device>` makes it persistent.

### PCIe link width

A GPU trained at x8 gets about 13 instead of 26 GB/s (PCIe Gen4) to the host and to peers, and caps every NCCL ring through it. The width can change between boots; `run_host.sh` records it. Fix it in hardware (seating, riser, slot).

### Kernel and driver upgrades

- The driver packages are held: upgrading the userspace driver alone gives `Driver/library version mismatch`.
- After a kernel upgrade the node boots the stock modules: safe, but without P2P until the modules are rebuilt and installed for the new kernel.
- A driver upgrade needs a fork branch for the new version: run `--restore` (below), install the new driver, build, install, reboot and verify.
- Never upgrade kernel or driver while the node runs tasks ([Maintainance](01_First-time_Setup_of_Cluster_Nodes.md#maintainance-upgrade-apt-packages--determined-ai)).

## Rollback

1. Disable the node ([step 1](#1-disable-the-node-in-determined)).
2. **root**: `sudo K=<K> scripts/gpu-p2p/install-p2p-modules.sh --restore`. It removes the modules and the override and, unless other kernels still have P2P modules, the GRUB parameters and package holds it added. HMM stays off while `iommu=pt` stays. Details and the manual equivalent: [script README](../scripts/gpu-p2p/README.md#roll-back). Remove a `pci=disable_acs_redir=...` parameter by hand.
3. **root**: reboot. `nvidia-smi topo -p2p r` shows `GNS` again and BAR1 is back at 256 MiB.
4. Enable the node.

## Appendix A: Measurements

All runs used the branch `610.57.04-p2p-48g`, `iommu=pt`, HMM off and `uvm_bar1_p2p_managed=0` unless noted, and `run_host.sh` with `MANAGED=1` on the idle node. Every service run ended with `OVERALL: PASS` and a clean kernel log: peer access and integrity on all ordered pairs (56; 42 on the 7 GPUs of Node 1), `ordering`, `stale` and `hostnuma` passed, every all-reduce was `correct=True`, and managed memory passed in all modes on all pairs including oversubscription. NCCL is PyTorch 2.3 / NCCL 2.20.5 (`P2P/IPC`) unless noted, busbw at 1024 MiB.

### GPU Node 1: RTX 3090, EPYC 7302 (Rome)

8x RTX 3090 24 GB, 256 GB, kernel 6.8.0-138-generic. Measured on 7 GPUs (`GPUS` setting of `run_host.sh`): GPU4 (`81:00.0`) trained at x4 and reset the host whenever it was loaded, also with the stock driver, so it is left out of the tests and of the Determined agent. GPU3 (`61:00.0`) and GPU5 (`a1:00.0`) trained at x8. In the 7-GPU numbering below, GPU3 and GPU4 are the x8 GPUs. Baseline: stock open driver 610.57.04, default IOMMU mode. Static BAR1 on (8 GiB held raised "BAR1 Used" to 8457 MiB). Service run on the 7 GPUs: `OVERALL: PASS`, kernel log clean.

| | Stock | P2P |
| :--- | ---: | ---: |
| Host<->GPU, x16 GPUs (GB/s, H2D / D2H) | 24.4-25.9 / 20.8-21.4 | 25.9-26.1 / 25.5-26.4 |
| Copy through host memory (GB/s) | 5.9-11.3 | (peer access off: 5.9-11.3) |
| P2P copy, x16 pairs, same / cross socket | - | 26.4 / 22.7 (bidirectional 51.2 / 41.9) |
| P2P copy, pairs with an x8 GPU | - | 13.2 (bidirectional 25.3) |
| NCCL 7 GPUs | 1.5-1.6 | 12.6 |
| NCCL GPU0-3 / GPU4-6 | 3.6 / 3.3 | 12.7 / 12.7 |
| NCCL pair 0,1 (both x16) | 3.6 | 24.1-24.3 |
| NCCL pair 4,5 | 3.6 | 12.6 |
| NCCL cross-socket pairs 0,4 / 3,6 | 1.3 / 1.6 | 11.9-12.0 / 12.0 |

Every set except pair 0,1 contains an x8 GPU and stops at about 12.6 GB/s; the x16 pair matches Node 4 (same CPU family and GPU). Without P2P, cross-socket all-reduce runs at 1.3-1.6 GB/s, about the speed of 10 GbE.

### GPU Node 2: RTX 4090 24 GB, EPYC 7402 (Rome)

8x RTX 4090 24 GB, 512 GB, Ubuntu 24.04, kernel 6.8.0-146-generic (modules built for it from the same branch). All GPUs at x16. Baseline: stock open driver 610.57.04, default IOMMU mode. Static BAR1 on (8 GiB held raised "BAR1 Used" to 8587 MiB).

| | Stock | P2P |
| :--- | ---: | ---: |
| Host<->GPU (GB/s, H2D / D2H) | 24.5-26.0 / 21.2-22.7 | 25.8-26.1 / 25.3-26.1 |
| Copy, same socket (GB/s) | 12.0-22.3 (staged) | 26.3-26.4 (bidirectional 51.0-51.3) |
| Copy, cross socket | 12.7-22.6 (staged) | 21.8-22.7 (bidirectional 40.9-42.2) |
| NCCL 8 GPUs | 1.3 | 20.6 |
| NCCL GPU0-3 / GPU4-7 | 3.9 / 3.9 | 25.1 / 25.2 |
| NCCL pairs 0,1 / 4,5 | 3.4 / 3.4 | 24.5-24.8 / 24.8-24.9 |
| NCCL cross-socket pairs 0,4 / 3,7 | 1.2 / 1.3 | 19.2-19.4 / 19.3-19.4 |

Rome's cross-socket P2P is symmetric (unlike Milan), but slower than within a socket.

### GPU Node 3: RTX 3090, EPYC 7402 (Rome)

8x RTX 3090 24 GB, 512 GB, kernel 6.8.0-107-generic. All GPUs at x16. Baseline: stock closed driver 590.48.01. Static BAR1 on (8 GiB held raised "BAR1 Used" to 8457 MiB). Service run: `OVERALL: PASS`, kernel log clean.

| | Stock | P2P |
| :--- | ---: | ---: |
| Host<->GPU (GB/s, H2D / D2H) | 24.4-26.0 / 19.9-23.1 | 25.8-26.1 / 24.8-26.1 |
| Copy, same socket (GB/s) | 7.6-11.3 (staged) | 24.6-26.4 (bidirectional 50.1-51.2) |
| Copy, cross socket | 8.0-11.2 (staged) | 21.5-22.7 (bidirectional 40.4-42.1) |
| NCCL 8 GPUs | 1.6 | 20.1 |
| NCCL GPU0-3 / GPU4-7 | 4.0 / 4.0 | 24.6 / 24.7 |
| NCCL pairs 0,1 / 4,5 | 3.6 / 3.6 | 24.0-24.1 / 24.0-24.3 |
| NCCL cross-socket pairs 0,4 / 3,7 | 1.4 / 1.6 | 18.3 / 18.3 |

Gains: 12.6x for 8 GPUs, about 6x within a socket, 11-13x across sockets. NCCL matches Node 4 (same hardware) within 0.2 GB/s.

### GPU Node 4: RTX 3090, EPYC 7402 (Rome)

8x RTX 3090 24 GB, 512 GB, kernel 6.8.0-138-generic. All GPUs at x16. The closed driver 590 ran before the patch, so there is no stock NCCL baseline; the host-staged copies are the same run with peer access off. Static BAR1 on (BAR1 resized from 256 MiB to 32 GiB; 8 GiB held raised "BAR1 Used" to 8457 MiB).

| | Host staged | P2P |
| :--- | ---: | ---: |
| Host<->GPU (GB/s, H2D / D2H) | | 25.8-26.1 / 25.4-26.1 |
| Copy, same socket (GB/s) | 7.8-11.2 | 26.3-26.4 (bidirectional 51.0-51.3) |
| Copy, cross socket | 8.1-11.3 | 21.9-22.7 (bidirectional 41.1-42.2) |
| NCCL 8 GPUs | | 20.1 |
| NCCL GPU0-3 / GPU4-7 | | 24.7 / 24.7 |
| NCCL pairs 0,1 / 4,5 | | 24.1 / 24.1-24.2 |
| NCCL cross-socket pairs 0,4 / 3,7 | | 18.2 / 18.4 |

Within 0.5-1 GB/s of the RTX 4090 on the same platform (Node 2). Stock baseline on the same hardware: Node 3.

### GPU Node 5: RTX 4090 24 GB, EPYC 7543 (Milan)

8x RTX 4090 24 GB, 512 GB, kernel 6.5.0-25. GPU1 and GPU4 at x8. Baseline: stock open driver 610.57.04. Static BAR1 on (8 GiB held raised "BAR1 Used" from 1 to 8653 MiB).

| | Stock | P2P |
| :--- | ---: | ---: |
| P2P copy, same socket (GB/s) | - | 26.3 |
| P2P copy, cross socket | - | socket 0 -> 1 about 22, 1 -> 0 about 10 |
| NCCL 8 GPUs | 0.8 | 12.9 |
| NCCL GPU0-3 / GPU4-7 | 4.6 / 4.5 | 12.9 / 12.9 |
| NCCL pair 2,3 (x16) | 4.5 | 25.1 |
| NCCL pairs 0,1 / 4,5 (x8 GPU) | 4.4 / 4.0 | 12.8 / 12.8 |
| NCCL cross-socket pairs 0,4 / 3,7 | 0.7 / 0.8 | 12.4 / 19.2 |

#### Opt-in (`uvm_bar1_p2p_managed=1`)

Same node, in an [opt-in window](../scripts/gpu-p2p/README.md#managed-memory-and-the-uvm-bar1-fix). UVM's peer info showed link type `UVM_GPU_LINK_PCIE_BAR1`, aperture `SYS_NON_COHERENT`. With the debug UVM build (asserts on): the page-table path alone on one pair, the copy-engine path on one pair, then fault, prefetch and memcpy on all 56 pairs and accessedby and atomic on all pairs (quick size). With the release build: accessedby on all pairs, then the full service run. All passed with a clean kernel log; NCCL numbers were unchanged. Not covered: `uvm_peer_copy=virt` and the speed of managed migrations.

#### Before the fixes

A run with plain aikitoria v3 hit Xid 31 `FAULT_UNSUPPORTED_APERTURE` during managed-page migration, then Xid 154 on all GPUs; it needed a reboot.

### GPU Node 6: RTX 4090 24 GB, EPYC 9554 (Genoa)

8x RTX 4090 24 GB, 1.5 TiB, kernel 6.5.0-25. GPU6 at x8. No stock baseline; the host-staged copies are the same run with peer access off. Static BAR1 on.

| | Host staged | P2P |
| :--- | ---: | ---: |
| Copy per x16 pair, any direction (GB/s) | 22.0-22.6 | 26.3-26.4 (bidirectional 52.1) |
| Copy, pairs with GPU6 | 12.9-13.0 | 13.2 |
| NCCL 8 GPUs / GPU4-7 (with GPU6) | | 13.0 / 13.0 |
| NCCL GPU0-3 | | 25.6 |
| NCCL pairs 0,1 / 4,5 | | 25.2 / 25.1 |
| NCCL cross-socket pairs 0,4 / 3,7 | | 23.5 / 23.6 |

### GPU Node 7: RTX 4090 48 GB, EPYC 9554 (Genoa)

8x RTX 4090 48 GB (modded, VBIOS 95.02.3C.00.02, BAR1 32 GiB), ASUS ESC8000A-E12, 1.5 TiB, kernel 6.5.0-25, one GPU per root port. GPU1 at x8. Baseline: stock closed driver 590.48.01. Dynamic BAR1 (Method 3). Service run with 38 GiB resident per GPU; managed memory stages through host memory on dynamic pairs.

| | Stock (SHM) | P2P, NCCL 2.20 | P2P, NCCL 2.27 + `NCCL_P2P_LEVEL=SYS` |
| :--- | ---: | ---: | ---: |
| Copy per pair without GPU1 (GB/s) | up to 22.7-23.4 (staged) | 26.3-26.4 | |
| NCCL 8 GPUs (with GPU1) | 10.6 | 13.0 | 13.0 |
| NCCL GPU4-7 | 21.1 | 25.7 | 25.7 |
| NCCL pairs 2,3 / 4,5 | 18.5 / 18.3 | 25.5 / 25.0 | 25.1 / 25.2 |
| NCCL cross-socket pairs 0,4 / 3,7 | 8.7 / 16.4 | 23.4 / 23.6 | 23.5 / 23.6 |

- NCCL 2.27 without `NCCL_P2P_LEVEL=SYS` used P2P only for pairs; groups stayed on SHM (8 GPUs 10.2).
- Pair 0,4 in the stock column had GPU0 at x8 in that boot.
- Plain aikitoria v3 (before Method 3) reported P2P but failed at the first peer mapping (`mapping of buffer object failed`); every NCCL run with P2P crashed.
- HMM evidence: before `uvm_disable_hmm=1`, `/proc/iomem` showed 8 `nvidia-uvm-hmm` regions just below 2^52, cuMem host allocations came from ZONE_DMA32, and NCCL 2.27 crashed on 8 GPUs; after the option and a reboot the same run passed.

### Comparison with duanyll's blog

| | Blog, Xeon 4416+ | Blog, EPYC 7302 (Rome) | Ours, EPYC 7402 (Rome), Node 2 | Ours, EPYC 9554 (Genoa), Nodes 6 and 7, all x16 |
| :--- | :--- | :--- | :--- | :--- |
| Cards | 8x RTX 4090 48 GB | 4x RTX 4090 48 GB | 8x RTX 4090 24 GB | RTX 4090 24 GB and 48 GB |
| P2P copy per pair | 22.7 | 26.3 | 26.4 (cross socket 22.4) | 26.4 |
| NCCL 4 GPUs, without -> with P2P | 16.8 -> 20.4 | 4.1 -> 25.15 | 3.9 -> 25.1-25.2 | 20.4-21.1 -> 25.6-25.7 |
| NCCL 8 GPUs, without -> with P2P | 14.2 -> 20.46 | - | 1.3 -> 20.6 | about 16 -> about 24-25 (estimated) |

The tools differ (blog: `cudaMemcpyPeer` and nccl-tests; ours: `p2ptest.cu` and `torchrun`), so compare trends, not decimals. Node 2 reproduces the blog's Rome result for four GPUs. Every AMD platform reaches the Gen4 x16 line rate per pair within a socket.

## Appendix B: References

- tinygrad P2P modules: <https://github.com/tinygrad/open-gpu-kernel-modules>
- aikitoria P2P branches: <https://github.com/aikitoria/open-gpu-kernel-modules>
- duanyll Method 3 fork: <https://github.com/duanyll/open-gpu-kernel-modules> (branches `*-p2p-48g`)
- duanyll blog, "48G 4090 P2P" (Chinese): <https://github.com/duanyll/duanyll.com-hexo/blob/master/source/_posts/tech/2026-7-13-4090-48G-P2P.md>
- Our fork: <https://github.com/LingzheZhao/open-gpu-kernel-modules>, branches [`610.57.04-p2p-48g`](https://github.com/LingzheZhao/open-gpu-kernel-modules/tree/610.57.04-p2p-48g) and [`610.57.04-p2p-fixes`](https://github.com/LingzheZhao/open-gpu-kernel-modules/tree/610.57.04-p2p-fixes)
- NCCL SHM/cuMem host bug: <https://github.com/NVIDIA/nccl/issues/2190>, proposed fix <https://github.com/NVIDIA/nccl/pull/2388>
- Scripts and tests: [`scripts/gpu-p2p/`](../scripts/gpu-p2p/README.md)
