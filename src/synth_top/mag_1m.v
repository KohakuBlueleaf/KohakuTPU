// MAG with ONE AXI master: stock `mag` plus a per-master AXI->request shim
// feeding mag_dram_port. `mag` is instantiated VERBATIM, nothing is edited.

`default_nettype none

module mag_1m #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer DATA_W     = 256,
    parameter integer ADDR_W     = 34,
    parameter integer ID_W       = 4,
    parameter integer MEM_PORTS  = 2,
    parameter integer MEM_X      = 1,
    parameter integer MEM_Y      = 1,
    parameter integer MEM_X1     = 1,
    parameter integer MEM_Y1     = 2,
    parameter integer GRID_LO    = 1,
    parameter integer GRID_HI    = 2,
    parameter integer STAGE_FLITS = 128,
    parameter integer ILINK      = 0,
    parameter integer MESH_ID    = 0,
    parameter integer LINK_W     = 288,
    parameter integer TUSER_W    = 96,
    parameter integer MW         = 512,
    parameter integer NREQ       = MEM_PORTS + 2 + ((ILINK != 0) ? 1 : 0)
)(
    input  wire clk,
    input  wire resetn,
    input  wire dram_aclk,
    input  wire dram_aresetn,

    // MAG's two AXI slaves pass straight through.
    input  wire [ID_W-1:0]   sm_awid,
    input  wire [ADDR_W-1:0] sm_awaddr,
    input  wire [7:0]        sm_awlen,
    input  wire              sm_awvalid,
    output wire              sm_awready,
    input  wire [DATA_W-1:0] sm_wdata,
    input  wire [DATA_W/8-1:0] sm_wstrb,
    input  wire              sm_wlast,
    input  wire              sm_wvalid,
    output wire              sm_wready,
    output wire [ID_W-1:0]   sm_bid,
    output wire [1:0]        sm_bresp,
    output wire              sm_bvalid,
    input  wire              sm_bready,
    input  wire [ID_W-1:0]   sm_arid,
    input  wire [ADDR_W-1:0] sm_araddr,
    input  wire [7:0]        sm_arlen,
    input  wire              sm_arvalid,
    output wire              sm_arready,
    output wire [ID_W-1:0]   sm_rid,
    output wire [DATA_W-1:0] sm_rdata,
    output wire [1:0]        sm_rresp,
    output wire              sm_rlast,
    output wire              sm_rvalid,
    input  wire              sm_rready,

    input  wire [ID_W-1:0]   sc_awid,
    input  wire [31:0]       sc_awaddr,
    input  wire [7:0]        sc_awlen,
    input  wire              sc_awvalid,
    output wire              sc_awready,
    input  wire [63:0]       sc_wdata,
    input  wire [7:0]        sc_wstrb,
    input  wire              sc_wlast,
    input  wire              sc_wvalid,
    output wire              sc_wready,
    output wire [ID_W-1:0]   sc_bid,
    output wire [1:0]        sc_bresp,
    output wire              sc_bvalid,
    input  wire              sc_bready,
    input  wire [ID_W-1:0]   sc_arid,
    input  wire [31:0]       sc_araddr,
    input  wire [7:0]        sc_arlen,
    input  wire              sc_arvalid,
    output wire              sc_arready,
    output wire [ID_W-1:0]   sc_rid,
    output wire [63:0]       sc_rdata,
    output wire [1:0]        sc_rresp,
    output wire              sc_rlast,
    output wire              sc_rvalid,
    input  wire              sc_rready,

    // NoC
    input  wire [MEM_PORTS*FLIT_WIDTH-1:0] mem_in_data,
    input  wire [MEM_PORTS-1:0]            mem_in_valid,
    output wire [MEM_PORTS-1:0]            mem_in_busy,
    output wire [MEM_PORTS*FLIT_WIDTH-1:0] mem_out_data,
    output wire [MEM_PORTS-1:0]            mem_out_valid,
    input  wire [MEM_PORTS-1:0]            mem_out_busy,
    output wire [15:0]                     mem_rd_count,
    output wire [15:0]                     mem_wr_count,
    output wire                            mv_busy,
    output wire [3:0]                      mv_fault,
    output wire [31:0]                     mv_done,

    // THE POINT: one AXI master, one memory clock.
    output wire [ID_W-1:0]   m_awid,
    output wire [ADDR_W-1:0] m_awaddr,
    output wire [7:0]        m_awlen,
    output wire [2:0]        m_awsize,
    output wire [1:0]        m_awburst,
    output wire              m_awvalid,
    input  wire              m_awready,
    output wire [MW-1:0]     m_wdata,
    output wire [MW/8-1:0]   m_wstrb,
    output wire              m_wlast,
    output wire              m_wvalid,
    input  wire              m_wready,
    input  wire [ID_W-1:0]   m_bid,
    input  wire [1:0]        m_bresp,
    input  wire              m_bvalid,
    output wire              m_bready,
    output wire [ID_W-1:0]   m_arid,
    output wire [ADDR_W-1:0] m_araddr,
    output wire [7:0]        m_arlen,
    output wire [2:0]        m_arsize,
    output wire [1:0]        m_arburst,
    output wire              m_arvalid,
    input  wire              m_arready,
    input  wire [ID_W-1:0]   m_rid,
    input  wire [MW-1:0]     m_rdata,
    input  wire [1:0]        m_rresp,
    input  wire              m_rlast,
    input  wire              m_rvalid,
    output wire              m_rready,

    // Straight through to `mag`; constants at ILINK=0, as there.
    output wire [LINK_W-1:0]  link0_out_tdata,
    output wire [TUSER_W-1:0] link0_out_tuser,
    output wire               link0_out_tlast,
    output wire               link0_out_tvalid,
    input  wire               link0_out_tready,
    input  wire [LINK_W-1:0]  link0_in_tdata,
    input  wire [TUSER_W-1:0] link0_in_tuser,
    input  wire               link0_in_tlast,
    input  wire               link0_in_tvalid,
    output wire               link0_in_tready,
    output wire [LINK_W-1:0]  link1_out_tdata,
    output wire [TUSER_W-1:0] link1_out_tuser,
    output wire               link1_out_tlast,
    output wire               link1_out_tvalid,
    input  wire               link1_out_tready,
    input  wire [LINK_W-1:0]  link1_in_tdata,
    input  wire [TUSER_W-1:0] link1_in_tuser,
    input  wire               link1_in_tlast,
    input  wire               link1_in_tvalid,
    output wire               link1_in_tready
);
    // ---- the stock MAG, verbatim ----------------------------------------
    wire [NREQ*ID_W-1:0]     x_awid,  x_arid,  x_bid,  x_rid;
    wire [NREQ*ADDR_W-1:0]   x_awaddr, x_araddr;
    wire [NREQ*8-1:0]        x_awlen, x_arlen;
    wire [NREQ*3-1:0]        x_awsize, x_arsize;
    wire [NREQ*2-1:0]        x_awburst, x_arburst, x_bresp, x_rresp;
    wire [NREQ-1:0]          x_awvalid, x_awready, x_arvalid, x_arready;
    wire [NREQ*DATA_W-1:0]   x_wdata, x_rdata;
    wire [NREQ*DATA_W/8-1:0] x_wstrb;
    wire [NREQ-1:0]          x_wlast, x_wvalid, x_wready;
    wire [NREQ-1:0]          x_bvalid, x_bready, x_rlast, x_rvalid, x_rready;

    mag #(.FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH), .DATA_W(DATA_W),
          .ADDR_W(ADDR_W), .ID_W(ID_W), .MEM_PORTS(MEM_PORTS),
          .MEM_X(MEM_X), .MEM_Y(MEM_Y), .MEM_X1(MEM_X1), .MEM_Y1(MEM_Y1),
          .GRID_LO(GRID_LO), .GRID_HI(GRID_HI), .STAGE_FLITS(STAGE_FLITS),
          .ILINK(ILINK), .MESH_ID(MESH_ID), .LINK_W(LINK_W),
          .TUSER_W(TUSER_W)) u_mag (
        .clk(clk), .resetn(resetn),
        .sm_awid(sm_awid), .sm_awaddr(sm_awaddr), .sm_awlen(sm_awlen),
        .sm_awvalid(sm_awvalid), .sm_awready(sm_awready),
        .sm_wdata(sm_wdata), .sm_wstrb(sm_wstrb), .sm_wlast(sm_wlast),
        .sm_wvalid(sm_wvalid), .sm_wready(sm_wready),
        .sm_bid(sm_bid), .sm_bresp(sm_bresp), .sm_bvalid(sm_bvalid),
        .sm_bready(sm_bready),
        .sm_arid(sm_arid), .sm_araddr(sm_araddr), .sm_arlen(sm_arlen),
        .sm_arvalid(sm_arvalid), .sm_arready(sm_arready),
        .sm_rid(sm_rid), .sm_rdata(sm_rdata), .sm_rresp(sm_rresp),
        .sm_rlast(sm_rlast), .sm_rvalid(sm_rvalid), .sm_rready(sm_rready),
        .sc_awid(sc_awid), .sc_awaddr(sc_awaddr), .sc_awlen(sc_awlen),
        .sc_awvalid(sc_awvalid), .sc_awready(sc_awready),
        .sc_wdata(sc_wdata), .sc_wstrb(sc_wstrb), .sc_wlast(sc_wlast),
        .sc_wvalid(sc_wvalid), .sc_wready(sc_wready),
        .sc_bid(sc_bid), .sc_bresp(sc_bresp), .sc_bvalid(sc_bvalid),
        .sc_bready(sc_bready),
        .sc_arid(sc_arid), .sc_araddr(sc_araddr), .sc_arlen(sc_arlen),
        .sc_arvalid(sc_arvalid), .sc_arready(sc_arready),
        .sc_rid(sc_rid), .sc_rdata(sc_rdata), .sc_rresp(sc_rresp),
        .sc_rlast(sc_rlast), .sc_rvalid(sc_rvalid), .sc_rready(sc_rready),
        .m_awid(x_awid), .m_awaddr(x_awaddr), .m_awlen(x_awlen),
        .m_awsize(x_awsize), .m_awburst(x_awburst),
        .m_awvalid(x_awvalid), .m_awready(x_awready),
        .m_wdata(x_wdata), .m_wstrb(x_wstrb), .m_wlast(x_wlast),
        .m_wvalid(x_wvalid), .m_wready(x_wready),
        .m_bid(x_bid), .m_bresp(x_bresp), .m_bvalid(x_bvalid),
        .m_bready(x_bready),
        .m_arid(x_arid), .m_araddr(x_araddr), .m_arlen(x_arlen),
        .m_arsize(x_arsize), .m_arburst(x_arburst),
        .m_arvalid(x_arvalid), .m_arready(x_arready),
        .m_rid(x_rid), .m_rdata(x_rdata), .m_rresp(x_rresp),
        .m_rlast(x_rlast), .m_rvalid(x_rvalid), .m_rready(x_rready),
        .mem_in_data(mem_in_data), .mem_in_valid(mem_in_valid),
        .mem_in_busy(mem_in_busy),
        .mem_out_data(mem_out_data), .mem_out_valid(mem_out_valid),
        .mem_out_busy(mem_out_busy),
        .mem_rd_count(mem_rd_count), .mem_wr_count(mem_wr_count),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done),
        .link0_out_tdata(link0_out_tdata), .link0_out_tuser(link0_out_tuser),
        .link0_out_tlast(link0_out_tlast), .link0_out_tvalid(link0_out_tvalid),
        .link0_out_tready(link0_out_tready),
        .link0_in_tdata(link0_in_tdata), .link0_in_tuser(link0_in_tuser),
        .link0_in_tlast(link0_in_tlast), .link0_in_tvalid(link0_in_tvalid),
        .link0_in_tready(link0_in_tready),
        .link1_out_tdata(link1_out_tdata), .link1_out_tuser(link1_out_tuser),
        .link1_out_tlast(link1_out_tlast), .link1_out_tvalid(link1_out_tvalid),
        .link1_out_tready(link1_out_tready),
        .link1_in_tdata(link1_in_tdata), .link1_in_tuser(link1_in_tuser),
        .link1_in_tlast(link1_in_tlast), .link1_in_tvalid(link1_in_tvalid),
        .link1_in_tready(link1_in_tready)
    );

    // ---- AXI -> request, one shim per master -----------------------------
    wire [NREQ-1:0]        q_valid, q_ready, q_write;
    wire [NREQ*ADDR_W-1:0] q_addr;
    wire [NREQ*16-1:0]     q_len;
    wire [NREQ-1:0]        w_valid, w_ready;
    wire [NREQ*DATA_W-1:0] w_data;
    wire [NREQ-1:0]        r_valid, r_ready, r_last;
    wire [NREQ*DATA_W-1:0] r_data;
    wire [NREQ-1:0]        b_done;

    genvar i;
    generate for (i = 0; i < NREQ; i = i + 1) begin : g_shim
        // WRITE WINS when both are offered: the W beats are already queued
        // behind it, and a read can wait a burst.
        wire aw = x_awvalid[i];
        wire ar = x_arvalid[i] && !aw;
        assign q_valid[i]              = aw || ar;
        assign q_write[i]              = aw;
        assign q_addr[i*ADDR_W +: ADDR_W] = aw ? x_awaddr[i*ADDR_W +: ADDR_W]
                                               : x_araddr[i*ADDR_W +: ADDR_W];
        assign q_len[i*16 +: 16]       = aw ? {8'd0, x_awlen[i*8 +: 8]}
                                            : {8'd0, x_arlen[i*8 +: 8]};
        assign x_awready[i]            = q_ready[i] && aw;
        assign x_arready[i]            = q_ready[i] && ar;

        assign w_valid[i]              = x_wvalid[i];
        assign w_data[i*DATA_W +: DATA_W] = x_wdata[i*DATA_W +: DATA_W];
        assign x_wready[i]             = w_ready[i];

        assign x_rvalid[i]             = r_valid[i];
        assign x_rdata[i*DATA_W +: DATA_W] = r_data[i*DATA_W +: DATA_W];
        assign x_rlast[i]              = r_last[i];
        assign x_rid[i*ID_W +: ID_W]   = {ID_W{1'b0}};
        assign x_rresp[i*2 +: 2]       = 2'b00;
        assign r_ready[i]              = x_rready[i];

        assign x_bvalid[i]             = b_done[i];
        assign x_bid[i*ID_W +: ID_W]   = {ID_W{1'b0}};
        assign x_bresp[i*2 +: 2]       = 2'b00;
    end endgenerate

    mag_dram_port #(.N(NREQ), .ADDR_W(ADDR_W), .SW(DATA_W), .MW(MW),
                    .ID_W(ID_W)) u_dp (
        .s_aclk(clk), .s_aresetn(resetn),
        .q_valid(q_valid), .q_ready(q_ready), .q_addr(q_addr),
        .q_len(q_len), .q_write(q_write),
        .w_valid(w_valid), .w_ready(w_ready), .w_data(w_data),
        .r_valid(r_valid), .r_ready(r_ready), .r_data(r_data),
        .r_last(r_last), .b_valid(b_done),
        .m_aclk(dram_aclk), .m_aresetn(dram_aresetn),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen),
        .m_awsize(m_awsize), .m_awburst(m_awburst), .m_awvalid(m_awvalid),
        .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid),
        .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen),
        .m_arsize(m_arsize), .m_arburst(m_arburst), .m_arvalid(m_arvalid),
        .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp),
        .m_rlast(m_rlast), .m_rvalid(m_rvalid), .m_rready(m_rready)
    );
endmodule

`default_nettype wire
