"""The Rust stack (software/rust, crate ktpu): graph or L2 -> L2 passes -> L1
-> image schedule -> L0 packages -> verifier, each stage cached on disk."""

from typing import TypedDict

def library_key(source: str, machine_json: str, zero_sign: str | None = None) -> str:
    """Name-free exact graph key, target digest (machine and zero_sign) and
    stage versions as JSON."""

def library_machine(machine_json: str, zero_sign: str | None = None) -> str:
    """The target digest: the directory a key's entries live in."""

def library_get(
    root: str,
    source: str,
    machine_json: str,
    mutant: str | None = None,
    zero_sign: str | None = None,
) -> dict | None:
    """Load a verified relocatable artifact without compiling."""

def library_prebuild(
    root: str, source: str, machine_json: str, zero_sign: str | None = None
) -> dict:
    """Compile under `zero_sign` and store a missing exact-class artifact offline."""

def template_verify(
    packages: dict[int, bytes],
    tables: dict[int, str],
    bindings: dict[int, list[int]],
    machine_json: str,
    digest: str,
    interior: int = 16,
    cap: int = 65536,
    confined: bool = False,
    mode: str = "sample",
) -> str:
    """sample: coverage points; proof: PROVEN/certificate or REFUSED/reason JSON."""

__version__: str
#: The machine-file schema version this build reads (derived from the schema
#: definition). Importing a build that cannot read every machine file raises
#: ImportError naming each file and its error.
MACHINE_SCHEMA: str
#: The spec-vector mutants this build carries: every stage crate's, in a
#: build with the `mutants` feature (scripts/py/build_rs.py spec); none in
#: a production build.
MUTANTS: list[str]
SPEC_RUNNERS: list[str]
#: `compile`'s `zero_sign` when None ("native" or "preserve").
ZERO_SIGN: str
ZERO_SIGNS: list[str]

def machine_view(
    machine_json: str,
    nodes: int | None = None,
    units: dict | None = None,
    mutant: str | None = None,
) -> dict:
    """Topology, profiles, fetch port and staging capacity of a named machine."""

def stage_addr(
    machine_json: str,
    offset: int,
    mesh: int,
    mutant: str | None = None,
    *,
    nodes: int | None = None,
) -> int:
    """Global staging address; refuses an absent mesh or out-of-range offset."""

def global_addr(
    machine_json: str,
    base: int,
    mesh: int,
    mutant: str | None = None,
    *,
    nodes: int | None = None,
) -> int:
    """Global memory address; refuses an absent mesh or oversized local base."""

def spec_run(
    runner: str,
    input_json: str,
    machine: str | None = None,
    mutant: str | None = None,
    vector_id: str | None = None,
    expect_json: str | None = None,
) -> str:
    """JSON output, or {result, expect} with Rust supersession when an id is given."""

class Refused(ValueError):
    """A stage refused the kernel."""

    #: "graph", "passes", "lower", "schedule", "order" or "verify".
    stage: str
    #: The spec clause the kernel breaks ("L2.SPACE.008").
    clause: str
    #: The refusal's message without its clause (set by `mg_encode`,
    #: `sru_words`, `vc_encode` and `Cores.rewrite`).
    reason: str

def package_admit(
    package: bytes,
    machine_json: str,
    mesh: int = 0,
    bindings: list[int] | None = None,
    *,
    cached_context: bool = True,
) -> None:
    """Mandatory receiving-board conversion admission for bound packages."""

def package_signature(unit_words: list[int]) -> int:
    """The package format's signature of the node boot unit words."""

class Package(TypedDict):
    #: The L0 package, relocatable: no address in its bytes (`bind`).
    bytes: bytes
    #: The fn parameter each buffer binds, in buffer order.
    params: list[str]

class Finding(TypedDict):
    kind: str
    clause: str
    at: str
    text: str

class Verdict(TypedDict):
    #: A finding of a refused kind: the run is refused.
    refused: bool
    findings: list[Finding]

class Compiled(TypedDict):
    name: str
    #: L2 as given or traced, before the passes.
    l2: str
    #: L1 after the image scheduler.
    l1: str
    #: Each node's package, by mesh.
    packages: dict[int, Package]
    verdict: Verdict
    #: Why the build is not relocatable (STACK.RELOC), else None.
    bound: str | None

class RefusedItem(TypedDict):
    stage: str
    clause: str
    message: str

class RefusedResult(TypedDict):
    refused: RefusedItem

def template_compile(
    representative: str,
    sources: list[str],
    machine_json: str,
    parameter_rotations: bool = False,
    zero_sign: str | None = None,
) -> list[Compiled]:
    """Freeze image/retile choices; optionally declare rotations bind-time values."""

def template_width(
    width: int, kblock: int, max_block: int, policy: str = "pow2"
) -> int | None:
    """Compile-time width class; None denotes the wide class."""

def template_points(class_json: str) -> str:
    """Required differential basis, corner and interior points as JSON."""

class TemplateProof:
    """Opaque checker-safety capability; tied to fixed physical bindings and extents."""

    @property
    def certificate(self) -> str:
        """The whole-box argument as JSON (not a forgeable capability)."""

    def bind(self, params_json: str) -> bytes:
        """Bind any in-box point without compilation, checking or member byte proof."""

def template_certify(
    package: bytes,
    table: str,
    bindings: list[int],
    machine_json: str,
    cap: int = 65536,
    mesh: int = 0,
) -> TemplateProof:
    """Prove an explicit L0 box once, or raise REFUSED; no numerical equivalence claim."""

def template_certificate(package: bytes, table: str, machine_json: str) -> str:
    """Derive a field-by-field ISA confinement certificate for a template."""

def template_environment(
    package: bytes, bindings: list[int], machine_json: str, mesh: int = 0
) -> str:
    """Pairwise buffer overlap/alias relations assumed by coverage bindings."""

def template_derive(
    packages: list[bytes],
    params_json: str,
    class_json: str,
    machine_json: str,
    mesh: int = 0,
    values_json: str = "{}",
) -> dict:
    """Derive and sample-prove T1 metadata from precompiled packages."""

def template_values(recipes_json: str, shape_json: str) -> str:
    """Evaluate checked host arithmetic; does not compile or access packages."""

def template_prove(
    package: bytes,
    template_json: str,
    direct: bytes,
    params_json: str,
    bindings: list[int],
    machine_json: str,
    label: str,
    mesh: int = 0,
) -> None:
    """Refuse unless template bind and direct bind serialize byte-identically."""

def cache_root() -> str | None:
    """Where `compile`'s stage caches live by default: `KTPU_COMPILE_CACHE`
    (alias `KTPU_RCACHE`; empty: off, None), else build/rcache."""

def l2_of(graph_json: str, machine_json: str) -> str:
    """The tracer's graph (`Traced.graph` as JSON text) as L2 text, for the
    machine file `machine_json` (its text). Raises `Refused`."""

def debug_decode(region: bytes, machine_json: str) -> dict:
    """A node's debug region (the debug firmware's per-step trace and
    report) decoded for the machine file `machine_json` (its text). Raises
    `Refused` (clause DBG.API.002 or DBG.TRACE.002)."""

def compile(
    source: str,
    machine_json: str,
    name: str | None = None,
    cache_dir: str | None = None,
    fetch: bool = False,
    mutant: str | None = None,
    l1: bool = False,
    binds: dict[str, int] | None = None,
    passes: bool = True,
    exp2_ranges: str | None = None,
    zero_sign: str | None = None,
) -> Compiled:
    """`source` (L2 text, or the tracer's graph as JSON text) for the machine
    file `machine_json` (its text). `name`: the fn compiled (the first when
    None). `cache_dir`: the stage caches (None: build/rcache or KTPU_RCACHE;
    "": none). `fetch`: the packages fetch through the die's fetch port, as a
    card runs them. `mutant`: that spec-vector mutant on in every stage crate
    for this compile, no stage cache; `ValueError` on a build that does not
    carry it (`MUTANTS`). `l1`: `source` is L1 text built as written (no
    passes, no image scheduler; `name` required). `binds` (with `fetch`):
    the packages built at those parameter addresses, not relocatable; without
    it a build whose words hold an address outside its spans returns no
    packages and `bound` saying why (STACK.RELOC). `passes=False` (with
    `fetch`): the L2 lowered with no L2 -> L2 pass. `exp2_ranges` (with
    `fetch`): "off", "intervals" or "rowmax", what the vector search's range
    analysis may prove of EXP2 / EXP2D arguments (None: its default,
    "intervals"). `zero_sign`: "native" or "preserve", how the lowering
    moves a value it does not change (ISA.VC.058; None: `ZERO_SIGN`). Raises
    `Refused`."""

def bind(
    package: bytes,
    bindings: list[int],
    machine_json: str,
    mesh: int = 0,
    lower: bool = False,
    mutant: str | None = None,
    after: int | None = None,
    params: dict[str, int] | None = None,
    template_json: str | None = None,
    translations: list[tuple[int, int, int]] | None = None,
    count_path: str = "step",
    cores: Cores | None = None,
    images_json: str | None = None,
) -> bytes:
    """A relocatable package (`compile`'s) bound at `bindings` (one address a
    buffer), and with `lower` lowered for mesh `mesh`'s dispatch engine: the
    bytes a card runs. `template_json` is the host-only ParameterTable JSON:
    `parameters` maps names to inclusive {lo, hi} bounds; `sites` contains
    {name, payload, bit, width, shift=0, constant=0, coefficients={}, buffer=None}.
    Coefficients map parameter names to signed 64-bit integers; `params` supplies
    their signed 64-bit values. Missing, unknown or out-of-class values refuse.
    The unsigned affine result (plus the optional buffer base) must fit the
    segments; address offsets lie in [0, buffer.size). Raises `Refused` (stage
    "bind") naming the L0 clause, site and parameter values. Metadata is never
    emitted in the returned bytes; omitted metadata preserves ordinary binding.
    A site with step=i (payload=0) selects a whitelisted step count field.
    count_path='register' installs step values through r15 MOV/PATCH pairs;
    it refuses existing register/control-flow users and prevents engine lowering."""

def bind_cache_stats() -> dict:
    """Counts and sizes of the host bind and library caches."""

def bind_cache_configure(
    machines: int | None = None,
    templates: int | None = None,
    bound: int | None = None,
    library: int | None = None,
    clear: bool = False,
) -> None:
    """Configure or clear the host bind and library caches."""

def compile_many(
    sources: list[str],
    machine_json: str | list[str],
    cache_dir: str | None = None,
    threads: int | None = None,
    zero_sign: str | None = None,
) -> list[Compiled | RefusedResult]:
    """`compile` of every source in parallel (`threads`: None for one a
    CPU), on one machine or, given a list, source i on machine i; item i is
    source i's, the same whatever the thread count."""

# The bindings that replace the Python compiler's library calls.
# Each takes the machine file's text; `mutant` as `compile`'s.

class Cores:
    """Every vector core's resident programs and descriptors
    (`ktpu_l1::lower::imem`), sized by the machine (`vc.imem_words`,
    `vc.run_reach`), made on first use."""

    #: Instruction words a core holds.
    words: int
    def __init__(self, machine_json: str) -> None: ...
    def forget(self) -> None:
        """Assume nothing is resident on any core."""

    def digest(self) -> bytes:
        """sha256 of every core's whole state."""

    def snapshot(self) -> Cores:
        """A copy of every core's state, for `restore`."""

    def restore(self, snap: Cores) -> None: ...
    def resident(self, coord: tuple[int, int]) -> list[tuple[int, int]]:
        """`(base, length)` of each program the core holds, oldest use first."""

    def state(self, coord: tuple[int, int]) -> dict:
        """`slots`, `last`, `desc` (`[[ad, field], [value, value_hi]]`) and
        `mem` (`[address, hex word]`) of the core, both sorted."""

    def rewrite(
        self, coord: tuple[int, int], words: list[int], mutant: str | None = None
    ) -> list[int]:
        """One RUN-terminated kernel's CU payloads against what the core
        holds. Raises `Refused` (L1.LOW.003, L1.LOW.014)."""

def sru_config(machine_json: str, mutant: str | None = None) -> dict:
    """The SRU configuration map (`G`, `P`, `O`, `F`, the source and
    finalise codes) with its depths and pool timing, under `isa/sru.py`'s
    names. Raises `Refused` on a die without SR units."""

def sru_pack(
    fields: dict[str, tuple[int, int, int]], values: dict[str, int], machine_json: str
) -> dict[int, int]:
    """Pack configuration fields into checked 32-bit words."""

def sru_schedule(
    program_json: str, f_lanes: int, h_taps: int, machine_json: str
) -> str:
    """JSON [op fields, initiation interval, issue slots] for the machine pool."""

def sru_words(
    job_json: str,
    f_lanes: int,
    h_taps: int,
    machine_json: str,
    mutant: str | None = None,
) -> list[tuple[int, int]]:
    """`(address, value)` of every configuration word of the job (`{"g",
    "passes", "ops", "fin"}`), its programs scheduled on `f_lanes` lanes and
    `h_taps` result taps. Raises `Refused`."""

def mg_encode(
    op: str, fields: dict[str, int], machine_json: str, mutant: str | None = None
) -> int:
    """The cluster payload of `op` (FILL, GEMM, DRAIN, POST) with `fields`,
    an `addr` split. Raises `Refused` (a FILL past `mg.max_fill`)."""

def vc_encode(
    words_json: str, machine_json: str, mutant: str | None = None
) -> list[int]:
    """Vector core words (`VWord`s as JSON) encoded for the machine's core.
    Raises `Refused`."""

def package_build(
    builder_json: str,
    calls_json: str,
    machine_json: str,
    mesh: int = 0,
    defaults: bool = True,
    checksum: bool = False,
    lower: bool = False,
    mutant: str | None = None,
) -> bytes:
    """An L0 package from `PackageBuilder` calls: the builder as `{"types":
    [[x, y, name]], "credit", "mesh", "signature", "fetch"}`, each call
    `[name, *args, {kw}]`; with `lower`, lowered for the dispatch engine.
    Raises `Refused`, or ValueError on a call that does not parse."""

def package_read(
    package: bytes,
    machine_json: str,
    mesh: int = 0,
    bindings: list[int] | None = None,
    mutant: str | None = None,
) -> dict:
    """The package's tables as ints (`units`, `buffers`, `steps`, `relocs`,
    `payloads`, `signature`, `ack_reserve`, `checksum`), each ENGINE step's
    `(code, value)` entries by step index (`engines`), and with `bindings`
    the payloads relocated (`bound`, else None). Raises `Refused`."""

def package_stream(
    package: bytes,
    entries_json: str,
    machine_json: str,
    mesh: int = 0,
    mutant: str | None = None,
) -> bytes:
    """`package` with its steps replaced by one ENGINE step of `entries`
    (`[["put", code, value] | ["wait", source, count]]`, a WAIT of no
    positive count left out), their payloads appended. Raises `Refused`."""

def package_format(machine_json: str, mesh: int = 0) -> dict:
    """The codes a package reader names: `op`, `alu`, `cond` (name -> code),
    the `F_*` step flags, `REGS`, `MOVER_SKIP`, `ANY_UNIT`, the dispatch
    engine's register codes, `SRC_MOVER`, `CTR_MASK` and `PAYLOAD` bytes."""
