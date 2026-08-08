"""What a Program costs: instructions, memory, DRAM traffic, useful work.

Every number here is counted from the instruction stream, not estimated, so a
change that adds a load or a region shows up immediately. The one figure worth
watching is `overhead`: bytes moved per useful arithmetic operation. Anything
that raises it bought nothing.
"""

from dataclasses import dataclass

from ktpu.ir.program import Opcode, Program
from ktpu.ir.sched import Engine, Schedule
from ktpu.target import Target

#: Bits per element in memory, by the dtype an instruction names.
WIDTH = {"fp16": 16, "acc24": 24, "e8m15": 24}


@dataclass
class Cost:
    """Counted, not modelled. `flops` is the arithmetic the KERNEL asked for."""

    insts: dict[str, int]
    nodes: dict[str, int]
    image_words: int
    region_words: dict[str, int]
    dram_read: int
    dram_write: int
    flops: int
    rounds: int
    vector_body: int

    @property
    def total_insts(self) -> int:
        return sum(self.insts.values())

    @property
    def dram_bytes(self) -> int:
        return (self.dram_read + self.dram_write) * 4

    @property
    def overhead(self) -> float:
        """DRAM bytes moved per useful flop. Lower is strictly better."""
        return self.dram_bytes / max(1, self.flops)

    def __str__(self) -> str:
        per = "  ".join(f"{k} {v}" for k, v in sorted(self.insts.items()))
        rows = [
            f"instructions   {self.total_insts:>10,}   ({per})",
            "  on nodes                  "
            + "  ".join(f"{k} {v}" for k, v in sorted(self.nodes.items())),
            (
                f"device image   {self.image_words:>10,} words"
                f"  = {self.image_words * 4 / 1024:,.0f} KiB"
            ),
            f"dram read      {self.dram_read:>10,} words",
            f"dram write     {self.dram_write:>10,} words",
            f"useful flops   {self.flops:>10,}",
            f"overhead       {self.overhead:>10.3f} dram bytes per flop",
            f"rounds         {self.rounds:>10,}",
            f"vector body    {self.vector_body:>10,} instructions inside the loop",
        ]
        return "\n".join(rows)


def _elems(fields: dict) -> int:
    return fields.get("n", 0)


def cost_of(prog: Program, sched: Schedule, t: Target) -> Cost:
    """Count what `prog` costs on `t`.

    DRAM traffic is counted from FILL and DRAIN (the cluster's own memory
    operations) and from every vector load and store, each weighted by the
    elements it moves and the width of the dtype it names. A broadcast load is
    counted at its REAL size -- one value per column is not a full operand, and
    counting it as one would hide the saving.

    `flops` is `2*m*k*n` per matmul band plus one per elementwise op per output
    element, which is what the kernel asked for and does not change when the
    schedule does. Returns a Cost.
    """
    insts: dict[str, int] = {}
    nodes: dict[str, set] = {}
    read = write = 0
    body = 0

    by_name = {b.name: b for b in sched.bands}
    vband = {}
    for b in sched.bands:
        if b.engine is Engine.VECTOR:
            vband[b.name] = b.tile.m * b.tile.n * b.grid.size

    loop = 0
    for i in prog.insts:
        insts[i.op.value] = insts.get(i.op.value, 0) + 1
        nodes.setdefault(i.engine.value, set()).add(i.node)
        w = WIDTH.get(i.fields.get("dtype", "fp16"), 16)
        match i.op:
            case Opcode.FILL:
                read += _elems(i.fields) * t.lanes * t.kblock * 16 // 32
            case Opcode.DRAIN:
                write += _elems(i.fields) * t.lanes * t.lanes * w // 32
            case Opcode.VLOOP:
                loop = i.fields["count"]
                body += i.fields["body"]
            case Opcode.VLD:
                n = loop * t.vlmax
                read += (n if i.fields.get("bcast", "elem") == "elem" else 0) * w // 32
            case Opcode.VST:
                write += loop * t.vlmax * w // 32
            case _:
                pass

    flops = 0
    for b in sched.bands:
        if b.engine is Engine.MATMUL:
            a = b.ops[0].attrs
            flops += 2 * a["m"] * a["k"] * a["n"]
        else:
            flops += len(b.ops) * vband.get(b.name, 0)

    del by_name
    return Cost(
        insts=insts,
        nodes={k: len(v) for k, v in nodes.items()},
        image_words=prog.memory.total,
        region_words={r.name: r.words for r in prog.memory.regions},
        dram_read=read,
        dram_write=write,
        flops=flops,
        rounds=len(prog.rounds),
        vector_body=body,
    )
