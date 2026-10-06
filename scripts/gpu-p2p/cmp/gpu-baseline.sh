#!/bin/bash
# Per-GPU SM count, memory as CUDA sees it (total/free) and device-to-device copy bandwidth, to compare
# two driver builds. A GPU workload: idle node only (refuses while GPUs run compute processes).
# usage: ./gpu-baseline.sh <image> <out-file>      image: a CUDA + PyTorch image
set -euo pipefail
IMAGE=${1:?usage: gpu-baseline.sh <image> <out-file>}
OUT=${2:?usage: gpu-baseline.sh <image> <out-file>}
busy=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | grep -c . || true)
[ "$busy" -eq 0 ] || { echo "REFUSED: $busy compute processes on the GPUs"; exit 3; }
docker run --rm --init --gpus all -e CUDA_DEVICE_ORDER=PCI_BUS_ID --entrypoint python3 "$IMAGE" -c '
import torch, time
for i in range(torch.cuda.device_count()):
    p = torch.cuda.get_device_properties(i)
    free, total = torch.cuda.mem_get_info(i)
    x = torch.empty(1 << 30, dtype=torch.uint8, device=i); y = torch.empty_like(x)
    for _ in range(3): y.copy_(x)
    torch.cuda.synchronize(i); t = time.time()
    for _ in range(20): y.copy_(x)
    torch.cuda.synchronize(i); dt = time.time() - t
    print(f"GPU{i} sm={p.multi_processor_count} total={total >> 20}MiB free={free >> 20}MiB d2d={2 * 20 * (1 << 30) / dt / 1e9:.1f}GB/s", flush=True)
' | tee "$OUT"
