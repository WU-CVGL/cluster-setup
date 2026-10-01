// Peer access to COMPRESSIBLE device memory (cuMemCreate with CU_MEM_ALLOCATION_COMP_GENERIC) over BAR1 P2P.
// GPU B owns the allocation, GPU A maps it (cuMemMap + cuMemSetAccess), reads it and writes it.
//
// HOST-RAM HAZARD. Without an RM fix for compressible peer allocations, the peer PTE that RM builds for a
// compressible allocation can carry comptag bits inside the address field (Turing/Ampere/Ada PTE layout),
// so A's accesses can go to a wrong physical address, e.g. 0x0100_0000_0000 + x = 1 TiB + x: host RAM on a
// node whose RAM extends above 1 TiB. The program therefore refuses to run without --allow-host-ram-risk, and
// in addition without --above-1tib when System RAM ends above 1 TiB. The RAM end comes from the firmware memory
// map (/sys/firmware/memmap, world-readable; docker masks it, so run_host.sh reads it on the host and passes
// HOST_RAM_END); if neither is available, from MemTotal >= 960 GiB (a node with 1 TiB of DIMMs reports less
// than 1024 GiB, but the PCI and HyperTransport holes push part of its RAM above 1 TiB) or MemTotal unknown.
// Run it only on nodes whose RAM ends below 1 TiB; on nodes with more RAM only with the RM fix for compressible
// peer allocations installed (--above-1tib).
// Order per pair: B fills and verifies locally, A reads and verifies; only if A's read is clean does A
// write (a wrong read means the mapping points elsewhere, and a write there would corrupt that memory).
//
// usage: compress --allow-host-ram-risk [--above-1tib] [--pair a,b | --all] [--mib N]
//   --pair a,b  accessor A = a, owner B = b (default 0,1); --all: every ordered pair
//   --mib N     allocation size in MiB, rounded up to the granularity (default 64)
// The data mixes constant tiles (compressible) and random tiles (not compressible).
// Each read prints bad words and a checksum (sum of the words read vs expected).
// Build: nvcc -O2 -arch=sm_89 -o compress compress.cu -lcuda
// Per pair one of: MAPPED (A's peer mapping works: reads and writes verified), REFUSED (cuMemSetAccess for A
// returns CUDA_ERROR_NOT_SUPPORTED: the RM fix refuses a static-BAR1 peer mapping of compressible memory that the
// static BAR1 does not map with the allocation's kind), SKIP (no compression, VMM or peer access, or the
// allocation was not made compressible: the peer-mapping path was not reached), FAIL (wrong data, or any other
// cuMemSetAccess error).
// Output ends with "RESULT: PASS (m mapped, r refused by driver, s skipped)" (exit 0: at least one pair mapped
// or refused, none failed), "RESULT: SKIP ..." (exit 4: every pair skipped, nothing tested) or "RESULT: FAIL"
// (exit 1). CUDA errors exit 2, usage errors and refusals 3.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <utility>
#include <vector>
#include <cuda.h>
#include <cuda_runtime.h>

static const char* err(CUresult r) {
  const char* s = "?";
  cuGetErrorName(r, &s);
  return s;
}
#define CK(x)                                                                                   \
  do {                                                                                          \
    cudaError_t e_ = (x);                                                                       \
    if (e_ != cudaSuccess) {                                                                    \
      fprintf(stderr, "%s:%d %s: %s\n", __FILE__, __LINE__, #x, cudaGetErrorString(e_));        \
      exit(2);                                                                                  \
    }                                                                                           \
  } while (0)
#define CU(x)                                                                                   \
  do {                                                                                          \
    CUresult r_ = (x);                                                                          \
    if (r_ != CUDA_SUCCESS) {                                                                   \
      fprintf(stderr, "%s:%d %s: %s\n", __FILE__, __LINE__, #x, err(r_));                      \
      exit(2);                                                                                  \
    }                                                                                           \
  } while (0)

typedef unsigned long long u64;
static const size_t MB = 1ull << 20;

__device__ __host__ __forceinline__ u64 mix(u64 x) {
  x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33;
  return x;
}
// 2 KiB tiles: a constant for the whole buffer, a constant per tile, or random words.
__device__ __forceinline__ u64 pattern(u64 seed, size_t i) {
  size_t tile = i >> 8;
  switch (tile % 3) {
  case 0: return mix(seed);
  case 1: return mix(seed ^ (0x100000000ULL + tile));
  default: return mix(seed ^ (0x200000000000ULL + i));
  }
}

struct Stat {
  u64 bad, sum, expect;
};

__global__ void fill(u64* p, size_t n, u64 seed) {
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
    p[i] = pattern(seed, i);
}
__global__ void verify(const u64* p, size_t n, u64 seed, Stat* st) {
  u64 bad = 0, sum = 0, expect = 0;
  for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x) {
    u64 v = p[i], e = pattern(seed, i);
    bad += v != e;
    sum += v;
    expect += e;
  }
  if (bad) atomicAdd(&st->bad, bad);
  atomicAdd(&st->sum, sum);
  atomicAdd(&st->expect, expect);
}

static std::vector<Stat*> stat;

static void fillOn(int dev, CUdeviceptr p, size_t n, u64 seed) {
  CK(cudaSetDevice(dev));
  fill<<<1024, 256>>>((u64*)p, n, seed);
  CK(cudaGetLastError());
  CK(cudaDeviceSynchronize());
}
static Stat verifyOn(int dev, CUdeviceptr p, size_t n, u64 seed) {
  Stat s;
  CK(cudaSetDevice(dev));
  CK(cudaMemset(stat[dev], 0, sizeof(Stat)));
  verify<<<1024, 256>>>((const u64*)p, n, seed, stat[dev]);
  CK(cudaGetLastError());
  CK(cudaMemcpy(&s, stat[dev], sizeof(Stat), cudaMemcpyDeviceToHost));
  return s;
}
static bool report(const char* what, const Stat& s) {
  bool good = s.bad == 0 && s.sum == s.expect;
  printf("   %-24s bad words %llu checksum %016llx expected %016llx  %s\n", what, s.bad, s.sum, s.expect,
         good ? "ok" : "MISMATCH");
  fflush(stdout);
  return good;
}

enum Outcome { MAPPED, REFUSED, SKIP, FAIL };

// A = accessor, B = owner.
static Outcome testPair(int a, int b, size_t want) {
  printf("== A=GPU%d maps compressible memory of B=GPU%d\n", a, b);
  CUdevice devA, devB;
  CU(cuDeviceGet(&devA, a));
  CU(cuDeviceGet(&devB, b));
  int comp = 0, vmm = 0, peer = 0;
  CU(cuDeviceGetAttribute(&comp, CU_DEVICE_ATTRIBUTE_GENERIC_COMPRESSION_SUPPORTED, devB));
  CU(cuDeviceGetAttribute(&vmm, CU_DEVICE_ATTRIBUTE_VIRTUAL_MEMORY_MANAGEMENT_SUPPORTED, devB));
  CU(cuDeviceCanAccessPeer(&peer, devA, devB));
  printf("   GENERIC_COMPRESSION_SUPPORTED=%d VIRTUAL_MEMORY_MANAGEMENT_SUPPORTED=%d canAccessPeer=%d\n", comp, vmm,
         peer);
  if (!comp || !vmm || !peer) {
    printf("   SKIP\n");
    return SKIP;
  }

  CUmemAllocationProp prop = {};
  prop.type = CU_MEM_ALLOCATION_TYPE_PINNED;
  prop.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
  prop.location.id = b;
  prop.allocFlags.compressionType = CU_MEM_ALLOCATION_COMP_GENERIC;  // before the granularity query
  size_t gran = 0;
  CU(cuMemGetAllocationGranularity(&gran, &prop, CU_MEM_ALLOC_GRANULARITY_RECOMMENDED));
  size_t bytes = (want + gran - 1) / gran * gran, n = bytes / sizeof(u64);

  CK(cudaSetDevice(b));
  CUmemGenericAllocationHandle h;
  CU(cuMemCreate(&h, bytes, &prop, 0));
  CUmemAllocationProp got = {};
  CU(cuMemGetAllocationPropertiesFromHandle(&got, h));
  printf("   %zu MiB, granularity %zu KiB, compressionType granted %d\n", bytes / MB, gran >> 10,
         (int)got.allocFlags.compressionType);
  if (got.allocFlags.compressionType != CU_MEM_ALLOCATION_COMP_GENERIC) {
    printf("   SKIP (allocation is not compressible)\n");
    CU(cuMemRelease(h));
    return SKIP;
  }
  CUdeviceptr va;
  CU(cuMemAddressReserve(&va, bytes, gran, 0, 0));
  CU(cuMemMap(va, bytes, 0, h, 0));
  CUmemAccessDesc own = {};
  own.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
  own.location.id = b;
  own.flags = CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
  CU(cuMemSetAccess(va, bytes, &own, 1));

  u64 seed = ((u64)a << 40) | ((u64)b << 32) | 0xc0;
  fillOn(b, va, n, seed);
  bool ok = report("B writes, B reads", verifyOn(b, va, n, seed));
  Outcome out = ok ? MAPPED : FAIL;

  if (ok) {
    CUmemAccessDesc acc = own;
    acc.location.id = a;
    CUresult r = cuMemSetAccess(va, bytes, &acc, 1);
    if (r == CUDA_ERROR_NOT_SUPPORTED) {  // a driver with the RM fix may refuse compressible peer mappings
      printf("   cuMemSetAccess for A: %s\n   REFUSED by the driver (peer access to compressible memory)\n", err(r));
      out = REFUSED;
    } else if (r != CUDA_SUCCESS) {
      printf("   cuMemSetAccess for A: %s\n", err(r));
      out = FAIL;
    } else if (!report("B writes, A reads", verifyOn(a, va, n, seed))) {
      printf("   A's read is wrong: not writing from A (the mapping may point at other memory)\n");
      out = FAIL;
    } else {
      fillOn(a, va, n, seed ^ 0x77);
      ok = report("A writes, B reads", verifyOn(b, va, n, seed ^ 0x77));
      ok = report("A writes, A reads", verifyOn(a, va, n, seed ^ 0x77)) && ok;
      out = ok ? MAPPED : FAIL;
      if (ok) printf("   MAPPED\n");
    }
  }
  CK(cudaSetDevice(b));
  CK(cudaDeviceSynchronize());
  CU(cuMemUnmap(va, bytes));
  CU(cuMemAddressFree(va, bytes));
  CU(cuMemRelease(h));
  return out;
}

// End (exclusive) of System RAM from the firmware memory map, or 0 when it cannot be read.
static u64 ramEndFromMemmap() {
  u64 top = 0;
  for (int k = 0;; k++) {
    char path[96], type[64] = "";
    unsigned long long end = 0;
    snprintf(path, sizeof(path), "/sys/firmware/memmap/%d/type", k);
    FILE* f = fopen(path, "r");
    if (!f) break;
    bool gotType = fgets(type, sizeof(type), f) != nullptr;
    fclose(f);
    snprintf(path, sizeof(path), "/sys/firmware/memmap/%d/end", k);
    f = fopen(path, "r");
    if (!f) break;
    bool gotEnd = fscanf(f, "%llx", &end) == 1;
    fclose(f);
    if (gotType && gotEnd && !strncmp(type, "System RAM", 10) && end + 1 > top) top = end + 1;
  }
  return top;
}

static double memTotalGiB() {
  FILE* f = fopen("/proc/meminfo", "r");
  if (!f) return -1;
  char line[256];
  double kb = -1;
  while (fgets(line, sizeof(line), f))
    if (sscanf(line, "MemTotal: %lf kB", &kb) == 1) break;
  fclose(f);
  return kb < 0 ? -1 : kb / (1024.0 * 1024.0);
}

static void usage() {
  fprintf(stderr, "usage: compress --allow-host-ram-risk [--above-1tib] [--pair a,b | --all] [--mib N]\n");
  exit(3);
}

int main(int argc, char** argv) {
  bool allow = false, above = false, all = false;
  int pa = 0, pb = 1;
  long mib = 64;
  for (int k = 1; k < argc; k++) {
    if (!strcmp(argv[k], "--allow-host-ram-risk")) {
      allow = true;
    } else if (!strcmp(argv[k], "--above-1tib")) {
      above = true;
    } else if (!strcmp(argv[k], "--all")) {
      all = true;
    } else if (!strcmp(argv[k], "--pair") && k + 1 < argc) {
      if (sscanf(argv[++k], "%d,%d", &pa, &pb) != 2) usage();
    } else if (!strcmp(argv[k], "--mib") && k + 1 < argc) {
      mib = atol(argv[++k]);
      if (mib < 1) usage();
    } else {
      usage();
    }
  }
  if (!allow) {
    printf("REFUSED: without the RM fix this test can write to host RAM at 1 TiB + offset. Run it only on nodes\n"
           "whose RAM ends below 1 TiB (or with the RM fix and --above-1tib); then pass --allow-host-ram-risk.\n");
    return 3;
  }
  // BAR1P2P: the rule is where System RAM ends, not how much there is.
  const u64 TIB = 1ull << 40;
  double ram = memTotalGiB();
  u64 ramEnd = ramEndFromMemmap();
  const char* src = "/sys/firmware/memmap";
  const char* env = getenv("HOST_RAM_END");
  if (!ramEnd && env && *env) {
    ramEnd = strtoull(env, nullptr, 0);
    src = "HOST_RAM_END (host /sys/firmware/memmap, from run_host.sh)";
  }
  bool risk;
  if (ramEnd) {
    printf("System RAM ends at 0x%llx (%s), MemTotal %.0f GiB\n", ramEnd, src, ram);
    risk = ramEnd > TIB;
  } else {
    printf("RAM end unknown (/sys/firmware/memmap unreadable, HOST_RAM_END unset): MemTotal %.0f GiB decides\n", ram);
    risk = ram < 0 || ram >= 960;
  }
  if (risk && !above) {
    if (ramEnd)
      printf("REFUSED: host RAM extends above 1 TiB (ends at 0x%llx): RAM may lie at 1 TiB + offset;\n", ramEnd);
    else
      printf("REFUSED: cannot determine where host RAM ends and MemTotal is %s: RAM may lie at 1 TiB + offset;\n",
             ram < 0 ? "unknown" : "960 GiB or more");
    printf("--above-1tib overrides (only with a driver that has the RM fix for compressible peer allocations)\n");
    return 3;
  }

  int n;
  CK(cudaGetDeviceCount(&n));
  if (n < 2) {
    printf("RESULT: FAIL (%d GPU, need at least 2)\n", n);
    return 1;
  }
  stat.resize(n);
  for (int d = 0; d < n; d++) {  // creates the primary contexts the driver API calls run in
    CK(cudaSetDevice(d));
    CK(cudaFree(0));
    CK(cudaMalloc(&stat[d], sizeof(Stat)));
  }
  std::vector<std::pair<int, int>> pairs;
  if (all) {
    for (int a = 0; a < n; a++)
      for (int b = 0; b < n; b++)
        if (a != b) pairs.push_back(std::make_pair(a, b));
  } else {
    if (pa == pb || pa < 0 || pb < 0 || pa >= n || pb >= n) usage();
    pairs.push_back(std::make_pair(pa, pb));
  }
  int nmap = 0, nref = 0, nskip = 0, nfail = 0;
  for (size_t k = 0; k < pairs.size(); k++) {
    Outcome o = testPair(pairs[k].first, pairs[k].second, (size_t)mib * MB);
    nmap += o == MAPPED;
    nref += o == REFUSED;
    nskip += o == SKIP;
    nfail += o == FAIL;
  }
  if (nfail) {
    printf("RESULT: FAIL (%d of %zu pairs failed; %d mapped, %d refused by driver, %d skipped)\n", nfail,
           pairs.size(), nmap, nref, nskip);
    return 1;
  }
  if (!nmap && !nref) {
    printf("RESULT: SKIP (%d pairs skipped: no pair reached a compressible peer mapping)\n", nskip);
    return 4;
  }
  printf("RESULT: PASS (%d mapped, %d refused by driver, %d skipped)\n", nmap, nref, nskip);
  return 0;
}
