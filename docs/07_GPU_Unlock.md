# GPU Tensor Unlock

This guide covers the tensor-speed selector unlock for RTX 3090 and RTX 4090 GPUs on Ubuntu 22.04 with `initramfs-tools`. The unlock module must finish its firmware transaction and device reset before NVIDIA initializes each selected GPU.

## Sources

Use the source matching the GPU and pin the revision used to build the packages.

| GPU | Source |
| --- | --- |
| RTX 3090 | [Zagitta/gpu-unlock 1.0.0](https://github.com/Zagitta/gpu-unlock/tree/af28cbbfd3d922a659227ecfb7369adfd924cf03) |
| RTX 4090, including modified 48 GB cards | [JustAResearcher/gpu-unlock-ada 1.1.1](https://github.com/JustAResearcher/gpu-unlock-ada/tree/d28c485418c3d9d034cd4bac19200bc33dd3453b) |

Follow the selected upstream build instructions and retain firmware input checksum verification. Use the Python and Debian packaging dependencies required by that revision.

## Install

Stop GPU workloads, record a clean baseline and NVIDIA module hashes, and confirm recovery-console access. Back up the boot configuration and bootable image:

```bash
unlock_kernel=$(uname -r)
unlock_backup="/var/backups/gpu-unlock-$(date +%Y%m%d-%H%M%S)"
sudo mkdir -p "$unlock_backup"
sudo cp -a /etc/default/grub /boot/grub/grub.cfg /etc/initramfs-tools \
  "/boot/initrd.img-$unlock_kernel" "$unlock_backup/"
```

Install the locally built firmware and DKMS packages. Select the version for the source above:

```bash
unlock_packages=/path/to/reviewed/debs
unlock_version=1.1.1
sudo apt-get install \
  "$unlock_packages/gpu-unlock-firmware_${unlock_version}_all.deb" \
  "$unlock_packages/gpu-unlock-dkms_${unlock_version}_all.deb"
sudo bash docs/examples/gpu_unlock/install_early_initramfs.sh
```

The [installer](./examples/gpu_unlock/install_early_initramfs.sh) adds an early module-loading script, a thin udev wrapper and a build hook that copies the current packaged udev script. It refuses to overwrite an unrelated administrator override. Image rebuild and reboot remain explicit steps.

Enumerate the GPU PCI addresses with `lspci -Dnn`. Add `gpu_unlock.targets=<comma-separated-PCI-addresses>` inside the existing quoted `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`, preserving the other options. Use an explicit allowlist and omit faulty or unselected GPUs; use the same healthy GPU UUID set for verification and benchmarks.

```bash
sudo update-initramfs -u -k "$unlock_kernel"
sudo update-grub
```

## Verify loading order

`init-top/udev` triggers device discovery before the normal module-loading stage. Listing `gpu_unlock` in `modules.d` alone does not guarantee that it runs first. The integration enforces:

```text
blacklist → gpu-unlock-early → udev → framebuffer (if present)
```

Inspect the generated image after every rebuild:

```bash
unlock_inspect=$(mktemp -d /var/tmp/gpu-unlock-initrd.XXXXXX)
sudo unmkinitramfs "/boot/initrd.img-$unlock_kernel" "$unlock_inspect"
unlock_order=$(sudo find "$unlock_inspect" -path '*/scripts/init-top/ORDER' -print -quit)
sudo cat "$unlock_order"
unlock_image_root=${unlock_order%/scripts/init-top/ORDER}
sudo cmp /usr/share/initramfs-tools/scripts/init-top/udev \
  "$unlock_image_root/lib/gpu-unlock-initramfs/udev"
modinfo -F firmware gpu_unlock
sudo lsinitramfs "/boot/initrd.img-$unlock_kernel" | grep -E 'gpu_unlock\.ko|gpu-unlock/'
```

Require `gpu-unlock-early` before `udev`. Check every firmware path reported by `modinfo` and compare its SHA-256 with the installed asset; basename-only matching can select ordinary NVIDIA firmware with the same filename. The pinned RTX 3090 package declares five assets and the Ada package nine. Verify the existing NVIDIA module hashes on the root filesystem and, when included, in the initramfs. Some images omit framebuffer and load NVIDIA modules from the root filesystem.

Use an OS reboot. Follow upstream's fresh-power-state requirements; if the firmware transaction refuses an already initialized device, use a confirmed shutdown and power-off before restarting through BMC. Avoid retrying the firmware transaction on a live device.

## Verify the boot

```bash
cat /proc/sys/kernel/random/boot_id
cat /proc/cmdline
sudo journalctl -k -b -o short-monotonic | grep -E 'gpu_unlock|post-FLR|NVRM|nvidia'
sudo journalctl -k -b | grep -E 'NVRM: Xid|assert|fallen off'
systemctl --failed
```

For every selected PCI address, require one successful unlock and post-FLR selector readback of `SS0=88888888`, `SS1=00000008`; Ada also reports its effective full-rate state as `valid=1 full=1`. All must complete before the early script returns and NVIDIA initializes the devices. Excluded addresses must have no unlock or FLR activity. A loaded module alone does not establish a successful unlock.

Confirm the expected healthy GPUs, unchanged driver, power limits and NVIDIA module hashes, then repeat the baseline correctness and performance checks. Inspect the kernel journal for new errors.

## Benchmark

Keep the same GPU UUIDs, image digest, binaries, kernel, driver, power limits, clock policy and test parameters in both phases. A clean baseline has neither an active unlock module nor its kernel opt-in. Preserve separate baseline and unlocked outputs.

Use upstream numerical validators and GEMM benchmarks. A reproducible GEMM protocol uses 8192 × 8192 inputs, a fixed per-GPU seed, 10 warmups and five groups of 20 CUDA-event-timed iterations. Explicitly select the accumulation and TF32 settings; record any `NVIDIA_TF32_OVERRIDE` value. Check finite outputs and compare against a numerical reference.

Save per-GPU timings, correctness checks, temperatures, clocks and software versions. Distinguish instruction issue rates from GEMM or application throughput, and serial per-card medians from simultaneous node throughput.

## Roll back

For a recovery boot, remove `gpu_unlock.targets=...` from the GRUB entry. Remove it from `/etc/default/grub` and run `sudo update-grub` for a persistent change. Unloading the module does not undo the GPU state it already set; restart with the fresh power state required by upstream and verify stock behavior.

To remove the packages, first remove this integration's site scripts because package removal rebuilds the initramfs:

```bash
if sudo grep -q 'gpu-unlock site wrapper' /etc/initramfs-tools/scripts/init-top/udev; then
  sudo rm /etc/initramfs-tools/scripts/init-top/udev
fi
sudo rm -f /etc/initramfs-tools/scripts/init-top/gpu-unlock-early \
  /etc/initramfs-tools/hooks/zz-gpu-unlock-early
sudo apt-get purge gpu-unlock-dkms gpu-unlock-firmware
sudo update-initramfs -u -k "$(uname -r)"
sudo update-grub
```

Restore the saved boot configuration or image if recovery requires it. Prefer OS reboot; use BMC recovery when the OS is unavailable.



## Deployment and maintenance record

Measurements and maintenance below were completed on 2026-10-25–28 (JST).

| Hosts | GPUs | Matched baseline/unlocked environment | Source |
| --- | --- | --- | --- |
| cvgl-node01 | 7 healthy RTX 3090 24 GB | Ubuntu 22.04.5, kernel 6.8.0-138-generic, NVIDIA 615.71.09, 350 W | gpu-unlock 1.0.0 |
| cvgl-node02 | 8 RTX 4090 24 GB | Ubuntu 24.04.5, kernel 6.8.0-147-generic, NVIDIA 615.71.09, 450 W | gpu-unlock-ada 1.1.1 |
| cvgl-node03, cvgl-node04 | 8 RTX 3090 24 GB each | Ubuntu 22.04.5, kernel 6.8.0-138-generic, NVIDIA 615.71.09, 350 W | gpu-unlock 1.0.0 |
| cvgl-node05 | 8 RTX 4090 24 GB | Ubuntu 22.04, kernel 6.5.0-25-generic, NVIDIA 610.57.04, 450 W | gpu-unlock-ada 1.1.1 |
| cvgl-node06 | 8 RTX 4090 24 GB | Ubuntu 22.04, kernel 6.8.0-138-generic, NVIDIA 615.71.09, 450 W | gpu-unlock-ada 1.1.1 |
| cvgl-node07 | 8 modified RTX 4090 48 GB | Ubuntu 22.04, kernel 6.8.0-138-generic, NVIDIA 615.71.09, 450 W | gpu-unlock-ada 1.1.1 |

Pinned sources:
- [Ampere `af28cbbfd3d922a659227ecfb7369adfd924cf03`](https://github.com/Zagitta/gpu-unlock/tree/af28cbbfd3d922a659227ecfb7369adfd924cf03).
- [Ada `d28c485418c3d9d034cd4bac19200bc33dd3453b`](https://github.com/JustAResearcher/gpu-unlock-ada/tree/d28c485418c3d9d034cd4bac19200bc33dd3453b).

cvgl-node01 excludes physical GPU 4 at `0000:81:00.0` from both the unlock PCI allowlist and benchmark UUID set. The faulty card received no unlock/FLR transaction or benchmark allocation. The other tested nodes each selected eight GPUs.

cvgl-node05 was subsequently upgraded and revalidated on NVIDIA 615.71.09/kernel 6.8.0-138-generic. Its matched 610 measurements are kept separate from that later acceptance run. cvgl-node05/cvgl-node06/cvgl-node07 completed apt, driver and open-kernel-module maintenance; the deployed P2P branch is `615.71.09-p2p-48g`, source `59712effca53dbbcf3290f9724c25768fb3a27ea`.

cvgl-node06 also upgraded the OFED stack to 24.10-5.1.6.1 with modules for the current 6.8 kernel and fallback 6.5 kernel. The 100 Gb interface, management NIC, NFS access, verbs device and NVIDIA peer-memory module passed checks. NIC firmware remained 12.28.2006. The matching NFS-RDMA DKMS package was installed; two unused legacy preloads, `ib_srpt` and `rds_rdma`, were removed from the autoload list because this OFED release supplies dummy implementations while inbox modules fail its symbol-version checks. RDMA core/verbs and NFS-RDMA support remain available.

Experiment 12761 was canceled with the owner's approval after its export failed on a missing URDF mesh and left an occupied process; its allocation and container were released before cvgl-node07 maintenance.

## Boot and package validation

- Each selected GPU completed its unlock and post-FLR selector readback before NVIDIA initialization. Ada additionally reported `valid=1 full=1`. NVIDIA module hashes, power limits and the graphics-agent configuration were preserved during unlock activation.
- cvgl-node01 used seven explicit PCI targets. Each Ada host used eight exact targets and nine firmware assets, verified by full path and SHA-256 in the extracted initramfs; Ampere used five assets.
- cvgl-node05's original Ada activation completed its last GPU at 16.42 s and early loading at 16.44 s, before NVIDIA initialization at 18.73 s.
- cvgl-node06's Ada activation completed its last GPU at 6.096 s and early loading at 6.101 s, before NVIDIA initialization at 14.572 s.
- cvgl-node07's Ada activation completed its last GPU at 6.103 s and early loading at 6.106 s, before NVIDIA initialization at 9.820 s.
- OS reboots activated Ada on the four tested hosts. Their BMC access was checked as fallback; no BMC power/reset was used during planned upgrade/unlock activation. cvgl-node05's later fault-recovery reset is recorded separately below.
- Firmware transaction tests passed. Three optional Ada offline decoder tests were skipped because PyCryptodome was absent.
- Jammy packaging required checking that `dh_dkms` and its debhelper sequence were present before bypassing the newer `dh-sequence-dkms` dependency declaration. The Ampere firmware build used Python 3.11 for build helpers; Ada built with Python 3.10. The system Python was retained.
- Some initramfs images omit framebuffer/console setup and NVIDIA GPU modules. The required dependency remains unlock before udev, with NVIDIA module hashes verified on the root filesystem and in the image wherever included.

## Matched PyTorch GEMM results

TFLOP/s below are medians across the selected GPUs, measured serially. They are not simultaneous whole-node throughput. Within each host's comparison, the image, GPU UUIDs, numerical computation, timing protocol, kernel, driver and power caps were unchanged. Ada reused the same built instruction, validator and cuBLAS binaries in both phases.

| Host | Operation | Before | After | Change |
| --- | --- | --- | --- | --- |
| cvgl-node01 | FP16 / FP32 accumulation | 71.66 | 114.79 | +60.19% |
| cvgl-node01 | BF16 / FP32 accumulation | 72.90 | 117.70 | +61.46% |
| cvgl-node01 | TF32 | 38.85 | 63.68 | +63.89% |
| cvgl-node02 | FP16 / FP32 accumulation | 165.31 | 178.11 | +7.74% |
| cvgl-node02 | BF16 / FP32 accumulation | 151.64 | 152.22 | +0.38% |
| cvgl-node02 | TF32 | 89.14 | 141.70 | +58.96% |
| cvgl-node03 | FP16 / FP32 accumulation | 71.02 | 113.28 | +59.52% |
| cvgl-node03 | BF16 / FP32 accumulation | 72.05 | 116.72 | +62.00% |
| cvgl-node03 | TF32 | 38.61 | 62.31 | +61.40% |
| cvgl-node04 | FP16 / FP32 accumulation | 71.54 | 115.87 | +61.96% |
| cvgl-node04 | BF16 / FP32 accumulation | 73.13 | 119.01 | +62.74% |
| cvgl-node04 | TF32 | 38.80 | 64.18 | +65.43% |
| cvgl-node05 | FP16 / FP32 accumulation | 164.44 | 180.12 | +9.54% |
| cvgl-node05 | BF16 / FP32 accumulation | 149.63 | 151.67 | +1.36% |
| cvgl-node05 | TF32 | 88.82 | 145.09 | +63.35% |
| cvgl-node06 | FP16 / FP32 accumulation | 165.38 | 180.21 | +8.96% |
| cvgl-node06 | BF16 / FP32 accumulation | 150.44 | 151.24 | +0.53% |
| cvgl-node06 | TF32 | 88.23 | 144.57 | +63.86% |
| cvgl-node07 | FP16 / FP32 accumulation | 162.73 | 176.92 | +8.72% |
| cvgl-node07 | BF16 / FP32 accumulation | 149.83 | 150.26 | +0.28% |
| cvgl-node07 | TF32 | 89.57 | 140.35 | +56.69% |

Protocol: pinned image `determinedai/pytorch-ngc@sha256:67ab2dc2f6fcfba90d2085e82ea2f3a8a5ef3205cbf3203f6d1f5490d000f525`, PyTorch 2.3.0a0+40ec155e58.nv24.03, CUDA 12.4; 8192³ GEMM; seed `20261025 + physical GPU index`; 10 warmups; five groups of 20 CUDA-event-timed iterations. Both reduced-precision reduction flags were false, TF32 was explicitly enabled for FP32 input, and `NVIDIA_TF32_OVERRIDE` was unset. Each result includes a finite-output check and an 8×8 CPU FP64 reference tile over the full reduction dimension, with relative-L2 limits 0.02 for BF16 and 0.01 for FP16/TF32.

Each phase passed 21 GEMM checks on cvgl-node01 and 24 on each eight-card host. The cvgl-node04 after harness added an explicit phase guard and stable physical/UUID selection; its computation, per-device seeds and timing were unchanged. Its copy baseline uses the corrected full-peer-initialization harness; superseded incomplete-peer-setup measurements are excluded.

## Ada instruction issue-rate results

The [upstream MMA probe](https://github.com/JustAResearcher/gpu-unlock-ada/blob/d28c485418c3d9d034cd4bac19200bc33dd3453b/diagnostics/bench/mma_bench.cu) used 20,000 iterations and five repetitions per GPU. Each row is the median of per-GPU median repetitions. Floating-point rows are TFLOP/s; `s8/s32` and `dp4a` are TOP/s. These are register-resident instruction rates, separate from application GEMM.

| Host | Operation | Before | After | Change |
| --- | --- | --- | --- | --- |
| cvgl-node02 | f16/f16 | 328.10 | 302.70 | -7.74% |
| cvgl-node02 | f16/f32 | 167.05 | 333.90 | +99.88% |
| cvgl-node02 | bf16/f32 | 166.95 | 332.20 | +98.98% |
| cvgl-node02 | s8/s32 | 666.55 | 666.55 | +0.00% |
| cvgl-node02 | fp8/e4m3 | 332.90 | 664.10 | +99.49% |
| cvgl-node02 | dp4a | 165.80 | 165.80 | +0.00% |
| cvgl-node02 | ffma | 79.55 | 79.60 | +0.06% |
| cvgl-node05 | f16/f16 | 303.00 | 318.40 | +5.08% |
| cvgl-node05 | f16/f32 | 166.40 | 333.35 | +100.33% |
| cvgl-node05 | bf16/f32 | 166.30 | 331.00 | +99.04% |
| cvgl-node05 | s8/s32 | 665.10 | 664.80 | -0.05% |
| cvgl-node05 | fp8/e4m3 | 332.30 | 662.45 | +99.35% |
| cvgl-node05 | dp4a | 165.40 | 165.25 | -0.09% |
| cvgl-node05 | ffma | 79.50 | 79.50 | +0.00% |
| cvgl-node06 | f16/f16 | 327.95 | 312.55 | -4.70% |
| cvgl-node06 | f16/f32 | 165.40 | 330.45 | +99.79% |
| cvgl-node06 | bf16/f32 | 165.25 | 329.20 | +99.21% |
| cvgl-node06 | s8/s32 | 660.40 | 659.95 | -0.07% |
| cvgl-node06 | fp8/e4m3 | 330.15 | 657.40 | +99.12% |
| cvgl-node06 | dp4a | 164.15 | 164.00 | -0.09% |
| cvgl-node06 | ffma | 78.70 | 78.70 | +0.00% |
| cvgl-node07 | f16/f16 | 326.95 | 323.30 | -1.12% |
| cvgl-node07 | f16/f32 | 167.15 | 334.85 | +100.33% |
| cvgl-node07 | bf16/f32 | 167.05 | 332.95 | +99.31% |
| cvgl-node07 | s8/s32 | 671.70 | 669.40 | -0.34% |
| cvgl-node07 | fp8/e4m3 | 335.55 | 667.05 | +98.79% |
| cvgl-node07 | dp4a | 167.05 | 166.50 | -0.33% |
| cvgl-node07 | ffma | 80.20 | 80.10 | -0.12% |

Integer/scalar controls stayed close to baseline. FP16/FP16 issue rate decreased on cvgl-node02/cvgl-node06/cvgl-node07; those cases are retained. The FP8 issue probe does not compare numerical outputs.

## Ada upstream cuBLAS results

Median TFLOP/s across eight GPUs, using each GPU's median repetition. All 48 shape/card checks in each phase had zero output mismatches, and all 48 separate seeded FP16/BF16 numerical cases passed.

| Host | Operation | Before | After | Change |
| --- | --- | --- | --- | --- |
| cvgl-node02 | FP16/FP32 n=1024 | 114.28 | 131.06 | +14.69% |
| cvgl-node02 | BF16/FP32 n=1024 | 132.52 | 160.73 | +21.29% |
| cvgl-node02 | FP16/FP32 n=4096 | 165.58 | 206.14 | +24.49% |
| cvgl-node02 | BF16/FP32 n=4096 | 157.52 | 156.12 | -0.89% |
| cvgl-node02 | FP16/FP32 n=8192 | 175.31 | 194.71 | +11.07% |
| cvgl-node02 | BF16/FP32 n=8192 | 170.29 | 192.50 | +13.05% |
| cvgl-node05 | FP16/FP32 n=1024 | 113.51 | 130.30 | +14.79% |
| cvgl-node05 | BF16/FP32 n=1024 | 132.52 | 159.78 | +20.57% |
| cvgl-node05 | FP16/FP32 n=4096 | 164.55 | 207.28 | +25.97% |
| cvgl-node05 | BF16/FP32 n=4096 | 156.15 | 154.95 | -0.77% |
| cvgl-node05 | FP16/FP32 n=8192 | 175.26 | 193.42 | +10.36% |
| cvgl-node05 | BF16/FP32 n=8192 | 170.35 | 190.30 | +11.71% |
| cvgl-node06 | FP16/FP32 n=1024 | 112.60 | 130.73 | +16.11% |
| cvgl-node06 | BF16/FP32 n=1024 | 131.88 | 158.88 | +20.47% |
| cvgl-node06 | FP16/FP32 n=4096 | 163.65 | 211.17 | +29.03% |
| cvgl-node06 | BF16/FP32 n=4096 | 153.88 | 155.21 | +0.87% |
| cvgl-node06 | FP16/FP32 n=8192 | 174.30 | 192.21 | +10.27% |
| cvgl-node06 | BF16/FP32 n=8192 | 169.75 | 189.69 | +11.75% |
| cvgl-node07 | FP16/FP32 n=1024 | 112.97 | 133.19 | +17.91% |
| cvgl-node07 | BF16/FP32 n=1024 | 132.98 | 160.84 | +20.94% |
| cvgl-node07 | FP16/FP32 n=4096 | 166.93 | 215.34 | +29.00% |
| cvgl-node07 | BF16/FP32 n=4096 | 156.92 | 153.95 | -1.89% |
| cvgl-node07 | FP16/FP32 n=8192 | 176.19 | 194.10 | +10.17% |
| cvgl-node07 | BF16/FP32 n=8192 | 171.34 | 191.01 | +11.48% |

cvgl-node05's same-device RM canary changed `fmla32` from half-rate (1) to full-rate (0) on 610.57.04. On the tested multi-GPU 615 setup, the optional upstream single-device RM getter failed device allocation with status 0x1b; its result is not counted as a pass. The eight-card full-rate boot readbacks and measured per-card instruction rates provide the unlock evidence there.

## P2P coexistence and integrity

All 42 directed pairs on cvgl-node01 and 56 on each eight-card host passed full-buffer SHA-256 verification with 256 MiB copies in both phases. Both peer directions were established before timing every pair.

GB/s; each cell is baseline → unlocked:

| Host | Matched driver | Directed pairs | Minimum | Median | Maximum |
| --- | --- | --- | --- | --- | --- |
| cvgl-node01 | 615.71.09 | 42 | 13.17 → 13.18 | 13.21 → 13.21 | 26.37 → 26.38 |
| cvgl-node02 | 615.71.09 | 56 | 21.84 → 21.86 | 22.70 → 22.71 | 26.37 → 26.38 |
| cvgl-node03 | 615.71.09 | 56 | 19.98 → 20.76 | 22.71 → 22.71 | 26.38 → 26.38 |
| cvgl-node04 | 615.71.09 | 56 | 21.84 → 21.77 | 22.71 → 22.72 | 26.37 → 26.39 |
| cvgl-node05 | 610.57.04 | 56 | 9.64 → 9.75 | 13.15 → 13.15 | 26.32 → 26.32 |
| cvgl-node06 | 615.71.09 | 56 | 13.17 → 13.17 | 26.32 → 26.33 | 26.33 → 26.34 |
| cvgl-node07 | 615.71.09 | 56 | 13.17 → 13.17 | 26.32 → 26.33 | 26.33 → 26.34 |

cvgl-node01's mean was 18.3023 → 18.3201 GB/s. Small min/median/max differences are run-to-run observations, not evidence of a bandwidth gain from tensor unlock. The results are consistent with the documented PCIe topology and existing x8 limitations.

## Acceptance after the 615 upgrades

cvgl-node05's separate post-upgrade run passed 24 GEMM checks, 56 directed copy checks, 48 seeded numerical cases, all eight instruction probes, host-NUMA allocation tests, eight-GPU NCCL and non-root Vulkan.

| cvgl-node05 post-upgrade measurement | Result |
| --- | ---: |
| FP16, FP32 accumulation GEMM | 180.40 TFLOP/s |
| BF16, FP32 accumulation GEMM | 151.84 TFLOP/s |
| TF32 GEMM | 145.42 TFLOP/s |
| FP16/FP32 instruction issue median | 333.40 TFLOP/s |
| P2P min / median / max | 10.98 / 16.49 / 26.32 GB/s |

These are acceptance values on 615, not a matched baseline/unlocked comparison.

All four Ada hosts passed host-NUMA tests: 96 allocation cases plus 4 GiB held per NUMA node. Eight-GPU NCCL used 64/256 MiB sizes, five timed iterations and default/`p2p-sys` variants, with all four correctness checks passing and transport logs showing P2P/IPC.

| Host | Default busbw, 64 / 256 MiB | p2p-sys busbw, 64 / 256 MiB |
| --- | --- | --- |
| cvgl-node02 | 19.6 / 20.6 GB/s | 20.0 / 20.6 GB/s |
| cvgl-node05 | 12.3 / 12.9 GB/s | 12.4 / 12.9 GB/s |
| cvgl-node06 | 12.9 / 12.9 GB/s | 12.9 / 12.9 GB/s |
| cvgl-node07 | 12.9 / 12.9 GB/s | 12.9 / 12.9 GB/s |

cvgl-node07 additionally passed NCCL 2.27.5/PyTorch 2.9.1 with 38 GiB resident on each of eight 48 GB cards. Both collective sizes were correct, all resident data stayed intact, and the logs showed P2P/CUMEM. Bus bandwidth was 12.9 GB/s for both sizes. This validates the dynamic BAR1 case beyond the 32 GiB aperture. GPU 1 on cvgl-node07, GPU 6 on cvgl-node06, and GPUs 1/4 on cvgl-node05 retained their existing x8 link widths.

Vulkan succeeded as UID/GID 1001 with all Linux capabilities dropped, without privileged mode or a Docker socket. NFS reads passed. No new NVIDIA Xid/assert was found during the accepted tests.

An initial cvgl-node07 high-residency harness used an image without PyTorch and failed before running that check. The successful rerun used the verified cached PyTorch/NCCL image above; the failed attempt is not counted as a hardware failure or pass.

## Network boot cleanup and final scheduling state

On cvgl-node07, `ens6f0` has no carrier and retains `activation-mode: off`. It now explicitly has `optional: true`; the active `enp34s0np0` was already optional. The generated networkd file contains `RequiredForOnline=no`. `systemd-networkd-wait-online.service` is persistently masked and inactive, and its old failed state is cleared.

Applied with Netplan generation and `networkctl reload`, without a reboot or network-service restart. The generated active-NIC configuration, live address/routes, four existing container IDs/PIDs/start times and NFS access all matched their pre-change state. The Netplan file permissions were set to 0600.

At their original acceptance, cvgl-node05/cvgl-node06/cvgl-node07 were enabled with eight slots each; their respective pools `64c128t_512_4090`, `128c256t_1536_4090` and `128c256t_1536_4090_48` are public. cvgl-node06 remained disabled/draining throughout maintenance and was enabled only after its complete acceptance and PR update. cvgl-node01 remains a seven-GPU agent with the faulty GPU excluded.

## cvgl-node02 maintenance and matched unlock validation (2026-10-28 JST)

cvgl-node02 completed Ubuntu 24.04.5 apt maintenance, NVIDIA `615.71.09-0ubuntu0.24.04.2` (the configured apt repositories' current candidate), kernel `6.8.0-147-generic`, the matching P2P fork `59712effca53dbbcf3290f9724c25768fb3a27ea`, and Ada unlock `d28c485418c3d9d034cd4bac19200bc33dd3453b` / 1.1.1. P2P modules were built with GCC 13.3. Both older installed boot kernels, 6.8.0-107 and 6.8.0-146, have matching stock 615 modules as driver fallbacks; the old 610 P2P override was backed up and disarmed. All 16 installed 615 driver packages are held; apt reports no remaining upgrades and dpkg reports no unfinished packages.

There were no Determined allocations at entry. The burn supervisor was persistently paused. The pool stayed restricted during maintenance and direct Docker validation; the agent was drained, then stopped with automatic restart temporarily disabled for the unlock reboot. Its graphics-v2 image and original restart policy were preserved.

Two OS reboots were used: first to activate the new kernel/615/P2P environment without tensor unlock for the baseline, then to activate Ada. BMC authentication and health reads succeeded; no BMC reset or power operation was used on this host. Both matched phases used the same eight GPU UUIDs, 24 GB cards, 450 W caps, driver, kernel, image and benchmark binaries. Every GPU retained PCIe x16/x16. Nine firmware assets were checked by full path and SHA-256 in the actual initramfs, with unlock ordered before udev and framebuffer. The last GPU unlocked at 7.818 s, early loading completed at 7.824 s, and NVIDIA initialization began at 14.235 s. All eight post-FLR selectors and `valid=1 full=1` readbacks passed; all five NVIDIA module hashes were unchanged.

Noble satisfied `dh-sequence-dkms` directly, so no build-dependency bypass was used. Firmware transaction/build tests passed; three optional offline decoder tests remained skipped because PyCryptodome was absent. Each measured phase passed 24 PyTorch GEMM checks, 56 full-buffer-SHA-256 directed peer copies, 48 upstream seeded numerical cases and 48 cuBLAS shape checks. The pinned images and timing/numerical protocols are the same as recorded above.

Its matched PyTorch GEMM, instruction-rate, cuBLAS and directed P2P results are the cvgl-node02 rows of the tables above. FP16/FP32 and BF16/FP32 instruction rates are approximately 2×, as is this probe's FP8/E4M3 rate; application GEMM gains remain shape dependent.

Host-NUMA validation passed 96 allocation cases and 4 GiB held on each of two NUMA nodes. Eight-GPU NCCL passed all four 64/256 MiB/default/`p2p-sys` correctness checks, with 30 P2P/IPC transport entries per variant. Bus bandwidth is in the NCCL table above. Vulkan enumerated the RTX 4090 as UID 1001 with all Linux capabilities dropped, without privileged mode. The optional single-device RM getter was omitted for the already documented multi-GPU 615 limitation.

At 200 ms sampling, the matched serial GEMM/numerical/MMA/cuBLAS windows reached 361.56/318.95 W peak reported draw and 61/57 °C peak temperature before/after. SW Power Cap was observed on all eight cards in both sets of serial upstream probe windows; neither hardware nor software thermal-slowdown flags appeared. NCCL/Vulkan samples are excluded from this comparison. These windows include validation and idle gaps and do not isolate individual kernels; sampled peaks below the 450 W setting do not negate the driver's power-cap reason. This supports power limiting being present, but does not establish it as the sole cause of the smaller GEMM gains.

Both management and 100 Gb links, TCP NFS reads, native mlx5 modules and unchanged NIC firmware 12.28.2006 passed checks. Both configured interfaces are optional and networkd wait-online is persistently masked. Netplan 1.1.2 represents the all-optional setup through a wait-online override without interface-wait commands, rather than the older `RequiredForOnline=no` rendering. No failed services or Xids were found after the final tests; idle GPU temperatures were 27–29 °C.

After publishing this validation record, the preserved graphics-v2 agent container and its original `unless-stopped` restart policy were restored. At 17:00 UTC on 2026-10-27 (02:00 JST on 2026-10-28), master reported cvgl-node02 enabled/not draining with eight healthy slots, and `48c96t_512_4090` was changed to public. All other pool-access records were unchanged. Burn workers remain paused; no further GPU load tests were run after restoring scheduling.

## cvgl-node05 recurrent GPU fault (2026-10-27 UTC)

The earlier short acceptance results above are retained as measurements, not evidence of sustained stability. GPU 3 at `0000:61:00.0` fell off the bus with Xid 79 at 10:27:44 UTC. During that investigation GPU 6/7 were 77/69 °C with their own fans at 100/95%; those were different cards, and no pre-failure temperature was available for GPU 3.

All 16 chassis fans reported healthy. GPU chassis fans were approximately 15,860–16,120 RPM and the system/CPU fan bank approximately 27,170–28,210 RPM, with 24 °C inlet air. BMC reported fan mode 3 (Full speed). Installing ipmitool and reading local IPMI confirmed fan RPM/status. The web PWM endpoint exposes configured control curves, not live per-fan duty, so an actual PWM=100% readback was not established. No fan settings were changed.

At the user's request, one BMC chassis reset was issued at 11:27:26 UTC after an OS reboot stalled. SSH returned around 11:31 and all eight cards initially enumerated. The same GPU 3 fell off the bus again at 12:25:10 UTC, followed by Xid 154 recovery requests. A later read-only check confirmed it was still unavailable. The agent was initially disabled/draining with zero allocated containers at that transition. Its pool remained public. The subsequent user-requested reboot and persistent scheduling exclusion are recorded below. The cause is unresolved; cooling, card, slot/riser/power, driver and unlock effects have not been isolated.

### Subsequent reboot and persistent GPU 3 exclusion (2026-10-28 JST)

At the user's request, cvgl-node05 was restarted and physical GPU 3 (`0000:61:00.0`, UUID `GPU-0190894d-1267-ff6b-403b-caad83af5ed0`) was persistently excluded from Determined scheduling.

The OS reboot was requested at approximately 17:08 UTC on 2026-10-27. Neither management nor data-network SSH recovered after more than six minutes; an authenticated SOL session produced no console output. One BMC chassis reset was accepted at 17:14:53 UTC as fallback, and SSH recovered around 17:19 UTC. All eight cards initially enumerated again with driver 615.71.09/kernel 6.8.0-138, without new kernel Xids. Shared-storage reads passed. A short CUDA arithmetic check selected only the seven allowed UUIDs and passed on all seven; it made no allocation on GPU 3.

The old agent was stopped and its automatic restart disabled. The master was allowed to forget its old reconnect record before changing the slot set. The replacement preserves the same graphics-v2 image and configuration, with `run --exclude-gpus GPU-0190894d-1267-ff6b-403b-caad83af5ed0` and `unless-stopped` restart policy. This uses the agent's native UUID exclusion, not a temporary disabled slot; the remaining physical slot IDs stay unchanged.

At 17:21 UTC, master verification showed the agent enabled/not draining with seven slots: `0,1,2,4,5,6,7`, topology `3+4 NODE/SYS p2p`, and the faulty UUID marked `excluded=true`. Its historical Xid 79 remains visible under the excluded device. The seven schedulable GPUs have no NVML errors or recent Xids; the previously recorded x8 links on GPUs 1/4 remain. Pool `64c128t_512_4090` stays public.

GPU 3 remains physically present and powered, and the existing unlock allowlist was unchanged; this is scheduling exclusion, not hardware repair. No extended stability claim is made. This boot also reported `pcsd-ruby` missing `webrick` and `rpc-statd` saying its service was already running. Those service errors were recorded without changes during this GPU exclusion; `/SSD_home` remained readable.

### Later concurrent-load thermal observation on cvgl-node02

At approximately 17:40 UTC on 2026-10-27, all eight GPUs reported 100% utilization. GPU 0's configured/current/requested/default ceiling remained 450 W, while its actual draw was approximately 321–323 W and SM clocks 2265–2310 MHz. Eight successive one-second samples all reported `SW Thermal Slowdown=Active`, with `SW Power Cap` and `HW Power Brake` inactive. Its reported core temperature was 71–72 °C, T.Limit margin approximately 1 °C, and fan command 95–96%.

GPU 6 similarly reported thermal slowdown in all eight samples at 380–385 W, 2415–2430 MHz, 70 °C and 96% fan. The other six cards reached approximately 448–450 W with the power-cap reason active and thermal slowdown inactive. All eight had the same VBIOS version `95.02.18.C0.09`. Thus the approximately 320 W observation on GPU 0 is associated with active thermal clock limiting, rather than a configured 320 W power ceiling.

BMC reported all ten chassis fans healthy: FAN1–8 approximately 10,920–11,760 RPM and FAN9–10 approximately 3640 RPM; inlet temperature was 21 °C. A subsequent read-only IPMI fan-mode query (`raw 0x30 0x45 0x00`) returned `0x01` (Full Speed), confirming that the BMC is already configured for its full-speed fan profile. These readings establish the selected control mode and fan RPM/status, not an independent live per-zone PWM-duty measurement. The sensor upper alarm thresholds are not fan-rated maximum RPM. No settings or workloads were changed for this read-only diagnosis.

NVIDIA defines software thermal limiting in terms of GPU and memory operating-temperature constraints ([NVML field definitions](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlFieldValueEnums.html)); the displayed core temperature alone does not identify the triggering sensor. Hotspot/maximum-die and memory temperatures were unavailable in this GeForce NVML report, so local airflow versus heatsink contact, paste/pads or another card-local thermal cause was not isolated. This later simultaneous-load observation is separate from the earlier short serial benchmark windows, which showed no thermal-slowdown flags.

## Limits and documentation checks

These are short correctness and serial performance tests, not extended production stability or application-training benchmarks. Gains depend on shape, math mode and thermal/power limits. Successful OS reboots on the tested boards do not establish warm-reboot behavior for every GPU/firmware combination.

Docs contain reusable procedures and pinned source references, with incident-specific measurements and host state kept in this description. Local Bash syntax, links/anchors, whitespace and canonical-hostname checks pass. No additional benchmark scripts or driver-upgrade procedure are added.

