"""Resident KDS graphs on an already booted KohakuAccel OS node.

Preparation supports DISPATCH/AWAIT/BARRIER/END and optional synchronous MOVER/MWAIT.
Other operations refuse; this is not an interpreter for arbitrary firmware packages.
"""

import hashlib
import struct
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.split import BoundGraphArtifact, SlotLeases

C_ABI = A.C_ABI
MAGIC = A.OSTPU_MAGIC
TASK_BYTES = A.OSTPU_TASK_BYTES
KDS_NOP = A.KDS_NOP
KDS_UNIT = A.KDS_UNIT
KDS_MOVE = A.KDS_MOVE
KDS_CPU = A.KDS_CPU
KDS_STREAM = A.KDS_STREAM
KDS_STAMP = A.KDS_STAMP
KDS_DE_STREAM = A.KDS_DE_STREAM
KDS_LAUNCH_MARK = A.KDS_LAUNCH_MARK
KDS_MAXPRED = A.KDS_MAXPRED
KDS_MAXPATCH = A.KDS_MAXPATCH
KDS_MAXTASKS = A.KDS_MAXTASKS
KOS_DE_QUEUE_DEPTH = A.KOS_DE_QUEUE_DEPTH
KOS_DE_MAX_ENTRIES = A.KOS_DE_MAX_ENTRIES
KOS_DE_CODE_MASK = A.KOS_DE_CODE_MASK
KOS_DE_RELOC_FETCH = A.KOS_DE_RELOC_FETCH
KOS_DE_RELOC_MOVE = A.KOS_DE_RELOC_MOVE
KOS_DE_FETCH_SHIFT = A.KOS_DE_FETCH_SHIFT
KOS_DE_MOVE_SHIFT = A.KOS_DE_MOVE_SHIFT
KOS_DE_FETCH_CODE = A.KOS_DE_FETCH_CODE
KOS_DE_MOVE_CODE = A.KOS_DE_MOVE_CODE
KOS_DE_WAIT_CODE = A.KOS_DE_WAIT_CODE
KOS_DE_CHECK_ZERO = A.KOS_DE_CHECK_ZERO
KOS_DE_MAX_UNITS = A.KOS_DE_MAX_UNITS
KOS_DE_MOVER_SRC = A.KOS_DE_MOVER_SRC
KOS_DE_CTR_MASK = A.KOS_DE_CTR_MASK
KOS_MOVE_MAX_WORDS = A.KOS_MOVE_MAX_WORDS
KDS_LZ_USER = A.KDS_LZ_USER
KOS_LAND_SIZE = A.KOS_LAND_SIZE
KOS_LRING_OFF = A.KOS_LRING_OFF
KOS_LRING_SLOTS = A.KOS_LRING_SLOTS
KOS_LRING_STEP_SLOT = A.KOS_LRING_STEP_SLOT
KOS_LRING_SCRATCH = A.KOS_LRING_SCRATCH
KOS_LRING_MAX_LAUNCHES = A.KOS_LRING_MAX_LAUNCHES
KOS_LRING_REC_BYTES = A.KOS_LRING_REC_BYTES
OSTPU_OFF_READY = A.OSTPU_OFF_READY
OSTPU_OFF_REQ = A.OSTPU_OFF_REQ
OSTPU_REQ_EPOCH_WORD = A.OSTPU_REQ_EPOCH_WORD
OSTPU_OFF_CMD = A.OSTPU_OFF_CMD
OSTPU_OFF_DONE = A.OSTPU_OFF_DONE
OSTPU_OFF_STATUS = A.OSTPU_OFF_STATUS
OSTPU_OFF_CAP = A.OSTPU_OFF_CAP
OSTPU_OFF_STAT = A.OSTPU_OFF_STAT
OSTPU_OP_RUN = A.OSTPU_OP_RUN
OSTPU_OP_STOP = A.OSTPU_OP_STOP
STAT_NAMES = A.STAT_NAMES
STAT_FMT = A.STAT_FMT
LAUNCH_REC_FIELDS = A.LAUNCH_REC_FIELDS
LAUNCH_REC_FMT = A.LAUNCH_REC_FMT
#: Xache picks a write burst's home from its first address: a 256-byte MAG burst off
#: this line can cross a 16 KB interleave boundary, its tail landing 48 KB higher.
DATA_ALIGN = 256


def task_capacity(tasks):
    """`ostpu`'s first argument for `tasks` records: its data arena follows them."""
    return -(-tasks * TASK_BYTES // DATA_ALIGN) * DATA_ALIGN // TASK_BYTES


def mover_code(mreg):
    """The DE stream code that writes mover register byte offset `mreg`."""
    if mreg & 7 or mreg >> 3 > A.KOS_DE_MV_END - A.KOS_DE_MV_BASE:
        raise ValueError(f"{mreg:#x} is not a mover register offset")
    return A.KOS_DE_MV_BASE + (mreg >> 3)


@dataclass(frozen=True)
class Task:
    kind: int
    unit: int = 0
    node: int = 0
    words: tuple = (0, 0, 0, 0)
    pred: tuple = ()
    patches: tuple = ()

    def pack(self):
        if len(self.pred) > A.KDS_MAXPRED or len(self.patches) > A.KDS_MAXPATCH:
            raise ValueError("KDS record exceeds predecessor/patch capacity")
        out = bytearray(A.OSTPU_TASK_BYTES)
        struct.pack_into(
            "<4BHH",
            out,
            A.KDS_TASK_OFF_HEADER,
            self.kind,
            self.node,
            len(self.pred),
            len(self.patches),
            self.unit,
            0,
        )
        struct.pack_into(
            f"<{A.KDS_MAXPRED}H",
            out,
            A.KDS_TASK_OFF_PRED,
            *self.pred,
            *([0] * (A.KDS_MAXPRED - len(self.pred))),
        )
        struct.pack_into("<4Q", out, A.KDS_TASK_OFF_WORDS, *self.words)
        for i, (ref, lo, width, shift) in enumerate(self.patches):
            struct.pack_into(
                "<QHBBI",
                out,
                A.KDS_TASK_OFF_PATCH + A.KDS_PATCH_BYTES * i,
                ref,
                lo,
                width,
                shift,
                0,
            )
        return bytes(out)


class Graph:
    def __init__(
        self,
        batch=1,
        compact=False,
        movers=False,
        prune=False,
        de_stream=False,
        de_fetch=True,
        de_fuse=False,
        wait_window=False,
    ):
        if not 1 <= batch <= 32:
            raise ValueError("OS instruction segment size is 1..32")
        self.tasks = []
        self.batch, self.compact, self.movers = batch, compact, movers
        self.de_stream = de_stream
        self.de_fetch = de_fetch
        self.de_fuse = de_fuse
        self.wait_window = wait_window
        self._de: dict[str, Any] | None = None
        self.streams = {}
        self.prune = prune
        self.ancestors = []
        self._persistent = False
        self._node_ids = {}

    def de_break(self, frontier):
        """End the open descriptor stream: the next package starts its own task."""
        return (self.add(Task(A.KDS_NOP, pred=tuple(frontier))),)

    def _open_de(self, after):
        after = tuple(after)
        if self.de_fuse and self._de is not None and after == (self._de["task"],):
            return self._de, True
        kind = A.KDS_DE_FEED if self._persistent else A.KDS_DE_STREAM
        t = self.add(Task(kind, words=(0, 0, 0, 0), pred=after))
        state: dict[str, Any] = dict(
            task=t,
            entries=[],
            blobs=bytearray(),
            mb=[None] * 5,
            wire=[0] * 5,
            sent={},
            exp={},
            waited={},
            mv_gos=0,
        )
        self._de = state
        return state, False

    def _sync_de(self, state):
        t = state["task"]
        entries = state["entries"]
        blobs = state["blobs"]
        if not entries:
            self.tasks[t] = replace(self.tasks[t], kind=A.KDS_NOP)
            self.streams.pop(t, None)
            return (t,)
        cmd_bytes = A.de_cmd_bytes(len(entries))
        group_n = A.KOS_DE_GROUP_ENTRIES
        payload = bytearray()
        for k in range(0, len(entries), group_n):
            chunk = entries[k : k + group_n]
            codes = 0
            vals = [0] * group_n
            for j, (c, v) in enumerate(chunk):
                if c & A.KOS_DE_RELOC_FETCH:
                    v = (v + (cmd_bytes << A.KOS_DE_FETCH_SHIFT)) & ((1 << 64) - 1)
                elif c & A.KOS_DE_RELOC_MOVE:
                    v = (v + (cmd_bytes << A.KOS_DE_MOVE_SHIFT)) & ((1 << 64) - 1)
                codes |= (c & 0xFF) << (8 * j)
                vals[j] = v
            payload.extend(struct.pack("<4Q", codes, *vals))
        payload.extend(blobs)
        self.tasks[t] = replace(self.tasks[t], words=(0, len(entries), len(blobs), 0))
        self.streams[t] = bytes(payload)
        return (t,)

    def move_de(self, data, dst_pa, after=()):
        if not self.de_stream:
            raise ValueError("move_de requires de_stream=True")
        frontier = tuple(after)
        max_chunk = A.KOS_MOVE_MAX_WORDS * 32
        for at in range(0, len(data), max_chunk):
            chunk = data[at : at + max_chunk]
            words = len(chunk) // 32
            state, reused = self._open_de(frontier)
            if reused and len(state["entries"]) + 12 > A.KOS_DE_MAX_ENTRIES:
                self._de = None
                state, _ = self._open_de(frontier)
            entries = state["entries"]
            blobs = state["blobs"]
            blob_off = len(blobs)
            blobs.extend(chunk)
            dst = dst_pa + at
            entries.extend(A.mv_1d_entries(A.KOS_DE_MOVE_CODE, blob_off, dst, words))
            state["mv_gos"] += 1
            want = state["mv_gos"]
            entries.append(
                (
                    A.KOS_DE_WAIT_CODE,
                    (A.KOS_DE_MOVER_SRC << 56) | (want & A.KOS_DE_CTR_MASK),
                )
            )
            state["waited"][A.KOS_DE_MOVER_SRC] = want
            frontier = self._sync_de(state)
        return frontier

    def add(self, task):
        index = len(self.tasks)
        self.tasks.append(task)
        if self.prune:
            ancestors = 0
            for pred in task.pred:
                ancestors |= self.ancestors[pred] | 1 << pred
            self.ancestors.append(ancestors)
        return index

    def join(self, predecessors):
        pred = list(dict.fromkeys(predecessors))
        if self.prune:
            covered = 0
            for p in pred:
                covered |= self.ancestors[p]
            pred = [p for p in pred if not covered >> p & 1]
        if self.compact and len(pred) <= 1:
            return tuple(pred)
        while len(pred) > A.KDS_MAXPRED:
            pred = [
                self.add(Task(A.KDS_NOP, pred=tuple(pred[i : i + A.KDS_MAXPRED])))
                for i in range(0, len(pred), A.KDS_MAXPRED)
            ]
        return () if not pred else (self.add(Task(A.KDS_NOP, pred=tuple(pred))),)

    @classmethod
    def package_parts(
        cls, artifact, units, *, node_ids=None, unit_types=None, **options
    ):
        """Translate bound node parts without reassigning events or addresses.

        `node_ids` maps physical sysnode/mesh IDs to the OS boot node IDs.
        Each result is submitted to that node's resident, never to CPU0 as a
        remote task graph. Unsupported package operations refuse before upload.
        """
        nodes = {part.node for part in artifact.parts}
        node_ids = {node: node for node in nodes} if node_ids is None else node_ids
        result = {}
        for part in artifact.parts:
            h = struct.unpack_from(A.PKG_HDR_FMT, part.package)
            for i in range((h[A.KA_PH_COUNTS2] >> 32) & 65535):
                word = struct.unpack_from(
                    "<Q", part.package, h[A.KA_PH_OFF_UNIT] + 16 * i
                )[0]
                if (word >> 16) & 255 != part.node:
                    raise ValueError("a node part must use its own CPU's units")
            graph = cls(**{"de_stream": True, "movers": True, **options})
            graph._persistent = True
            graph._node_ids = node_ids
            local_units = {
                coord: index
                for index, (coord, _) in enumerate(
                    sorted(
                        (
                            (coord, uid)
                            for coord, uid in units.items()
                            if coord[0] == part.node
                        ),
                        key=lambda item: item[1],
                    )
                )
            }
            frontier = graph.package(
                part.package,
                (),
                local_units if graph.de_stream else units,
                unit_types=unit_types,
            )
            graph.add(Task(A.KDS_DE_FINISH, pred=frontier))
            graph.tasks = [replace(t, node=node_ids[part.node]) for t in graph.tasks]
            if not graph.tasks:
                graph.add(Task(A.KDS_NOP, node=node_ids[part.node]))
            result[part.node] = graph
        return result

    def package(self, raw, refs, units, after=(), unit_types=None):
        """Append one captured package; refs are KDS buffer references.

        units maps (mesh, x, y) to the OS boot table's unit index. Single-word
        relocations remain OS patches. Segment payloads must already be bound.
        """
        h = struct.unpack_from(A.PKG_HDR_FMT, raw)
        if h[A.KA_PH_MAGIC] != (
            struct.calcsize(A.PKG_HDR_FMT) << 48
            | A.KA_PKG_VERSION << 32
            | A.KA_PKG_MAGIC
        ):
            raise ValueError("expected KAPK v2")
        nstep = (h[A.KA_PH_COUNTS2] >> 16) & 0xFFFF
        nunit = (h[A.KA_PH_COUNTS2] >> 32) & 0xFFFF
        unit_ids = []
        for i in range(nunit):
            u = struct.unpack_from("<Q", raw, h[A.KA_PH_OFF_UNIT] + 16 * i)[0]
            coord = ((u >> 16) & 255, u & 255, (u >> 8) & 255)
            if unit_types is not None and unit_types.get(coord) != (u >> 32) & 65535:
                raise ValueError(
                    f"package unit type differs from OS topology at {coord}"
                )
            unit_ids.append(units[coord])
        reloc = {}
        for i in range(h[A.KA_PH_COUNTS] >> 32):
            r, addend = struct.unpack_from("<Qq", raw, h[A.KA_PH_OFF_REL] + 16 * i)
            reloc.setdefault(r & 0xFFFFFFFF, []).append(
                (
                    refs[r >> 56] + addend,
                    (r >> 32) & 255,
                    (r >> 40) & 255,
                    (r >> 48) & 255,
                )
            )
        if self.batch > 1 and reloc:
            raise ValueError("instruction segments require host-bound payloads")
        if self.de_stream:
            if reloc:
                raise ValueError("DE stream lowering requires host-bound payloads")
            if any(uid < 0 or uid >= A.KOS_DE_MAX_UNITS for uid in unit_ids):
                raise ValueError("unit index exceeds DE counter capacity")
            coords = []
            credits = []
            fetches = []
            for i in range(nunit):
                u, cw = struct.unpack_from("<2Q", raw, h[A.KA_PH_OFF_UNIT] + 16 * i)
                coords.append((u & 255) | ((u >> 8) & 255) << 8)
                credits.append((cw & 0xFFFFFFFF) or 512)
                fetches.append((cw >> 32) & 0xFFFFFFFF)

            def build_onto(state, dedupe_waits, allow_split=True):
                entries = state["entries"]
                blobs = state["blobs"]
                mb = state["mb"]
                sent_cnt = state["sent"]
                exp_cnt = state["exp"]
                waited = state["waited"]
                mv_base = state["mv_gos"]
                pkg_pay_off = None
                split_sent = {}
                split_seen = {}
                split_mv = 0
                cum_exp = {}
                reserve = len(set(unit_ids)) + 1
                engine_base = {
                    uid: max(sent_cnt.get(uid, 0), exp_cnt.get(uid, 0))
                    for uid in unit_ids
                }
                coordinate_ids = dict(zip(coords, unit_ids, strict=True))

                def push(code, val):
                    register = code & A.KOS_DE_CODE_MASK
                    if register < A.KOS_DE_MB_GO:
                        state["wire"][register] = val
                    entries.append((code & 0xFF, val & ((1 << 64) - 1)))

                def push_wait(src, want):
                    if self.wait_window:
                        sent = (
                            state["mv_gos"]
                            if src == A.KOS_DE_MOVER_SRC
                            else sent_cnt.get(src, 0)
                        )
                        want = max(want, sent - (A.KOS_DE_CTR_MASK >> 1))
                    if want > 0 and (not dedupe_waits or want > waited.get(src, 0)):
                        push(
                            A.KOS_DE_WAIT_CODE,
                            ((src & 0x1F) << 56) | (want & A.KOS_DE_CTR_MASK),
                        )
                        waited[src] = max(waited.get(src, 0), want)

                def ensure_room(needed):
                    nonlocal state, entries, blobs, mb, sent_cnt, exp_cnt, waited, mv_base, pkg_pay_off, split_mv
                    if (
                        not allow_split
                        or not entries
                        or len(entries) + needed + reserve <= A.KOS_DE_MAX_ENTRIES
                    ):
                        return
                    if self._persistent:
                        resume(self._sync_de(state))
                        return
                    for uid in set(unit_ids):
                        want = max(sent_cnt.get(uid, 0), exp_cnt.get(uid, 0))
                        push_wait(uid, want)
                        split_sent[uid] = split_sent.get(uid, 0) + sent_cnt.get(uid, 0)
                        split_seen[uid] = split_seen.get(uid, 0) + want
                    push_wait(A.KOS_DE_MOVER_SRC, state["mv_gos"])
                    split_mv += state["mv_gos"] - mv_base
                    frontier = self._sync_de(state)
                    self._de = None
                    state, _ = self._open_de(frontier)
                    entries = state["entries"]
                    blobs = state["blobs"]
                    mb = state["mb"]
                    sent_cnt = state["sent"]
                    exp_cnt = state["exp"]
                    waited = state["waited"]
                    mv_base = state["mv_gos"]
                    pkg_pay_off = None

                def resume(frontier):
                    nonlocal state, entries, blobs, mb, sent_cnt, exp_cnt, waited
                    nonlocal pkg_pay_off
                    previous = state
                    self._de = None
                    state, _ = self._open_de(frontier)
                    for key in ("mb", "wire", "sent", "exp", "waited", "mv_gos"):
                        state[key] = previous[key]
                    entries, blobs = state["entries"], state["blobs"]
                    mb = state["mb"]
                    sent_cnt, exp_cnt, waited = (
                        state["sent"],
                        state["exp"],
                        state["waited"],
                    )
                    pkg_pay_off = None

                def release(destination, slot, amount, fence):
                    frontier = self._sync_de(state)
                    drained = self.add(Task(A.KDS_DE_DRAIN, pred=frontier))
                    frontier = self.release_slot(
                        self._node_ids[destination],
                        slot,
                        amount,
                        (drained,),
                        fence=fence,
                    )
                    resume(frontier)

                def push_mb(reg, val):
                    if mb[reg] != val:
                        mb[reg] = val
                        push(reg, val)

                def emit_words(unit, word_bytes):
                    uid = unit_ids[unit]
                    cred = credits[unit]
                    fetch = fetches[unit]
                    dst = coords[unit]
                    nwords = len(word_bytes) // 32
                    if self.de_fetch and (fetch >> 16):
                        room = min(cred, 512)
                        most = max(1, min(room, 255))
                        x, y = dst & 0xFF, (dst >> 8) & 0xFF
                        off = 0
                        rem = nwords
                        while rem:
                            ensure_room(7)
                            s_uid = sent_cnt.get(uid, 0)
                            n = min(rem, most)
                            chunk_bytes = word_bytes[off : off + n * 32]
                            blob_off = len(blobs)
                            blobs.extend(chunk_bytes)
                            push_wait(uid, s_uid + n - room)
                            push_mb(A.KOS_DE_MB_DST, A.de_fetch_dst(fetch))
                            push_mb(A.KOS_DE_MB_ARG0, 0)
                            push_mb(A.KOS_DE_MB_ARG1, 0)
                            push_mb(A.KOS_DE_MB_ARG2, A.de_fetch_arg2(x, y))
                            mb[A.KOS_DE_MB_ARG3] = None
                            push(A.KOS_DE_FETCH_CODE, A.de_fetch_arg3(blob_off, n))
                            push(A.KOS_DE_MB_GO, 1)
                            sent_cnt[uid] = s_uid + n
                            off += n * 32
                            rem -= n
                    else:
                        for k in range(nwords):
                            ensure_room(7)
                            s_uid = sent_cnt.get(uid, 0)
                            push_wait(uid, s_uid + 1 - cred)
                            w0, w1, w2, w3 = struct.unpack_from(
                                "<4Q", word_bytes, 32 * k
                            )
                            push_mb(A.KOS_DE_MB_DST, dst)
                            push_mb(A.KOS_DE_MB_ARG0, w0)
                            push_mb(A.KOS_DE_MB_ARG1, w1)
                            push_mb(A.KOS_DE_MB_ARG2, w2)
                            push_mb(A.KOS_DE_MB_ARG3, w3)
                            push(A.KOS_DE_MB_GO, 1)
                            sent_cnt[uid] = s_uid + 1

                reg = [0] * A.KA_PKG_REGS
                reg_mem = {}
                for i in range(nstep):
                    a, arg = struct.unpack_from(
                        "<QQ", raw, h[A.KA_PH_OFF_STEP] + 16 * i
                    )
                    op, flags, unit, count = (
                        a & 255,
                        (a >> 8) & 255,
                        (a >> 16) & 0xFFFF,
                        a >> 32,
                    )
                    if op == A.KA_OP_END:
                        break
                    if op == A.KA_OP_DISPATCH:
                        pay = h[A.KA_PH_OFF_PAY]
                        emit_words(unit, raw[pay + 32 * arg : pay + 32 * (arg + count)])
                    elif op == A.KA_OP_REPEAT:
                        emit_words(unit, _unroll_repeat(raw, h, arg, count))
                    elif op in (A.KA_OP_LOAD, A.KA_OP_ALU, A.KA_OP_ASSERT_ZERO):
                        check_pa = _eval_contract_step(
                            op, flags, unit, count, arg, raw, h, reg, reg_mem
                        )
                        if check_pa is not None:
                            ensure_room(1)
                            push(A.KOS_DE_CHECK_ZERO, check_pa)
                            mb[:] = [None] * 5
                    elif op == A.KA_OP_FETCH:
                        uid = unit_ids[unit]
                        cred = credits[unit]
                        fetch = fetches[unit]
                        if not (fetch >> 16):
                            raise ValueError("FETCH step on unit without fetch port")
                        room = min(cred, 512)
                        most = max(1, min(room, 255))
                        dst = coords[unit]
                        x, y = dst & 0xFF, (dst >> 8) & 0xFF
                        at, rem = arg, count
                        while rem:
                            ensure_room(7)
                            s_uid = sent_cnt.get(uid, 0)
                            n = min(rem, most)
                            push_wait(uid, s_uid + n - room)
                            push_mb(A.KOS_DE_MB_DST, A.de_fetch_dst(fetch))
                            push_mb(A.KOS_DE_MB_ARG0, 0)
                            push_mb(A.KOS_DE_MB_ARG1, 0)
                            push_mb(A.KOS_DE_MB_ARG2, A.de_fetch_arg2(x, y))
                            mb[A.KOS_DE_MB_ARG3] = None
                            push(A.KOS_DE_MB_ARG3, A.de_fetch_arg3(at, n))
                            push(A.KOS_DE_MB_GO, 1)
                            sent_cnt[uid] = s_uid + n
                            at += n * 32
                            rem -= n
                    elif op == A.KA_OP_AWAIT:
                        uid = unit_ids[unit]
                        s_total = split_sent.get(uid, 0) + sent_cnt.get(uid, 0)
                        e_total = cum_exp.get(uid, exp_cnt.get(uid, 0))
                        if flags == A.KA_STEP_F_IMM:
                            e_total = s_total
                        else:
                            e_total += count
                        # Above s_total: a peer's retirements, counted as this
                        # unit's. Only an AWAIT raises `exp`, never a BARRIER.
                        cum_exp[uid] = e_total
                        local_e = e_total - split_seen.get(uid, 0)
                        if local_e > 0:
                            ensure_room(1)
                            exp_cnt[uid] = max(exp_cnt.get(uid, 0), local_e)
                            push_wait(uid, exp_cnt[uid])
                    elif op == A.KA_OP_MOVER and self.movers:
                        posted = (flags & A.KA_STEP_F_POSTED) != 0
                        if not self._persistent:
                            ensure_room(2 * count + (0 if posted else 1))
                        for p in range(arg, arg + count):
                            for pair in range(2):
                                mreg, val = struct.unpack_from(
                                    "<QQ",
                                    raw,
                                    h[A.KA_PH_OFF_PAY] + 32 * p + 16 * pair,
                                )
                                if mreg == A.KA_MOVER_SKIP:
                                    continue
                                if self._persistent:
                                    ensure_room(1)
                                push(mover_code(mreg), val)
                                if mreg == 0 and (val & A.KOS_MV_CTRL_GO):
                                    state["mv_gos"] += 1
                        if not posted:
                            ensure_room(1)
                            push_wait(A.KOS_DE_MOVER_SRC, state["mv_gos"])
                    elif op == A.KA_OP_MWAIT and self.movers:
                        if count > split_mv + (state["mv_gos"] - mv_base):
                            raise ValueError("MWAIT exceeds submitted mover commands")
                        local_mv = mv_base + count - split_mv
                        if local_mv > mv_base:
                            ensure_room(1)
                            push_wait(A.KOS_DE_MOVER_SRC, local_mv)
                    elif op == A.KA_OP_WAIT_SLOT and self._persistent:
                        if flags & ~A.KA_STEP_F_CONSUME or arg:
                            raise ValueError("invalid WAIT_SLOT flags or argument")
                        frontier = self.wait_slot(
                            unit,
                            count,
                            self._sync_de(state),
                            consume=bool(flags & A.KA_STEP_F_CONSUME),
                        )
                        resume(frontier)
                    elif op == A.KA_OP_RING:
                        if self._persistent:
                            release(unit, count, arg & 65535, bool((arg >> 16) & 1))
                            continue
                        ensure_room(1)
                        ring_val = (
                            (unit & 3)
                            | ((count & 0xFF) << 8)
                            | ((arg & 0xFFFF) << 16)
                            | (((arg >> 16) & 1) << 32)
                        )
                        push(A.KOS_DE_IL_RING, ring_val)
                    elif op == A.KA_OP_ENGINE:
                        for k in range(count):
                            ensure_room(1)
                            c_word, v1, v2, v3 = struct.unpack_from(
                                "<4Q",
                                raw,
                                h[A.KA_PH_OFF_PAY]
                                + 32 * (arg + k // A.KOS_DE_GROUP_ENTRIES),
                            )
                            j = k % A.KOS_DE_GROUP_ENTRIES
                            c = (c_word >> (8 * j)) & 0xFF
                            v = (v1, v2, v3)[j]
                            if c & A.KOS_DE_RELOC_FETCH:
                                if pkg_pay_off is None:
                                    pkg_pay_off = len(blobs)
                                    blobs.extend(
                                        raw[
                                            h[A.KA_PH_OFF_PAY] : h[A.KA_PH_OFF_PAY]
                                            + 32 * arg
                                        ]
                                    )
                                v = (v + (pkg_pay_off << A.KOS_DE_FETCH_SHIFT)) & (
                                    (1 << 64) - 1
                                )
                            if (
                                self._persistent
                                and (c & A.KOS_DE_CODE_MASK) == A.KOS_DE_IL_RING
                            ):
                                release(
                                    v & 3,
                                    (v >> 8) & 255,
                                    (v >> 16) & 65535,
                                    bool(v >> 32 & 1),
                                )
                                continue
                            if (c & A.KOS_DE_CODE_MASK) == A.KOS_DE_WAIT_CODE:
                                src = (v >> 56) & 0x1F
                                want = v & A.KOS_DE_CTR_MASK
                                if self._persistent:
                                    uid = (
                                        unit_ids[src]
                                        if src < nunit
                                        else A.KOS_DE_MOVER_SRC
                                    )
                                    base_u = engine_base.get(uid, mv_base)
                                    sent = (
                                        state["mv_gos"]
                                        if uid == A.KOS_DE_MOVER_SRC
                                        else sent_cnt.get(uid, 0)
                                    )
                                    half = (A.KOS_DE_CTR_MASK + 1) // 2
                                    target = (
                                        sent
                                        + (
                                            (base_u + want - sent + half)
                                            & A.KOS_DE_CTR_MASK
                                        )
                                        - half
                                    )
                                    exp_cnt[uid] = max(exp_cnt.get(uid, 0), target)
                                    waited[uid] = max(waited.get(uid, 0), target)
                                    push(
                                        A.KOS_DE_WAIT_CODE,
                                        (uid << 56)
                                        | ((base_u + want) & A.KOS_DE_CTR_MASK),
                                    )
                                elif src < nunit:
                                    uid = unit_ids[src]
                                    base_u = exp_cnt.get(uid, 0)
                                    push_wait(uid, base_u + want)
                                else:
                                    push_wait(A.KOS_DE_MOVER_SRC, mv_base + want)
                            else:
                                if (
                                    self._persistent
                                    and (c & A.KOS_DE_CODE_MASK) == A.KOS_DE_MB_GO
                                ):
                                    wire = state["wire"]
                                    fetch = bool(wire[A.KOS_DE_MB_DST] >> 16)
                                    if fetch:
                                        xy = (
                                            wire[A.KOS_DE_MB_ARG2]
                                            >> A.KOS_DE_FETCH_COORD_SHIFT
                                        )
                                        dst = (xy & 15) | (
                                            (xy >> A.KOS_DE_FETCH_Y_SHIFT) & 15
                                        ) << 8
                                        count_words = wire[A.KOS_DE_MB_ARG3] & 255
                                    else:
                                        dst = wire[A.KOS_DE_MB_DST] & 65535
                                        count_words = 1
                                    uid = coordinate_ids[dst]
                                    sent_cnt[uid] = sent_cnt.get(uid, 0) + count_words
                                if (
                                    self._persistent
                                    and (c & A.KOS_DE_CODE_MASK) == A.KOS_DE_MV_CTRL
                                    and v & A.KOS_MV_CTRL_GO
                                ):
                                    state["mv_gos"] += 1
                                if (c & A.KOS_DE_CODE_MASK) < A.KOS_DE_MB_GO:
                                    mb[c & A.KOS_DE_CODE_MASK] = (
                                        None if (c & A.KOS_DE_RELOC_FETCH) else v
                                    )
                                push(c, v)
                    elif op == A.KA_OP_BARRIER:
                        ensure_room(reserve)
                        for uid in set(unit_ids):
                            want = max(sent_cnt.get(uid, 0), exp_cnt.get(uid, 0))
                            push_wait(uid, want)
                        push_wait(A.KOS_DE_MOVER_SRC, state["mv_gos"])
                    else:
                        raise ValueError(
                            f"KDS package lowering: unsupported opcode {op} at step {i}"
                        )
                for uid in set(unit_ids):
                    want = max(sent_cnt.get(uid, 0), exp_cnt.get(uid, 0))
                    exp_cnt[uid] = want
                    push_wait(uid, want)
                push_wait(A.KOS_DE_MOVER_SRC, state["mv_gos"])
                return state

            state, reused = self._open_de(after)
            if reused:
                snap = (
                    len(state["entries"]),
                    len(state["blobs"]),
                    list(state["mb"]),
                    dict(state["sent"]),
                    dict(state["exp"]),
                    dict(state["waited"]),
                    state["mv_gos"],
                    list(state["wire"]),
                )
                end_state = build_onto(state, True, allow_split=False)
                if len(end_state["entries"]) > A.KOS_DE_MAX_ENTRIES:
                    del state["entries"][snap[0] :]
                    del state["blobs"][snap[1] :]
                    state["mb"][:] = snap[2]
                    state["sent"] = snap[3]
                    state["exp"] = snap[4]
                    state["waited"] = snap[5]
                    state["mv_gos"] = snap[6]
                    state["wire"][:] = snap[7]
                    self._sync_de(state)
                    self._de = None
                    state, _ = self._open_de(after)
                    end_state = build_onto(state, self.de_fuse, allow_split=True)
            else:
                end_state = build_onto(state, self.de_fuse, allow_split=True)
            return self._sync_de(end_state)
        steps = [
            struct.unpack_from("<QQ", raw, h[A.KA_PH_OFF_STEP] + 16 * i)
            for i in range(nstep)
        ]
        cuts = [set() for _ in unit_ids]
        issued, wants = [0] * nunit, [0] * nunit
        for a, arg in steps:
            op, unit, count = a & 255, (a >> 16) & 65535, a >> 32
            if op == A.KA_OP_END:
                break
            if op == A.KA_OP_DISPATCH:
                issued[unit] += count
            elif op == A.KA_OP_REPEAT:
                issued[unit] += count * ((arg >> 32) & 0xFFFF)
            elif op == A.KA_OP_AWAIT:
                wants[unit] = (
                    issued[unit]
                    if (a >> 8) & 255 == A.KA_STEP_F_IMM
                    else wants[unit] + count
                )
                cuts[unit].add(wants[unit])
        sent = [[] for _ in unit_ids]
        expected = [0] * nunit
        gate = tuple(after)
        since_barrier = []
        mover_pairs = []
        mover_sent = 0
        reg = [0] * A.KA_PKG_REGS
        for i in range(nstep):
            a, arg = struct.unpack_from("<QQ", raw, h[A.KA_PH_OFF_STEP] + 16 * i)
            op, flags, unit, count = (
                a & 255,
                (a >> 8) & 255,
                (a >> 16) & 0xFFFF,
                a >> 32,
            )
            if op == A.KA_OP_END:
                break
            if op in (A.KA_OP_LOAD, A.KA_OP_ALU, A.KA_OP_ASSERT_ZERO):
                _eval_contract_step(op, flags, unit, count, arg, raw, h, reg)
                continue
            if op in (A.KA_OP_DISPATCH, A.KA_OP_REPEAT):
                pay = h[A.KA_PH_OFF_PAY]
                word_buf = (
                    raw[pay + 32 * arg : pay + 32 * (arg + count)]
                    if op == A.KA_OP_DISPATCH
                    else _unroll_repeat(raw, h, arg, count)
                )
                total_w = len(word_buf) // 32
                p = 0
                while p < total_w:
                    width = min(self.batch, total_w - p)
                    boundary = min(
                        (c for c in cuts[unit] if c > len(sent[unit])),
                        default=len(sent[unit]) + width,
                    )
                    width = min(width, boundary - len(sent[unit]))
                    if self.batch > 1:
                        t = self.add(
                            Task(
                                A.KDS_STREAM,
                                unit_ids[unit],
                                words=(0, width, 0, 0),
                                pred=gate,
                            )
                        )
                        self.streams[t] = word_buf[32 * p : 32 * (p + width)]
                    else:
                        words = struct.unpack_from("<4Q", word_buf, 32 * p)
                        t = self.add(
                            Task(
                                A.KDS_UNIT,
                                unit_ids[unit],
                                words=words,
                                pred=gate,
                                patches=(
                                    tuple(reloc.get(arg + p, ()))
                                    if op == A.KA_OP_DISPATCH
                                    else ()
                                ),
                            )
                        )
                    sent[unit].extend([t] * width)
                    since_barrier.append(t)
                    p += width
            elif op == A.KA_OP_AWAIT:
                if flags == A.KA_STEP_F_IMM:
                    expected[unit] = len(sent[unit])
                else:
                    expected[unit] += count
                if expected[unit] > len(sent[unit]):
                    raise ValueError(
                        "KDS package lowering does not support peer acknowledgement AWAIT"
                    )
                waited = sent[unit][: expected[unit]]
                gate = self.join((*gate, *waited))
            elif op == A.KA_OP_MOVER and self.movers:
                if reloc:
                    raise ValueError("mover streams require host-bound packages")
                gate = self.join((*gate, *since_barrier))
                since_barrier = []
                for p in range(arg, arg + count):
                    for pair in range(2):
                        mreg, value = struct.unpack_from(
                            "<QQ", raw, h[A.KA_PH_OFF_PAY] + 32 * p + 16 * pair
                        )
                        if mreg == A.KA_MOVER_SKIP:
                            continue
                        mover_pairs.append((mover_code(mreg), value))
                        if mreg == 0 and value & A.KOS_MV_CTRL_GO:
                            # One mover command: its writes, then a wait for its
                            # one completion, as a synchronous DE stream.
                            mover_pairs.append(
                                (A.KOS_DE_WAIT_CODE, (A.KOS_DE_MOVER_SRC << 56) | 1)
                            )
                            t = self.add(Task(A.KDS_DE_STREAM, pred=gate))
                            gate = self._sync_de(
                                dict(task=t, entries=mover_pairs, blobs=bytearray())
                            )
                            mover_pairs = []
                            mover_sent += 1
            elif op == A.KA_OP_MWAIT and self.movers:
                if count > mover_sent:
                    raise ValueError("MWAIT exceeds submitted mover commands")
            elif op == A.KA_OP_BARRIER:
                pending = (
                    since_barrier
                    if self.compact
                    else [t for stream in sent for t in stream]
                )
                gate = self.join((*gate, *pending))
                since_barrier = []
            else:
                raise ValueError(
                    f"KDS package lowering: unsupported opcode {op} at step {i}"
                )
        if mover_pairs:
            raise ValueError("incomplete mover configuration at package end")
        pending = (
            since_barrier if self.compact else [t for stream in sent for t in stream]
        )
        return gate if self.compact and not pending else self.join((*gate, *pending))

    def wait_slot(self, slot, count=1, after=(), *, consume=True):
        """Acquire a leased receiver-local event without occupying the feeder."""
        if not A.KOS_SLOT_USER <= slot < A.KOS_SLOT_LIMIT or not 0 <= count < 1 << 32:
            raise ValueError("invalid user slot or consume count")
        if count == 0:
            return self.join(after)
        return (
            self.add(
                Task(
                    A.KDS_WAIT_SLOT,
                    words=(slot, count, 0, int(not consume)),
                    pred=after,
                )
            ),
        )

    def release_slot(self, node, slot, count=1, after=(), *, fence=True):
        """Publish a proven producer frontier through a leased user slot."""
        if (
            not 0 <= node < 256
            or not A.KOS_SLOT_USER <= slot < A.KOS_SLOT_LIMIT
            or not 0 <= count <= A.KOS_SLOT_AMOUNT
        ):
            raise ValueError("invalid release destination, user slot or ring count")
        count = count or 1
        return (
            self.add(
                Task(
                    A.KDS_RELEASE_SLOT,
                    words=(slot, count, node, int(fence)),
                    pred=after,
                )
            ),
        )

    def stamp(self, after, label):
        task = self.add(Task(A.KDS_STAMP, words=(0, label, 0, 0), pred=after))
        self.streams[task] = bytes(32)
        return (task,)

    def launch_mark(self, after, index):
        return (
            self.add(
                Task(
                    A.KDS_LAUNCH_MARK,
                    words=(index & ((1 << 64) - 1), 0, 0, 0),
                    pred=after,
                )
            ),
        )

    def pack(self, base_ref=0):
        records, payload = [], bytearray()
        for i, task in enumerate(self.tasks):
            if i in self.streams:
                task = replace(
                    task,
                    words=(
                        base_ref + len(self.tasks) * A.OSTPU_TASK_BYTES + len(payload),
                        *task.words[1:],
                    ),
                )
                payload.extend(self.streams[i])
            records.append(task.pack())
        return b"".join(records) + payload


def _eval_contract_step(op, flags, unit, count, arg, raw, h, reg, reg_mem=None):
    mask64 = (1 << 64) - 1
    if op == A.KA_OP_LOAD:
        if unit >= A.KA_PKG_REGS:
            raise ValueError("LOAD register out of range")
        w0 = struct.unpack_from("<Q", raw, h[A.KA_PH_OFF_PAY] + 32 * arg)[0]
        if flags == A.KA_STEP_F_IMM and count == 0:
            if reg_mem is not None:
                reg_mem.pop(unit, None)
            reg[unit] = w0
            return None
        if flags == 0 and count == 0 and reg_mem is not None and (w0 & 7) == 0:
            reg_mem[unit] = w0
            return None
        raise ValueError(
            "KDS package lowering supports only immediate or aligned status LOAD"
        )
    if op == A.KA_OP_ALU:
        if unit >= A.KA_PKG_REGS or (
            reg_mem is not None
            and (
                ((count >> 8) & 15) in reg_mem
                or (not (flags & A.KA_STEP_F_IMM) and ((count >> 12) & 15) in reg_mem)
            )
        ):
            raise ValueError(
                "ALU register out of range or uses unmaterialized memory load"
            )
        if reg_mem is not None:
            reg_mem.pop(unit, None)
        va = reg[(count >> 8) & 15]
        vb = arg & mask64 if (flags & A.KA_STEP_F_IMM) else reg[(count >> 12) & 15]
        aop = count & 0xFF
        if aop == A.KA_ALU_MOV:
            res = vb
        elif aop == A.KA_ALU_ADD:
            res = (va + vb) & mask64
        elif aop == A.KA_ALU_SUB:
            res = (va - vb) & mask64
        elif aop == A.KA_ALU_MUL:
            res = (va * vb) & mask64
        elif aop == A.KA_ALU_AND:
            res = va & vb
        elif aop == A.KA_ALU_OR:
            res = va | vb
        elif aop == A.KA_ALU_XOR:
            res = va ^ vb
        elif aop == A.KA_ALU_SHL:
            res = (va << (vb & 63)) & mask64
        elif aop == A.KA_ALU_SHR:
            res = va >> (vb & 63)
        elif aop == A.KA_ALU_MIN:
            res = min(va, vb)
        elif aop == A.KA_ALU_MAX:
            res = max(va, vb)
        elif aop == A.KA_ALU_DIVU:
            res = va // vb if vb else mask64
        elif aop == A.KA_ALU_REMU:
            res = va % vb if vb else va
        else:
            raise ValueError(f"unsupported ALU op {aop}")
        reg[unit] = res
        return None
    if unit >= A.KA_PKG_REGS or flags or arg:
        raise ValueError("invalid ASSERT_ZERO step")
    if reg_mem is not None and unit in reg_mem:
        return reg_mem[unit]
    if reg[unit] != 0:
        raise ValueError(f"package contract {count} failed: {reg[unit]:#x}")
    return None


def _unroll_repeat(raw, h, arg, count):
    pay = h[A.KA_PH_OFF_PAY]
    first = arg & 0xFFFFFFFF
    reps = (arg >> 32) & 0xFFFF
    ninc = arg >> 48
    incs = [
        struct.unpack_from("<QQ", raw, pay + 32 * (first + count) + 16 * k)
        for k in range(ninc)
    ]
    unrolled = bytearray()
    for r in range(reps):
        for j in range(count):
            w = int.from_bytes(
                raw[pay + 32 * (first + j) : pay + 32 * (first + j + 1)],
                "little",
            )
            for head, delta in incs:
                if (head & 0xFFFFFFFF) == j:
                    bit = (head >> 32) & 0xFF
                    width = (head >> 40) & 0xFF
                    mask = (1 << width) - 1
                    field = (((w >> bit) & mask) + r * delta) & mask
                    w = (w & ~(mask << bit)) | (field << bit)
            unrolled.extend(w.to_bytes(32, "little"))
    return bytes(unrolled)


class ResidentGraph:
    """Single-flight host ABI; transport addresses are node physical addresses.

    io supplies read(pa, bytes), write(pa, data), idle(). Host writes and
    preparation occur only between successful completions. Failure poisons this
    session; stop/reset the node before reclaiming its memory.
    """

    def __init__(self, io, mailbox, timeout=60, exited=None):
        self._lock = threading.RLock()
        self.io, self.mailbox, self.timeout = io, mailbox, timeout
        self.graph_epoch = 0
        self._prep_nonce = 0
        self.poisoned = False
        self.closed = False
        # `exited()`: the node's exit code once its OS has shut down, else None.
        # A resident that fails shuts the OS down and never writes what is waited for.
        self._exited = exited
        self._wait(A.OSTPU_OFF_READY, A.OSTPU_MAGIC)
        _, self.tasks_pa, self.data_pa, self.data_ref = struct.unpack(
            "<4Q", io.read(mailbox + A.OSTPU_OFF_READY, 32)
        )
        if self.data_pa % DATA_ALIGN:
            self.poisoned = True
            raise ValueError(
                f"data arena {self.data_pa:#x} is off a {DATA_ALIGN}-byte line:"
                " size `ostpu` with task_capacity"
            )
        self.sequence, self.cpu_fn, self.capabilities, _ = struct.unpack(
            "<4Q", io.read(mailbox + A.OSTPU_OFF_DONE, 32)
        )
        _, self.capacity, self.data_bytes, _ = struct.unpack(
            "<4Q", io.read(mailbox + A.OSTPU_OFF_CAP, 32)
        )
        self.count = 0
        self._pending: int | None = None
        self._retainer: object | None = None

    def _wait(self, offset, value):
        end = time.monotonic() + self.timeout
        while (
            struct.unpack_from("<Q", self.io.read(self.mailbox + offset, 32))[0]
            != value
        ):
            if time.monotonic() >= end:
                self.poisoned = True
                raise TimeoutError("OS graph mailbox deadline")
            code = self._exited() if self._exited is not None else None
            if code is not None:
                self.poisoned = True
                raise RuntimeError(
                    f"the node's OS exited ({code}) before the mailbox read"
                    f" {value:#x}: its kernel log says why"
                )
            self.io.idle()

    def _active(self, owner=None):
        if self._retainer is not None and self._retainer is not owner:
            raise RuntimeError("OS resident belongs to a retained multi-part graph")
        if self.closed or self.poisoned:
            raise RuntimeError(
                "OS graph session is closed or failed; reset before reuse"
            )

    def encode(self, graph, *, _owner=None):
        """Preflight without writes; all parts can be checked before any upload."""
        self._active(_owner)
        if not self.capabilities & A.OSTPU_CAP_SLOTS and any(
            task.kind in (A.KDS_WAIT_SLOT, A.KDS_RELEASE_SLOT) for task in graph.tasks
        ):
            raise ValueError("resident image does not advertise slot tasks")
        if not self.capabilities & A.OSTPU_CAP_EPOCH and any(
            task.kind in (A.KDS_DE_FEED, A.KDS_DE_DRAIN, A.KDS_DE_FINISH)
            for task in graph.tasks
        ):
            raise ValueError("resident image does not advertise persistent DE epochs")
        if not 0 < len(graph.tasks) <= self.capacity:
            raise ValueError("graph exceeds resident capacity or is empty")
        raw = graph.pack(self.data_ref + self.tasks_pa - self.data_pa)
        if len(raw) > self.capacity * A.OSTPU_TASK_BYTES:
            raise ValueError("graph records and payloads exceed resident byte capacity")
        return raw

    def prepare(self, graph):
        with self._lock:
            self._install(self.encode(graph), len(graph.tasks))

    def _install(self, raw, count, *, _owner=None, identity=b""):
        """Install a preflighted task array; publication is a separate operation."""
        with self._lock:
            self._active(_owner)
            self.poisoned = True
            self.io.write(self.tasks_pa, raw)
            self.count = count
            self._prep_nonce += 1
            digest = int.from_bytes(
                hashlib.blake2b(raw + identity, digest_size=8).digest(), "little"
            )
            self.graph_epoch = (
                (digest ^ ((self.sequence + 1) << 16) ^ self._prep_nonce)
                & ((1 << 64) - 1)
            ) or 1
            self.poisoned = False

    def submit(self, repeats=1, timeout_cycles=1 << 26, cpu_write=None, *, _owner=None):
        with self._lock:
            self._active(_owner)
            if repeats < 1 or not self.count:
                raise ValueError(
                    "prepare a nonempty graph and request positive repeats"
                )
            self.poisoned = True
            self.io.write(
                self.mailbox + A.OSTPU_OFF_CMD,
                struct.pack("<4Q", A.OSTPU_OP_RUN, self.count, repeats, timeout_cycles),
            )
            self.sequence += 1
            req_words = [self.sequence, 0, 0, 0]
            req_words[A.OSTPU_REQ_EPOCH_WORD] = self.graph_epoch
            if cpu_write is not None:
                (
                    req_words[A.OSTPU_REQ_WRITE_REF_WORD],
                    req_words[A.OSTPU_REQ_WRITE_VAL_WORD],
                ) = cpu_write
            self._pending = self.sequence
            self.io.write(
                self.mailbox + A.OSTPU_OFF_REQ,
                struct.pack("<4Q", *req_words),
            )
            return self.sequence

    def collect(self, token, *, _owner=None):
        """Acknowledge the exact published epoch; failure keeps the session poisoned."""
        with self._lock:
            if self._retainer is not _owner:
                raise RuntimeError("OS resident belongs to a retained multi-part graph")
            if self.closed or self._pending != token:
                raise ValueError("completion token does not name the pending OS graph")
            try:
                self._wait(A.OSTPU_OFF_DONE, token)
                status, failed, detail, cycles, completed, _, _, failed_repeat = (
                    struct.unpack(
                        "<q7Q", self.io.read(self.mailbox + A.OSTPU_OFF_STATUS, 64)
                    )
                )
                stats = dict(
                    zip(
                        A.STAT_NAMES,
                        struct.unpack(
                            A.STAT_FMT,
                            self.io.read(
                                self.mailbox + A.OSTPU_OFF_STAT, 8 * len(A.STAT_NAMES)
                            ),
                        ),
                        strict=True,
                    )
                )
                if status:
                    self.poisoned = True
                    raise RuntimeError(
                        f"KDS status {status}, repeat {failed_repeat}, task {failed}, detail {detail}"
                    )
                self._pending = None
                self.poisoned = False
                return dict(cycles=cycles, completed=completed, submissions=1, **stats)
            except BaseException:
                self._pending = None
                self.poisoned = True
                raise

    def cancel(self, *, _owner=None):
        """Request STOP after any reached execution; never reclaim on this path."""
        with self._lock:
            if self._retainer is not _owner:
                raise RuntimeError("OS resident belongs to a retained multi-part graph")
            self.poisoned = True
            self.sequence = (self.sequence + 1) | A.OSTPU_REQ_CANCEL
            self._pending = None
            self.io.write(
                self.mailbox + A.OSTPU_OFF_REQ,
                struct.pack("<4Q", self.sequence, 0, 0, 0),
            )
            return self.sequence

    def cancelled(self, token, *, _owner=None):
        with self._lock:
            if self._retainer is not _owner:
                raise RuntimeError("OS resident belongs to a retained multi-part graph")
            self._wait(A.OSTPU_OFF_DONE, token)
            self.closed = True

    def run(self, repeats=1, timeout_cycles=1 << 26, cpu_write=None):
        return self.collect(self.submit(repeats, timeout_cycles, cpu_write))

    def close(self):
        with self._lock:
            self._active()
            self.closed = True
            self.io.write(
                self.mailbox + A.OSTPU_OFF_CMD,
                struct.pack("<4Q", A.OSTPU_OP_STOP, 0, 0, 0),
            )
            self.sequence += 1
            self.io.write(
                self.mailbox + A.OSTPU_OFF_REQ,
                struct.pack("<4Q", self.sequence, 0, 0, 0),
            )
            self._wait(A.OSTPU_OFF_DONE, self.sequence)


@dataclass
class MultiCompletion:
    owner: "PreparedMultiGraph"
    tickets: tuple[tuple[int, int], ...]
    results: dict[int, dict] | None = None

    def wait(self):
        return self.owner.collect(self)


class PreparedMultiGraph:
    """One retained, single-flight epoch across node-local OS residents.

    Each resident is booted with `ostpu capacity bytes retain 1`. The caller
    supplies one card-wide lease pool and already-live allocation references.
    Failed or uncertain epochs remain pinned until the card is reset.
    """

    def __init__(
        self,
        artifact: BoundGraphArtifact,
        residents: dict[int, ResidentGraph],
        units,
        leases: SlotLeases,
        *,
        references=(),
        unit_types=None,
        **options,
    ):
        self._lock = threading.RLock()
        self.artifact = artifact
        self.residents = {part.node: residents[part.node] for part in artifact.parts}
        self.leases = leases
        self.references = []
        self.pending: MultiCompletion | None = None
        self.closed = self.poisoned = False
        # Participants a failed cancellation could not settle: (resident, error).
        self.unsettled = []
        claimed = []
        try:
            for resident in self.residents.values():
                with resident._lock:
                    resident._active()
                    if resident.capabilities & (
                        A.OSTPU_CAP_LOCAL | A.OSTPU_CAP_EPOCH
                    ) != (A.OSTPU_CAP_LOCAL | A.OSTPU_CAP_EPOCH):
                        raise ValueError(
                            "multi-part graphs require node-local residents"
                        )
                    resident._retainer = self
                    claimed.append(resident)
            node_ids = {
                node: resident.data_ref >> 56
                for node, resident in self.residents.items()
            }
            graphs = Graph.package_parts(
                artifact, units, node_ids=node_ids, unit_types=unit_types, **options
            )
            encoded = {
                node: resident.encode(graphs[node], _owner=self)
                for node, resident in self.residents.items()
            }
            leases.acquire(artifact.slot_leases, self)
            for arena, address in references:
                arena.retain_pinned(address)
                self.references.append((arena, address))
            for node, resident in self.residents.items():
                resident._install(
                    encoded[node],
                    len(graphs[node].tasks),
                    _owner=self,
                    identity=artifact.digest,
                )
        except BaseException:
            for resident in claimed:
                resident._retainer = None
                resident.count = 0
            for arena, address in self.references:
                arena.unretain(address)
            leases.release(self)
            raise

    def submit(self, timeout_cycles=1 << 26):
        with self._lock:
            if self.closed or self.poisoned or self.pending is not None:
                raise RuntimeError(
                    "multi-part graph is closed, failed or already in flight"
                )
            for resident in self.residents.values():
                resident._active(self)
            tickets = []
            try:
                for node, resident in self.residents.items():
                    tickets.append(
                        (
                            node,
                            resident.submit(timeout_cycles=timeout_cycles, _owner=self),
                        )
                    )
            except BaseException:
                self._cancel_all()
                raise
            self.pending = MultiCompletion(self, tuple(tickets))
            return self.pending

    def _cancel_all(self):
        self._poison()
        stops = []
        # A failed STOP publication or acknowledgement leaves that participant
        # quarantined; only a reset can settle it.
        unsettled = []
        for resident in self.residents.values():
            try:
                stops.append((resident, resident.cancel(_owner=self)))
            except Exception as error:  # noqa: BLE001
                unsettled.append((resident, error))
        for resident, token in stops:
            try:
                resident.cancelled(token, _owner=self)
            except Exception as error:  # noqa: BLE001
                unsettled.append((resident, error))
        self.unsettled = unsettled
        self._poison()

    def _poison(self):
        self.poisoned = True
        for resident in self.residents.values():
            resident.poisoned = True

    def collect(self, token: MultiCompletion):
        with self._lock:
            if token.owner is not self:
                raise ValueError("completion belongs to another multi-part graph")
            if token.results is not None:
                return token.results
            if self.pending is not token or self.poisoned:
                raise RuntimeError("no collectable multi-part epoch")
            results = {}
            failure = None
            for node, ticket in token.tickets:
                try:
                    results[node] = self.residents[node].collect(ticket, _owner=self)
                except Exception as error:  # noqa: BLE001
                    if failure is None:
                        failure = error
            if failure is not None:
                self._cancel_all()
                raise failure
            token.results = results
            self.pending = None
            return results

    def run(self, timeout_cycles=1 << 26):
        return self.submit(timeout_cycles).wait()

    def close(self):
        with self._lock:
            if self.closed:
                return
            if self.pending is not None or self.poisoned:
                raise RuntimeError(
                    "reset the card before reclaiming an uncertain graph"
                )
            for resident in self.residents.values():
                resident.count = 0
                resident._retainer = None
            for arena, address in self.references:
                arena.unretain(address)
            self.references.clear()
            self.leases.release(self)
            self.closed = True
