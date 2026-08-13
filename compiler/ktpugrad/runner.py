"""The seam: one function that tries the kernel library before tinygrad lowers.

`tinygrad.codegen.to_program` is the last place an ast is still an ast. Below it
the loop nest is linearized and rendered, and `Renderer.render` receives uops by
which point the matmul is an accumulator in a loop. `pm_compile` calls
`to_program` by name out of `engine.realize`'s globals, so replacing it there
needs no fork.

What we hand back is a PROGRAM whose BINARY holds a KEY rather than machine
code. `get_runtime` passes that key to :class:`KTPUProgram`, which looks the
matched kernel up and dispatches it. Every other device reaches tinygrad's own
`to_program` untouched.
"""

import time
from dataclasses import replace

import numpy as np
from tinygrad.device import Device
from tinygrad.engine import realize
from tinygrad.helpers import BEAM, NOOPT
from tinygrad.renderer import Estimates
from tinygrad.uop.ops import Ops, PatternMatcher, ProgramInfo, UOp

from kohakutpu import kernels as K
from kohakutpu import ops as _o
from ktpugrad.chain import Recipe, read_chain
from ktpugrad.device import NAME, WORD_BYTES, register
from ktpugrad.errors import KTPUUnsupported
from ktpugrad.generated import FOLDS, KERNELS
from ktpugrad.match import Copy, Match, match_ast
from ktpugrad.tiling import plan

FP16 = np.float16

#: Kernel name in a :class:`Match` to the library function that runs it. Which
#: tiling it takes is `tiling.plan`'s; whether the epilogue fuses is `relax`'s.
LIBRARY = {
    # A plain contraction is an OP, not a fused kernel -- `kohakutpu.ops`.
    "matmul": _o.matmul,
    "linear_add": K.linear_add,
    "linear_bias": K.linear_bias,
    "linear_add_gelu": K.linear_add_gelu,
    "linear_add_relu": K.linear_add_relu,
    "linear_add_silu": K.linear_add_silu,
    "linear_gate_add": K.linear_gate_add,
    "linear_gelu": K.linear_gelu,
    "linear_relu": K.linear_relu,
    "linear_scale": K.linear_scale,
    "linear_silu": K.linear_silu,
}


class LibraryRunner:
    """One matched library kernel, run where a rendered program would be.

    The operands come back to the host and the result goes out again: a tinygrad
    buffer is flat row-major fp16 and a kernel operand is in a device layout.
    That is one readback and one upload per kernel and it is this prototype's
    main cost.
    """

    def __init__(self, device: str, match: Match) -> None:
        self.match = match
        machine = Device[device].rt.machine
        # A PER-CHANNEL bias rides the resident tile and allocates nothing, so
        # it goes to `linear_bias` rather than being broadcast to full shape.
        name = match.kernel
        if name == "linear_add" and match.c is not None and match.c.order == "N":
            name = "linear_bias"
        self.channelwise = name == "linear_bias"
        self.kernel, self.knobs = plan(
            LIBRARY[name],
            match.m,
            match.n,
            machine.count("VC"),
            k=match.k,
            machine=machine,
        )
        if match.scale is not None:
            self.knobs["s"] = match.scale
        self.device = device
        self.buffers = tuple(match.buffers)
        moved = 2 * (match.m * match.k + match.k * match.n + match.m * match.n)
        self.name = f"ktpu {self.kernel.name} {match.m}x{match.k}x{match.n}"
        self.estimates = Estimates(
            ops=2 * match.m * match.n * match.k, lds=moved, mem=moved
        )

    def __call__(self, rt, addrs: list) -> None:
        """Run the kernel over the arena spans `addrs`, result first."""
        out, *held = addrs
        match = self.match
        got = self.kernel(
            *(rt.tensor(a) for a in self._operands(rt, held)), **self.knobs
        )
        _upload(rt, out, got.numpy(), match.m * match.n)

    def _operands(self, rt, held: list) -> list:
        """Each input span as the row-major fp16 array its kernel takes."""
        match = self.match
        out = [
            _download(rt, held[0], match.a_shape, match.a.order == "KM"),
            _download(rt, held[1], match.b_shape, match.b.order == "KN"),
        ]
        for at, addend in enumerate(match.addends, start=2):
            shape = addend.held(match.m, match.n)
            got = _download(rt, held[at], shape, False)
            if self.channelwise:
                # `linear_bias` reads the N values themselves; nothing to spread.
                out.append(np.ascontiguousarray(got.reshape(-1)))
                continue
            # A gate and a residual are as long as the result already; a bias
            # this kernel cannot read per-channel is spread to match.
            out.append(np.ascontiguousarray(np.broadcast_to(got, (match.m, match.n))))
        return out


class ChainRunner:
    """One GENERATED chain, where a rendered program would be.

    Not a library kernel and named as such. `match_ast` is asked first and a
    kernel it matches always wins, since those are hand-tuned and witnessed;
    this takes the expressions the library has no entry for.
    """

    def __init__(self, device: str, recipe: Recipe) -> None:
        self.recipe = recipe
        family = FOLDS if recipe.folds else KERNELS
        self.kernel = family[len(recipe.operands)]
        # A band steps one ROW when it reduces, so one instance takes the array.
        self.knobs = {"part": recipe.elems} if recipe.folds else {}
        self.device = device
        self.buffers = (recipe.out, *(s.index for s in recipe.operands))
        moved = 2 * recipe.elems * (len(recipe.operands) + 1)
        self.name = (
            f"ktpu {self.kernel.name} {len(recipe.nodes)} ops "
            f"{'x'.join(map(str, recipe.shape))}"
        )
        self.estimates = Estimates(
            ops=recipe.elems * len(recipe.nodes), lds=moved, mem=moved
        )

    def __call__(self, rt, addrs: list) -> None:
        """Run the chain over the arena spans `addrs`, result first."""
        out, *held = addrs
        args = [rt.tensor(a) for a in self._operands(rt, held)]
        got = self.kernel(*args, expr=self.recipe.root, **self.knobs)
        held_out = self._result(got)
        _upload(rt, out, held_out, held_out.size)

    def _operands(self, rt, held: list) -> list:
        """Each input span at the WALKED shape, spread on the host.

        An elementwise pass reads operands of one length, so a buffer the
        scheduler broadcast arrives here shorter and is widened, exactly as a
        `linear_add` bias is. A chain that folds reads its row-shaped operands
        and its already-folded ones the same way, since the second are simply
        the narrower broadcast.
        """
        out = []
        for addr, source in zip(held, self.recipe.operands):
            flat = _download(rt, addr, (source.elems, 1), False).reshape(source.held)
            out.append(np.ascontiguousarray(np.broadcast_to(flat, self.recipe.shape)))
        return out

    def _result(self, got):
        """The kernel's output as the shape tinygrad's buffer holds.

        `VRED` broadcasts a fold back across the row, so a reduction writes
        `[rows][cols]` where the buffer wants `[rows][1]` and the extra columns
        are copies. Taking one is layout, not arithmetic -- but it does mean a
        generated reduction reads back `cols` times what it needs.
        """
        held = got.numpy()
        return held if not self.recipe.folds else np.ascontiguousarray(held[..., :1])


class CopyRunner:
    """A whole-buffer copy between two spans of the same arena.

    THROUGH THE HOST: this machine has no instruction that moves memory, so a
    copy tinygrad asks for is a readback and an upload, the same cost
    `Runtime.convert` pays.
    """

    def __init__(self, device: str, copy: Copy) -> None:
        self.copy = copy
        self.device = device
        self.buffers = (copy.out, copy.src)
        moved = 2 * 2 * copy.elems
        self.name = f"ktpu copy {copy.elems}"
        self.estimates = Estimates(lds=moved, mem=moved)

    def __call__(self, rt, addrs: list) -> None:
        """Copy `src` into `out`, both whole spans of `copy.elems` fp16."""
        out, src = addrs
        rt.write(out, rt.read(src, _words(2 * self.copy.elems)))


class KTPUProgram:
    """What `get_runtime` hands back: one matched kernel, ready to dispatch.

    Stands where a compiled program object would. `lib` is not machine code but
    the key `to_program` filed the match under, since the kernel this runs was
    chosen while the ast was still an ast.
    """

    def __init__(self, dev, name: str, lib: bytes, *aux, runtimevars=None, prg=None):
        self.dev = dev
        self.runner = _plans.get(lib)
        if self.runner is None:
            raise KTPUUnsupported(
                f"no KTPU kernel was filed for {name!r}; this program did not "
                f"come through ktpugrad.runner.to_program"
            )

    def __call__(self, *addrs, global_size=None, local_size=None, vals=(), **kwargs):
        """Run the kernel over the arena spans it was matched against.

        `addrs` arrive in `ProgramInfo.globals` order -- sorted buffer index --
        and are reordered here into the order the kernel's ports take them.
        Returns the seconds the whole round trip took.
        """
        runner = self.runner
        at = {n: j for j, n in enumerate(sorted(set(runner.buffers)))}
        start = time.perf_counter()
        runner(self.dev.rt, [addrs[at[n]] for n in runner.buffers])
        return time.perf_counter() - start


def _words(nbytes: int) -> int:
    """`nbytes` rounded up to a whole transport word."""
    return -(-nbytes // WORD_BYTES) * WORD_BYTES


def _download(rt, addr: int, shape: tuple, transposed: bool):
    """One arena span as the row-major fp16 array a kernel takes.

    `shape` is the buffer's own order and `transposed` says the kernel wants the
    other one.
    """
    want = shape[0] * shape[1]
    held = np.frombuffer(rt.read(addr, _words(2 * want)), FP16)[:want]
    held = held.reshape(shape)
    return np.ascontiguousarray(held.T if transposed else held)


def _upload(rt, addr: int, held, want: int) -> None:
    """Write `held` back to `addr`, padded to a whole transport word."""
    blob = np.ascontiguousarray(held, FP16).tobytes()[: 2 * want]
    rt.write(addr, blob.ljust(_words(len(blob)), b"\0"))


#: `ast.key` of every kernel matched so far, to the runner that will run it.
_plans: dict = {}
_pinned: tuple | None = None

#: Where :func:`capture` collects asts while it is running, else None.
_recording: list | None = None

#: tinygrad's own, kept so every other device still reaches it.
_TO_PROGRAM = realize.to_program


def get_runner(device: str, ast):
    """The runner for `ast`: a matched library kernel, else a generated chain.

    A MATCHED kernel always wins -- those are hand-tuned and witnessed. Raises
    :class:`ChainRefused` for an ast neither will spell.
    """
    match = match_ast(ast)
    if match is None:
        return ChainRunner(device, read_chain(ast))
    if isinstance(match, Copy):
        return CopyRunner(device, match)
    return LibraryRunner(device, match)


def to_program(ast, renderer):
    """tinygrad's `to_program`, diverted to the kernel library for KTPU.

    Only KTPU is diverted: this stands in for a module global that EVERY
    device's lowering goes through, so anything else must reach tinygrad's own
    untouched. Returns a PROGRAM carrying the matched kernel's key.
    """
    if _recording is not None:
        _recording.append(ast)
    if getattr(renderer.target, "device", None) != NAME:
        return _TO_PROGRAM(ast, renderer)
    key = ast.key
    if key not in _plans:
        _plans[key] = get_runner(renderer.target.device, ast)
    runner = _plans[key]
    sink = ast.replace(arg=replace(ast.arg, estimates=runner.estimates))
    return UOp(
        Ops.PROGRAM,
        src=(
            sink,
            UOp(Ops.DEVICE, arg=renderer.target.device),
            UOp(Ops.LINEAR),
            UOp(Ops.SOURCE, arg=runner.name),
            UOp(Ops.BINARY, arg=key),
        ),
        arg=ProgramInfo.from_sink(sink),
    )


def capture(run, strict: bool = True) -> list:
    """Every ast `run()` hands to lowering, in order.

    0.13 has no `Tensor.schedule`: an ast exists only where `to_program` is
    called with one. This is how a test or a demo sees WHICH kernels an
    expression scheduled, rather than only what it computed.

    With `strict=False` a refusal is swallowed and the asts collected so far
    are returned -- the ast is handed over BEFORE the kernel is chosen, so an
    expression this backend declines still has one to look at.

    Records through the INSTALLED seam rather than swapping the global again --
    `PatternMatcher` compiles its patterns lazily, so a second swap can bind a
    stale snapshot and the recorder silently sees nothing.
    """
    global _recording
    if realize.to_program is not to_program:
        raise KTPUUnsupported(
            "ktpugrad.capture reads the installed seam; call ktpugrad.install() "
            "first"
        )
    held, _recording = _recording, []
    try:
        try:
            run()
        except KTPUUnsupported:
            if strict:
                raise
        return _recording
    finally:
        _recording = held


#: `pm_compile`'s patterns as tinygrad built them. Rebuilding from a REBOUND
#: matcher re-snapshots its private dict and the seam silently stops firing.
_PATTERNS = realize.pm_compile.patterns


def _rebind() -> None:
    """Rebuild `pm_compile` so its lambda sees the current `to_program`.

    `PatternMatcher` COPIES the globals its patterns name into a private dict
    and rebinds each function to it, so assigning the module global alone
    changes nothing. Always rebuilt from :data:`_PATTERNS`, whose functions
    still resolve against the real module.
    """
    realize.pm_compile = PatternMatcher(_PATTERNS)


def install() -> None:
    """Register KTPU with tinygrad and route lowering through the matcher.

    Idempotent. Pins `NOOPT` and `BEAM` as well: every `OptOps` is a loop
    transform over generated code and this backend generates none, and beam
    search times its candidates by RUNNING them.
    """
    global _pinned
    register()
    if _pinned is None:
        _pinned = (NOOPT.value, BEAM.value)
    NOOPT.value, BEAM.value = 1, 0
    realize.to_program = to_program
    _rebind()


def uninstall() -> None:
    """Put tinygrad's own `to_program` back and unpin its knobs.

    The device stays registered: nothing else answers to the name, so leaving it
    costs nothing and taking it away would strand any buffer still allocated.
    """
    global _pinned
    if _pinned is not None:
        NOOPT.value, BEAM.value = _pinned
        _pinned = None
    realize.to_program = _TO_PROGRAM
    _rebind()
