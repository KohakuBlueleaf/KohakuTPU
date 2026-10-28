"""Every hardware fact the stack reads: one row each, with where it comes from.

A row is (dotted path, reader). The reader takes the image's sources
(read.image.Image) and returns (value, source). Adding a fact is one row;
the stack reads it by its path, never by a constant of its own.
"""

import json
import re

from .read.image import ROOT as ROOT_PATH
from .read.image import _firmware_build, rel

#: CU_SIGNAL codes, CU_CAPS fields and mover modes are RTL decodes with no
#: parameter; the rows below cite the decoding lines.
MOVER_MODES = {
    "copy": 0,
    "transpose": 1,
    "gather": 2,
    "generate": 3,
    "fill": 4,
    "transform": 5,
    "scatter": 6,
}
VC_XB = ["none", "xor", "rot", "bcast", "merge"]
VC_PACK = ["f16", "gt4", "f32", "mx7"]

#: The RTL files the rows read beyond the die's top and the vector core.
CLUSTER = "src/kohakutpu/matmul/mx_cluster_cu.v"
CLUSTER_MGR = "src/kohakutpu/matmul/mx_cluster_mgr_pump.v"
CLUSTER_NODE = "src/kohakutpu/matmul/mx_cluster_node_pump.v"
VEC_CU = "src/kohakutpu/vector/vec_cu.v"
VEC_CORE = "src/kohakutpu/vector2/v2_core.v"
VEC_AGU = "src/kohakutpu/vector2/v2_agu.v"
VEC_LANES = "src/kohakutpu/vector2/v2_lanes.v"
MOVER = "src/kohakuaccel/sysnode/mover/mm_mover.v"
XFORM = "src/kohakutpu/transform/xform_bank.v"
QUANT = "src/kohakutpu/transform/mx_quant.v"
SYSCORE = "src/kohakuaccel/pe/rv64-sys/rv64_syscore.v"
MBOX = "src/kohakuaccel/pe/rv64-sys/rv64_noc_mbox.v"
LOAD_WIN = "src/kohakuaccel/sysnode/cpu/rv64_load_win.v"
ILINK = "src/kohakuaccel/sysnode/interlink/mag_ilink.v"
SRU_CFG = "src/kohakutpu/sru/sru_cfg.vh"
SRU_CORE = "src/kohakutpu/sru/sru_core.v"
SRU_CU = "src/kohakutpu/sru/sru_cu.v"
#: The SRU pool's schedule timing (sru_pool.v's pipeline), as the map the
#: RTL header is generated from records it.
SRU_POOL = "src/kohakutpu/sru/pool/sru_pool.v"


def conversion(img, key):
    """Board reliability constraints, not inferred from transform geometry."""
    path = ROOT_PATH / "scripts/py/machine/conversion.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    board = img.board_dict["name"]
    policy = doc["boards"].get(board, doc["default"])
    source = f"boards.{board}" if board in doc["boards"] else "default"
    return policy[key], f"scripts/py/machine/conversion.json:{source}.{key}"


def derived(value, note):
    return value, f"derived: {note}"


def dram_bytes(img):
    """Capacity from the card board, or the model build's per-channel depth."""
    if "model" in img.board_dict:
        model, src = img.board("model")
        return derived(
            model["dram_words"] * 64 * len(img.cfg.meshes()),
            f"{src}.dram_words * 64 bytes * build DRAM channels (gen_card.py)",
        )
    words, src = img.board("mem_words")
    return derived(words * 32, f"{src} * 32 bytes")


def dram_line_bytes(img):
    """Write beat of the Xache wrapper selected by this image's build."""
    build = f"scripts/tcl/multimesh_{img.ver}_bd.tcl"
    stages, build_src = img.grep(build, r"^set V8_STAGES \$here/(\w+)$")
    stage = f"scripts/tcl/{stages.group(1)}/35_xache.tcl"
    module, stage_src = img.grep(
        stage, r"create_bd_cell -type module -reference (\w+) xache"
    )
    wrapper = f"xilinx-fpga/xcvu13p/bd/{module.group(1)}.v"
    width, width_src = img.grep(wrapper, r"localparam integer .*\bW\s*=\s*(\d+)")
    bits = int(width.group(1))
    if bits <= 0 or bits % 8:
        raise ValueError(f"{width_src}: Xache W must be positive whole bytes")
    return derived(bits // 8, f"W / 8, {width_src}; {stage_src}; {build_src}")


def die_units(img, kind):
    return [
        list(p) for p in img.board_dict["units"].get(kind, [])
    ], f"boards/{img.board_dict['name']}.json:units.{kind}"


def die_target(img):
    """The IR target the die's programs are written for (RUL.010): `ktpu.`
    and the image's generation, the `vN` of its build's design name."""
    name, src = img.config("design_name")
    gen = re.search(r"_(v\d+)", name).group(1)
    return derived(f"ktpu.{gen}", f"ktpu.<vN of {name}>, {src}")


def unit_count(img, kind):
    units, src = die_units(img, kind)
    return derived(len(units), f"die.units.{kind.lower()} ({src})")


def field_max(width, note):
    """The largest value of a `width`-bit field read as (bits, source)."""
    bits, src = width
    return derived((1 << bits) - 1, f"2^{note} - 1, {src}")


def field_span(width, note):
    """The values a `width`-bit field names, read as (bits, source)."""
    bits, src = width
    return derived(1 << bits, f"2^{note}, {src}")


def with_sr(read):
    """`read` on a die with SR units, null on one without."""

    def row(img):
        if img.board_dict["units"].get("SR"):
            return read(img)
        return derived(None, "no SR units")

    return row


def sr_buf_words(img):
    """Sub-row buffer words: the die's override of BUF_WORDS, else the default."""
    return img.rtl_override(rel(img.top_path), "BUF_WORDS") or img.rtl(
        SRU_CU, "BUF_WORDS"
    )


def sr_npass(img):
    """Passes a job: the values of the configuration map's NPASS field."""
    m, src = img.grep(SRU_CFG, r"SRU_G_NPASS cfg\[\d+\]\[(\d+):(\d+)\]")
    bits = int(m.group(1)) - int(m.group(2)) + 1
    return derived(1 << bits, f"2^NPASS field bits, {src}")


#: The pool's pipeline from an op's slot cycle c0 (sru_pool.v:15-18): it reads
#: its history in c3 and hands the lane its operands in c4.
SRU_READ, SRU_ISSUE = 3, 4


def sr_ldep(img):
    """Slot cycles from an op to the first op that may read its result: the
    lane's result is out OUT_AT cycles after c4, in its result SRL a cycle
    later, and read in the reader's c3."""
    m, src = img.grep(SRU_POOL, r"localparam integer OUT_AT\s*=\s*(\d+);")
    out_at = int(m.group(1))
    return derived(
        SRU_ISSUE + out_at + 1 - SRU_READ,
        f"c{SRU_ISSUE} + OUT_AT {out_at} + 1 - c{SRU_READ}, {src}; {SRU_POOL}:15-18",
    )


def sr_wlead(img):
    """Slot cycles from a word's take to the first op that may read it: the
    word lands in the X history when the window-start delay line `wsd`
    shifts out, is readable a cycle later, and is read in the op's c3."""
    m, src = img.grep(SRU_POOL, r"reg\s+\[(\d+):0\]\s+wsd;")
    delay = int(m.group(1)) + 1
    return derived(
        delay + 1 - SRU_READ,
        f"wsd's {delay} cycles + 1 - c{SRU_READ}, {src}; {SRU_POOL}:196-209",
    )


def sr_taps(img):
    """Depth of a lane's result history (`h0`), cycles back a tap reads."""
    return img.rtl_array(SRU_POOL, "h0")


def mg_kstep(img):
    """K blocks one sweep issue carries: KSTEP of the double-pumped manager
    when the die builds its clusters with PUMP, else one."""
    pump = img.rtl_override(rel(img.top_path), "PUMP")
    if pump and pump[0]:
        value, src = img.rtl(CLUSTER_MGR, "KSTEP")
        return derived(value, f"KSTEP {src}, the clusters built with PUMP {pump[1]}")
    return derived(
        1,
        f"no .PUMP(1) in {rel(img.top_path)}: one K block an issue (mx_cluster_mgr.v)",
    )


def vc_l1_banks(img):
    """L1 banks: L1_DEPTH over the words a bank (HD = L1_DEPTH / banks)."""
    m, src = img.grep(VEC_CORE, r"\bHD\s*=\s*L1_DEPTH\s*/\s*(\d+)")
    return derived(int(m.group(1)), f"HD = L1_DEPTH / banks, {src}")


def vc_walk_max(img):
    """Words one VFILL / VDRAIN walk takes before it faults F_LEN."""
    m, src = img.grep(VEC_CORE, r"f_total > 32'd(\d+)")
    return derived(int(m.group(1)), f"F_LEN past this count, {src}")


def vc_sig_bits(img):
    """E8M15 significand bits: the lane word less its sign and 8 exponent
    bits, plus the implicit one."""
    bits, src = img.rtl_width(VEC_CORE, "sreg")
    return derived(bits - 1 - 8 + 1, f"lane word bits - sign - 8 exponent + 1, {src}")


def node_uncached_bit(img):
    """The processor's uncached alias bit: UNC_BIT = ADDR_W - 2."""
    m, src = img.grep(SYSCORE, r"UNC_BIT\s*=\s*ADDR_W\s*-\s*(\d+)")
    addr_w, _ = img.rtl(SYSCORE, "ADDR_W")
    return derived(addr_w - int(m.group(1)), f"ADDR_W - {m.group(1)}, {src}")


def il_meshes(img):
    """Meshes an interlink packet names: its destination mesh field, from
    U_DMESH up to U_SMESH."""
    m, src = img.grep(ILINK, r"U_DMESH = (\d+), U_SMESH = (\d+)")
    bits = int(m.group(2)) - int(m.group(1))
    return derived(1 << bits, f"2^(U_SMESH - U_DMESH), {src}")


def fw_debug(img):
    """The debug image (RUL.073): its name, the features it carries
    (KA_DEBUG_F_*) and the completion words it keeps per unit, from the
    firmware build's DEFINES."""
    build = _firmware_build()
    name = next(n for n, d in build.DEFINES.items() if "-DKA_DEBUG=1" in d)
    last = next(
        int(d.split("=")[1])
        for d in build.DEFINES[name]
        if d.startswith("-DKA_DEBUG_LAST=")
    )
    feats, src = img.fw_flags("package/debug.h", "KA_DEBUG_F_")
    return {"image": name, "features": feats, "last": last}, (
        f"software/firmware/build.py:DEFINES; {src}"
    )


def fw_package_copy_bytes(img):
    """The image's package-copy build define, else the source default."""
    flag = "-DKA_PKG_COPY_BYTES="
    given = [d for d in _firmware_build().DEFINES.get(img.fw, []) if d.startswith(flag)]
    if given:
        return (
            int(given[-1].removeprefix(flag), 0),
            "software/firmware/build.py:DEFINES",
        )
    return img.fw_src_define("kohakuaccel/package/interp.c", "KA_PKG_COPY_BYTES")


def fw_queue_record(img):
    """The run record's version, bytes and field offsets from queue/layout.h."""
    version, src = img.fw_define("queue/layout.h", "KA_Q_REC_VERSION")
    nbytes = img.fw_define("queue/layout.h", "KA_Q_REC_BYTES")[0]
    word = img.fw_define("queue/layout.h", "KA_Q_REC_WORD")[0]
    fields, _ = img.fw_enum("queue/layout.h", "ka_q_rec")
    return {
        "version": version,
        "bytes": nbytes,
        "fields": {
            k.removeprefix("KA_QR_").lower(): {"offset": v, "width": word}
            for k, v in fields.items()
        },
    }, f"{src}; queue/layout.h:ka_q_rec"


def mg_dq_depth(img):
    """Drain-queue sub-tiles: DQ_DEPTH = max(TILES, floor) of the cluster."""
    m, src = img.grep(CLUSTER, r"DQ_DEPTH\s*=\s*\(TILES < (\d+)\) \? \d+ : TILES")
    return derived(max(img.top("TILES")[0], int(m.group(1))), f"max(mg.tiles, {src})")


def mg_dq_limit(img):
    """Sub-tiles the drain queue holds before a sweep is held: DQ_LIMIT =
    DQ_DEPTH - margin."""
    m, src = img.grep(CLUSTER_NODE, r"DQ_LIMIT\s*=\s*DQ_DEPTH\s*-\s*(\d+)")
    return derived(mg_dq_depth(img)[0] - int(m.group(1)), f"mg.dq_depth - {src}")


def mg_acc_bits(img):
    """Bits of one accumulator element, the internal float a peer sub-tile
    carries: PW = 16*(ACC_MW+8) over its 16 lanes."""
    m, src = img.grep(CLUSTER, r"PW\s*=\s*16\*\(ACC_MW\+(\d+)\)")
    mw = img.rtl("src/kohakutpu/matmul/mx_cluster_cu_pump.v", "ACC_MW")[0]
    return derived(mw + int(m.group(1)), f"mg.acc_mantissa_bits + {m.group(1)}, {src}")


def vc_rf_banks(img):
    """Register-file banks: the lanes' bank loop (bank = chunk bit 0)."""
    m, src = img.grep(VEC_LANES, r"for \(b = 0; b < (\d+); b = b \+ 1\) begin : g_bank")
    return derived(int(m.group(1)), f"the g_bank loop, {src}")


def sr_burst(img):
    """Words of a row unit's output burst: min(room, N)."""
    m, src = img.grep(SRU_CU, r"blen = \(room < 12'd(\d+)\)")
    return derived(int(m.group(1)), f"the burst length's cap, {src}")


def sr_held(img):
    """Sub-rows a row unit holds past the output that has left: its buffer's
    halves, one `filled` bit each."""
    bits, src = img.rtl_width(SRU_CORE, "filled")
    return derived(bits, f"one `filled` bit a buffer half, {src}")


def sr_param(img, name):
    """An SRU build parameter: the die top's override on its SRU instances,
    else sru_cu.v's default."""
    return img.rtl_override(rel(img.top_path), name) or img.rtl(SRU_CU, name)


def sr_profile(img):
    """The board's SRU profile with the knobs the die builds and the board
    does not name (HAS_MX7)."""
    prof, src = img.board("sru")
    mx7, mx7_src = sr_param(img, "HAS_MX7")
    return {**prof, "HAS_MX7": mx7}, f"{src}; HAS_MX7 {mx7_src}"


SIMT_ALU = "src/kohakumpe/simd/khs_fp32_alu.v"


def simt_param(img, name):
    """A SIMT float-tier parameter: the die top's override, else the float
    ALU's default; 0 on a die without SIMT units."""
    if not img.board_dict["units"].get("SIMT"):
        return derived(0, "no SIMT units")
    return img.rtl_override(rel(img.top_path), name) or img.rtl(SIMT_ALU, name)


def mg_macs(img):
    """MACs a cycle: a 4x4 sub-tile of lanes x lanes over a K block, kstep
    blocks an issue."""
    lanes, _ = mg_geometry(img, 1)
    kblock, _ = mg_geometry(img, 2)
    kstep, _ = mg_kstep(img)
    return derived(lanes * lanes * kblock * kstep, "mg.lanes^2 x mg.kblock x mg.kstep")


def vc_faults(img):
    """Fault codes the core raises: each F_* code v2_core.v assigns outside a
    `default:` arm (F_OPCODE is only the default arm of a case every opcode
    decodes, ISA.VC.045)."""
    text = (ROOT_PATH / VEC_CORE).read_text(encoding="utf-8").splitlines()
    codes, lines, raised = {}, [], set()
    for n, line in enumerate(text, 1):
        for name, value in re.findall(r"\b(F_[A-Z0-9]+)\s*=\s*8'd(\d+)", line):
            codes[name] = int(value)
            lines.append(n)
        if "default:" not in line and "localparam" not in line:
            raised |= set(re.findall(r"(?:<=|=)\s*(F_[A-Z0-9]+)\b", line))
    value = sorted(codes[n] for n in raised if n in codes)
    return derived(
        value,
        f"F_* raised outside a default arm, {VEC_CORE}:{min(lines)}-{max(lines)}",
    )


def vc_fault_names(img):
    """The names of `vc.faults`: each raised code's F_* name in v2_core.v."""
    codes, src = vc_faults(img)
    text = (ROOT_PATH / VEC_CORE).read_text(encoding="utf-8")
    names = dict(re.findall(r"\b(F_[A-Z0-9]+)\s*=\s*8'd(\d+)", text))
    return {n: int(v) for n, v in names.items() if int(v) in codes}, src


def mover_faults(img):
    """The mover's fault codes by name: mm_mover.v's F_* localparams."""
    text = (ROOT_PATH / MOVER).read_text(encoding="utf-8").splitlines()
    out, lines = {}, []
    for n, line in enumerate(text, 1):
        for name, value in re.findall(r"\b(F_[A-Z]+)\s*=\s*4'd(\d+)", line):
            out[name] = int(value)
            lines.append(n)
    return out, f"{MOVER}:{min(lines)}-{max(lines)}"


def vc_timing(name: str):
    """A measured core variant; unmatched image overrides have no default."""

    def read(img):
        defines = sorted(
            (
                k
                if v == "1" and k in ("VEC_CORE_V2", "V2_SB_LUT", "V2_TAB_LUT")
                else f"{k}={v}"
            )
            for k, v in img.defines.items()
            if k.startswith(("V2_", "VEC_CORE_"))
        )
        core = img.config("VEC_CORE")[0]
        matches = []
        for path in sorted((ROOT_PATH / "machines/measured").glob("vc_timing.*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["vc_build"] == core and record["defines"] == defines:
                matches.append((path, record))
        if len(matches) != 1:
            raise ValueError(
                f"{img.ver}: no unique measured VC timing for {core}: {defines}"
            )
        path, record = matches[0]
        parameter = record["parameters"][name]
        value = parameter["value"]
        if type(value) is not int or value < 0:
            raise ValueError(f"{path}: invalid timing value {name}")
        return (
            value,
            f"{rel(path)}:{name}; vec2_replay/{','.join(parameter['benches'])}",
        )

    return read


def mg_geometry(img, group):
    """Lanes (group 1) or K extent (group 2) of one L1 entry, as the cluster
    manager lays it out."""
    m, src = img.grep(CLUSTER_MGR, r"A rows (\d+)g\.\.\d+g\+\d+, all (\d+) K")
    return derived(int(m.group(group)), f"L1A[g] layout, {src}")


STAGE = "src/kohakuaccel/sysnode/core/mag_stage.v"
MAG = "src/kohakuaccel/sysnode/core/mag.v"
MEM_PORT = "src/kohakuaccel/sysnode/core/mag_mem_port.v"


def address_fields(img):
    """Address fields from whole decode lines, widths and shifts together."""
    mesh, ms = img.grep(
        STAGE,
        r"^\s*wire a_mesh = \(a_addr\[(\d+):(\d+)\] == MESH_ID\) && !a_addr\[(\d+)\];\s*$",
    )
    ap, aps = img.grep(STAGE, r"^\s*wire \[(\d+):0\] a_ap = a_addr\[(\d+):(\d+)\];\s*$")
    special, ss = img.grep(
        STAGE,
        r"^\s*assign a_mine = a_mesh && a_addr\[(\d+)\] && \(a_ap == AP_STAGE\);\s*$",
    )
    hi, lo, reserved = map(int, mesh.groups())
    width, ahi, alo = map(int, ap.groups())
    if width != ahi - alo:
        raise ValueError("aperture decode width disagrees with its slice")
    return {
        "special_bit": (int(special.group(1)), ss),
        "reserved_bit": (reserved, ms),
        "mesh_shift": (lo, ms),
        "mesh_bits": (hi - lo + 1, ms),
        "aperture_shift": (alo, aps),
        "aperture_bits": (ahi - alo + 1, aps),
    }


def staging_param(img, name):
    """A staging parameter and the sites that repeat its value must agree."""
    value, source = img.rtl(STAGE, name)
    sites = []
    if name == "WORDS":
        for path in (MAG, MEM_PORT):
            match, site = img.grep(path, r"\.WORDS\s*\(\s*(\d+)\s*\)")
            sites.append((int(match.group(1)), site))
    elif name == "AP_STAGE":
        match, site = img.grep(MAG, r"\.AP_STAGE\(4'h([0-9a-fA-F]+)\)")
        sites.append((int(match.group(1), 16), site))
        for op in ("==", "!="):
            match, site = img.grep(MEM_PORT, rf"a\[35:32\] {op} 4'h([0-9a-fA-F]+)")
            sites.append((int(match.group(1), 16), site))
    if any(other != value for other, _ in sites):
        raise ValueError(f"{source}: {name} disagrees with {sites}")
    return value, "; ".join([source, *(site for _, site in sites)])


def check_address(values):
    """Address fields are ordered, disjoint and large enough for staging."""
    a = {
        k: values[f"die.address.{k}"]
        for k in (
            "special_bit",
            "reserved_bit",
            "mesh_shift",
            "mesh_bits",
            "aperture_shift",
            "aperture_bits",
        )
    }
    if not (
        0 <= a["aperture_shift"]
        and a["mesh_bits"] > 0
        and a["aperture_bits"] > 0
        and a["aperture_shift"] + a["aperture_bits"] <= a["mesh_shift"]
        and a["mesh_shift"] + a["mesh_bits"] <= a["reserved_bit"]
        and a["reserved_bit"] < a["special_bit"] < values["die.noc.addr_bits"] <= 64
    ):
        raise ValueError(f"invalid address layout: {a}")
    size = (
        values["die.staging.entries"]
        * values["die.staging.entry_words"]
        * values["die.word_bytes"]
    )
    if not 0 < size <= 1 << a["aperture_shift"]:
        raise ValueError("staging must fit its aperture")
    aperture = values["die.staging.aperture"]
    if not (
        0 <= aperture < 1 << a["aperture_bits"]
        and (values["die.staging.implemented"] >> aperture) & 1
    ):
        raise ValueError("staging aperture must be implemented and fit its field")


ROWS = [
    (f"conversion.fp16_mx7.{key}", lambda img, key=key: conversion(img, key))
    for key in (
        "version",
        "allowed_backends",
        "preferred_backend",
        "nonfinite_policy",
        "vector_slots",
        "vector_cores",
        "vector_looped",
    )
] + [
    # The card: its nodes and how the host reaches them.
    ("card.name", lambda i: i.board("name")),
    ("card.nodes", lambda i: i.board("meshes")),
    ("card.chain", lambda i: i.board("chain")),
    (
        "card.die",
        lambda i: derived(i.die_module, f"scripts/tcl/{i.ver}/00_config.tcl:MESHES"),
    ),
    ("card.profiles", lambda i: i.board("profiles")),
    ("card.idle_profile", lambda i: i.board("idle_profile")),
    ("card.clocks", lambda i: i.board("wizard_outputs")),
    ("card.dram.flat_gb", lambda i: i.board("dram_flat_gb")),
    ("card.dram.interleave_kb", lambda i: i.board("dram_interleave_kb")),
    ("card.dram.bytes", dram_bytes),
    ("card.dram.line_bytes", dram_line_bytes),
    ("card.host.ctrl_base", lambda i: i.board("ctrl_base")),
    ("card.host.ctrl_stride", lambda i: i.board("ctrl_stride")),
    ("card.host.wizard_base", lambda i: i.board("wizard_base")),
    ("card.host.vco_mhz", lambda i: i.board("vco_mhz")),
    ("card.host.mem_window_shift", lambda i: i.board("mem_window_shift")),
    ("card.host.mem_window_offset", lambda i: i.board("mem_window_offset")),
    ("card.host.dram_mesh_shift", lambda i: i.board("dram_mesh_shift")),
    ("card.host.write_granule", lambda i: i.board("host_write_granule")),
    ("card.host.write_replicates", lambda i: i.board("host_write_replicates")),
    ("card.host.burst_read", lambda i: i.board("burst_read")),
    ("card.host.max_burst_beats", lambda i: i.board("max_burst_beats")),
    # The die every node carries: units by kind and the network around them.
    ("die.units.mg", lambda i: die_units(i, "MG")),
    ("die.units.vc", lambda i: die_units(i, "VC")),
    ("die.units.sr", lambda i: die_units(i, "SR")),
    ("die.units.simt", lambda i: die_units(i, "SIMT")),
    ("die.target", die_target),
    ("die.sys_ports", lambda i: i.board("sys_ports")),
    ("die.fetch_port", lambda i: i.board("fetch_port")),
    ("die.noc.flit_bits", lambda i: i.top("FW")),
    ("die.noc.data_bits", lambda i: i.top("DW")),
    ("die.noc.addr_bits", lambda i: i.top("AW")),
    ("die.noc.pos_bits", lambda i: i.top("PW")),
    ("die.noc.router_depth", lambda i: i.top("ROUTER_DEPTH")),
    ("die.word_bytes", lambda i: derived(i.top("DW")[0] // 8, "die.noc.data_bits / 8")),
    *[
        (f"die.address.{key}", lambda i, k=key: address_fields(i)[k])
        for key in (
            "special_bit",
            "reserved_bit",
            "mesh_shift",
            "mesh_bits",
            "aperture_shift",
            "aperture_bits",
        )
    ],
    ("die.staging.entry_words", lambda i: staging_param(i, "WORDS")),
    ("die.staging.aperture", lambda i: staging_param(i, "AP_STAGE")),
    ("die.staging.implemented", lambda i: staging_param(i, "AP_IMPL")),
    ("die.staging.banks", lambda i: i.top("L2_MAG_BANKS")),
    ("die.staging.entries", lambda i: i.top("L2_MAG_ENTRIES")),
    (
        "die.staging.flits",
        lambda i: i.rtl("src/kohakuaccel/sysnode/sysnode.v", "STAGE_FLITS"),
    ),
    # Every unit's instruction path.
    ("unit.inst_depth", lambda i: i.top("INST_DEPTH")),
    ("unit.recv_depth", lambda i: i.top("RECV_DEPTH")),
    (
        "unit.burst_words",
        lambda i: field_span(
            i.rtl_width(VEC_CU, "cud_len"), "CU_DATA length field bits (N - 1)"
        ),
    ),
    # Streaming Row Unit: the profile the die's SR units are built with.
    ("sr.count", lambda i: unit_count(i, "SR")),
    (
        "sr.profile",
        lambda i: (
            sr_profile(i) if "sru" in i.board_dict else derived(None, "no SR units")
        ),
    ),
    ("sr.f16_ieee", with_sr(lambda i: sr_param(i, "F16_IEEE"))),
    ("sr.buf_words", with_sr(sr_buf_words)),
    ("sr.fifo_depth", with_sr(lambda i: i.rtl(SRU_CU, "FIFO_DEPTH"))),
    ("sr.op_depth", with_sr(lambda i: i.vh_define(SRU_CFG, "SRU_OP_DEPTH"))),
    ("sr.fin_depth", with_sr(lambda i: i.vh_define(SRU_CFG, "SRU_FIN_DEPTH"))),
    ("sr.npass", with_sr(sr_npass)),
    ("sr.rows", with_sr(lambda i: i.rtl_override(SRU_CORE, "DEPTH", inst="u_rs"))),
    ("sr.ldep", with_sr(sr_ldep)),
    ("sr.wlead", with_sr(sr_wlead)),
    ("sr.taps", with_sr(sr_taps)),
    ("sr.burst", with_sr(sr_burst)),
    ("sr.held", with_sr(sr_held)),
    # SIMT units.
    ("simt.count", lambda i: unit_count(i, "SIMT")),
    ("simt.flanes", lambda i: simt_param(i, "FLANES")),
    ("simt.fsfu_units", lambda i: simt_param(i, "FSFU_UNITS")),
    # Matmul cluster.
    ("mg.count", lambda i: unit_count(i, "MG")),
    ("mg.l1_a_entries", lambda i: i.top("GA")),
    ("mg.l1_b_entries", lambda i: i.top("GB")),
    (
        "mg.l1_bank_entries",
        lambda i: derived(
            i.top("GA")[0] // 2,
            "mg.l1_a_entries / 2 banks (isa.md, mx_cluster_mgr_pump.v)",
        ),
    ),
    ("mg.tiles", lambda i: i.top("TILES")),
    ("mg.dq_depth", mg_dq_depth),
    ("mg.dq_limit", mg_dq_limit),
    (
        "mg.acc_mantissa_bits",
        lambda i: i.rtl("src/kohakutpu/matmul/mx_cluster_cu_pump.v", "ACC_MW"),
    ),
    ("mg.acc_bits", mg_acc_bits),
    ("mg.shared_b_max", lambda i: i.measured("mg.shared_b_max")),
    ("mg.lanes", lambda i: mg_geometry(i, 1)),
    ("mg.kblock", lambda i: mg_geometry(i, 2)),
    ("mg.entry_bits", lambda i: i.rtl_width(CLUSTER, "l1_data")),
    ("mg.kstep", mg_kstep),
    (
        "mg.max_fill",
        lambda i: field_max(i.rtl_width(CLUSTER, "cnt"), "memory request count bits"),
    ),
    (
        "mg.max_peers",
        lambda i: field_max(i.rtl_width(CLUSTER, "i_npeer"), "FILL npeer bits"),
    ),
    ("mg.reuse_min", lambda i: i.rtl(CLUSTER, "ACU_REUSE_MIN")),
    ("mg.wburst", lambda i: i.rtl(CLUSTER, "WBURST")),
    ("mg.macs_per_cycle", mg_macs),
    # Vector core.
    ("vc.count", lambda i: unit_count(i, "VC")),
    ("vc.build", lambda i: i.config("VEC_CORE")),
    ("vc.regs", lambda i: derived(1 << i.core("RW")[0], f"2^RW, {i.core('RW')[1]}")),
    ("vc.rf_copies", lambda i: i.core("RF_COPIES")),
    ("vc.rf_banks", vc_rf_banks),
    ("vc.vlmax", lambda i: i.core("VLMAX")),
    (
        "vc.lanes",
        lambda i: derived(
            i.top("DW")[0] // 16, "die.noc.data_bits / 16-bit lanes (one word of f16)"
        ),
    ),
    (
        "vc.chunks",
        lambda i: derived(
            i.core("VLMAX")[0] // (i.top("DW")[0] // 16), "vc.vlmax / vc.lanes"
        ),
    ),
    ("vc.l1_words", lambda i: i.top("L1_DEPTH")),
    ("vc.l1_banks", vc_l1_banks),
    ("vc.imem_words", lambda i: i.core("IMEM_DEPTH")),
    (
        "vc.run_reach",
        lambda i: field_span(i.rtl_width(VEC_CORE, "start_pc"), "RUN start pc bits"),
    ),
    ("vc.queue_depth", lambda i: i.core("QD")),
    (
        "vc.dma_queue_depth",
        lambda i: i.rtl_override(VEC_CORE, "D", inst="u_fq"),
    ),
    ("vc.dualq", lambda i: i.core("DUALQ")),
    ("vc.has_mx", lambda i: i.core("HAS_MX")),
    ("vc.xnet", lambda i: i.core("XNET")),
    ("vc.tau", lambda i: i.core("TAU")),
    ("vc.sregs", lambda i: i.rtl_array(VEC_CORE, "sreg")),
    ("vc.kregs", lambda i: i.rtl_array(VEC_CORE, "kreg")),
    ("vc.preds", lambda i: i.rtl_array(VEC_LANES, "preg")),
    ("vc.marks", lambda i: i.rtl_array(VEC_CORE, "mk_tgt")),
    ("vc.sig_bits", vc_sig_bits),
    ("vc.descriptors", lambda i: i.rtl_array(VEC_AGU, "base")),
    ("vc.descriptor_dims", lambda i: i.rtl_array(VEC_AGU, "strd", 1)),
    ("vc.ainc_bits", lambda i: i.rtl_width(VEC_CORE, "a_inc")),
    ("vc.walk_max", vc_walk_max),
    ("vc.faults", vc_faults),
    ("vc.fault_names", vc_fault_names),
    ("vc.timing.math_math", vc_timing("math_math")),
    ("vc.timing.xbar", vc_timing("xbar")),
    ("vc.timing.unpack_math", vc_timing("unpack_math")),
    ("vc.timing.math_pack", vc_timing("math_pack")),
    ("vc.timing.cond", vc_timing("cond")),
    ("vc.timing.seti", vc_timing("seti")),
    ("vc.timing.mx7_issue_gap", vc_timing("mx7_issue_gap")),
    ("vc.timing.mx7_pack_words", vc_timing("mx7_pack_words")),
    (
        "vc.crossbar_modes",
        lambda i: derived(
            VC_XB if i.core("XNET")[0] else ["none"], "vc.xnet (v2_core.v XNET)"
        ),
    ),
    (
        "vc.pack_modes",
        lambda i: derived(
            VC_PACK if i.core("HAS_MX")[0] else VC_PACK[:3],
            "v2_core.v MD_* with vc.has_mx",
        ),
    ),
    ("vc.fill_alone", lambda i: i.measured("vc.fill_alone")),
    ("vc.fill_shared", lambda i: i.measured("vc.fill_shared")),
    # Memory mover.
    (
        "mover.index_words",
        lambda i: i.rtl("src/kohakuaccel/sysnode/mover/mm_mover.v", "IDX_WORDS"),
    ),
    (
        "mover.max_index",
        lambda i: derived(
            i.rtl("src/kohakuaccel/sysnode/mover/mm_mover.v", "IDX_WORDS")[0] * 8,
            "mover.index_words x 8 indices a word",
        ),
    ),
    (
        "mover.levels",
        lambda i: i.rtl("src/kohakuaccel/sysnode/mover/mx_tdesc.v", "NDIM"),
    ),
    (
        "mover.burst_max",
        lambda i: i.rtl("src/kohakuaccel/sysnode/mover/mm_mover.v", "BURST_MAX"),
    ),
    (
        "mover.fifo_depth",
        lambda i: i.rtl("src/kohakuaccel/sysnode/mover/mm_mover.v", "FIFO_D"),
    ),
    ("mover.one_word_runs", lambda i: i.measured("mover.one_word_runs")),
    ("mover.faults", mover_faults),
    ("mover.cfgq_depth", lambda i: i.rtl(MOVER, "CFGQ_D")),
    ("mover.cfgq_margin", lambda i: i.rtl(MOVER, "CFGQ_MARGIN")),
    ("mover.fault_bits", lambda i: i.rtl_width(MOVER, "stat_fault")),
    (
        "mover.row_words",
        lambda i: field_max(i.rtl_width(MOVER, "gath_words"), "gather row count bits"),
    ),
    (
        "mover.modes",
        lambda i: derived(
            MOVER_MODES,
            "mm_mover.v mode decode (src/kohakuaccel/sysnode/mover/mm_mover.v:130-132)",
        ),
    ),
    # The transform slot's occupants: a quantiser entry is a word a lane out;
    # GT4 transposes g_o rows as a rows x rows array of granules.
    ("mover.xform.quant_lanes", lambda i: i.rtl(XFORM, "OUT_WORDS")),
    (
        "mover.xform.quant_values",
        lambda i: derived(
            i.rtl(XFORM, "IN_BITS")[0] // 16 // i.rtl(XFORM, "OUT_WORDS")[0],
            f"IN_BITS / 16 / OUT_WORDS ({i.rtl(XFORM, 'IN_BITS')[1]}, "
            f"{i.rtl(XFORM, 'OUT_WORDS')[1]}; {QUANT}:7-10,71)",
        ),
    ),
    ("mover.xform.gt4_rows", lambda i: i.rtl_array(XFORM, "g_o")),
    (
        "mover.xform.gt4_cols",
        lambda i: derived(
            i.rtl(XFORM, "DATA_W")[0] // 16,
            f"DATA_W / 16 FP16 values a word ({i.rtl(XFORM, 'DATA_W')[1]})",
        ),
    ),
    (
        "mover.xform.gt4_granule_bits",
        lambda i: derived(
            i.rtl(XFORM, "DATA_W")[0] // i.rtl_array(XFORM, "g_o")[0],
            f"DATA_W / gt4_rows ({XFORM}:104-113)",
        ),
    ),
    # The system node: processor, dispatch engine, queue.
    (
        "node.imem_bytes",
        lambda i: derived(
            i.rtl("src/kohakuaccel/sysnode/sysnode.v", "PE_IMEM")[0] * 4,
            "PE_IMEM x 4 bytes",
        ),
    ),
    (
        "node.spad_bytes",
        lambda i: derived(
            i.rtl("src/kohakuaccel/sysnode/sysnode.v", "PE_SPAD")[0] * 8,
            "PE_SPAD x 8 bytes",
        ),
    ),
    (
        "node.engine.depth",
        lambda i: i.rtl("src/kohakuaccel/sysnode/dispatch/dispatch_engine.v", "DEPTH"),
    ),
    (
        "node.engine.counters",
        lambda i: i.rtl("src/kohakuaccel/sysnode/dispatch/dispatch_engine.v", "NCTR"),
    ),
    (
        "node.engine.counter_bits",
        lambda i: i.rtl("src/kohakuaccel/sysnode/dispatch/dispatch_engine.v", "CTR_W"),
    ),
    ("node.spad_base", lambda i: i.rtl(SYSCORE, "SPAD_BASE")),
    ("node.uncached_bit", node_uncached_bit),
    ("node.ctrl_bytes", lambda i: i.fw_define("hal/node.h", "KA_CTRL_SPAN")),
    ("node.console_fifo_bytes", lambda i: i.rtl_array(LOAD_WIN, "con_mem")),
    ("node.rx_depth", lambda i: i.rtl(MBOX, "RQ_DEPTH")),
    ("node.cq_depth", lambda i: i.fw_define("hal/node.h", "KA_NM_CQ_DEPTH")),
    ("node.ring_depth", lambda i: i.fw_define("hal/node.h", "KA_IL_RING_DEPTH")),
    ("node.mover_span", lambda i: i.fw_define("hal/node.h", "KA_MV_SPAN")),
    (
        "node.mover_room_writes",
        lambda i: i.fw_define("hal/node.h", "KA_MV_ROOM_WRITES"),
    ),
    # The interlink between nodes.
    ("il.slots", lambda i: i.rtl(ILINK, "SIG_SLOTS")),
    ("il.meshes", il_meshes),
    # Firmware: limits, formats and what the image carries.
    ("fw.image", lambda i: derived(i.fw, "the machine's firmware image")),
    ("fw.parts", lambda i: i.fw_parts()),
    ("fw.unit_classes", lambda i: i.fw_unit_classes()),
    ("fw.memory", lambda i: i.fw_memory()),
    ("fw.sections", lambda i: i.fw_size()),
    ("fw.max_units", lambda i: i.fw_define("boot/args.h", "KA_MAX_UNITS")),
    (
        "fw.max_pkg_units",
        lambda i: i.fw_define("dispatch/engine.h", "KA_MAX_PKG_UNITS"),
    ),
    ("fw.engine_units", lambda i: i.fw_define("dispatch/de.h", "KA_DE_UNITS")),
    ("fw.fetch_depth", lambda i: i.fw_define("dispatch/engine.h", "KA_FETCH_DEPTH")),
    ("fw.max_buffers", lambda i: i.fw_define("package/interp.h", "KA_MAX_BUFFERS")),
    ("fw.enum_wait", lambda i: i.fw_define("unit/enumerate.h", "KA_ENUM_WAIT")),
    ("fw.heap.regions", lambda i: i.fw_define("os/mem.h", "KA_MEM_REGIONS")),
    ("fw.heap.blocks", lambda i: i.fw_define("os/mem.h", "KA_MEM_BLOCKS")),
    ("fw.boot.version", lambda i: i.fw_define("boot/args.h", "KA_BOOT_VERSION")),
    ("fw.boot.flags", lambda i: i.fw_flags("boot/args.h", "KA_BOOT_F_")),
    ("fw.boot.regions", lambda i: i.fw_define("boot/args.h", "KA_BOOT_REGIONS")),
    ("fw.boot.region_uses", lambda i: i.fw_flags("boot/args.h", "KA_REGION_")),
    ("fw.debug", fw_debug),
    ("fw.queue.version", lambda i: i.fw_define("queue/layout.h", "KA_Q_VERSION")),
    ("fw.queue.line", lambda i: i.fw_define("queue/layout.h", "KA_Q_LINE")),
    ("fw.queue.sq_bytes", lambda i: i.fw_define("queue/layout.h", "KA_Q_SQ_BYTES")),
    ("fw.queue.cq_bytes", lambda i: i.fw_define("queue/layout.h", "KA_Q_CQ_BYTES")),
    ("fw.queue.record", fw_queue_record),
    ("fw.package.magic", lambda i: i.fw_define("package/format.h", "KA_PKG_MAGIC")),
    ("fw.package.version", lambda i: i.fw_define("package/format.h", "KA_PKG_VERSION")),
    (
        "fw.package.header_bytes",
        lambda i: i.fw_define("package/format.h", "KA_PKG_HEADER"),
    ),
    ("fw.package.regs", lambda i: i.fw_define("package/format.h", "KA_PKG_REGS")),
    ("fw.package.copy_bytes", fw_package_copy_bytes),
    (
        "fw.package.unbounded",
        lambda i: i.fw_define("package/format.h", "KA_PKG_UNBOUNDED"),
    ),
    ("fw.status", lambda i: i.fw_enum("package/status.h", "ka_status")),
    ("fw.waits", lambda i: i.fw_enum("package/status.h", "ka_wait")),
    ("fw.signals", lambda i: i.fw_enum("hal/node.h", "KA_SIG_")),
]


def check_unique():
    paths = [p for p, _ in ROWS]
    dup = {p for p in paths if paths.count(p) > 1}
    if dup:
        raise SystemExit(f"machine table: duplicate paths {sorted(dup)}")
    for p in paths:
        if any(q.startswith(p + ".") for q in paths):
            raise SystemExit(f"machine table: {p} is both a value and a section")


__all__ = ["ROWS", "check_unique", "derived"]
