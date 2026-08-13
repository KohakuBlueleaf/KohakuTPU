"""A buffer held in more than one byte order, and the span that has to cover it.

`Compiled.sizes` has always priced a buffer at its WIDEST order, and the temps
have always been allocated that way -- but a RESULT was allocated at
`layouts[name]`, the order the kernel first writes it in. A conversion rewrites
a buffer where it lies, so a wider order after that ran off the end of the
result and into whatever the arena handed out next.

Nothing shipped could reach it: `TpuBackend._conversions` refuses a port needed
in two orders outright, so every library port has exactly one order -- asserted
at the bottom, because that is what makes this latent rather than fixed. The
backend here is the one that does not refuse, which is the only way to run the
case at all.
"""

import numpy as np
import pytest
from kohakuaccel.lang import iface
from kohakuaccel.lang.kernel import KernelError, kernel
from kohakuaccel.lang.record import active, units
from kohakuaccel.lifetime import AliasError
from kohakuaccel.machinespec import MachineSpec
from kohakuaccel.memory import Arena, Buffer
from kohakuaccel.rt import Runtime
from kohakutpu.model import SimDevice

from kohakutpu import kernels as K
from kohakutpu import ops as O

#: Every buffer in the toy kernels is one axis of this many elements.
ELEMS = 8


# ------------------------------------------------------------------- layouts
class Flat:
    """fp16, one element after the next."""

    key = "flat"

    def nbytes(self, shape) -> int:
        return 2 * int(np.prod(shape))

    def pack(self, array) -> bytes:
        return np.asarray(array, np.float16).tobytes()

    def unpack(self, raw: bytes, shape):
        return np.frombuffer(raw, np.float16).reshape(shape).copy()


class Padded:
    """fp16 with two bytes of slack after every element: twice :class:`Flat`."""

    key = "padded"
    step = 4

    def nbytes(self, shape) -> int:
        return int(np.prod(shape)) * self.step

    def pack(self, array) -> bytes:
        flat = np.asarray(array, np.float16).reshape(-1)
        out = bytearray(flat.size * self.step)
        for i, value in enumerate(flat):
            out[i * self.step : i * self.step + 2] = np.float16(value).tobytes()
        return bytes(out)

    def unpack(self, raw: bytes, shape):
        held = np.frombuffer(raw, np.uint8).reshape(-1, self.step)[:, :2]
        return held.copy().view(np.float16).reshape(shape)


FLAT, PADDED = Flat(), Padded()


# ------------------------------------------------------------------- backend
class Toy:
    """A backend whose statements are ``dst = f(src)`` and whose orders are given.

    `rewrites` are the conversions to plant, each ``(stage, name, before,
    after)``.
    """

    def __init__(self, *rewrites) -> None:
        self.rewrites = list(rewrites)

    def unit_of(self, kinds: set) -> str:
        return "U"

    def handle(self, port, knobs: dict) -> str:
        return port.name

    def layouts(self, compiled) -> dict:
        compiled.conversions = list(self.rewrites)
        held = {p.name: FLAT for p in compiled.signature.ports}
        return {**held, **dict.fromkeys(compiled.temps, FLAT)}

    def constants(self, compiled) -> dict:
        return {}

    def encode(self, compiled, stage, addrs: dict) -> dict:
        """One payload of `(kind, factor, dst, src)` for the single instance."""
        ops = [
            (
                s.kind,
                s.args.get("by", 1.0),
                _where(compiled, s.writes, addrs, stage.index),
                _where(compiled, s.reads[0], addrs, stage.index),
            )
            for s in stage.instances[0].stmts
        ]
        return {(0,): ops}


def _where(compiled, name: str, addrs: dict, at: int) -> tuple:
    """`name` as ``(address, shape, order)`` while stage `at` runs.

    A conversion is due BEFORE the stage it is recorded against, so one AT `at`
    has already happened by the time that stage reads.
    """
    order = compiled.layouts[name]
    for when, who, _, after in compiled.conversions:
        if who == name and when <= at:
            order = after
    return addrs[name], compiled.shape(name), order


# ------------------------------------------------------------------- runtime
class Value:
    """A device value backed by one span of the fake machine's memory."""

    def __init__(self, rt, shape, host=None) -> None:
        self.rt = rt
        self.shape = tuple(shape)
        self.host = None if host is None else np.asarray(host, np.float16)
        self.buffers: dict = {}

    @property
    def runtime(self):
        return self.rt

    def address(self, layout) -> int:
        got = self.buffers.get(layout.key)
        if got is None:
            got = self.rt.put(self.host, layout, self.shape)
            self.buffers[layout.key] = got
        return got.addr

    def claim(self, buffer: Buffer) -> "Value":
        self.buffers[buffer.layout.key] = buffer
        return self

    def numpy(self):
        if self.host is not None:
            return self.host
        return self.rt.get(next(iter(self.buffers.values())))


class Fake(Runtime):
    """A machine that is a `bytearray`, so an overrun lands on a real neighbour."""

    def __init__(self, size: int = 1 << 12, align: int = 16) -> None:
        machine = MachineSpec(name="toy", units={"U": ((0, 0),)}, inst_depth=64)
        super().__init__(machine, Arena(0, size, align=align), self)
        self.mem = bytearray(size)

    def write_block(self, addr: int, blob: bytes) -> None:
        self.mem[addr : addr + len(blob)] = blob

    def read_block(self, addr: int, nbytes: int) -> bytes:
        return bytes(self.mem[addr : addr + nbytes])

    def tensor(self, array) -> Value:
        held = np.asarray(array, np.float16)
        return Value(self, held.shape, held)

    def empty(self, shape: tuple, layout, nbytes: int = 0) -> Value:
        span = max(int(nbytes), layout.nbytes(shape))
        return Value(self, shape).claim(Buffer(self.alloc(span), tuple(shape), layout))

    def load(self, where: tuple):
        addr, shape, order = where
        return order.unpack(self.read_block(addr, order.nbytes(shape)), shape)

    def store(self, where: tuple, array) -> None:
        addr, _, order = where
        self.write_block(addr, order.pack(array))

    def dispatch(self, payloads: dict, unit: str, name="kernel", nodes=None, acks=None):
        for ops in payloads.values():
            for kind, by, dst, src in ops:
                held = self.load(src)
                if kind == "scale":
                    held = held * by
                elif kind == "add":
                    held = held + self.load(dst)
                self.store(dst, held)
        return 1


def _emit(kind: str, dst: str, src: str, **args) -> None:
    active().emit(kind, writes=dst, reads=(src,), dst=dst, src=src, **args)


@kernel(backend=Toy((1, "y", FLAT, PADDED)))
def widened_result(x=iface.In(ELEMS), y=iface.Out(ELEMS)):
    """``y = 3x``, with `y` rewritten from `Flat` into `Padded` in between."""
    t = active().declare("t", (ELEMS,))
    with units(1):
        _emit("copy", "y", "x")
        _emit("scale", t, "x", by=2.0)
    with units(1):
        _emit("add", "y", t)


@kernel(backend=Toy((1, "y", FLAT, PADDED), (2, "y", PADDED, FLAT)))
def round_tripped_result(x=iface.In(ELEMS), y=iface.Out(ELEMS)):
    """``y = 5x``, held in `Padded` for one stage and handed back in `Flat`.

    The case that separates the two questions: the span it needs is the padded
    one and the order it ends in is the flat one.
    """
    t = active().declare("t", (ELEMS,))
    with units(1):
        _emit("copy", "y", "x")
        _emit("scale", t, "x", by=2.0)
    with units(1):
        _emit("add", "y", t)
    with units(1):
        _emit("add", "y", t)


@kernel(backend=Toy((1, "x", FLAT, PADDED)))
def widened_operand(x=iface.In(ELEMS), y=iface.Out(ELEMS)):
    """The same rewrite aimed at the operand, whose bytes the CALLER owns."""
    t = active().declare("t", (ELEMS,))
    with units(1):
        _emit("scale", t, "x", by=2.0)
    with units(1):
        _emit("copy", "y", "x")


def _run(kern):
    """`kern` over ``[1..ELEMS]`` on a fresh fake machine. Returns ``(dev, out)``."""
    dev = Fake()
    return dev, kern(dev.tensor(np.arange(1, ELEMS + 1, dtype=np.float16)))


# --------------------------------------------------------------------- tests
def test_a_result_rewritten_wider_gets_a_span_for_the_widest_order():
    """The defect itself, at the arena: 16 bytes handed out for a 32-byte rewrite.

    The 16 past the end are the temp the allocator hands out next.
    """
    dev, out = _run(widened_result)
    addr = out.buffers[PADDED.key].addr
    assert dev.arena.live[addr] >= PADDED.nbytes((ELEMS,))
    assert dev.arena.live[addr] > FLAT.nbytes((ELEMS,))


def test_the_rewrite_does_not_reach_into_the_buffer_behind_it():
    """Element for element, which is what an overrun of the temp destroys."""
    _, out = _run(widened_result)
    want = np.arange(1, ELEMS + 1, dtype=np.float16) * 3
    assert np.array_equal(np.asarray(out.numpy()), want)


def test_a_result_is_labelled_with_the_order_it_ends_in():
    """Sizing alone is not enough to make a rewritten result readable.

    The caller decodes the buffer through the layout it is recorded under, and
    the conversion has moved every element since it was written.
    """
    _, out = _run(widened_result)
    assert set(out.buffers) == {PADDED.key}


def test_the_widest_and_the_final_order_are_separate_questions():
    """One sizes the span, the other labels the result. The code needs both."""
    dev = Fake()
    compiled = round_tripped_result.plan(dev.tensor(np.zeros(ELEMS, np.float16)))
    assert compiled.orders("y") == [FLAT, PADDED, FLAT]
    assert compiled.widest("y") is PADDED
    assert compiled.final("y") is FLAT
    assert compiled.sizes()["y"] == PADDED.nbytes((ELEMS,))


def test_a_result_that_ends_narrow_still_gets_the_widest_span():
    """The order a result ENDS in does not bound what it needed on the way.

    Sized by the final order this is 16 bytes for a 32-byte rewrite, and the
    aliasing guard catches it sitting on the temp rather than corrupting it.
    """
    _, out = _run(round_tripped_result)
    want = np.arange(1, ELEMS + 1, dtype=np.float16) * 5
    assert np.array_equal(np.asarray(out.numpy()), want)
    assert set(out.buffers) == {FLAT.key}

    short = Fake()
    short.empty = lambda shape, layout, nbytes=0: Value(short, shape).claim(
        Buffer(short.alloc(layout.nbytes(shape)), tuple(shape), layout)
    )
    with pytest.raises(AliasError, match="overwritten by"):
        round_tripped_result(short.tensor(np.arange(1, ELEMS + 1, dtype=np.float16)))


def test_an_operand_the_kernel_would_rewrite_is_refused():
    """The same defect on the other role, where this level CANNOT size the span.

    An operand's bytes are the caller's, sized for the order it arrived in.
    """
    dev = Fake()
    with pytest.raises(KernelError, match="rewritten into another byte order"):
        widened_operand(dev.tensor(np.zeros(ELEMS, np.float16)))


# ----------------------------------------------------------- what it costs us
LIBRARY = [
    ("silu", O.silu, [(64, 64)], {}),
    ("softmax", K.softmax, [(64, 64)], {}),
    ("rmsnorm", K.rmsnorm, [(64, 64), (64, 64)], {}),
    ("layernorm", K.layernorm, [(64, 64)] * 3, {}),
    ("group_norm", K.group_norm, [(64, 64)] * 3, {}),
    ("matmul", O.matmul, [(128, 128), (128, 128)], {}),
    ("batched_matmul", O.matmul, [(8, 64, 64), (64, 64)], {}),
    ("linear_silu", K.linear_silu, [(64, 64), (64, 64)], {}),
    ("attention", K.flash_attention, [(64, 64), (256, 64), (64, 256)], {"block": 64}),
    ("attention_batched", K.flash_attention, [(2, 64, 64)] * 3, {"block": 64}),
]


@pytest.mark.parametrize(
    ("name", "kern", "shapes", "knobs"), LIBRARY, ids=[r[0] for r in LIBRARY]
)
def test_no_shipped_port_is_held_in_more_than_one_order(name, kern, shapes, knobs):
    """WHY THE DEFECT IS LATENT, and why the fix costs no arena.

    Every port of every shipped kernel is held in exactly the order it was
    declared in, so its widest order IS its declared one and not one address
    moves. The day this fails, the sizing above is what stops it corrupting a
    neighbour, and the footprint changes by the difference.
    """
    dev = SimDevice(size=1 << 26)
    rng = np.random.default_rng(11)
    args = [dev.tensor(np.asarray(rng.normal(0, 1, s), np.float16)) for s in shapes]
    compiled = kern.plan(*args, **knobs)
    for port in compiled.signature.ports:
        orders = compiled.orders(port.name)
        assert len(orders) == 1, f"{name}: {port.name} is held as {orders}"
        assert compiled.widest(port.name) is compiled.layouts[port.name]
