// Managed memory (cudaMallocManaged) across GPU pairs. CRASH-RISKY on drivers without the UVM BAR1 fix:
// on static-BAR1 GPUs (24 GB RTX 4090) UVM migrates or maps managed pages peer-to-peer with an aperture
// the pre-Hopper HALs cannot encode (Xid 31, then Xid 154 on every GPU; reboot). Run it only through the
// opt-in stage of run_tests.sh (MANAGED=1), and first with --quick --pair a,b (in the opt-in window of
// uvm_bar1_p2p_managed=1: first --quick --pair a,b --modes accessedby, see below).
// With the fixed driver and uvm_bar1_p2p_managed=0 (default), managed pages of BAR1 peers stage through
// host memory; with =1 UVM uses direct BAR1 peer mappings and copies (experimental).
//
// For each ordered pair (i, j) - GPU i owns / writes, GPU j is the peer - the modes run in this order
// (simplest UVM operations first; each mode prints its result line before the next starts). No crash-risk
// order is claimed for the uvm_bar1_p2p_managed=1 window: there fault, prefetch, memcpy, atomic and oversub
// can make UVM peer copy-engine copies (an encoding error there is a global fatal error, Xid 154 on every
// GPU, reboot), while accessedby on a fresh buffer only uses peer remote mappings (page-table entries). All
// modes share one managed buffer, so a mode starts from the residency the previous mode left (accessedby
// after memcpy begins with a peer migration): run one mode alone with --modes to exercise only its path,
// and in that window run --quick --pair a,b --modes accessedby first.
//   fault       filled on i, read on j through page faults, sampled on the host
//   prefetch    filled on i, cudaMemPrefetchAsync to j, read on j
//   memcpy      cudaMemcpy between a managed buffer resident on i and one resident on j, both directions
//   accessedby  SetPreferredLocation(i) + SetAccessedBy(j): j reads, j writes, verified on i and the host
//   atomic      counters resident on i (preferred location i, accessed by j); only j does atomicAdd
//   oversub     first pair only: 1.25x GPU i's free memory, halves on i and j, read on i then j, rewritten
//               on i, read on j (eviction and migration back and forth)
// usage: managedtest [--pair a,b] [--quick] [--mib N] [--modes m1,m2,...]
//   --pair a,b  only the ordered pairs (a,b) and (b,a)      (default: every ordered pair)
//   --quick     2 MiB per transfer and no oversub unless named in --modes
//   --mib N     MiB per transfer (default 256)
//   --modes     subset of the modes above, run in the order above
// Every read prints the number of bad words and a checksum (sum of the words read vs expected).
// Build: nvcc -O2 -arch=sm_89 -o managedtest managedtest.cu
// Output ends with "RESULT: PASS" (exit 0) or "RESULT: FAIL" (exit 1). CUDA errors exit 2, usage errors 3.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
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
#define LAUNCH(...)            \
  do {                         \
    __VA_ARGS__;               \
    CK(cudaGetLastError());    \
  } while (0)

typedef unsigned long long u64;
static const size_t MB = 1ull << 20;
static const char* MODES[] = {"fault", "prefetch", "memcpy", "accessedby", "atomic", "oversub"};
static const int NMODES = 6;
static const u64 ATOMIC_ROUNDS = 4;

// CUDA 13 replaced the device-ordinal forms of these calls with cudaMemLocation.
static cudaError_t prefetchTo(const void* p, size_t bytes, int dev) {
#if CUDART_VERSION >= 13000
  cudaMemLocation loc = {};
  loc.type = cudaMemLocationTypeDevice;
  loc.id = dev;
  return cudaMemPrefetchAsync(p, bytes, loc, 0, 0);
#else
  return cudaMemPrefetchAsync(p, bytes, dev, 0);
#endif
}
static cudaError_t adviseDevice(const void* p, size_t bytes, cudaMemoryAdvise advice, int dev) {
#if CUDART_VERSION >= 13000
  cudaMemLocation loc = {};
  loc.type = cudaMemLocationTypeDevice;
  loc.id = dev;
  return cudaMemAdvise(p, bytes, advice, loc);
#else
  return cudaMemAdvise(p, bytes, advice, dev);
#endif
}

__device__ __host__ __forceinline__ u64 mix(u64 x) {
  x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33;
  return x;
}

struct Stat {
  u64 bad, sum, expect;
};

// Word k of the buffer holds mix(seed ^ (base + k)); base lets a buffer be filled in parts.
__global__ void fill(u64* p, size_t n, u64 seed, size_t base) {
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
    p[i] = mix(seed ^ (base + i));
}
__global__ void setAll(u64* p, size_t n, u64 v) {
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
    p[i] = v;
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
// Every counter gets `rounds` increments.
__global__ void addCounters(u64* c, size_t n, u64 rounds) {
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n * rounds; i += (size_t)gridDim.x * blockDim.x)
    atomicAdd(&c[i % n], 1ULL);
}
__global__ void verifyCounters(const u64* c, size_t n, u64 want, Stat* st) {
  u64 bad = 0, sum = 0;
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x) {
    bad += c[i] != want;
    sum += c[i];
  }
  if (bad) atomicAdd(&st->bad, bad);
  atomicAdd(&st->sum, sum);
}

static int ngpu;
static std::vector<Stat*> stat;  // one device-memory Stat per GPU (not managed: keeps it out of the test)
static bool concurrent;

static void add(Stat& a, const Stat& b) {
  a.bad += b.bad;
  a.sum += b.sum;
  a.expect += b.expect;
}
static void syncDev(int dev) {
  CK(cudaSetDevice(dev));
  CK(cudaDeviceSynchronize());
}
static void fillOn(int dev, u64* p, size_t n, u64 seed, size_t base = 0) {
  CK(cudaSetDevice(dev));
  LAUNCH(fill<<<1024, 256>>>(p, n, seed, base));
  CK(cudaDeviceSynchronize());
}
static Stat verifyOn(int dev, const u64* p, size_t n, u64 seed) {
  Stat s;
  CK(cudaSetDevice(dev));
  CK(cudaMemset(stat[dev], 0, sizeof(Stat)));
  LAUNCH(verify<<<1024, 256>>>(p, n, seed, stat[dev]));
  CK(cudaMemcpy(&s, stat[dev], sizeof(Stat), cudaMemcpyDeviceToHost));
  return s;
}
// Samples every 4099th word on the host (at most ~64 Ki samples; all GPUs idle).
static Stat hostCheck(const u64* p, size_t n, u64 seed) {
  Stat s = {0, 0, 0};
  const size_t step = n / 65536 > 4099 ? n / 65536 + 1 : 4099;
  for (int d = 0; d < ngpu; d++) syncDev(d);
  for (size_t k = 0; k < n; k += step) {
    u64 e = mix(seed ^ k);
    s.bad += p[k] != e;
    s.sum += p[k];
    s.expect += e;
  }
  return s;
}

static Stat modeFault(int i, int j, u64* p, size_t n, u64 seed) {
  fillOn(i, p, n, seed);
  Stat s = verifyOn(j, p, n, seed);
  add(s, hostCheck(p, n, seed));
  return s;
}
static Stat modePrefetch(int i, int j, u64* p, size_t n, u64 seed) {
  fillOn(i, p, n, seed);
  CK(cudaSetDevice(i));
  CK(prefetchTo(p, n * sizeof(u64), j));
  CK(cudaDeviceSynchronize());
  return verifyOn(j, p, n, seed);
}
static Stat modeMemcpy(int i, int j, u64* p, u64* q, size_t n, u64 seed) {
  const size_t bytes = n * sizeof(u64);
  fillOn(i, p, n, seed);             // p resident on i
  fillOn(j, q, n, ~seed);            // q resident on j, other data
  CK(cudaSetDevice(i));
  CK(cudaMemcpy(q, p, bytes, cudaMemcpyDefault));
  Stat s = verifyOn(j, q, n, seed);
  fillOn(j, q, n, seed ^ 0x5a5a);    // and back: j -> i
  fillOn(i, p, n, ~seed);
  CK(cudaSetDevice(j));
  CK(cudaMemcpy(p, q, bytes, cudaMemcpyDefault));
  add(s, verifyOn(i, p, n, seed ^ 0x5a5a));
  return s;
}
static Stat modeAccessedBy(int i, int j, u64* p, size_t n, u64 seed) {
  const size_t bytes = n * sizeof(u64);
  CK(adviseDevice(p, bytes, cudaMemAdviseSetPreferredLocation, i));
  CK(adviseDevice(p, bytes, cudaMemAdviseSetAccessedBy, j));
  fillOn(i, p, n, seed);
  Stat s = verifyOn(j, p, n, seed);  // j reads
  fillOn(j, p, n, seed ^ 0xa5a5);    // j writes
  add(s, verifyOn(i, p, n, seed ^ 0xa5a5));
  add(s, hostCheck(p, n, seed ^ 0xa5a5));
  CK(adviseDevice(p, bytes, cudaMemAdviseUnsetAccessedBy, j));
  CK(adviseDevice(p, bytes, cudaMemAdviseUnsetPreferredLocation, i));
  return s;
}
static Stat modeAtomic(int i, int j, u64* p, size_t n) {
  size_t nc = n / 16 < 1024 ? (n < 1024 ? n : 1024) : n / 16;
  const size_t bytes = nc * sizeof(u64);
  CK(adviseDevice(p, bytes, cudaMemAdviseSetPreferredLocation, i));
  CK(adviseDevice(p, bytes, cudaMemAdviseSetAccessedBy, j));
  CK(cudaSetDevice(i));
  LAUNCH(setAll<<<1024, 256>>>(p, nc, 0));
  CK(prefetchTo(p, bytes, i));
  CK(cudaDeviceSynchronize());
  CK(cudaSetDevice(j));
  LAUNCH(addCounters<<<1024, 256>>>(p, nc, ATOMIC_ROUNDS));
  CK(cudaDeviceSynchronize());
  Stat s;
  CK(cudaSetDevice(i));
  CK(cudaMemset(stat[i], 0, sizeof(Stat)));
  LAUNCH(verifyCounters<<<1024, 256>>>(p, nc, ATOMIC_ROUNDS, stat[i]));
  CK(cudaMemcpy(&s, stat[i], sizeof(Stat), cudaMemcpyDeviceToHost));
  s.expect = nc * ATOMIC_ROUNDS;
  CK(adviseDevice(p, bytes, cudaMemAdviseUnsetAccessedBy, j));
  CK(adviseDevice(p, bytes, cudaMemAdviseUnsetPreferredLocation, i));
  return s;
}
static Stat modeOversub(int i, int j, u64 seed, bool* skipped) {
  Stat s = {0, 0, 0};
  *skipped = !concurrent;
  if (!concurrent) return s;
  size_t freeB, totalB;
  CK(cudaSetDevice(i));
  CK(cudaMemGetInfo(&freeB, &totalB));
  const size_t align = 2 * MB;
  size_t bytes = (freeB + freeB / 4) / align * align, half = bytes / 2 / align * align;
  size_t n = bytes / sizeof(u64), nh = half / sizeof(u64);
  printf(" (%.1f GiB managed, GPU%d has %.1f GiB free)", bytes / 1073741824.0, i, freeB / 1073741824.0);
  fflush(stdout);
  u64* o;
  CK(cudaMallocManaged(&o, bytes));
  CK(prefetchTo(o, half, i));
  CK(cudaDeviceSynchronize());
  CK(cudaSetDevice(j));
  CK(prefetchTo((char*)o + half, bytes - half, j));
  CK(cudaDeviceSynchronize());
  fillOn(i, o, nh, seed, 0);
  fillOn(j, o + nh, n - nh, seed, nh);
  add(s, verifyOn(i, o, n, seed));   // pulls the second half to i, evicting
  add(s, verifyOn(j, o, n, seed));   // and back
  fillOn(i, o, n, seed ^ 0x3c3c);    // rewritten on i
  add(s, verifyOn(j, o, n, seed ^ 0x3c3c));
  add(s, hostCheck(o, n, seed ^ 0x3c3c));
  CK(cudaFree(o));
  return s;
}

static void usage() {
  fprintf(stderr, "usage: managedtest [--pair a,b] [--quick] [--mib N] [--modes fault,prefetch,memcpy,"
                  "accessedby,atomic,oversub]\n");
  exit(3);
}

int main(int argc, char** argv) {
  int pa = -1, pb = -1;
  bool quick = false;
  long mib = -1;
  std::string modeArg;
  for (int k = 1; k < argc; k++) {
    if (!strcmp(argv[k], "--pair") && k + 1 < argc) {
      if (sscanf(argv[++k], "%d,%d", &pa, &pb) != 2) usage();
    } else if (!strcmp(argv[k], "--quick")) {
      quick = true;
    } else if (!strcmp(argv[k], "--mib") && k + 1 < argc) {
      mib = atol(argv[++k]);
      if (mib < 1) usage();
    } else if (!strcmp(argv[k], "--modes") && k + 1 < argc) {
      modeArg = "," + std::string(argv[++k]) + ",";
    } else {
      usage();
    }
  }
  bool run[NMODES];
  for (int m = 0; m < NMODES; m++) {
    if (modeArg.empty())
      run[m] = !(quick && m == NMODES - 1);  // --quick leaves out oversub
    else
      run[m] = modeArg.find("," + std::string(MODES[m]) + ",") != std::string::npos;
  }
  if (!modeArg.empty()) {  // reject unknown mode names
    std::string rest = modeArg;
    for (int m = 0; m < NMODES; m++) {
      std::string tok = "," + std::string(MODES[m]) + ",";
      for (size_t at; (at = rest.find(tok)) != std::string::npos;) rest.replace(at, tok.size(), ",");
    }
    if (rest.find_first_not_of(',') != std::string::npos) usage();
  }

  CK(cudaGetDeviceCount(&ngpu));
  if (ngpu < 2) {
    printf("RESULT: FAIL (%d GPU, need at least 2)\n", ngpu);
    return 1;
  }
  std::vector<std::pair<int, int>> pairs;
  if (pa >= 0) {
    if (pa == pb || pa >= ngpu || pb < 0 || pb >= ngpu) usage();
    pairs.push_back(std::make_pair(pa, pb));
    pairs.push_back(std::make_pair(pb, pa));
  } else {
    for (int i = 0; i < ngpu; i++)
      for (int j = 0; j < ngpu; j++)
        if (i != j) pairs.push_back(std::make_pair(i, j));
  }
  const size_t bytes = (quick && mib < 0 ? 2 : (mib < 0 ? 256 : mib)) * MB, words = bytes / sizeof(u64);

  stat.resize(ngpu);
  for (int d = 0; d < ngpu; d++) {
    CK(cudaSetDevice(d));
    CK(cudaMalloc(&stat[d], sizeof(Stat)));
  }
  int cma = 0;
  CK(cudaDeviceGetAttribute(&cma, cudaDevAttrConcurrentManagedAccess, 0));
  concurrent = cma != 0;
  u64 *p, *q;
  CK(cudaMallocManaged(&p, bytes));
  CK(cudaMallocManaged(&q, bytes));
  printf("managed memory, %d GPUs, %zu ordered pairs, %zu MiB per transfer, concurrentManagedAccess=%d\n", ngpu,
         pairs.size(), bytes / MB, cma);
  fflush(stdout);

  bool ok = true;
  for (int m = 0; m < NMODES; m++) {
    if (!run[m]) continue;
    printf("== mode %s, pairs:", MODES[m]);  // the last pair printed is the one running
    fflush(stdout);
    Stat total = {0, 0, 0};
    int done = 0;
    bool skipped = false;
    for (size_t k = 0; k < pairs.size(); k++) {
      int i = pairs[k].first, j = pairs[k].second;
      u64 seed = ((u64)m << 48) | ((u64)i << 40) | ((u64)j << 32);
      printf(" %d->%d", i, j);
      fflush(stdout);
      Stat s = {0, 0, 0};
      switch (m) {
      case 0: s = modeFault(i, j, p, words, seed); break;
      case 1: s = modePrefetch(i, j, p, words, seed); break;
      case 2: s = modeMemcpy(i, j, p, q, words, seed); break;
      case 3: s = modeAccessedBy(i, j, p, words, seed); break;
      case 4: s = modeAtomic(i, j, p, words); break;
      case 5: s = modeOversub(i, j, seed, &skipped); break;
      }
      if (s.bad || s.sum != s.expect) {
        printf("\n   MISMATCH %s %d->%d: bad words %llu checksum %016llx expected %016llx\n", MODES[m], i, j, s.bad,
               s.sum, s.expect);
        fflush(stdout);
      }
      add(total, s);
      done++;
      if (m == 5) break;  // oversub: first pair only
    }
    printf("\n");
    bool good = total.bad == 0 && total.sum == total.expect;
    ok = ok && good;
    if (skipped)
      printf("mode %-10s SKIP (concurrentManagedAccess=0)\n", MODES[m]);
    else
      printf("mode %-10s pairs %d: bad words %llu checksum %016llx expected %016llx  %s\n", MODES[m], done, total.bad,
             total.sum, total.expect, good ? "PASS" : "FAIL");
    fflush(stdout);
  }
  CK(cudaFree(p));
  CK(cudaFree(q));
  printf("RESULT: %s\n", ok ? "PASS" : "FAIL");
  return ok ? 0 : 1;
}
