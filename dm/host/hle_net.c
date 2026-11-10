// Networking (out of scope: online play/friends/store/cloud saves): all hooked up as "network unavailable", the game takes the offline branch.
// Cuts: nifm reports no network; socket only hooks up Initialize (returns success, creates no session), the rest of socket/ssl/friends are hooked up as failures when they come into use.
#include "hle.h"
#include <stdatomic.h>

HLE_FORCE("_ZN2nn6socket10InitializeERKNS0_6ConfigE", "connects to the bsd socket service; offline, creates no session (cut)", Result, h_socket_Initialize, const void *cfg) { (void)cfg; return RES_OK; }
HLE_FORCE("_ZN2nn4nifm10InitializeEv", "connects to the nifm network management service; offline (cut)", Result, h_nifm_Initialize, void) { return RES_OK; }
// The request is asynchronous on Horizon: SubmitNetworkRequest only hands it to the nifm service, and IsNetworkRequestOnHold stays true until the service has evaluated it, so a query
// right after the submission finds it on hold. Pikmin 4 (Unreal Engine 4.26, main+0x203f3b0) relies on that: it starts its BCAT thread, whose Run (main+0x203f510) sets its status
// to 2, submits, waits while on hold and sets 6 (done, offline), and only then stores status 1 itself; answered at once, a thread that ran first finished before that store, the 1 stayed,
// and the boot state machine (main+0x1f2f764, state 7) waited for the status forever at the black loading screen (about 1 boot in 6, 3 in 4 with four at once). Pikmin 4 sleeps 10 ms
// between its queries, so a request that is on hold at the first query is all it needs.
// Each query is an IPC to the nifm service on Horizon, which evaluates the request as it serves them (offline, a rejection), so the request is on hold for the first query after
// the submission and settled at the next. The hold used to be 50 ms of host time: Hades' NetworkPollThread (main+0x27aaa0) polls with SleepThread(0) and cancels after 2,000
// queries, 2,000 IPCs on the Switch but 0.1 ms here, so it cancelled and submitted again forever (1.17 billion queries a minute, a host core). Its stamp, now_ns() | 1, also read
// as settled at once whenever the next query came within the same 42 ns clock tick (30% of Hades' submissions)
static _Atomic int g_nifm_hold;   // queries the pending request still answers "on hold" (0 = none, or settled)
HLE_FORCE("_ZN2nn4nifm20SubmitNetworkRequestEv", "offline: the request is on hold for the first query, then settles as unavailable", void, h_nifm_Submit, void) { atomic_store(&g_nifm_hold, 1); }
HLE_FORCE("_ZN2nn4nifm20CancelNetworkRequestEv", "offline: drops the pending request", void, h_nifm_Cancel, void) { atomic_store(&g_nifm_hold, 0); }
HLE_FORCE("_ZN2nn4nifm22IsNetworkRequestOnHoldEv", "offline: on hold for the first query after the submission", int, h_nifm_OnHold, void) {
    int h = atomic_load(&g_nifm_hold);
    while (h > 0 && !atomic_compare_exchange_weak(&g_nifm_hold, &h, h - 1)) {}
    return h > 0;
}
HLE_FORCE("_ZN2nn4nifm18IsNetworkAvailableEv", "offline: the network is always unavailable", int, h_nifm_Available, void) { return 0; }
// Returns bool (`and w0, w0, #1`, Metroid Dread's sdk+0x336210): whether the system's network screen resolved the error
HLE_FORCE("_ZN2nn4nifm31HandleNetworkRequestErrorResultEv", "offline: not resolved, shows no system network screen", unsigned, h_nifm_HandleErr, void) { return 0; }
HLE_FORCE("_ZN2nn4nifm26GetCurrentPrimaryIpAddressEPNS_6socket6InAddrE", "offline: no IP", Result, h_nifm_GetIp, uint32_t *a) { if (a) *a = 0; return RES_FAIL; }
HLE_FORCE("_ZN2nn4nifm22GetCurrentProxySettingEPNS0_12ProxySettingE", "offline: no proxy setting", Result, h_nifm_GetProxy, void *p) { (void)p; return RES_FAIL; }
HLE_FORCE("nnsslInitialize", "connects to the ssl service; the network library's global init needs it to succeed, offline creates no session (cut)", Result, h_ssl_Initialize, void) { return RES_OK; }
HLE_FORCE("nnsslFinalize", "pairs with nnsslInitialize (cut)", Result, h_ssl_Finalize, void) { return RES_OK; }

// ---------- socket: a real loopback pass-through (Minecraft Bedrock's WebRTC Signaler RTC_CHECKs at boot when Socket
// returns -1 -- physical_socket_server.cc:1358 "fdNone"; every other game so far takes the offline branch on the nifm
// stubs above, so live descriptors change nothing for them). Horizon's bsd service and Darwin share the FreeBSD
// heritage: the sockaddr_in / pollfd / fd_set layouts and the SOL_SOCKET option numbers line up, so the descriptors
// pass straight through (the guest holds them as opaque ints; the host fs is path-based, no fd table collision).
// Name resolution stays cut (EAI_FAIL): no DNS, the game's own online stack concludes offline on its own.
#include <sys/socket.h>
#include <poll.h>
#include <unistd.h>
#include <sys/select.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <errno.h>
#include <netinet/in.h>
static __thread int t_skerr = 0;
static int sk_errno(void) { int e = errno; t_skerr = e ? e : 50 /*ENETDOWN*/; return -1; }
#define SKF(m, cname, ...) HLE_FORCE(m, "bsd socket pass-through (loopback works; no DNS)", int, cname, __VA_ARGS__)
SKF("_ZN2nn6socket6SocketENS0_6FamilyENS0_4TypeENS0_8ProtocolE", h_sk_Socket, int a, int b, int c) { int r = socket(a, b, c); return r < 0 ? sk_errno() : r; }
// Loopback-only: the pass-through exists for local pairs (WebRTC's Signaler socketpair, a local stub server);
// anything aimed at the wider network fails at once with ENETDOWN, so the game's own online stack (Bedrock's
// NetherNet spins 100% of a core retrying Xbox Live endpoints otherwise) concludes offline instead of retrying
static int sk_remote(const void *a) {
    if (!a) return 0;
    const struct sockaddr_in *in = a;                       // nn SockAddrIn layout = sockaddr_in (no sin_len)
    if (in->sin_family == AF_INET && (in->sin_addr.s_addr == htonl(INADDR_LOOPBACK) || in->sin_addr.s_addr == 0)) return 0;
    return 1;
}
static int sk_down(void) { errno = ENETDOWN; return sk_errno(); }
SKF("_ZN2nn6socket4BindEiPKNS0_8SockAddrEj", h_sk_Bind, int s, const void *a, unsigned n) { if (sk_remote(a)) return sk_down(); return bind(s, a, n) < 0 ? sk_errno() : 0; }
SKF("_ZN2nn6socket7ConnectEiPKNS0_8SockAddrEj", h_sk_Connect, int s, const void *a, unsigned n) { if (sk_remote(a)) return sk_down(); return connect(s, a, n) < 0 ? sk_errno() : 0; }
SKF("_ZN2nn6socket6ListenEii", h_sk_Listen, int s, int n) { return listen(s, n) < 0 ? sk_errno() : 0; }
SKF("_ZN2nn6socket6AcceptEiPNS0_8SockAddrEPj", h_sk_Accept, int s, void *a, unsigned *n) {
    socklen_t l = n ? *n : 0; int r = accept(s, a, n ? &l : NULL); if (r < 0) return sk_errno(); if (n) *n = l; return r;
}
SKF("_ZN2nn6socket5CloseEi", h_sk_Close, int s) { return close(s) < 0 ? sk_errno() : 0; }
SKF("_ZN2nn6socket11GetPeerNameEiPNS0_8SockAddrEPj", h_sk_GetPeerName, int s, void *a, unsigned *n) {
    socklen_t l = n ? *n : 0; int r = getpeername(s, a, n ? &l : NULL); if (r < 0) return sk_errno(); if (n) *n = l; return r;
}
SKF("_ZN2nn6socket11GetSockNameEiPNS0_8SockAddrEPj", h_sk_GetSockName, int s, void *a, unsigned *n) {
    socklen_t l = n ? *n : 0; int r = getsockname(s, a, n ? &l : NULL); if (r < 0) return sk_errno(); if (n) *n = l; return r;
}
SKF("_ZN2nn6socket10GetSockOptEiNS0_5LevelENS0_6OptionEPvPj", h_sk_GetSockOpt, int s, int l, int o, void *v, unsigned *n) {
    socklen_t ln = n ? *n : 0; int r = getsockopt(s, l, o, v, n ? &ln : NULL); if (r < 0) return sk_errno(); if (n) *n = ln; return r;
}
SKF("_ZN2nn6socket10SetSockOptEiNS0_5LevelENS0_6OptionEPKvj", h_sk_SetSockOpt, int s, int l, int o, const void *v, unsigned n) { return setsockopt(s, l, o, v, n) < 0 ? sk_errno() : 0; }
// fcntl: Horizon (FreeBSD) O_NONBLOCK 0x80 vs Darwin 0x04; F_GETFL 3 / F_SETFL 4 are the same numbers
SKF("_ZN2nn6socket5FcntlEiNS0_12FcntlCommandEz", h_sk_Fcntl, int s, int c, long a) {
    if (c == 4) a = (a & ~0x80l) | ((a & 0x80l) ? 4 : 0);
    long r = fcntl(s, c, a);
    if (r < 0) return sk_errno();
    if (c == 3) r = (r & ~4l) | ((r & 4l) ? 0x80 : 0);
    return (int)r;
}
SKF("_ZN2nn6socket4PollEPNS0_6PollFdEmi", h_sk_Poll, void *f, unsigned long n, int t) { int r = poll(f, (nfds_t)n, t); return r < 0 ? sk_errno() : r; }
SKF("_ZN2nn6socket6SelectEiPNS0_5FdSetES2_S2_PNS0_7TimeValE", h_sk_Select, int n, void *r, void *w, void *e, void *t) {
    struct { long s, u; } *tv = t;
    struct timeval d = { tv ? tv->s : 0, tv ? tv->u : 0 };
    int k = select(n, r, w, e, t ? &d : NULL); return k < 0 ? sk_errno() : k;
}
HLE_FORCE("_ZN2nn6socket4RecvEiPvmNS0_7MsgFlagE", "bsd socket pass-through", long, h_sk_Recv, int s, void *b, unsigned long n, int f) { long r = recv(s, b, n, f); return r < 0 ? sk_errno() : r; }
HLE_FORCE("_ZN2nn6socket8RecvFromEiPvmNS0_7MsgFlagEPNS0_8SockAddrEPj", "bsd socket pass-through", long, h_sk_RecvFrom, int s, void *b, unsigned long n, int f, void *a, unsigned *al) {
    socklen_t l = al ? *al : 0; long r = recvfrom(s, b, n, f, a, al ? &l : NULL); if (r < 0) return sk_errno(); if (al) *al = l; return r;
}
HLE_FORCE("_ZN2nn6socket4SendEiPKvmNS0_7MsgFlagE", "bsd socket pass-through", long, h_sk_Send, int s, const void *b, unsigned long n, int f) { long r = send(s, b, n, f); return r < 0 ? sk_errno() : r; }
HLE_FORCE("_ZN2nn6socket7SendToEiPKvmNS0_7MsgFlagEPKNS0_8SockAddrEj", "bsd socket pass-through", long, h_sk_SendTo, int s, const void *b, unsigned long n, int f, const void *a, unsigned al) { long r = sendto(s, b, n, f, a, al); return r < 0 ? sk_errno() : r; }
HLE_FORCE("_ZN2nn6socket8ShutdownEii", "bsd socket pass-through", int, h_sk_Shutdown, int s, int how) { return shutdown(s, how) < 0 ? sk_errno() : 0; }
// C interface: the same pass-through
SKF("nnsocketSocket", h_nsk_Socket, int a, int b, int c) { int r = socket(a, b, c); return r < 0 ? sk_errno() : r; }
SKF("nnsocketBind", h_nsk_Bind, int s, const void *a, unsigned n) { if (sk_remote(a)) return sk_down(); return bind(s, a, n) < 0 ? sk_errno() : 0; }
SKF("nnsocketConnect", h_nsk_Connect, int s, const void *a, unsigned n) { if (sk_remote(a)) return sk_down(); return connect(s, a, n) < 0 ? sk_errno() : 0; }
SKF("nnsocketClose", h_nsk_Close, int s) { return close(s) < 0 ? sk_errno() : 0; }
SKF("nnsocketGetPeerName", h_nsk_GetPeerName, int s, void *a, unsigned *n) { socklen_t l = n ? *n : 0; int r = getpeername(s, a, n ? &l : NULL); if (r < 0) return sk_errno(); if (n) *n = l; return r; }
SKF("nnsocketGetSockName", h_nsk_GetSockName, int s, void *a, unsigned *n) { socklen_t l = n ? *n : 0; int r = getsockname(s, a, n ? &l : NULL); if (r < 0) return sk_errno(); if (n) *n = l; return r; }
SKF("nnsocketGetSockOpt", h_nsk_GetSockOpt, int s, int l, int o, void *v, unsigned *n) { socklen_t ln = n ? *n : 0; int r = getsockopt(s, l, o, v, n ? &ln : NULL); if (r < 0) return sk_errno(); if (n) *n = ln; return r; }
SKF("nnsocketSetSockOpt", h_nsk_SetSockOpt, int s, int l, int o, const void *v, unsigned n) { return setsockopt(s, l, o, v, n) < 0 ? sk_errno() : 0; }
SKF("nnsocketFcntl", h_nsk_Fcntl, int s, int c, long a) { if (c == 4) a = (a & ~0x80l) | ((a & 0x80l) ? 4 : 0); long r = fcntl(s, c, a); if (r < 0) return sk_errno(); if (c == 3) r = (r & ~4l) | ((r & 4l) ? 0x80 : 0); return (int)r; }
SKF("nnsocketPoll", h_nsk_Poll, void *f, unsigned long n, int t) { int r = poll(f, (nfds_t)n, t); return r < 0 ? sk_errno() : r; }
HLE_FORCE("nnsocketRecv", "bsd socket pass-through", long, h_nsk_Recv, int s, void *b, unsigned long n, int f) { long r = recv(s, b, n, f); return r < 0 ? sk_errno() : r; }
HLE_FORCE("nnsocketSend", "bsd socket pass-through", long, h_nsk_Send, int s, const void *b, unsigned long n, int f) { long r = send(s, b, n, f); return r < 0 ? sk_errno() : r; }

// ---------- ssl: offline, Initialize succeeds (the network library's global initialization depends on it), everything else that contacts the service fails ----------
// Not cut: Context / Connection construction and destruction, Context::GetContextId (only read and write game objects).
#define SSF(m, cname, ...) HLE_FORCE(m, "ssl service (cut): fails", Result, cname, __VA_ARGS__)
HLE_FORCE("_ZN2nn3ssl10InitializeEv", "same as nnsslInitialize: succeeds, creates no session (cut)", Result, h_ssl_InitCpp, void) { return RES_OK; }
HLE_FORCE("_ZN2nn3ssl8FinalizeEv", "pairs with Initialize (cut)", Result, h_ssl_FinCpp, void) { return RES_OK; }
SSF("_ZN2nn3ssl7Context6CreateENS1_10SslVersionE", h_ssl_CtxCreate, void *self, int v) { (void)self; (void)v; return RES_FAIL; }
SSF("_ZN2nn3ssl7Context7DestroyEv", h_ssl_CtxDestroy, void *self) { (void)self; return RES_FAIL; }
SSF("_ZN2nn3ssl7Context19RegisterInternalPkiEPmNS1_11InternalPkiE", h_ssl_CtxRegPki, void *self, uint64_t *id, int p) { (void)self; (void)id; (void)p; return RES_FAIL; }
SSF("_ZN2nn3ssl7Context9RemovePkiEm", h_ssl_CtxRemovePki, void *self, uint64_t id) { (void)self; (void)id; return RES_FAIL; }
SSF("_ZN2nn3ssl14BuiltInManager28GetBuiltInCertificateBufSizeEPjPNS0_15CaCertificateIdEj", h_ssl_BiBufSize, uint32_t *o, void *ids, unsigned n) { (void)ids; (void)n; if (o) *o = 0; return RES_FAIL; }
SSF("_ZN2nn3ssl14BuiltInManager22GetBuiltInCertificatesEPPNS1_22BuiltInCertificateInfoEPhjPNS0_15CaCertificateIdEj", h_ssl_BiCerts, void **o, uint8_t *b, unsigned bs, void *ids, unsigned n) { (void)o; (void)b; (void)bs; (void)ids; (void)n; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection6CreateEPNS0_7ContextE", h_ssl_CnCreate, void *self, void *ctx) { (void)self; (void)ctx; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection7DestroyEv", h_ssl_CnDestroy, void *self) { (void)self; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection11DoHandshakeEv", h_ssl_CnHandshake, void *self) { (void)self; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection11SetHostNameEPKcj", h_ssl_CnHost, void *self, const char *h, unsigned n) { (void)self; (void)h; (void)n; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection9SetIoModeENS1_6IoModeE", h_ssl_CnIoMode, void *self, int m) { (void)self; (void)m; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection19SetSessionCacheModeENS1_16SessionCacheModeE", h_ssl_CnCache, void *self, int m) { (void)self; (void)m; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection19SetSocketDescriptorEi", h_ssl_CnSock, void *self, int s) { (void)self; (void)s; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection12GetLastErrorEPNS_6ResultE", h_ssl_CnLastErr, void *self, uint32_t *r) { (void)self; if (r) *r = RES_FAIL; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection18GetVerifyCertErrorEPNS_6ResultE", h_ssl_CnVerifyErr, void *self, uint32_t *r) { (void)self; if (r) *r = RES_FAIL; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection4PeekEPcPij", h_ssl_CnPeek, void *self, char *b, int *o, unsigned n) { (void)self; (void)b; (void)n; if (o) *o = 0; return RES_FAIL; }
SSF("_ZN2nn3ssl10Connection4PollEPNS1_9PollEventES3_j", h_ssl_CnPoll, void *self, void *o, void *i, unsigned t) { (void)self; (void)o; (void)i; (void)t; return RES_FAIL; }
HLE_FORCE("_ZN2nn3ssl10Connection4ReadEPcj", "ssl service (cut): read returns -1", int, h_ssl_CnRead, void *self, char *b, unsigned n) { (void)self; (void)b; (void)n; return -1; }
HLE_FORCE("_ZN2nn3ssl10Connection5WriteEPKcj", "ssl service (cut): write returns -1", int, h_ssl_CnWrite, void *self, const char *b, unsigned n) { (void)self; (void)b; (void)n; return -1; }
// C interface: all return nnResult
#define SSC(m, cname) HLE_FORCE(m, "ssl service (cut): fails", Result, cname, void) { return RES_FAIL; }
SSC("nnsslConnectionCreate", h_nssl_Create) SSC("nnsslConnectionDestroy", h_nssl_Destroy)
SSC("nnsslConnectionDoHandshakeWithCertBuffer", h_nssl_Handshake) SSC("nnsslConnectionGetConnectionId", h_nssl_GetId)
SSC("nnsslConnectionGetNextAlpnProto", h_nssl_GetAlpn) SSC("nnsslConnectionGetVerifyCertError", h_nssl_VerifyErr)
SSC("nnsslConnectionPeek", h_nssl_Peek) SSC("nnsslConnectionPending", h_nssl_Pending) SSC("nnsslConnectionRead", h_nssl_Read)
SSC("nnsslConnectionSetHostName", h_nssl_SetHost) SSC("nnsslConnectionSetIoMode", h_nssl_SetIo)
SSC("nnsslConnectionSetNextAlpnProto", h_nssl_SetAlpn) SSC("nnsslConnectionSetOption", h_nssl_SetOpt)
SSC("nnsslConnectionSetSessionCacheMode", h_nssl_SetCache) SSC("nnsslConnectionSetSocketDescriptor", h_nssl_SetSock)
SSC("nnsslConnectionSetVerifyOption", h_nssl_SetVerify) SSC("nnsslConnectionWrite", h_nssl_Write)
SSC("nnsslContextDestroy", h_nssl_CtxDestroy) SSC("nnsslContextGetContextId", h_nssl_CtxGetId)

// ---------- EventFd: Horizon's eventfd (an AF_EVENTFD socket in the bsd service, a family Darwin has none of), emulated
// over a pipe. Minecraft Bedrock's WebRTC Signaler (rtc_base) wakes its wait thread with it: Write adds a wake, Read clears.
// nn::socket::detail::EventFd aborts on any error, so with the raw socket pass-through it died at socket(AF_EVENTFD).
static __thread int t_efd_err;
HLE_FORCE("_ZN2nn6socket7EventFdEmNS0_12EventFdFlagsE", "eventfd emulated over a pipe (AF_EVENTFD has no Darwin form)", int, h_sk_EventFd, unsigned long v, int f) {
    (void)v;
    int p[2];
    if (pipe(p) < 0) { t_efd_err = errno; return -1; }
    fcntl(p[0], F_SETFL, (f & 0x800) ? O_NONBLOCK : 0);   // EFD_NONBLOCK
    fcntl(p[1], F_SETFL, O_NONBLOCK);                     // writes never block in eventfd use
    close(p[1]);
    return p[0];
}
HLE_FORCE("_ZN2nn6socket11EventFdReadEiPm", "eventfd read: one wake value, then the pipe is empty", int, h_sk_EventFdRead, int s, unsigned long *v) {
    unsigned long w = 0;
    ssize_t r = read(s, &w, 8);
    if (v) *v = w;
    return r == 8 ? 0 : (r < 0 ? -1 : 0);
}
HLE_FORCE("_ZN2nn6socket12EventFdWriteEim", "eventfd write: adds a wake (a pipe write of 8 bytes)", int, h_sk_EventFdWrite, int s, unsigned long v) {
    if (write(s, &v, 8) == 8) return 0;
    // full pipe = already signalled, that is success for a wake
    return errno == EAGAIN ? 0 : -1;
}
