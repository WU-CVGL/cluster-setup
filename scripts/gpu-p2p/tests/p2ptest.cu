// P2P check for the patched open kernel modules: peer access matrix, data integrity of peer stores,
// peer loads and copy-engine copies across the whole VRAM (BAR1 may be smaller than VRAM), and
// copy bandwidth with peer access disabled (staged through the host) vs enabled, summarized by
// topology (GPUs on the same CPU socket / NUMA node vs across sockets), and host<->GPU bandwidth.
// Uses the legacy whole-device peer access (cudaDeviceEnablePeerAccess), which maps every allocation of
// the peer: with dynamic BAR1 P2P (BAR1 smaller than VRAM) the test buffer must fit into BAR1, so cap it
// with P2PTEST_BUF_GB (e.g. 2). Default: all free memory minus 2 GiB.
// Build: nvcc -O2 -arch=sm_<cc> -o p2ptest p2ptest.cu   (sm_86 on the RTX 3090, sm_89 on the RTX 4090)
// Output ends with "RESULT: PASS" (exit 0) when every ordered GPU pair has working peer access and all
// integrity checks are clean, else "RESULT: FAIL ..." (exit 1). CUDA errors exit 2.
#include <sched.h>
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
static const size_t MB = 1ull << 20, GB = 1ull << 30;
static const size_t CHUNK = 64 * MB;  // integrity test block
static const int REGIONS = 12;        // blocks spread over each GPU's buffer

__device__ __forceinline__ u64 mix(u64 x) {
  x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33;
  return x;
}
__global__ void fill(u64* dst, size_t n, u64 seed) {
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
    dst[i] = mix(seed ^ i);
}
__global__ void verify(const u64* src, size_t n, u64 seed, u64* bad) {
  u64 local = 0;
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
    local += src[i] != mix(seed ^ i);
  if (local) atomicAdd(bad, local);
}

int n;
std::vector<int> numa;
std::vector<std::string> localCpus;
std::vector<char*> big;
std::vector<size_t> bigSize;
std::vector<u64*> badCounter;

static size_t regionOffset(int d, int r) {
  size_t step = (bigSize[d] - CHUNK) / (REGIONS - 1);
  return (step * r) / CHUNK * CHUNK;
}
// Runs `fill` on GPU `writer` into GPU `owner`'s region, then `verify` on GPU `reader`.
static u64 roundTrip(int writer, int owner, int reader, int r, u64 seed) {
  u64* p = (u64*)(big[owner] + regionOffset(owner, r));
  size_t words = CHUNK / sizeof(u64);
  CK(cudaSetDevice(writer));
  fill<<<1024, 256>>>(p, words, seed);
  CK(cudaGetLastError());
  CK(cudaDeviceSynchronize());
  CK(cudaSetDevice(reader));
  CK(cudaMemset(badCounter[reader], 0, sizeof(u64)));
  verify<<<1024, 256>>>(p, words, seed, badCounter[reader]);
  CK(cudaGetLastError());
  u64 bad;
  CK(cudaMemcpy(&bad, badCounter[reader], sizeof(u64), cudaMemcpyDeviceToHost));
  return bad;
}

static double copyBandwidth(int src, int dst, bool bidirectional) {
  const size_t bytes = 256 * MB;
  const int reps = 10;
  // Forward copies read the start of src and write the end of dst; reverse copies the other way.
  char* a = big[src];
  char* b = big[dst] + bigSize[dst] - bytes;
  char* a2 = big[src] + bigSize[src] - bytes;
  char* b2 = big[dst];
  cudaStream_t s1, s2;
  cudaEvent_t start, stop, done2 = nullptr;
  CK(cudaSetDevice(src));
  CK(cudaStreamCreateWithFlags(&s1, cudaStreamNonBlocking));
  CK(cudaEventCreate(&start));
  CK(cudaEventCreate(&stop));
  CK(cudaSetDevice(dst));
  CK(cudaStreamCreateWithFlags(&s2, cudaStreamNonBlocking));
  CK(cudaSetDevice(src));
  CK(cudaMemcpyPeerAsync(b, dst, a, src, bytes, s1));  // warm-up
  CK(cudaStreamSynchronize(s1));
  CK(cudaEventRecord(start, s1));
  if (bidirectional) {
    CK(cudaStreamWaitEvent(s2, start, 0));
  }
  for (int i = 0; i < reps; i++) {
    CK(cudaMemcpyPeerAsync(b, dst, a, src, bytes, s1));
    if (bidirectional) CK(cudaMemcpyPeerAsync(a2, src, b2, dst, bytes, s2));
  }
  if (bidirectional) {
    CK(cudaSetDevice(dst));
    CK(cudaEventCreate(&done2));
    CK(cudaEventRecord(done2, s2));
    CK(cudaSetDevice(src));
    CK(cudaStreamWaitEvent(s1, done2, 0));
  }
  CK(cudaEventRecord(stop, s1));
  CK(cudaEventSynchronize(stop));
  float ms;
  CK(cudaEventElapsedTime(&ms, start, stop));
  CK(cudaEventDestroy(start));
  CK(cudaEventDestroy(stop));
  CK(cudaStreamDestroy(s1));
  CK(cudaSetDevice(dst));
  CK(cudaStreamDestroy(s2));
  if (done2) CK(cudaEventDestroy(done2));
  return (bidirectional ? 2.0 : 1.0) * bytes * reps / (ms / 1e3) / 1e9;
}

static void bandwidthMatrix(const char* title, bool bidirectional) {
  std::vector<std::vector<double>> m(n, std::vector<double>(n, 0.0));
  printf("\n%s (GB/s)\n     ", title);
  for (int j = 0; j < n; j++) printf("%7d", j);
  printf("\n");
  for (int i = 0; i < n; i++) {
    printf("%4d ", i);
    for (int j = 0; j < n; j++) {
      if (i != j) m[i][j] = copyBandwidth(i, j, bidirectional);
      printf("%7.1f", m[i][j]);
    }
    printf("\n");
  }
  const char* names[2] = {"same socket ", "cross socket"};
  for (int c = 0; c < 2; c++) {
    double lo = 1e30, hi = 0, sum = 0;
    int cnt = 0;
    for (int i = 0; i < n; i++)
      for (int j = 0; j < n; j++)
        if (i != j && (numa[i] != numa[j]) == c) {
          lo = m[i][j] < lo ? m[i][j] : lo;
          hi = m[i][j] > hi ? m[i][j] : hi;
          sum += m[i][j];
          cnt++;
        }
    if (cnt)
      printf("  %s: min %6.1f  mean %6.1f  max %6.1f GB/s  (%d ordered pairs)\n", names[c], lo, sum / cnt, hi, cnt);
  }
  fflush(stdout);
}

static std::string readFile(const std::string& path) {
  FILE* f = fopen(path.c_str(), "r");
  if (!f) return "";
  char buf[512] = {0};
  size_t len = fread(buf, 1, sizeof(buf) - 1, f);
  fclose(f);
  while (len && (buf[len - 1] == '\n' || buf[len - 1] == ' ')) buf[--len] = 0;
  return buf;
}

// Pins the calling thread to the CPUs local to the GPU, so pinned host buffers land on its socket.
static void pinToGpuSocket(int d) {
  cpu_set_t set;
  CPU_ZERO(&set);
  std::string list = localCpus[d];
  for (char* tok = strtok(&list[0], ","); tok; tok = strtok(nullptr, ",")) {
    int a, b;
    if (sscanf(tok, "%d-%d", &a, &b) == 2) {
      for (int c = a; c <= b; c++) CPU_SET(c, &set);
    } else if (sscanf(tok, "%d", &a) == 1) {
      CPU_SET(a, &set);
    }
  }
  if (CPU_COUNT(&set)) sched_setaffinity(0, sizeof(set), &set);
}

static void hostBandwidth() {
  const size_t bytes = 256 * MB;
  const int reps = 10;
  cpu_set_t original;
  bool restore = sched_getaffinity(0, sizeof(original), &original) == 0;
  printf("\nHost <-> GPU, pinned memory on the GPU's socket (GB/s)\n  GPU  numa     H2D     D2H\n");
  for (int d = 0; d < n; d++) {
    pinToGpuSocket(d);
    CK(cudaSetDevice(d));
    char* h;
    CK(cudaHostAlloc(&h, bytes, cudaHostAllocDefault));
    memset(h, 1, bytes);
    cudaStream_t s;
    cudaEvent_t start, stop;
    CK(cudaStreamCreateWithFlags(&s, cudaStreamNonBlocking));
    CK(cudaEventCreate(&start));
    CK(cudaEventCreate(&stop));
    double gbps[2];
    for (int dir = 0; dir < 2; dir++) {
      void* dst = dir ? (void*)h : (void*)big[d];
      void* src = dir ? (void*)big[d] : (void*)h;
      cudaMemcpyKind kind = dir ? cudaMemcpyDeviceToHost : cudaMemcpyHostToDevice;
      CK(cudaMemcpyAsync(dst, src, bytes, kind, s));  // warm-up
      CK(cudaEventRecord(start, s));
      for (int i = 0; i < reps; i++) CK(cudaMemcpyAsync(dst, src, bytes, kind, s));
      CK(cudaEventRecord(stop, s));
      CK(cudaEventSynchronize(stop));
      float ms;
      CK(cudaEventElapsedTime(&ms, start, stop));
      gbps[dir] = (double)bytes * reps / (ms / 1e3) / 1e9;
    }
    printf("  %3d  %4d  %6.1f  %6.1f\n", d, numa[d], gbps[0], gbps[1]);
    CK(cudaStreamDestroy(s));
    CK(cudaEventDestroy(start));
    CK(cudaEventDestroy(stop));
    CK(cudaFreeHost(h));
  }
  if (restore) sched_setaffinity(0, sizeof(original), &original);
  fflush(stdout);
}

int main() {
  CK(cudaGetDeviceCount(&n));
  numa.resize(n);
  localCpus.resize(n);
  big.resize(n);
  bigSize.resize(n);
  badCounter.resize(n);
  for (int d = 0; d < n; d++) {
    cudaDeviceProp p;
    CK(cudaGetDeviceProperties(&p, d));
    CK(cudaSetDevice(d));
    size_t freeB, totalB;
    CK(cudaMemGetInfo(&freeB, &totalB));
    // P2PTEST_BUF_GB caps the buffer: with dynamic BAR1 P2P, whole-device peer access maps every
    // allocation into the 32 GiB BAR1 of its GPU.
    if (freeB < 3 * GB) {
      fprintf(stderr, "GPU%d has only %.1f GiB free: the node is not idle\n", d, freeB / (double)GB);
      return 2;
    }
    bigSize[d] = (freeB - 2 * GB) / CHUNK * CHUNK;
    if (getenv("P2PTEST_BUF_GB") && *getenv("P2PTEST_BUF_GB")) {
      size_t cap = (size_t)(atof(getenv("P2PTEST_BUF_GB")) * GB) / CHUNK * CHUNK;
      if (cap < GB) cap = GB;  // the bandwidth test uses 256 MiB at each end of the buffer
      if (cap < bigSize[d]) bigSize[d] = cap;
    }
    CK(cudaMalloc(&big[d], bigSize[d]));
    CK(cudaMalloc(&badCounter[d], sizeof(u64)));
    char dir[64];
    snprintf(dir, sizeof(dir), "/sys/bus/pci/devices/%04x:%02x:%02x.0", p.pciDomainID, p.pciBusID, p.pciDeviceID);
    numa[d] = atoi(readFile(std::string(dir) + "/numa_node").c_str());
    localCpus[d] = readFile(std::string(dir) + "/local_cpulist");
    printf("GPU%d %s %s numa %d cpus %s, total %.1f GiB, test buffer %.1f GiB\n", d, p.name, dir + 21, numa[d],
           localCpus[d].c_str(), totalB / (double)GB, bigSize[d] / (double)GB);
  }
  hostBandwidth();

  std::vector<std::vector<int>> can(n, std::vector<int>(n, 0));
  int pairs = 0;
  printf("\ncudaDeviceCanAccessPeer\n     ");
  for (int j = 0; j < n; j++) printf("%3d", j);
  printf("\n");
  for (int i = 0; i < n; i++) {
    printf("%4d ", i);
    for (int j = 0; j < n; j++) {
      if (i != j) CK(cudaDeviceCanAccessPeer(&can[i][j], i, j));
      pairs += can[i][j];
      printf("%3s", i == j ? "X" : (can[i][j] ? "1" : "0"));
    }
    printf("\n");
  }
  fflush(stdout);

  bandwidthMatrix("Unidirectional copy, peer access DISABLED", false);

  if (pairs == 0) {
    printf("\nRESULT: FAIL (no peer access between any GPU pair)\n");
    return 1;
  }
  // Failures are reported, not fatal: the remaining pairs are still tested.
  int enableFailures = 0;
  for (int i = 0; i < n; i++) {
    CK(cudaSetDevice(i));
    for (int j = 0; j < n; j++) {
      if (!can[i][j]) continue;
      cudaError_t e = cudaDeviceEnablePeerAccess(j, 0);
      if (e != cudaSuccess) {
        printf("cudaDeviceEnablePeerAccess %d->%d failed: %s\n", i, j, cudaGetErrorString(e));
        cudaGetLastError();
        can[i][j] = 0;
        enableFailures++;
      }
    }
  }
  if (enableFailures)
    printf("%d ordered pairs advertise peer access but cannot enable it (with dynamic BAR1 P2P: is the test "
           "buffer larger than BAR1? set P2PTEST_BUF_GB)\n",
           enableFailures);
  fflush(stdout);

  // Integrity across the whole buffer of every peer.
  u64 badStore = 0, badLoad = 0, badCopy = 0, blocks = 0;
  for (int i = 0; i < n; i++)
    for (int j = 0; j < n; j++) {
      if (i == j || !can[i][j] || !can[j][i]) continue;
      u64 pairStore = 0, pairLoad = 0, pairCopy = 0;
      for (int r = 0; r < REGIONS; r++) {
        u64 seed = ((u64)i << 56) | ((u64)j << 48) | ((u64)r << 40);
        pairStore += roundTrip(i, j, j, r, seed | 1);  // i stores into j's memory, j checks
        pairLoad += roundTrip(j, j, i, r, seed | 2);   // j writes locally, i loads it from peer
        // copy engine: i's region r -> j's region (REGIONS-1-r)
        roundTrip(i, i, i, r, seed | 3);
        char* src = big[i] + regionOffset(i, r);
        char* dst = big[j] + regionOffset(j, REGIONS - 1 - r);
        CK(cudaSetDevice(i));
        CK(cudaMemcpyPeer(dst, j, src, i, CHUNK));
        CK(cudaSetDevice(j));
        CK(cudaMemset(badCounter[j], 0, sizeof(u64)));
        verify<<<1024, 256>>>((u64*)dst, CHUNK / sizeof(u64), seed | 3, badCounter[j]);
        u64 bad;
        CK(cudaMemcpy(&bad, badCounter[j], sizeof(u64), cudaMemcpyDeviceToHost));
        pairCopy += bad;
        blocks += 3;
      }
      if (pairStore || pairLoad || pairCopy)
        printf("MISMATCH %d->%d: store %llu load %llu copy %llu words\n", i, j, pairStore, pairLoad, pairCopy);
      badStore += pairStore;
      badLoad += pairLoad;
      badCopy += pairCopy;
    }
  printf("\nIntegrity: %llu blocks of %zu MiB over %d regions per GPU (up to %.1f GiB offset): "
         "bad words store=%llu load=%llu copy=%llu\n",
         blocks, CHUNK / MB, REGIONS, regionOffset(0, REGIONS - 1) / (double)GB, badStore, badLoad, badCopy);
  fflush(stdout);

  bandwidthMatrix("Unidirectional copy, peer access ENABLED", false);
  bandwidthMatrix("Bidirectional copy, peer access ENABLED", true);

  bool clean = badStore == 0 && badLoad == 0 && badCopy == 0;
  bool ok = clean && enableFailures == 0 && pairs == n * (n - 1);
  printf("\nRESULT: %s (%d of %d ordered pairs have peer access, %d failed to enable it, integrity %s)\n",
         ok ? "PASS" : "FAIL", pairs, n * (n - 1), enableFailures, clean ? "clean" : "MISMATCH");
  return ok ? 0 : 1;
}
