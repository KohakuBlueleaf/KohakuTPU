#!/usr/bin/env python3
"""Emit BD-facing wrappers (root, leaf, link) around the station-bus blocks.

    gen_station_wrap.py --kind root --nk 3 --mgr-w 32,512,32 --loc-w 512,32 \
        -o src/kohakuaccel/axi/bd/sb_bd_root.v

Vivado infers an interface from a port NAMING CONVENTION, so the stations'
flattened buses arrive in a BD as loose wires. Each named port here becomes one
inferred interface; the link is AXIS because a credited flit stream is AXIS.

The link is a separate block on purpose: its pipeline registers must be free to
place across the die, which a station pinned to a per-SLR pblock cannot allow.
"""

import argparse
import sys
from pathlib import Path

#: (signal, width expression or None for 1 bit, direction on a SLAVE port).
FIELDS = [
    ("awid", "IDW", "i"),
    ("awaddr", "AW", "i"),
    ("awlen", "8", "i"),
    ("awsize", "3", "i"),
    ("awburst", "2", "i"),
    ("awvalid", None, "i"),
    ("awready", None, "o"),
    ("wdata", "DW", "i"),
    ("wstrb", "DW/8", "i"),
    ("wlast", None, "i"),
    ("wvalid", None, "i"),
    ("wready", None, "o"),
    ("bid", "IDW", "o"),
    ("bresp", "2", "o"),
    ("bvalid", None, "o"),
    ("bready", None, "i"),
    ("arid", "IDW", "i"),
    ("araddr", "AW", "i"),
    ("arlen", "8", "i"),
    ("arsize", "3", "i"),
    ("arburst", "2", "i"),
    ("arvalid", None, "i"),
    ("arready", None, "o"),
    ("rid", "IDW", "o"),
    ("rdata", "DW", "o"),
    ("rresp", "2", "o"),
    ("rlast", None, "o"),
    ("rvalid", None, "o"),
    ("rready", None, "i"),
]

ID_FIELDS = {"awid", "bid", "arid", "rid"}

#: Signals AXI4-Lite lacks. XDMA's M_AXI_LITE cannot connect to a full-AXI4
#: port (BD 41-1285), and sb_line4 drives these single-beat via MAX_BURST=1.
LITE_DROP = {
    "awid",
    "awlen",
    "awsize",
    "awburst",
    "wlast",
    "bid",
    "arid",
    "arlen",
    "arsize",
    "arburst",
    "rid",
    "rlast",
}

AW = 40
MAXW = 512
MAXID = 4
FW = 512
TAGW = 4
DSTW = 3
SRCW = 2


def req_w():
    return SRCW + DSTW + TAGW + 3 + AW + 8 + 3 + FW + FW // 8


def rsp_w():
    return SRCW + TAGW + 2 + 2 + FW


def pad8(n):
    return (n + 7) // 8 * 8


def ev(expr, dw):
    """Width expression to an int. `/` must floor: `512/8` as a float emits
    `[0*64.0 +: 4.0]`, which Vivado rejects as a real in an integer context."""
    return eval(expr.replace("DW", str(dw)).replace("AW", str(AW)).replace("/", "//"))


def axi_decls(prefix, dw, mirror, lite=False):
    """Port declarations for one AXI interface named `prefix`, DW bits wide."""
    out = []
    for sig, w, d in FIELDS:
        if lite and sig in LITE_DROP:
            continue
        kind = d if not mirror else ("o" if d == "i" else "i")
        word = "output wire" if kind == "o" else "input  wire"
        width = str(MAXID) if sig in ID_FIELDS else w
        if width is not None:
            width = f"[{ev(width, dw)}-1:0] "
        else:
            width = ""
        out.append(f"    {word} {width}{prefix}_{sig},")
    return "\n".join(out)


def axi_glue(i, prefix, dw, bus, mirror, lite=False):
    """Wire named interface `prefix` into packed bus `bus` at index `i`.

    `mirror` is True for a manager-side (M_AXI) port, where the packed bus
    carries what the named port drives out. `lite` ties off the AXI4 signals
    the named port does not have, with the constants AXI4-Lite implies.
    """
    #: Constants AXI4-Lite implies. BOTH directions: a MASTER port mirrors, so
    #: rlast/bid/rid become things the wrapper drives into the packed bus.
    tie = {
        "awlen": "8'd0",
        "arlen": "8'd0",
        "awburst": "2'b01",
        "arburst": "2'b01",
        "wlast": "1'b1",
        "rlast": "1'b1",
    }
    out = []
    for sig, w, d in FIELDS:
        width = str(MAXID) if sig in ID_FIELDS else w
        if width is None:
            sl = f"[{i}]"
        else:
            sl = f"[{i}*{ev(width, MAXW)} +: {ev(width, dw)}]"
        drives_in = (d == "i") if not mirror else (d == "o")
        if lite and sig in LITE_DROP:
            if drives_in:
                if sig in ("awsize", "arsize"):
                    v = f"3'd{(dw // 8).bit_length() - 1}"
                elif sig in ID_FIELDS:
                    v = f"{ev(str(MAXID), dw)}'d0"
                else:
                    v = tie[sig]
                out.append(f"    assign {bus}_{sig}{sl} = {v};")
            # A signal the named port lacks in the other direction is dropped.
            continue
        if drives_in:
            out.append(f"    assign {bus}_{sig}{sl} = {prefix}_{sig};")
        else:
            out.append(f"    assign {prefix}_{sig} = {bus}_{sig}{sl};")
    return "\n".join(out)


def axi_lite_conv_glue(k, prefix, dw, bus, nq):
    """A Lite LOCAL port gets a real burst-to-Lite converter (sb_axi2lite)
    instead of dropped burst signals; termination leaves orphan W beats at the
    endpoint. Clock choice mirrors sb_line4's port_dom()."""
    stn, qp = k // nq, k % nq
    if qp < 2:
        clk, rstn = f"clk_s{stn}", f"aresetn_s{stn}"
    elif qp == 2:
        clk, rstn = f"clk_ddr{stn}", f"aresetn_ddr{stn}"
    else:
        clk, rstn = "clk_ctrl", "aresetn_ctrl"
    idw = MAXID
    return f"""    sb_axi2lite #(.DW({dw}), .AW({AW}), .IDW({idw})) u_a2l_{k} (
        .clk({clk}), .resetn({rstn}),
        .s_awid({bus}_awid[{k}*{idw} +: {idw}]),
        .s_awaddr({bus}_awaddr[{k}*{AW} +: {AW}]),
        .s_awlen({bus}_awlen[{k}*8 +: 8]),
        .s_awvalid({bus}_awvalid[{k}]), .s_awready({bus}_awready[{k}]),
        .s_wdata({bus}_wdata[{k}*{MAXW} +: {dw}]),
        .s_wstrb({bus}_wstrb[{k}*{MAXW // 8} +: {dw // 8}]),
        .s_wlast({bus}_wlast[{k}]),
        .s_wvalid({bus}_wvalid[{k}]), .s_wready({bus}_wready[{k}]),
        .s_bid({bus}_bid[{k}*{idw} +: {idw}]),
        .s_bresp({bus}_bresp[{k}*2 +: 2]),
        .s_bvalid({bus}_bvalid[{k}]), .s_bready({bus}_bready[{k}]),
        .s_arid({bus}_arid[{k}*{idw} +: {idw}]),
        .s_araddr({bus}_araddr[{k}*{AW} +: {AW}]),
        .s_arlen({bus}_arlen[{k}*8 +: 8]),
        .s_arvalid({bus}_arvalid[{k}]), .s_arready({bus}_arready[{k}]),
        .s_rid({bus}_rid[{k}*{idw} +: {idw}]),
        .s_rdata({bus}_rdata[{k}*{MAXW} +: {dw}]),
        .s_rresp({bus}_rresp[{k}*2 +: 2]),
        .s_rlast({bus}_rlast[{k}]),
        .s_rvalid({bus}_rvalid[{k}]), .s_rready({bus}_rready[{k}]),
        .m_awaddr({prefix}_awaddr),
        .m_awvalid({prefix}_awvalid), .m_awready({prefix}_awready),
        .m_wdata({prefix}_wdata), .m_wstrb({prefix}_wstrb),
        .m_wvalid({prefix}_wvalid), .m_wready({prefix}_wready),
        .m_bresp({prefix}_bresp),
        .m_bvalid({prefix}_bvalid), .m_bready({prefix}_bready),
        .m_araddr({prefix}_araddr),
        .m_arvalid({prefix}_arvalid), .m_arready({prefix}_arready),
        .m_rdata({prefix}_rdata), .m_rresp({prefix}_rresp),
        .m_rvalid({prefix}_rvalid), .m_rready({prefix}_rready)
    );"""


def axis_decls(prefix, w, master):
    d0, d1 = (
        ("output wire", "input  wire") if master else ("input  wire", "output wire")
    )
    return (
        f"    {d0} [{w}-1:0] {prefix}_tdata,\n"
        f"    {d0} {prefix}_tvalid,\n"
        f"    {d1} {prefix}_tready,"
    )


def seg_table(nk):
    """The regular validation map: the top 4 address bits are the SLR, bit 16
    the endpoint, so a manager's whole map is one masked compare per endpoint.

    Returns (nseg, base, mask, dst, dport) as Verilog concatenation literals.
    Root is SLR1; link k reaches SLR (0, 2, 3)[k].
    """
    slr_of_link = [0, 2, 3][:nk]
    nd = (AW + 3) // 4
    rows = []
    for slr in [1] + slr_of_link:
        for ep in range(2):
            dst = ep if slr == 1 else 2 + slr_of_link.index(slr)
            rows.append(((slr << (AW - 4)) | (ep << 16), dst, ep))
    # Verilog concatenation is most-significant first, so emit in reverse.
    base = ", ".join(f"{AW}'h{b:0{nd}X}" for b, _, _ in reversed(rows))
    dst = ", ".join(f"3'd{d}" for _, d, _ in reversed(rows))
    dpt = ", ".join(f"3'd{p}" for _, _, p in reversed(rows))
    msk = ((1 << AW) - 1) ^ 0xFFFF
    return (len(rows), base, f"{{{len(rows)}{{{AW}'h{msk:0{nd}X}}}}}", dst, dpt)


def emit_root(name, mgr_w, loc_w, nk):
    nm, nl = len(mgr_w), len(loc_w)
    s_names = [f"S{i:02d}_AXI" for i in range(nm)]
    m_names = [f"M{i:02d}_AXI" for i in range(nl)]
    nseg, base, mask, dst, dpt = seg_table(nk)
    rw, sw = pad8(req_w()), pad8(rsp_w())

    lk_decls = "\n".join(
        axis_decls(f"L{k}_REQ", rw, True) + "\n" + axis_decls(f"L{k}_RSP", sw, False)
        for k in range(nk)
    )
    lk_glue = "\n".join(
        f"    assign L{k}_REQ_tdata  = {{{rw - req_w()}'d0, lk_req_pay}};\n"
        f"    assign L{k}_REQ_tvalid = lk_req_valid[{k}];\n"
        f"    assign lk_req_ready[{k}] = L{k}_REQ_tready;\n"
        f"    assign lk_rsp_valid[{k}] = L{k}_RSP_tvalid;\n"
        f"    assign L{k}_RSP_tready = lk_rsp_ready[{k}];\n"
        f"    assign lk_rsp_pay[{k}*{rsp_w()} +: {rsp_w()}] = "
        f"L{k}_RSP_tdata[{rsp_w()}-1:0];"
        for k in range(nk)
    )

    # bus_clk owns ONLY the links. Listing the local AXI ports here too makes
    # IPI report them as multiply-clocked and pick a frequency at random.
    assoc_m = ":".join(f"L{k}_REQ:L{k}_RSP" for k in range(nk))

    s_decls = "\n\n".join(axi_decls(p, w, False) for p, w in zip(s_names, mgr_w))
    m_decls = "\n\n".join(axi_decls(p, w, True) for p, w in zip(m_names, loc_w))
    s_glue = "\n\n".join(
        axi_glue(i, p, w, "mp", False) for i, (p, w) in enumerate(zip(s_names, mgr_w))
    )
    m_glue = "\n\n".join(
        axi_glue(i, p, w, "sp", True) for i, (p, w) in enumerate(zip(m_names, loc_w))
    )
    loc_bits = sum((1 << i) for i, w in enumerate(loc_w) if w == FW)

    return f"""// {name} -- GENERATED by scripts/py/gen_station_wrap.py. Do not edit by hand.
//
// The root station with every interface named so a block design infers it:
// {nm} AXI4 slaves, {nl} local AXI4 masters, {nk} AXIS links to other stations.
// The logic is all in sb_stn_root.v; this file is declarations and assigns.

`default_nettype none

module {name} #(
    parameter integer OST          = 4,
    parameter integer STORE_FWD    = 1,
    parameter integer LUT_PER_BRAM = 0,
    parameter integer TIMEOUT      = 0
)(
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 bus_clk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF {assoc_m}, ASSOCIATED_RESET bus_rst" *)
    input  wire bus_clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 bus_rst RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_HIGH" *)
    input  wire bus_rst,

    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_ctrl CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF {s_names[0]}, ASSOCIATED_RESET aresetn_ctrl" *)
    input  wire clk_ctrl,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn_ctrl RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire aresetn_ctrl,

    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_xdma CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF {':'.join(s_names[1:])}, ASSOCIATED_RESET aresetn_xdma" *)
    input  wire clk_xdma,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn_xdma RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire aresetn_xdma,

    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_loc CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF {':'.join(m_names)}, ASSOCIATED_RESET aresetn_loc" *)
    input  wire clk_loc,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn_loc RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire aresetn_loc,

{s_decls}

{m_decls}

{lk_decls}

    output wire [31:0] stat_decerr
);
    localparam integer NM   = {nm};
    localparam integer NL   = {nl};
    localparam integer NK   = {nk};
    localparam integer AW   = {AW};
    localparam integer MAXW = {MAXW};
    localparam integer MAXID = {MAXID};
    localparam integer FW   = {FW};

    localparam integer NSEG = {nseg};
    localparam [NSEG*AW-1:0]   Q_BASE = {{{base}}};
    localparam [NSEG*AW-1:0]   Q_MASK = {mask};
    localparam [NSEG*3-1:0]    Q_DST  = {{{dst}}};
    localparam [NSEG*3-1:0]    Q_DPT  = {{{dpt}}};

    wire [NM*MAXID-1:0]    mp_awid, mp_arid, mp_bid, mp_rid;
    wire [NM*AW-1:0]       mp_awaddr, mp_araddr;
    wire [NM*8-1:0]        mp_awlen, mp_arlen;
    wire [NM*3-1:0]        mp_awsize, mp_arsize;
    wire [NM*2-1:0]        mp_awburst, mp_arburst, mp_bresp, mp_rresp;
    wire [NM-1:0]          mp_awvalid, mp_awready, mp_wvalid, mp_wready;
    wire [NM-1:0]          mp_wlast, mp_bvalid, mp_bready;
    wire [NM-1:0]          mp_arvalid, mp_arready, mp_rvalid, mp_rready, mp_rlast;
    wire [NM*MAXW-1:0]     mp_wdata, mp_rdata;
    wire [NM*(MAXW/8)-1:0] mp_wstrb;

    wire [NL*MAXID-1:0]    sp_awid, sp_arid, sp_bid, sp_rid;
    wire [NL*AW-1:0]       sp_awaddr, sp_araddr;
    wire [NL*8-1:0]        sp_awlen, sp_arlen;
    wire [NL*3-1:0]        sp_awsize, sp_arsize;
    wire [NL*2-1:0]        sp_awburst, sp_arburst, sp_bresp, sp_rresp;
    wire [NL-1:0]          sp_awvalid, sp_awready, sp_wvalid, sp_wready;
    wire [NL-1:0]          sp_wlast, sp_bvalid, sp_bready;
    wire [NL-1:0]          sp_arvalid, sp_arready, sp_rvalid, sp_rready, sp_rlast;
    wire [NL*MAXW-1:0]     sp_wdata, sp_rdata;
    wire [NL*(MAXW/8)-1:0] sp_wstrb;

    wire [NK-1:0]      lk_req_valid, lk_req_ready, lk_rsp_valid, lk_rsp_ready;
    wire [{req_w()}-1:0]  lk_req_pay;
    wire [NK*{rsp_w()}-1:0] lk_rsp_pay;

{s_glue}

{m_glue}

{lk_glue}

    wire [2:0]   rq_dport;
    wire [1:0]   rq_src;
    wire [3:0]   rq_tag;
    wire         rq_wr, rq_head, rq_last;
    wire [AW-1:0] rq_addr;
    wire [7:0]   rq_len;
    wire [2:0]   rq_size;
    wire [FW-1:0] rq_data;
    wire [FW/8-1:0] rq_strb;

    assign lk_req_pay = {{rq_src, rq_dport, rq_tag, rq_wr, rq_head, rq_last,
                        rq_addr, rq_len, rq_size, rq_data, rq_strb}};

    wire [NK*2-1:0]  rs_dst;
    wire [NK*4-1:0]  rs_tag;
    wire [NK-1:0]    rs_wr, rs_last;
    wire [NK*2-1:0]  rs_resp;
    wire [NK*FW-1:0] rs_data;

    genvar k;
    generate
    for (k = 0; k < NK; k = k + 1) begin : g_unpack
        assign {{rs_dst[k*2 +: 2], rs_tag[k*4 +: 4], rs_wr[k], rs_last[k],
                rs_resp[k*2 +: 2], rs_data[k*FW +: FW]}} =
            lk_rsp_pay[k*{rsp_w()} +: {rsp_w()}];
    end
    endgenerate

    sb_stn_root #(.FW(FW), .AW(AW), .MAXW(MAXW), .MAXID(MAXID), .NM(NM),
                  .NL(NL), .NK(NK), .TAGW({TAGW}), .DSTW({DSTW}), .SRCW({SRCW}),
                  .OST(OST), .STORE_FWD(STORE_FWD),
                  .LUT_PER_BRAM(LUT_PER_BRAM), .TIMEOUT(TIMEOUT),
                  .LOC_W(32'h{loc_bits:08X}), .NSEG(NSEG),
                  .SEG_BASE(Q_BASE), .SEG_MASK(Q_MASK),
                  .SEG_DST(Q_DST), .SEG_DPORT(Q_DPT)) u_stn (
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .clk_ctrl(clk_ctrl), .aresetn_ctrl(aresetn_ctrl),
        .clk_xdma(clk_xdma), .aresetn_xdma(aresetn_xdma),
        .clk_loc(clk_loc), .aresetn_loc(aresetn_loc),
        .mp_awid(mp_awid), .mp_awaddr(mp_awaddr), .mp_awlen(mp_awlen),
        .mp_awsize(mp_awsize), .mp_awburst(mp_awburst),
        .mp_awvalid(mp_awvalid), .mp_awready(mp_awready),
        .mp_wdata(mp_wdata), .mp_wstrb(mp_wstrb), .mp_wlast(mp_wlast),
        .mp_wvalid(mp_wvalid), .mp_wready(mp_wready),
        .mp_bid(mp_bid), .mp_bresp(mp_bresp), .mp_bvalid(mp_bvalid),
        .mp_bready(mp_bready),
        .mp_arid(mp_arid), .mp_araddr(mp_araddr), .mp_arlen(mp_arlen),
        .mp_arsize(mp_arsize), .mp_arburst(mp_arburst),
        .mp_arvalid(mp_arvalid), .mp_arready(mp_arready),
        .mp_rid(mp_rid), .mp_rdata(mp_rdata), .mp_rresp(mp_rresp),
        .mp_rlast(mp_rlast), .mp_rvalid(mp_rvalid), .mp_rready(mp_rready),
        .sp_awid(sp_awid), .sp_awaddr(sp_awaddr), .sp_awlen(sp_awlen),
        .sp_awsize(sp_awsize), .sp_awburst(sp_awburst),
        .sp_awvalid(sp_awvalid), .sp_awready(sp_awready),
        .sp_wdata(sp_wdata), .sp_wstrb(sp_wstrb), .sp_wlast(sp_wlast),
        .sp_wvalid(sp_wvalid), .sp_wready(sp_wready),
        .sp_bid(sp_bid), .sp_bresp(sp_bresp), .sp_bvalid(sp_bvalid),
        .sp_bready(sp_bready),
        .sp_arid(sp_arid), .sp_araddr(sp_araddr), .sp_arlen(sp_arlen),
        .sp_arsize(sp_arsize), .sp_arburst(sp_arburst),
        .sp_arvalid(sp_arvalid), .sp_arready(sp_arready),
        .sp_rid(sp_rid), .sp_rdata(sp_rdata), .sp_rresp(sp_rresp),
        .sp_rlast(sp_rlast), .sp_rvalid(sp_rvalid), .sp_rready(sp_rready),
        .lk_req_valid(lk_req_valid), .lk_req_ready(lk_req_ready),
        .lk_req_dport(rq_dport), .lk_req_src(rq_src), .lk_req_tag(rq_tag),
        .lk_req_wr(rq_wr), .lk_req_head(rq_head), .lk_req_last(rq_last),
        .lk_req_addr(rq_addr), .lk_req_len(rq_len), .lk_req_size(rq_size),
        .lk_req_data(rq_data), .lk_req_strb(rq_strb),
        .lk_rsp_valid(lk_rsp_valid), .lk_rsp_ready(lk_rsp_ready),
        .lk_rsp_dst(rs_dst), .lk_rsp_tag(rs_tag), .lk_rsp_wr(rs_wr),
        .lk_rsp_last(rs_last), .lk_rsp_resp(rs_resp), .lk_rsp_data(rs_data),
        .stat_decerr(stat_decerr)
    );
endmodule

`default_nettype wire
"""


def emit_leaf(name, loc_w):
    ns = len(loc_w)
    m_names = [f"M{i:02d}_AXI" for i in range(ns)]
    rw, sw = pad8(req_w()), pad8(rsp_w())
    m_decls = "\n\n".join(axi_decls(p, w, True) for p, w in zip(m_names, loc_w))
    m_glue = "\n\n".join(
        axi_glue(i, p, w, "sp", True) for i, (p, w) in enumerate(zip(m_names, loc_w))
    )
    loc_bits = sum((1 << i) for i, w in enumerate(loc_w) if w == FW)

    return f"""// {name} -- GENERATED by scripts/py/gen_station_wrap.py. Do not edit by hand.
//
// A leaf station with every interface named so a block design infers it: one
// AXIS link in, one AXIS link out, {ns} local AXI4 masters on one clock.

`default_nettype none

module {name} #(
    parameter integer OST          = 4,
    parameter integer LUT_PER_BRAM = 0,
    parameter integer TIMEOUT      = 0
)(
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 bus_clk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF L_REQ:L_RSP, ASSOCIATED_RESET bus_rst" *)
    input  wire bus_clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 bus_rst RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_HIGH" *)
    input  wire bus_rst,

    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_loc CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF {':'.join(m_names)}, ASSOCIATED_RESET aresetn_loc" *)
    input  wire clk_loc,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn_loc RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire aresetn_loc,

{axis_decls("L_REQ", rw, False)}
{axis_decls("L_RSP", sw, True)}

{m_decls[:-1]}
);
    localparam integer NS   = {ns};
    localparam integer AW   = {AW};
    localparam integer MAXW = {MAXW};
    localparam integer MAXID = {MAXID};
    localparam integer FW   = {FW};

    wire [NS*MAXID-1:0]    sp_awid, sp_arid, sp_bid, sp_rid;
    wire [NS*AW-1:0]       sp_awaddr, sp_araddr;
    wire [NS*8-1:0]        sp_awlen, sp_arlen;
    wire [NS*3-1:0]        sp_awsize, sp_arsize;
    wire [NS*2-1:0]        sp_awburst, sp_arburst, sp_bresp, sp_rresp;
    wire [NS-1:0]          sp_awvalid, sp_awready, sp_wvalid, sp_wready;
    wire [NS-1:0]          sp_wlast, sp_bvalid, sp_bready;
    wire [NS-1:0]          sp_arvalid, sp_arready, sp_rvalid, sp_rready, sp_rlast;
    wire [NS*MAXW-1:0]     sp_wdata, sp_rdata;
    wire [NS*(MAXW/8)-1:0] sp_wstrb;

{m_glue}

    wire [1:0]      lq_src;
    wire [2:0]      lq_dpt;
    wire [3:0]      lq_tag;
    wire            lq_wr, lq_head, lq_last;
    wire [AW-1:0]   lq_addr;
    wire [7:0]      lq_len;
    wire [2:0]      lq_size;
    wire [FW-1:0]   lq_data;
    wire [FW/8-1:0] lq_strb;

    assign {{lq_src, lq_dpt, lq_tag, lq_wr, lq_head, lq_last, lq_addr, lq_len,
            lq_size, lq_data, lq_strb}} = L_REQ_tdata[{req_w()}-1:0];

    wire [1:0]    ls_dst;
    wire [3:0]    ls_tag;
    wire          ls_wr, ls_last;
    wire [1:0]    ls_resp;
    wire [FW-1:0] ls_data;

    assign L_RSP_tdata = {{{sw - rsp_w()}'d0, ls_dst, ls_tag, ls_wr, ls_last,
                         ls_resp, ls_data}};

    sb_stn_leaf #(.FW(FW), .AW(AW), .MAXW(MAXW), .MAXID(MAXID), .NS(NS),
                  .TAGW({TAGW}), .DSTW({DSTW}), .SRCW({SRCW}), .OST(OST),
                  .TIMEOUT(TIMEOUT), .LUT_PER_BRAM(LUT_PER_BRAM),
                  .LOC_W(32'h{loc_bits:08X})) u_stn (
        .bus_clk(bus_clk), .bus_rst(bus_rst),
        .clk_loc(clk_loc), .aresetn_loc(aresetn_loc),
        .lk_req_valid(L_REQ_tvalid), .lk_req_ready(L_REQ_tready),
        .lk_req_dport(lq_dpt), .lk_req_src(lq_src), .lk_req_tag(lq_tag),
        .lk_req_wr(lq_wr), .lk_req_head(lq_head), .lk_req_last(lq_last),
        .lk_req_addr(lq_addr), .lk_req_len(lq_len), .lk_req_size(lq_size),
        .lk_req_data(lq_data), .lk_req_strb(lq_strb),
        .lk_rsp_valid(L_RSP_tvalid), .lk_rsp_ready(L_RSP_tready),
        .lk_rsp_dst(ls_dst), .lk_rsp_tag(ls_tag), .lk_rsp_wr(ls_wr),
        .lk_rsp_last(ls_last), .lk_rsp_resp(ls_resp), .lk_rsp_data(ls_data),
        .sp_awid(sp_awid), .sp_awaddr(sp_awaddr), .sp_awlen(sp_awlen),
        .sp_awsize(sp_awsize), .sp_awburst(sp_awburst),
        .sp_awvalid(sp_awvalid), .sp_awready(sp_awready),
        .sp_wdata(sp_wdata), .sp_wstrb(sp_wstrb), .sp_wlast(sp_wlast),
        .sp_wvalid(sp_wvalid), .sp_wready(sp_wready),
        .sp_bid(sp_bid), .sp_bresp(sp_bresp), .sp_bvalid(sp_bvalid),
        .sp_bready(sp_bready),
        .sp_arid(sp_arid), .sp_araddr(sp_araddr), .sp_arlen(sp_arlen),
        .sp_arsize(sp_arsize), .sp_arburst(sp_arburst),
        .sp_arvalid(sp_arvalid), .sp_arready(sp_arready),
        .sp_rid(sp_rid), .sp_rdata(sp_rdata), .sp_rresp(sp_rresp),
        .sp_rlast(sp_rlast), .sp_rvalid(sp_rvalid), .sp_rready(sp_rready)
    );
endmodule

`default_nettype wire
"""


def emit_line4(name, mgr_w, nq, fw, mgr_lite=(), loc_lite=(), mgr0_bus=False):
    """Wrap sb_line4 so a block design can infer its interfaces.

    `mgr_w` is the manager widths (station 1 holds them all); `nq` endpoints per
    station, port 0 at `fw` bits and the rest 32. Emits 3 slaves and 4*nq
    masters plus the clocks, and instantiates sb_line4 on packed buses.
    `mgr0_bus` puts manager 0 (jtag) on station 1's bus clock: MGR0_DOM
    defaults to 1 and S00 is associated with bus_clk1 instead of clk_ctrl.
    """
    nm, ns = len(mgr_w), 4 * nq
    portw = max(1, (nq - 1).bit_length()) if nq > 1 else 1
    # sb_line4 packs station and port indices at one width; STNW is 2 there.
    dstw = max(2, portw)
    s_names = [f"S{i:02d}_AXI" for i in range(nm)]
    m_names = [f"M{k:02d}_AXI" for k in range(ns)]
    loc_w = [fw if (k % nq) == 0 else 32 for k in range(ns)]

    s_decls = "\n\n".join(
        axi_decls(p, w, False, i in mgr_lite)
        for i, (p, w) in enumerate(zip(s_names, mgr_w))
    )
    m_decls = "\n\n".join(
        axi_decls(p, w, True, (k % nq) in loc_lite)
        for k, (p, w) in enumerate(zip(m_names, loc_w))
    )
    s_glue = "\n\n".join(
        axi_glue(i, p, w, "mp", False, i in mgr_lite)
        for i, (p, w) in enumerate(zip(s_names, mgr_w))
    )
    m_glue = "\n\n".join(
        (
            axi_lite_conv_glue(k, p, w, "sp", nq)
            if (k % nq) in loc_lite
            else axi_glue(k, p, w, "sp", True)
        )
        for k, (p, w) in enumerate(zip(m_names, loc_w))
    )

    # Each station's endpoints belong to its own local clock; port 2 is DDR and
    # ports 3+ are ctrl, matching sb_line4's port_dom().
    def bus_assoc(s):
        if mgr0_bus and s == 1:
            return f"ASSOCIATED_BUSIF {s_names[0]}, ASSOCIATED_RESET bus_rst{s}"
        return f"ASSOCIATED_RESET bus_rst{s}"

    bus_clks = "\n".join(
        f'    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 bus_clk{s} CLK" *)\n'
        f'    (* X_INTERFACE_PARAMETER = "{bus_assoc(s)}" *)\n'
        f"    input  wire bus_clk{s},\n"
        f'    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 bus_rst{s} RST" *)\n'
        f'    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_HIGH" *)\n'
        f"    input  wire bus_rst{s},"
        for s in range(4)
    )

    def mbusif(pred):
        return ":".join(m for k, m in enumerate(m_names) if pred(k))

    loc_clks = "\n".join(
        f'    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_s{s} CLK" *)\n'
        f'    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF'
        f" {mbusif(lambda k, s=s: k // nq == s and k % nq < 2)},"
        f' ASSOCIATED_RESET aresetn_s{s}" *)\n'
        f"    input  wire clk_s{s},\n"
        f'    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn_s{s} RST" *)\n'
        f'    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)\n'
        f"    input  wire aresetn_s{s},"
        for s in range(4)
    )

    ctl_busif = mbusif(lambda k: k % nq >= 3)
    ctl_assoc = ctl_busif if mgr0_bus else f"{s_names[0]}:{ctl_busif}"
    # Port 2 per station, not one shared clk_ddr: each SLR's DDR4 controller
    # runs on its own ui_clk, and one clock here forces a crossing IP.
    ddr_clks = "\n".join(
        f'    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_ddr{s} CLK" *)\n'
        f'    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF'
        f" {mbusif(lambda k, s=s: k // nq == s and k % nq == 2)},"
        f' ASSOCIATED_RESET aresetn_ddr{s}" *)\n'
        f"    input  wire clk_ddr{s},\n"
        f'    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn_ddr{s} RST" *)\n'
        f'    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)\n'
        f"    input  wire aresetn_ddr{s},"
        for s in range(4)
    )

    return f"""// {name} -- GENERATED by scripts/py/gen_station_wrap.py. Do not edit by hand.
//
// sb_line4 with every port named so a block design infers an interface:
// {nm} AXI4 slaves, {ns} AXI4 masters ({nq} per station), FW={fw}.

`default_nettype none

module {name} #(
    // Flit width is internal, so a block design may retune it without
    // regenerating this file; the AXI port widths below do not follow it.
    parameter integer FW           = {fw},
    parameter integer OST          = 4,
    parameter integer STORE_FWD    = 1,
    parameter integer LUT_PER_BRAM = 0,
    parameter integer TIMEOUT      = 0,
    parameter integer LINK_CDC     = 1,
    parameter integer LINK_FULL    = 0,
    parameter integer CRED         = 16,
    parameter integer PIPE         = 4,
    parameter integer LINK_KTS     = 0,
    parameter integer MGR0_DOM     = {1 if mgr0_bus else 0},
    // Per-station overrides; each defaults to the line-wide value above.
    parameter integer OST0 = OST, OST1 = OST, OST2 = OST, OST3 = OST,
    parameter integer SFW0 = STORE_FWD, SFW1 = STORE_FWD,
    parameter integer SFW2 = STORE_FWD, SFW3 = STORE_FWD,
    parameter integer LPB0 = LUT_PER_BRAM, LPB1 = LUT_PER_BRAM,
    parameter integer LPB2 = LUT_PER_BRAM, LPB3 = LUT_PER_BRAM,
    parameter integer TMO0 = TIMEOUT, TMO1 = TIMEOUT,
    parameter integer TMO2 = TIMEOUT, TMO3 = TIMEOUT,
    // Per-manager queue depths and burst bound (sb_line4 MREQ/MRSP/MMAXB).
    parameter integer MREQ0 = 256, MREQ1 = 256, MREQ2 = 16,
    parameter integer MRSP0 = 256, MRSP1 = 256, MRSP2 = 16,
    parameter integer MMAXB0 = 0, MMAXB1 = 0, MMAXB2 = 1,
    // Address map, handed to sb_line4. At 0 its uniform 64 KB map applies,
    // which cannot express the mesh's 1 TB S_AXI_MEM window.
    parameter integer DSTW_P       = {dstw},
    // SIZED literal: `0` types these as a long that cannot hold {ns * AW} bits
    // (19-3452), and `{{N{{1'b0}}}}` reads its default back quoted (19-594).
    parameter integer SEG_OVERRIDE = 0,
    parameter [{ns}*{AW}-1:0]   SEG_BASE_P  = {ns * AW}'h0,
    parameter [{ns}*{AW}-1:0]   SEG_MASK_P  = {ns * AW}'h0,
    parameter [{ns}*{AW}-1:0]   SEG_XLT_P   = {ns * AW}'h0,
    parameter [{ns}*{dstw}-1:0] SEG_DST_P   = {ns * dstw}'h0,
    parameter [{ns}*{dstw}-1:0] SEG_DPORT_P = {ns * dstw}'h0,
    parameter [{ns}-1:0]        SEG_VLD_P   = {ns}'h{(1 << ns) - 1:X}
)(
{bus_clks}

    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_ctrl CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF {ctl_assoc}, ASSOCIATED_RESET aresetn_ctrl" *)
    input  wire clk_ctrl,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn_ctrl RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire aresetn_ctrl,

    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_xdma CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF {':'.join(s_names[1:])}, ASSOCIATED_RESET aresetn_xdma" *)
    input  wire clk_xdma,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn_xdma RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire aresetn_xdma,

{loc_clks}

{ddr_clks}

{s_decls}

{m_decls}

    output wire [31:0] stat_decerr
);
    localparam integer NM    = {nm};
    localparam integer NS    = {ns};
    localparam integer AW    = {AW};
    localparam integer MAXW  = {MAXW};
    localparam integer MAXID = {MAXID};

    wire [NM*MAXID-1:0]    mp_awid, mp_arid, mp_bid, mp_rid;
    wire [NM*AW-1:0]       mp_awaddr, mp_araddr;
    wire [NM*8-1:0]        mp_awlen, mp_arlen;
    wire [NM*3-1:0]        mp_awsize, mp_arsize;
    wire [NM*2-1:0]        mp_awburst, mp_arburst, mp_bresp, mp_rresp;
    wire [NM-1:0]          mp_awvalid, mp_awready, mp_wvalid, mp_wready;
    wire [NM-1:0]          mp_wlast, mp_bvalid, mp_bready;
    wire [NM-1:0]          mp_arvalid, mp_arready, mp_rvalid, mp_rready, mp_rlast;
    wire [NM*MAXW-1:0]     mp_wdata, mp_rdata;
    wire [NM*(MAXW/8)-1:0] mp_wstrb;

    wire [NS*MAXID-1:0]    sp_awid, sp_arid, sp_bid, sp_rid;
    wire [NS*AW-1:0]       sp_awaddr, sp_araddr;
    wire [NS*8-1:0]        sp_awlen, sp_arlen;
    wire [NS*3-1:0]        sp_awsize, sp_arsize;
    wire [NS*2-1:0]        sp_awburst, sp_arburst, sp_bresp, sp_rresp;
    wire [NS-1:0]          sp_awvalid, sp_awready, sp_wvalid, sp_wready;
    wire [NS-1:0]          sp_wlast, sp_bvalid, sp_bready;
    wire [NS-1:0]          sp_arvalid, sp_arready, sp_rvalid, sp_rready, sp_rlast;
    wire [NS*MAXW-1:0]     sp_wdata, sp_rdata;
    wire [NS*(MAXW/8)-1:0] sp_wstrb;

{s_glue}

{m_glue}

    sb_line4 #(.FW(FW), .AW(AW), .MAXW(MAXW), .MAXID(MAXID), .NM(NM),
               .NQ({nq}), .PORTW({portw}), .TAGW(4), .OST(OST),
               .STORE_FWD(STORE_FWD), .LUT_PER_BRAM(LUT_PER_BRAM),
               .TIMEOUT(TIMEOUT), .PIPE(PIPE), .CRED(CRED),
               .LINK_CDC(LINK_CDC), .LINK_FULL(LINK_FULL),
               .LINK_KTS(LINK_KTS), .MGR0_DOM(MGR0_DOM),
               .OST0(OST0), .OST1(OST1), .OST2(OST2), .OST3(OST3),
               .SFW0(SFW0), .SFW1(SFW1), .SFW2(SFW2), .SFW3(SFW3),
               .LPB0(LPB0), .LPB1(LPB1), .LPB2(LPB2), .LPB3(LPB3),
               .TMO0(TMO0), .TMO1(TMO1), .TMO2(TMO2), .TMO3(TMO3),
               .MREQ0(MREQ0), .MREQ1(MREQ1), .MREQ2(MREQ2),
               .MRSP0(MRSP0), .MRSP1(MRSP1), .MRSP2(MRSP2),
               .MMAXB0(MMAXB0), .MMAXB1(MMAXB1), .MMAXB2(MMAXB2),
               .DSTW_P(DSTW_P), .SEG_OVERRIDE(SEG_OVERRIDE),
               .SEG_BASE_P(SEG_BASE_P), .SEG_MASK_P(SEG_MASK_P),
               .SEG_XLT_P(SEG_XLT_P), .SEG_DST_P(SEG_DST_P),
               .SEG_DPORT_P(SEG_DPORT_P), .SEG_VLD_P(SEG_VLD_P)) u_line (
        .bus_clk0(bus_clk0), .bus_rst0(bus_rst0),
        .bus_clk1(bus_clk1), .bus_rst1(bus_rst1),
        .bus_clk2(bus_clk2), .bus_rst2(bus_rst2),
        .bus_clk3(bus_clk3), .bus_rst3(bus_rst3),
        .clk_ctrl(clk_ctrl), .aresetn_ctrl(aresetn_ctrl),
        .clk_xdma(clk_xdma), .aresetn_xdma(aresetn_xdma),
        .clk_s0(clk_s0), .aresetn_s0(aresetn_s0),
        .clk_s1(clk_s1), .aresetn_s1(aresetn_s1),
        .clk_s2(clk_s2), .aresetn_s2(aresetn_s2),
        .clk_s3(clk_s3), .aresetn_s3(aresetn_s3),
        .clk_ddr0(clk_ddr0), .aresetn_ddr0(aresetn_ddr0),
        .clk_ddr1(clk_ddr1), .aresetn_ddr1(aresetn_ddr1),
        .clk_ddr2(clk_ddr2), .aresetn_ddr2(aresetn_ddr2),
        .clk_ddr3(clk_ddr3), .aresetn_ddr3(aresetn_ddr3),
        .mp_awid(mp_awid), .mp_awaddr(mp_awaddr), .mp_awlen(mp_awlen),
        .mp_awsize(mp_awsize), .mp_awburst(mp_awburst),
        .mp_awvalid(mp_awvalid), .mp_awready(mp_awready),
        .mp_wdata(mp_wdata), .mp_wstrb(mp_wstrb), .mp_wlast(mp_wlast),
        .mp_wvalid(mp_wvalid), .mp_wready(mp_wready),
        .mp_bid(mp_bid), .mp_bresp(mp_bresp), .mp_bvalid(mp_bvalid),
        .mp_bready(mp_bready),
        .mp_arid(mp_arid), .mp_araddr(mp_araddr), .mp_arlen(mp_arlen),
        .mp_arsize(mp_arsize), .mp_arburst(mp_arburst),
        .mp_arvalid(mp_arvalid), .mp_arready(mp_arready),
        .mp_rid(mp_rid), .mp_rdata(mp_rdata), .mp_rresp(mp_rresp),
        .mp_rlast(mp_rlast), .mp_rvalid(mp_rvalid), .mp_rready(mp_rready),
        .sp_awid(sp_awid), .sp_awaddr(sp_awaddr), .sp_awlen(sp_awlen),
        .sp_awsize(sp_awsize), .sp_awburst(sp_awburst),
        .sp_awvalid(sp_awvalid), .sp_awready(sp_awready),
        .sp_wdata(sp_wdata), .sp_wstrb(sp_wstrb), .sp_wlast(sp_wlast),
        .sp_wvalid(sp_wvalid), .sp_wready(sp_wready),
        .sp_bid(sp_bid), .sp_bresp(sp_bresp), .sp_bvalid(sp_bvalid),
        .sp_bready(sp_bready),
        .sp_arid(sp_arid), .sp_araddr(sp_araddr), .sp_arlen(sp_arlen),
        .sp_arsize(sp_arsize), .sp_arburst(sp_arburst),
        .sp_arvalid(sp_arvalid), .sp_arready(sp_arready),
        .sp_rid(sp_rid), .sp_rdata(sp_rdata), .sp_rresp(sp_rresp),
        .sp_rlast(sp_rlast), .sp_rvalid(sp_rvalid), .sp_rready(sp_rready),
        .stat_decerr(stat_decerr)
    );
endmodule

`default_nettype wire
"""


def emit_link(name):
    rw, sw = pad8(req_w()), pad8(rsp_w())
    return f"""// {name} -- GENERATED by scripts/py/gen_station_wrap.py. Do not edit by hand.
//
// One station-to-station crossing as two AXIS interfaces each way. Its own
// block, and NOT in any pblock: the pipeline registers have to be free to place
// across the die, which a station pinned to one SLR cannot allow.

`default_nettype none

module {name} #(
    parameter integer PIPE = 4,
    parameter integer CRED = 16
)(
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 bus_clk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF S_REQ:M_REQ:S_RSP:M_RSP, ASSOCIATED_RESET bus_rst" *)
    input  wire bus_clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 bus_rst RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_HIGH" *)
    input  wire bus_rst,

{axis_decls("S_REQ", rw, False)}
{axis_decls("M_REQ", rw, True)}
{axis_decls("S_RSP", sw, False)}
{axis_decls("M_RSP", sw, True)[:-1]}
);
    sb_link #(.W({rw}), .PIPE(PIPE), .CRED(CRED)) u_req (
        .clk(bus_clk), .rst(bus_rst),
        .i_valid(S_REQ_tvalid), .i_ready(S_REQ_tready), .i_data(S_REQ_tdata),
        .o_valid(M_REQ_tvalid), .o_ready(M_REQ_tready), .o_data(M_REQ_tdata));

    sb_link #(.W({sw}), .PIPE(PIPE), .CRED(CRED)) u_rsp (
        .clk(bus_clk), .rst(bus_rst),
        .i_valid(S_RSP_tvalid), .i_ready(S_RSP_tready), .i_data(S_RSP_tdata),
        .o_valid(M_RSP_tvalid), .o_ready(M_RSP_tready), .o_data(M_RSP_tdata));
endmodule

`default_nettype wire
"""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--kind", choices=["root", "leaf", "link", "line4"], required=True)
    ap.add_argument("--nq", type=int, default=4)
    ap.add_argument("--fw", type=int, default=256)
    ap.add_argument("--mgr-w", default="32,512,32")
    # Manager indices to declare AXI4-Lite. S02 carries XDMA's M_AXI_LITE,
    # which will not connect to a full-AXI4 port (BD 41-1285).
    ap.add_argument("--mgr-lite", default="")
    # Port indices WITHIN a station to declare AXI4-Lite. Port 2 is the DDR4
    # controller's C0_DDR4_S_AXI_CTRL, which is Lite.
    ap.add_argument("--loc-lite", default="")
    ap.add_argument("--loc-w", default="512,32")
    # line4 only: manager 0 (jtag) on station 1's bus clock, MGR0_DOM 1.
    ap.add_argument("--mgr0-bus", action="store_true")
    ap.add_argument("--nk", type=int, default=3)
    ap.add_argument("--aw", type=int, default=40)
    # OST outstanding needs clog2(OST) local id bits, so this cannot go below 2.
    ap.add_argument("--idw", type=int, default=4)
    ap.add_argument("-o", "--out")
    ap.add_argument("-m", "--module")
    args = ap.parse_args()

    global AW, MAXID
    AW, MAXID = args.aw, args.idw

    mgr_w = [int(x) for x in args.mgr_w.split(",") if x]
    loc_w = [int(x) for x in args.loc_w.split(",") if x]
    # The line4 kind converts widths itself, so its whitelist follows the
    # REQUESTED flit width, not the module default.
    for w in mgr_w + loc_w:
        if w not in (32, 64, FW, args.fw):
            sys.exit(f"gen_station_wrap: width {w} is not 32, 64, {FW} or {args.fw}")

    mgr_lite = {int(x) for x in args.mgr_lite.split(",") if x.strip()}
    loc_lite = {int(x) for x in args.loc_lite.split(",") if x.strip()}
    name = args.module or f"sb_bd_{args.kind}"
    if args.kind == "line4":
        text = emit_line4(
            name, mgr_w, args.nq, args.fw, mgr_lite, loc_lite, args.mgr0_bus
        )
    elif args.kind == "root":
        text = emit_root(name, mgr_w, loc_w, args.nk)
    elif args.kind == "leaf":
        text = emit_leaf(name, loc_w)
    else:
        text = emit_link(name)

    # Record the invocation in the file. Nothing else stores it, so without
    # this a regenerated wrapper silently differs from the one that was built.
    argv = " ".join(sys.argv[1:])
    head, sep, rest = text.partition("\n")
    text = f"{head}{sep}//   gen_station_wrap.py {argv}\n{rest}"

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{args.out}: {name}")
    else:
        print(text)


if __name__ == "__main__":
    main()
