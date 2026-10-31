"""Resident-graph dispatch probe and B1/B3/B4/B5 model witnesses."""

import argparse
import itertools
import json
import pathlib
import struct
import sys
import time

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "ostpu"))

import klog
from boot import RG_DRAM_PRIV, RG_DRAM_SHARED, load, regions, tables
from image import UIMG, segments
from package import expected_output, mg_package, operand
from run_card import Card, arrays, load_node

from kohakuaccel.driver.device import rv64load
from kohakutpu.application.runtime import osabi as A
from kohakutpu.application.runtime.device import Device, Recorded
from kohakutpu.application.runtime.osgraph import (
    KDS_CPU,
    KDS_DE_STREAM,
    KDS_LZ_USER,
    KDS_STREAM,
    KDS_UNIT,
    KOS_LAND_SIZE,
    OSTPU_OFF_STAT,
    OSTPU_OFF_STATUS,
    STAT_FMT,
    STAT_NAMES,
    Graph,
    ResidentGraph,
    Task,
)


def mg_graph(ref, rounds, chain, unit=0):
    g = Graph()
    for _ in range(rounds):
        for op, sel, off in ((1, 0, 0), (1, 1, 0), (3, 0, 256)):
            word = op << 252 | 1 << 202 | sel << 201 | 1 << 176 | 40 << 168
            pred = (len(g.tasks) - 1,) if chain and g.tasks else ()
            g.add(
                Task(
                    KDS_UNIT,
                    unit=unit,
                    words=struct.unpack("<4Q", word.to_bytes(32, "little")),
                    pred=pred,
                    patches=((ref + off, 218, 34, 0), (ref + off, 63, 6, 34)),
                )
            )
    return g


def vc_fault_de_graph(mg_x, mg_y, mg_uid, mg_pa, vc_x, vc_y, vc_uid, mg_post=64):
    g = Graph(de_stream=True, de_fetch=False)
    mg_dst = (mg_y << 8) | mg_x
    vc_dst = (vc_y << 8) | vc_x
    vsetvl_zero_word = 0x18 << 27
    imem_w3 = 1 << 60
    run_w3 = 3 << 60
    # GEMM(gm=16, gn=16, nk=64, anchor=40) holds gemm_busy for 16,384 clk2x (8,192 clk1x) cycles;
    # the following DRAIN(n=1) waits for !gemm_busy before retiring (~8,200 cycles after issue).
    gemm_w2, gemm_w3 = 0x1040280000000000, 0x2000000000000010
    pa = mg_pa + 256
    drain_word = (
        3 << 252
        | ((pa & ((1 << 34) - 1)) << 218)
        | 1 << 202
        | 1 << 176
        | 40 << 168
        | (((pa >> 34) & 0x3F) << 63)
    )
    dw0, dw1, dw2, dw3 = struct.unpack("<4Q", drain_word.to_bytes(32, "little"))
    entries = [
        (A.KOS_DE_MB_DST, mg_dst),
        (A.KOS_DE_MB_ARG0, 0),
        (A.KOS_DE_MB_ARG1, 0),
        (A.KOS_DE_MB_ARG2, gemm_w2),
        (A.KOS_DE_MB_ARG3, gemm_w3),
        (A.KOS_DE_MB_GO, 1),
        (A.KOS_DE_MB_ARG0, dw0),
        (A.KOS_DE_MB_ARG1, dw1),
        (A.KOS_DE_MB_ARG2, dw2),
        (A.KOS_DE_MB_ARG3, dw3),
        (A.KOS_DE_MB_GO, 1),
        (A.KOS_DE_MB_DST, vc_dst),
        (A.KOS_DE_MB_ARG0, vsetvl_zero_word),
        (A.KOS_DE_MB_ARG1, 0),
        (A.KOS_DE_MB_ARG2, 0),
        (A.KOS_DE_MB_ARG3, imem_w3),
        (A.KOS_DE_MB_GO, 1),
        (A.KOS_DE_MB_ARG0, 0),
        (A.KOS_DE_MB_ARG3, run_w3),
        (A.KOS_DE_MB_GO, 1),
        (A.KOS_DE_WAIT_CODE, (vc_uid << 56) | 2),
        (A.KOS_DE_MB_DST, mg_dst),
        (A.KOS_DE_MB_ARG0, dw0),
        (A.KOS_DE_MB_ARG3, dw3),
        *((A.KOS_DE_MB_GO, 1) for _ in range(mg_post)),
        (A.KOS_DE_WAIT_CODE, (mg_uid << 56) | (2 + mg_post)),
    ]
    payload = bytearray()
    for k in range(0, len(entries), A.KOS_DE_GROUP_ENTRIES):
        chunk = entries[k : k + A.KOS_DE_GROUP_ENTRIES]
        codes = 0
        vals = [0] * A.KOS_DE_GROUP_ENTRIES
        for j, (c, v) in enumerate(chunk):
            codes |= (c & 0xFF) << (8 * j)
            vals[j] = v
        payload.extend(struct.pack("<4Q", codes, *vals))
    t = g.add(Task(KDS_DE_STREAM, words=(0, len(entries), 0, 0)))
    g.streams[t] = bytes(payload)
    return g


def mover_de_graph(pairs, after=()):
    g = Graph(de_stream=True, movers=True)
    state, _ = g._open_de(after)
    state["entries"].extend(
        (*pairs, (A.KOS_DE_WAIT_CODE, (A.KOS_DE_MOVER_SRC << 56) | 1))
    )
    state["mv_gos"] = 1
    g._sync_de(state)
    return g


def mover_copy_de_graph(src_pa, dst_pa, words=1, after=()):
    return mover_de_graph(
        A.mv_1d_entries(A.KOS_DE_MV_HDR, src_pa, dst_pa, words), after
    )


def refused(card, mailbox_pa, runtime, graph, status, detail, what):
    runtime.prepare(graph)
    try:
        runtime.run()
    except RuntimeError:
        pass
    else:
        raise AssertionError(f"{what} was not refused")
    st, _, det = struct.unpack("<q2Q", card.read(mailbox_pa + OSTPU_OFF_STATUS, 24))
    assert (st, det) == (status, detail & ((1 << 64) - 1)), (what, st, det)
    return st


MG_SOURCE = struct.pack(
    "<16Q", *[((i + 1) * 0x9E3779B97F4A7C15) & ((1 << 64) - 1) for i in range(16)]
)
ROWS = (8, 64, 512, 2048, 2050, 2056, 4096)
IN_OFF, OUT_OFF = 0x500, 0x540
ROWS_OFF, TABLE_OFF, GATH_OFF, SCAT_OFF = 0x600, 0x680, 0x700, 0x780


class Witness:
    """The card and resident runtime every model witness runs on."""

    def __init__(self, card, runtime, board, rows):
        self.card = card
        self.runtime = runtime
        self.board = board
        self.rows = rows
        self.mailbox_pa = runtime.mailbox
        self.base_pa = runtime.data_pa
        self.base_ref = runtime.data_ref
        self.cpu_fn = runtime.cpu_fn
        self.last_user_pa = runtime.tasks_pa - A.KDS_LZ_SLICE + 0x10000 - 32
        assert self.cpu_fn != 0, "ostpu_cpu_rw function pointer missing in mailbox"

    def fresh(self):
        """A new ResidentGraph on the still-running ostpu, after a refused run."""
        self.runtime = ResidentGraph(
            self.card, self.mailbox_pa, timeout=self.runtime.timeout
        )
        return self.runtime


def w_b1(w):
    """B1: a CPU task after KDS_DE_STREAM reads fresh DE output (not a stale L1 line)."""
    card, runtime, base_pa, base_ref, cpu_fn = (
        w.card,
        w.runtime,
        w.base_pa,
        w.base_ref,
        w.cpu_fn,
    )
    src_off, de_out_off, cpu_out_off = 0x400, 0x440, 0x480
    card.write(
        base_pa + src_off,
        struct.pack("<4Q", 0xCAFE0001, 0xCAFE0002, 0xCAFE0003, 0xCAFE0004),
    )
    card.write(base_pa + de_out_off, b"\x00" * 32)
    card.write(base_pa + cpu_out_off, b"\x00" * 32)
    g_b1 = Graph(de_stream=True, movers=True)
    # Task 0 (KDS_CPU): prime CPU L1 cache at de_out_off with stale 0xDEAD0000..0xDEAD0003.
    t0 = g_b1.add(
        Task(
            KDS_CPU,
            words=(cpu_fn, base_ref + de_out_off, 0, 0xDEAD0000),
        )
    )
    # Task 1 (KDS_DE_STREAM): hardware mover copies src_off (0xCAFE...) to de_out_off in DRAM.
    state, _ = g_b1._open_de((t0,))
    state["entries"].extend(
        (
            *A.mv_1d_entries(
                A.KOS_DE_MV_HDR, base_pa + src_off, base_pa + de_out_off, 1
            ),
            (A.KOS_DE_WAIT_CODE, (A.KOS_DE_MOVER_SRC << 56) | 1),
        )
    )
    state["mv_gos"] = 1
    (t1,) = g_b1._sync_de(state)
    # Task 2 (KDS_CPU): read de_out_off through CPU L1 cache, XOR with 0x1111, write to cpu_out_off.
    g_b1.add(
        Task(
            KDS_CPU,
            words=(
                cpu_fn,
                base_ref + cpu_out_off,
                base_ref + de_out_off,
                0x1111,
            ),
            pred=(t1,),
        )
    )
    runtime.prepare(g_b1)
    r_b1 = runtime.run()
    got_b1 = struct.unpack("<4Q", card.read(base_pa + cpu_out_off, 32))
    want_b1 = tuple((0xCAFE0001 + i) ^ 0x1111 for i in range(4))
    assert got_b1 == want_b1, f"B1 failed: got {got_b1!r}, want {want_b1!r}"
    return dict(cycles=r_b1["cycles"], output=got_b1)


def w_b3(w):
    """B3: the CPU writes cached input between retained KDS_DE_STREAM runs; the DE sees it."""
    card, runtime, base_pa, base_ref = w.card, w.runtime, w.base_pa, w.base_ref
    card.write(base_pa + OUT_OFF, b"\xa5" * 32)
    g_b3 = mover_copy_de_graph(base_pa + IN_OFF, base_pa + OUT_OFF, words=1)
    runtime.prepare(g_b3)
    runtime.run(cpu_write=(base_ref + IN_OFF, 0x11110000))
    got_b3_run0 = struct.unpack("<4Q", card.read(base_pa + OUT_OFF, 32))
    assert got_b3_run0 == tuple(0x11110000 + i for i in range(4)), got_b3_run0
    # Second (retained!) run: CPU writes 0x22220000..0x22220003 into cached kds_va(in_ref).
    runtime.run(cpu_write=(base_ref + IN_OFF, 0x22220000))
    got_b3_run1 = struct.unpack("<4Q", card.read(base_pa + OUT_OFF, 32))
    assert got_b3_run1 == tuple(0x22220000 + i for i in range(4)), got_b3_run1
    return dict(run0=got_b3_run0, run1=got_b3_run1)


def w_coherence(w):
    """A retained KDS_CPU task reads fresh host DRAM input and writes output the host reads."""
    card, runtime, base_pa, base_ref, cpu_fn = (
        w.card,
        w.runtime,
        w.base_pa,
        w.base_ref,
        w.cpu_fn,
    )
    cpu_in_off, cpu_ret_out_off = 0x580, 0x5C0
    card.write(base_pa + cpu_ret_out_off, b"\xa5" * 32)
    g_cpu_ret = Graph()
    g_cpu_ret.add(
        Task(
            KDS_CPU,
            words=(
                cpu_fn,
                base_ref + cpu_ret_out_off,
                base_ref + cpu_in_off,
                0x5555,
            ),
        )
    )
    runtime.prepare(g_cpu_ret)
    card.write(
        base_pa + cpu_in_off,
        struct.pack("<4Q", 0xAAAA0000, 0xAAAA0001, 0xAAAA0002, 0xAAAA0003),
    )
    runtime.run()
    got_cpu_run0 = struct.unpack("<4Q", card.read(base_pa + cpu_ret_out_off, 32))
    assert got_cpu_run0 == tuple(
        (0xAAAA0000 + i) ^ 0x5555 for i in range(4)
    ), got_cpu_run0
    card.write(
        base_pa + cpu_in_off,
        struct.pack("<4Q", 0xBBBB0000, 0xBBBB0001, 0xBBBB0002, 0xBBBB0003),
    )
    runtime.run()
    got_cpu_run1 = struct.unpack("<4Q", card.read(base_pa + cpu_ret_out_off, 32))
    assert got_cpu_run1 == tuple(
        (0xBBBB0000 + i) ^ 0x5555 for i in range(4)
    ), got_cpu_run1
    return dict(run0=got_cpu_run0, run1=got_cpu_run1)


def w_b4(w):
    """B4: a KDS_UNIT part after KDS_DE_STREAM retires on the model."""
    card, runtime, base_pa, base_ref = w.card, w.runtime, w.base_pa, w.base_ref
    card.write(base_pa, MG_SOURCE)
    card.write(base_pa + 128, b"\xa5" * 384)
    g_b4 = mg_graph(base_ref, rounds=2, chain=False, unit=0)
    runtime.prepare(g_b4)
    r_b4 = runtime.run()
    got_b4 = card.read(base_pa + 128, 384)
    changed_b4 = [
        i for i in range(0, len(got_b4), 8) if got_b4[i : i + 8] != b"\xa5" * 8
    ]
    assert changed_b4 == [128, 136, 144, 152], changed_b4
    return dict(cycles=r_b4["cycles"], changed_offsets=changed_b4)


def w_extent(w):
    """Item 3: a DE-stream mover extent past the last user frame is refused with
    KOS_EFAULT (-7) and the node stays alive."""
    card, runtime, base_pa = w.card, w.runtime, w.base_pa
    g_ext_bad = mover_copy_de_graph(w.last_user_pa, base_pa + OUT_OFF, words=2)
    runtime.prepare(g_ext_bad)
    try:
        runtime.run()
    except RuntimeError:
        pass
    else:
        raise AssertionError("extent past user frame was not refused")
    st_ext = struct.unpack("<q", card.read(w.mailbox_pa + OSTPU_OFF_STATUS, 8))[0]
    assert st_ext == -7, f"expected KOS_EFAULT (-7) for extent past frame, got {st_ext}"
    w.fresh()
    return dict(status=st_ext)


def w_mover_fault(w):
    """Item 2: a DE-stream move the mover faults at GO (MODE_TRANSPOSE = 1 -> F_MODE)
    returns KOS_EFAULT (-7), then the next DE copy succeeds."""
    card, base_pa = w.card, w.base_pa
    mv_bad_pairs = list(
        A.mv_1d_entries(A.KOS_DE_MV_HDR, base_pa + IN_OFF, base_pa + OUT_OFF, 1)
    )
    mv_bad_pairs[-1] = (A.KOS_DE_MV_CTRL, A.KOS_MV_CTRL_1D | 1)
    st_mv = refused(
        card,
        w.mailbox_pa,
        w.runtime,
        mover_de_graph(mv_bad_pairs),
        -7,
        -7,
        "mover fault",
    )
    runtime = w.fresh()
    card.write(
        base_pa + IN_OFF,
        struct.pack("<4Q", 0x44440000, 0x44440001, 0x44440002, 0x44440003),
    )
    card.write(base_pa + OUT_OFF, b"\xa5" * 32)
    runtime.prepare(mover_copy_de_graph(base_pa + IN_OFF, base_pa + OUT_OFF))
    runtime.run()
    got_mv_ok = struct.unpack("<4Q", card.read(base_pa + OUT_OFF, 32))
    assert got_mv_ok == tuple(0x44440000 + i for i in range(4)), got_mv_ok
    return dict(fault_status=st_mv, output=got_mv_ok)


def w_negative_stride(w):
    """Item 3: a negative-stride source ending at the last user word spans
    [end - 64, end); the kernel admits it and the mover reads it backwards."""
    card, runtime, base_pa, last_user_pa = w.card, w.runtime, w.base_pa, w.last_user_pa
    neg_pairs = list(
        A.mv_1d_entries(A.KOS_DE_MV_HDR, last_user_pa, base_pa + OUT_OFF, 2)
    )
    stride_unit = A.KOS_MV_DIM_STRIDE // 32
    neg_pairs[1] = (
        A.KOS_DE_MV_DIM,
        (2 << A.KOS_MV_DIM_WORDS_SHIFT) | (-32 & 0xFFFFFFFF) * stride_unit,
    )
    card.write(base_pa + OUT_OFF, b"\xa5" * 64)
    runtime.prepare(mover_de_graph(neg_pairs))
    runtime.run()
    got_neg = card.read(base_pa + OUT_OFF, 64)
    want_neg = card.read(last_user_pa, 32) + card.read(last_user_pa - 32, 32)
    assert got_neg == want_neg, (got_neg.hex(), want_neg.hex())
    return dict(output_hex=got_neg.hex())


INDEXED = A.KOS_DE_MV_BASE + (0x50 >> 3)
INDEXED_CTRL = A.KOS_MV_CTRL_1D & ~7


def source_rows(w, count):
    """Writes `count` distinct 32-byte rows at ROWS_OFF and returns them."""
    rows = [
        struct.pack("<4Q", *(0x5A5A0000 + 16 * r + i for i in range(4)))
        for r in range(count)
    ]
    w.card.write(w.base_pa + ROWS_OFF, b"".join(rows))
    return rows


def w_gather_scatter(w):
    """GATHER then SCATTER through the kernel's index copy: dst row k = src row idx[k]."""
    card, runtime, base_pa = w.card, w.runtime, w.base_pa
    rows = source_rows(w, 4)
    card.write(base_pa + TABLE_OFF, struct.pack("<8I", 2, 0, 3, 1, 3, 2, 1, 0))
    card.write(base_pa + GATH_OFF, b"\xa5" * 128)
    card.write(base_pa + SCAT_OFF, b"\xa5" * 128)

    def indexed_move(src, dst, table, mode):
        pairs = list(A.mv_1d_entries(A.KOS_DE_MV_HDR, src, dst, 4))
        pairs[-1:] = [
            (A.KOS_DE_MV_INDEX, (4 << 40) | table),
            (INDEXED, (1 << 32) | 32),
            (A.KOS_DE_MV_CTRL, INDEXED_CTRL | mode),
        ]
        return mover_de_graph(pairs)

    runtime.prepare(
        indexed_move(base_pa + ROWS_OFF, base_pa + GATH_OFF, base_pa + TABLE_OFF, 2)
    )
    runtime.run()
    got_gather = card.read(base_pa + GATH_OFF, 128)
    assert got_gather == b"".join(rows[i] for i in (2, 0, 3, 1)), got_gather.hex()
    runtime.prepare(
        indexed_move(
            base_pa + GATH_OFF, base_pa + SCAT_OFF, base_pa + TABLE_OFF + 16, 6
        )
    )
    runtime.run()
    got_scatter = card.read(base_pa + SCAT_OFF, 128)
    want_scatter = [None] * 4
    for k, i in enumerate((3, 2, 1, 0)):
        want_scatter[i] = got_gather[32 * k : 32 * k + 32]
    assert got_scatter == b"".join(want_scatter), got_scatter.hex()
    return dict(gather_hex=got_gather.hex(), scatter_hex=got_scatter.hex())


def w_rows(w):
    """Rows past 2048 and the cost per row, for each row count in `w.rows`. An
    8-entry vector, a stride-0 destination: the word left is row n-1's, which
    reads entry (n-1) mod 2048 (the kernel's zeros past 8). The same n-word copy,
    both sides stride 0, is the mover's cost without the index."""
    card, runtime, base_pa = w.card, w.runtime, w.base_pa
    src_rows = source_rows(w, 8)
    card.write(base_pa + TABLE_OFF, struct.pack("<8I", *range(8)))

    def wide(n, mode):
        pairs = list(
            A.mv_1d_entries(A.KOS_DE_MV_HDR, base_pa + ROWS_OFF, base_pa + GATH_OFF, n)
        )
        pairs[1] = (A.KOS_DE_MV_DIM, n << A.KOS_MV_DIM_WORDS_SHIFT)
        pairs[6] = (A.KOS_DE_MV_DIM, (n << A.KOS_MV_DIM_WORDS_SHIFT) | 1)
        if mode == 0:
            return mover_de_graph(pairs)
        pairs[-1:] = [
            (A.KOS_DE_MV_INDEX, (8 << 40) | (base_pa + TABLE_OFF)),
            (INDEXED, (1 << 32) | 32),
            (A.KOS_DE_MV_CTRL, INDEXED_CTRL | mode),
        ]
        return mover_de_graph(pairs)

    per_rows = {}
    for n in w.rows:
        cyc = {}
        for name, mode in (("gather", 2), ("copy", 0)):
            card.write(base_pa + GATH_OFF, b"\xa5" * 32)
            runtime.prepare(wide(n, mode))
            cyc[name] = runtime.run()["cycles"]
            got = card.read(base_pa + GATH_OFF, 32)
            e = (n - 1) % 2048
            want = src_rows[e if e < 8 else 0] if mode else src_rows[0]
            assert got == want, (n, name, got.hex(), want.hex())
        per_rows[n] = cyc
    ns = sorted(per_rows)
    slope = {
        f"{a}-{b}": {
            k: (per_rows[b][k] - per_rows[a][k]) / (b - a) for k in ("gather", "copy")
        }
        for a, b in itertools.pairwise(ns)
    }
    return dict(cycles=per_rows, cycles_per_row=slope)


def w_mem_wr_dst(w):
    """A typed DST other than the fetch's MEM_RD_REQ (here MEM_WR_REQ, type 1) is
    refused with KOS_EINVAL (-1) before any GO."""
    wr_dst = A.KOS_DE_FETCH_TYPED | (1 << 20)
    g_wr = Graph(de_stream=True, de_fetch=False)
    t_wr = g_wr.add(Task(KDS_DE_STREAM, words=(0, 2, 0, 0)))
    g_wr.streams[t_wr] = struct.pack(
        "<4Q", A.KOS_DE_MB_DST | (A.KOS_DE_MB_GO << 8), wr_dst, 1, 0
    )
    st_wr = refused(w.card, w.mailbox_pa, w.runtime, g_wr, -1, -1, "MEM_WR_REQ DST")
    w.fresh()
    return dict(status=st_wr)


def w_b5(w):
    """N1 + B5: two units in flight (MG busy + VC faults) in a >64-entry
    KDS_DE_STREAM reaching room == 0 on an MG GO: the error is returned, both
    units drain, and the node runs a DE stream and KDS_GRAPHS + 1 unit graphs after."""
    card, runtime, board, base_pa, base_ref = (
        w.card,
        w.runtime,
        w.board,
        w.base_pa,
        w.base_ref,
    )
    mailbox_pa = w.mailbox_pa
    card.write(base_pa, MG_SOURCE)
    mg_idx, mg_u = next(
        (i, u) for i, u in enumerate(board["units"]) if u["type"] == "MG"
    )
    vc_idx, vc_u = next(
        (i, u) for i, u in enumerate(board["units"]) if u["type"] == "VC"
    )
    g_b5 = vc_fault_de_graph(
        mg_u["x"],
        mg_u["y"],
        mg_idx,
        base_pa,
        vc_u["x"],
        vc_u["y"],
        vc_idx,
        mg_post=64,
    )
    runtime.prepare(g_b5)
    try:
        runtime.run()
    except RuntimeError as exc:
        fault_msg = str(exc)
    else:
        raise AssertionError("injected VC unit fault in KDS_DE_STREAM did not fail")
    st_b5, failed_b5, detail_b5, _cyc_b5, comp_b5, _, _, _ = struct.unpack(
        "<q7Q", card.read(mailbox_pa + OSTPU_OFF_STATUS, 64)
    )
    assert st_b5 == -7 and comp_b5 == 0, (st_b5, comp_b5, fault_msg)

    # Re-attach a fresh ResidentGraph to the still-running ostpu and run KDS_DE_STREAM + KDS_GRAPHS+1 (5) KDS_UNIT graphs.
    recovered = w.fresh()
    card.write(base_pa + OUT_OFF, b"\xa5" * 32)
    recovered.prepare(mover_copy_de_graph(base_pa + IN_OFF, base_pa + OUT_OFF))
    recovered.run(cpu_write=(base_ref + IN_OFF, 0x33330000))
    got_after_de = struct.unpack("<4Q", card.read(base_pa + OUT_OFF, 32))
    assert got_after_de == tuple(0x33330000 + i for i in range(4)), got_after_de
    for _ in range(5):
        card.write(base_pa + 128, b"\xa5" * 384)
        recovered.prepare(mg_graph(base_ref, rounds=1, chain=False, unit=0))
        r_after_unit = recovered.run(timeout_cycles=500_000)
        got_after = card.read(base_pa + 128, 384)
        assert [
            i
            for i in range(0, len(got_after), 8)
            if got_after[i : i + 8] != b"\xa5" * 8
        ] == [
            128,
            136,
            144,
            152,
        ]
    return dict(
        error=fault_msg,
        status=st_b5,
        failed_task=failed_b5,
        detail=detail_b5,
        post_fault_unit_cycles=r_after_unit["cycles"],
        post_fault_de_output=got_after_de,
    )


WITNESSES = {
    "b1_cpu_after_de_stream": w_b1,
    "b3_cpu_write_between_retained_de": w_b3,
    "coherence_retained_cpu_read_by_host": w_coherence,
    "b4_kds_unit_after_de_stream": w_b4,
    "extent_past_frame_refused": w_extent,
    "mover_fault_then_de_copy": w_mover_fault,
    "negative_stride_extent": w_negative_stride,
    "gather_scatter_through_kernel_index": w_gather_scatter,
    "gather_rows_past_2048_and_cost": w_rows,
    "mem_wr_dst_refused": w_mem_wr_dst,
    "b5_unit_fault_in_de_stream": w_b5,
}


def run_model_witnesses(card, runtime, board, names, rows):
    """Runs the named witnesses in WITNESSES order on one booted node."""
    w = Witness(card, runtime, board, rows)
    out = {}
    for name, fn in WITNESSES.items():
        if name in names:
            started = time.monotonic()
            print(f"witness {name} ...", flush=True)
            out[name] = dict(passed=True, **fn(w))
            out[name]["seconds"] = time.monotonic() - started
            print(f"witness {name} passed {out[name]['seconds']:.1f} s", flush=True)
    w.runtime.close()
    return out


def measure(a):
    board = load(a.board)
    if board["nodes"] != 1:
        raise ValueError("probe uses one node")
    board["clock_hz"] = 200_000_000
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    image = pathlib.Path(a.image)
    parts = segments(image)
    card = Card(board, a.model, out / "model.log")
    started = time.monotonic()
    try:
        base, size = regions(board, 0)[0][:2]
        if a.jtag:
            rv64load.LoadWindow(
                card.t, card.m["ctrl"][0] + rv64load.WINDOW_OFFSET
            ).stop()
            for pa, count, kind, _owner in regions(board, 0):
                if kind in (RG_DRAM_PRIV, RG_DRAM_SHARED):
                    card.write(pa, bytes(count))
        flags = (" -e" if a.engine else "") + (" -u" if a.uncached else "")
        capacity = (
            32 if a.witness else (3 * a.rounds if not a.adapter else 16 * a.rounds)
        )
        retain_arg = " 1" if a.witness else ""
        command = f"ostpu {capacity} 4096{retain_arg}{flags}"
        table = tables(board, 0, 0, command)[0]
        imem, spad, dram = arrays(image, table)
        for pa, data in parts:
            if pa >= UIMG:
                card.write(base + pa - UIMG, data)
        for pa, data in dram:
            card.write(pa, data)
        win = load_node(card, 0, 0, imem, spad, "burn" if a.model else "axi")
        win.boot()
        print(f"booted {time.monotonic() - started:.1f} s", flush=True)
        mailbox_pa = base + size - KOS_LAND_SIZE + KDS_LZ_USER
        runtime = ResidentGraph(card, mailbox_pa, timeout=a.timeout)
        if a.witness:
            try:
                wres = run_model_witnesses(card, runtime, board, a.witness, a.rows)
            except Exception as exc:
                win.stop()
                log_info = klog.parse(card.read(base + klog.offset(size), 16384))
                mb_words = struct.unpack("<24Q", card.read(mailbox_pa, 192))
                raise RuntimeError(
                    f"witness failed: {exc!r} status={win.status()!r} halt_pc={card.t.read64(win.base + rv64load.R_HALTPC):#x} log={log_info!r} mb={mb_words!r}"
                ) from exc
            wres["run_seconds"] = time.monotonic() - started
            return wres
        if a.bad_span:
            graph = Graph()
            graph.add(Task(KDS_STREAM, words=(size - 32, 8, 0, 0)))
            runtime.prepare(graph)
            try:
                runtime.run()
            except RuntimeError as exc:
                failure = str(exc)
            else:
                raise AssertionError("cross-span stream was accepted")
            status, _task, _detail, cycles, completed, _, _, _repeat = struct.unpack(
                "<q7Q", card.read(runtime.mailbox + OSTPU_OFF_STATUS, 64)
            )
            stats = dict(
                zip(
                    STAT_NAMES,
                    struct.unpack(
                        STAT_FMT,
                        card.read(
                            runtime.mailbox + OSTPU_OFF_STAT, 8 * len(STAT_NAMES)
                        ),
                    ),
                    strict=True,
                )
            )
            assert status == -1 and completed == 0 and stats["unit_batches"] == 0
            assert runtime.poisoned
            return dict(
                expected_failure=failure,
                status=status,
                cycles=cycles,
                stats=stats,
                run_seconds=time.monotonic() - started,
            )
        graph = mg_graph(runtime.data_ref, a.rounds, a.chain)
        if not a.adapter:
            runtime.prepare(graph)
        card.write(runtime.data_pa, MG_SOURCE)
        card.write(runtime.data_pa + 128, b"\xa5" * 384)
        if a.adapter:
            units = {
                (u["mesh"], u["x"], u["y"]): i for i, u in enumerate(board["units"])
            }
            dev = Device(
                "os", board=board["card"]["board"], os_session=runtime, os_units=units
            )
            raw = mg_package(
                dev.card.machine_json,
                runtime.data_pa,
                a.rounds,
                arithmetic=a.arithmetic,
            )
            log = Recorded([("run", {0: (raw, ())})], [], dev)
            dev.prepare_recorded(log)
            replays = []
            for value in (1, 2):
                if a.arithmetic:
                    card.write(runtime.data_pa, operand(value))
                card.write(runtime.data_pa + 128, b"\xa5" * 384)
                dev.replay(log)
                snapshot = dict(dev.card.last_result)
                got = card.read(runtime.data_pa + 128, 384)
                snapshot["output_hex"] = got.hex()
                if a.arithmetic:
                    assert (
                        got == b"\xa5" * 128 + expected_output(value) + b"\xa5" * 224
                    ), got.hex()
                replays.append(snapshot)
            first, result = replays
            result["first_replay"] = first
            result["adapter"] = True
            log.close()
        else:
            result = runtime.run(a.repeats)
        got = card.read(runtime.data_pa + 128, 384)
        changed = [i for i in range(0, len(got), 8) if got[i : i + 8] != b"\xa5" * 8]
        assert changed == [128, 136, 144, 152], changed
        if a.adapter:
            dev.close()
        else:
            runtime.close()
        result.update(
            board=a.board,
            engine=a.engine,
            uncached=a.uncached,
            chain=a.chain,
            rounds=a.rounds,
            repeats=a.repeats,
            run_seconds=time.monotonic() - started,
            instructions_per_step=(4 if a.arithmetic else 3) * a.rounds,
            cycles_per_instruction=result["cycles"]
            / ((4 if a.arithmetic else 3) * a.rounds * a.repeats),
            reference_200mhz_seconds=result["cycles"] / 200_000_000,
            output_hex=got.hex(),
            idle_cycles_between_launches=None,
            workload=(
                "4x32x4 exact matmul"
                if a.arithmetic
                else "MG dispatch microcase (no matmul arithmetic)"
            ),
            segment=dev.card.segment if a.adapter else 1,
            compact=dev.card.compact if a.adapter else False,
        )
        return result
    finally:
        card.t.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--board", default="card_v9t6s_1n")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--model")
    source.add_argument("--jtag", action="store_true")
    p.add_argument("--image", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--engine", type=int, choices=(0, 1), default=1)
    p.add_argument("--uncached", type=int, choices=(0, 1), default=0)
    p.add_argument("--chain", action="store_true")
    p.add_argument("--arithmetic", action="store_true")
    p.add_argument("--bad-span", action="store_true")
    p.add_argument(
        "--witness",
        nargs="?",
        const="all",
        help="model witnesses to run: 'all' or a comma list of " + ", ".join(WITNESSES),
    )
    p.add_argument(
        "--rows",
        default=",".join(map(str, ROWS)),
        help="row counts gather_rows_past_2048_and_cost measures",
    )
    p.add_argument("--adapter", action="store_true")
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--timeout", type=float, default=120)
    a = p.parse_args()
    if a.arithmetic and not a.adapter:
        p.error("arithmetic uses the package adapter")
    if a.adapter and (a.chain or a.repeats != 1):
        p.error("adapter probe uses two single-step replays without chain edges")
    if a.rounds < 1 or a.repeats < 1:
        p.error("rounds and repeats must be positive")
    if a.witness is not None:
        a.witness = set(WITNESSES) if a.witness == "all" else set(a.witness.split(","))
        if a.witness - set(WITNESSES):
            p.error(f"unknown witness {sorted(a.witness - set(WITNESSES))}")
    a.rows = tuple(int(n) for n in a.rows.split(","))
    result = measure(a)
    (pathlib.Path(a.out) / "result.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
