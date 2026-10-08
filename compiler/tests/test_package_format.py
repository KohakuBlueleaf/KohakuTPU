"""The package format: byte layout, relocation, reuse, and the reference run.

The layout is witnessed against bytes packed HERE, field by field from the
spec (docs/spec/package-format.md), not by a round trip through format.py --
a round trip passes when writer and reader are wrong together. The firmware's
constants are read out of its C header and compared, since nothing else ties
the two copies of the table.
"""

import hashlib
import pathlib
import re
import struct

import numpy as np
import pytest
from kohakuaccel.artifact import Artifact, Await, Barrier, Kick, SeedCredits
from kohakuaccel.backend.slots import Prebuilt
from kohakuaccel.compile import compile
from kohakuaccel.ir.l2 import Policy, ScheduleIR
from kohakuaccel.machinespec import MachineSpec
from kohakuaccel.package import format as F
from kohakuaccel.package.build import PackageBuilder
from kohakuaccel.package.interp import Interpreter, LocalNode
from kohakuaccel.package.lower import machine_units
from kohakuaccel.sim.mailbox import SimMailbox
from kohakutpu.hw.vector import desc_flit
from kohakutpu.isa import ISA
from kohakutpu.isa.fields import FIELDS, cluster_addresses, vector_addresses
from kohakutpu.model import SimDevice, run_move

from kohakutpu import ops

ROOT = pathlib.Path(__file__).resolve().parents[2]
HEADER_H = ROOT / "firmware/kohakuaccel/include/ka/package/format.h"


def c_defines(path: pathlib.Path) -> dict:
    """`#define NAME value` and `NAME = value,` pairs of a C header, as ints."""
    text = path.read_text()
    out = {}
    for name, val in re.findall(r"#define\s+(\w+)\s+(0x[0-9A-Fa-f]+|\d+)", text):
        out[name] = int(val, 0)
    for name, val in re.findall(r"\b(KA_\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*,", text):
        out[name] = int(val, 0)
    return out


# ------------------------------------------------------------------ the table
def test_python_and_firmware_tables_agree():
    c = c_defines(HEADER_H)
    assert c["KA_PKG_MAGIC"] == F.MAGIC
    assert c["KA_PKG_VERSION"] == F.VERSION
    assert c["KA_PKG_HEADER"] == F.HEADER_BYTES
    assert c["KA_PKG_UNIT_BYTES"] == F.UNIT_BYTES
    assert c["KA_PKG_BUF_BYTES"] == F.BUFFER_BYTES
    assert c["KA_PKG_STEP_BYTES"] == F.STEP_BYTES
    assert c["KA_PKG_REL_BYTES"] == F.RELOC_BYTES
    assert c["KA_PKG_PAY_BYTES"] == F.PAYLOAD_BYTES
    assert c["KA_PH_CHECK"] == F.CHECK_WORD
    for op in F.Op:
        assert c[f"KA_OP_{op.name}"] == op.value, op


def test_fnv1a_known_vectors():
    # FNV-1a 64 reference values (the algorithm's published test vectors).
    assert F.fnv1a(b"") == 0xCBF29CE484222325
    assert F.fnv1a(b"a") == 0xAF63DC4C8601EC8C
    assert F.fnv1a(b"foobar") == 0x85944171F73967E8


def _tiny() -> F.Package:
    return F.Package(
        units=[
            F.Unit(F.type_code("MG"), 1, 1, 0, 512),
            F.Unit(F.type_code("VC"), 1, 0, 0, 512),
        ],
        buffers=[F.Buffer("a", 4096, 0x40_0000, F.Kind.INPUT)],
        steps=[
            F.Step(F.Op.DISPATCH, 0, 2, 0),
            F.Step(F.Op.AWAIT, 0, 2),
            F.Step(F.Op.BARRIER),
        ],
        relocs=[F.Reloc(1, 212, 34, 0, 0x40, 0), F.Reloc(1, 63, 6, 0, 0x40, 34)],
        payloads=[0x1234, 0xABCD << 200],
        signature=0x1122334455667788,
        ack_reserve=3,
    )


def test_layout_against_bytes_packed_from_the_spec():
    got = _tiny().to_bytes()
    # Section offsets per the spec: header 96, then 32-aligned sections.
    units = 96
    bufs = units + 32  # 2 units x 16
    steps = bufs + 32  # 1 buffer x 32
    rels = steps + 64  # 3 steps x 16 = 48 -> 64
    pays = rels + 32  # 2 relocs x 16
    total = pays + 64
    want = bytearray(total)
    struct.pack_into(
        "<12Q",
        want,
        0,
        0x4B50414B | 1 << 32 | 96 << 48,
        0x1122334455667788,
        total,
        2 | 2 << 32,
        1 | 3 << 16 | 2 << 32 | 3 << 48,
        units,
        bufs,
        steps,
        rels,
        pays,
        0,
        0,
    )
    struct.pack_into("<QQ", want, units, 0x4D47 << 32 | 1 << 8 | 1, 512)
    struct.pack_into("<QQ", want, units + 16, 0x5643 << 32 | 0 << 8 | 1, 512)
    struct.pack_into("<QQQQ", want, bufs, F.fnv1a(b"a"), 4096, 0x40_0000, 0)
    struct.pack_into("<QQ", want, steps, 1 | 0 << 16 | 2 << 32, 0)
    struct.pack_into("<QQ", want, steps + 16, 2 | 2 << 32, 0)
    struct.pack_into("<QQ", want, steps + 32, 3, 0)
    struct.pack_into("<QQ", want, rels, 1 | 63 << 32 | 6 << 40 | 34 << 48, 0x40)
    struct.pack_into("<QQ", want, rels + 16, 1 | 212 << 32 | 34 << 40, 0x40)
    want[pays : pays + 32] = (0x1234).to_bytes(32, "little")
    want[pays + 32 : pays + 64] = (0xABCD << 200).to_bytes(32, "little")
    assert got == bytes(want)
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(bytes(want)).hexdigest()


def test_parse_reads_what_the_spec_wrote():
    p = F.Package.from_bytes(_tiny().to_bytes())
    t = _tiny()
    assert [u.word for u in p.units] == [u.word for u in t.units]
    assert p.steps == t.steps and p.payloads == t.payloads
    assert sorted(p.relocs, key=lambda r: r.bit) == sorted(
        t.relocs, key=lambda r: r.bit
    )
    assert p.signature == t.signature and p.ack_reserve == 3


def test_checksum_catches_one_flipped_bit():
    pkg = _tiny()
    pkg.checksum = True
    raw = bytearray(pkg.to_bytes())
    F.Package.from_bytes(bytes(raw))
    raw[-1] ^= 0x10
    with pytest.raises(F.PackageError, match="checksum"):
        F.Package.from_bytes(bytes(raw))


def test_malformed_packages_are_refused():
    raw = bytearray(_tiny().to_bytes())
    with pytest.raises(F.PackageError):
        F.Package.from_bytes(bytes(raw[:64]))
    bad = bytearray(raw)
    bad[0] ^= 1
    with pytest.raises(F.PackageError, match="magic"):
        F.Package.from_bytes(bytes(bad))
    with pytest.raises(F.PackageError):
        F.Package(steps=[F.Step(F.Op.DISPATCH, 0, 1, 0)]).to_bytes()


def test_signature_is_order_free_and_unit_sensitive():
    a = [F.unit_word(F.type_code("MG"), 1, 1), F.unit_word(F.type_code("VC"), 1, 0)]
    assert F.signature(a) == F.signature(a[::-1])
    assert F.signature(a) != F.signature(a[:1])
    want = F.fnv1a(b"".join(struct.pack("<Q", w) for w in sorted(a)))
    assert F.signature(a) == want


# ------------------------------------------------------------ relocation
def test_split_fields_are_found_where_the_encoders_put_them():
    """The fields reader against the ENCODERS, the independent side."""
    addr = (2 << 36) | 0x0123_4560
    fill = ISA.fill(addr=addr, n=4, sel=1)
    [(segs, value)] = cluster_addresses(fill)
    assert value == addr
    cleared = fill
    for bit, width, _ in segs:
        cleared &= ~(((1 << width) - 1) << bit)
    assert cleared == ISA.fill(addr=0, n=4, sel=1)
    desc = desc_flit(1, 0, addr) & ((1 << 256) - 1)
    [(segs, value)] = vector_addresses(desc)
    assert value == addr
    assert vector_addresses(desc_flit(1, 2, 77) & ((1 << 256) - 1)) == []
    assert cluster_addresses(ISA.gemm(gm=1, gn=1, nk=1)) == []


def test_relocated_words_bind_back_to_what_was_encoded():
    words = [
        ISA.fill(addr=0x40_0040, n=4),
        ISA.drain(addr=0x50_0100, n=2),
        desc_flit(0, 0, 0x40_0800) & ((1 << 256) - 1),
    ]
    b = PackageBuilder(types={(1, 1): "MG", (1, 0): "VC"})
    b.spans_from({0x40_0000: 0x1000, 0x50_0000: 0x1000})
    b.dispatch(b.unit((1, 1)), words[:2], FIELDS.addresses)
    b.dispatch(b.unit((1, 0)), words[2:], FIELDS.addresses)
    pkg = b.build(defaults=False)
    assert all(
        not (p >> 212) & ((1 << 34) - 1) for p in pkg.payloads[2:]
    ), "vector base not cleared"
    assert pkg.bound(b.bindings()) == words
    moved = pkg.bound([0x80_0000, 0x90_0000])
    assert moved[0] == ISA.fill(addr=0x80_0040, n=4)
    assert moved[1] == ISA.drain(addr=0x90_0100, n=2)
    assert moved[2] == desc_flit(0, 0, 0x80_0800) & ((1 << 256) - 1)


# ------------------------------------------------------- the framework pipeline
def test_emit_awaits_acknowledgements_once_per_unit():
    s = ScheduleIR()
    s.task(
        "MG",
        payload=[1, 2],
        flits=2,
        signals=2,
        policy=Policy.PINNED,
        coord=(1, 1),
        acks=(((1, 0), 3),),
    )
    s.task(
        "MG",
        payload=[3],
        flits=1,
        signals=1,
        policy=Policy.PINNED,
        coord=(1, 1),
        acks=(((1, 1), 1),),
    )
    m = MachineSpec(
        units={"MG": ((1, 1),), "VC": ((1, 0),)}, stage_flits=64, ncmd=64, inst_depth=64
    )
    art = compile(s, m, Prebuilt()).artifact
    waits = [x for x in art.steps if isinstance(x, Await)]
    assert waits == [Await((1, 1), 4), Await((1, 0), 3)]
    assert art.flits == [1, 2, 3]


def test_artifact_lowering_keeps_order_and_reserves_ack_room():
    art = Artifact(
        flits=[10, 11, 12],
        steps=[
            SeedCredits(9),
            Kick((1, 1), 0, 2),
            Kick((1, 0), 2, 1),
            Await((1, 1), 2),
            Await((1, 0), 4),
            Barrier(),
        ],
    )
    b = PackageBuilder(types={(1, 1): "MG", (1, 0): "VC"})
    b.artifact(art)
    pkg = b.build()
    ops_ = [(s.op, s.unit, s.count, s.arg) for s in pkg.steps]
    assert ops_ == [
        (F.Op.DISPATCH, 0, 2, 0),
        (F.Op.DISPATCH, 1, 1, 2),
        (F.Op.AWAIT, 0, 2, 0),
        (F.Op.AWAIT, 1, 4, 0),
        (F.Op.BARRIER, 0, 0, 0),
    ]
    assert pkg.ack_reserve == 3
    assert pkg.payloads == [10, 11, 12]


# ---------------------------------------------------------- end to end, models
def _device(node: bool) -> SimDevice:
    dev = SimDevice(mg=((1, 1),), vc=((1, 0),), agent=(0, 1))
    if node:
        dev.node = LocalNode(
            SimMailbox(dev.card),
            machine_units(dev.machine),
            mover=lambda wr: run_move(wr, dev.card.mem),
        )
        dev.fields = FIELDS
        dev.keep_packages = True
    return dev


def test_node_dispatch_matches_host_dispatch_exactly():
    rng = np.random.default_rng(3)
    a = rng.standard_normal((32, 64)).astype(np.float16)
    w = rng.standard_normal((32, 64)).astype(np.float16)
    x = rng.standard_normal(1024).astype(np.float16)
    got = {}
    for node in (False, True):
        dev = _device(node)
        got[node] = (
            ops.matmul(dev.tensor(a), dev.tensor(w)).numpy(),
            ops.residual(dev.tensor(x), dev.tensor(x)).numpy(),
        )
    for host, nodes in zip(got[False], got[True], strict=True):
        assert np.array_equal(host, nodes)


def test_one_package_serves_calls_at_different_addresses():
    """Relocation, witnessed by RESULTS: the second call's operands are elsewhere."""
    dev = _device(True)
    rng = np.random.default_rng(4)
    held, outs, refs = [], [], []
    for _ in range(3):
        a = rng.standard_normal((32, 64)).astype(np.float16)
        w = rng.standard_normal((32, 64)).astype(np.float16)
        ta, tw = dev.tensor(a), dev.tensor(w)
        held += [ta, tw]
        outs.append(ops.matmul(ta, tw).numpy())
        host = _device(False)
        refs.append(ops.matmul(host.tensor(a), host.tensor(w)).numpy())
    ran = dev.node.ran
    assert len({pkg for pkg, _ in ran}) == 1, "the three calls built different packages"
    assert len({tuple(b) for _, b in ran}) == 3, "the bindings never moved"
    for o, r in zip(outs, refs, strict=True):
        assert np.array_equal(o, r)


class _CountingMailbox(SimMailbox):
    """Holds completions back to see the credit bound bite."""

    def __init__(self, machine) -> None:
        super().__init__(machine)
        self.most = 0
        self.held: list = []

    def send(self, x, y, payload):
        before = len(self.queue)
        super().send(x, y, payload)
        self.held += self.queue[before:]
        self.queue = self.queue[:before]
        self.most = max(self.most, len(self.held))

    def drain(self):
        out, self.held = self.held[:1], self.held[1:]
        return out


def test_reference_interpreter_keeps_the_mailbox_bound():
    dev = _device(False)
    mb = _CountingMailbox(dev.card)
    x = np.ones(1024, np.float16)
    node = LocalNode(
        mb,
        machine_units(dev.machine),
        cq_depth=16,
        mover=lambda wr: run_move(wr, dev.card.mem),
    )
    dev.node, dev.fields = node, FIELDS
    ops.residual(dev.tensor(x), dev.tensor(x)).numpy()
    assert mb.sent > 16
    assert mb.most <= 16


def test_a_wrong_signature_is_refused_before_anything_is_sent():
    dev = _device(False)
    mb = SimMailbox(dev.card)
    interp = Interpreter(mb, machine_units(dev.machine))
    b = PackageBuilder(types={(1, 1): "MG"}, signature=0xBAD)
    b.dispatch(b.unit((1, 1)), [ISA.gemm(gm=1, gn=1, nk=1)])
    res = interp.run(b.build())
    assert res.status == 0x12 and mb.sent == 0
