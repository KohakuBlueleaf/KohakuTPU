"""The queue region's layout and codes (docs/spec/node-queue.md).

Built from the generated machine schema (`fw.queue`, derived from
``software/firmware/kohakuaccel/include/ka/queue/layout.h`` and
``ka/package/status.h``). Every field has one writer and a `line`-byte line of
its own: the widest whole write a host makes (XDMA 64 bytes, JTAG 32), so no
host write carries another writer's field.
"""

import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path

#: Completion status names and TIMEOUT wait names, generated from status.h.
from kohakuaccel.driver.node.queue.status import STATUS, WAITS  # noqa: F401

# RUN flags (ka/package/interp.h KA_RUN_F_*). IRQ toggles the node's host
# interrupt once the completion is posted; a host that cannot take one omits it.
RUN_F_CHECKSUM, RUN_F_IRQ = 1, 2
#: The debug image only: trace this run; keep it off the dispatch engine.
RUN_F_TRACE, RUN_F_NOENGINE = 4, 8


@dataclass(frozen=True)
class Record:
    """A run record's schema layout: field name -> (byte offset, width)."""

    version: int
    nbytes: int
    fields: dict
    word: int

    @classmethod
    def from_schema(cls, rec: dict, expected_bytes: int | None = None) -> "Record":
        """Decode `fw.queue.record`, refusing fields outside the entry."""
        nbytes = rec["bytes"]
        word = rec["word"]
        limit = nbytes if expected_bytes is None else expected_bytes
        fields = {k: (f["offset"], f["width"]) for k, f in rec["fields"].items()}
        if nbytes != limit or any(o + w > limit for o, w in fields.values()):
            raise ValueError(f"a run record is {limit} bytes: {rec}")
        return cls(rec["version"], nbytes, fields, word)

    def decode(self, raw: bytes) -> dict:
        return {
            k: int.from_bytes(raw[o : o + w], "little")
            for k, (o, w) in self.fields.items()
        }


@dataclass(frozen=True)
class Layout:
    """Queue region, LIST and run-record layout built from `fw.queue`."""

    schema: dict
    magic: int = 0
    min_version: int = 0
    version: int = 0
    list_version: int = 0
    line: int = 0
    sq_bytes: int = 0
    cq_bytes: int = 0
    sq_words: int = 0
    nbind_shift: int = 0
    stdio_mask: int = 0
    op_mask: int = 0
    cq_status_mask: int = 0
    offsets: dict | None = None
    cq_offsets: dict | None = None
    info_words: dict | None = None
    fw_words: dict | None = None
    states: dict | None = None
    opcodes: dict | None = None
    run_words: dict | None = None
    list_words: dict | None = None
    status: dict | None = None
    record: Record | None = None

    @classmethod
    def from_schema(cls, q: dict) -> "Layout":
        rec = q["record"]
        return cls(
            schema=q,
            magic=q["magic"],
            min_version=q["min_version"],
            version=q["version"],
            list_version=q["list_version"],
            line=q["line"],
            sq_bytes=q["sq_bytes"],
            cq_bytes=q["cq_bytes"],
            sq_words=q["sq_words"],
            nbind_shift=q["nbind_shift"],
            stdio_mask=q["stdio_mask"],
            op_mask=q["op_mask"],
            cq_status_mask=q["cq_status_mask"],
            offsets=dict(q["offsets"]),
            cq_offsets=dict(q["cq_offsets"]),
            info_words=dict(q["info_words"]),
            fw_words=dict(q["fw_words"]),
            states=dict(q["states"]),
            opcodes=dict(q["opcodes"]),
            run_words=dict(q["run_words"]),
            list_words=dict(q["list_words"]),
            status=dict(q["status"]),
            record=Record.from_schema(rec, expected_bytes=rec["bytes"]),
        )

    @classmethod
    def from_machine(cls, machine: str | bytes | os.PathLike | dict) -> "Layout":
        if isinstance(machine, os.PathLike):
            doc = json.loads(Path(machine).read_text(encoding="utf-8"))
        elif isinstance(machine, (str, bytes)):
            doc = json.loads(machine)
        else:
            doc = machine
        q = (
            doc["machine"]["fw"]["queue"]
            if "machine" in doc
            else doc["fw"]["queue"] if "fw" in doc else doc
        )
        return cls.from_schema(q)

    @property
    def versions(self) -> tuple[int, ...]:
        return tuple(range(self.min_version, self.version + 1))

    @property
    def info(self) -> int:
        return self.offsets["info"]

    @property
    def epoch(self) -> int:
        return self.offsets["epoch"]

    @property
    def fw(self) -> int:
        return self.offsets["fw"]

    @property
    def list_abi(self) -> int:
        return self.offsets["list_abi"]

    @property
    def sq_tail(self) -> int:
        return self.offsets["sq_tail"]

    @property
    def sq_head(self) -> int:
        return self.offsets["sq_head"]

    @property
    def cq_tail(self) -> int:
        return self.offsets["cq_tail"]

    @property
    def cq_head(self) -> int:
        return self.offsets["cq_head"]

    @property
    def so_wr(self) -> int:
        return self.offsets["so_wr"]

    @property
    def so_rd(self) -> int:
        return self.offsets["so_rd"]

    @property
    def si_wr(self) -> int:
        return self.offsets["si_wr"]

    @property
    def si_rd(self) -> int:
        return self.offsets["si_rd"]

    @property
    def units(self) -> int:
        return self.offsets["units"]

    @property
    def heaps(self) -> int:
        return self.offsets["heaps"]

    @property
    def sq(self) -> int:
        return self.offsets["sq"]

    @property
    def cq_tag(self) -> int:
        return self.cq_offsets["tag"]

    @property
    def cq_status(self) -> int:
        return self.cq_offsets["status"]

    @property
    def cq_cycles(self) -> int:
        return self.cq_offsets["cycles"]

    @property
    def cq_value(self) -> int:
        return self.cq_offsets["value"]

    @property
    def rec_bytes(self) -> int:
        return self.record.nbytes

    @property
    def rec_version(self) -> int:
        return self.record.version

    @property
    def rec_word(self) -> int:
        return self.record.word

    @property
    def sq_tail_words(self) -> int:
        return self.sq_words - 2

    @property
    def tail_poll_bytes(self) -> int:
        return self.so_wr + self.line - self.cq_tail

    @property
    def so_wr_from_cq_tail(self) -> int:
        return self.so_wr - self.cq_tail

    @property
    def fw_state(self) -> int:
        return self.fw_words["state"]

    @property
    def fw_abi(self) -> int:
        return self.fw_words["abi"]

    @property
    def fw_done(self) -> int:
        return self.fw_words["done"]

    @property
    def fw_fatal(self) -> int:
        return self.fw_words["fatal"]

    @property
    def fw_bytes(self) -> int:
        return len(self.fw_words) * 8

    def decode_fw(self, raw: bytes) -> dict:
        words = struct.unpack_from(f"<{len(self.fw_words)}Q", raw)
        st = words[self.fw_state]
        return {
            "state": self.state_names.get(st, st),
            "abi": words[self.fw_abi],
            "done": words[self.fw_done],
            "fatal": words[self.fw_fatal],
        }

    @property
    def st_none(self) -> int:
        return self.states["none"]

    @property
    def st_ready(self) -> int:
        return self.states["ready"]

    @property
    def st_stopped(self) -> int:
        return self.states["stopped"]

    @property
    def st_fatal(self) -> int:
        return self.states["fatal"]

    @property
    def state_names(self) -> dict[int, str]:
        return {code: name for name, code in self.states.items()}

    @property
    def op_nop(self) -> int:
        return self.opcodes["nop"]

    @property
    def op_run(self) -> int:
        return self.opcodes["run"]

    @property
    def op_stop(self) -> int:
        return self.opcodes["stop"]

    @property
    def op_list(self) -> int:
        return self.opcodes["list"]

    @property
    def run_op(self) -> int:
        return self.run_words["op"]

    @property
    def run_tag(self) -> int:
        return self.run_words["tag"]

    @property
    def run_pkg(self) -> int:
        return self.run_words["pkg"]

    @property
    def run_bytes(self) -> int:
        return self.run_words["bytes"]

    @property
    def run_bind(self) -> int:
        return self.run_words["bind"]

    @property
    def run_timeout(self) -> int:
        return self.run_words["timeout"]

    @property
    def run_flags(self) -> int:
        return self.run_words["flags"]

    @property
    def list_op(self) -> int:
        return self.list_words["op"]

    @property
    def list_tag(self) -> int:
        return self.list_words["tag"]

    @property
    def list_addr(self) -> int:
        return self.list_words["addr"]

    @property
    def list_count(self) -> int:
        return self.list_words["count"]

    @property
    def list_epoch(self) -> int:
        return self.list_words["epoch"]

    @property
    def list_version_word(self) -> int:
        return self.list_words["version"]

    @property
    def list_flags(self) -> int:
        return self.list_words["flags"]

    @property
    def ok(self) -> int:
        return self.status["ok"]

    @property
    def progress(self) -> int:
        return self.status["progress"]

    @property
    def bad_op(self) -> int:
        return self.status["bad_op"]

    @property
    def bad_arg(self) -> int:
        return self.status["bad_arg"]

    @property
    def unit_fault(self) -> int:
        return self.status["unit_fault"]

    @property
    def mover_fault(self) -> int:
        return self.status["mover_fault"]

    @property
    def timeout(self) -> int:
        return self.status["timeout"]

    @property
    def no_memory(self) -> int:
        return self.status["no_memory"]

    @property
    def no_slots(self) -> int:
        return self.status["no_slots"]

    @property
    def bad_free(self) -> int:
        return self.status["bad_free"]

    @property
    def heap_busy(self) -> int:
        return self.status["heap_busy"]

    def line_bytes(self, *words: int) -> bytes:
        w = list(words) + [0] * (self.line // 8 - len(words))
        return struct.pack(f"<{self.line // 8}Q", *w)

    def entry_bytes(self, *words: int) -> bytes:
        w = list(words) + [0] * (self.sq_words - len(words))
        return struct.pack(f"<{self.sq_words}Q", *w)

    def list_descriptor_bytes(self, count: int) -> int:
        return count * self.sq_bytes

    def info_line(
        self,
        version: int,
        sq_n: int,
        cq_n: int,
        so_bytes: int,
        si_bytes: int,
        epoch: int = 0,
    ) -> bytes:
        words = [0] * (self.line // 8)
        values = {
            "magic": self.magic,
            "version": version,
            "rings": sq_n | cq_n << 16,
            "stdio": so_bytes | si_bytes << 32,
            "epoch": epoch,
        }
        for name, index in self.info_words.items():
            words[index] = values[name]
        return self.line_bytes(*words)

    def run_entry(
        self,
        tag: int,
        pkg: int,
        nbytes: int,
        bind: int = 0,
        nbind: int = 0,
        timeout: int = 0,
        flags: int = 0,
        op: int | None = None,
    ) -> tuple[int, ...]:
        words = [0] * self.sq_words
        opcode = self.op_run if op is None else op
        values = {
            "op": opcode | nbind << self.nbind_shift,
            "tag": tag,
            "pkg": pkg,
            "bytes": nbytes,
            "bind": bind,
            "timeout": timeout,
            "flags": flags,
        }
        for name, index in self.run_words.items():
            words[index] = values[name]
        return tuple(words)

    def list_entry(
        self, tag: int, address: int, count: int, epoch: int, flags: int = 0
    ) -> tuple[int, ...]:
        words = [0] * self.sq_words
        values = {
            "op": self.op_list,
            "tag": tag,
            "addr": address,
            "count": count,
            "epoch": epoch,
            "version": self.list_version,
            "flags": flags,
        }
        for name, index in self.list_words.items():
            words[index] = values[name]
        return tuple(words)

    def list_tail(
        self, address: int, count: int, epoch: int, flags: int = 0
    ) -> tuple[int, ...]:
        return self.list_entry(0, address, count, epoch, flags)[2:]

    def geometry(
        self,
        sq_n: int,
        cq_n: int,
        so_bytes: int,
        si_bytes: int,
        rec_bytes: int | None = None,
    ) -> dict:
        if rec_bytes is None:
            rec_bytes = self.rec_bytes
        if not (0 < sq_n < 1 << 16 and 0 < cq_n < 1 << 16):
            raise ValueError("ring depths are 1..65535")
        if so_bytes % self.line or si_bytes % self.line or not so_bytes or not si_bytes:
            raise ValueError(f"stdio rings are whole {self.line}-byte lines")
        sq = self.sq
        cq = sq + sq_n * self.sq_bytes
        so = -(-(cq + cq_n * self.cq_bytes) // self.line) * self.line
        si = so + so_bytes
        rec = si + si_bytes
        return {
            "sq": sq,
            "cq": cq,
            "so": so,
            "si": si,
            "rec": rec,
            "end": rec + cq_n * rec_bytes,
        }
