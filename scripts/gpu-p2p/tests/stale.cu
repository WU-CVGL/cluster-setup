// Stale peer reads over PCIe BAR1 P2P: GPU A reads a buffer in GPU B's memory, B overwrites it, A waits for
// B's event and reads again; the second read must see the new data (a stale word means A served the peer
// data from a cache that the event wait did not invalidate). The buffer (default 8 MiB) fits into A's L2.
// Per iteration, without host synchronization (only events):
//   A: kernel reads the old data (checked too), records e1
//   B: waits for e1, overwrites the buffer (variant sm: a kernel; variant ce: cudaMemcpyAsync from a
//      staging buffer on B, i.e. B's copy engine), records e2
//   A: waits for e2, kernel reads the new data
// usage: stale [--pair a,b] [--iters N] [--mib N]
//   --pair a,b  reader A, owner B (default: every ordered pair); --iters per pair and variant (default 200)
// Build: nvcc -O2 -arch=sm_89 -o stale stale.cu
// Output ends with "RESULT: PASS" (exit 0) or "RESULT: FAIL" (exit 1). CUDA errors exit 2, usage errors 3.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <utility>
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

__device__ __host__ __forceinline__ u64 mix(u64 x) {
  x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33;
  return x;
}

struct Stat {
  u64 bad, sum, expect;
};

__global__ void fill(u64* p, size_t n, u64 seed) {
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
    p[i] = mix(seed ^ i);
}
__global__ void verify(const u64* p, size_t n, u64 seed, Stat* st) {
  u64 bad = 0, sum = 0, expect = 0;
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x) {
    u64 v = p[i], e = mix(seed ^ i);
    bad += v != e;
    sum += v;
    expect += e;
  }
  if (bad) atomicAdd(&st->bad, bad);
  atomicAdd(&st->sum, sum);
  atomicAdd(&st->expect, expect);
}

// Returns false on a mismatch. Stats live on A: [0] first read (old data), [1] second read (new data).
static bool testPair(int a, int b, long iters, size_t bytes) {
  const size_t n = bytes / sizeof(u64);
  int can = 0;
  CK(cudaDeviceCanAccessPeer(&can, a, b));
  if (!can) {
    printf("  %d<-%d: FAIL (no peer access)\n", a, b);
    return false;
  }
  CK(cudaSetDevice(a));
  cudaError_t e = cudaDeviceEnablePeerAccess(b, 0);
  if (e != cudaSuccess && e != cudaErrorPeerAccessAlreadyEnabled) CK(e);
  cudaGetLastError();
  Stat* st;
  cudaStream_t sA, sB;
  cudaEvent_t e1, e2;
  CK(cudaMalloc(&st, 2 * sizeof(Stat)));
  CK(cudaStreamCreateWithFlags(&sA, cudaStreamNonBlocking));
  CK(cudaEventCreateWithFlags(&e1, cudaEventDisableTiming));
  u64 *data, *staging;
  CK(cudaSetDevice(b));
  CK(cudaMalloc(&data, bytes));
  CK(cudaMalloc(&staging, bytes));
  CK(cudaStreamCreateWithFlags(&sB, cudaStreamNonBlocking));
  CK(cudaEventCreateWithFlags(&e2, cudaEventDisableTiming));

  bool ok = true;
  const char* names[2] = {"sm", "ce"};
  for (int v = 0; v < 2; v++) {
    u64 base = ((u64)a << 56) | ((u64)b << 48) | ((u64)v << 44);
    CK(cudaSetDevice(b));
    fill<<<1024, 256, 0, sB>>>(data, n, base);
    CK(cudaGetLastError());
    CK(cudaStreamSynchronize(sB));
    CK(cudaSetDevice(a));
    CK(cudaMemsetAsync(st, 0, 2 * sizeof(Stat), sA));
    for (long it = 1; it <= iters; it++) {
      u64 oldSeed = base + ((u64)(it - 1) << 24), newSeed = base + ((u64)it << 24);
      CK(cudaSetDevice(a));
      verify<<<1024, 256, 0, sA>>>(data, n, oldSeed, st);
      CK(cudaGetLastError());
      CK(cudaEventRecord(e1, sA));
      CK(cudaSetDevice(b));
      if (v == 1) {
        fill<<<1024, 256, 0, sB>>>(staging, n, newSeed);
        CK(cudaGetLastError());
      }
      CK(cudaStreamWaitEvent(sB, e1, 0));
      if (v == 0) {
        fill<<<1024, 256, 0, sB>>>(data, n, newSeed);
        CK(cudaGetLastError());
      } else {
        CK(cudaMemcpyAsync(data, staging, bytes, cudaMemcpyDeviceToDevice, sB));
      }
      CK(cudaEventRecord(e2, sB));
      CK(cudaSetDevice(a));
      CK(cudaStreamWaitEvent(sA, e2, 0));
      verify<<<1024, 256, 0, sA>>>(data, n, newSeed, st + 1);
      CK(cudaGetLastError());
    }
    Stat s[2];
    CK(cudaSetDevice(a));
    CK(cudaMemcpyAsync(s, st, sizeof(s), cudaMemcpyDeviceToHost, sA));
    CK(cudaStreamSynchronize(sA));
    CK(cudaSetDevice(b));
    CK(cudaStreamSynchronize(sB));
    bool good = s[0].bad == 0 && s[1].bad == 0 && s[0].sum == s[0].expect && s[1].sum == s[1].expect;
    ok = ok && good;
    printf("  A=%d B=%d %s: first read bad %llu, re-read stale %llu, checksum %016llx expected %016llx  %s\n", a, b,
           names[v], s[0].bad, s[1].bad, s[1].sum, s[1].expect, good ? "ok" : "MISMATCH");
    fflush(stdout);
  }
  CK(cudaSetDevice(b));
  CK(cudaFree(data));
  CK(cudaFree(staging));
  CK(cudaStreamDestroy(sB));
  CK(cudaEventDestroy(e2));
  CK(cudaSetDevice(a));
  CK(cudaFree(st));
  CK(cudaStreamDestroy(sA));
  CK(cudaEventDestroy(e1));
  return ok;
}

static void usage() {
  fprintf(stderr, "usage: stale [--pair a,b] [--iters N] [--mib N]\n");
  exit(3);
}

int main(int argc, char** argv) {
  int pa = -1, pb = -1;
  long iters = 200, mib = 8;
  for (int k = 1; k < argc; k++) {
    if (!strcmp(argv[k], "--pair") && k + 1 < argc) {
      if (sscanf(argv[++k], "%d,%d", &pa, &pb) != 2) usage();
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
  std::vector<std::pair<int, int>> pairs;
  if (pa >= 0 || pb >= 0) {
    if (pa == pb || pa < 0 || pb < 0 || pa >= n || pb >= n) usage();
    pairs.push_back(std::make_pair(pa, pb));
  } else {
    for (int a = 0; a < n; a++)
      for (int b = 0; b < n; b++)
        if (a != b) pairs.push_back(std::make_pair(a, b));
  }
  printf("stale peer reads: %zu ordered pairs, %ld MiB, %ld iterations per pair and variant\n", pairs.size(), mib,
         iters);
  fflush(stdout);
  int bad = 0;
  for (size_t k = 0; k < pairs.size(); k++) bad += !testPair(pairs[k].first, pairs[k].second, iters, (size_t)mib * MB);
  printf("RESULT: %s (%zu pairs, %d with mismatches)\n", bad ? "FAIL" : "PASS", pairs.size(), bad);
  return bad ? 1 : 0;
}
