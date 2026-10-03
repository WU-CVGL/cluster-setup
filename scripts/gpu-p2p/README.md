# GPU P2P tools (GeForce)

Scripts to install, verify, test and roll back P2P-patched NVIDIA open kernel modules on a GPU node. The procedure (including building the modules), the background and the measured results are in [docs/05_GPU_P2P_GeForce.md](../../docs/05_GPU_P2P_GeForce.md#procedure). Placeholders as in [docs/05](../../docs/05_GPU_P2P_GeForce.md#introduction): `<K>` kernel, `<N>` driver branch, `<version>` driver version, `<fork>` absolute path of the built fork tree, `<image>` CUDA + PyTorch image; here also `<n>` resident GiB per GPU (VRAM minus a few GiB) and `<label>` log name.

## Contents

- [GPU P2P tools (GeForce)](#gpu-p2p-tools-geforce)
  - [Contents](#contents)
  - [Files](#files)
  - [Install and verify](#install-and-verify)
  - [Test](#test)
    - [Managed memory and the UVM BAR1 fix](#managed-memory-and-the-uvm-bar1-fix)
    - [Compressible memory](#compressible-memory)
    - [Expected output](#expected-output)
    - [Settings](#settings)
    - [Building and running the tests by hand](#building-and-running-the-tests-by-hand)
  - [ACS](#acs)
  - [Roll back](#roll-back)

## Files

| File | Runs as | Purpose |
| --- | --- | --- |
| `install-p2p-modules.sh` | root | Disables UVM HMM, sets `iommu=pt` in GRUB, holds the driver packages, then installs the patched modules into `/lib/modules/<K>/updates/p2p` next to the Ubuntu DKMS build and adds the depmod override. `--restore` undoes it. Never reboots. |
| `verify.sh` | user | Checks after the reboot: open module and version, loaded modules are the P2P build, BAR1 resized, `iommu=pt` and identity IOMMU domains, HMM off, `nvidia-smi topo -p2p r` all `OK`, PCIe link width, no Xid in the kernel log, package hold, `unattended-upgrades` purged. `PASS`/`FAIL`/`WARN`/`SKIP` per line, exit 1 on any `FAIL`. |
| `acs-redir.sh` | root | `status`, `off`, `restore` of the PCIe ACS redirect bits on the bridges above the GPUs at runtime; `status` prints the equivalent kernel parameter. |
| `tests/p2ptest.cu` | user | Peer access matrix; integrity of peer stores, peer loads and copy-engine copies over 12 regions of each GPU's buffer; copy bandwidth with peer access disabled and enabled (uni- and bidirectional), summarized same-socket vs cross-socket; host-GPU bandwidth per GPU. |
| `tests/ordering.cu` | user | Peer stores into GPU B, `__threadfence_system()`, then a sequence flag in host memory (CPU waits, then checks on B), in B's memory (control) or in a third GPU's memory; a stale word fails. |
| `tests/stale.cu` | user | GPU A reads B's buffer, B overwrites it (kernel or copy engine) and signals an event, A waits and must re-read the new data; every ordered pair. |
| `tests/atomics.cu` | user | **Opt-in** (`ATOMICS=1`), a diagnostic run, not part of service validation: P2P attribute matrix (`AccessSupported`/`NativeAtomicSupported`); `atomicAdd` on one counter in B's memory from B alone (control), each peer alone, all at once. Lost increments without native P2P atomics are `INFO`, not a failure ([atomics](#expected-output)). |
| `tests/managedtest.cu` | user | **Opt-in, crash-risky** (`MANAGED=1`): `cudaMallocManaged` per ordered GPU pair, modes `fault`, `prefetch`, `memcpy`, `accessedby` (peer reads and writes), `atomic` (peer-only `atomicAdd` on owner-resident pages), `oversub` (1.25x a GPU's free memory, migrated back and forth). `--pair a,b`, `--quick` (2 MiB), `--modes`, `--mib`. Faults all GPUs on static-BAR1 cards without the UVM BAR1 fix ([managed memory](#managed-memory-and-the-uvm-bar1-fix)). |
| `tests/compress.cu` | user | **Opt-in, host-RAM hazard** (`COMPRESS=1`): peer read and write of compressible `cuMemCreate` memory; per pair `MAPPED`, `REFUSED` (by a driver with the RM fix) or `SKIP` (not reached: no compression support, no peer access). Refuses without `--allow-host-ram-risk`, and when host RAM extends above 1 TiB ([compressible memory](#compressible-memory)). |
| `tests/hostnuma.cu` | user | `cuMemCreate` pinned host memory per NUMA node (what NCCL's SHM transport uses): `sizes` mode from every GPU, `cumulative` mode holds 1 GiB blocks per node. Catches the HMM + `iommu=pt` DMA32 issue. |
| `tests/nccl_allreduce.py` | user | `torch.distributed` all-reduce: busbw per size, correctness, optional resident tensor (`RESIDENT_GB`) checked after the collectives. |
| `tests/run_tests.sh` | in container | Builds the tests and runs the stages p2ptest → ordering → stale → hostnuma → NCCL (GPU-set × NCCL-setting matrix), then the opt-in stages atomics (`ATOMICS=1`), managedtest (`MANAGED=1`) and compress (`COMPRESS=1`), stopping at the first failing stage. |
| `tests/run_host.sh` | user (docker) | Starts `run_tests.sh` in a CUDA + PyTorch container, logs the UVM managed-memory mode, samples the PCIe links, checks the kernel log, writes `<label>.log`. Refuses `MANAGED=1` on static-BAR1 cards whose driver lacks the UVM BAR1 fix. |

## Install and verify

From this directory, after steps 1-3 of [docs/05](../../docs/05_GPU_P2P_GeForce.md#procedure) (node disabled and drained in Determined, matching open driver installed, fork built):

```bash
sudo SRC=<fork>/kernel-open ./install-p2p-modules.sh install   # K=<K> V=<version> optional
sudo systemctl reboot
nvidia-modprobe -u -c 0     # loads nvidia_uvm (otherwise loaded on first CUDA use) so verify.sh can check it
./verify.sh                                                     # no root needed
```

After the reboot `det agent list` must still show the node as disabled; enable it only after [docs/05 step 6](../../docs/05_GPU_P2P_GeForce.md#6-run-the-tests) passed (step 7 there). If `verify.sh` reports `nvidia_modeset` or `nvidia_drm` not loaded (a headless node), run `sudo modprobe nvidia_drm` and run it again.

- Refusals and the order of changes: [docs/05 step 4](../../docs/05_GPU_P2P_GeForce.md#4-install-the-modules); all options: `./install-p2p-modules.sh --help`.
- **State** in `/var/backups/nvidia-p2p/`: the original `/etc/default/grub` (`grub.pre-p2p`, taken once), the GRUB parameters the script added (`grub-added-params.txt`), the packages it held (`held-packages.txt`), package lists and replaced modules. `--restore` uses the two lists.
- **Per kernel**: the override applies to `<K>` only; after a kernel upgrade the node boots the stock DKMS modules (safe, but without P2P) until the modules are rebuilt and installed with `K=<new kernel>`.
- `verify.sh` prints BAR1 vs VRAM but cannot tell whether static BAR1 is actually on; use the [hold test](../../docs/05_GPU_P2P_GeForce.md#static-or-dynamic-how-to-tell) (it also loads `nvidia_uvm`; run it before `verify.sh`). It does not check the UVM gate either: `/sys/module/nvidia_uvm/parameters/uvm_bar1_p2p_managed` must exist and be `0`.
- `verify.sh` ends with `RESULT: PASS` when everything is in place. `WARN` lines do not fail it: a narrow PCIe link (such a GPU caps every transfer and NCCL ring through it), `METHOD3` lines in the kernel log (dynamic BAR1 window errors: investigate before enabling the node), a missing package hold. The kernel-log check needs the `adm` or `systemd-journal` group, otherwise it prints `SKIP`.

## Test

The tests must run on an **idle node**: `p2ptest` allocates nearly all free GPU memory, and the bandwidth numbers are meaningless with other load. `run_host.sh` refuses to start while any GPU has a compute process (`FORCE=1` overrides).

```bash
cd tests
export OUT_DIR=~/p2p-logs                                    # default: the current directory
MANAGED=1 RESIDENT_GB=<n> ./run_host.sh <image> service      # service validation (after the quick run of docs/05 step 6)
./run_host.sh <image> after-p2p                              # writes $OUT_DIR/after-p2p.log; no opt-in stages
ATOMICS=1 STAGES=atomics ./run_host.sh <image> atomics       # diagnostic, not a service run
KEEP_GOING=1 NCCL_VARIANTS="default no-p2p" ./run_host.sh <image> baseline   # before installing P2P
```

Service validation of a node is the default stages plus `MANAGED=1` with the UVM gate on, and `RESIDENT_GB` a few GiB below VRAM (e.g. 20 on 24 GB cards, 38 on 48 GB cards). `managedtest`, `atomics` and `compress` do not run by default. `atomics` is a diagnostic: any Xid while it runs fails the kernel-log check, so it stays out of service validation. `managedtest` is safe with the gate on all three card types; on static-BAR1 cards without the fix it faults all GPUs (next section).

The image needs `nvcc` and `torchrun` (for example an NGC PyTorch image). The container runs with `--init --gpus all --ipc=host --ulimit memlock=-1`, the tests directory is mounted read-only and the binaries are built inside the container. After `TIMEOUT` the container is stopped and removed.

GPU numbering follows `nvidia-smi` (`CUDA_DEVICE_ORDER=PCI_BUS_ID`). By default the NCCL stage runs on all GPUs, the GPUs of each NUMA node, one same-socket pair per node and two cross-socket pairs, each with NCCL defaults (`default`) and with `NCCL_P2P_LEVEL=SYS NCCL_LOCAL_REGISTER=0 NCCL_GRAPH_REGISTER=0` (`p2p-sys`). The `p2p-sys` runs fail if NCCL does not use a P2P transport; the `default` runs may use SHM on newer NCCL.

### Managed memory and the UVM BAR1 fix

On static-BAR1 GPUs (BAR1 >= VRAM: the RTX 3090 and RTX 4090 24 GB) cross-GPU managed memory faults all GPUs with drivers that lack the UVM BAR1 fix: observed on the RTX 4090 24 GB (Xid 31 `FAULT_UNSUPPORTED_APERTURE`, then Xid 154; only a reboot recovers), and expected on the RTX 3090, which uses the same pre-Hopper UVM code but was not tested without the fix ([why](../../docs/05_GPU_P2P_GeForce.md#managed-memory-uvm)). The fixed driver has the `nvidia_uvm` module parameter `uvm_bar1_p2p_managed`: `0` (default, the gate) keeps managed memory of BAR1 peers off direct peer access, so managed pages stage through host memory; `1` (opt-in, validated per platform and card type in an opt-in window; results for the RTX 4090 24 GB: [docs/05 Appendix A](../../docs/05_GPU_P2P_GeForce.md#opt-in-uvm_bar1_p2p_managed1)) lets UVM map and copy them over BAR1 with the new page-table and copy-engine encodings. `run_host.sh` logs which mode is active and refuses `MANAGED=1` whenever the parameter is missing (`MANAGED_FORCE=1` overrides): on static-BAR1 cards the test would fault all GPUs, and on dynamic-BAR1 cards duanyll's Method 3 without this fork's dynamic-pair guard would address host RAM. Run `managedtest` first with `MANAGED_ARGS="--quick --pair 0,1"` (2 MiB, one pair, no `oversub`), then in full.

Returning a static-BAR1 node to service needs only the gate (step 1); the opt-in is a separate window (steps 2-4):

1. **Gate validation** (the node stays out of service until it passes):
   1. Install the fixed modules and reboot; do not set `uvm_bar1_p2p_managed` (default `0`). `verify.sh` passes.
   2. Run the two commands of [docs/05 step 6](../../docs/05_GPU_P2P_GeForce.md#6-run-the-tests) (quick pair first, then the service run with `MANAGED=1 RESIDENT_GB=<n>`).
   3. Both logs end with `OVERALL: PASS` and `KERNEL LOG: PASS` (no Xid, no assert), and the log header says `gate on`. Then the node can be enabled. The header reflects only the value of `uvm_bar1_p2p_managed`. A `managedtest` PASS shows that managed memory works without an Xid; it cannot show whether pages staged through host memory or went over a direct BAR1 peer mapping, since both give correct data. That the gate keeps static pre-Hopper pairs off direct peer access rests on review of the driver predicate `uvm_parent_gpus_bar1_managed_unsupported()`.
2. **Opt-in window** (node disabled in the scheduler, a reboot is acceptable): set the option in a file of its own, `echo 'options nvidia-uvm uvm_bar1_p2p_managed=1' | sudo tee /etc/modprobe.d/nvidia-uvm-bar1-optin.conf` (do not edit `nvidia-uvm-hmm.conf`: the installer rewrites it and `--restore` deletes it), preferably with the UVM debug build (`make modules ... UVM_BUILD_TYPE=debug`, installed like the release build), reboot, and run in this order. Instead of a reboot, `nvidia-uvm` alone can be swapped and reloaded (copy its `nvidia-uvm.ko` into `updates/p2p`, `depmod -a`, `modprobe -r nvidia_uvm && modprobe nvidia_uvm`) when no process holds `/dev/nvidia-uvm` (stop GPU jobs, the scheduler agent and DCGM first) and the loaded `nvidia.ko` comes from the same tree (same `srcversion`). The debug and release `nvidia-uvm.ko` have the same `srcversion`: tell them apart by file hash.
   1. `MANAGED=1 MANAGED_ARGS="--quick --pair 0,1 --modes accessedby" STAGES=managedtest ./run_host.sh <image> optin-pte`. This tests the page-table encoding alone (peer remote mappings through `SetAccessedBy`, no peer copy-engine copies). It is not a lower-risk run: a wrong peer address can reach host memory.
   2. The quick and the service command of docs/05 step 6 (labels e.g. `optin-quick`, `optin-full`). With the debug build, full-size runs of `accessedby` are very slow: use `--quick` there and run the full size with the release build (step 4a).

   Any Xid or `Assert failed` line ends the window: remove the option and reboot, then close the window with step 4b. The option only affects static-BAR1 pairs whose DMA window is 2MB aligned: dynamic pairs (BAR1 < VRAM) and misaligned windows always stage through host memory, and the log says so (header, and a `NOTE:` after the kernel-log summary). Leave `uvm_peer_copy` at its default (`phys`): steps 1 and 2 do not cover the virtual peer-copy mode and its peer identity mapping for BAR1 peers.
3. **Virtual peer copy (optional)**, after step 2 passed, same window: `options nvidia-uvm uvm_bar1_p2p_managed=1 uvm_peer_copy=virt` on the same line of `nvidia-uvm-bar1-optin.conf`, the UVM debug build, reboot, then the quick and the service command as in step 2 (the `prefetch` and `memcpy` modes make peer copies). It covers the virtual peer-copy addressing and the peer identity mapping over BAR1 (Ampere and Ada default to `phys`). With a BAR1 window aligned to the largest page size (check the DMA base in the kernel log) it does not exercise the page-size reduction for misaligned windows.
4. **Close the window**, one of:
   - a. **Keep the opt-in** (step 2 passed): keep `nvidia-uvm-bar1-optin.conf` with `uvm_bar1_p2p_managed=1` only (no `uvm_peer_copy`), reinstall the release build (`sudo SRC=<fork>/kernel-open ./install-p2p-modules.sh install`, `<fork>` the release tree), reboot, check that `/sys/module/nvidia_uvm/parameters/uvm_bar1_p2p_managed` is `1` and that `updates/p2p/SOURCE` names the release tree, then run the service command of docs/05 step 6 again with the release build (header `OPT-IN window`, `OVERALL: PASS`, `KERNEL LOG: PASS`) before enabling the node.
   - b. **Back to the gate**: `sudo rm /etc/modprobe.d/nvidia-uvm-bar1-optin.conf`, reinstall the release build (`sudo SRC=<fork>/kernel-open ./install-p2p-modules.sh install`, `<fork>` the release tree), reboot, check that `/sys/module/nvidia_uvm/parameters/uvm_bar1_p2p_managed` is `0` and that `updates/p2p/SOURCE` names the release tree, then repeat the gate validation (step 1) before enabling the node.

### Compressible memory

`compress` maps compressible memory (`CU_MEM_ALLOCATION_COMP_GENERIC`) of GPU B into GPU A. Without the RM fix for compressible peer allocations the peer page-table entry can point at a wrong physical address, for a peer BAR1 at `0x0100_0000_0000` + x at 1 TiB + x, which is host RAM on a node whose RAM extends above 1 TiB. Both fork branches have the fix (`c811a9c`, and `37d11f9` for dynamic windows). Without it, run the test only on nodes whose RAM ends below 1 TiB; on nodes with more RAM only with the fix installed (`COMPRESS_ARGS=--above-1tib`). The program refuses without `--allow-host-ram-risk` (`COMPRESS=1` passes it) and, in addition, without `--above-1tib` when System RAM ends above 1 TiB. The RAM end comes from the firmware memory map (`/sys/firmware/memmap`, readable without root; docker masks it, so `run_host.sh` reads it on the host and passes `HOST_RAM_END`); when neither is available, `MemTotal` of 960 GiB or more (or unknown) refuses, since a node with 1 TiB of DIMMs reports less than 1024 GiB while the PCI and HyperTransport holes push part of its RAM above 1 TiB. It writes from A only after A read B's data correctly.

Before passing `--above-1tib`, confirm the installed build has the fix: take the commit from `/lib/modules/<K>/updates/p2p/SOURCE` (line `source: ... @ <commit>`) and, in a full clone of the fork (the build clone of docs/05 step 3 is shallow), check that it descends from `c811a9c` and `37d11f9` (`git merge-base --is-ancestor c811a9c <commit>`, same for `37d11f9`; branch `610.57.04-p2p-48g`) or from `542395e` (history of tag `610.57.04-p2p-48g-v1`; static and dynamic windows in one commit). On `610.57.04-p2p-fixes` builds (static BAR1 only) `c811a9c` suffices. If there is no commit or the check fails, do not pass `--above-1tib`.

```bash
COMPRESS=1 STAGES=compress ./run_host.sh <image> compress            # pair 0,1
COMPRESS=1 COMPRESS_ARGS=--all STAGES=compress ./run_host.sh <image> compress-all
```

Each pair ends as one of:

- `MAPPED`: A's peer mapping works, reads and writes verified (a driver with the RM fix maps it uncompressed on A).
- `REFUSED`: `cuMemSetAccess` for A returns `CUDA_ERROR_NOT_SUPPORTED`. The RM fix refuses a static-BAR1 peer mapping when the owner's static BAR1 does not map the allocation with its compressible kind (the kernel log has a `BAR1P2P: compressible kind ... peer mapping not supported` line). On static-BAR1 cards (24 GB) this is the expected outcome, unless the static BAR1 already carries the allocation's kind (pages of 2 MB or more), in which case the pair is `MAPPED`.
- `SKIP`: no compression support, no VMM or peer access, or the allocation was not made compressible; the peer-mapping path was not reached.
- `FAIL`: wrong data, or any other `cuMemSetAccess` error. A refusal that surfaces as another CUDA error also fails the pair; if the RM refusal line is in the kernel log, extend the `CUDA_ERROR_NOT_SUPPORTED` check in `compress.cu` to that error.

The program ends with `RESULT: PASS (m mapped, r refused by driver, s skipped)` when at least one pair was mapped or refused and none failed, `RESULT: FAIL` on any failure, and `RESULT: SKIP` (exit 4, `STAGE compress: SKIP`) when every pair skipped. The RM fix is validated on a static-BAR1 node whose RAM ends below 1 TiB by `COMPRESS_ARGS=--all` ending with `RESULT: PASS`, no `FAIL` and no Xid; `MAPPED` pairs are not required there. `STAGE compress: SKIP` does not validate it.

### Expected output

A passing run ends each stage with `STAGE <name>: PASS` and the log with `OVERALL: PASS`. Key lines (8 GPUs on two sockets; bandwidth numbers depend on the platform):

```text
===== p2ptest
...
Integrity: 2016 blocks of 64 MiB over 12 regions per GPU (...): bad words store=0 load=0 copy=0
Unidirectional copy, peer access ENABLED (GB/s)
  same socket : min ...  mean ...  max ... GB/s  (24 ordered pairs)
  cross socket: min ...  mean ...  max ... GB/s  (32 ordered pairs)
RESULT: PASS (56 of 56 ordered pairs have peer access, 0 failed to enable it, integrity clean)
STAGE p2ptest: PASS
===== ordering
flag in host iterations 200: stale words 0, timeouts 0, checksum ... expected ...  PASS
flag in B    iterations 200: stale words 0, timeouts 0, checksum ... expected ...  PASS
flag in C    iterations 200: stale words 0, timeouts 0, checksum ... expected ...  PASS
RESULT: PASS
===== stale
  A=0 B=1 sm: first read bad 0, re-read stale 0, checksum ... expected ...  ok
  A=0 B=1 ce: first read bad 0, re-read stale 0, checksum ... expected ...  ok
RESULT: PASS (56 pairs, 0 with mismatches)
===== hostnuma
RESULT: PASS (96 host allocations)                 # 8 GPUs x 2 nodes x 3 sizes x 2 handle types
RESULT: PASS (8 GiB held on each of 2 NUMA nodes)
===== nccl
== [p2p-sys] GPUs 0,1,2,3,4,5,6,7 (NCCL_P2P_LEVEL=SYS NCCL_LOCAL_REGISTER=0 NCCL_GRAPH_REGISTER=0)
   all_reduce  1024 MiB x8:   ... ms  algbw ... GB/s  busbw ... GB/s  correct=True
   RESULT: PASS
   transports:  16 via P2P/CUMEM;              # "via P2P/IPC" with older NCCL; "via SHM" = no P2P
STAGE nccl: PASS
===== atomics                                         # ATOMICS=1 only
  GPU0 alone (control)                     got ... expected ... lost ...%  ok
  GPU1 alone (peer)                        got ... expected ... lost ...%  ...
  all at once: GPU1 GPU2 + GPU0            got ... expected ... lost ...%  ...
RESULT: PASS ...
===== managedtest                                     # MANAGED=1 only
WARNING: managed memory across GPUs. ...
mode fault      pairs 56: bad words 0 checksum ... expected ...  PASS
...
mode oversub    pairs 1: bad words 0 checksum ... expected ...  PASS
RESULT: PASS
OVERALL: PASS
```

The `ordering`, `stale`, `atomics` and `managedtest` lines show the output format, not measured results.

The RTX 4090 has no native P2P atomics (`NativeAtomicSupported` = 0 in the `atomics` matrix; not measured on the RTX 3090): an `atomicAdd` from one GPU on another GPU's memory is not atomic with respect to other GPUs, so concurrent adds lose increments. This is expected and only `INFO`; jobs must not rely on cross-GPU atomics on peer memory. A count above the expected value, a lossy control, or a loss with `NativeAtomicSupported` = 1 fail.

Failure signatures:

| Output | Meaning |
| --- | --- |
| `cudaDeviceEnablePeerAccess i->j failed: ...` | Peer access advertised but not usable. On GPUs with BAR1 < VRAM: the p2ptest buffer did not fit into BAR1 (set `P2PTEST_BUF_GB`). |
| `bad words store=N` / `MISMATCH i->j` | Data corruption over P2P: do not put the node back into service. |
| `hostnuma` failures `CUDA_ERROR_OUT_OF_MEMORY` on NUMA node 1, node 0 stops after 1-2 GiB | UVM HMM still on with `iommu=pt` (`verify.sh` shows it); NCCL's SHM transport breaks. |
| `transports: ... via SHM` in `p2p-sys` | NCCL did not use P2P. |
| `stale words N` (ordering) / `re-read stale N` (stale) | Peer writes not ordered by `__threadfence_system()`, or stale peer data after an event wait: do not put the node back into service. |
| Xid 31 with an `ATOMIC` fault type during `atomics` | Peer atomics are disabled in the page tables: a finding, not a regression. `atomics` is opt-in (`ATOMICS=1`) for this reason; run it outside service validation. |
| Xid 31 `FAULT_UNSUPPORTED_APERTURE`, then Xid 154 during `managedtest` | Managed memory over static BAR1 without the UVM BAR1 fix (or a bug in the opt-in path): reboot, see [managed memory](#managed-memory-and-the-uvm-bar1-fix). |
| `STAGE managedtest: SKIP` | `managedtest` listed in `STAGES` without `MANAGED=1` (same for `atomics` and `ATOMICS=1`, `compress` and `COMPRESS=1`). |
| `STAGE compress: SKIP` with `COMPRESS=1` | Every pair skipped (`compress` exit 4): no compression, no peer access, or no compressible allocation; the RM fix was not exercised. The run still ends with `OVERALL: PASS (skipped stages: compress)`. |
| `REFUSED: host RAM extends above 1 TiB` (or `cannot determine where host RAM ends`), then `STAGE compress: FAIL` | `compress` exit 3 by design: `COMPRESS=1` on a node whose System RAM ends above 1 TiB, or whose RAM end is unknown with `MemTotal` of 960 GiB or more. Nothing was tested. Run it there only with a driver that has the RM fix for compressible peer allocations, confirmed from `updates/p2p/SOURCE` as in [compressible memory](#compressible-memory), and pass `COMPRESS_ARGS=--above-1tib`. |
| `NOTE: a static BAR1 DMA window is not 2MB aligned` | UVM keeps that pair off managed-memory peer access (it stages through host memory), also with `uvm_bar1_p2p_managed=1`: the opt-in was not exercised for it. Not a failure. |
| `KERNEL LOG: FAIL (Xid or assert)` | See the kernel log lines above it. `Assert failed` lines come from UVM release asserts: the driver only logs them by default (`uvm_release_asserts_set_global_error=0`), but the run fails on them; investigate before enabling the node. |

### Settings

Environment variables of `run_host.sh` (passed into the container) and `run_tests.sh`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `STAGES` | `p2ptest ordering stale hostnuma nccl`, then `atomics` with `ATOMICS=1`, `managedtest` with `MANAGED=1`, `compress` with `COMPRESS=1` | Stages to run, in order. `atomics`, `managedtest` and `compress` are skipped without their opt-in variable. |
| `ATOMICS` | `0` | `1`: run `atomics` (diagnostic, not part of service validation). |
| `MANAGED` | `0` | `1`: run `managedtest` (opt-in; [managed memory](#managed-memory-and-the-uvm-bar1-fix)). |
| `MANAGED_ARGS` | none | Options of `managedtest`, e.g. `--quick --pair 0,1`, `--modes fault,prefetch`, `--mib 64`. |
| `COMPRESS`, `COMPRESS_ARGS` | `0`, none | `1`: run `compress` with `--allow-host-ram-risk` ([compressible memory](#compressible-memory)); options e.g. `--all`, `--pair 1,0`. |
| `HOST_RAM_END` | from the host's `/sys/firmware/memmap` (`run_host.sh`) | End of System RAM (hex) for the `compress` RAM check; docker masks `/sys/firmware` in the container. |
| `KEEP_GOING` | `0` | `1`: run all stages and NCCL runs despite failures (baseline measurements). |
| `P2PTEST_BUF_GB` | all free memory − 2 GiB; `2` if 256 MiB < BAR1 < VRAM | Per-GPU buffer of `p2ptest`. Legacy whole-device peer access maps every allocation into the peer's BAR1, so with BAR1 < VRAM the buffer must fit into BAR1. |
| `HOSTNUMA_GB` | `8` | GiB held per NUMA node by `hostnuma cumulative`. |
| `GPU_SETS` | from the NUMA topology | Space-separated `CUDA_VISIBLE_DEVICES` lists, e.g. `"0,1,2,3,4,5,6,7 0,4"`. |
| `NCCL_VARIANTS` | `default p2p-sys` | Any of `default`, `p2p-sys`, `no-p2p` (`NCCL_P2P_DISABLE=1`). |
| `RESIDENT_GB` | `0` | GiB per GPU held with a known pattern during the NCCL runs and checked afterwards. |
| `SIZES_MB`, `ITERS` | `64,256,1024`, `10` | All-reduce sizes and timed iterations. |
| `CUDA_ARCH` | GPU0's compute capability | `nvcc -arch`, e.g. `sm_89` (RTX 4090) or `sm_86` (RTX 3090); read with `nvidia-smi --query-gpu=compute_cap`. If that fails, `run_tests.sh` uses `sm_89`, which does not run on the RTX 3090: set `CUDA_ARCH=sm_86` there. |
| `BUILD_DIR` | `/tmp/gpu-p2p-build` | `run_tests.sh` only (`run_host.sh` does not pass it): build directory of the test binaries inside the container. |
| `TIMEOUT_NCCL` | `600` | Seconds per `torchrun`. |
| `OUT_DIR`, `TIMEOUT`, `FORCE` | `.`, `10800`, `0` | `run_host.sh` only: log directory, container timeout in seconds, skip the idle check. |
| `GPUS` | all | `run_host.sh` only: comma-separated GPU indices or UUIDs to test, e.g. to leave out a faulty GPU. Only these GPUs go into the container (`docker --gpus device=...`, where CUDA renumbers them from 0) and into the idle check, BAR1 and link sampling. |
| `MANAGED_FORCE` | `0` | `run_host.sh` only: `1` runs `MANAGED=1` even when the `uvm_bar1_p2p_managed` parameter is missing (static BAR1: expect a reboot; dynamic BAR1 without the guard: host RAM corruption). |

### Building and running the tests by hand

Inside any container or host with the CUDA toolkit (the GPU numbering then follows CUDA's default unless `CUDA_DEVICE_ORDER=PCI_BUS_ID` is set):

```bash
arch=sm_$(nvidia-smi -i 0 --query-gpu=compute_cap --format=csv,noheader | tr -d .)   # sm_89 on the RTX 4090, sm_86 on the RTX 3090
for t in p2ptest ordering stale atomics managedtest; do nvcc -O2 -arch=$arch -o $t $t.cu; done
nvcc -O2 -arch=$arch -o compress compress.cu -lcuda
nvcc -O2 -o hostnuma hostnuma.cu -lcuda
P2PTEST_BUF_GB=2 ./p2ptest
./ordering --gpus 0,1,2 && ./stale --pair 0,1 && ./atomics --owner 0 --peers 1,2
./managedtest --quick --pair 0,1   # crash-risky: see "Managed memory and the UVM BAR1 fix"
./compress --allow-host-ram-risk   # host-RAM hazard: see "Compressible memory"
./hostnuma sizes && ./hostnuma cumulative 8
NCCL_DEBUG=INFO NCCL_P2P_LEVEL=SYS CUDA_VISIBLE_DEVICES=0,1,2,3 \
    torchrun --standalone --nproc_per_node=4 nccl_allreduce.py
```

Every program ends with a `RESULT: PASS|FAIL` line (`compress` also `SKIP`, exit 4) and exits non-zero on failure; the data checks print bad words and a checksum (sum of the words read vs expected).

## ACS

When and why: [docs/05 ACS redirect](../../docs/05_GPU_P2P_GeForce.md#acs-redirect).

```bash
sudo ./acs-redir.sh status     # also prints the persistent pci=disable_acs_redir=pci:<vendor>:<device>
sudo ./acs-redir.sh off        # saves the old values in /root/acs-redir-saved.txt
sudo ./acs-redir.sh restore
```

`off` refuses unless every GPU is in an identity IOMMU domain (`iommu=pt`) or has no IOMMU group (`ACS_FORCE=1` overrides). `ACS_STATE=<file>` replaces `/root/acs-redir-saved.txt`. A reboot also restores the kernel's defaults.

## Roll back

Procedure: [docs/05 Rollback](../../docs/05_GPU_P2P_GeForce.md#rollback).

```bash
sudo ./install-p2p-modules.sh --restore       # K=<K> for another kernel than the running one
sudo systemctl reboot
```

This removes `updates/p2p` of the kernel and its depmod override. While other kernels still have P2P modules, the global settings stay. Otherwise it also removes the GRUB parameters listed in `grub-added-params.txt` and unholds the packages in `held-packages.txt`. It removes the HMM setting only when `iommu=pt` is no longer on the GRUB command line: keep `/etc/modprobe.d/nvidia-uvm-hmm.conf` as long as `iommu=pt` stays ([why](../../docs/05_GPU_P2P_GeForce.md#hmm-breaks-host-cumem-allocations-under-iommu-passthrough)). It does not remove a `pci=disable_acs_redir=...` parameter or `/etc/modprobe.d/nvidia-uvm-bar1-optin.conf`.

Without `grub-added-params.txt` (a node set up by hand, or by an older version of this script) `--restore` leaves GRUB unchanged and says so; without `held-packages.txt` it leaves the package holds unchanged. Remove the parameters from `/etc/default/grub` by hand if wanted, run `update-grub`, then remove the HMM setting; release holds with `apt-mark unhold`.
