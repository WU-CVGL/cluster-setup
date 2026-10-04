// Atomics on peer memory over PCIe BAR1 P2P. The RTX 4090 has no native P2P atomics
// (cudaDevP2PAttrNativeAtomicSupported = 0): a peer atomicAdd is not atomic with respect to atomics of other
// GPUs on the same address, so increments get lost. The test measures this; with the attribute 0 a loss is
// reported as INFO and does not fail the test.
// An MMU fault (Xid 31, fault type ATOMIC) while this runs means peer atomics are disabled in the page
// tables: a finding, not a regression of the P2P patch. Opt-in stage (ATOMICS=1 in run_tests.sh); run it
// outside service validation, since any Xid fails the run_host.sh kernel-log check.
//
// Prints the P2P attribute matrix (access / native atomics, row = accessing GPU), then on one counter in
// GPU B's memory: (1) B alone (control, must be exact), (2) each peer alone, (3) B and all peers at once,
// released together by a host flag.
// usage: atomics [--owner b] [--peers a[,c...]] [--adds N]
//   --owner b   GPU that holds the counter (default 0)
//   --peers     accessing GPUs (default: the next two GPUs after the owner, one if only two GPUs)
//   --adds N    atomicAdd per thread per launch (default 64; 64 blocks x 256 threads per GPU)
// Build: nvcc -O2 -arch=sm_<cc> -o atomics atomics.cu   (sm_86 on the RTX 3090, sm_89 on the RTX 4090)
// Output ends with "RESULT: PASS ..." (exit 0) or "RESULT: FAIL ..." (exit 1): the control is wrong, a count
// is too high, a count is too low although the attribute says native atomics, or a start timed out.
// CUDA errors exit 2, usage errors 3.
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
static const int BLOCKS = 64, THREADS = 256;
static const u64 START_TIMEOUT_NS = 10000000000ULL;

__device__ __forceinline__ u64 globalTimer() {
  u64 t;
  asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(t));
  return t;
}

// Waits for *go (pinned host memory) unless go is null, then adds 1 to *counter `adds` times per thread.
__global__ void addKernel(u64* counter, u64 adds, const volatile int* go, int* timedOut) {
  __shared__ int run;
  if (threadIdx.x == 0) {
    run = 1;
    if (go) {
      u64 t0 = globalTimer();
      while (!*go) {
        if (globalTimer() - t0 > START_TIMEOUT_NS) {
          run = 0;
          atomicExch(timedOut, 1);
          break;
        }
        __nanosleep(1000);
      }
    }
  }
  __syncthreads();
  if (!run) return;
  for (u64 k = 0; k < adds; k++) atomicAdd(counter, 1ULL);
}

static int n;
static std::vector<int*> timedOut;  // per GPU, device memory

// Runs addKernel on every GPU in `devs` against `counter` (on `owner`) and returns the final count.
static u64 run(int owner, u64* counter, const std::vector<int>& devs, u64 adds, volatile int* go, bool* timeout) {
  CK(cudaSetDevice(owner));
  CK(cudaMemset(counter, 0, sizeof(u64)));
  CK(cudaDeviceSynchronize());
  *go = 0;
  for (size_t k = 0; k < devs.size(); k++) {
    CK(cudaSetDevice(devs[k]));
    CK(cudaMemset(timedOut[devs[k]], 0, sizeof(int)));
    addKernel<<<BLOCKS, THREADS>>>(counter, adds, devs.size() > 1 ? (const volatile int*)go : nullptr,
                                   timedOut[devs[k]]);
    CK(cudaGetLastError());
  }
  __sync_synchronize();
  *go = 1;  // releases all launches together
  *timeout = false;
  for (size_t k = 0; k < devs.size(); k++) {
    int t;
    CK(cudaSetDevice(devs[k]));
    CK(cudaDeviceSynchronize());
    CK(cudaMemcpy(&t, timedOut[devs[k]], sizeof(int), cudaMemcpyDeviceToHost));
    *timeout = *timeout || t;
  }
  u64 got;
  CK(cudaSetDevice(owner));
  CK(cudaMemcpy(&got, counter, sizeof(u64), cudaMemcpyDeviceToHost));
  return got;
}

static std::vector<int> parseList(const char* s) {
  std::vector<int> v;
  std::string list = s;
  for (char* tok = strtok(&list[0], ","); tok; tok = strtok(nullptr, ",")) v.push_back(atoi(tok));
  return v;
}
static void usage() {
  fprintf(stderr, "usage: atomics [--owner b] [--peers a[,c...]] [--adds N]\n");
  exit(3);
}

int main(int argc, char** argv) {
  int owner = 0;
  std::vector<int> peers;
  long adds = 64;
  for (int k = 1; k < argc; k++) {
    if (!strcmp(argv[k], "--owner") && k + 1 < argc) {
      owner = atoi(argv[++k]);
    } else if (!strcmp(argv[k], "--peers") && k + 1 < argc) {
      peers = parseList(argv[++k]);
    } else if (!strcmp(argv[k], "--adds") && k + 1 < argc) {
      adds = atol(argv[++k]);
      if (adds < 1) usage();
    } else {
      usage();
    }
  }
  CK(cudaGetDeviceCount(&n));
  if (n < 2) {
    printf("RESULT: FAIL (%d GPU, need at least 2)\n", n);
    return 1;
  }
  if (owner < 0 || owner >= n) usage();
  if (peers.empty())
    for (int k = 1; k <= 2 && k < n; k++) peers.push_back((owner + k) % n);
  for (size_t k = 0; k < peers.size(); k++)
    if (peers[k] < 0 || peers[k] >= n || peers[k] == owner) usage();

  // Attribute matrix.
  printf("P2P attributes AccessSupported/NativeAtomicSupported (row = accessing GPU, column = owner)\n     ");
  for (int j = 0; j < n; j++) printf("%5d", j);
  printf("\n");
  for (int i = 0; i < n; i++) {
    printf("%4d ", i);
    for (int j = 0; j < n; j++) {
      int acc = 0, nat = 0;
      if (i != j) {
        CK(cudaDeviceGetP2PAttribute(&acc, cudaDevP2PAttrAccessSupported, i, j));
        CK(cudaDeviceGetP2PAttribute(&nat, cudaDevP2PAttrNativeAtomicSupported, i, j));
      }
      printf("%5s", i == j ? "X" : (std::to_string(acc) + "/" + std::to_string(nat)).c_str());
    }
    printf("\n");
  }

  timedOut.resize(n);
  for (int d = 0; d < n; d++) {
    CK(cudaSetDevice(d));
    CK(cudaMalloc(&timedOut[d], sizeof(int)));
  }
  bool native = true;
  for (size_t k = 0; k < peers.size(); k++) {
    int can = 0, nat = 0;
    CK(cudaDeviceCanAccessPeer(&can, peers[k], owner));
    if (!can) {
      printf("RESULT: FAIL (GPU%d cannot access GPU%d)\n", peers[k], owner);
      return 1;
    }
    CK(cudaDeviceGetP2PAttribute(&nat, cudaDevP2PAttrNativeAtomicSupported, peers[k], owner));
    native = native && nat;
    CK(cudaSetDevice(peers[k]));
    cudaError_t e = cudaDeviceEnablePeerAccess(owner, 0);
    if (e != cudaSuccess && e != cudaErrorPeerAccessAlreadyEnabled) CK(e);
    cudaGetLastError();
  }
  u64* counter;
  CK(cudaSetDevice(owner));
  CK(cudaMalloc(&counter, sizeof(u64)));
  int* goHost;
  CK(cudaHostAlloc(&goHost, sizeof(int), cudaHostAllocMapped | cudaHostAllocPortable));
  volatile int* go = goHost;
  const u64 perGpu = (u64)BLOCKS * THREADS * adds;
  printf("\ncounter on GPU%d, %llu atomicAdd per GPU, peers native atomics: %s\n", owner, perGpu,
         native ? "yes" : "no (loss expected when GPUs add concurrently)");

  bool fail = false, info = false;
  // label, GPUs that add
  struct Case {
    std::string label;
    std::vector<int> devs;
  };
  std::vector<Case> cases;
  cases.push_back({"GPU" + std::to_string(owner) + " alone (control)", {owner}});
  for (size_t k = 0; k < peers.size(); k++)
    cases.push_back({"GPU" + std::to_string(peers[k]) + " alone (peer)", {peers[k]}});
  Case all = {"all at once:", {owner}};
  for (size_t k = 0; k < peers.size(); k++) {
    all.devs.push_back(peers[k]);
    all.label += " GPU" + std::to_string(peers[k]);
  }
  all.label += " + GPU" + std::to_string(owner);
  cases.push_back(all);

  for (size_t c = 0; c < cases.size(); c++) {
    bool timeout;
    u64 got = run(owner, counter, cases[c].devs, adds, go, &timeout);
    u64 want = perGpu * cases[c].devs.size();
    const char* verdict = "ok";
    if (timeout) {
      verdict = "FAIL (a launch did not start within 10 s)";
      fail = true;
    } else if (got > want) {
      verdict = "FAIL (more increments than issued)";
      fail = true;
    } else if (got < want) {
      if (c == 0 || native) {
        verdict = c == 0 ? "FAIL (local atomics lost increments)" : "FAIL (loss despite native P2P atomics)";
        fail = true;
      } else {
        verdict = "INFO (loss: peer atomics are not atomic across GPUs)";
        info = true;
      }
    }
    printf("  %-40s got %12llu expected %12llu lost %6.2f%%  %s\n", cases[c].label.c_str(), got, want,
           100.0 * (double)(want - (got < want ? got : want)) / (double)want, verdict);
    fflush(stdout);
  }
  CK(cudaFreeHost(goHost));
  if (fail)
    printf("RESULT: FAIL\n");
  else
    printf("RESULT: PASS%s\n",
           info ? " (INFO: lost increments on peer atomics, expected without native P2P atomics)" : "");
  return fail ? 1 : 0;
}
