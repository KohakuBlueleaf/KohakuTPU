// Two RV64 control complexes on one fabric.
//
// They share NOTHING inside this wrapper -- separate host windows, separate
// node ports -- because at this phase the only coupling that should exist is
// contention for the fabric itself. Anything else that couples them is a bug,
// and this wrapper must not be where it comes from.
//
// The harness answers both node ports out of ONE memory, so a cross-write is
// visible: it is how the mailbox will eventually work, and how an aliasing
// address decode is caught.

`default_nettype none

module rv64_syscore_pair #(
    parameter integer ADDR_W     = 40,
    parameter integer DATA_W     = 256,
    parameter integer IMEM_WORDS = 8192,
    parameter integer SPAD_WORDS = 4096,
    parameter integer L1_LINES   = 64,
    parameter         MEM_PRIM   = "block"
)(
    input  wire                  clk,
    input  wire                  resetn,

    input  wire [31:0]           a_hs_addr,
    input  wire                  a_hs_wr,
    input  wire [63:0]           a_hs_wdata,
    input  wire [7:0]            a_hs_wstrb,
    input  wire                  a_hs_rd,
    output wire [63:0]           a_hs_rdata,
    output wire                  a_console_we,
    output wire [7:0]            a_console,
    output wire [63:0]           a_cycles,
    output wire [63:0]           a_retired,

    output wire [ADDR_W-1:0]     a_awaddr,
    output wire [7:0]            a_awlen,
    output wire                  a_awvalid,
    input  wire                  a_awready,
    output wire [DATA_W-1:0]     a_wdata,
    output wire [DATA_W/8-1:0]   a_wstrb,
    output wire                  a_wlast,
    output wire                  a_wvalid,
    input  wire                  a_wready,
    input  wire                  a_bvalid,
    output wire                  a_bready,
    output wire [ADDR_W-1:0]     a_araddr,
    output wire [7:0]            a_arlen,
    output wire                  a_arvalid,
    input  wire                  a_arready,
    input  wire [DATA_W-1:0]     a_rdata,
    input  wire                  a_rlast,
    input  wire                  a_rvalid,
    output wire                  a_rready,

    input  wire [31:0]           b_hs_addr,
    input  wire                  b_hs_wr,
    input  wire [63:0]           b_hs_wdata,
    input  wire [7:0]            b_hs_wstrb,
    input  wire                  b_hs_rd,
    output wire [63:0]           b_hs_rdata,
    output wire                  b_console_we,
    output wire [7:0]            b_console,
    output wire [63:0]           b_cycles,
    output wire [63:0]           b_retired,

    output wire [ADDR_W-1:0]     b_awaddr,
    output wire [7:0]            b_awlen,
    output wire                  b_awvalid,
    input  wire                  b_awready,
    output wire [DATA_W-1:0]     b_wdata,
    output wire [DATA_W/8-1:0]   b_wstrb,
    output wire                  b_wlast,
    output wire                  b_wvalid,
    input  wire                  b_wready,
    input  wire                  b_bvalid,
    output wire                  b_bready,
    output wire [ADDR_W-1:0]     b_araddr,
    output wire [7:0]            b_arlen,
    output wire                  b_arvalid,
    input  wire                  b_arready,
    input  wire [DATA_W-1:0]     b_rdata,
    input  wire                  b_rlast,
    input  wire                  b_rvalid,
    output wire                  b_rready
);
    rv64_syscore #(
        .ADDR_W(ADDR_W), .DATA_W(DATA_W), .IMEM_WORDS(IMEM_WORDS),
        .SPAD_WORDS(SPAD_WORDS), .L1_LINES(L1_LINES), .MEM_PRIM(MEM_PRIM)
    ) u_a (
        .clk(clk), .resetn(resetn),
        .hs_addr(a_hs_addr), .hs_wr(a_hs_wr), .hs_wdata(a_hs_wdata),
        .hs_wstrb(a_hs_wstrb), .hs_rd(a_hs_rd), .hs_rdata(a_hs_rdata),
        .hs_ready(),
        .cp_awaddr(a_awaddr), .cp_awlen(a_awlen), .cp_awvalid(a_awvalid),
        .cp_awready(a_awready), .cp_wdata(a_wdata), .cp_wstrb(a_wstrb),
        .cp_wlast(a_wlast), .cp_wvalid(a_wvalid), .cp_wready(a_wready),
        .cp_bvalid(a_bvalid), .cp_bready(a_bready),
        .cp_araddr(a_araddr), .cp_arlen(a_arlen), .cp_arvalid(a_arvalid),
        .cp_arready(a_arready), .cp_rdata(a_rdata), .cp_rlast(a_rlast),
        .cp_rvalid(a_rvalid), .cp_rready(a_rready),
        .mv_cfg_en(), .mv_cfg_addr(), .mv_cfg_data(),
        .mv_busy(1'b0), .mv_fault(4'd0), .mv_done(32'd0),
        .db_en(), .db_addr(), .db_data(), .db_status(64'd0),
        .irq_summary(1'b0), .running(),
        .dbg_console_we(a_console_we), .dbg_console(a_console),
        .dbg_cycles(a_cycles), .dbg_retired(a_retired)
    );

    rv64_syscore #(
        .ADDR_W(ADDR_W), .DATA_W(DATA_W), .IMEM_WORDS(IMEM_WORDS),
        .SPAD_WORDS(SPAD_WORDS), .L1_LINES(L1_LINES), .MEM_PRIM(MEM_PRIM)
    ) u_b (
        .clk(clk), .resetn(resetn),
        .hs_addr(b_hs_addr), .hs_wr(b_hs_wr), .hs_wdata(b_hs_wdata),
        .hs_wstrb(b_hs_wstrb), .hs_rd(b_hs_rd), .hs_rdata(b_hs_rdata),
        .hs_ready(),
        .cp_awaddr(b_awaddr), .cp_awlen(b_awlen), .cp_awvalid(b_awvalid),
        .cp_awready(b_awready), .cp_wdata(b_wdata), .cp_wstrb(b_wstrb),
        .cp_wlast(b_wlast), .cp_wvalid(b_wvalid), .cp_wready(b_wready),
        .cp_bvalid(b_bvalid), .cp_bready(b_bready),
        .cp_araddr(b_araddr), .cp_arlen(b_arlen), .cp_arvalid(b_arvalid),
        .cp_arready(b_arready), .cp_rdata(b_rdata), .cp_rlast(b_rlast),
        .cp_rvalid(b_rvalid), .cp_rready(b_rready),
        .mv_cfg_en(), .mv_cfg_addr(), .mv_cfg_data(),
        .mv_busy(1'b0), .mv_fault(4'd0), .mv_done(32'd0),
        .db_en(), .db_addr(), .db_data(), .db_status(64'd0),
        .irq_summary(1'b0), .running(),
        .dbg_console_we(b_console_we), .dbg_console(b_console),
        .dbg_cycles(b_cycles), .dbg_retired(b_retired)
    );

endmodule

`default_nettype wire
