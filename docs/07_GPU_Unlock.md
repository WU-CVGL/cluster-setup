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
