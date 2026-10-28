/* Messages between node kernels. Node s sends to node d through a ring of
 * KOS_MSG_SLOTS 64-byte slots in d's staging (offset KOS_STG_RING(s)): eight
 * posted stores, then a FENCED ring of d's slot KOS_SLOT_MSG(s), which counts
 * only once the stores have landed. d drains its rings into an inbox, returns
 * credits on s's slot KOS_SLOT_CRED(d), then handles the inbox; a sender out
 * of credits keeps draining, so two nodes sending to each other never stall.
 * The doorbell count each ring raises is the interrupt that wakes a node. */
#include "kernel.h"
#include <kos/start.h>

static uint32_t tx_sent[KOS_MAX_NODES], rx_done[KOS_MAX_NODES];
static uint32_t rings_written;
static struct kmsg *inbox;
static unsigned in_head, in_tail;
static uint64_t next_req;
#ifdef KOS_POLL_EVENTS
static int rx_pending;
#endif

static volatile uint64_t *ring_at(unsigned dst_node, unsigned src, unsigned idx)
{
    return (volatile uint64_t *)(HW_STAGING(kn.mesh_of[dst_node]) + KOS_STG_RING(src) + idx * KOS_MSG_BYTES);
}

void k_ring(unsigned node, unsigned slot, unsigned amount, int fence)
{
    uint64_t w = hw_sig_word();
    if (((rings_written - HW_SIG_SENT(w)) & 0xffffu) >= HW_RING_DEPTH) {
        k_where_wait(KOS_W_RING, rings_written, w);
        while (((rings_written - HW_SIG_SENT(hw_sig_word())) & 0xffffu) >= HW_RING_DEPTH) {
        }
        k_where_back();
    }
    hw_wr(HW_R_IL + HW_IL_RING, ((uint64_t)(fence != 0) << 32) | ((uint64_t)amount << 16) |
                                    ((uint64_t)slot << 8) | kn.mesh_of[node]);
    ++rings_written;
}

void k_rings_drain(void)
{
    uint64_t w = hw_sig_word();
    if (((rings_written - HW_SIG_SENT(w)) & 0xffffu) != 0) {
        k_where_wait(KOS_W_RING, rings_written, w);
        while (((rings_written - HW_SIG_SENT(hw_sig_word())) & 0xffffu) != 0) {
        }
        k_where_back();
    }
}

struct chanmsg {
    uint32_t chan, src, len, pad;
    uint8_t data[48];
};
struct kbar {
    uint64_t id, parties, count;
    struct {
        uint32_t node;
        uint64_t req;
    } w[K_BAR_WAITERS];
};
/* in private DRAM: the inbox, the channel queue, the barriers (node 0's) */
static struct chanmsg *chq;
#ifndef KOS_CHAN_FIFO
static unsigned ch_head, ch_tail;
#else
struct channel {
    unsigned key, head, tail, count, next;
};
struct channel k_channels[K_CHANQ];
unsigned k_chan_buckets[K_CHANQ];
#define channels k_channels
#define buckets k_chan_buckets
static unsigned free_channel, free_message;
_Static_assert(sizeof(struct channel) == 20 && offsetof(struct channel, count) == 12 &&
               offsetof(struct channel, next) == 16 && K_CHANQ == 64, "entry.S channel index");

static unsigned *channel_link(unsigned key)
{
    unsigned *link = &buckets[(key ^ (key >> 16)) % K_CHANQ];
    while (*link && channels[*link].key != key) {
        link = &channels[*link].next;
    }
    return link;
}
#endif
static struct kbar *bars;

void k_msg_init(void)
{
    inbox = k_alloc(K_INBOX * sizeof *inbox);
    chq = k_alloc(K_CHANQ * sizeof *chq);
#ifdef KOS_CHAN_FIFO
    free_channel = free_message = 1;
    for (unsigned i = 1; i < K_CHANQ; ++i) {
        channels[i].next = i + 1 < K_CHANQ ? i + 1 : 0;
        chq[i].pad = channels[i].next;
    }
#endif
    if (kn.me == 0) {
        bars = k_alloc(K_BAR * sizeof *bars);
    }
    next_req = (uint64_t)kn.me << 56;
    kos_start(&k_boot, &rings_written, k_log_hdr());
}

uint64_t k_req_id(void) { return ++next_req; }

/* Drain every peer's ring into the inbox; return what was taken as credits. */
static void drain(void)
{
#ifdef KOS_POLL_EVENTS
    rx_pending = 0;
#endif
    hw_wr(HW_R_IL + HW_IL_CTL, 1 | 2); /* keep the link enabled, clear the doorbell counts */
    for (unsigned p = 0; p < kn.n; ++p) {
        if (p == kn.me) {
            continue;
        }
        uint32_t have = hw_slot(KOS_SLOT_MSG(p));
        uint32_t took = 0;
        while (rx_done[p] != have && (in_tail + 1) % K_INBOX != in_head) {
            volatile uint64_t *m = ring_at(kn.me, p, rx_done[p] % KOS_MSG_SLOTS);
            struct kmsg *d = &inbox[in_tail];
            d->hdr = m[0];
            for (int i = 0; i < 7; ++i) {
                d->p[i] = m[1 + i];
            }
            in_tail = (in_tail + 1) % K_INBOX;
            ++rx_done[p];
            ++took;
        }
#ifdef KOS_POLL_EVENTS
        rx_pending |= rx_done[p] != have;
#endif
        if (took) {
            kstat.msgs_rcvd += took;
            k_ring(p, KOS_SLOT_CRED(kn.me), took, 0);
        }
    }
}

int k_send_room(unsigned node)
{
    return (uint32_t)(tx_sent[node] - hw_slot(KOS_SLOT_CRED(node))) < KOS_MSG_SLOTS;
}

void k_send(unsigned node, unsigned type, const uint64_t *p, unsigned np)
{
    if (!k_send_room(node)) {
        k_where_wait(KOS_W_SEND, node, (uint64_t)tx_sent[node] << 32 | hw_slot(KOS_SLOT_CRED(node)));
        while (!k_send_room(node)) {
            ++kstat.msg_waits;
            drain();
            k_units_poll();
        }
        k_where_back();
    }
    volatile uint64_t *m = ring_at(node, kn.me, tx_sent[node] % KOS_MSG_SLOTS);
    m[0] = (uint64_t)type | (uint64_t)kn.me << 8;
    for (unsigned i = 0; i < 7; ++i) {
        m[1 + i] = i < np ? p[i] : 0;
    }
    k_ring(node, KOS_SLOT_MSG(kn.me), 1, 1);
    ++tx_sent[node];
    ++kstat.msgs_sent;
    if (kn.flags & KOS_F_TRACE) {
        k_printf("[k%u] -> %u type %u p0 %lx\n", kn.me, node, type, np ? p[0] : 0);
    }
}

void k_reply(unsigned node, uint64_t req, int64_t status, uint64_t value)
{
    uint64_t p[3] = {req, (uint64_t)status, value};
    if (node == kn.me) {
        for (unsigned i = 0; i < K_THREADS; ++i) {
            struct thread *t = &kthreads[i];
            if (t->state == KOS_T_BLOCKED && t->wkind == W_REPLY && t->wa == req) {
                t->x[11] = value;
                k_wake(t, status);
            }
        }
        return;
    }
    k_send(node, M_REPLY, p, 3);
}

/* The calling thread waits for the reply to `p[0]` (a fresh request id is
 * put there); the reply's status lands in a0 and its value in a1. */
void k_rpc(unsigned node, unsigned type, uint64_t *p, unsigned np)
{
    p[0] = k_req_id();
    k_block(kcur, W_REPLY, p[0], 0, 0);
    k_send(node, type, p, np);
}

/* ---- the handlers ---- */
#ifdef KOS_REMOTE_BATCH
__attribute__((noinline, minsize))
#endif
static void handle(struct kmsg *m)
{
    unsigned src = KMSG_SRC(m);
    uint64_t *p = m->p;
    if (kn.flags & KOS_F_TRACE) {
        k_printf("[k%u] <- %u type %u p0 %lx\n", kn.me, src, KMSG_TYPE(m), p[0]);
    }
    switch (KMSG_TYPE(m)) {
    case M_REPLY:
        k_reply(kn.me, p[0], (int64_t)p[1], p[2]);
        break;
    case M_SPAWN: {
        struct thread *t = k_new_thread(p[1], p[2], p[3], "thread");
        k_reply(src, p[0], t ? KOS_OK : KOS_EAGAIN, t ? t->tid : 0);
        break;
    }
    case M_RUN: {
        char args[41];
        memcpy(args, &p[2], 40);
        args[40] = 0;
        int64_t tid = k_run_local((unsigned)p[1], args);
        k_reply(src, p[0], tid < 0 ? tid : KOS_OK, tid < 0 ? 0 : (uint64_t)tid);
        break;
    }
    case M_JOIN: {
        unsigned slot = KOS_TID_SLOT(p[1]);
        struct thread *t = slot < K_THREADS ? &kthreads[slot] : 0;
        if (!t || t->tid != p[1] || t->state == KOS_T_FREE) {
            k_reply(src, p[0], KOS_ENOENT, 0);
        } else if (t->state == KOS_T_ZOMBIE) {
            t->state = KOS_T_FREE;
            k_reply(src, p[0], KOS_OK, (uint64_t)t->exit_code);
        } else {
            t->rjoin_node = (int)src;
            t->rjoin_req = p[0];
        }
        break;
    }
    case M_ATOMIC:
        k_reply(src, p[0], KOS_OK, k_atomic_local(p[1], (unsigned)p[2], p[3], p[4]));
        break;
    case M_CHAN: /* k_msg_poll puts it */
        break;
    case M_DISPATCH: {
        int64_t r = k_dispatch_local((unsigned)p[1], &p[2]);
        k_reply(src, p[0], r < 0 ? r : KOS_OK, r < 0 ? 0 : (uint64_t)r);
        break;
    }
#ifdef KOS_REMOTE_BATCH
    case M_BATCH: {
        const uint64_t *w = (const uint64_t *)((kn.priv + kn.land_off + p[2]) | HW_UNC);
        k_batch_start(src, p[0], (unsigned)p[1], w, (unsigned)p[3]);
        break;
    }
#endif
    case M_UNIT_WAIT:
        k_pending_add(M_UNIT_WAIT, src, p[0], p[1], p[2]);
        break;
    case M_UNIT_CTRL:
        k_pending_add(M_UNIT_CTRL, src, p[0], p[1], p[2]);
        break;
    case M_BAR:
        k_bar_arrive(src, p[0], p[1], p[2]);
        break;
    case M_PING:
        k_reply(src, p[0], KOS_OK, hw_time());
        break;
    case M_SHUTDOWN:
        k_halt(p[0]);
    default:
        k_printf("node %u: message type %u from %u dropped\n", kn.me, KMSG_TYPE(m), src);
        break;
    }
}

/* Handle the inbox in arrival order. A channel message that finds the
 * channel queue full stays in the inbox, and so does every channel message
 * after it (channel order holds); the rest are handled. A full inbox stops
 * the drain, so the senders run out of credits: that is the flow control of
 * channels. */
void k_msg_poll(void)
{
    if (kn.n < 2) {
        return;
    }
#ifdef KOS_POLL_EVENTS
    if (rx_pending || hw_rd(HW_R_DBCNT)) {
        drain();
    }
#else
    drain();
#endif
    unsigned keep = in_head;
    int held = 0;
    for (unsigned i = in_head; i != in_tail; i = (i + 1) % K_INBOX) {
        struct kmsg m = inbox[i];
        if (KMSG_TYPE(&m) == M_CHAN &&
            (held || k_chan_put(KMSG_SRC(&m), (unsigned)m.p[0], &m.p[2], (unsigned)m.p[1]))) {
            held = 1;
            inbox[keep] = m;
            keep = (keep + 1) % K_INBOX;
            continue;
        }
        handle(&m);
    }
    in_tail = keep;
}

/* ---- channels: a node-local queue every sender feeds ---- */

int k_chan_put(unsigned src, unsigned chan, const void *data, unsigned len)
{
#ifdef KOS_CHAN_FIFO
    if (!free_message) {
        ++kstat.chan_full;
        return -1;
    }
    unsigned *link = channel_link(chan);
    if (!*link) {
        unsigned q = free_channel;
        free_channel = channels[q].next;
        channels[q] = (struct channel){.key = chan};
        *link = q;
    }
    struct channel *q = &channels[*link];
    unsigned i = free_message;
    struct chanmsg *c = &chq[i];
    free_message = c->pad;
    c->src = src;
    c->len = len > 40 ? 40 : len;
    memcpy(c->data, data, c->len);
    c->pad = 0;
    if (q->count) {
        chq[q->tail].pad = i;
    } else {
        q->head = i;
    }
    q->tail = i;
    ++q->count;
#ifdef KOS_POLL_EVENTS
    k_channel_ready(chan);
#endif
    return 0;
#else
    if ((ch_tail + 1) % K_CHANQ == ch_head) {
        ++kstat.chan_full;
        return -1;
    }
    struct chanmsg *c = &chq[ch_tail];
    c->chan = chan;
    c->src = src;
    c->len = len > 40 ? 40 : len;
    memcpy(c->data, data, c->len);
    ch_tail = (ch_tail + 1) % K_CHANQ;
#ifdef KOS_POLL_EVENTS
    k_channel_ready(chan);
#endif
    return 0;
#endif
}

unsigned k_chan_count(unsigned chan)
{
#ifdef KOS_CHAN_FIFO
    return channels[*channel_link(chan)].count;
#else
    unsigned n = 0;
    for (unsigned i = ch_head; i != ch_tail; i = (i + 1) % K_CHANQ) {
        n += chq[i].chan == chan;
    }
    return n;
#endif
}

/* A user send: into the local queue, or a message to the node, when there is
 * room; the caller blocks (W_SEND) and retries otherwise. */
int k_chan_send(uint64_t node_chan, const void *data, unsigned len)
{
    unsigned node = (unsigned)(node_chan >> 32), chan = (unsigned)node_chan;
    if (node == kn.me) {
        return k_chan_put(kn.me, chan, data, len);
    }
    if (!k_send_room(node)) {
        return -1;
    }
    uint64_t p[7] = {chan, len, 0, 0, 0, 0, 0};
    memcpy(&p[2], data, len);
    k_send(node, M_CHAN, p, 7);
    return 0;
}

/* The oldest message on t's channel (wa) into its user buffer (wb); its
 * length (KOS_EFAULT when the buffer is not mapped), or -1 when none. a1 gets
 * the source node. */
int k_chan_take(struct thread *t)
{
#ifdef KOS_CHAN_FIFO
    if (t->wa > 0xffffffffUL) {
        return -1;
    }
    unsigned *link = channel_link((unsigned)t->wa), id = *link;
    if (!id) {
        return -1;
    }
    struct channel *q = &channels[id];
    unsigned i = q->head;
    struct chanmsg *c = &chq[i];
    const void *payload = c->data;
#if defined(KOS_HOT_MUT) && KOS_HOT_MUT == 6
    payload = chq[q->tail].data;
#endif
    int len = k_copy_out(t->wb, payload, c->len) ? KOS_EFAULT : (int)c->len;
    t->x[11] = c->src;
    q->head = c->pad;
    c->pad = free_message;
    free_message = i;
    if (!--q->count) {
        *link = q->next;
        q->next = free_channel;
        free_channel = id;
    }
    return len;
#else
    for (unsigned i = ch_head; i != ch_tail; i = (i + 1) % K_CHANQ) {
        struct chanmsg *c = &chq[i];
        if (c->chan != t->wa) {
            continue;
        }
        int len = k_copy_out(t->wb, c->data, c->len) ? KOS_EFAULT : (int)c->len;
        t->x[11] = c->src;
        /* close the gap, keeping order */
        for (unsigned j = i; j != ch_head;) {
            unsigned prev = (j + K_CHANQ - 1) % K_CHANQ;
            chq[j] = chq[prev];
            j = prev;
        }
        ch_head = (ch_head + 1) % K_CHANQ;
        return len;
    }
    return -1;
#endif
}

/* ---- atomics: one home per shared word, the word's uncached alias ---- */
unsigned k_home(uint64_t off) { return (unsigned)((off >> 12) % kn.n); }

uint64_t k_atomic_local(uint64_t off, unsigned op, uint64_t a, uint64_t b)
{
    volatile uint64_t *w = (volatile uint64_t *)((kn.shared + off) | HW_UNC);
    uint64_t old = *w;
    switch (op) {
    case KOS_A_ADD:
        *w = old + a;
        break;
    case KOS_A_SWAP:
        *w = a;
        break;
    case KOS_A_CAS:
        if (old == a) {
            *w = b;
        }
        break;
    case KOS_A_MAX:
        if ((int64_t)a > (int64_t)old) {
            *w = a;
        }
        break;
    default:
        break;
    }
    return old;
}

/* ---- barriers: node 0 counts arrivals and answers them all ---- */

void k_bar_arrive(unsigned node, uint64_t req, uint64_t id, uint64_t parties)
{
    struct kbar *b = 0;
    for (unsigned i = 0; i < K_BAR; ++i) {
        if (bars[i].count && bars[i].id == id) {
            b = &bars[i];
            break;
        }
    }
    if (!b) {
        for (unsigned i = 0; i < K_BAR; ++i) {
            if (!bars[i].count) {
                b = &bars[i];
                b->id = id;
                b->parties = parties;
                break;
            }
        }
    }
    if (!b || b->count >= K_BAR_WAITERS) {
        k_reply(node, req, KOS_EAGAIN, 0);
        return;
    }
    b->w[b->count].node = node;
    b->w[b->count].req = req;
    if (++b->count < b->parties) {
        return;
    }
    for (uint64_t i = 0; i < b->count; ++i) {
        k_reply(b->w[i].node, b->w[i].req, KOS_OK, 0);
    }
    b->count = 0;
}
