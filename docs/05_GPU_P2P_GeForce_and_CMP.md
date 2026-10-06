# GPU P2P on GeForce and CMP

## Contents

- [GPU P2P on GeForce and CMP](#gpu-p2p-on-geforce-and-cmp)
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
  - [CMP 170HX](#cmp-170hx)
    - [How BAR1 P2P works on the CMP 170HX](#how-bar1-p2p-works-on-the-cmp-170hx)
    - [ACS redirect behind PCIe switches](#acs-redirect-behind-pcie-switches)
  - [CMP 170HX: prerequisites](#cmp-170hx-prerequisites)
  - [CMP 170HX: procedure](#cmp-170hx-procedure)
    - [1. Stop the GPU workloads](#1-stop-the-gpu-workloads)
    - [2. Save the running module tree](#2-save-the-running-module-tree)
    - [3. Build and install cmpunlocker](#3-build-and-install-cmpunlocker)
    - [4. Check and save the new tree](#4-check-and-save-the-new-tree)
    - [5. Cold power cycle](#5-cold-power-cycle)
    - [6. Verify the boot](#6-verify-the-boot)
    - [7. Turn ACS redirect off and set NCCL](#7-turn-acs-redirect-off-and-set-nccl)
    - [8. Run the P2P tests](#8-run-the-p2p-tests)
  - [CMP 170HX: using P2P in jobs](#cmp-170hx-using-p2p-in-jobs)
  - [CMP 170HX: pitfalls](#cmp-170hx-pitfalls)
  - [CMP 170HX: rollback](#cmp-170hx-rollback)
  - [Appendix A: Measurements](#appendix-a-measurements)
  - [Appendix B: References](#appendix-b-references)

## Introduction

GeForce drivers disable PCIe peer-to-peer (P2P) between GPUs, so CUDA peer copies and NCCL collectives are staged through host memory. A patched build of the kernel modules of NVIDIA's **open** driver re-enables P2P on the RTX 3090 (Ampere) and RTX 4090 (Ada, including the modded 48 GB card): `cudaDeviceCanAccessPeer` returns true and NCCL uses its P2P transport. Userspace (libcuda, NCCL, PyTorch) stays unchanged. This is an **unofficial community patch** (tinygrad, then aikitoria's branches per driver version, then duanyll's "Method 3" for 48 GB cards; links in [Appendix B](#appendix-b-references)), not supported by NVIDIA.

The CMP 170HX, a mining card with the A100's GA100 chip, has no P2P either. **cmpunlocker**, another community patch set for the same open kernel modules, unlocks its memory, SMs and PCIe Gen2; BAR1 P2P for it comes from bayley's cmpunlocker and our fork (links in [Appendix B](#appendix-b-references)).

This guide covers P2P over PCIe BAR1 on the four card types of this cluster listed below; other GeForce and CMP models were not tested. "24 GB cards" means the RTX 3090 and the RTX 4090 24 GB, "48 GB cards" the modded RTX 4090 48 GB.

We deploy one branch of our fork on every node with GeForce cards: [`610.57.04-p2p-48g`](https://github.com/LingzheZhao/open-gpu-kernel-modules/tree/610.57.04-p2p-48g). It works for all three GeForce card types; the Method 3 code stays dormant where VRAM fits into BAR1. The CMP 170HX node runs the branch [`g292/p2p-bar1-v0.5`](https://github.com/LingzheZhao/cmpunlocker/tree/g292/p2p-bar1-v0.5) of our cmpunlocker fork: upstream cmpunlocker v0.5 with BAR1 P2P.

The sections from [How it works](#how-it-works) to [Rollback](#rollback) cover the GeForce cards. The CMP 170HX needs its own kernel, installer and PCIe settings; its sections start at [CMP 170HX](#cmp-170hx).

| Card | Architecture | VRAM | Max BAR1 | P2P path | Nodes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| RTX 3090 | Ampere (`sm_86`) | 24 GB | 32 GiB | static BAR1: all of VRAM is mapped once | 1, 3, 4 |
| RTX 4090 24 GB | Ada (`sm_89`) | 24 GB | 32 GiB | static BAR1: all of VRAM is mapped once | 2, 5, 6 |
| RTX 4090 48 GB (modded) | Ada (`sm_89`) | 48 GiB | 32 GiB | dynamic BAR1: each shared allocation is mapped on demand | 7 |
| CMP 170HX | Ampere GA100 (`sm_80`) | 64 GiB with cmpunlocker (8 GB stock) | 64 GiB with the kernel patches | static BAR1, forced: all of VRAM is mapped once | g292 |

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

The CMP 170HX node has one socket and its GPUs in pairs behind PCIe switches, so its sets differ. Same quantity, measured with nccl-tests: cmpunlocker without P2P -> BAR1 P2P with ACS redirect off and `NCCL_P2P_LEVEL=SYS`. Details: [Appendix A](#g292-cmp-170hx-epyc-7j13-milan).

| Platform and card | 8 GPUs | 4 GPUs, two switches | 4 GPUs, one per switch | Pair, one switch | Pair, two switches |
| :--- | :--- | :--- | :--- | :--- | :--- |
| EPYC 7J13 (Milan), CMP 170HX, all GPUs at PCIe Gen2 x16 | 2.17 -> 6.38 | 1.93 -> 6.36 | 4.19 -> 6.34 | 1.70 -> 6.29 | 3.00 -> 6.21 |

- **PCIe Gen2 bounds the CMP 170HX.** With P2P every set runs at 6.2-6.4 GB/s, about 80% of the 8 GB/s a Gen2 x16 link carries per direction, and 1.5-3.7x the host-staged numbers.
- **Behind PCIe switches, ACS redirect must be off.** With the kernel's default, traffic between the two GPUs of a switch goes up to the root port and back down, and the 8-GPU ring drops to 0.47 GB/s, below host staging ([why](#acs-redirect-behind-pcie-switches)).
- **NCCL needs `NCCL_P2P_LEVEL=SYS` there as well.** Its defaults stage groups of four and eight GPUs through host memory between root ports (2.3-4.1 GB/s).

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

PCIe ACS redirect forces peer traffic through the root complex. With `iommu=pt` and one GPU per root port it did not limit P2P on our GeForce nodes; behind PCIe switches it does ([CMP 170HX](#acs-redirect-behind-pcie-switches)). If P2P bandwidth is far below host<->GPU bandwidth, `sudo scripts/gpu-p2p/acs-redir.sh status|off|restore` clears it until the next reboot (only with `iommu=pt`). To keep it off, `gpu-acs-redir-off.service` runs `off` at every boot ([script README](../scripts/gpu-p2p/README.md#acs)). `pci=disable_acs_redir=pci:<vendor>:<device>` does the same from the kernel command line when the bridges above the GPUs share one ID that no other device has (`acs-redir.sh status` says so).

### PCIe link width

A GPU trained at x8 gets about 13 instead of 26 GB/s (PCIe Gen4) to the host and to peers, and caps every NCCL ring through it. The width can change between boots; `run_host.sh` records it. Fix it in hardware (seating, riser, slot).

### Kernel and driver upgrades

- The driver packages are held: upgrading the userspace driver alone gives `Driver/library version mismatch`.
- After a kernel upgrade the node boots the stock modules: safe, but without P2P until the modules are rebuilt and installed for the new kernel.
- A driver upgrade needs a fork branch for the new version: run `--restore` (below), install the new driver, build, install, reboot and verify.
- Never upgrade kernel or driver while the node runs tasks ([Maintainance](01_First-time_Setup_of_Cluster_Nodes.md#maintainance-upgrade-apt-packages--determined-ai)).

## Rollback

1. Disable the node ([step 1](#1-disable-the-node-in-determined)).
2. **root**: `sudo K=<K> scripts/gpu-p2p/install-p2p-modules.sh --restore`. It removes the modules and the override and, unless other kernels still have P2P modules, the GRUB parameters and package holds it added. HMM stays off while `iommu=pt` stays. Details and the manual equivalent: [script README](../scripts/gpu-p2p/README.md#roll-back). Remove a `pci=disable_acs_redir=...` parameter or `gpu-acs-redir-off.service` by hand ([ACS](../scripts/gpu-p2p/README.md#acs)).
3. **root**: reboot. `nvidia-smi topo -p2p r` shows `GNS` again and BAR1 is back at 256 MiB.
4. Enable the node.

## CMP 170HX

Node g292 has eight CMP 170HX (GA100, PCI ID `10de:20c2`) and one AMD EPYC 7J13 (one NUMA node). The GPUs come in four pairs; each pair sits behind a Switchtec PCIe switch (`11f8:4052`) on its own root port (`1022:1483`). Without the P2P patches the driver reports `GNS` for every pair, and NCCL stages through host memory.

The node runs NVIDIA's open driver 610.57.04 with kernel modules built by cmpunlocker. It unlocks 64 GiB of memory, more SMs and PCIe Gen2 (the cards come up at Gen1), and our branch adds BAR1 P2P. Every link runs at Gen2 x16.

Placeholders in these sections: `<K>` the `-cmp` kernel release (`uname -r`, e.g. `6.8.12-cmp`), `<gpus>` number of GPUs, `<old-label>` and `<new-label>` names of saved module trees, `<sha256>` hash of a build's `nvidia.ko`, `<label>` log name, `<image>` CUDA + PyTorch image, `<bmc>` and `<user>` the node's BMC address and user.

### How BAR1 P2P works on the CMP 170HX

The principle is the static BAR1 of the 24 GB GeForce cards ([How it works](#how-it-works)): a peer GPU reads and writes `BAR1 bus address + offset` over PCIe, through the IOMMU in passthrough mode. cmpunlocker's P2P patches add what the CMP 170HX needs:

- **P2P caps forced.** The GPU's firmware (GSP) reports no P2P support for the CMP 170HX. The driver overrides this on the CPU side and switches GA100 to the BAR1 P2P code that the stock driver uses only from Hopper on.
- **Static BAR1 of the whole memory.** The driver's automatic sizing refuses a static BAR1 on these cards, so cmpunlocker forces it (static BAR1 `ENABLE`) for the CMP device IDs. The map starts at offset 0 of the 64 GiB BAR1 and covers the whole client framebuffer (`0xfe8e00000` bytes, about 63.6 GiB). The remaining BAR1, about 370 MiB, is all the driver has left for its dynamic BAR1 mappings.
- **P2P type BAR1 from the start.** cmpunlocker sets the BAR1 P2P type when the driver constructs KernelBus (`kbusInitRegistryOverrides`), not in KernelBif: KernelBif is constructed before the PCI device ID is read, so a device-ID test there never matches. If the type stays `MAILBOX`, the driver reserves the P2P mailbox at the bottom of BAR1. The static map then moves to offset 512 MiB, where the client framebuffer no longer fits (its top 144 MiB stay unmapped), and the BAR1 P2P caps are off, so every pair reports P2P as not supported. The driver, `nv_gpu_ops` and UVM add the static offset themselves; offset 0 is about covering the whole framebuffer. A good boot logs `static BAR1 mapped offset 0x0` with the full size on every GPU.
- **No mailbox fallback.** When BAR1 P2P is not possible for a pair, the driver reports the pair as not supported and logs `CMPUNLOCK_BAR1P2P: BAR1 P2P unavailable for gpuMask ...`. Mailbox P2P moved wrong data on these cards.
- **64 GiB BAR1 from the kernel.** BAR1 is 64 MiB as shipped. Resizing it in the driver comes too late: the bridge windows are sized at enumeration and cannot grow. Two host-kernel patches (cmpunlocker `kernel-patches/`, `linux-6.8/` for 6.8) fix this. 0001 makes the kernel budget the alignment padding of bridge windows; without it some GPUs silently get no BAR1. 0002 is an early PCI quirk that programs the BAR1 Resizable BAR capability to 64 GB before enumeration. `install.sh` adds `pci=realloc pci=hpmmioprefsize=2T` to place the windows.

### ACS redirect behind PCIe switches

PCIe ACS (Access Control Services) Request and Completion Redirect on a switch's downstream ports send peer traffic up to the root complex instead of across the switch. Linux turns redirect on whenever an IOMMU driver is active, also with `iommu=pt`, so a BIOS setting does not stick. With redirect on, P2P between the two GPUs of one switch goes up to the root port and back down the same uplink. A pair alone barely notices (6.22 against 6.29 GB/s), but rings that include both GPUs of a switch do: four GPUs reach 4.29 GB/s and eight GPUs 0.47 GB/s, against 6.36 and 6.38 with redirect off ([Appendix A](#g292-cmp-170hx-epyc-7j13-milan)).

The CMP node therefore has two settings ([step 7](#7-turn-acs-redirect-off-and-set-nccl)):

| Setting | What it does |
| :--- | :--- |
| `gpu-acs-redir-off.service` | Runs `acs-redir.sh off` at every boot, after the kernel modules load and before docker starts. It clears the redirect bits on exactly the bridges between the GPUs and the root complex (16 on g292: four root ports and twelve switch ports) and saves the old values in `/run/acs-redir-saved.txt`; stopping the unit restores them. |
| `/etc/nccl.conf` with `NCCL_P2P_LEVEL=SYS` | NCCL's defaults use P2P for pairs but stage groups of four and eight GPUs through host memory between root ports, also with redirect off (2.3-4.1 GB/s instead of about 6.3). |

Use them only with `iommu=pt` and with no GPU of the node passed through to a VM:

- With a translating IOMMU, peer requests carry I/O virtual addresses, which a switch could route, untranslated, to the wrong device. `acs-redir.sh off` refuses unless every GPU is in an identity IOMMU domain.
- With redirect off, the two GPUs of a switch reach each other without passing the IOMMU, while their IOMMU groups (formed at boot with redirect on) still look separate. Disable the unit before giving any of these GPUs to a VM.

The kernel parameter `pci=disable_acs_redir=` does the same at boot, but the unit is the better choice here:

- The bridges have two IDs, so the list needs a `;` (`pci:1022:1483;pci:11f8:4052`). GRUB's `10_linux` writes the parameters unquoted into the `linux` line, so GRUB ends the command at the `;` and the kernel gets only the first ID.
- An ID matches every device with that ID: on g292 all ten `1022:1483` root ports, including those of the NICs, NVMe drives, USB and the BMC.
- A list of bus addresses instead has 16 entries tied to bus numbers.
- It needs a reboot, and it merges the IOMMU groups of the two GPUs behind a switch (which matters only for passthrough).

## CMP 170HX: prerequisites

- [ ] **Kernel** `<K>` built with cmpunlocker's `kernel-patches/` (Ubuntu's HWE 6.8 source with `linux-6.8/` and `LOCALVERSION=-cmp` gives `6.8.12-cmp`; build steps in the README there). The stock kernel stays installed as the GRUB fallback, GRUB shows its menu (a visible timeout), and the kernel packages are held. `dmesg | grep 'CMP 170HX'` shows `BAR1 REBAR programmed to 64GB` once per GPU, and `sudo lspci -vv -d 10de:` shows `Region 1` with `[size=64G]` on every GPU, ending below 2^47 (the GPU DMA mask is 47 bits).
- [ ] **Kernel build tree**: `/lib/modules/<K>/build` points to the configured source tree of `<K>`; `install.sh` builds against it.
- [ ] **BIOS**: Above 4G Decoding on; the high MMIO window must fit a 64 GiB BAR1 per GPU plus the alignment padding of the switch windows.
- [ ] **Driver**: Ubuntu's `nvidia-driver-610-open` at 610.57.04, installed and running (`install.sh` reads the version from `/proc/driver/nvidia/version`). cmpunlocker replaces only the kernel modules.
- [ ] **Build tools**: `gcc-12` or newer, `python3-yaml`, `rsync`, and NVIDIA's open-gpu-kernel-modules source of the driver version (`install.sh` downloads it, or place the tarball in `driver/.build/`).
- [ ] **`iommu=pt`** on the kernel command line (`install.sh` adds `amd_iommu=on iommu=pt`).
- [ ] **Out-of-band power**: the node's BMC (IPMI power control and KVM console) or someone at the machine. Every driver change ends with a cold power cycle, and a failed boot is recovered from the GRUB menu.
- [ ] **No VM passthrough** of these GPUs while ACS redirect is off.
- [ ] **Maintenance window**: the GPU workloads stop for the whole procedure.

## CMP 170HX: procedure

Scripts: `scripts/gpu-p2p/cmp/` and `scripts/gpu-p2p/` ([script README](../scripts/gpu-p2p/README.md#cmp-170hx)). Steps marked **root** need `sudo`. `install-p2p-modules.sh` and `verify.sh` are for the GeForce modules and do not apply here.

### 1. Stop the GPU workloads

Stop every process on the GPUs: `nvidia-smi --query-compute-apps=pid --format=csv,noheader` must print nothing (the test scripts refuse otherwise). Record the baseline of the running build:

```bash
scripts/gpu-p2p/cmp/gpu-baseline.sh <image> baseline-before.txt   # SM count, memory, device-to-device copy per GPU
```

### 2. Save the running module tree

**root**:

```bash
sudo scripts/gpu-p2p/cmp/trees.sh save <old-label>
```

It copies `/lib/modules/<K>/updates/cmpunlocker`, the initramfs, `/etc/modprobe.d`, `/etc/default/grub` and the cmpunlocker units to `/root/cmpunlocker-trees/<old-label>` and checks the copy. `sudo scripts/gpu-p2p/cmp/trees.sh show` lists the saved trees with the sha256 of their `nvidia.ko`.

### 3. Build and install cmpunlocker

**root**:

```bash
git clone -b g292/p2p-bar1-v0.5 https://github.com/LingzheZhao/cmpunlocker.git
cd cmpunlocker
echo 'options nvidia-uvm uvm_disable_hmm=1' | sudo tee /etc/modprobe.d/cmpunlocker-uvm.conf
sudo ./install.sh --no-ecc
```

- `uvm_disable_hmm=1` avoids the open driver's HMM bug under `iommu=pt` ([HMM](#hmm-breaks-host-cumem-allocations-under-iommu-passthrough)). Set it before `install.sh`, which rebuilds the initramfs.
- `--no-ecc` leaves out the ECC patches ([why](#ecc-stays-off)). BAR1 P2P is on by default.
- The installer patches and builds the modules, installs them into `/lib/modules/<K>/updates/cmpunlocker`, rebuilds the initramfs and leaves the running driver alone.

Its log must show, in this order: `ECC patches left out`, `BAR1 P2P patches enabled`, `All patches applied`, both self-tests passing, `contains the cmpunlocker safety-v4 provenance marker`, `nvidia resolves to /lib/modules/<K>/updates/cmpunlocker/nvidia.ko` and `running NVIDIA driver was left untouched`. If it stops before `Modules built`, no module was replaced: run `sudo scripts/gpu-p2p/cmp/trees.sh activate <old-label>` and stop here.

### 4. Check and save the new tree

```bash
M=/lib/modules/<K>/updates/cmpunlocker/nvidia.ko
grep -ac CMPUNLOCK_BAR1P2P $M                                     # above 0: the P2P patches are in
sha256sum $M                                                       # <sha256>: identifies this build
lsinitramfs /boot/initrd.img-<K> | grep cmpunlocker-uvm.conf       # the HMM setting is in the initramfs
sudo scripts/gpu-p2p/cmp/trees.sh save <new-label>
sudo scripts/gpu-p2p/cmp/trees.sh activate <old-label>             # rehearse the rollback
sudo scripts/gpu-p2p/cmp/trees.sh activate <new-label>
```

Each `activate` prints the sha256 of the tree it made live; the last one must print `<sha256>`. `srcversion` does not tell cmpunlocker builds apart ([why](#srcversion-does-not-identify-a-build)).

### 5. Cold power cycle

**root**: `sudo shutdown -h now`. Once the BMC reports the power off (`ipmitool -I lanplus -H <bmc> -U <user> -E chassis power status`), wait at least two minutes, then power on (`... chassis power on`, the BMC web interface or the power button). POST takes a few minutes.

Never `reboot` and never unload or reload the NVIDIA modules after a driver change ([why](#cold-power-cycles-only)).

### 6. Verify the boot

Unprivileged (the kernel log needs the `adm` or `systemd-journal` group):

```bash
nvidia-modprobe -u -c 0                                  # loads nvidia_uvm for the HMM check
scripts/gpu-p2p/cmp/verify-boot.sh <gpus> <sha256>
```

It must end with `RESULT rc=0`. A `HARD-STOP` line means [roll back](#cmp-170hx-rollback); a `FAIL` line keeps the workloads off until it is understood. One exception: when fewer than `<gpus>` GPUs enumerate, cold power-cycle again before anything else (a boot with seven of the eight GPUs has happened on g292).

| Check | Expected on every GPU |
| :--- | :--- |
| PCI | `<gpus>` devices `10de:20c2`; kernel log `CMP 170HX: BAR1 REBAR programmed to 64GB` |
| Module | `nvidia` resolves to `updates/cmpunlocker/nvidia.ko` with sha256 `<sha256>`; no `RmInitAdapter failed`, no Xid |
| Unlock | `SEC2_DEBUG_FB_LAYOUT: validated ... status=safe build=cmpunlocker-safety-v4`, the same for `SEC2_DEBUG_PMA_GUARD`; `SM-RECONFIG start` |
| Static BAR1 | `CMPUNLOCK_BAR1P2P: forcing static BAR1 ENABLE (devId=0x20c2)` and `CMPUNLOCK_BAR1P2P: static BAR1 mapped offset 0x0 size 0xfe8e00000`; no `clamping static BAR1`, `static BAR1 mapping failed` or `beyond mappable` line |
| P2P caps | `CMPUNLOCK_BAR1P2P: GSP P2P caps forced to OK`; no `BAR1 P2P unavailable for gpuMask` line; `nvidia-smi topo -p2p r` `OK` for all ordered pairs (56 with 8 GPUs) |
| `nvidia-smi` | memory.total 65536 MiB, BAR1 Total 65536 MiB, PCIe Gen2 x16, ECC `[N/A]` |
| UVM | `/sys/module/nvidia_uvm/parameters/uvm_disable_hmm` is `Y` |

Then compare with the baseline of step 1:

```bash
scripts/gpu-p2p/cmp/gpu-baseline.sh <image> baseline-after.txt
```

Same SM count per GPU, total and free memory within a few MiB, device-to-device bandwidth within 5%.

### 7. Turn ACS redirect off and set NCCL

**root**, once per node ([why](#acs-redirect-behind-pcie-switches)); the unit then runs at every boot:

```bash
sudo install -m 755 scripts/gpu-p2p/acs-redir.sh /usr/local/sbin/
sudo install -m 644 scripts/gpu-p2p/gpu-acs-redir-off.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-acs-redir-off.service
echo NCCL_P2P_LEVEL=SYS | sudo tee /etc/nccl.conf
sudo /usr/local/sbin/acs-redir.sh status        # RR=0 CR=0 on every bridge
```

After the next boot, check that `sudo /usr/local/sbin/acs-redir.sh status` still shows RR=0 CR=0 on every bridge.

### 8. Run the P2P tests

Unprivileged, on the idle node; the GPU sets are those of g292 (a switch pair, a pair across switches, two switch pairs, one GPU per switch, all eight):

```bash
scripts/gpu-p2p/cmp/p2p-copy-check.sh <image> copy-check.txt
cd scripts/gpu-p2p/tests
export OUT_DIR=~/p2p-logs
STAGES="p2ptest ordering stale hostnuma" ./run_host.sh <image> <label>
STAGES=nccl GPU_SETS="0,1 0,2 0,1,2,3 0,2,4,6 0,1,2,3,4,5,6,7" ./run_host.sh <image> <label>-nccl
journalctl -k -b | grep -c 'NVRM: Xid'                              # 0
```

- `p2p-copy-check.sh` checks peer access on every ordered pair and copies a 256 MiB random block to every peer and back. Then it fills nearly all free memory of each GPU from a peer in 512 MiB blocks and reads them back, which reaches the top of the memory that a truncated static BAR1 would miss. Pass: `RESULT ok`.
- The `run_host.sh` stages ([script README](../scripts/gpu-p2p/README.md#test)) check peer copies and their integrity on every pair, write ordering, stale reads and host memory. Pass: `OVERALL: PASS` and `KERNEL LOG: PASS`.
- NCCL pass: every all-reduce `correct=True`, and `via P2P` for `p2p-sys` on every set. The `default` runs may stage groups through host memory between root ports; that is why `/etc/nccl.conf` sets `NCCL_P2P_LEVEL=SYS`. Bandwidths for comparison: [Appendix A](#g292-cmp-170hx-epyc-7j13-milan).

Then start the workloads again and tell the users of the node about [the NCCL setting](#cmp-170hx-using-p2p-in-jobs).

## CMP 170HX: using P2P in jobs

`/etc/nccl.conf` sets `NCCL_P2P_LEVEL=SYS` for NCCL processes on the host. A container sees it only when it is mounted, or with the variable set directly:

```bash
docker run ... -v /etc/nccl.conf:/etc/nccl.conf:ro ...      # or: -e NCCL_P2P_LEVEL=SYS
```

With `NCCL_DEBUG=INFO`, NCCL prints `NCCL_P2P_LEVEL set by environment to SYS` (also when the value comes from the file) and `via P2P` for its connections; `via SHM` is host staging. Without the setting, groups of four and eight GPUs run at 2.3-4.1 instead of about 6.3 GB/s. Managed memory across GPUs is not tested ([below](#managed-memory-across-cmp-gpus)).

## CMP 170HX: pitfalls

### Cold power cycles only

cmpunlocker changes the memory geometry and the GPU's protected-memory (WPR) setup when the driver boots the GPUs. Unloading and reloading the modules on a running system can hang it, and a warm reboot starts the driver on GPUs still in that state. After every driver change: `shutdown -h`, power off, power on. cmpunlocker's default passthrough setup also blocks function-level and bus resets of the cards, so a GPU that needs a reset after an Xid needs a cold power cycle as well.

### srcversion does not identify a build

`modinfo -F srcversion` and `/sys/module/nvidia/srcversion` stay the same across cmpunlocker builds. Identify a build by the sha256 of its `nvidia.ko` (`trees.sh show`, `verify-boot.sh <gpus> <sha256>`).

### No PCIe error reporting

The platform's firmware does not hand AER (or DPC) to the OS, so PCIe errors on peer traffic are not logged. The data checks (`p2p-copy-check.sh`, `p2ptest`) are the evidence that P2P moves correct data.

### No P2P registry keys

Never put `RmForceP2P`, `RMForceStaticBar1` or `RMPcieP2PType` into `NVreg_RegistryDwords`: GSP rejects them (`NV_ERR_INVALID_REGISTRY_KEY`) and leaves its protected memory region up, and only a cold power off recovers. `ForceP2P=0x11` hangs the machine. cmpunlocker sets what P2P needs inside the driver.

### ECC stays off

Install with `--no-ecc`; `nvidia-smi` then shows ECC `[N/A]`. With the ECC patches, `nvidia-smi` reports ECC enabled with zero errors on every CMP 170HX: the driver rewrites the ECC report by PCI device ID, whatever the hardware does. Nothing the driver reports shows whether the DRAM ECC setting takes effect, and there is no page retirement.

### Managed memory across CMP GPUs

Cross-GPU managed memory (`cudaMallocManaged`) is not tested on the CMP 170HX, and cmpunlocker has no `uvm_bar1_p2p_managed` gate ([GeForce](#managed-memory-uvm)). The `managedtest` stage can fault all GPUs (Xid 31, then Xid 154), which needs a cold power cycle: run it only in a maintenance window, and only when jobs use managed memory across GPUs. NCCL, CUDA IPC and peer copies are what the tests above cover.

### Little dynamic BAR1

The static map leaves about 370 MiB of BAR1 for the driver's dynamic mappings, and forcing static BAR1 skips the driver's budget check for them. NCCL and the tests above run within it.

### Kernel and driver upgrades on the CMP node

- A distro kernel lacks the BAR1 patches: BAR1 stays at 64 MiB and P2P is gone. Keep the kernel packages held; a new kernel needs the patches, then the procedure above.
- A driver upgrade needs a cmpunlocker branch that supports the new version (its `VERSION` file).
- `install.sh` and `remove.sh` build against `/lib/modules/<K>/build`: keep the tree it points to.

## CMP 170HX: rollback

1. Stop the GPU workloads.
2. **root**: `sudo scripts/gpu-p2p/cmp/trees.sh activate <old-label>` (rsync, depmod, initramfs; prints the sha256). It restores the module tree only: compare `/etc/modprobe.d` and `/etc/default/grub` with the copies in `/root/cmpunlocker-trees/<old-label>/etc` and restore what differs by hand. If `update-initramfs` fails, copy back `/root/cmpunlocker-trees/<old-label>/initrd.img-<K>`.
3. [Cold power cycle](#5-cold-power-cycle), then check: `<gpus>` GPUs, no Xid, the old sha256 (`trees.sh show`). A build without the P2P patches shows `GNS` in `nvidia-smi topo -p2p r` again.
4. The ACS and NCCL settings are harmless without P2P. To remove them: `sudo systemctl disable --now gpu-acs-redir-off.service` (stopping restores the saved ACS values) and `sudo rm /etc/nccl.conf`.

If SSH does not come back: on the BMC's KVM console, pick the stock kernel under "Advanced options" in GRUB (64 MiB BAR1, no P2P; it may come up without a working GPU driver), run `sudo K=<K> scripts/gpu-p2p/cmp/trees.sh activate <old-label>`, then cold power-cycle into `<K>`.

Do not roll back with cmpunlocker's `remove.sh`: it deletes the cmpunlocker trees of every kernel and reinstalls the stock DKMS modules.

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

### g292: CMP 170HX, EPYC 7J13 (Milan)

8x CMP 170HX (`10de:20c2`), one EPYC 7J13 (one NUMA node), kernel 6.8.12-cmp with the BAR1 patches, `iommu=pt`, HMM off. GPU pairs 0,1 / 2,3 / 4,5 / 6,7 each sit behind one Switchtec switch, every switch on its own root port; all links at Gen2 x16. Build: cmpunlocker `g292/p2p-bar1-v0.5`, `install.sh --no-ecc`. Stock: cmpunlocker without the P2P patches (`GNS` for every pair, NCCL over host memory).

- **Boot**: `static BAR1 mapped offset 0x0 size 0xfe8e00000` on all 8 GPUs, `topo -p2p r` `OK` on all 56 ordered pairs, 65536 MiB memory and BAR1 per GPU, Gen2 x16.
- **Unchanged against stock**: 74 SMs per GPU, 64910 MiB total and 64604 MiB free as CUDA sees them, device-to-device copy 1592-1602 GB/s (stock 1589-1599).
- **Integrity, ACS redirect on**: an all-pairs peer copy test (64 MiB 3 times, 1024 MiB once) passed 56 of 56 ordered pairs. `p2p-copy-check.sh` found peer access on all 56 pairs and 0 mismatches in the 256 MiB round trips and the whole-memory fill. `run_host.sh` with the stages `p2ptest ordering stale hostnuma` ended with `OVERALL: PASS` and `KERNEL LOG: PASS`.
- **Integrity, ACS redirect off**: the all-pairs test (64 MiB 3 times) passed 56 of 56, `p2p-copy-check.sh` ended with `RESULT ok`.
- No Xid in any run.

NCCL all-reduce bus bandwidth (GB/s) from nccl-tests `all_reduce_perf`, 1 GiB, median of 3 runs; every run had 0 wrong results. The transport NCCL chose by itself is in parentheses.

ACS redirect on (the kernel's default):

| GPU set | Stock | P2P build, `NCCL_P2P_DISABLE=1` | P2P build, NCCL defaults | P2P build, `NCCL_P2P_LEVEL=SYS` |
| :--- | ---: | ---: | ---: | ---: |
| 0,1 (one switch) | 1.70 | 1.70 | 6.22 (P2P) | 6.22 |
| 0,2 (two switches) | 3.00 | 2.93 | 6.20 (P2P) | 6.21 |
| 0,1,2,3 (two switches, both GPUs of each) | 1.93 | 1.87 | 2.33 (P2P and SHM) | 4.29 |
| 0,2,4,6 (one GPU per switch) | 4.19 | 4.12 | 4.11 (SHM) | 6.33 |
| All 8 | 2.17 | 2.16 | 1.57 (P2P and SHM) | 0.47 |

ACS redirect off (`acs-redir.sh off` on all 16 bridges above the GPUs: 4 root ports, 12 switch ports):

| GPU set | P2P build, NCCL defaults | P2P build, `NCCL_P2P_LEVEL=SYS` |
| :--- | ---: | ---: |
| 0,1 (one switch) | 6.29 (P2P) | 6.29 |
| 0,2 (two switches) | 6.21 (P2P) | 6.21 |
| 0,1,2,3 (two switches, both GPUs of each) | 2.31 (P2P and SHM) | 6.36 |
| 0,2,4,6 (one GPU per switch) | 4.10 (SHM) | 6.34 |
| All 8 | 2.42 (P2P and SHM) | 6.38 |

- The `NCCL_P2P_DISABLE=1` column is the control in the same boot: it reproduces the stock numbers, so the new build and the HMM setting leave host staging unchanged.
- With redirect on, the rings that include both GPUs of a switch lose bandwidth with P2P: 4.29 (0,1,2,3) and 0.47 (all 8) against 6.36 and 6.38 with redirect off. The pair 0,1 alone and 0,2,4,6 (one GPU per switch) are hardly affected.
- With redirect off and only `/etc/nccl.conf` mounted into the container (no variable set), NCCL logged `NCCL_P2P_LEVEL set by environment to SYS` and gave the same 8-GPU result over P2P: 6.38 GB/s.

## Appendix B: References

- tinygrad P2P modules: <https://github.com/tinygrad/open-gpu-kernel-modules>
- aikitoria P2P branches: <https://github.com/aikitoria/open-gpu-kernel-modules>
- duanyll Method 3 fork: <https://github.com/duanyll/open-gpu-kernel-modules> (branches `*-p2p-48g`)
- duanyll blog, "48G 4090 P2P" (Chinese): <https://github.com/duanyll/duanyll.com-hexo/blob/master/source/_posts/tech/2026-7-13-4090-48G-P2P.md>
- Our fork: <https://github.com/LingzheZhao/open-gpu-kernel-modules>, branches [`610.57.04-p2p-48g`](https://github.com/LingzheZhao/open-gpu-kernel-modules/tree/610.57.04-p2p-48g) and [`610.57.04-p2p-fixes`](https://github.com/LingzheZhao/open-gpu-kernel-modules/tree/610.57.04-p2p-fixes)
- NCCL SHM/cuMem host bug: <https://github.com/NVIDIA/nccl/issues/2190>, proposed fix <https://github.com/NVIDIA/nccl/pull/2388>
- cmpunlocker (upstream): <https://github.com/amoghmunikote/cmpunlocker>
- bayley's cmpunlocker (BAR1 P2P and the kernel patches): <https://github.com/bayley/cmpunlocker>
- Our cmpunlocker fork: <https://github.com/LingzheZhao/cmpunlocker>, branch [`g292/p2p-bar1-v0.5`](https://github.com/LingzheZhao/cmpunlocker/tree/g292/p2p-bar1-v0.5) (kernel patches in `kernel-patches/`)
- Scripts and tests: [`scripts/gpu-p2p/`](../scripts/gpu-p2p/README.md)
