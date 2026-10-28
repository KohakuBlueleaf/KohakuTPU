/* The contracts between the layers: L0 calls (S -> M), system calls (U -> S),
 * the address layout every node shares, and how the interlink's signal slots
 * and the staging store are divided. */
#ifndef KOS_ABI_H
#define KOS_ABI_H

/* ---- L0 calls: ecall from S, a7 = function, a0..a5 args; a0 = status, a1 = value ---- */
#define KOS_M_PUTC      0  /* (byte) */
#define KOS_M_WRITE     1  /* (spad address, length): console bytes */
#define KOS_M_SET_TIMER 2  /* (absolute time) */
#define KOS_M_MAP       3  /* (va, pa, size, attr) */
#define KOS_M_UNMAP     4  /* (va, size) */
#define KOS_M_QUERY     5  /* (va) -> leaf PTE, 0 if none */
#define KOS_M_EXIT      6  /* (code): this node stops */
#define KOS_M_INFO      7  /* (key) -> value, KOS_MI_* */
#define KOS_M_NOP       8  /* round trip, for its cost */
#define KOS_M_NCALLS    9

#define KOS_MI_MAPS   0 /* map calls served */
#define KOS_MI_TABLES 1 /* page-table pages in use */
#define KOS_MI_TRAPS  2 /* traps L0 took */
#define KOS_MI_POOL   3 /* page-table pages in the pool */
#define KOS_MI_SATP   4 /* the satp value user mode runs under */

/* Map attributes (KOS_M_MAP). UNCACHED applies to DRAM: L0 maps the alias. */
#define KOS_MAP_R        (1u << 0)
#define KOS_MAP_W        (1u << 1)
#define KOS_MAP_X        (1u << 2)
#define KOS_MAP_U        (1u << 3)
#define KOS_MAP_UNCACHED (1u << 4)

/* L0 status codes */
#define KOS_OK       0
#define KOS_EINVAL  (-1)
#define KOS_EPERM   (-2)
#define KOS_ENOMEM  (-3)
#define KOS_EBUSY   (-4)
#define KOS_ENOENT  (-5)
#define KOS_EAGAIN  (-6)
#define KOS_EFAULT  (-7)
#define KOS_ETIME   (-8)

/* ---- system calls: ecall from U, a7 = number, a0..a5; a0 = result ---- */
#define SYS_EXIT          0  /* (code): this thread */
#define SYS_YIELD         1
#define SYS_WRITE         2  /* (buf, len) */
#define SYS_READ          3  /* (buf, len) -> bytes, waits for at least one */
#define SYS_SPAWN         4  /* (entry, arg, node) -> tid */
#define SYS_JOIN          5  /* (tid) -> exit code */
#define SYS_SELF          6  /* -> tid */
#define SYS_INFO          7  /* (key) -> value, KOS_I_* */
#define SYS_SLEEP         8  /* (cycles) */
#define SYS_TIME          9  /* -> global time (cycles) */
#define SYS_MMAP          10 /* (size, flags) -> address in this node's private area */
#define SYS_WAIT          11 /* (addr, value, timeout) -> 0 once *addr != value, KOS_ETIME */
#define SYS_ATOMIC        12 /* (addr, op, a, b) -> old value, at the address's home */
#define SYS_CACHE         13 /* (KOS_C_*) */
#define SYS_UNITS         14 /* (buf, max) -> count of struct kos_unit */
#define SYS_DISPATCH      15 /* (unit, words) -> ticket: words is 4 x u64 */
#define SYS_UNIT_WAIT     16 /* (unit, ticket, timeout) -> 0 / fault code */
#define SYS_UNIT_CTRL     17 /* (unit, index) -> CU_CTRL value */
#define SYS_MOVE          18 /* (src, dst, words) -> 0 / fault: a 1-D mover copy of 32-byte words */
#define SYS_SEND          19 /* (node, chan, buf, len<=40) */
#define SYS_RECV          20 /* (chan, buf, timeout) -> len, from any node */
#define SYS_STAT          21 /* (what, buf, max) -> bytes, KOS_S_* */
#define SYS_RUN           22 /* (name, args, node) -> tid: start a registered app */
#define SYS_BARRIER       23 /* (id, parties) cross-node barrier, a kernel object */
#define SYS_ENGINE        24 /* (unit, words, count) -> 0 / fault: instructions through the dispatch engine */
#define SYS_SHUTDOWN      25 /* (code): every node exits; the caller's node last */
#define SYS_LAND          26 /* (node) -> user address of this node's landing zone; `node` must be this node */
#define SYS_NCALLS        27

#define KOS_I_NODE    0
#define KOS_I_NODES   1
#define KOS_I_MESH    2
#define KOS_I_FLAGS   3
#define KOS_I_SHARED  4 /* user address of the shared region, 0 if none */
#define KOS_I_SHSIZE  5
#define KOS_I_SHHEAP  6 /* first free byte of the shared region (after .ushared) */
#define KOS_I_CLOCK   7
#define KOS_I_NUNITS  8
#define KOS_I_L0RTT   9 /* cycles of one kernel -> L0 call round trip, measured now */
#define KOS_I_SHPA    10 /* the address units and the mover name the shared region by, 0 if none */
#define KOS_I_LANDPA  11 /* the address units and the mover name this node's landing zone by */

#define KOS_C_RELEASE 1 /* write back this node's dirty lines */
#define KOS_C_ACQUIRE 2 /* write back, then drop every line */

/* SYS_ATOMIC ops, applied at the home node to the word's uncached alias */
#define KOS_A_ADD  0
#define KOS_A_SWAP 1
#define KOS_A_CAS  2 /* (a = expected, b = new) */
#define KOS_A_LOAD 3
#define KOS_A_MAX  4

#define KOS_S_THREADS 0 /* struct kos_thread_stat[] */
#define KOS_S_KERNEL  1 /* struct kos_kernel_stat */
#define KOS_S_UNITS   2 /* struct kos_unit_stat[] */

/* SYS_MOVE flags (a3): the destination is node n's landing zone, offset dst */
#define KOS_MV_LAND(n) (0x100u | (n))

/* tid = node << 8 | slot */
#define KOS_TID(node, slot) (((node) << 8) | (slot))
#define KOS_TID_NODE(t)     ((unsigned)(t) >> 8)
#define KOS_TID_SLOT(t)     ((unsigned)(t) & 0xffu)

/* ---- the address layout (the same on every node) ----
 * Only user mode runs translated. The kernel runs with satp 0 and names
 * physical addresses. Under Sv39 this core sends a load or store that follows
 * another one back to back to the first one's address (tests/xlat), so
 * translated code is built with a gap after every memory access
 * (tools/memgap.py); untranslated code needs none. */
#define KOS_UV_TEXT    0x0000000010000000UL /* the user image: text, rodata, app table (private span + 0) */
#define KOS_UV_DATA    0x0000000011000000UL /* user per-node data and bss (in the user image, after the text) */
#define KOS_UV_SHARED  0x0000000020000000UL /* the shared region, .ushared first */
#define KOS_UV_SHUNC   0x0000000028000000UL /* the same region, uncached */
#define KOS_UV_STACK   0x0000000030000000UL /* + node * 2 MB + slot * KOS_STACK_SPAN */
#define KOS_UV_LAND    0x0000000038000000UL /* this node's landing zone, uncached: where units and movers deliver */
#define KOS_UV_MMAP    0x0000000040000000UL /* SYS_MMAP's private area */
#define KOS_STACK_SPAN 0x0000000000020000UL
#define KOS_STACK_SIZE 0x0000000000004000UL
#define KOS_LAND_SIZE  0x0000000000010000UL /* the tail of each node's private span */
#define KOS_LOG_SIZE   0x0000000000004000UL /* the kernel's console log, before the landing zone */
/* The log's first 64 bytes, written uncached so a host reads them while the
 * node runs or after it hangs (u64 index); the bytes follow from +64. */
#define KOS_LOG_COUNT    0 /* bytes ever written */
#define KOS_LOG_WHERE    1 /* what the node is doing now: KOS_W_* */
#define KOS_LOG_A        2
#define KOS_LOG_B        3
#define KOS_LOG_SWITCHES 4 /* threads started or resumed: moves while the node schedules */
#define KOS_W_BOOT  1 /* a = boot step */
#define KOS_W_START 2 /* start.h: a = run id, b = 0 clearing slots, 1 + j waiting for node j, 9 waiting for go */
#define KOS_W_RING  3 /* waiting for ring room: a = rings this node queued, b = the sig word */
#define KOS_W_SEND  4 /* waiting for credit: a = node, b = messages sent << 32 | credits back */
#define KOS_W_IDLE  5 /* nothing to run, polling */
#define KOS_W_USER  6 /* running a thread: a = tid, b = its pc */
#define KOS_W_PROBE 7 /* m0: a = probe index */
#define KOS_W_DONE  8 /* exiting: a = code */
#define KOS_W_BAR   9 /* m0: in a barrier, a = epoch, b = its slot's count on entry */
/* A private span of `size` bytes, from its tail: landing zone, log, and (with
 * KOS_F_PT_DRAM) L0's page-table pool; the kernel's frames are the rest. */
#define KOS_PRIV_LAND_OFF(size) ((size) - KOS_LAND_SIZE)
#define KOS_PRIV_LOG_OFF(size)  (KOS_PRIV_LAND_OFF(size) - KOS_LOG_SIZE)
#define KOS_PRIV_PT_OFF(size)   (KOS_PRIV_LOG_OFF(size) - KOS_STG_PTSIZE)


/* ---- signal slots (16 per node) ---- */
#define KOS_SLOT_MSG(src)  (src)       /* messages from node src: 0..3 */
#define KOS_SLOT_CRED(dst) (4 + (dst)) /* credits returned by node dst: 4..7 */
#define KOS_SLOT_BARRIER   8           /* kernel barrier arrivals */
#define KOS_SLOT_USER      9           /* 9..15: free */

/* ---- the staging store, from each mesh's staging base ---- */
#define KOS_STG_PT     0x00000UL /* L0's page-table pool: 32 tables */
#define KOS_STG_PTSIZE 0x20000UL
#define KOS_STG_MSG    0x20000UL /* message rings, one per sender */
#define KOS_MSG_SLOTS  16u
#define KOS_MSG_BYTES  64u
#define KOS_STG_RING(src) (KOS_STG_MSG + (src) * KOS_MSG_SLOTS * KOS_MSG_BYTES)
#define KOS_STG_START  (KOS_STG_MSG + 4 * KOS_MSG_SLOTS * KOS_MSG_BYTES) /* start.h: ready[4], go */
#define KOS_STG_END    (KOS_STG_START + 64)

#endif
