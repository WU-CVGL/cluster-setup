# GPU Tensor Unlock

## Contents

- [Validated deployment](#validated-deployment)
- [Results](#results)
- [Initramfs ordering on Ubuntu 22.04](#initramfs-ordering-on-ubuntu-2204)
- [Installation](#installation)
- [Boot verification](#boot-verification)
- [Benchmark procedure](#benchmark-procedure)
- [Rollback](#rollback)

## Validated deployment

On 2026-10-25, S3 and S4 each ran eight RTX 3090 24 GB GPUs with [Zagitta/gpu-unlock](https://github.com/Zagitta/gpu-unlock/tree/af28cbbfd3d922a659227ecfb7369adfd924cf03), version `1.0.0`, commit `af28cbbfd3d922a659227ecfb7369adfd924cf03`.

The tested environment was Ubuntu 22.04.5, kernel `6.8.0-138-generic`, NVIDIA `615.71.09`, and our [P2P modules](https://github.com/LingzheZhao/open-gpu-kernel-modules/tree/7812dde8eed7b13fbf924bdde4f9cbda33847f87). Every GPU retained its 350 W power limit; clock settings and PCIe/IOMMU configuration were unchanged.

`gpu_unlock` runs before NVIDIA binds the GPUs, sets the tensor speed selectors, and resets the devices before NVIDIA initialization. The existing P2P modules continue to provide BAR1/UVM peer transfers. Both features were verified together on these two hosts.

## Results

GEMM numbers below are the median of the eight individual GPUs, in TFLOP/s. GPUs were measured serially; these are not simultaneous whole-node throughput figures.

| Node | Operation | Before | After | Change |
| --- | --- | ---: | ---: | ---: |
| S3 | FP16 input, FP32 accumulation | 71.02 | 113.28 | +59.52% |
| S3 | BF16 input, FP32 accumulation | 72.05 | 116.72 | +62.00% |
| S3 | TF32 | 38.61 | 62.31 | +61.40% |
| S4 | FP16 input, FP32 accumulation | 71.54 | 115.87 | +61.96% |
| S4 | BF16 input, FP32 accumulation | 73.13 | 119.01 | +62.74% |
| S4 | TF32 | 38.80 | 64.18 | +65.43% |

Each node also passed all 56 directed P2P transfers, including SHA-256 validation of every 256 MiB destination buffer.

| Node | P2P median before (GB/s) | P2P median after (GB/s) |
| --- | ---: | ---: |
| S3 | 22.7078 | 22.7068 |
| S4 | 22.7095 | 22.7187 |

The P2P bandwidth remained effectively unchanged. The [existing RTX 3090 P2P measurements](./05_GPU_P2P_GeForce_and_CMP.md#appendix-a-measurements) on the same EPYC 7402 platform record about 26.4 GB/s per pair and 21.5–22.7 GB/s across sockets, consistent with this run. NVIDIA's unmodified CUDA 12.4 `p2pBandwidthLatencyTest` independently measured 20.78–26.00 GB/s unidirectional and 39.28–50.67 GB/s bidirectional on S4 before the tensor unlock.

These checks cover GEMM and explicit P2P data transfers on the configuration above. They do not establish production NCCL/training performance or dynamic 48 GB/mixed-generation compatibility.

## Initramfs ordering on Ubuntu 22.04

The tested `initramfs-tools` version, `0.140ubuntu13.5`, supports `/usr/share/initramfs-tools/modules.d/`. However, `init-top/udev` triggers device discovery before `load_modules`. Because NVIDIA modules are already in the initramfs, listing `gpu_unlock` in `modules.d` alone does not guarantee that it runs first.

The [early-loading installer](./examples/gpu_unlock/install_early_initramfs.sh) supplies:

- `init-top/gpu-unlock-early`, which waits for `blacklist` and synchronously loads `gpu_unlock` when the kernel command line opts in.
- A thin site `init-top/udev` wrapper whose prerequisites include `gpu-unlock-early`.
- A build hook that includes the unlock module and copies the **current** packaged udev script into each generated image. The wrapper executes this copy; the packaged vendor file is unchanged.

The validated order was:

```text
00_mount_efivarfs
all_generic_ide
blacklist
gpu-unlock-early
udev
framebuffer
console_setup
```

Inspect the generated image itself. Changing only a generated udev script's `PREREQS` in a build hook was insufficient: Jammy's ordering code reads the original source scripts when generating `ORDER`.

## Installation

Run these steps on the target node during a maintenance window, with GPU workloads stopped and authenticated BMC access available. Record the clean baseline using the [benchmark procedure](#benchmark-procedure) before enabling the unlock. The commands below assume the `cluster-setup` repository is the working directory.

Back up the boot configuration and the currently bootable image:

```bash
unlock_kernel=$(uname -r)
unlock_backup="/var/backups/gpu-unlock-$(date +%Y%m%d-%H%M%S)"
sudo mkdir -p "$unlock_backup"
sudo cp -a /etc/default/grub "$unlock_backup/grub"
sudo cp -a /boot/grub/grub.cfg "$unlock_backup/grub.cfg"
sudo cp -a /etc/initramfs-tools "$unlock_backup/initramfs-tools"
sudo cp -a "/boot/initrd.img-$unlock_kernel" "$unlock_backup/initrd.img-$unlock_kernel"
```

Build the two Debian packages from the pinned [upstream source](https://github.com/Zagitta/gpu-unlock/tree/af28cbbfd3d922a659227ecfb7369adfd924cf03), retaining its input SHA-256 checks. The source's firmware build uses `hashlib.file_digest`, so the Jammy build used Python 3.11 for the build helpers without replacing the system Python. Jammy's DKMS package already provided `dh_dkms` and its debhelper sequence, but did not satisfy the newer `dh-sequence-dkms` dependency declaration; the deployment build bypassed that declaration only after verifying the actual helpers were present.

Install the locally built packages, replacing the example package directory:

```bash
unlock_packages=/path/to/reviewed/debs
sudo apt-get install \
  "$unlock_packages/gpu-unlock-firmware_1.0.0_all.deb" \
  "$unlock_packages/gpu-unlock-dkms_1.0.0_all.deb"
sudo bash docs/examples/gpu_unlock/install_early_initramfs.sh
```

The installer refuses to replace an unrelated administrator-provided udev override. It installs the ordering integration but does not rebuild images or reboot.

Add `gpu_unlock.targets=all` inside `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`, preserving all existing options, including `amd_iommu=on iommu=pt` on these nodes. Then rebuild:

```bash
sudo update-initramfs -u -k "$unlock_kernel"
sudo update-grub
```

Before rebooting, extract and inspect the actual image:

```bash
unlock_inspect=$(mktemp -d /var/tmp/gpu-unlock-initrd.XXXXXX)
sudo unmkinitramfs "/boot/initrd.img-$unlock_kernel" "$unlock_inspect"
unlock_order=$(sudo find "$unlock_inspect" -path '*/scripts/init-top/ORDER' -print -quit)
sudo cat "$unlock_order"
unlock_image_root=${unlock_order%/scripts/init-top/ORDER}
sudo cmp /usr/share/initramfs-tools/scripts/init-top/udev \
  "$unlock_image_root/lib/gpu-unlock-initramfs/udev"
modinfo -F firmware gpu_unlock
sudo lsinitramfs "/boot/initrd.img-$unlock_kernel" | grep -E 'gpu_unlock\.ko|gpu-unlock/|nvidia.*\.ko'
```

Confirm `gpu-unlock-early` precedes `udev`, which precedes `framebuffer`; all five firmware files reported by `modinfo` must be present. Confirm the existing P2P NVIDIA modules remain unchanged in the generated image. Repeat this image verification whenever rebuilding the initramfs for this integration.

Use an OS reboot:

```bash
sudo systemctl reboot
```

## Boot verification

```bash
cat /proc/sys/kernel/random/boot_id
cat /proc/cmdline
sudo journalctl -k -b -o short-monotonic | grep -E 'gpu_unlock|post-FLR|NVRM|nvidia'
nvidia-smi
sudo journalctl -k -b | grep -E 'NVRM: Xid|assert|fallen off'
systemctl --failed
```

Match the journal entries to all eight GPU PCI addresses. Each must have `gpu_unlock: unlocked` and a post-FLR readback of `SS0=88888888`, `SS1=00000008`. All eight completions must precede the early script's completion message and the first NVIDIA initialization. A loaded module by itself does not prove that any GPU unlocked.

Verify the unchanged 350 W limits, P2P module paths, and all directed P2P copies before accepting the deployment. On S3 and S4, the boot-order checks, register readbacks, GEMM correctness, and P2P data validation all passed without new Xid/assert errors or failed systemd services.

## Benchmark procedure

Use the [benchmark script](./examples/gpu_unlock/benchmark.py) and the same container image, GPU set, power limits, and clock policy in both phases. Copy the script once before installation and reuse that exact copy after reboot:

```bash
unlock_results="$HOME/p2p-logs/gpu-unlock-comparison"
mkdir -p "$unlock_results"
cp docs/examples/gpu_unlock/benchmark.py "$unlock_results/benchmark.py"
sha256sum "$unlock_results/benchmark.py" > "$unlock_results/benchmark.sha256"
```

Set `unlock_phase=before` for the clean baseline. After installation and boot verification, use the same results directory and set `unlock_phase=after` to create separate result files:

```bash
unlock_results="$HOME/p2p-logs/gpu-unlock-comparison"
unlock_phase=before
unlock_image='determinedai/pytorch-ngc@sha256:67ab2dc2f6fcfba90d2085e82ea2f3a8a5ef3205cbf3203f6d1f5490d000f525'
sha256sum -c "$unlock_results/benchmark.sha256"
sudo docker run --rm --gpus all --network none --ipc=host --entrypoint python \
  -v "$unlock_results:/results" "$unlock_image" \
  /results/benchmark.py --mode gemm --phase "$unlock_phase" \
  --node-label "$(hostname -s)" --output "/results/gemm-$unlock_phase.json"
sudo python3 "$unlock_results/benchmark.py" --mode p2p --phase "$unlock_phase" \
  --node-label "$(hostname -s)" --output "$unlock_results/p2p-$unlock_phase.json"
```

The tested image contains PyTorch `2.3.0a0+40ec155e58.nv24.03` and CUDA `12.4`. `NVIDIA_TF32_OVERRIDE` was unset. The script rejects a forced value of `0`.

The fixed protocol is 8192³ GEMM, 10 warmups, and five groups of 20 iterations timed with CUDA events. FP16/BF16 reductions use FP32 accumulation settings; TF32 is explicitly enabled. Finite-output and CPU FP64 reference checks run outside the timed sections. The JSON records individual GPUs, settings, clocks, temperatures, power limits, and script hashes.

P2P uses 256 MiB per copy, five warmups, and 20 timed synchronous copies per directed pair. **Enable every directed peer relationship before timing any pair.** The initial one-direction-at-a-time implementation produced a false 4.6 GB/s half-matrix; those speed results were superseded. The included script establishes both directions in advance and validates the full destination buffer outside the timed section.

The `before` phase rejects an active unlock or opt-in parameter. The `after` phase requires the module to be loaded; the separate boot-log verification above establishes that every card actually unlocked. Preserve the original before JSON files when collecting after results.

Raw deployment records remain on S3 under `/home/cvgladmin/p2p-logs/maintenance-20261025/node13/` and on S4 under `/home/cvgladmin/p2p-logs/maintenance-20261025/S4/`.

## Rollback

For a single recovery boot, remove `gpu_unlock.targets=...` from the GRUB entry. Once the OS is accessible, remove it from `/etc/default/grub` and run `sudo update-grub`. Merely unloading the module does not undo the GPU state it already set; verify the boot and stock behavior after restarting.

For complete removal, remove this integration's site scripts **before** purging the packages, because package removal rebuilds the initramfs:

```bash
if sudo grep -q 'gpu-unlock site wrapper' /etc/initramfs-tools/scripts/init-top/udev; then
  sudo rm /etc/initramfs-tools/scripts/init-top/udev
fi
sudo rm -f /etc/initramfs-tools/scripts/init-top/gpu-unlock-early \
  /etc/initramfs-tools/hooks/zz-gpu-unlock-early
sudo apt-get purge gpu-unlock-dkms gpu-unlock-firmware
sudo update-initramfs -u -k "$(uname -r)"
sudo update-grub
sudo systemctl reboot
```

Remove the GRUB opt-in before running the removal commands. If recovery requires the saved boot image or site configuration, restore them from the backup made above. Prefer OS reboot; use BMC recovery only when the OS is unavailable. A clean pre-unlock benchmark also requires the module to be absent, not just an absent opt-in parameter.
