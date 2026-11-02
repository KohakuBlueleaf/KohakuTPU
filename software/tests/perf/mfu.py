"""MFU of a step, per kernel and whole: the share of a machine resource spent on math.

Matrix and vector MFU are a kernel's math-only floors, in cycles with all units of the
class running. Memory MFU is the bytes the math must move, each operand read once and
each result written once a launch, against what the node's read and write paths carry
together. Form changes count as no useful work on all three.
"""

import dataclasses
import json
import pathlib
import re

#: A launch's kinds that only change a tensor's form.
FORM_KINDS = ("quant", "convert", "transpose", "copy", "move")
#: Bytes of an element in memory: MXFP7 is 128 B a 4 x 32 K entry.
ELEMENT_BYTES = {"f16": 2, "f32": 4, "mx7a": 1, "mx7b": 1}
#: A cluster MAC is a multiply and an add; a vector math beat is one op on 16 lanes.
FLOPS_MAC = 2
FLOPS_BEAT = 16
PARAM = re.compile(r"(\w+): (\w+)\[([^\]]*)\] @(\w+)")
PARAMS = pathlib.Path(__file__).with_name("params.json")


def card_buses() -> tuple[float, float]:
    """Read and write bytes a node cycle on the card, from the memory anchors."""
    params = json.loads(PARAMS.read_text("utf-8"))
    return params["read_bus"]["card"], params["write_bus"]["card"]


def useful_bytes(l2: str) -> int:
    """DRAM bytes of a kernel's parameters, from its L2 signature."""
    head = next(line for line in l2.splitlines() if line.startswith("fn "))
    total = 0
    for _, kind, shape, space in PARAM.findall(head):
        if space == "dram":
            count = 1
            for extent in shape.split(","):
                count *= int(extent)
            total += count * ELEMENT_BYTES[kind]
    return total


@dataclasses.dataclass
class Kernel:
    """One kernel's launches in a step."""

    name: str
    kind: str
    launches: int = 0
    cycles: int = 0
    mg: float = 0.0
    vc: float = 0.0
    useful: int = 0
    moved: float = 0.0
    flops: float = 0.0

    def row(self, peak: float) -> dict:
        """The kernel's own MFU: its floors and bytes over its run cycles."""
        return dict(
            dataclasses.asdict(self),
            mfu_mg=self.mg / self.cycles,
            mfu_vc=self.vc / self.cycles,
            mfu_mem=self.useful / (self.cycles * peak),
            bus=self.moved / (self.cycles * peak),
        )


def kernels(step: list[dict], timed: dict, programs: dict) -> dict[str, Kernel]:
    """A step's rows by kernel. A row with no timed record or program is a broken
    trace, so it raises rather than counting as no work."""
    out: dict[str, Kernel] = {}
    for row in step:
        name = row["name"]
        k = out.setdefault(name, Kernel(name, row["kind"]))
        t = timed[name]
        k.launches += 1
        k.cycles += row["cycles"]
        work = t["work"]
        k.moved += work["read_bytes"] + work["write_bytes"] + work["mover_bytes"]
        if row["kind"] not in FORM_KINDS:
            k.mg += t["floor"]["mg"]
            k.vc += t["floor"]["vc"]
            k.useful += useful_bytes(programs[name])
            k.flops += FLOPS_MAC * work["macs"] + FLOPS_BEAT * work["math_beats"]
    return out


def whole(by_kernel: dict[str, Kernel], span: int, peak: float) -> dict:
    """The step's MFU over `span` node cycles."""
    rows = list(by_kernel.values())
    return dict(
        mfu_mg=sum(k.mg for k in rows) / span,
        mfu_vc=sum(k.vc for k in rows) / span,
        mfu_mem=sum(k.useful for k in rows) / (span * peak),
        bus=sum(k.moved for k in rows) / (span * peak),
        flops=sum(k.flops for k in rows),
    )
