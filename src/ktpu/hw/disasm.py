"""Turn a control program back into something readable.

The orchestrator's command RAM is four 64-bit words per entry and the CU
instructions inside it are 288-bit flits split across five writes, so a wrong
field shifts everything below it and the program still looks plausible. Decoding
is the cheapest defence: it needs no hardware, and reading the listing catches
the class of bug that otherwise only shows up as a wrong number after a
simulation.
"""

from ktpu.hw import device as dev
from ktpu.hw.matmul import FLIT_BITS

OPS = {dev.OP_WR: "WR", dev.OP_POLL: "POLL", dev.OP_DONE: "DONE"}
CU_OPS = {1: "FILL", 2: "GEMM", 3: "DRAIN"}

# agent registers, offset from MAG_BASE
AGENT_REGS = {
    dev.A_PROG_DST: "PROG_DST",
    dev.A_PROG_LEN: "PROG_LEN",
    dev.A_PROG_KICK: "PROG_KICK",
    dev.A_PROG_CRED: "PROG_CRED",
    dev.A_PROG_BASE: "PROG_BASE",
    dev.A_PROG_STAT: "PROG_STAT",
}


def target(addr):
    """A symbolic name for an address the program writes or polls."""
    if addr & dev.MAG_BASE:
        off = addr - dev.MAG_BASE
        if off in AGENT_REGS:
            return f"MAG.{AGENT_REGS[off]}", None
        if dev.A_STAGE <= off < dev.A_STAGE + 0x1000:
            word = (off - dev.A_STAGE) // 8
            slot, w = divmod(word, dev.FLIT_WORDS)
            return f"MAG.STAGE[{slot}].w{w}", (slot, w)
        if dev.A_NODE <= off < dev.A_NODE + 0x1000:
            # the register is indexed {y,x}; spelling both out beats a bare
            # pair that reads as (x,y) to everyone who sees it
            idx = (off - dev.A_NODE) // 8
            return f"MAG.NODE[x={idx & 0xF},y={idx >> 4}]", None
        return f"MAG+{off:#x}", None
    if addr == dev.MO_CTRL:
        return "ORC.CTRL", None
    if addr == dev.MO_PC:
        return "ORC.PC", None
    if addr == dev.MO_CODE:
        return "ORC.CODE", None
    return f"{addr:#x}", None


def decode_flit(flit):
    """Unpack one CU instruction flit into its fields.

    Positions mirror mx_cluster_cu.v's decode, which is the only thing that
    makes them correct -- they are stated once there and mirrored here. They
    are the LOW bit of each field, because the widths are what shifted when `n`
    grew to sixteen bits and a top-bit-plus-width spelling hid that: it decoded
    the high half of `n` and read every field below it one slot high.
    """

    def bits(lo, width):
        return (flit >> lo) & ((1 << width) - 1)

    op = bits(252, 4)
    peers, npeer = bits(144, 24), bits(142, 2)
    return {
        "op": op,
        "op_name": CU_OPS.get(op, f"op{op}"),
        "addr": bits(218, 34),
        "n": bits(202, 16),
        "sel": bits(201, 1),
        "acc": bits(200, 1),
        "gm": bits(192, 8),
        "gn": bits(184, 8),
        "nk": bits(176, 8),
        "anchor": bits(168, 8),
        "npeer": npeer,
        "peers": [(peers >> (8 * i)) & 0xFF for i in range(npeer)],
        "preq": bits(141, 1),
        "eoff": bits(133, 8),
        "aoff": bits(125, 8),
        "boff": bits(117, 8),
        "emit": bits(116, 1),
        "fuse": bits(115, 1),
        "last": bits(FLIT_BITS - 29, 1),
    }


def describe_flit(f):
    """One line of plain English for a decoded CU instruction."""
    if f["op_name"] == "FILL":
        which = "B" if f["sel"] else "A"
        peers = (
            "  shared with "
            + ", ".join(f"(x={p & 0xF},y={p >> 4})" for p in f["peers"])
            if f["npeer"]
            else ""
        )
        return (
            f"FILL {which}  {f['n']} L1 entries from {f['addr']:#x} -> L1 "
            f"{f['eoff']}"
            + ("  pre-quantised" if f["preq"] else "  FP16, quantised on the way out")
            + peers
        )
    if f["op_name"] == "GEMM":
        return (
            f"GEMM  {f['gm']}x{f['gn']} sub-tiles over {f['nk']} K blocks, "
            f"A@{f['aoff']} B@{f['boff']}, anchor={f['anchor']}"
            + ("  accumulate" if f["acc"] else "  load")
            + (f"  emit -> {f['addr']:#x}" if f["emit"] else "")
        )
    if f["op_name"] == "DRAIN":
        return f"DRAIN {f['n']} sub-tiles to {f['addr']:#x}" + (
            "  (barrier; the sweep already wrote them)" if f["fuse"] else ""
        )
    return f"{f['op_name']} raw"


def listing(commands):
    """Annotated rows for the whole program, staged flits reassembled.

    ``commands`` maps index -> [op, addr, data, mask], as recorded by
    :class:`~ktpu.hw.device.RecordingTransport`.

    The staging window MAY be reused. Concurrent dispatch gives each cluster
    its own base slot, but the serial path stages every cluster into slots
    0..n-1 and dispatches before the next one overwrites them. So a flit is
    decoded where it is COMPLETED, walking the program in order -- decoding
    from a final snapshot of the window shows the last cluster's instructions
    on every cluster's rows, which looks right and is wrong.
    """
    staged = {}
    rows = []
    for idx in sorted(commands):
        op, addr, data, mask = commands[idx]
        name, slot_w = target(addr)
        if op == dev.OP_WR and slot_w:
            slot, w = slot_w
            if w == 0:
                staged[slot] = 0
            staged[slot] = staged.get(slot, 0) | (data & ((1 << 64) - 1)) << (w * 64)
        row = {
            "index": idx,
            "op": OPS.get(op, f"op{op}"),
            "target": name,
            "data": f"{data:#018x}",
            "note": "",
            "kind": "plain",
        }
        if op == dev.OP_DONE:
            row["target"] = "-"
            row["note"] = f"halt, report code {data:#06x}"
            row["kind"] = "done"
        elif op == dev.OP_POLL:
            row["note"] = f"wait until (value & {mask:#x}) == {data:#x}"
            row["kind"] = "poll"
        elif slot_w:
            slot, w = slot_w
            row["kind"] = "stage"
            if w == dev.FLIT_WORDS - 1:
                f = decode_flit(staged.get(slot, 0))
                row["note"] = describe_flit(f)
                row["kind"] = "stage_last"
            else:
                row["note"] = f"stage flit {slot}, word {w}"
        elif name.endswith("PROG_KICK"):
            row["note"] = "dispatch the staged program"
            row["kind"] = "kick"
        elif name.endswith("PROG_DST"):
            row["note"] = f"target cluster x={data & 0xF}, y={(data >> 4) & 0xF}"
        elif name.startswith("MAG.PROG"):
            row["note"] = f"= {data}"
        rows.append(row)
    return rows


def dispatches(commands):
    """The CU instructions, grouped by the dispatch that sent them.

    A group is the slot RANGE a kick names -- PROG_BASE to PROG_BASE+PROG_LEN
    -- not "everything staged since the last kick". Concurrent dispatch stages
    every cluster's program first and only then kicks them all, so grouping by
    what was staged most recently puts every flit under the first cluster and
    leaves the rest looking empty.
    """
    staged, groups = {}, []
    dst, base, length = None, 0, 0
    for idx in sorted(commands):
        op, addr, data, _ = commands[idx]
        if op != dev.OP_WR:
            continue
        name, slot_w = target(addr)
        if slot_w:
            slot, w = slot_w
            if w == 0:
                staged[slot] = 0
            staged[slot] = staged.get(slot, 0) | (data & ((1 << 64) - 1)) << (w * 64)
        elif name.endswith("PROG_DST"):
            dst = {"x": data & 0xF, "y": (data >> 4) & 0xF}
        elif name.endswith("PROG_BASE"):
            base = data
        elif name.endswith("PROG_LEN"):
            length = data
        elif name.endswith("PROG_KICK"):
            out = []
            for slot in range(base, base + length):
                if slot not in staged:
                    continue
                f = decode_flit(staged[slot])
                f["slot"] = slot
                f["text"] = describe_flit(f)
                out.append(f)
            groups.append({"cluster": dst, "base": base, "flits": out})
    return groups
