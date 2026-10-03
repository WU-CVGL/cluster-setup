// Pinned host memory through cuMemCreate (CU_MEM_LOCATION_TYPE_HOST_NUMA), as NCCL >= 2.23 allocates it
// for its SHM transport. With the open driver, UVM HMM on and iommu=pt, these allocations come from
// ZONE_DMA32 (~2 GiB on NUMA node 0 only) and fail; see install-p2p-modules.sh (uvm_disable_hmm=1).
//
// usage: hostnuma [sizes]               from every GPU's context, on every NUMA node with memory:
//                                       2 MiB, 64 MiB and 1 GiB, with and without a POSIX fd handle
//        hostnuma cumulative [GiB]      from GPU 0: keep allocating 1 GiB HOST_NUMA blocks on each node
//                                       until GiB are held (default 8); then the same without a NUMA
//                                       node (CU_MEM_LOCATION_TYPE_HOST) and cuMemHostAlloc (information)
// NUMA nodes: /sys/devices/system/node/has_memory (override: HOSTNUMA_NODES="0,1").
// Build: nvcc -O2 -o hostnuma hostnuma.cu -lcuda
// Output ends with "RESULT: PASS" (exit 0) or "RESULT: FAIL ..." (exit 1). Driver API errors exit 2.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <cuda.h>

static const char* err(CUresult r) {
  const char* s = "?";
  cuGetErrorName(r, &s);
  return s;
}

#define CK(x)                                                                \
  do {                                                                       \
    CUresult r_ = (x);                                                       \
    if (r_ != CUDA_SUCCESS) {                                                \
      fprintf(stderr, "%s:%d %s: %s\n", __FILE__, __LINE__, #x, err(r_));    \
      exit(2);                                                               \
    }                                                                        \
  } while (0)

// Parses a kernel CPU/node list such as "0-1,3".
static std::vector<int> parseList(const std::string& s) {
  std::vector<int> out;
  std::string list = s;
  for (char* tok = strtok(&list[0], ",\n"); tok; tok = strtok(nullptr, ",\n")) {
    int a, b;
    if (sscanf(tok, "%d-%d", &a, &b) == 2) {
      for (int i = a; i <= b; i++) out.push_back(i);
    } else if (sscanf(tok, "%d", &a) == 1) {
      out.push_back(a);
    }
  }
  return out;
}

static std::vector<int> numaNodes() {
  if (getenv("HOSTNUMA_NODES") && *getenv("HOSTNUMA_NODES")) return parseList(getenv("HOSTNUMA_NODES"));
  const char* files[] = {"/sys/devices/system/node/has_memory", "/sys/devices/system/node/online"};
  for (const char* f : files) {
    FILE* fp = fopen(f, "r");
    if (!fp) continue;
    char buf[256] = {0};
    size_t len = fread(buf, 1, sizeof(buf) - 1, fp);
    fclose(fp);
    buf[len] = 0;
    std::vector<int> v = parseList(buf);
    if (!v.empty()) return v;
  }
  return {0};
}

static CUmemAllocationProp hostProp(CUmemLocationType type, int id, bool fd) {
  CUmemAllocationProp prop = {};
  prop.type = CU_MEM_ALLOCATION_TYPE_PINNED;
  prop.location.type = type;
  prop.location.id = id;
  prop.requestedHandleTypes = fd ? CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR : CU_MEM_HANDLE_TYPE_NONE;
  return prop;
}

static CUcontext useDevice(int d, CUdevice* dev) {
  CUcontext ctx;
  CK(cuDeviceGet(dev, d));
  CK(cuDevicePrimaryCtxRetain(&ctx, *dev));
  CK(cuCtxSetCurrent(ctx));
  return ctx;
}

// Every GPU context, every NUMA node, several sizes, with and without an exportable fd handle.
static int sizesMode(const std::vector<int>& nodes) {
  int n;
  CK(cuDeviceGetCount(&n));
  const size_t sizes[] = {2ull << 20, 64ull << 20, 1ull << 30};
  int fails = 0, tries = 0;
  for (int d = 0; d < n; d++) {
    CUdevice dev;
    useDevice(d, &dev);
    int hostNumaId = -1;
    cuDeviceGetAttribute(&hostNumaId, CU_DEVICE_ATTRIBUTE_HOST_NUMA_ID, dev);
    printf("GPU%d (host NUMA id %d):", d, hostNumaId);
    int gpuFails = 0;
    for (int numa : nodes)
      for (size_t s : sizes)
        for (int fd = 0; fd < 2; fd++) {
          CUmemAllocationProp prop = hostProp(CU_MEM_LOCATION_TYPE_HOST_NUMA, numa, fd);
          size_t gran = 0;
          CK(cuMemGetAllocationGranularity(&gran, &prop, CU_MEM_ALLOC_GRANULARITY_MINIMUM));
          size_t size = (s + gran - 1) / gran * gran;
          CUmemGenericAllocationHandle h;
          CUresult r = cuMemCreate(&h, size, &prop, 0);
          tries++;
          if (r == CUDA_SUCCESS) {
            CK(cuMemRelease(h));
          } else {
            gpuFails++;
            printf(" [numa%d %zuMiB%s: %s]", numa, size >> 20, fd ? " fd" : "", err(r));
          }
        }
    printf("%s\n", gpuFails ? "" : " all OK");
    fails += gpuFails;
    CK(cuDevicePrimaryCtxRelease(dev));
  }
  if (fails)
    printf("RESULT: FAIL (%d of %d host allocations failed)\n", fails, tries);
  else
    printf("RESULT: PASS (%d host allocations)\n", tries);
  return fails ? 1 : 0;
}

// Holds up to `target` 1 GiB blocks; returns how many it got and the error that stopped it.
static size_t holdBlocks(CUmemLocationType type, int id, size_t target, CUresult* last) {
  std::vector<CUmemGenericAllocationHandle> hs;
  CUmemAllocationProp prop = hostProp(type, id, false);
  *last = CUDA_SUCCESS;
  while (hs.size() < target) {
    CUmemGenericAllocationHandle h;
    *last = cuMemCreate(&h, 1ull << 30, &prop, 0);
    if (*last != CUDA_SUCCESS) break;
    hs.push_back(h);
  }
  for (auto h : hs) CK(cuMemRelease(h));
  return hs.size();
}

// Cumulative 1 GiB blocks per NUMA node from GPU 0: stops near 1-2 GiB on node 0 and immediately on the
// other nodes when the allocations come from ZONE_DMA32.
static int cumulativeMode(const std::vector<int>& nodes, size_t target) {
  CUdevice dev;
  useDevice(0, &dev);
  int short_ = 0;
  for (int numa : nodes) {
    CUmemAllocationProp prop = hostProp(CU_MEM_LOCATION_TYPE_HOST_NUMA, numa, false);
    size_t gmin = 0, grec = 0;
    CK(cuMemGetAllocationGranularity(&gmin, &prop, CU_MEM_ALLOC_GRANULARITY_MINIMUM));
    CK(cuMemGetAllocationGranularity(&grec, &prop, CU_MEM_ALLOC_GRANULARITY_RECOMMENDED));
    CUresult r;
    size_t got = holdBlocks(CU_MEM_LOCATION_TYPE_HOST_NUMA, numa, target, &r);
    printf("HOST_NUMA %d (granularity min %zu KiB, rec %zu KiB): %zu of %zu x 1 GiB held%s%s\n", numa, gmin >> 10,
           grec >> 10, got, target, r == CUDA_SUCCESS ? "" : ", then ", r == CUDA_SUCCESS ? "" : err(r));
    if (got < target) short_++;
  }
  CUresult r;
  size_t got = holdBlocks(CU_MEM_LOCATION_TYPE_HOST, 0, target, &r);
  printf("HOST (no NUMA node): %zu of %zu x 1 GiB held%s%s\n", got, target, r == CUDA_SUCCESS ? "" : ", then ",
         r == CUDA_SUCCESS ? "" : err(r));
  void* p = nullptr;
  CUresult rh = cuMemHostAlloc(&p, target << 30, 0);
  printf("cuMemHostAlloc %zu GiB (legacy, no NUMA node): %s\n", target, err(rh));
  if (p) CK(cuMemFreeHost(p));
  CK(cuDevicePrimaryCtxRelease(dev));
  if (short_)
    printf("RESULT: FAIL (%d NUMA nodes could not hold %zu GiB of cuMemCreate host memory)\n", short_, target);
  else
    printf("RESULT: PASS (%zu GiB held on each of %zu NUMA nodes)\n", target, nodes.size());
  return short_ ? 1 : 0;
}

int main(int argc, char** argv) {
  const char* mode = argc > 1 ? argv[1] : "sizes";
  CK(cuInit(0));
  std::vector<int> nodes = numaNodes();
  printf("NUMA nodes with memory:");
  for (int x : nodes) printf(" %d", x);
  printf("\n");
  if (!strcmp(mode, "sizes")) return sizesMode(nodes);
  if (!strcmp(mode, "cumulative")) {
    long gib = argc > 2 ? atol(argv[2]) : 8;
    if (gib < 1) gib = 1;
    return cumulativeMode(nodes, (size_t)gib);
  }
  fprintf(stderr, "usage: %s [sizes | cumulative [GiB]]\n", argv[0]);
  return 2;
}
