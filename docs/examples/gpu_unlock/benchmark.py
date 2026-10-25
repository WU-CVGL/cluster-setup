#!/usr/bin/env python3
"""GPU baseline: serial GEMM and all directed P2P copies; never alters clocks/power."""
import argparse
import ctypes as C
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

SEED = 20261025
SMI_FIELDS = ['index', 'uuid', 'pci.bus_id', 'name', 'driver_version', 'power.limit', 'power.draw',
              'temperature.gpu', 'clocks.current.sm', 'clocks.current.memory']

def smi():
    p = subprocess.run(['nvidia-smi', '--query-gpu=' + ','.join(SMI_FIELDS),
                        '--format=csv,noheader,nounits'], text=True, capture_output=True, check=True)
    return [dict(zip(SMI_FIELDS, (v.strip() for v in line.split(','))))
            for line in p.stdout.splitlines() if line.strip()]

def save(result, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    tmp.replace(path)

def run_gemm(args, result):
    import torch
    if os.environ.get('NVIDIA_TF32_OVERRIDE') == '0':
        raise RuntimeError('NVIDIA_TF32_OVERRIDE=0 would invalidate the TF32 baseline')
    if not torch.cuda.is_available():
        raise RuntimeError('PyTorch CUDA is unavailable')
    torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False
    torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = False
    if hasattr(torch.backends.cuda.matmul, 'allow_fp16_accumulation'):
        torch.backends.cuda.matmul.allow_fp16_accumulation = False
    torch.set_float32_matmul_precision('high')
    torch.backends.cuda.matmul.allow_tf32 = True
    result['torch'] = {'version': torch.__version__, 'cuda': torch.version.cuda,
                       'device_count': torch.cuda.device_count(),
                       'float32_matmul_precision': torch.get_float32_matmul_precision(),
                       'allow_tf32': torch.backends.cuda.matmul.allow_tf32,
                       'allow_fp16_reduced_precision_reduction': torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction,
                       'allow_bf16_reduced_precision_reduction': torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction,
                       'allow_fp16_accumulation': getattr(torch.backends.cuda.matmul, 'allow_fp16_accumulation', None)}
    selected_gpus = selected_devices(args, result, torch.cuda.device_count())
    result['gemm'] = []
    n = args.gemm_size
    print('GPU  mode                 median_ms     TFLOP/s  check', flush=True)
    with torch.inference_mode():
        for selected in selected_gpus:
            gpu = selected['cuda_ordinal']
            physical_gpu = selected['physical_index']
            torch.cuda.set_device(gpu)
            prop = torch.cuda.get_device_properties(gpu)
            for mode, dtype in [('FP16_FP32acc', torch.float16), ('BF16_FP32acc', torch.bfloat16), ('TF32', torch.float32)]:
                if dtype == torch.bfloat16 and not torch.cuda.is_bf16_supported():
                    raise RuntimeError(f'GPU{gpu} has no BF16 support')
                generator = torch.Generator(device=f'cuda:{gpu}').manual_seed(args.seed + physical_gpu)
                a = torch.randn((n, n), dtype=dtype, device=f'cuda:{gpu}', generator=generator)
                b = torch.randn((n, n), dtype=dtype, device=f'cuda:{gpu}', generator=generator)
                out = torch.empty((n, n), dtype=dtype, device=f'cuda:{gpu}')
                before = next(x for x in smi() if int(x['index']) == physical_gpu)
                for _ in range(args.warmup):
                    torch.mm(a, b, out=out)
                torch.cuda.synchronize(gpu)
                group_ms = []
                clock_samples = []
                for group in range(args.groups):
                    start = torch.cuda.Event(enable_timing=True)
                    end = torch.cuda.Event(enable_timing=True)
                    start.record()
                    for _ in range(args.iterations):
                        torch.mm(a, b, out=out)
                    end.record()
                    end.synchronize()
                    group_ms.append(start.elapsed_time(end) / args.iterations)
                    clock_samples.append(next(x for x in smi() if int(x['index']) == physical_gpu))
                # Numerical validation happens after all timed groups. CPU FP64
                # reference for an 8x8 output tile uses the entire reduction axis.
                finite = bool(torch.isfinite(out).all().item())
                tile = min(n, 8)
                reference = a[:tile, :].to(device='cpu', dtype=torch.float64) @ b[:, :tile].to(device='cpu', dtype=torch.float64)
                observed = out[:tile, :tile].to(device='cpu', dtype=torch.float64)
                error = observed - reference
                relative_l2 = float(torch.linalg.vector_norm(error) / torch.linalg.vector_norm(reference).clamp_min(1e-30))
                max_abs = float(error.abs().max())
                tolerance = 0.02 if mode == 'BF16_FP32acc' else 0.01
                valid = finite and relative_l2 <= tolerance
                median_ms = statistics.median(group_ms)
                row = {'gpu': physical_gpu, 'cuda_ordinal': gpu, 'pci_bus_id': selected['pci_bus_id'], 'physical_uuid': selected['uuid'], 'name': prop.name, 'uuid': str(getattr(prop, 'uuid', 'unavailable')), 'compute_capability': [prop.major, prop.minor],
                       'sm_count': prop.multi_processor_count, 'total_memory_bytes': prop.total_memory,
                       'mode': mode, 'input_dtype': str(dtype), 'output_dtype': str(dtype),
                       'accumulation': 'FP32', 'matrix_shape': [n, n, n], 'seed': args.seed + physical_gpu,
                       'warmup': args.warmup, 'groups': args.groups, 'iterations_per_group': args.iterations,
                       'group_mean_ms': group_ms, 'median_ms': median_ms,
                       'tflops': 2 * n ** 3 / (median_ms * 1e9),
                       'check': {'finite': finite, 'sample_shape': [tile, tile],
                                 'reference': 'CPU FP64 over full reduction dimension',
                                 'relative_l2': relative_l2, 'max_abs_error': max_abs,
                                 'relative_l2_limit': tolerance, 'passed': valid},
                       'smi_before': before, 'smi_after_each_group': clock_samples}
                result['gemm'].append(row)
                save(result, args.output)
                print(f'{physical_gpu:>3}  {mode:<19} {median_ms:>10.3f} {row["tflops"]:>11.2f}  {"PASS" if valid else "FAIL"}', flush=True)
                del a, b, out, reference, observed, error
                torch.cuda.empty_cache()
                if not valid:
                    raise RuntimeError(f'GEMM correctness failed on GPU{gpu} {mode}')

class Driver:
    def __init__(self):
        self.lib = C.CDLL('libcuda.so.1')
        p, u, i, z = C.c_void_p, C.c_uint64, C.c_int, C.c_size_t
        definitions = {
            'cuInit': [C.c_uint], 'cuDriverGetVersion': [C.POINTER(i)],
            'cuDeviceGetCount': [C.POINTER(i)], 'cuDeviceGet': [C.POINTER(i), i],
            'cuDeviceGetPCIBusId': [p, i, i],
            'cuDeviceCanAccessPeer': [C.POINTER(i), i, i],
            'cuCtxCreate_v2': [C.POINTER(p), C.c_uint, i], 'cuCtxSetCurrent': [p],
            'cuCtxEnablePeerAccess': [p, C.c_uint], 'cuCtxSynchronize': [],
            'cuMemAlloc_v2': [C.POINTER(u), z], 'cuMemFree_v2': [u],
            'cuMemcpyHtoD_v2': [u, p, z], 'cuMemcpyDtoH_v2': [p, u, z],
            'cuMemcpyPeer': [u, p, u, p, z], 'cuCtxDestroy_v2': [p]}
        for name, types in definitions.items():
            f = getattr(self.lib, name)
            f.argtypes = types
            f.restype = i
    def call(self, name, *values):
        rc = getattr(self.lib, name)(*values)
        if rc:
            raise RuntimeError(f'{name} returned CUDA error {rc}')

def selected_devices(args, result, visible_count):
    if visible_count != args.expected_gpus:
        raise RuntimeError(f'Expected {args.expected_gpus} CUDA-visible GPUs, found {visible_count}')
    d = Driver()
    d.call('cuInit', 0)
    def bus_key(text):
        domain, bus, rest = text.strip().split(':')
        device, function = rest.split('.')
        return tuple(int(x, 16) for x in [domain, bus, device, function])
    physical = {bus_key(x['pci.bus_id']): x for x in smi()}
    mapped = []
    for ordinal in range(visible_count):
        device = C.c_int()
        d.call('cuDeviceGet', C.byref(device), ordinal)
        bus = C.create_string_buffer(32)
        d.call('cuDeviceGetPCIBusId', C.cast(bus, C.c_void_p), 32, device)
        bus_id = bus.value.decode()
        record = physical[bus_key(bus_id)]
        mapped.append({'physical_index': int(record['index']), 'cuda_ordinal': ordinal,
                       'pci_bus_id': bus_id, 'uuid': record['uuid']})
    requested = ([x['physical_index'] for x in mapped] if args.gpu_indices is None
                 else [int(x.strip()) for x in args.gpu_indices.split(',') if x.strip()])
    if not requested or len(requested) != len(set(requested)):
        raise RuntimeError('gpu-indices must be a nonempty list without duplicates')
    by_index = {x['physical_index']: x for x in mapped}
    absent = sorted(set(requested) - set(by_index))
    if absent:
        raise RuntimeError(f'Requested physical GPUs are not CUDA-visible: {absent}')
    excluded_uuids = {x.strip().lower() for x in (args.exclude_uuids or '').split(',') if x.strip()}
    selected = [by_index[x] for x in requested if by_index[x]['uuid'].lower() not in excluded_uuids]
    if not selected:
        raise RuntimeError('No GPUs remain after UUID exclusions')
    if args.expected_selected_gpus is not None and len(selected) != args.expected_selected_gpus:
        raise RuntimeError(f'Expected {args.expected_selected_gpus} selected GPUs after UUID exclusion, found {len(selected)}')
    # Final guard immediately before either caller is allowed to create any context.
    if any(x['uuid'].lower() in excluded_uuids for x in selected):
        raise RuntimeError('Excluded GPU UUID remains in selection')
    result['gpu_selection'] = {'requested_physical_indices': requested,
                              'excluded_uuid_arguments': sorted(excluded_uuids),
                              'excluded_uuid_matches': [x for x in mapped if x['uuid'].lower() in excluded_uuids],
                              'excluded_physical_indices': sorted(set(by_index) - {x['physical_index'] for x in selected}),
                              'all_visible_physical_mapping': mapped,
                              'selected': selected,
                              'policy': 'Only selected GPUs receive contexts, generators, allocations or compute; excluded devices are queried for identity only'}
    print('Selected physical GPUs:', ','.join(str(x['physical_index']) for x in selected), flush=True)
    print('Excluded UUIDs:', ','.join(sorted(excluded_uuids)) or '(none)', flush=True)
    return selected

def run_p2p(args, result):
    d = Driver()
    d.call('cuInit', 0)
    version = C.c_int()
    d.call('cuDriverGetVersion', C.byref(version))
    count = C.c_int()
    d.call('cuDeviceGetCount', C.byref(count))
    selected = selected_devices(args, result, count.value)
    result['p2p_driver_api_version'] = version.value
    result['p2p'] = []
    devices, contexts, send, recv, expected_hash = [], [], [], [], []
    size = args.p2p_mib * 1024 * 1024
    readback = C.create_string_buffer(size)
    print('P2P pair       GB/s       ms/copy  check', flush=True)
    try:
        for selected_gpu in selected:
            gpu = selected_gpu['cuda_ordinal']
            physical_gpu = selected_gpu['physical_index']
            device = C.c_int()
            d.call('cuDeviceGet', C.byref(device), gpu)
            devices.append(device)
            ctx = C.c_void_p()
            d.call('cuCtxCreate_v2', C.byref(ctx), 0, device)
            contexts.append(ctx)
            a, b = C.c_uint64(), C.c_uint64()
            d.call('cuMemAlloc_v2', C.byref(a), size)
            send.append(a)
            d.call('cuMemAlloc_v2', C.byref(b), size)
            recv.append(b)
            data = bytes((j + physical_gpu * 31) % 256 for j in range(256)) * (size // 256)
            expected_hash.append(hashlib.sha256(data).hexdigest())
            host = C.create_string_buffer(data)
            d.call('cuMemcpyHtoD_v2', a, C.cast(host, C.c_void_p), size)
            del data, host
        # Establish every directed relationship BEFORE any timing. A copy may
        # use either context's access to its peer; enabling one direction only
        # as pairs are visited creates an order-dependent host-staging fallback.
        for dst in range(len(selected)):
            d.call('cuCtxSetCurrent', contexts[dst])
            for src in range(len(selected)):
                if src == dst:
                    continue
                cap = C.c_int()
                d.call('cuDeviceCanAccessPeer', C.byref(cap), devices[dst], devices[src])
                if cap.value != 1:
                    raise RuntimeError(f'GPU{dst} cannot access GPU{src}')
                d.call('cuCtxEnablePeerAccess', contexts[src], 0)
        result['p2p_peer_setup'] = f'All {len(selected) * (len(selected) - 1)} directed selected context peer relationships enabled before any timed copy; both directions active for every pair'
        for src in range(len(selected)):
            for dst in range(len(selected)):
                if src == dst:
                    continue
                d.call('cuCtxSetCurrent', contexts[dst])
                for _ in range(args.p2p_warmup):
                    d.call('cuMemcpyPeer', recv[dst], contexts[dst], send[src], contexts[src], size)
                d.call('cuCtxSynchronize')
                start = time.perf_counter_ns()
                for _ in range(args.p2p_copies):
                    d.call('cuMemcpyPeer', recv[dst], contexts[dst], send[src], contexts[src], size)
                d.call('cuCtxSynchronize')
                elapsed = (time.perf_counter_ns() - start) / 1e9
                d.call('cuMemcpyDtoH_v2', C.cast(readback, C.c_void_p), recv[dst], size)
                digest = hashlib.sha256(memoryview(readback).cast('B')).hexdigest()
                valid = digest == expected_hash[src]
                row = {'src': selected[src]['physical_index'], 'dst': selected[dst]['physical_index'],
                       'src_cuda_ordinal': selected[src]['cuda_ordinal'], 'dst_cuda_ordinal': selected[dst]['cuda_ordinal'], 'bytes_per_copy': size,
                       'warmup': args.p2p_warmup, 'copies': args.p2p_copies,
                       'timing': 'perf_counter_ns wall clock around synchronous cuMemcpyPeer copies plus final cuCtxSynchronize',
                       'elapsed_seconds': elapsed, 'ms_per_copy': elapsed * 1000 / args.p2p_copies,
                       'gb_per_second': size * args.p2p_copies / elapsed / 1e9,
                       'peer_access_enabled': True, 'full_buffer_sha256_passed': valid,
                       'source_sha256': expected_hash[src], 'received_sha256': digest}
                result['p2p'].append(row)
                save(result, args.output)
                print(f'{row["src"]}->{row["dst"]:<10} {row["gb_per_second"]:>8.3f} {row["ms_per_copy"]:>13.3f}  {"PASS" if valid else "FAIL"}', flush=True)
                if not valid:
                    raise RuntimeError(f'GPU{src}->GPU{dst} P2P data mismatch')
        values = [r['gb_per_second'] for r in result['p2p']]
        result['p2p_summary_gb_per_second'] = {'min': min(values), 'median': statistics.median(values), 'max': max(values)}
    finally:
        for gpu, ctx in enumerate(contexts):
            d.lib.cuCtxSetCurrent(ctx)
            if gpu < len(send):
                d.lib.cuMemFree_v2(send[gpu])
            if gpu < len(recv):
                d.lib.cuMemFree_v2(recv[gpu])
            d.lib.cuCtxDestroy_v2(ctx)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=['all', 'gemm', 'p2p'], default='all')
    p.add_argument('--phase', choices=['before', 'after'], default='before', help='before refuses an active unlock; after requires gpu_unlock loaded')
    p.add_argument('--output', default='gpu-baseline-20261025.json')
    p.add_argument('--node-label', default=platform.node())
    p.add_argument('--gpu-indices', default=None, help='Comma-separated nvidia-smi physical indices; default all visible GPUs')
    p.add_argument('--exclude-uuids', default=None, help='Comma-separated immutable GPU UUIDs that must never receive a context or allocation')
    p.add_argument('--expected-selected-gpus', type=int, default=None, help='Require this many selected GPUs after physical-index and UUID filtering')
    p.add_argument('--expected-gpus', type=int, default=8, help='Expected CUDA-visible enumeration count; exclusion does not change this count')
    p.add_argument('--seed', type=int, default=SEED)
    p.add_argument('--gemm-size', type=int, default=8192)
    p.add_argument('--warmup', type=int, default=10)
    p.add_argument('--groups', type=int, default=5)
    p.add_argument('--iterations', type=int, default=20)
    p.add_argument('--p2p-mib', type=int, default=256)
    p.add_argument('--p2p-warmup', type=int, default=5)
    p.add_argument('--p2p-copies', type=int, default=20)
    args = p.parse_args()
    for name in ['gemm_size', 'warmup', 'groups', 'iterations', 'p2p_mib', 'p2p_warmup', 'p2p_copies']:
        if getattr(args, name) <= 0:
            p.error(name + ' must be positive')
    kernel_modules = Path('/proc/modules').read_text()
    kernel_cmdline = Path('/proc/cmdline').read_text().strip()
    unlock_loaded = any(line.split()[0].replace('-', '_') == 'gpu_unlock' for line in kernel_modules.splitlines())
    target_parameters = [x for x in kernel_cmdline.split() if 'targets=' in x and ('gpu_unlock' in x or 'gpu-unlock' in x)]
    if args.phase == 'before' and (unlock_loaded or target_parameters):
        raise RuntimeError('gpu-unlock is present; the before phase requires the pre-unlock baseline')
    if args.phase == 'after' and not unlock_loaded:
        raise RuntimeError('gpu_unlock is not loaded; refusing to label this an after-unlock result')
    try:
        dkms = subprocess.run(['dkms', 'status'], text=True, capture_output=True, timeout=30)
        dkms_state = {'returncode': dkms.returncode, 'stdout': dkms.stdout.strip()}
    except FileNotFoundError:
        dkms_state = {'unavailable': 'dkms executable absent in this runtime; capture host dkms separately'}
    result = {'schema_version': 2, 'host': platform.node(), 'node_label': args.node_label, 'platform': platform.platform(),
              'phase': args.phase, 'gpu_unlock_state': {'module_loaded': unlock_loaded, 'target_parameters': target_parameters, 'kernel_cmdline': kernel_cmdline, 'dkms': dkms_state},
              'python': sys.version, 'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'arguments': vars(args), 'smi_start': smi(),
              'policy': 'No power limits or clocks changed. GPUs tested serially. gpu-unlock not installed by this harness.',
              'environment': {k: os.environ.get(k) for k in ['CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES', 'NVIDIA_TF32_OVERRIDE', 'CUDA_DEVICE_ORDER']},
              'status': 'running'}
    save(result, args.output)
    try:
        if args.mode in ['all', 'gemm']:
            run_gemm(args, result)
        if args.mode in ['all', 'p2p']:
            run_p2p(args, result)
        result['status'] = 'passed'
    except BaseException as e:
        result['status'] = 'failed'
        result['error'] = f'{type(e).__name__}: {e}'
        raise
    finally:
        result['smi_end'] = smi()
        result['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save(result, args.output)
        print(f'JSON {args.output} status={result["status"]}', flush=True)

if __name__ == '__main__':
    main()
