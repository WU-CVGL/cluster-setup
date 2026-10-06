#!/bin/bash
# Data-verified GPU-to-GPU copies over P2P. A GPU workload: idle node only (refuses while GPUs run compute
# processes).
#  1. every ordered pair must have peer access; then a 256 MiB random block is copied i->j and back j->i and
#     compared bit for bit;
#  2. whole-memory coverage: on each destination GPU j, nearly all free memory is filled with 512 MiB blocks,
#     each written from a peer (i = j+1 mod n), read back and compared, so peer writes also land in the top
#     of the memory (the part a truncated static BAR1 map misses).
# usage: ./p2p-copy-check.sh <image> <out-file>     image: a CUDA + PyTorch image
# Ends with "RESULT ok" (exit 0) or "RESULT fail: ..." (exit 1: a mismatch or a pair without peer access).
set -euo pipefail
IMAGE=${1:?usage: p2p-copy-check.sh <image> <out-file>}
OUT=${2:?usage: p2p-copy-check.sh <image> <out-file>}
busy=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | grep -c . || true)
[ "$busy" -eq 0 ] || { echo "REFUSED: $busy compute processes on the GPUs"; exit 3; }
docker run --rm --init --gpus all -e CUDA_DEVICE_ORDER=PCI_BUS_ID --entrypoint python3 "$IMAGE" -c '
import sys, torch
n = torch.cuda.device_count(); bad = 0
for i in range(n):
    for j in range(n):
        if i != j and not torch.cuda.can_device_access_peer(i, j):
            print(f"NO-PEER {i}->{j}", flush=True); bad += 1
if bad:
    print(f"RESULT fail: {bad} pairs without peer access (copies would be staged through host)"); sys.exit(1)
g = torch.Generator(device="cpu").manual_seed(1)
blk = torch.randint(-2**62, 2**62, (32 << 20,), dtype=torch.int64, generator=g)  # 256 MiB
for i in range(n):
    x = blk.to(i)
    for j in range(n):
        if i == j: continue
        y = x.to(j); z = y.to(i); torch.cuda.synchronize(i); torch.cuda.synchronize(j)
        ok = torch.equal(x, z) and torch.equal(y.cpu(), blk)
        res = "ok" if ok else "MISMATCH"
        print(f"pair {i}->{j} {res}", flush=True); bad += (not ok)
for j in range(n):
    i = (j + 1) % n
    src = blk.repeat(2).to(i)  # 512 MiB pattern on the source
    free, _ = torch.cuda.mem_get_info(j)
    k = max(0, free // (512 << 20) - 2)
    dst = [torch.empty(64 << 20, dtype=torch.int64, device=j) for _ in range(k)]
    nbad = 0
    for t, d in enumerate(dst):
        d.copy_(src.add(t)); torch.cuda.synchronize(j)
        back = d.to(i); torch.cuda.synchronize(i)
        nbad += not torch.equal(back, src.add(t))
    print(f"fill GPU{j} from GPU{i}: {k} x 512 MiB blocks, {nbad} mismatched", flush=True)
    bad += nbad; del dst, src; torch.cuda.empty_cache()
print("RESULT " + ("ok" if bad == 0 else f"fail: {bad} mismatches")); sys.exit(1 if bad else 0)
' | tee "$OUT"
