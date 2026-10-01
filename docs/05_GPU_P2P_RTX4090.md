# GPU P2P on RTX 4090

## Contents

- [GPU P2P on RTX 4090](#gpu-p2p-on-rtx-4090)
  - [Contents](#contents)
  - [Introduction](#introduction)
  - [How it works](#how-it-works)
    - [Static BAR1 P2P](#static-bar1-p2p)
    - [48 GB cards: dynamic BAR1 windows](#48-gb-cards-dynamic-bar1-windows)
    - [Static or dynamic: how to tell](#static-or-dynamic-how-to-tell)
    - [Patch sources: our fork](#patch-sources-our-fork)
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
  - [Pitfalls](#pitfalls)
    - [depmod override matches file names](#depmod-override-matches-file-names)
    - [Do not use the upstream install script](#do-not-use-the-upstream-install-script)
    - [initramfs and package hold](#initramfs-and-package-hold)
    - [HMM breaks host cuMem allocations under IOMMU passthrough](#hmm-breaks-host-cumem-allocations-under-iommu-passthrough)
    - [ACS redirect](#acs-redirect)
    - [PCIe link width](#pcie-link-width)
    - [Kernel and driver upgrades](#kernel-and-driver-upgrades)
  - [Using P2P in jobs](#using-p2p-in-jobs)
  - [Rollback](#rollback)
  - [Appendix A: Results, RTX 4090 24 GB](#appendix-a-results-rtx-4090-24-gb)
    - [Opt-in (`uvm_bar1_p2p_managed=1`)](#opt-in-uvm_bar1_p2p_managed1)
    - [Second platform: EPYC 9554 (Genoa)](#second-platform-epyc-9554-genoa)
  - [Appendix B: Results, RTX 4090 48 GB](#appendix-b-results-rtx-4090-48-gb)
    - [Hardware and software](#hardware-and-software)
    - [aikitoria v3 alone](#aikitoria-v3-alone)
    - [Host and peer copies](#host-and-peer-copies)
    - [NCCL all-reduce](#nccl-all-reduce)
    - [Managed memory and resident memory](#managed-memory-and-resident-memory)
    - [HMM and DMA32 evidence](#hmm-and-dma32-evidence)
    - [Comparison with the blog](#comparison-with-the-blog)
  - [Appendix C: References](#appendix-c-references)

## Introduction

GeForce drivers disable PCIe peer-to-peer (P2P) between GPUs, so CUDA peer copies and NCCL collectives are staged through host memory. A patched build of the kernel modules of NVIDIA's **open** driver re-enables P2P on the RTX 4090 (AD102): `cudaDeviceCanAccessPeer` returns true and NCCL uses its P2P transport. Userspace (libcuda, NCCL, PyTorch) stays unchanged.

This is an **unofficial community patch**, not supported by NVIDIA. Lineage (links in [Appendix C](#appendix-c-references)):

- tinygrad: ports the GH100 PCIe BAR1 P2P path (static BAR1 identity mapping) to AD102.
- aikitoria: maintained P2P branches per driver version (e.g. `610.57.04-p2p-v3`).
- duanyll's fork and blog post (Chinese): "Method 3", dynamic BAR1 P2P for modded 48 GB cards.
- Our fork ([below](#patch-sources-our-fork)): `610.57.04-p2p-fixes` = aikitoria v3 + general fixes; `610.57.04-p2p-48g` = those fixes + duanyll's Method 3. **Deploy `610.57.04-p2p-48g` on every node**: its Method 3 code stays dormant when BAR1 >= VRAM.

Card types:

| Card | VRAM | Max BAR1 | P2P path |
| :--- | :--- | :--- | :--- |
| RTX 4090 (stock) | 24 GB | 32 GiB (>= VRAM) | static BAR1 (tinygrad/aikitoria) |
| RTX 4090 48 GB (modded, leaked VBIOS) | 48 GiB | 32 GiB | dynamic BAR1, duanyll's Method 3 |

On both, cross-GPU managed memory (`cudaMallocManaged`) stages through host memory by default; plain aikitoria v3 is not safe there ([managed memory](#managed-memory-uvm)). Everything else (NCCL, CUDA IPC, cuMem, `cudaMemcpyPeer`, kernel peer access) uses P2P.

When it pays off: the gain depends on how fast the platform stages copies through host memory. On EPYC Milan (24 GB node) 8-GPU NCCL all-reduce went from 0.8 to 12.9 GB/s ([Appendix A](#appendix-a-results-rtx-4090-24-gb)); on EPYC Genoa (48 GB node), whose host staging is already fast, P2P adds 1.24-1.47x ([Appendix B](#comparison-with-the-blog)). A GPU trained at x8 caps every NCCL ring that contains it ([PCIe link width](#pcie-link-width)).

Placeholders: `<agent>` Determined agent ID, `<K>` kernel release (`uname -r`), `<N>` driver branch (e.g. `610`), `<version>` full driver version (e.g. `610.57.04`), `<revision>` Ubuntu package revision (from `apt-cache policy`), `<gcc>` major version of the compiler the kernel was built with, `<fork>` absolute path of the built fork tree, `<image>` CUDA + PyTorch image.

## How it works

### Static BAR1 P2P

BAR1 is the GPU's PCIe window into its VRAM. With Resizable BAR the patched driver resizes BAR1 at load time to the largest size the card advertises (the stock driver left it at 256 MiB on our cards). The tinygrad/aikitoria patch then identity-maps all of VRAM into BAR1 ("static BAR1") and lets a GPU address a peer's memory as `peer BAR1 bus address + VRAM offset`. The peer's DMA goes straight over PCIe (through the IOMMU, hence `iommu=pt`). This only works when **BAR1 >= VRAM**: the driver turns static BAR1 on by itself when the usable VRAM plus a reserve fits BAR1 (24 GB cards with a 32 GiB BAR1).

### 48 GB cards: dynamic BAR1 windows

The modded 48 GB cards expose at most a 32 GiB BAR1 (`resource1_resize` = `0xffc0`, 64 MiB to 32 GiB); changing that would need a re-signed VBIOS. Static BAR1 is therefore impossible. Plain aikitoria v3 still reports P2P as available, falls back to the mailbox P2P type, and fails at the first peer mapping (`mapping of buffer object failed`, NCCL crashes; see [Appendix B](#aikitoria-v3-alone)).

duanyll's Method 3 maps each **peer-shared allocation** on demand into the owner's BAR1 (`kbusMapFbAperture`), IOMMU-maps that window for the peer, and encodes peer PTEs as `window DMA base + offset`. The GPU's MMU translates the window to any VRAM page, so allocations above 32 GiB work; only the total of simultaneously peer-mapped allocations must fit BAR1. NCCL maps only its own buffers (the blog estimates about 180 MB per GPU for an 8-GPU node, from NCCL source). Pairs with static BAR1 keep the original path.

### Static or dynamic: how to tell

`verify.sh` prints BAR1 and VRAM per GPU: BAR1 >= VRAM means static BAR1 is possible, BAR1 < VRAM means only dynamic windows. Whether static BAR1 is actually on shows only under load, because nvidia-smi's "BAR1 Used" leaves out the static identity map and, with static BAR1 on, follows the VRAM in use. Hold test on an idle GPU:

```bash
docker run --rm -d --name bar1-hold --gpus device=0 <image> \
    python -c "import torch, time; x = torch.empty(8 << 30, dtype=torch.uint8, device='cuda'); time.sleep(120)"
sleep 60; nvidia-smi -i 0 -q -d MEMORY | grep -A 3 BAR1; docker stop bar1-hold
```

| BAR1 Used while 8 GiB are held | Meaning |
| :--- | :--- |
| about 8 GiB above the idle value | static BAR1 on |
| unchanged (a few MiB) | static BAR1 off: dynamic windows (48 GB cards) or no BAR1 P2P |

- An idle value of 1-3 MiB says nothing, and neither does `nvidia-smi topo -p2p r` `OK`: plain aikitoria v3 reports `OK` even when BAR1 P2P is unavailable.
- Never force static BAR1 globally: `RMForceStaticBar1=1` makes driver init fail on GPUs whose VRAM exceeds BAR1. If ever needed, set it per device (`NVreg_RegistryDwordsPerDevice`).

### Patch sources: our fork

[LingzheZhao/open-gpu-kernel-modules](https://github.com/LingzheZhao/open-gpu-kernel-modules), both branches on top of aikitoria `610.57.04-p2p-v3` (`461d638`):

| Branch | Content | Use |
| :--- | :--- | :--- |
| `610.57.04-p2p-fixes` (`5fd0a2b`) | aikitoria v3 + six general fixes; no Method 3 code | Static-BAR1 cards only (no Method 3); all its fixes are in `610.57.04-p2p-48g`. |
| `610.57.04-p2p-48g` (`e896127`) | `610.57.04-p2p-fixes` + duanyll's Method 3 + Method 3 follow-ups | **Deploy on all nodes** (24 GB and 48 GB). |

Commits, oldest first (our Method 3 follow-ups are prefixed `48g-4090-p2p:`):

| Commit | Subject | Why | Scope |
| :--- | :--- | :--- | :--- |
| `6e03400` | Report no P2P without a mailbox area when BAR1 P2P is unavailable | The patch forces the BAR1 P2P type, so no mailbox area is reserved. When BAR1 P2P is unavailable for a pair (BAR1 resize failed, BAR1 < VRAM without Method 3), plain v3 still advertises P2P, falls back to the mailbox type and fails at the first peer mapping (NCCL crashes). Now P2P is reported as not supported. | general |
| `29eae1b` | Gate UVM managed memory off pre-Hopper BAR1 peers by default | Module parameter `uvm_bar1_p2p_managed`, default `0` ([managed memory](#managed-memory-uvm)). | general |
| `7a01901` | Encode SYS_NON_COHERENT in Turing/Ampere UVM HALs | Copy-engine and page-table encodings for the aperture UVM uses for BAR1 peers; used only with `uvm_bar1_p2p_managed=1`. Invalid apertures now log a UVM release assert instead of being mis-encoded. | general |
| `85bf240` | Require an aligned BAR1 DMA window for UVM peer access | A window that is not 2 MB aligned would lose address bits in UVM's PTEs: such pairs stay off managed-memory peer access; peer identity mappings lower their page size. | general |
| `c811a9c` | Map compressible BAR1 peer allocations uncompressed in UVM PTEs | On Turing/Ampere/Ada the PTE comptag field overlaps the sysmem address: a peer mapping of a compressible allocation pointed at (address mod 1 TiB) + 1 TiB, typically host RAM. Now mapped uncompressed, or refused when the owner's static BAR1 does not carry the compressed kind. | general |
| `5fd0a2b` | Invalidate sysmem L2 lines when unmapping BAR1 peer external mappings | Cached peer mappings are cached in L2 as sysmem; the unmap invalidated peer lines and left stale sysmem lines. | general |
| `d714238` | Port dynamic BAR1 P2P and remove session lock inversion on 615 (author duanyll; cherry-pick of `da77dddb`) | Method 3. Clean port; no API drift 615 -> 610 in the touched code. | Method 3 |
| `37d11f9` | map compressible dynamic BAR1 allocations uncompressed | `c811a9c` for dynamic windows, whose BAR1 PTE already decompresses; without it compressible allocations are refused. | Method 3 |
| `9574e44` | document dynamic BAR1 peers in the UVM managed-memory gate | Comments only: dynamic pairs never use managed-memory peer access (without the gate UVM would address host RAM: no static window, base 0). | Method 3 |
| `dec3e18` | safer dynamic BAR1 window teardown | Unmap only under the remote GPU lock; drain leftover windows on device destroy with the proper locks; `METHOD3` error logs for rejected window maps. | Method 3 |
| `12c62bb` | Method 3 wording for the p2p_caps no-mailbox gate | Comments and log tag only. Since `6e03400` no patched node advertises loopback P2P caps (the forced BAR1 type reserves no mailbox area); with Method 3 a mixed static/dynamic pair is refused as well. | Method 3 |
| `e896127` | require a page-aligned dynamic BAR1 window DMA address | A misaligned window would be accessed at the wrong address; now the mapping fails with a `METHOD3` error. | Method 3 |

Earlier history of the 48g branch (other commit hashes, same final tree): tag `610.57.04-p2p-48g-v1`.

### Managed memory (UVM)

Since driver 590.44.01 UVM supports PCIe BAR1 peers and enables them automatically in every multi-GPU CUDA process. The P2P patch makes RM report BAR1 P2P on Ada (and Turing/Ampere); without PCIe atomics over BAR1 (none before Blackwell) UVM addresses these peers with the aperture `SYS_NON_COHERENT`. NVIDIA enables BAR1 P2P only on Blackwell GB20x, whose Hopper-derived UVM HALs encode that aperture; the pre-Hopper HALs that Ada uses do not. With plain aikitoria v3 on static-BAR1 pairs:

| Managed-memory operation | Effect without the fix |
| :--- | :--- |
| GPU-to-GPU migration (copy engine) | Encoded as peer memory with a wrong peer ID: Xid 31 `FAULT_UNSUPPORTED_APERTURE` (physical write) on a UVM channel, then Xid 154 on all GPUs; only a reboot recovers (observed, [Appendix A](#appendix-a-results-rtx-4090-24-gb)). |
| Remote mapping (`cudaMemAdviseSetAccessedBy`, preferred location on a peer) | The PTE points into the accessing GPU's own VRAM: silent corruption (from code analysis). |

Only UVM-managed memory is affected. NCCL, CUDA IPC, cuMem, `cudaMemcpyPeer` and kernel peer access use page tables built by RM with the correct aperture.

The UVM BAR1 fix (`29eae1b`, `7a01901`) is the `nvidia_uvm` module parameter `uvm_bar1_p2p_managed` (read-only at runtime; set it in `/etc/modprobe.d/` and reboot):

| Value | Behaviour |
| :--- | :--- |
| `0` (default, the gate) | UVM does not use BAR1 peer access for managed memory on Turing/Ampere/Ada: managed pages migrate through host memory. External mappings keep P2P. |
| `1` (opt-in) | Managed memory uses BAR1 peer mappings and copies with the new encodings. Validated on 24 GB cards on EPYC Milan with the debug and the release UVM build ([Appendix A](#opt-in-uvm_bar1_p2p_managed1)). Pass an opt-in window ([script README](../scripts/gpu-p2p/README.md#managed-memory-and-the-uvm-bar1-fix)) on each platform before running services with it. Applies only to static pairs with a 2 MB aligned DMA window; dynamic (Method 3) pairs never use it. |

Hopper and newer GPUs are not affected by the parameter. The gate is the default and needs no extra validation; use `1` only on a platform where the opt-in window passed. Testing the opt-in needs a maintenance window in which a reboot is acceptable: [procedure](../scripts/gpu-p2p/README.md#managed-memory-and-the-uvm-bar1-fix).

### Other caveats

Peer atomics, the chipset check, the address range and ordering are left as the driver has them; compressible allocations are fixed in both branches.

| Caveat | Detail | What to do |
| :--- | :--- | :--- |
| Peer atomics | `cudaDevP2PAttrNativeAtomicSupported` is 0. An `atomicAdd` on another GPU's memory is atomic only among the issuing GPU's threads; concurrent updates from different GPUs can be lost. | Do not rely on cross-GPU atomics on peer memory. `ATOMICS=1` runs a diagnostic. |
| Compressible allocations | Fixed by the RM fix for compressible peer allocations (`c811a9c`, `37d11f9` for dynamic windows; see above). Without the fix a peer access writes host RAM at 1 TiB + offset. | The `COMPRESS=1` test refuses on nodes whose System RAM ends above 1 TiB unless `--above-1tib` confirms the fix is installed ([script README](../scripts/gpu-p2p/README.md#compressible-memory)). |
| Chipset P2P-read check | The patch forces P2P reads and writes on and skips the driver's check of chipset support for peer reads. | On a new host platform check the `p2ptest` integrity (peer loads) before service. Fine on EPYC Milan and Genoa. |
| BAR1 address range | The GPU DMA mask is 47 bits: every GPU's BAR1 must end below 2^47 (128 TiB). | On new platforms check Region 1 in `lspci -vv -d 10de:`. |
| Write ordering | Data and flag to the same destination (NCCL's pattern) are ordered. Data to GPU B with the flag in host memory or on GPU C is checked by the `ordering` stage. | Run the `ordering` stage on every new platform (results: [Appendix A](#appendix-a-results-rtx-4090-24-gb)). |

## Prerequisites

- [ ] **BIOS**: Above 4G Decoding and Resizable BAR enabled; the host bridge windows must fit each GPU's full BAR1 (32 GiB per GPU on 48 GB cards). Check after the first boot with the patched modules (BAR1 total in `nvidia-smi -q -d MEMORY`).
- [ ] **Secure Boot** off (or sign the modules yourself).
- [ ] **Driver**: Ubuntu's `nvidia-driver-<N>-open` of **exactly** the version of the fork branch (branch `<version>-p2p-48g`; the fork has only `610.57.04` branches). The closed driver cannot be patched. Other versions need a port; aikitoria and duanyll maintain branches for other versions.
- [ ] **Build tools**: `linux-headers-<K>` for the target kernel `<K>` and the compiler the kernel was built with (`cat /proc/version`; HWE kernels may use a newer gcc than the default one, pass it as `CC=x86_64-linux-gnu-gcc-<gcc>`).
- [ ] **Homogeneous node**: all GPUs of one type; do not mix 48 GB (dynamic) and 24 GB (static) cards.
- [ ] **New host platform** (not EPYC Milan/Genoa): BAR1 below 2^47 and peer reads verified ([caveats](#other-caveats)).
- [ ] **PCIe links**: every GPU at its full width under load (links downtrain when idle):

    ```bash
    nvidia-smi --query-gpu=index,pcie.link.gen.current,pcie.link.width.current,pcie.link.width.max --format=csv -lms 500
    ```

    A GPU at x8 halves its host and P2P bandwidth and caps every NCCL ring that contains it ([details](#pcie-link-width)).
- [ ] **Maintenance window**: the node reboots; its Determined tasks must finish or be stopped.
- [ ] **Test image**: a CUDA image with `nvcc` and PyTorch for [the tests](#6-run-the-tests).

## Procedure

Scripts: [`scripts/gpu-p2p/`](../scripts/gpu-p2p/README.md). Placeholders: [Introduction](#introduction). Steps marked **root** need `sudo`.

### 1. Disable the node in Determined

Admin, from any machine with the `det` CLI:

```bash
det agent disable --drain <agent>   # lets running tasks finish; without --drain they are stopped
det agent list                      # wait until nothing runs on <agent>
```

### 2. Install the matching open driver

**root**. Installs or upgrades the userspace driver and the stock DKMS modules (replaces a closed driver). Check first that apt offers the branch version, then install exactly that version:

```bash
apt-cache policy nvidia-driver-<N>-open                         # a <version>-* version must be listed
sudo apt install nvidia-driver-<N>-open=<version>-<revision>    # <revision>: the Ubuntu revision listed
dpkg-query -W 'nvidia-*-<N>-open'                               # upstream version must be <version>
```

No reboot yet.

### 3. Build the patched modules

Unprivileged, on the node (or a machine with the same kernel and headers):

```bash
# the 48g branch (see Patch sources); other driver versions need a port
git clone --depth 1 -b <version>-p2p-48g https://github.com/LingzheZhao/open-gpu-kernel-modules.git
cd open-gpu-kernel-modules
git rev-parse HEAD > .source-commit    # recorded in updates/p2p/SOURCE by the installer
make modules -j"$(nproc)" SYSSRC=/lib/modules/<K>/build CC=x86_64-linux-gnu-gcc-<gcc>
modinfo -F version kernel-open/nvidia.ko    # must be <version>
modinfo -F vermagic kernel-open/nvidia.ko   # must start with <K>
```

`SYSSRC` selects the kernel tree (needs `linux-headers-<K>`); without it the build targets the running kernel. The build takes well under a minute on a 128-thread node. Output: `kernel-open/nvidia{,-uvm,-modeset,-drm,-peermem}.ko`.

### 4. Install the modules

**root**. From this repository's checkout:

```bash
sudo K=<K> V=<version> SRC=<fork>/kernel-open scripts/gpu-p2p/install-p2p-modules.sh install
# or, if sudo refuses VAR=value arguments:
sudo scripts/gpu-p2p/install-p2p-modules.sh --kernel <K> --version <version> --src <fork>/kernel-open install
```

All options: `scripts/gpu-p2p/install-p2p-modules.sh --help`. `K` defaults to the running kernel and `V` to the version of the built modules. The script refuses to run while apt/dpkg is busy or holds its lock, with Secure Boot on, when the installed driver package or the modules do not match `<version>` or `<K>`, when GRUB already sets a different IOMMU mode, when `dpkg --audit` reports unfinished packages (run `dpkg --configure -a`), when `GRUB_CMDLINE_LINUX_DEFAULT` is not a simple double-quoted value or appears more than once (edit by hand), on non-GRUB systems, or on an unknown CPU vendor. It backs up what it replaces and then, in this order:

| Change | Purpose |
| :--- | :--- |
| `/etc/modprobe.d/nvidia-uvm-hmm.conf`: `options nvidia-uvm uvm_disable_hmm=1` | Avoids the HMM/DMA32 bug ([pitfall](#hmm-breaks-host-cumem-allocations-under-iommu-passthrough)). |
| `amd_iommu=on iommu=pt` (Intel: `intel_iommu=on iommu=pt`; set by the script but not tested here) in `GRUB_CMDLINE_LINUX_DEFAULT`; `update-grub` | IOMMU passthrough (identity domain) for the GPUs; `iommu=pt` is the operative part. |
| Checks that the default GRUB entry boots `<K>` with `iommu=pt` (`GRUB_DEFAULT=0` boots the highest installed kernel) | Stops here, before any module is installed, if not. With a non-zero `GRUB_DEFAULT` it only checks that an entry for `<K>` with `iommu=pt` exists and prints a `WARNING`: then check the default entry by hand before rebooting. |
| `apt-mark hold` on the `-<N>` driver packages | Keeps userspace at `<version>` ([pitfall](#initramfs-and-package-hold)). |
| Copies the five `.ko` to `/lib/modules/<K>/updates/p2p/` | Leaves the DKMS build in `updates/dkms/` untouched. |
| `/etc/depmod.d/nvidia-p2p.conf`: `override <file name> <K> updates/p2p` per module; `depmod -a <K>`; checks that `modinfo -k <K> -n <module>` resolves to `updates/p2p` for every module | Makes `modprobe` load the patched modules ([pitfall](#depmod-override-matches-file-names)). |
| `update-initramfs -u -k <K>` | In case the initramfs carries NVIDIA modules. |

If a step fails after the override was written, the script removes the override for `<K>` again, so the next boot loads the stock modules. It never reboots.

### 5. Reboot and verify

**root**: `sudo systemctl reboot`. Admin: `det agent list` must still show `<agent>` as disabled; if not, run `det agent disable <agent>` again before continuing. Then, as any user (the hold test needs the `docker` group):

1. Run the [hold test](#static-or-dynamic-how-to-tell): on 24 GB cards it must show static BAR1 on; on 48 GB cards it stays off (dynamic windows). It also loads `nvidia_uvm`, which otherwise loads only on first CUDA use, so that `verify.sh` can check it (without the hold test: `nvidia-modprobe -u -c 0`).
2. Run `verify.sh`:

```bash
scripts/gpu-p2p/verify.sh
```

| Check | Expected |
| :--- | :--- |
| `head -1 /proc/driver/nvidia/version` | `Open Kernel Module ... <version>`; `nvidia-smi` reports the same version. |
| Loaded modules | srcversion of each loaded module equals the `updates/p2p` build (for `nvidia.ko` this covers only the interface layer; BAR1 and `topo -p2p` confirm the RM core). |
| BAR1 (`nvidia-smi -q -d MEMORY`) | Total >= VRAM on 24 GB cards; 32768 MiB on 48 GB cards (256 MiB means the resize failed). |
| IOMMU | `/proc/cmdline` has `iommu=pt`; `/sys/bus/pci/devices/<gpu>/iommu_group/type` is `identity`. |
| HMM | `/sys/module/nvidia_uvm/parameters/uvm_disable_hmm` is `Y`; no `nvidia-uvm-hmm` in `/proc/iomem`. |
| `nvidia-smi topo -p2p r` | `OK` for all pairs (stock: `CNS` on the closed driver, `GNS` on the open driver). |
| PCIe link | Full width (WARN only; see [PCIe link width](#pcie-link-width)). |
| Kernel log | No `Xid` or assertions (FAIL); `METHOD3` lines (dynamic BAR1 window errors) are shown as WARN and must be investigated before enabling the node. |
| Package hold | `nvidia-driver-<N>-open` is held (WARN only). |

Pass: `verify.sh` ends with `RESULT: PASS`. If it reports `nvidia_modeset` or `nvidia_drm` not loaded (a headless node), load them with `sudo modprobe nvidia_drm` and run it again. Then check the UVM gate by hand:

- `modinfo -k <K> -F parm nvidia_uvm | grep uvm_bar1_p2p_managed` must print the parameter (no output: the modules lack the [UVM BAR1 fix](#managed-memory-uvm)), and `cat /sys/module/nvidia_uvm/parameters/uvm_bar1_p2p_managed` must print `0` (or `1` on a platform where the [opt-in window](../scripts/gpu-p2p/README.md#managed-memory-and-the-uvm-bar1-fix) passed with this build).
- If the parameter is missing, or `1` without a passed opt-in window: stop. Do not run `MANAGED=1` and do not enable the node. Missing: rebuild from `<version>-p2p-48g` ([step 3](#3-build-the-patched-modules)) and reinstall. `1` without a passed window: remove the `uvm_bar1_p2p_managed=1` option from `/etc/modprobe.d/` and reboot.

### 6. Run the tests

Unprivileged (member of the `docker` group), on the idle node. Service validation is the default stages plus `managedtest` with the gate on, in two runs:

```bash
# 6a: managedtest alone on one pair first; must end with OVERALL: PASS and KERNEL LOG: PASS
OUT_DIR=~/p2p-logs MANAGED=1 MANAGED_ARGS="--quick --pair 0,1" STAGES=managedtest scripts/gpu-p2p/tests/run_host.sh <image> <label>-quick
# 6b: the service run
OUT_DIR=~/p2p-logs MANAGED=1 RESIDENT_GB=<n> scripts/gpu-p2p/tests/run_host.sh <image> <label>
```

`<n>`: resident GiB per GPU, VRAM minus a few GiB (e.g. 20 on 24 GB cards, 38 on 48 GB cards); `<label>`: log name. `run_host.sh` starts `<image>` (container flags: [script README](../scripts/gpu-p2p/README.md#test)), logs driver, BAR1 and the UVM managed-memory mode, samples the PCIe links and checks the kernel log afterwards. Inside, `run_tests.sh` runs these stages in order and stops at the first failure:

| Stage | Checks |
| :--- | :--- |
| `p2ptest` | Host<->GPU bandwidth per GPU (pinned memory on the GPU's socket), peer-access matrix, copy bandwidth with P2P disabled/enabled (uni- and bidirectional, same- vs cross-socket), integrity of peer stores, peer loads and copy-engine copies over regions spread across the test buffers. |
| `ordering` | GPU0 stores into GPU1's memory, `__threadfence_system()`, then a flag in host memory, in GPU1's or in GPU2's memory: no stale data. |
| `stale` | A peer re-reads a buffer after its owner overwrote it and signalled an event, for every ordered pair: no stale data. |
| `hostnuma` | `cuMemCreate` pinned host memory on every NUMA node (what NCCL's SHM transport uses); catches the [HMM pitfall](#hmm-breaks-host-cumem-allocations-under-iommu-passthrough). |
| `nccl` | `nccl_allreduce.py` via `torchrun` over GPU sets (all, each socket, same- and cross-socket pairs), with NCCL defaults and with `NCCL_P2P_LEVEL=SYS` (`p2p-sys`): busbw, correctness, transport, resident tensor intact. |
| `managedtest` (`MANAGED=1`) | `cudaMallocManaged` across every ordered GPU pair: fault, prefetch, memcpy, `SetAccessedBy`, peer `atomicAdd`, oversubscription. `run_host.sh` refuses it when the UVM module lacks the `uvm_bar1_p2p_managed` parameter (driver without the fix). |

Pass: both logs have the header `UVM managed memory on BAR1 peers: gate on` (`OPT-IN window` with `uvm_bar1_p2p_managed=1`), `OVERALL: PASS` and `KERNEL LOG: PASS`; in 6b every all-reduce is `correct=True` and the `p2p-sys` runs use a `P2P/...` transport (the `default` runs may use SHM on newer NCCL; [why](#using-p2p-in-jobs)).

**Do not add `ATOMICS=1` or `COMPRESS=1` to service validation.** `atomics` can log Xid 31 (fails the kernel-log check). `compress` without the RM fix for compressible peer allocations (`c811a9c`/`37d11f9` in the installed build, see `updates/p2p/SOURCE`) writes host RAM at 1 TiB + offset; run it only as described in the [script README](../scripts/gpu-p2p/README.md#compressible-memory). All options: [script README](../scripts/gpu-p2p/README.md#settings).

### 7. Enable the node again

Only after step 5 (`verify.sh` `RESULT: PASS`, hold test as expected, `uvm_bar1_p2p_managed` as checked there) and step 6 (both logs `OVERALL: PASS` and `KERNEL LOG: PASS`; header `gate on`, or `OPT-IN window` when the node keeps the opt-in after a passed window) passed, with the release UVM build installed:

```bash
det agent enable <agent>
```

Check that the monitoring containers came back after the reboot (`docker ps`: `node-exporter`, `monitoring_cadvisor` and the `dcgm-exporter` service); each needs a restart policy ([check](../services/README.md#72-run)).

Tell the users of the node about [the NCCL settings](#using-p2p-in-jobs).

## Pitfalls

### depmod override matches file names

`override` lines in `depmod.d` match the **file name** with dashes (`nvidia-uvm`), not the module name (`nvidia_uvm`). With underscores only `nvidia` is overridden and the stock `nvidia-uvm`, `nvidia-modeset`, `nvidia-drm` and `nvidia-peermem` still load. Verify each one: `modinfo -k <K> -n nvidia_uvm` must print a path under `updates/p2p`.

### Do not use the upstream install script

aikitoria's `install.sh` runs `make modules_install`, which installs into `/lib/modules/<K>/kernel/...`. On Ubuntu the DKMS modules in `updates/dkms/` come first in the search order and win, so the patched modules are silently ignored. Use the `updates/p2p` + depmod override install instead.

### initramfs and package hold

- The NVIDIA modules were not in the initramfs on our nodes, but regenerate it anyway (`update-initramfs -u -k <K>`) so a stale copy cannot load first.
- The patched modules are built for exactly `<version>`. An apt upgrade of the userspace driver alone gives "Driver/library version mismatch". Keep the driver packages on hold (`apt-mark showhold`) and upgrade them only together with a rebuild ([below](#kernel-and-driver-upgrades)).

### HMM breaks host cuMem allocations under IOMMU passthrough

A bug in the open driver, independent of the P2P patches (none of them touches this path). It needs all of: open driver, UVM HMM enabled (the default), `iommu=pt`.

Mechanism:

1. With HMM, UVM registers device-private memory for each GPU's VRAM with `request_free_mem_region()`, at the top of the physical address space (on EPYC with 52-bit physical addresses, just below 2^52; `nvidia-uvm-hmm` in `/proc/iomem`). This inflates the NUMA node spans (ZONE_DEVICE; the `Device` zones in `/proc/zoneinfo`).
2. `nv_get_max_sysmem_address()` (`kernel-open/nvidia/nv-vm.c`) takes the largest node end, about 2^52, which exceeds the GPU DMA mask (47 bits). `nv_compute_gfp_mask()` then adds `GFP_DMA32` whenever DMA is direct, which is the case in the identity domain of `iommu=pt`.
3. Every cuMem host allocation (`cuMemCreate` with a HOST or HOST_NUMA location) comes from ZONE_DMA32, about 2 GiB on NUMA node 0 only: allocations on node 1 always fail, on node 0 after about 1 GiB (`CUDA_ERROR_OUT_OF_MEMORY`; kernel log `NV_ERR_NO_MEMORY` at `system_mem.c:353` / `mem_desc.c:1338`). Legacy `cuMemHostAlloc` is not affected. With the default IOMMU mode (DMA-FQ) the same driver allocates fine despite the HMM regions.

Consequence: NCCL >= 2.26.3 (2.27.5 measured) crashed in its SHM transport on 8 GPUs. Ranks whose cuMem host probe fails fall back to `/dev/shm` and misread their peers' cuMem descriptors, and the job fails with `Error while attaching to shared memory segment /dev/shm/nccl-<garbage> (size 0)` ([NVIDIA/nccl#2190](https://github.com/NVIDIA/nccl/issues/2190); proposed fix [NVIDIA/nccl#2388](https://github.com/NVIDIA/nccl/pull/2388), "transport: agree on cuMem host buffers with the peer"). Per-job workarounds: `NCCL_CUMEM_HOST_ENABLE=0`, or avoid SHM with `NCCL_P2P_LEVEL=SYS`.

Fix: `options nvidia-uvm uvm_disable_hmm=1` (set by the install script) and a **reboot**; unloading `nvidia-uvm` does not shrink the zone spans. Keep it as long as `iommu=pt` is set, also with the stock modules. Cost: no HMM, i.e. GPUs cannot access plain `malloc`'d pageable memory directly; typical PyTorch/Determined jobs do not use it. Related but different: [NVIDIA/open-gpu-kernel-modules#885](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/885) (memory-only NUMA nodes). Evidence in [Appendix B](#hmm-and-dma32-evidence).

### ACS redirect

PCIe ACS P2P Request/Completion Redirect forces peer traffic up to the root complex. Linux enables it on the bridges whenever an IOMMU driver is active, also with `iommu=pt`, so a BIOS switch may not stick.

With `iommu=pt` and each GPU behind its own root port, ACS redirect left on did not limit P2P ([Appendix B](#comparison-with-the-blog)); with ACS on and the default IOMMU mode duanyll's validation logs show about 3-9 GB/s. Check the bandwidth on each platform. If P2P bandwidth is far below the host<->GPU bandwidth:

- Runtime (**root**, until the next reboot): `sudo scripts/gpu-p2p/acs-redir.sh status|off|restore`. `off` clears Request Redirect, Completion Redirect and Egress Control on the bridges above the NVIDIA GPUs (`setpci ... ECAP_ACS+6.w`) and saves the old values for `restore`.
- Persistent: kernel parameter `pci=disable_acs_redir=pci:<vendor>:<device>` with the vendor:device ID of the GPU root ports (`lspci -nn`; make sure no other bridge shares the ID). The `pci:vendor:device` form avoids semicolons, which GRUB would split. `install-p2p-modules.sh --restore` does not remove it; see [Rollback](#rollback).

Clear the redirect only with `iommu=pt` (identity IOMMU domains, checked by `verify.sh`): with a translating IOMMU, peer requests carry IOVAs, and a PCIe switch could route them to the wrong device without translation. `acs-redir.sh off` refuses otherwise (`ACS_FORCE=1` overrides).

### PCIe link width

A GPU trained at x8 instead of x16 gets half the host<->GPU and P2P bandwidth (about 13 instead of 26 GB/s at Gen4), and caps every NCCL ring that includes it (a ring runs at its slowest link). The width can change between boots. Check under load (see [Prerequisites](#prerequisites)); `run_host.sh` records the widths seen during the tests. Fix it in hardware (seating, riser, slot), not in software.

### Kernel and driver upgrades

- The depmod override is per kernel. After a kernel upgrade the new kernel loads the stock DKMS modules: safe, but P2P is gone (`topo -p2p` shows `GNS`) until the modules are rebuilt for the new kernel (`SYSSRC=/lib/modules/<new K>/build`) and installed with the new `K`.
- A driver upgrade needs a P2P branch for the new version: disable the node, run `sudo K=<K> scripts/gpu-p2p/install-p2p-modules.sh --restore` for every kernel with P2P modules (`ls /lib/modules/*/updates/p2p`); only after the last one does it unhold the packages and remove the GRUB parameters and the HMM setting it added (the next install sets them again). Then install the new driver, build the matching branch, run the install script again, reboot and verify.
- Follow the general rule of [Maintainance](01_First-time_Setup_of_Cluster_Nodes.md#maintainance-upgrade-apt-packages--determined-ai): never upgrade kernel or driver packages while the node runs tasks.

## Using P2P in jobs

| Setting | When | Why |
| :--- | :--- | :--- |
| `NCCL_P2P_LEVEL=SYS` | Newer NCCL (e.g. 2.27 in PyTorch 2.9) | On nodes with several PCIe host bridges, NCCL rates GPU pairs beyond its default P2P level and uses SHM for groups larger than two. With this setting the log shows `via P2P/CUMEM`. Older NCCL (2.20) picks P2P by default (`via P2P/IPC`); setting it there measured the same. |
| `NCCL_LOCAL_REGISTER=0`, `NCCL_GRAPH_REGISTER=0` | 48 GB cards (Method 3) | duanyll's recommendation: keep large user buffers out of BAR1. |
| Per-allocation NCCL peer mappings (`P2P/CUMEM`, and `P2P/IPC` in the measured NCCL 2.20) | 48 GB cards | Only NCCL's buffers are mapped into BAR1, so about 47 GiB stay usable (38 GiB resident per GPU verified with both `P2P/IPC` and `P2P/CUMEM`). What must be avoided is legacy whole-device peer access (next row). |
| One process per GPU | Always | `torchrun`/Determined default. Legacy whole-device peer access (`cudaDeviceEnablePeerAccess` in a process that owns several GPUs, e.g. `DataParallel`, single-process benchmarks) must fit **all** allocations into BAR1: at most ~32 GiB per GPU on 48 GB cards. |

Check the transport with `NCCL_DEBUG=INFO`: `via P2P/IPC` or `via P2P/CUMEM` is P2P, `via SHM` is host staging. Managed memory (`cudaMallocManaged`) works across GPUs but stages through host memory ([UVM gate](#managed-memory-uvm)). Cross-GPU atomics on peer memory are not atomic ([caveats](#other-caveats)).

Determined: the task container defaults of a Determined dynamic resource pool are copied when the pool is created ([Add a resource pool](03_Setup_DeterminedAI.md#add-a-resource-pool)), so these variables cannot be added as a pool default later. Set them in the experiment configuration:

```yaml
environment:
  environment_variables:
    - NCCL_P2P_LEVEL=SYS
    - NCCL_LOCAL_REGISTER=0
    - NCCL_GRAPH_REGISTER=0
```

## Rollback

1. Disable the node in Determined ([step 1](#1-disable-the-node-in-determined)).
2. **root**: `sudo K=<K> scripts/gpu-p2p/install-p2p-modules.sh --restore` undoes the install for `<K>`: removes the modules and the override, and, unless other kernels still have P2P modules, removes the GRUB parameters the script added and unholds the packages it held (otherwise they stay; details: [script README](../scripts/gpu-p2p/README.md#roll-back)). If `iommu=pt` stays on the kernel command line (no record of the script adding it, or kept on purpose), it keeps `/etc/modprobe.d/nvidia-uvm-hmm.conf`; remove the HMM setting only together with `iommu=pt` ([why](#hmm-breaks-host-cumem-allocations-under-iommu-passthrough)).
   If `pci=disable_acs_redir=...` was added to `GRUB_CMDLINE_LINUX_DEFAULT` ([ACS redirect](#acs-redirect)), remove it before or together with `--restore` and run `update-grub`; `--restore` does not touch it.
   Manual equivalent: remove `/lib/modules/<K>/updates/p2p/` and `/etc/depmod.d/nvidia-p2p.conf` and run `depmod -a <K>`; if `iommu=pt` goes, remove `amd_iommu=on iommu=pt` from `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub` (the file from before the first install is `/var/backups/nvidia-p2p/grub.pre-p2p`), run `update-grub` and remove `/etc/modprobe.d/nvidia-uvm-hmm.conf`; run `update-initramfs -u -k <K>`; `sudo apt-mark unhold` the packages listed in `/var/backups/nvidia-p2p/held-packages.txt` (or `apt-mark showhold`).
3. Optional, before the reboot: reinstall the previous driver (`apt-get install` it; package lists in `/var/backups/nvidia-p2p/packages-*.txt`).
4. **root**: reboot. Check: `det agent list` still shows `<agent>` disabled; `nvidia-smi topo -p2p r` shows `GNS` (open driver) or `CNS` (closed driver); `nvidia-smi -q -d MEMORY` no longer shows the resized BAR1 (256 MiB on the measured cards); `modinfo -n nvidia_uvm` resolves to `updates/dkms`.
5. Enable the node again.

## Appendix A: Results, RTX 4090 24 GB

Measured hardware: GPU Node 5 (cvgl-node05), 8x MSI RTX 4090 24 GB, 2x EPYC 7543 (Milan), 512 GB, kernel 6.5.0-25-generic. GPU1 and GPU4 trained at Gen4 x8.

- Without P2P: stock open driver 610.57.04, default IOMMU mode, `topo -p2p` `GNS`, BAR1 256 MiB.
- With P2P: branch `610.57.04-p2p-48g` (tree of `e896127`), `iommu=pt`, `uvm_disable_hmm=1`, UVM gate on (`uvm_bar1_p2p_managed=0`). Static BAR1 on ([hold test](#static-or-dynamic-how-to-tell): holding 8 GiB on GPU0 raised "BAR1 Used" from 1 MiB to 8653 MiB). `run_host.sh` with the default stages and `MANAGED=1`, `RESIDENT_GB=20`: `OVERALL: PASS`, kernel log clean.
- Before the fixes, a run with plain aikitoria v3 hit Xid 31 `FAULT_UNSUPPORTED_APERTURE` (physical write) during managed-page migration, then Xid 154; it needed a reboot ([managed memory](#managed-memory-uvm)).

| Test | Without P2P (stock) | With P2P (`610.57.04-p2p-48g`) |
| :--- | ---: | ---: |
| Host<->GPU, x16 GPUs (GB/s, H2D / D2H) | 25.4-26.8 | 26.6 / 26.1 |
| Host<->GPU, x8 GPUs (GB/s, H2D / D2H) | 13.2-13.5 | 13.3 / 13.2 |
| Peer copy, host staged (GB/s, range) | 12-23 | 12-22.5 |
| Peer copy, host staged, mean same / cross socket (GB/s) | 15.5 / 15.9 | not recorded |
| P2P copy, unidirectional (GB/s) | no peer access | same socket max 26.3 (mean 19.7 with the x8 GPUs); cross socket mean 14.6: socket 0 -> 1 about 22, socket 1 -> 0 about 10; pairs with an x8 GPU 13.1 |
| P2P copy, bidirectional (GB/s) | no peer access | same socket 52.0; cross socket max 41.8; with an x8 GPU 25.9 |
| P2P integrity (`p2ptest`) | no peer access | 56/56 pairs; 2016 blocks up to a 21.0 GiB offset, 0 bad words |
| `ordering` | not run | PASS (GPU0 writes into GPU1; flag in host memory, in GPU1 and in GPU2; 200 iterations each; 0 stale words, 0 timeouts) |
| `stale` | not run | PASS (56 ordered pairs, `sm` and `ce` variants, 0 mismatches) |
| `hostnuma` | not run | PASS (96 allocations; 8 GiB held on each NUMA node) |
| Managed memory (`managedtest`) | not run | PASS with the gate on (managed pages stage through host memory per the driver gate; the test itself cannot tell): 56 pairs x fault, prefetch, memcpy, accessedby, atomic; oversub 28.3 GiB managed vs 22.7 GiB free |
| NCCL busbw, 8 GPUs (GB/s) | 0.8 | 12.9 |
| NCCL busbw, GPU0-3 | 4.6-4.7 | 12.9 |
| NCCL busbw, GPU4-7 | 4.4-4.5 | 12.9 |
| NCCL busbw, pair 0,1 | 4.4 | 12.8 |
| NCCL busbw, pair 2,3 | 4.5 | 25.1 |
| NCCL busbw, pair 4,5 | 4.0 | 12.8 |
| NCCL busbw, pair 0,4 (cross socket) | 0.7 | 12.4 |
| NCCL busbw, pair 3,7 (cross socket) | 0.8 | 19.2 |

- NCCL: PyTorch 2.3 / NCCL 2.20.5 in both columns, busbw at 1024 MiB. Without P2P the transport is SHM; with P2P it is `P2P/IPC` by default and `P2P/CUMEM` with `NCCL_CUMEM_ENABLE=1`. `NCCL_P2P_LEVEL=SYS` made no difference. Every all-reduce was `correct=True` and the 20 GiB resident tensor per GPU stayed intact. NCCL 2.27 (newer PyTorch) was not measured on this node.
- x8 links: every set that contains GPU1 or GPU4 is capped at about 12.4-12.9 GB/s (8 GPUs and both sockets 12.9, pairs 0,1 and 4,5 12.8, pair 0,4 12.4).
- Cross socket: P2P copies from socket 1 to socket 0 reach only about 10 GB/s on this platform (asymmetric); pair 3,7 (x16, cross socket) reached 19.2 against 25.1 for the same-socket pair 2,3.
- Milan's host staging makes NCCL without P2P very slow, as on the blog's Rome platform. With P2P, the x8-capped sets gained about 2.8-3.2x, pair 2,3 about 5.6x, and the cross-socket sets (8 GPUs, pairs 0,4 and 3,7) about 16-24x (ratios of the table values).

### Opt-in (`uvm_bar1_p2p_managed=1`)

Same node, build and settings, in an [opt-in window](../scripts/gpu-p2p/README.md#managed-memory-and-the-uvm-bar1-fix) (node disabled in the scheduler). `nvidia-uvm` was swapped between the debug build (`UVM_BUILD_TYPE=debug`, asserts active) and the release build by unloading and reloading the module, with the option in `/etc/modprobe.d/`. UVM's procfs peer info showed link type `UVM_GPU_LINK_PCIE_BAR1` with aperture `UVM_APERTURE_SYS_NON_COHERENT` for the pairs inspected, and the log header said `OPT-IN window`. No DMA window was misaligned (no `NOTE:` line).

| UVM build | Run | Result |
| :--- | :--- | :--- |
| debug | `managedtest --quick --pair 0,1 --modes accessedby` (page-table encoding alone) | PASS |
| debug | `--quick --pair 0,1`, modes fault, prefetch, memcpy (copy-engine encoding) | PASS |
| debug | fault, prefetch, memcpy, 56 pairs | PASS |
| debug | `--quick`, accessedby and atomic, 56 pairs | PASS |
| release | accessedby, 56 pairs | PASS |
| release | service run: default stages and `MANAGED=1`, `RESIDENT_GB=20` (all six managed modes, 56 pairs, oversub 28.3 GiB managed vs 22.7 GiB free) | `OVERALL: PASS` |

Every run ended with `KERNEL LOG: PASS` (no Xid, no assert). The NCCL results matched the gate run (8 GPUs 12.9 GB/s, every all-reduce `correct=True`), as expected: NCCL does not use managed memory. Not covered: `uvm_peer_copy=virt`, `oversub` with the debug build, and the managed-memory migration speed compared with the gate.

### Second platform: EPYC 9554 (Genoa)

Measured hardware: GPU Node 6 (cvgl-node06), 8x RTX 4090 24 GB, 2x EPYC 9554 (Genoa), 1.5 TiB, kernel 6.5.0-25-generic. GPU6 trained at Gen4 x8. Same build and settings as above (UVM gate on), installed with `install-p2p-modules.sh`; `verify.sh` `RESULT: PASS`, static BAR1 on. Service run with `MANAGED=1`, `RESIDENT_GB=20` on the idle node: `OVERALL: PASS`, kernel log clean. No stock-driver baseline was measured on this node; the host-staged row is the same run with peer access disabled.

| Test | With P2P (`610.57.04-p2p-48g`) |
| :--- | ---: |
| Host<->GPU, x16 GPUs (GB/s, H2D / D2H) | 26.8 / 26.4 |
| Host<->GPU, x8 GPU (GB/s, H2D / D2H) | 13.3 / 13.2 |
| Peer copy, host staged (GB/s) | 22.0-22.6; pairs with the x8 GPU 12.9-13.0 |
| P2P copy, unidirectional (GB/s) | 26.3-26.4 for every x16 pair, same or cross socket, both directions; pairs with the x8 GPU 13.2 |
| P2P copy, bidirectional (GB/s) | 52.1 for every x16 pair; pairs with the x8 GPU 26.0 |
| P2P integrity (`p2ptest`) | 56/56 pairs; 2016 blocks up to a 21.0 GiB offset, 0 bad words |
| `ordering`, `stale`, `hostnuma` | PASS |
| Managed memory (`managedtest`) | PASS with the gate on: 56 pairs x fault, prefetch, memcpy, accessedby, atomic; oversub 28.3 GiB managed vs 22.7 GiB free |
| NCCL busbw, 8 GPUs (GB/s) | 13.0 |
| NCCL busbw, GPU0-3 / GPU4-7 | 25.6 / 13.0 |
| NCCL busbw, pairs 0,1 / 4,5 | 25.2 / 25.1 |
| NCCL busbw, pairs 0,4 / 3,7 (cross socket) | 23.5 / 23.6 |

- NCCL: PyTorch 2.3 / NCCL 2.20.5, busbw at 1024 MiB, `P2P/IPC` in every run, every all-reduce `correct=True`; `NCCL_P2P_LEVEL=SYS` measured the same (within 0.2 GB/s).
- Unlike Milan, cross-socket P2P on Genoa runs at full speed in both directions, so the cross-socket pairs reach 23.5 GB/s (Milan: pair 3,7 19.2, pair 0,4 limited by its x8 GPU). The pair results match the 48 GB cards on the same platform ([Appendix B](#appendix-b-results-rtx-4090-48-gb)).
- The x8 GPU (GPU6) caps every set that contains it at about 13 GB/s (8 GPUs, GPU4-7), as on node05.

## Appendix B: Results, RTX 4090 48 GB

### Hardware and software

| Item | Value |
| :--- | :--- |
| Node | GPU Node 7 (cvgl-node07), ASUS ESC8000A-E12 |
| CPU / RAM | 2x EPYC 9554 (Genoa), 1.5 TiB |
| GPUs | 8x RTX 4090 48 GB modded (VBIOS 95.02.3C.00.02), BAR1 max 32 GiB; GPU0-3 on NUMA 0, GPU4-7 on NUMA 1, each on its own root port and host bridge |
| Links | Gen4 x16 slots (host max Gen5). GPU1 trained at Gen4 x8 in every run; in the stock baseline boot GPU0 was x8 too. |
| OS | Ubuntu 22.04, kernel 6.5.0-25-generic, Determined agent in Docker |
| Stock driver | 590.48.01 closed: `topo -p2p` `CNS`, BAR1 256 MiB |
| P2P driver | `nvidia-driver-610-open` 610.57.04 + `610.57.04-p2p-48g` @ `e896127` (UVM gate on), `amd_iommu=on iommu=pt` (on 6.5 `amd_iommu=on` only logs "Unknown option"), `uvm_disable_hmm=1`, ACS redirect **on** |
| Old stack | `determinedai/pytorch-ngc:0.38.1`: PyTorch 2.3, NCCL 2.20.5, CUDA 12.4 |
| New stack | PyTorch 2.9.1, NCCL 2.27.5 |

NCCL numbers are `torch.distributed.all_reduce` busbw at 1024 MiB, one process per GPU.

Columns C-E and the managed-memory and resident-memory results are from `e896127` (service validation: all default stages and `MANAGED=1` with `RESIDENT_GB=38`, both stacks). Copy bandwidth matches an earlier run with `d35aa39` (history of tag `610.57.04-p2p-48g-v1`) within 0.2 GB/s. `ordering` and `stale` passed (56 pairs for `stale`).

### aikitoria v3 alone

610.57.04 + aikitoria `610.57.04-p2p-v3`, `iommu=pt`, before Method 3: `topo -p2p` `OK`, BAR1 32 GiB but only 3 MiB used (static BAR1 off because 32 GiB < 48 GiB). `cudaDeviceEnablePeerAccess` failed with `mapping of buffer object failed` and every NCCL run with P2P crashed; with `NCCL_P2P_DISABLE=1` all runs passed (8 GPUs 10.0-10.1 GB/s). Cause: mailbox fallback without a mailbox area; `6e03400` reports such pairs as not supported, Method 3 makes them work.

### Host and peer copies

Host<->GPU, pinned memory on the GPU's socket (GB/s):

| GPU | Stock 590 H2D / D2H | P2P branch H2D / D2H |
| :--- | ---: | ---: |
| GPU0 | 13.4 / 13.5 (x8 that boot) | 26.8 / 26.4 |
| GPU1 (x8) | 13.3 / 13.5 | 13.3 / 13.2 |
| GPU2-7 | 26.8 / 27.1 | 26.8 / 26.4 |

GPU-to-GPU copies over all 56 ordered pairs (GB/s, min / mean / max):

| Mode | Same socket (24 pairs) | Cross socket (32 pairs) |
| :--- | :--- | :--- |
| Stock 590, host staged | 12.1 / 18.5 / 22.7 | 13.0 / 17.9 / 23.4 |
| P2P branch, peer access disabled (host staged) | 12.9 / 19.9 / 22.8 | 12.9 / 19.8 / 22.4 |
| P2P branch, unidirectional P2P | 13.2 / 23.1 / 26.4 | 13.2 / 23.1 / 26.4 |
| P2P branch, bidirectional P2P | 26.0 / 45.6 / 52.1 | 26.0 / 45.6 / 52.1 |

Pairs without GPU1 reach 26.3-26.4 (uni) and 52.1 (bi); pairs with GPU1 13.2 and 26.0. Same- and cross-socket are identical. Peer access 56/56 pairs; integrity 2016 blocks of 64 MiB over 12 regions per GPU, 0 bad words (store, load, copy). The copy test uses legacy peer access with 2 GiB buffers.

### NCCL all-reduce

busbw GB/s at 1024 MiB. Columns:
**A** stock 590, old stack (SHM; GPU0 and GPU1 x8);
**B** 610 + aikitoria v3, `NCCL_P2P_DISABLE=1` (SHM; same link widths as C-E: GPU1 x8, GPU0 x16);
**C** P2P branch, old stack default, 38 GiB resident per GPU (pair 2,3 from an earlier run without the resident tensor);
**D** P2P branch, new stack, `NCCL_P2P_LEVEL=SYS NCCL_LOCAL_REGISTER=0 NCCL_GRAPH_REGISTER=0`, 38 GiB resident per GPU;
**E** P2P branch, new stack default (HMM off), 38 GiB resident per GPU.

| GPUs | A | B | C | D | E | D / B |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| 0-7 | 10.6 | 10.1 | 13.0 (P2P/IPC) | 13.0 (P2P/CUMEM) | 10.2 (SHM) | 1.29x |
| 0-3 | 10.8 | 10.5 | 13.0 (P2P/IPC) | 13.0 (P2P/CUMEM) | 10.5 (SHM) | 1.24x |
| 4-7 | 21.1 | 20.4 | 25.7 (P2P/IPC) | 25.7 (P2P/CUMEM) | 20.4 (SHM) | 1.26x |
| 2,3 | 18.5 | 18.2 | 25.5 (P2P/IPC) | 25.1 (P2P/CUMEM) | 25.4 (P2P/CUMEM) | 1.38x |
| 0,4 | 8.7 | 16.0 | 23.4 (P2P/IPC) | 23.5 (P2P/CUMEM) | 23.4 (P2P/CUMEM) | 1.47x |
| 0,1 | 9.3 | 9.2 | 12.9 (P2P/IPC) | 12.9 (P2P/CUMEM) | 12.9 (P2P/CUMEM) | 1.40x |
| 4,5 | 18.3 | 18.2 | 25.0 (P2P/IPC) | 25.2 (P2P/CUMEM) | 25.2 (P2P/CUMEM) | 1.38x |
| 3,7 | 16.4 | 15.9 | 23.6 (P2P/IPC) | 23.6 (P2P/CUMEM) | 23.5 (P2P/CUMEM) | 1.48x |

- Every run with P2P printed `correct=True`. Column C with `NCCL_P2P_LEVEL=SYS` or `NCCL_CUMEM_ENABLE=1` gave the same numbers within ~4% (pair 0,4 with cuMem: 24.3; with cuMem the transport is `P2P/CUMEM`).
- Sets with GPU1 (0-7, 0-3) are capped at 13.0 by its x8 link; GPU4-7 shows what the x16 GPUs reach.
- Column E: the new stack picks P2P only for pairs; groups larger than two stay on SHM without `NCCL_P2P_LEVEL=SYS`.
- Gains are 1.24-1.29x for groups and 1.38-1.48x for pairs.

### Managed memory and resident memory

| Test | Result |
| :--- | :--- |
| `managedtest`, 56 ordered pairs, 256 MiB, `concurrentManagedAccess=1` | PASS: 0 bad words in every mode (fault, prefetch, memcpy, accessedby, atomic, oversub); dynamic pairs stage managed pages through host memory |
| 38 GiB resident tensor per GPU (peak 39.3 GiB), int32 pattern verified in 1 GiB chunks after the collectives | intact, collectives correct, on old stack (P2P/IPC, also with cuMem) and new stack (P2P/CUMEM) for 8 GPUs, GPU4-7 and pair 0,4 |

The kernel log stayed clean: no `METHOD3` errors, Xid or assertions.

### HMM and DMA32 evidence

Before `uvm_disable_hmm=1` (HMM on, `iommu=pt`): 8 `nvidia-uvm-hmm` regions at 0xfffa0c0000000-0xfffffffffffff in `/proc/iomem`; `/proc/zoneinfo` `Device` zones spanned 47.6 and 333.4 GiB; cuMem host allocations came from ZONE_DMA32 (about 1.6-2.2 GiB, node 0 only), failed on node 1 immediately and on node 0 after about 1 GiB; the kernel log showed `NV_ERR_NO_MEMORY` at `system_mem.c:353` / `mem_desc.c:1338`. The new stack's default 8-GPU run crashed with `Error while attaching to shared memory segment /dev/shm/nccl-<garbage> (size 0)`; with `NCCL_CUMEM_HOST_ENABLE=0` it ran (10.2 GB/s, SHM). After setting the option and rebooting, the same default run passed (column E above).

### Comparison with the blog

| | Blog Platform A | Blog Platform B | This cluster (48 GB node) |
| :--- | :--- | :--- | :--- |
| CPU | 2x Xeon Silver 4416+ | 1x EPYC 7302 (Rome) | 2x EPYC 9554 (Genoa) |
| GPUs | 8x 4090 48G | 4x 4090 48G | 8x 4090 48G, GPU1 at x8 |
| Driver / kernel | 595.71.05 / 6.8 | 595.71.05 / 6.8 | 610.57.04 / 6.5 |
| IOMMU / ACS redirect | IOMMU on / on | IOMMU on / off | `iommu=pt` / on |
| Peer copy per pair | 22.7 GB/s | 26.3 GB/s | 26.4 GB/s (13.2 with GPU1) |
| NCCL busbw without P2P (ours: column B, `NCCL_P2P_DISABLE=1`) | 4 GPUs 16.8, 8 GPUs 14.2 | 4 GPUs 4.1 | GPU4-7 20.4 (stock 21.1), GPU0-3 10.5 (stock 10.8), 8 GPUs 10.1 (stock 10.6) |
| NCCL busbw with P2P (blog and ours: `NCCL_P2P_LEVEL=SYS`; ours: column D) | 4 GPUs 20.4, 8 GPUs 20.46 | 4 GPUs 25.15 (6.1x) | GPU4-7 25.7 (1.26x), GPU0-3 13.0, 8 GPUs 13.0 (1.29x) |
| DDP step time | 8 GPUs 259.5 -> 153.4 ms (1.7x) | 4 GPUs 1203.7 -> 109.5 ms (11x) | not measured |

The tools differ (blog: `p2pcheck.cu` with 256 MB `cudaMemcpyPeer`, nccl-tests `all_reduce_perf`; here: `p2ptest.cu`, `torchrun` all-reduce, one process per GPU), so the numbers are indicative. The differences follow from what was measured:

- **Host staging speed decides the gain.** On Genoa host-staged copies already reach about 22 GB/s and SHM all-reduce 20-21 GB/s on four x16 GPUs, so P2P adds 1.24-1.47x. On Rome (blog) and Milan ([Appendix A](#appendix-a-results-rtx-4090-24-gb)) SHM all-reduce stays at 4-5 GB/s; P2P gave 6.1x on Rome and 2.8-24x on Milan.
- **GPU1 at x8** caps our 8-GPU and GPU0-3 rings at 13.0 GB/s, below the blog's 8-GPU 20.46; this is the link, not P2P.
- **Same line-rate ceiling.** Per pair P2P reaches the Gen4 x16 limit on both AMD platforms (26.4 vs 26.3 GB/s).
- **ACS** did not limit P2P here with `iommu=pt` (nor on the blog's Platform A); duanyll's validation logs measured about 3-9 GB/s with ACS on and without `iommu=pt`.

## Appendix C: References

- tinygrad P2P modules: <https://github.com/tinygrad/open-gpu-kernel-modules>
- aikitoria P2P branches: <https://github.com/aikitoria/open-gpu-kernel-modules>
- duanyll Method 3 fork: <https://github.com/duanyll/open-gpu-kernel-modules> (branches `*-p2p-48g`)
- duanyll blog, "48G 4090 P2P" (Chinese): <https://github.com/duanyll/duanyll.com-hexo/blob/master/source/_posts/tech/2026-7-13-4090-48G-P2P.md>
- Our fork: <https://github.com/LingzheZhao/open-gpu-kernel-modules>, branches [`610.57.04-p2p-48g`](https://github.com/LingzheZhao/open-gpu-kernel-modules/tree/610.57.04-p2p-48g) and [`610.57.04-p2p-fixes`](https://github.com/LingzheZhao/open-gpu-kernel-modules/tree/610.57.04-p2p-fixes) on aikitoria `461d638`
- NCCL SHM/cuMem host bug: <https://github.com/NVIDIA/nccl/issues/2190>, proposed fix <https://github.com/NVIDIA/nccl/pull/2388>
- Related driver issue (memory-only NUMA nodes): <https://github.com/NVIDIA/open-gpu-kernel-modules/issues/885>
- Scripts and tests: [`scripts/gpu-p2p/`](../scripts/gpu-p2p/README.md)
