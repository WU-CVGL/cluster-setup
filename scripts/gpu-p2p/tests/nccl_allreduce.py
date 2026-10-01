"""NCCL all-reduce bandwidth and correctness over the visible GPUs, one process per GPU.

usage: torchrun --standalone --nproc_per_node=<gpus> nccl_allreduce.py

For every size: warm-up, a correctness check (each rank contributes rank+1, the result must be
world*(world+1)/2 everywhere), then the mean time of ITERS all-reduces with algbw and busbw
(busbw = algbw * 2 * (world-1) / world, comparable across GPU counts).

With RESIDENT_GB > 0 every rank first fills that many GiB with a rank-dependent int32 pattern and
checks it after the collectives, in 1 GiB chunks. On GPUs whose BAR1 is smaller than VRAM (dynamic BAR1
P2P) this shows that peer traffic uses only NCCL's own buffers and never overwrites other memory.

Env: SIZES_MB (default "64,256,1024"), ITERS (default 10), RESIDENT_GB (default 0).
Output ends with "RESULT: PASS" or "RESULT: FAIL"; every rank exits 1 on failure.
Run with NCCL_DEBUG=INFO to see the transport ("via P2P/IPC", "via P2P/CUMEM", "via SHM").
"""
import os
import sys
import time

import torch
import torch.distributed as dist

GiB = 1 << 30
ROW = 1 << 20  # int32 elements per row of the resident tensor (4 MiB)
CHUNK_ROWS = 256  # rows compared at once (1 GiB)


def main() -> int:
    rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(rank)
    dist.init_process_group("nccl")
    world = dist.get_world_size()
    sizes_mb = [int(s) for s in os.environ.get("SIZES_MB", "64,256,1024").split(",") if s.strip()]
    iters = int(os.environ.get("ITERS", "10"))
    resident_gb = float(os.environ.get("RESIDENT_GB", "0"))

    resident = base = None
    if resident_gb > 0:
        # Rows of a rank-dependent pattern, so an overwrite anywhere changes the comparison.
        rows = int(resident_gb * GiB) // 4 // ROW
        base = torch.arange(ROW, dtype=torch.int32, device="cuda").mul_(7).add_(rank * 1_000_003)
        resident = base.repeat(rows).view(rows, ROW)
        torch.cuda.synchronize()

    expected = world * (world + 1) / 2
    ok = True
    for size_mb in sizes_mb:
        t = torch.full((size_mb * 2**20 // 4,), float(rank + 1), device="cuda")
        for _ in range(3):
            dist.all_reduce(t)
        t.fill_(float(rank + 1))
        dist.all_reduce(t)
        good = bool(torch.all(t == expected).item())
        ok = ok and good
        torch.cuda.synchronize()
        dist.barrier()
        start = time.perf_counter()
        for _ in range(iters):
            dist.all_reduce(t)
        torch.cuda.synchronize()
        dt = (time.perf_counter() - start) / iters
        algbw = size_mb * 2**20 / dt / 1e9
        flags = torch.tensor([int(good)], device="cuda")
        dist.all_reduce(flags, op=dist.ReduceOp.MIN)
        if rank == 0:
            print(
                f"all_reduce {size_mb:5d} MiB x{world}: {dt * 1e3:8.2f} ms  algbw {algbw:6.1f} GB/s  "
                f"busbw {algbw * 2 * (world - 1) / world:6.1f} GB/s  correct={bool(flags.item())}",
                flush=True,
            )
        del t

    intact = True
    if resident is not None:
        torch.cuda.synchronize()
        # Chunked: a whole-tensor comparison would need a temporary that does not fit next to it.
        for i in range(0, resident.shape[0], CHUNK_ROWS):
            chunk = resident[i:i + CHUNK_ROWS]
            if not torch.equal(chunk, base.expand_as(chunk)):
                intact = False
                break
    stats = torch.tensor([int(ok), int(intact)], device="cuda")
    dist.all_reduce(stats, op=dist.ReduceOp.MIN)
    passed = bool(stats[0].item()) and bool(stats[1].item())
    if rank == 0:
        if resident is not None:
            peak = torch.cuda.max_memory_allocated() / GiB
            print(
                f"resident {resident_gb:.0f} GiB/GPU (peak {peak:.1f} GiB): collectives correct={bool(stats[0].item())}, "
                f"resident intact={bool(stats[1].item())}",
                flush=True,
            )
        print(f"RESULT: {'PASS' if passed else 'FAIL'}", flush=True)
    dist.destroy_process_group()
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
