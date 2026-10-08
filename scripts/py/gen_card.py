"""Emit a simulated card: one multimesh block design as a single Verilog top,
built from the image's Tcl config and station map (sim/verilator/docs/card-backend.md).

    python scripts/py/gen_card.py --ver v8t8 --sim-mesh ktpu_sim_1x1_1c1v_1m_nol2_pump \\
        --compute 0,1 -o tests/system/card_v8t8_2n.v --module card_v8t8_2n
"""

import argparse
import itertools
import tkinter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TCL = ROOT / "scripts" / "tcl"

# (name, width, direction seen from the MANAGER: o = manager drives)
AXI4 = [
    ("awid", "IDW", "o"),
    ("awaddr", "AW", "o"),
    ("awlen", 8, "o"),
    ("awsize", 3, "o"),
    ("awburst", 2, "o"),
    ("awvalid", 1, "o"),
    ("awready", 1, "i"),
    ("wdata", "DW", "o"),
    ("wstrb", "SW", "o"),
    ("wlast", 1, "o"),
    ("wvalid", 1, "o"),
    ("wready", 1, "i"),
    ("bid", "IDW", "i"),
    ("bresp", 2, "i"),
    ("bvalid", 1, "i"),
    ("bready", 1, "o"),
    ("arid", "IDW", "o"),
    ("araddr", "AW", "o"),
    ("arlen", 8, "o"),
    ("arsize", 3, "o"),
    ("arburst", 2, "o"),
    ("arvalid", 1, "o"),
    ("arready", 1, "i"),
    ("rid", "IDW", "i"),
    ("rdata", "DW", "i"),
    ("rresp", 2, "i"),
    ("rlast", 1, "i"),
    ("rvalid", 1, "i"),
    ("rready", 1, "o"),
]
LITE_DROP = {"awid", "awlen", "awsize", "awburst", "wlast", "bid"}
LITE_DROP |= {"arid", "arlen", "arsize", "arburst", "rid", "rlast"}
LITE = [f for f in AXI4 if f[0] not in LITE_DROP]
# The node's slave ports carry no size/burst (single-size INCR only).
NODE = [f for f in AXI4 if f[0] not in ("awsize", "awburst", "arsize", "arburst")]


def width(w, aw, dw, idw):
    return {"AW": aw, "DW": dw, "SW": dw // 8, "IDW": idw}.get(w, w)


def decl(prefix, fields, aw, dw, idw, host=None):
    """Wires, or top-level ports (`host` True: the manager is outside)."""
    out = []
    for name, w, d in fields:
        n = width(w, aw, dw, idw)
        vec = f"[{n - 1}:0] " if n > 1 else ""
        if host is None:
            out.append(f"    wire {vec}{prefix}_{name};")
        else:
            dirn = "input " if (d == "o") == host else "output"
            out.append(f"    {dirn} wire {vec}{prefix}_{name},")
    return out


def conn(port, sig, fields, slices=None):
    s = slices or {}
    return [f".{port}_{n}({sig}_{n}{s.get(n, '')})" for n, _, _ in fields]


def wrap(items, indent=8, per=4):
    pad = " " * indent
    rows = [", ".join(items[i : i + per]) for i in range(0, len(items), per)]
    return [pad + r + ("," if k + 1 < len(rows) else "") for k, r in enumerate(rows)]


class Config:
    """The image's Tcl config, evaluated."""

    def __init__(self, ver, sets=()):
        self.ver = ver
        self.sets = list(sets)
        self.t = tkinter.Tcl()
        self.t.eval(f"source {{{(TCL / ver / '00_config.tcl').as_posix()}}}")
        for name, value in sets:
            self.t.eval(f"set {name} {{{value}}}")
        self.t.eval(f"source {{{(TCL / 'v8' / '50_addr_lit.tcl').as_posix()}}}")

    def s(self, name):
        return self.t.eval(f"set {name}")

    def i(self, name):
        return int(float(self.s(name)))

    def lst(self, name):
        return [int(x) for x in self.t.splitlist(self.s(name))]

    def meshes(self):
        v = self.t.splitlist(self.s("MESHES"))
        return [(int(v[k]), v[k + 1]) for k in range(0, len(v), 2)]


def pairs(items):
    return [tuple(x.split("=", 1)) for x in items]


STAND_INS = [
    "src/kohakuaccel/verif/axi_ram.v",
    "src/kohakuaccel/verif/axil_ram.v",
    "src/kohakuaccel/verif/axi_up32to64.v",
    "src/kohakuaccel/verif/clk_wiz_model.v",
]


def image_sources(ver, sim_mesh=""):
    """The image's RTL as its 10_sources.tcl adds it, headers dropped (they are
    on xsim.py's INCDIRS), plus the stand-ins and the simulation mesh."""
    t = tkinter.Tcl()
    t.eval(f"source {{{(TCL / ver / '00_config.tcl').as_posix()}}}")
    stubs = ("add_files", "set_property", "get_files", "update_compile_order", "puts")
    for stub in stubs:
        t.eval(f"proc {stub} {{args}} {{}}")
    t.eval(f"source {{{(TCL / 'v8t3' / '10_sources.tcl').as_posix()}}}")
    srcs = [f for f in t.splitlist(t.eval("set V8_SOURCES")) if not f.endswith(".vh")]
    if sim_mesh:
        srcs.append(f"src/kohakutpu/top/generated/{sim_mesh}.v")
    return srcs + STAND_INS


def cat(vals, w):
    v = 0
    for k, x in enumerate(vals):
        v |= (x & ((1 << w) - 1)) << (k * w)
    n = len(vals) * w
    return f"{n}'h{v:0{(n + 3) // 4}x}"


def swap_bytes(vals):
    v = 0
    for k, x in enumerate(vals):
        v |= x << (8 * k)
    return f"256'h{v:064x}"


def emit(c, module, sim_mesh, compute, dram_words, lock_cyc, mesh_extra=()):
    if c.i("PER_DIE_CLK") != 1:
        raise SystemExit("gen_card: only PER_DIE_CLK 1 images (v8t4 and later)")
    dies = c.meshes()
    n = len(dies)
    if n != 4:
        raise SystemExit("gen_card: sb_bd_line4_jbus and kx_pbd_4x4 take four dies")
    AW, NQ, FW = c.i("AW"), c.i("NQ"), c.i("FW")
    XIDW = c.i("KX_DRAM_IDW")
    ILW, ILC = c.i("IL_W"), c.i("IL_CN_W")
    if sim_mesh:
        dies = [(m, sim_mesh) for m, _ in dies]
    mods = sorted({mod for _, mod in dies})

    L = [
        f"// {module} -- GENERATED by scripts/py/gen_card.py from scripts/tcl/{c.ver}/00_config.tcl.",
        "// Do not edit by hand.",
        "//",
        f"// Die modules: {', '.join(mods)}; compute dies {sorted(compute)}.",
        "// Config overrides: "
        + (" ".join(f"{k}={v}" for k, v in c.sets + list(mesh_extra)) or "none"),
        "// Clocks are ports. `rstn` is the control wizard's lock; every sysnode",
        "// domain is held by lock_all, the AND of rstn and the mesh wizards' locks.",
        "",
        "`default_nettype none",
        "",
        "// 1 = behavioural DSPs; xsim.py and vlt.py define it.",
        "`ifndef MX_MODEL",
        "`define MX_MODEL 1",
        "`endif",
        "",
        f"module {module} #(",
        f"    parameter integer DRAM_WORDS = {dram_words},   // 512-bit words per channel",
        f"    parameter integer LOCK_CYC   = {lock_cyc}     // clk_ctrl cycles a LOAD holds the lock low",
        ")(",
        "    input  wire clk_ctrl,              // clk_wiz_ctrl out1: station port 3, the wizards",
        "    input  wire clk_xdma,              // xdma_0/axi_aclk: managers 1 and 2",
        "    input  wire [3:0] bus_clk,         // clk_wiz_ctrl out2..5; JTAG on bus_clk[1]",
        "    input  wire [3:0] sys_clk,         // mesh wizard out4: node, Xache partition, ports 0/1",
        "    input  wire [3:0] noc_clk,         // mesh wizard out1",
        "    input  wire [3:0] mat2x_clk,       // mesh wizard out2 (mat 1x through ktpu_div2)",
        "    input  wire [3:0] vec_clk,         // mesh wizard out3",
        "    input  wire [3:0] ddr_clk,         // MIG ui_clk: Xache home, station port 2",
        "    input  wire rstn,",
        "",
        "    // manager 0: jtag_axi, 64-bit, on bus_clk[1]",
    ]
    L += decl("h0", AXI4, AW, 64, 4, host=True)
    L += ["", "    // manager 1: XDMA M_AXI, 512-bit, on clk_xdma"]
    L += decl("h1", AXI4, AW, 512, 4, host=True)
    L += ["", "    // manager 2: XDMA M_AXI_LITE, 32-bit, on clk_xdma"]
    L += decl("h2", LITE, AW, 32, 4, host=True)
    L += [
        "",
        "    // the mesh wizards as applied: CLKOUT3..0 divides, M, D, lock",
        "    output wire [4*32-1:0] wiz_div,",
        "    output wire [4*8-1:0]  wiz_mult,",
        "    output wire [4*8-1:0]  wiz_divclk,",
        "    output wire [3:0]      wiz_locked,",
        "",
        "    // DRAM backdoors, channel h = Xache home h",
        "    input  wire [3:0]       bd_we,",
        "    input  wire [4*16-1:0]  bd_addr,",
        "    input  wire [4*512-1:0] bd_wdata,",
        "    output wire [4*512-1:0] bd_rdata,",
        "",
        "    output wire [31:0] stat_decerr,",
        "    output wire [3:0]  sys_rstn         // each die's sysnode reset",
        ");",
        "",
        "    // ---- resets: one per clock (20_clocks.tcl at PER_DIE_CLK 1) -------",
        "    wire rstn_ctrl, rstn_xdma;",
        "    wire [3:0] rstn_bus, rstn_sys, rstn_ddr;",
        "    wire lock_all = rstn & (&wiz_locked);",
        "    kh_rst_sync u_rs_ctrl (.clk(clk_ctrl), .arstn(rstn), .rstn(rstn_ctrl));",
        "    kh_rst_sync u_rs_xdma (.clk(clk_xdma), .arstn(rstn), .rstn(rstn_xdma));",
        "    assign sys_rstn = rstn_sys;",
        "    genvar gi;",
        "    generate for (gi = 0; gi < 4; gi = gi + 1) begin : g_rs",
        "        kh_rst_sync u_bus (.clk(bus_clk[gi]), .arstn(rstn), .rstn(rstn_bus[gi]));",
        "        kh_rst_sync u_sys (.clk(sys_clk[gi]), .arstn(lock_all), .rstn(rstn_sys[gi]));",
        "        kh_rst_sync u_ddr (.clk(ddr_clk[gi]), .arstn(rstn), .rstn(rstn_ddr[gi]));",
        "    end endgenerate",
        "",
        "    // ---- the station map (v8/50_addr_lit.tcl) ---------------------------",
    ]
    base, mask, xlt = c.lst("seg_base"), c.lst("seg_mask"), c.lst("seg_xlt")
    dst, dprt = c.lst("seg_dst"), c.lst("seg_dprt")
    ns = len(base)
    L += [
        f"    localparam [{ns}*{AW}-1:0] SEG_BASE  = {cat(base, AW)};",
        f"    localparam [{ns}*{AW}-1:0] SEG_MASK  = {cat(mask, AW)};",
        f"    localparam [{ns}*{AW}-1:0] SEG_XLT   = {cat(xlt, AW)};",
        f"    localparam [{ns}*2-1:0]  SEG_DST   = {cat(dst, 2)};",
        f"    localparam [{ns}*2-1:0]  SEG_DPORT = {cat(dprt, 2)};",
        "",
    ]
    for m, _ in dies:
        for q in range(NQ):
            p = f"M{m * NQ + q:02d}"
            if q == 0:
                L += decl(p, AXI4, AW, FW, 4)
            elif q == 1:
                L += decl(p, AXI4, AW, 32, 4)
            else:
                L += decl(p, LITE, AW, 32, 4)

    sp = [
        f".FW({FW})",
        f".OST({c.i('OST')})",
        f".STORE_FWD({c.i('STORE_FWD')})",
        f".LUT_PER_BRAM({c.i('LUT_PER_BRAM')})",
        f".LPB1({c.i('SB_LPB1')})",
        f".TIMEOUT({c.i('TIMEOUT')})",
        f".LINK_CDC({c.i('LINK_CDC')})",
        f".LINK_FULL({c.i('LINK_FULL')})",
        f".LINK_KTS({c.i('LINK_KTS')})",
        f".MGR0_DOM({c.i('MGR0_DOM')})",
        f".CRED({c.i('CRED')})",
        f".PIPE({c.i('PIPE')})",
    ]
    for k in range(3):
        sp += [
            f".MREQ{k}({c.i(f'SB_MREQ{k}')})",
            f".MRSP{k}({c.i(f'SB_MRSP{k}')})",
            f".MMAXB{k}({c.i(f'SB_MMAXB{k}')})",
        ]
    sp += [
        ".SEG_OVERRIDE(1)",
        ".SEG_BASE_P(SEG_BASE)",
        ".SEG_MASK_P(SEG_MASK)",
        ".SEG_XLT_P(SEG_XLT)",
        ".SEG_DST_P(SEG_DST)",
        ".SEG_DPORT_P(SEG_DPORT)",
        f".SEG_VLD_P({ns}'h{(1 << ns) - 1:x})",
    ]
    L += ["", f"    {c.s('SB_WRAP')} #("] + wrap(sp, 8) + ["    ) u_bus ("]
    bc = []
    for m, _ in dies:
        bc += [
            f".bus_clk{m}(bus_clk[{m}])",
            f".bus_rst{m}(!rstn_bus[{m}])",
            f".clk_s{m}(sys_clk[{m}])",
            f".aresetn_s{m}(rstn_sys[{m}])",
            f".clk_ddr{m}(ddr_clk[{m}])",
            f".aresetn_ddr{m}(rstn_ddr[{m}])",
        ]
    bc += [
        ".clk_ctrl(clk_ctrl)",
        ".aresetn_ctrl(rstn_ctrl)",
        ".clk_xdma(clk_xdma)",
        ".aresetn_xdma(rstn_xdma)",
    ]
    bc += conn("S00_AXI", "h0", AXI4) + conn("S01_AXI", "h1", AXI4)
    bc += conn("S02_AXI", "h2", LITE)
    for m, _ in dies:
        for q in range(NQ):
            p = f"M{m * NQ + q:02d}"
            bc += conn(f"{p}_AXI", p, AXI4 if q < 2 else LITE)
    bc.append(".stat_decerr(stat_decerr)")
    L += wrap(bc, 8) + ["    );", ""]

    # ---- per die
    divs = [c.i(x) for x in ("DIV_MESH", "DIV_MAT2X", "DIV_VEC", "DIV_SYS")]
    div0 = "{" + ", ".join(f"8'd{d}" for d in reversed(divs)) + "}"
    for m, mod in dies:
        p0, p1, p2, p3 = (f"M{m * NQ + q:02d}" for q in range(NQ))
        L += [f"    // ==== die {m} ====", ""]
        L += decl(f"c{m}", AXI4, 32, 64, 4)
        L += decl(f"x{m}", AXI4, 40, 512, 4)
        L += [
            "    // the BD's axi_dwidth_converter SI 32 -> MI 64, on the sysnode clock",
            f"    axi_up32to64 #(.AW(32), .IDW(4)) u_dwc{m} (.clk(sys_clk[{m}]), .resetn(rstn_sys[{m}]),",
        ]
        dc = conn("s", p1, AXI4, {"awaddr": "[31:0]", "araddr": "[31:0]"})
        dc += conn("m", f"c{m}", AXI4)
        L += wrap(dc, 8) + ["    );"]
        if m in compute:
            mp = [
                f".MESH_ID({m})",
                f".L2_MAG_BANKS({c.i('L2_MAG_BANKS')})",
                f".L2_MAG_ENTRIES({c.i('L2_MAG_ENTRIES')})",
                f".DRAM_CDC({c.i('DRAM_CDC')})",
                f".DRAM_AR_MAX({c.i('KX_RB_BEATS')})",
                f".GA({c.i('MESH_GA')})",
                f".GB({c.i('MESH_GB')})",
                f".TILES({c.i('MESH_TILES')})",
                f'.TILE_PRIM("{c.s("MESH_TILE_PRIM")}")',
                f'.VEC_PRIM("{c.s("MESH_VEC_PRIM")}")',
                f".MAG_CDC({c.i('MESH_MAG_CDC')})",
                f".UNIT_CDC({c.i('MESH_UNIT_CDC')})",
                ".MODEL(`MX_MODEL)",
            ] + [f".{k}({v})" for k, v in mesh_extra]
            L += [
                "    // matmul 1x from the 2x, cleared by NOT(lock)",
                f"    wire mat_clk{m};",
                f"    ktpu_div2 u_div2_{m} (.clk2x(mat2x_clk[{m}]), .clr(!wiz_locked[{m}]), .clk1x(mat_clk{m}));",
                f"    wire [63:0] hs_rdata{m};",
                f"    wire hs_cwe{m};",
                f"    wire [7:0] hs_c{m};",
                f"    {mod} #(",
            ]
            L += wrap(mp, 8) + [f"    ) u_mesh{m} ("]
            mc = [
                f".axi_aclk(sys_clk[{m}])",
                f".axi_aresetn(rstn_sys[{m}])",
                f".dram_aclk(sys_clk[{m}])",
                f".dram_aresetn(rstn_sys[{m}])",
                f".noc_clk(noc_clk[{m}])",
                f".vec_clk(vec_clk[{m}])",
                f".mat_clk(mat_clk{m})",
                f".mat_clk2x(mat2x_clk[{m}])",
                ".hs_addr(32'd0)",
                ".hs_wr(1'b0)",
                ".hs_wdata(64'd0)",
                ".hs_wstrb(8'd0)",
                ".hs_rd(1'b0)",
                f".hs_rdata(hs_rdata{m})",
                f".hs_console_we(hs_cwe{m})",
                f".hs_console(hs_c{m})",
            ]
            mc += conn("S_AXI_MEM", p0, NODE, {"awaddr": "[39:0]", "araddr": "[39:0]"})
            mc += conn("S_AXI_CTRL", f"c{m}", NODE)
            mc += conn("M_AXI_DRAM", f"x{m}", AXI4)
            for side in ("LINK0", "LINK1"):
                for f in ("valid", "vc", "last", "flit"):
                    mc += [
                        f".{side}_OUT_{f}(l{m}_{side}_OUT_{f})",
                        f".{side}_IN_{f}(l{m}_{side}_IN_{f})",
                    ]
                for f in ("crd_valid", "crd_vc", "crd_n"):
                    mc += [
                        f".{side}_OUT_{f}(l{m}_{side}_OUT_{f})",
                        f".{side}_IN_{f}(l{m}_{side}_IN_{f})",
                    ]
            pre = []
            for side in ("LINK0", "LINK1"):
                for d in ("OUT", "IN"):
                    pre += [
                        f"    wire l{m}_{side}_{d}_valid, l{m}_{side}_{d}_vc, l{m}_{side}_{d}_last;",
                        f"    wire [{ILW - 1}:0] l{m}_{side}_{d}_flit;",
                        f"    wire l{m}_{side}_{d}_crd_valid, l{m}_{side}_{d}_crd_vc;",
                        f"    wire [{ILC - 1}:0] l{m}_{side}_{d}_crd_n;",
                    ]
            idx = L.index(f"    wire mat_clk{m};")
            L[idx:idx] = pre
            L += wrap(mc, 8) + ["    );", ""]
        else:
            L += ["    // no node: RAMs behind both windows, the Xache master idle"]
            for nm, sig, dw in ((f"u_nomem{m}", p0, FW), (f"u_noctl{m}", f"c{m}", 64)):
                rc = conn("s", sig, AXI4, {"awaddr": "[15:0]", "araddr": "[15:0]"})
                rc += [
                    ".bd_we(1'b0)",
                    ".bd_addr(16'd0)",
                    f".bd_wdata({dw}'d0)",
                    ".bd_rdata()",
                ]
                L += [
                    f"    axi_ram #(.DATA_W({dw}), .ADDR_W(16), .ID_W(4), .WORDS(512), .PORTS(1)) {nm} (",
                    f"        .clk(sys_clk[{m}]), .resetn(rstn_sys[{m}]),",
                ]
                L += wrap(rc, 8) + ["    );"]
            for nm, w, d in AXI4:
                if d == "o":
                    L.append(f"    assign x{m}_{nm} = 0;")
            L.append("")
        L += [
            f"    axil_ram #(.AW({AW}), .WORDS(256)) u_ddrctl{m} (.clk(ddr_clk[{m}]), .resetn(rstn_ddr[{m}]),",
        ]
        L += wrap(conn("s", p2, LITE), 8) + ["    );"]
        L += [
            f"    clk_wiz_model #(.AW({AW}), .D({c.i('VCO_D')}), .M({c.i('VCO_M')}), .DIV0({div0}),",
            f"                    .LOCK_CYC(LOCK_CYC)) u_wiz{m} (.clk(clk_ctrl), .resetn(rstn_ctrl),",
        ]
        wc = conn("s", p3, LITE) + [
            f".locked(wiz_locked[{m}])",
            f".divclk(wiz_divclk[{m}*8 +: 8])",
            f".mult(wiz_mult[{m}*8 +: 8])",
            f".div(wiz_div[{m}*32 +: 32])",
        ]
        L += wrap(wc, 8) + ["    );", ""]

    # ---- interlink: 30_meshes.tcl's chain over the compute dies
    order = sorted(compute)
    linked = set()
    L += [
        "    // ---- interlink (30_meshes.tcl): one kts_pipe_bd per hop and direction"
    ]
    ip = [
        f".W({ILW})",
        f".VCW({c.i('IL_VCW')})",
        f".CN_W({ILC})",
        f".ASYNC({c.i('IL_ASYNC')})",
        f".CRED({c.i('IL_CRED')})",
        f".STAGES({c.i('IL_STAGES')})",
    ]
    for lo, hi in itertools.pairwise(order):
        for tx, rx, sp_, dp_ in (
            (lo, hi, "LINK1_OUT", "LINK0_IN"),
            (hi, lo, "LINK0_OUT", "LINK1_IN"),
        ):
            nm = f"u_pipe_{tx}_to_{rx}"
            pc = [
                f".clk(sys_clk[{tx}])",
                f".clk_rx(sys_clk[{rx}])",
                f".rstn_tx(rstn_sys[{tx}])",
                f".rstn_rx(rstn_sys[{rx}])",
            ]
            for f in ("valid", "vc", "last", "flit"):
                pc += [f".i_{f}(l{tx}_{sp_}_{f})", f".o_{f}(l{rx}_{dp_}_{f})"]
            for f in ("crd_valid", "crd_vc", "crd_n"):
                pc += [f".i_{f}(l{rx}_{dp_}_{f})", f".o_{f}(l{tx}_{sp_}_{f})"]
            L += ["    kts_pipe_bd #("] + wrap(ip, 8, 6) + [f"    ) {nm} ("]
            L += wrap(pc, 8) + ["    );"]
        linked |= {(lo, "LINK1"), (hi, "LINK0")}
    for m in order:
        for side in ("LINK0", "LINK1"):
            if (m, side) in linked:
                continue
            L += [
                f"    assign l{m}_{side}_IN_valid = 1'b0;",
                f"    assign l{m}_{side}_IN_vc = 1'b0;",
                f"    assign l{m}_{side}_IN_last = 1'b0;",
                f"    assign l{m}_{side}_IN_flit = {ILW}'d0;",
                f"    assign l{m}_{side}_OUT_crd_valid = 1'b0;",
                f"    assign l{m}_{side}_OUT_crd_vc = 1'b0;",
                f"    assign l{m}_{side}_OUT_crd_n = {ILC}'d0;",
            ]
    L.append("")

    # ---- Xache
    xp = [
        f".SETS({c.i('KX_SETS')})",
        f".SET_W({c.i('KX_SET_W')})",
        f".K({c.i('KX_K')})",
        '.RAM_STYLE("ultra")',
        f".BANKS({c.i('KX_BANKS')})",
        f".CDC_DEPTH({c.i('KX_CDC_DEPTH')})",
        f".RD_OUTQ({c.i('KX_RD_OUTQ')})",
        f".WR_OUTQ({c.i('KX_WR_OUTQ')})",
        f".RB_BEATS({c.i('KX_RB_BEATS')})",
        f".HOP_DEPTH({c.i('KX_HOP_DEPTH')})",
        f".HOP_RXREG({c.i('KX_HOP_RXREG')})",
        f".BND_TRUNK({c.i('KX_BND_TRUNK')})",
        f".PCLK({c.i('KX_PCLK')})",
        f".NSWAP({c.i('KX_NSWAP')})",
        f'.MEM_TRUNK("{c.s("KX_MEM_TRUNK")}")',
        f'.MEM_RB("{c.s("KX_MEM_RB")}")',
        f'.MEM_HRD("{c.s("KX_MEM_HRD")}")',
        f'.MEM_HWR("{c.s("KX_MEM_HWR")}")',
        f".SWAP_A({swap_bytes(c.lst('KX_SWAP_A'))})",
        f".SWAP_B({swap_bytes(c.lst('KX_SWAP_B'))})",
    ]
    L += ["    // ---- the Xache (35_xache.tcl): master m = die m, home h = channel h"]
    L += decl("d0", AXI4, 40, 512, XIDW) + decl("d1", AXI4, 40, 512, XIDW)
    L += decl("d2", AXI4, 40, 512, XIDW) + decl("d3", AXI4, 40, 512, XIDW)
    L += ["    kx_pbd_4x4 #("] + wrap(xp, 8, 3) + ["    ) u_xache ("]
    kc = []
    for m, _ in dies:
        kc += [
            f".p_clk{m}(sys_clk[{m}])",
            f".d_rstn{m}(rstn_sys[{m}])",
            f".h_clk{m}(ddr_clk[{m}])",
            f".h_rstn{m}(rstn_ddr[{m}])",
        ]
        kc += conn(f"S0{m}_AXI", f"x{m}", AXI4) + conn(f"M0{m}_AXI", f"d{m}", AXI4)
    L += wrap(kc, 8) + ["    );", ""]

    L += ["    // ---- DRAM: one axi_ram per channel on its ui clock"]
    for h in range(n):
        rc = conn("s", f"d{h}", AXI4) + [
            f".bd_we(bd_we[{h}])",
            f".bd_addr(bd_addr[{h}*16 +: 16])",
            f".bd_wdata(bd_wdata[{h}*512 +: 512])",
            f".bd_rdata(bd_rdata[{h}*512 +: 512])",
        ]
        L += [
            f"    axi_ram #(.DATA_W(512), .ADDR_W(40), .ID_W({XIDW}), .WORDS(DRAM_WORDS), .PORTS(1)) u_dram{h} (",
            f"        .clk(ddr_clk[{h}]), .resetn(rstn_ddr[{h}]),",
        ]
        L += wrap(rc, 8) + ["    );"]
    L += ["endmodule", "", "`default_nettype wire", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ver", default="v8t8")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--module", default=None)
    ap.add_argument("--sim-mesh", default="")
    ap.add_argument("--compute", default="0,1,2,3")
    ap.add_argument("--dram-words", type=int, default=1 << 16)
    ap.add_argument("--lock-cyc", type=int, default=64)
    ap.add_argument("--set", action="append", default=[], help="Tcl NAME=VALUE")
    ap.add_argument("--mesh-param", action="append", default=[], help="NAME=VALUE")
    a = ap.parse_args()
    sets = pairs(a.set)
    mesh_extra = pairs(a.mesh_param)
    c = Config(a.ver, sets)
    compute = {int(x) for x in a.compute.split(",") if x != ""}
    text = emit(
        c,
        a.module or f"card_{a.ver}",
        a.sim_mesh,
        compute,
        a.dram_words,
        a.lock_cyc,
        mesh_extra,
    )
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {a.out}: {text.count(chr(10))} lines")


if __name__ == "__main__":
    main()
