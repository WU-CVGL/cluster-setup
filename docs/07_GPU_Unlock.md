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

S1 joined this deployment on 2026-10-27 with seven healthy RTX 3090 GPUs. Its kernel opt-in uses an explicit PCI-address allowlist, excluding physical GPU 4 at `0000:81:00.0`.

The tested environment was Ubuntu 22.04.5, kernel `6.8.0-138-generic`, and NVIDIA `615.71.09`. Every GPU retained its 350 W power limit; clock settings and PCIe/IOMMU configuration were unchanged.

`gpu_unlock` runs before NVIDIA binds the GPUs, sets the tensor speed selectors, and resets the devices before NVIDIA initialization.

## Results

GEMM numbers below are medians across the tested GPUs: seven on S1 and eight each on S3 and S4, in TFLOP/s. GPUs were measured serially; these are not simultaneous whole-node throughput figures.

| Node | Operation | Before | After | Change |
| --- | --- | ---: | ---: | ---: |
| S1 | FP16 input, FP32 accumulation | 71.66 | 114.79 | +60.19% |
| S1 | BF16 input, FP32 accumulation | 72.90 | 117.70 | +61.46% |
| S1 | TF32 | 38.85 | 63.68 | +63.89% |
| S3 | FP16 input, FP32 accumulation | 71.02 | 113.28 | +59.52% |
| S3 | BF16 input, FP32 accumulation | 72.05 | 116.72 | +62.00% |
| S3 | TF32 | 38.61 | 62.31 | +61.40% |
| S4 | FP16 input, FP32 accumulation | 71.54 | 115.87 | +61.96% |
| S4 | BF16 input, FP32 accumulation | 73.13 | 119.01 | +62.74% |
| S4 | TF32 | 38.80 | 64.18 | +65.43% |

These figures measure GEMM throughput on the configuration above; production model performance depends on the workload.

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

On S3 and S4, add `gpu_unlock.targets=all` inside `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`, preserving all existing options, including `amd_iommu=on iommu=pt` on these nodes. S1 instead uses this explicit seven-card target list:

```text
gpu_unlock.targets=0000:01:00.0,0000:25:00.0,0000:41:00.0,0000:61:00.0,0000:a1:00.0,0000:c1:00.0,0000:e1:00.0
```

Then rebuild:

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

Confirm `gpu-unlock-early` precedes `udev`, which precedes `framebuffer`; all five firmware files reported by `modinfo` must be present. Confirm the existing NVIDIA modules remain unchanged in the generated image. Repeat this image verification whenever rebuilding the initramfs for this integration.

Use an OS reboot:

```bash
sudo systemctl reboot
```

## Boot verification

The all-card queries below apply to S3 and S4. On S1, use only the seven healthy UUIDs already passed to its agent for GPU queries and benchmark containers; see [Leaving out a faulty GPU](../services/determined/README.md#leaving-out-a-faulty-gpu).

```bash
cat /proc/sys/kernel/random/boot_id
cat /proc/cmdline
sudo journalctl -k -b -o short-monotonic | grep -E 'gpu_unlock|post-FLR|NVRM|nvidia'
nvidia-smi
sudo journalctl -k -b | grep -E 'NVRM: Xid|assert|fallen off'
systemctl --failed
```

Match the journal entries to every configured GPU PCI address: seven on S1 and eight each on S3 and S4. Each must have `gpu_unlock: unlocked` and a post-FLR readback of `SS0=88888888`, `SS1=00000008`. All completions must precede the early script's completion message and the first NVIDIA initialization. A loaded module by itself does not prove that any GPU unlocked. S1's excluded `0000:81:00.0` must have no unlock or post-FLR activity.

Verify the unchanged 350 W limits and run the same GEMM checks used for the baseline. On S3 and S4, the boot-order checks, register readbacks, and GEMM correctness all passed without new Xid/assert errors or failed systemd services.

## Benchmark procedure

Use the same container image, GPU set, power limits, and clock policy before and after the unlock. Record the baseline before enabling the kernel opt-in, then use separate output files after installation and boot verification.

The recorded run used `determinedai/pytorch-ngc@sha256:67ab2dc2f6fcfba90d2085e82ea2f3a8a5ef3205cbf3203f6d1f5490d000f525`, containing PyTorch `2.3.0a0+40ec155e58.nv24.03` and CUDA `12.4`. `NVIDIA_TF32_OVERRIDE` was unset.

The GEMM protocol was:

- Test each GPU serially with 8192 × 8192 inputs, using seed `20261025 + physical GPU index`. Preserve the physical index when a container renumbers S1's seven visible GPUs.
- Measure FP16 and BF16 inputs with FP32 accumulation settings: `allow_fp16_reduced_precision_reduction=False` and `allow_bf16_reduced_precision_reduction=False`. Explicitly enable `allow_tf32=True` for the FP32-input TF32 measurement.
- Warm up with 10 matrix multiplications, then time five groups of 20 iterations using CUDA events. Take the median group mean and calculate TFLOP/s as `2 * 8192**3 / seconds / 1e12`.
- After timing, check all outputs are finite and compare an 8 × 8 output tile against a CPU FP64 reference over the full reduction dimension. The relative L2 error limits were 0.02 for BF16 and 0.01 for FP16 and TF32.
- Save per-GPU timings, correctness results, power limits, temperatures, SM/memory clocks, and PyTorch/CUDA versions. The table above reports the median across each node's tested GPU set.

Confirm the baseline has no active unlock or kernel opt-in. For the after measurements, verify every GPU's boot-log entries as described above. Preserve the original baseline output.

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
