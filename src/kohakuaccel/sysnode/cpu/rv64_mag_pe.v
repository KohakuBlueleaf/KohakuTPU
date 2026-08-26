// rv64_mag_pe -- the system node's control complex with the RV64 core.
//
// The RV32 `rv_mag_pe` and this one hold the SAME three things: a processor, the
// memory mover as its SIMD memory unit, and the transform slot as that unit's
// extension. Only the processor differs. The mover and the slot are parts of the
// NODE, not of whichever CPU sits in it, so they are instantiated here unchanged
// and their 4,601 and 4,356 LUT are not this work's to spend or to save.
//
// `mv.go` IS A STORE, NOT AN OPCODE. `rv64_syscore` decodes the mover's window
// out of its control region and emits a cfg write per store, which keeps the ISA
// unchanged and matches the framework rule that control is a range. The
// processor wins over the host's `aux_cfg` when both pulse, which is the
// precedent design s4.1 cites for the doorbell.
//
// NO NoC COMPUTE-UNIT SHELL -- decisions.md D1. This unit is not kicked and does
// not report a completion; it boots once and runs. Dispatch to compute units
// needs a requestor and is NOT built here yet.

`default_nettype none

module rv64_mag_pe #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer ADDR_W     = 40,
    parameter integer DATA_W     = 256,
    parameter integer ID_W       = 4,
    parameter integer IMEM_WORDS = 8192,
    parameter integer SPAD_WORDS = 4096,
    parameter integer L1_LINES   = 64,
    parameter integer TLB_ENTRIES = 32,
    parameter         MEM_PRIM   = "block",
    parameter integer XFORM_SLOTS     = 1,
    parameter integer XID_W           = 4,
    parameter integer XMODE_W         = 4,
    parameter integer XFORM_IN_BITS   = 2048,
    parameter integer XFORM_OUT_WORDS = 4
)(
    input  wire                   clk,
    input  wire                   resetn,

    // flits: a client of sn_hub, with no port of its own
    input  wire [POS_WIDTH-1:0]   my_x,
    input  wire [POS_WIDTH-1:0]   my_y,
    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    // the host's window into the processor
    input  wire [31:0]            hs_addr,
    input  wire                   hs_wr,
    input  wire [63:0]            hs_wdata,
    input  wire [7:0]             hs_wstrb,
    input  wire                   hs_rd,
    output wire [63:0]            hs_rdata,
    output wire                   hs_ready,

    // the processor's converged path into MAG
    output wire [ADDR_W-1:0]      cp_awaddr,
    output wire [7:0]             cp_awlen,
    output wire                   cp_awvalid,
    input  wire                   cp_awready,
    output wire [DATA_W-1:0]      cp_wdata,
    output wire [DATA_W/8-1:0]    cp_wstrb,
    output wire                   cp_wlast,
    output wire                   cp_wvalid,
    input  wire                   cp_wready,
    input  wire                   cp_bvalid,
    output wire                   cp_bready,
    output wire [ADDR_W-1:0]      cp_araddr,
    output wire [7:0]             cp_arlen,
    output wire                   cp_arvalid,
    input  wire                   cp_arready,
    input  wire [DATA_W-1:0]      cp_rdata,
    input  wire                   cp_rlast,
    input  wire                   cp_rvalid,
    output wire                   cp_rready,

    // the mover's own master into MAG
    output wire [ID_W-1:0]        mv_awid,
    output wire [ADDR_W-1:0]      mv_awaddr,
    output wire [7:0]             mv_awlen,
    output wire [2:0]             mv_awsize,
    output wire [1:0]             mv_awburst,
    output wire                   mv_awvalid,
    input  wire                   mv_awready,
    output wire [DATA_W-1:0]      mv_wdata,
    output wire [DATA_W/8-1:0]    mv_wstrb,
    output wire                   mv_wlast,
    output wire                   mv_wvalid,
    input  wire                   mv_wready,
    input  wire [ID_W-1:0]        mv_bid,
    input  wire [1:0]             mv_bresp,
    input  wire                   mv_bvalid,
    output wire                   mv_bready,
    output wire [ID_W-1:0]        mv_arid,
    output wire [ADDR_W-1:0]      mv_araddr,
    output wire [7:0]             mv_arlen,
    output wire [2:0]             mv_arsize,
    output wire [1:0]             mv_arburst,
    output wire                   mv_arvalid,
    input  wire                   mv_arready,
    input  wire [ID_W-1:0]        mv_rid,
    input  wire [DATA_W-1:0]      mv_rdata,
    input  wire [1:0]             mv_rresp,
    input  wire                   mv_rlast,
    input  wire                   mv_rvalid,
    output wire                   mv_rready,

    // the host's config path, which the processor arbitrates against
    input  wire                   aux_cfg_en,
    input  wire [7:0]             aux_cfg_addr,
    input  wire [63:0]            aux_cfg_data,
    input  wire                   ilink_on,

    output wire                   mv_busy,
    output wire [3:0]             mv_fault,
    output wire [31:0]            mv_done,

    input  wire [63:0]            db_status,
    output wire                   db_en,
    output wire [7:0]             db_addr,
    output wire [63:0]            db_data,

    input  wire                   irq_summary,
    output wire                   busy,
    output wire                   dbg_console_we,
    output wire [7:0]             dbg_console
);
    wire        pe_cfg_en;
    wire [7:0]  pe_cfg_addr;
    wire [63:0] pe_cfg_data;
    wire [3:0]  xf_fault;

    rv64_syscore #(
        .ADDR_W(ADDR_W), .DATA_W(DATA_W),
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS),
        .L1_LINES(L1_LINES), .TLB_ENTRIES(TLB_ENTRIES), .MEM_PRIM(MEM_PRIM)
    ) u_cpu (
        .clk(clk), .resetn(resetn),
        .my_x(my_x), .my_y(my_y),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .hs_addr(hs_addr), .hs_wr(hs_wr), .hs_wdata(hs_wdata),
        .hs_wstrb(hs_wstrb), .hs_rd(hs_rd), .hs_rdata(hs_rdata),
        .hs_ready(hs_ready),
        .cp_awaddr(cp_awaddr), .cp_awlen(cp_awlen), .cp_awvalid(cp_awvalid),
        .cp_awready(cp_awready), .cp_wdata(cp_wdata), .cp_wstrb(cp_wstrb),
        .cp_wlast(cp_wlast), .cp_wvalid(cp_wvalid), .cp_wready(cp_wready),
        .cp_bvalid(cp_bvalid), .cp_bready(cp_bready),
        .cp_araddr(cp_araddr), .cp_arlen(cp_arlen), .cp_arvalid(cp_arvalid),
        .cp_arready(cp_arready), .cp_rdata(cp_rdata), .cp_rlast(cp_rlast),
        .cp_rvalid(cp_rvalid), .cp_rready(cp_rready),
        .mv_cfg_en(pe_cfg_en), .mv_cfg_addr(pe_cfg_addr),
        .mv_cfg_data(pe_cfg_data),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done),
        .db_en(db_en), .db_addr(db_addr), .db_data(db_data),
        .db_status(db_status),
        .irq_summary(irq_summary),
        .running(busy),
        .dbg_console_we(dbg_console_we), .dbg_console(dbg_console),
        .dbg_cycles(), .dbg_retired()
    );

    // The aux window splits at 0x80: below is the mover's, at or above the
    // interlink's. Without the interlink the gate is a constant and the mover
    // sees every write, as it always has.
    wire        host_cfg_mine = aux_cfg_en && (!ilink_on || !aux_cfg_addr[7]);
    wire        cfg_en_i      = pe_cfg_en || host_cfg_mine;
    wire [7:0]  cfg_addr_i    = pe_cfg_en ? pe_cfg_addr : aux_cfg_addr;
    wire [63:0] cfg_data_i    = pe_cfg_en ? pe_cfg_data : aux_cfg_data;

    // ---- the transform slot, on the mover's read-return path ---------------
    wire                x_req, x_gnt, x_start, x_bv, x_done;
    wire [XID_W-1:0]    x_id;
    wire [XMODE_W-1:0]  x_mode;
    wire [DATA_W-1:0]   x_beat, x_w0, x_w1, x_w2, x_w3;

    mm_mover #(.DATA_W(DATA_W), .ADDR_W(ADDR_W), .ID_W(ID_W),
               .XID_W(XID_W), .XMODE_W(XMODE_W),
               .XF_IN_BITS(XFORM_IN_BITS),
               .XF_OUT_WORDS(XFORM_OUT_WORDS)) u_mover (
        .clk(clk), .resetn(resetn),
        .cfg_en(cfg_en_i), .cfg_addr(cfg_addr_i), .cfg_data(cfg_data_i),
        .stat_busy(mv_busy), .stat_fault(mv_fault), .stat_done(mv_done),
        .m_awid(mv_awid), .m_awaddr(mv_awaddr), .m_awlen(mv_awlen),
        .m_awsize(mv_awsize), .m_awburst(mv_awburst),
        .m_awvalid(mv_awvalid), .m_awready(mv_awready),
        .m_wdata(mv_wdata), .m_wstrb(mv_wstrb), .m_wlast(mv_wlast),
        .m_wvalid(mv_wvalid), .m_wready(mv_wready),
        .m_bid(mv_bid), .m_bresp(mv_bresp),
        .m_bvalid(mv_bvalid), .m_bready(mv_bready),
        .m_arid(mv_arid), .m_araddr(mv_araddr), .m_arlen(mv_arlen),
        .m_arsize(mv_arsize), .m_arburst(mv_arburst),
        .m_arvalid(mv_arvalid), .m_arready(mv_arready),
        .m_rid(mv_rid), .m_rdata(mv_rdata), .m_rresp(mv_rresp),
        .m_rlast(mv_rlast), .m_rvalid(mv_rvalid), .m_rready(mv_rready),
        .x_req(x_req), .x_gnt(x_gnt), .x_start(x_start),
        .x_id(x_id), .x_mode(x_mode),
        .x_beat(x_beat), .x_beat_valid(x_bv),
        .x_done(x_done), .x_w0(x_w0), .x_w1(x_w1), .x_w2(x_w2), .x_w3(x_w3)
    );

    mag_xform #(.DATA_W(DATA_W), .NREQ(1), .SLOTS(XFORM_SLOTS),
                .ID_W(XID_W), .MODE_W(XMODE_W),
                .IN_BITS(XFORM_IN_BITS), .OUT_WORDS(XFORM_OUT_WORDS))
    u_xform (
        .clk(clk), .rst(!resetn),
        .req(x_req), .gnt(x_gnt),
        .start(x_start), .id(x_id), .mode(x_mode),
        .beat(x_beat), .beat_valid(x_bv),
        .done(x_done), .word0(x_w0), .word1(x_w1), .word2(x_w2), .word3(x_w3),
        .cfg_en(1'b0), .cfg_id({XID_W{1'b0}}),
        .cfg_addr(8'd0), .cfg_data(64'd0),
        .cfg_rdata(), .fault(xf_fault)
    );

endmodule

`default_nettype wire
