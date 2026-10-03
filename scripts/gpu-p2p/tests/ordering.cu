// Write ordering of peer stores over PCIe BAR1 P2P: GPU A writes a buffer in GPU B's memory, then
// __threadfence_system(), then a sequence flag. Whoever sees the flag must see all of the data (CUDA memory
// model); a stale word means the fence does not order peer writes against the flag write.
// Each iteration: A writes the whole buffer (grid-stride, every thread fences, the last block to finish
// fences again and stores the flag), then a check kernel on B verifies the buffer. Variants:
//   host  flag in pinned host memory: the CPU waits for it, then launches the check kernel on B
//   B     flag in B's memory (control: data and flag take the same path); the check kernel on B, launched
//         before the writer, spins on the flag
//   C     flag in GPU C's memory; the check kernel on B spins on it over P2P (needs 3 GPUs)
// usage: ordering [--gpus a,b[,c]] [--iters N] [--mib N]
//   --gpus   writer A, data owner B, flag holder C (default 0,1,2; C left out with 2 GPUs)
//   --iters  iterations per variant (default 200); --mib: buffer size (default 64)
// Build: nvcc -O2 -arch=sm_<cc> -o ordering ordering.cu   (sm_86 on the RTX 3090, sm_89 on the RTX 4090)
// Output ends with "RESULT: PASS" (exit 0) or "RESULT: FAIL" (exit 1: stale words, or a flag that never
// arrived within 10 s). CUDA errors exit 2, usage errors 3.
#include <time.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <cuda_runtime.h>

#define CK(x)                                                                                   \
  do {                                                                                          \
    cudaError_t e_ = (x);                                                                       \
    if (e_ != cudaSuccess) {                                                                    \
      fprintf(stderr, "%s:%d %s: %s\n", __FILE__, __LINE__, #x, cudaGetErrorString(e_));        \
      exit(2);                                                                                  \
    }                                                                                           \
  } while (0)

typedef unsigned long long u64;
static const size_t MB = 1ull << 20;
static const u64 TIMEOUT_NS = 10000000000ULL;
static const int WRITER_BLOCKS = 512, CHECK_BLOCKS = 256, THREADS = 256;

__device__ __host__ __forceinline__ u64 mix(u64 x) {
  x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33;
  return x;
}
__device__ __forceinline__ u64 value(u64 seq, size_t i) { return mix((seq << 36) ^ i); }
__device__ __forceinline__ u64 globalTimer() {
  u64 t;
  asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(t));
  return t;
}

struct Stat {
  u64 bad, sum, expect, timeouts;
};

// *done must be 0 at launch; the last block to finish stores seq to *flag.
__global__ void writer(u64* data, size_t n, u64 seq, unsigned* done, volatile u64* flag) {
  __shared__ bool last;
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
    data[i] = value(seq, i);
  __threadfence_system();
  __syncthreads();
  if (threadIdx.x == 0) last = atomicAdd(done, 1u) == gridDim.x - 1;
  __syncthreads();
  if (last && threadIdx.x == 0) {
    __threadfence_system();
    *flag = seq;
  }
}

// Each block waits until *flag >= seq, then verifies its share of the buffer.
__global__ void checker(const u64* data, size_t n, u64 seq, const volatile u64* flag, Stat* st) {
  __shared__ int ok;
  if (threadIdx.x == 0) {
    ok = 1;
    u64 t0 = globalTimer();
    while (*flag < seq) {
      if (globalTimer() - t0 > TIMEOUT_NS) {
        ok = 0;
        atomicAdd(&st->timeouts, 1ULL);
        break;
      }
      __nanosleep(500);
    }
  }
  __syncthreads();
  if (!ok) return;
  __threadfence_system();  // acquire: no data load before the flag was seen
  u64 bad = 0, sum = 0, expect = 0;
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x) {
    u64 v = data[i], e = value(seq, i);
    bad += v != e;
    sum += v;
    expect += e;
  }
  if (bad) atomicAdd(&st->bad, bad);
  atomicAdd(&st->sum, sum);
  atomicAdd(&st->expect, expect);
}

static double now() {
  timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return ts.tv_sec + ts.tv_nsec * 1e-9;
}
static void enablePeer(int from, int to) {
  int can = 0;
  CK(cudaDeviceCanAccessPeer(&can, from, to));
  if (!can) {
    printf("RESULT: FAIL (GPU%d cannot access GPU%d)\n", from, to);
    exit(1);
  }
  CK(cudaSetDevice(from));
  cudaError_t e = cudaDeviceEnablePeerAccess(to, 0);
  if (e != cudaSuccess && e != cudaErrorPeerAccessAlreadyEnabled) CK(e);
  cudaGetLastError();
}
static void usage() {
  fprintf(stderr, "usage: ordering [--gpus a,b[,c]] [--iters N] [--mib N]\n");
  exit(3);
}

int main(int argc, char** argv) {
  int g[3] = {0, 1, 2}, ng = -1;
  long iters = 200, mib = 64;
  for (int k = 1; k < argc; k++) {
    if (!strcmp(argv[k], "--gpus") && k + 1 < argc) {
      ng = sscanf(argv[++k], "%d,%d,%d", &g[0], &g[1], &g[2]);
      if (ng < 2) usage();
    } else if (!strcmp(argv[k], "--iters") && k + 1 < argc) {
      iters = atol(argv[++k]);
      if (iters < 1) usage();
    } else if (!strcmp(argv[k], "--mib") && k + 1 < argc) {
      mib = atol(argv[++k]);
      if (mib < 1) usage();
    } else {
      usage();
    }
  }
  int n;
  CK(cudaGetDeviceCount(&n));
  if (n < 2) {
    printf("RESULT: FAIL (%d GPU, need at least 2)\n", n);
    return 1;
  }
  if (ng < 0) ng = n >= 3 ? 3 : 2;
  for (int k = 0; k < ng; k++)
    if (g[k] < 0 || g[k] >= n) usage();
  if (g[0] == g[1] || (ng == 3 && (g[2] == g[0] || g[2] == g[1]))) usage();
  const int a = g[0], b = g[1], c = ng == 3 ? g[2] : -1;
  const size_t bytes = (size_t)mib * MB, words = bytes / sizeof(u64);

  enablePeer(a, b);
  if (c >= 0) {
    enablePeer(a, c);
    enablePeer(b, c);
  }
  u64* data;
  Stat* st;
  u64 *flagB, *flagC = nullptr, *flagHost;
  unsigned* done;
  CK(cudaSetDevice(b));
  CK(cudaMalloc(&data, bytes));
  CK(cudaMalloc(&st, sizeof(Stat)));
  CK(cudaMalloc(&flagB, sizeof(u64)));
  if (c >= 0) {
    CK(cudaSetDevice(c));
    CK(cudaMalloc(&flagC, sizeof(u64)));
  }
  CK(cudaSetDevice(a));
  CK(cudaMalloc(&done, sizeof(unsigned)));
  CK(cudaHostAlloc(&flagHost, sizeof(u64), cudaHostAllocMapped | cudaHostAllocPortable));
  printf("writer GPU%d, data on GPU%d (%zu MiB), flag holder GPU%s, %ld iterations per variant\n", a, b, bytes / MB,
         c >= 0 ? std::to_string(c).c_str() : "- (2 GPUs: variant C skipped)", iters);
  fflush(stdout);

  bool ok = true;
  const char* names[3] = {"host", "B", "C"};
  for (int v = 0; v < 3; v++) {
    if (v == 2 && c < 0) continue;
    u64* flag = v == 0 ? flagHost : (v == 1 ? flagB : flagC);
    volatile u64* hostFlag = flagHost;
    if (v == 0) {
      *hostFlag = 0;
    } else {
      CK(cudaSetDevice(v == 1 ? b : c));
      CK(cudaMemset(flag, 0, sizeof(u64)));
      CK(cudaDeviceSynchronize());
    }
    CK(cudaSetDevice(b));
    CK(cudaMemset(st, 0, sizeof(Stat)));
    CK(cudaDeviceSynchronize());
    u64 hostTimeouts = 0;
    for (long it = 1; it <= iters; it++) {
      u64 seq = ((u64)(v + 1) << 24) | (u64)it;  // grows within a variant
      if (v != 0) {  // the check kernel waits on the device
        CK(cudaSetDevice(b));
        checker<<<CHECK_BLOCKS, THREADS>>>(data, words, seq, flag, st);
        CK(cudaGetLastError());
      }
      CK(cudaSetDevice(a));
      CK(cudaMemset(done, 0, sizeof(unsigned)));
      writer<<<WRITER_BLOCKS, THREADS>>>(data, words, seq, done, flag);
      CK(cudaGetLastError());
      if (v == 0) {  // the CPU waits, then starts the check on B without synchronizing with A
        double t0 = now();
        bool seen = true;
        while (*hostFlag < seq) {
          if (now() - t0 > TIMEOUT_NS * 1e-9) {
            seen = false;
            break;
          }
        }
        if (seen) {
          CK(cudaSetDevice(b));
          checker<<<CHECK_BLOCKS, THREADS>>>(data, words, seq, flag, st);
          CK(cudaGetLastError());
        } else {
          hostTimeouts++;
        }
      }
      CK(cudaSetDevice(b));
      CK(cudaDeviceSynchronize());
      CK(cudaSetDevice(a));
      CK(cudaDeviceSynchronize());  // iterations must not overlap
    }
    Stat s;
    CK(cudaSetDevice(b));
    CK(cudaMemcpy(&s, st, sizeof(Stat), cudaMemcpyDeviceToHost));
    s.timeouts += hostTimeouts;
    bool good = s.bad == 0 && s.timeouts == 0 && s.sum == s.expect;
    ok = ok && good;
    printf("flag in %-4s iterations %ld: stale words %llu, timeouts %llu, checksum %016llx expected %016llx  %s\n",
           names[v], iters, s.bad, s.timeouts, s.sum, s.expect, good ? "PASS" : "FAIL");
    fflush(stdout);
  }
  CK(cudaFreeHost(flagHost));
  printf("RESULT: %s\n", ok ? "PASS" : "FAIL");
  return ok ? 0 : 1;
}
