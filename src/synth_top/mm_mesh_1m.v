// mm_mesh behind ONE 512-bit AXI master: the e2e that proves mag_dram_port
// carries agent, memory-port, mover and upload traffic, not directed bursts.

`default_nettype none

module mm_mesh_1m #(
    parameter integer FW      = 288,
    parameter integer PW      = 4,
    parameter integer DW      = 256,
    parameter integer AW      = 34,
    parameter integer IDW     = 4,
    parameter integer MEMP    = 1,
    parameter integer NCH     = MEMP + 2,
    parameter integer MW      = 512,
    parameter integer MODEL   = 0,
    parameter integer GRID_LO = 1,
    parameter integer GRID_HI = 2
)(
    input  wire clk,
    input  wire rst,
    input  wire dram_aclk,
    input  wire dram_aresetn,

    input  wire [AW-1:0]  sm_awaddr,
    input  wire [7:0]     sm_awlen,
    input  wire           sm_awvalid,
    input  wire [DW-1:0]  sm_wdata,
    input  wire           sm_wlast,
    input  wire           sm_wvalid,
    input  wire [31:0]    sc_awaddr,
    input  wire           sc_awvalid,
    output wire           sc_awready,
    input  wire [63:0]    sc_wdata,
    input  wire           sc_wvalid,
    output wire           sc_wready,
    output wire           sc_bvalid,
    input  wire [31:0]    sc_araddr,
    input  wire           sc_arvalid,
    output wire [63:0]    sc_rdata,
    output wire           sc_rvalid,

    output wire           mv_busy,
    output wire [3:0]     mv_fault,
    output wire [31:0]    mv_done,

    output wire [IDW-1:0] m_awid,
    output wire [AW-1:0]  m_awaddr,
    output wire [7:0]     m_awlen,
    output wire [2:0]     m_awsize,
    output wire [1:0]     m_awburst,
    output wire           m_awvalid,
    input  wire           m_awready,
    output wire [MW-1:0]  m_wdata,
    output wire [MW/8-1:0] m_wstrb,
    output wire           m_wlast,
    output wire           m_wvalid,
    input  wire           m_wready,
    input  wire [IDW-1:0] m_bid,
    input  wire [1:0]     m_bresp,
    input  wire           m_bvalid,
    output wire           m_bready,
    output wire [IDW-1:0] m_arid,
    output wire [AW-1:0]  m_araddr,
    output wire [7:0]     m_arlen,
    output wire [2:0]     m_arsize,
    output wire [1:0]     m_arburst,
    output wire           m_arvalid,
    input  wire           m_arready,
    input  wire [IDW-1:0] m_rid,
    input  wire [MW-1:0]  m_rdata,
    input  wire [1:0]     m_rresp,
    input  wire           m_rlast,
    input  wire           m_rvalid,
    output wire           m_rready,

    input  wire [FW-1:0]  ext_in_data,
    input  wire           ext_in_valid,
    output wire           ext_in_busy,
    output wire [FW-1:0]  ext_out_data,
    output wire           ext_out_valid,
    input  wire           ext_out_busy,
    output wire [47:0]    dbg_cluster,
    output wire [31:0]    dbg_vec_cycles,
    output wire           dbg_vec_fault,
    output wire [31:0]    obs
);
    wire [NCH*IDW-1:0]  x_awid, x_arid, x_bid, x_rid;
    wire [NCH*AW-1:0]   x_awaddr, x_araddr;
    wire [NCH*8-1:0]    x_awlen, x_arlen;
    wire [NCH*3-1:0]    x_awsize, x_arsize;
    wire [NCH*2-1:0]    x_awburst, x_arburst, x_bresp, x_rresp;
    wire [NCH-1:0]      x_awvalid, x_awready, x_arvalid, x_arready;
    wire [NCH*DW-1:0]   x_wdata, x_rdata;
    wire [NCH*DW/8-1:0] x_wstrb;
    wire [NCH-1:0]      x_wlast, x_wvalid, x_wready;
    wire [NCH-1:0]      x_bvalid, x_bready, x_rlast, x_rvalid, x_rready;

    mm_mesh #(.FW(FW), .PW(PW), .DW(DW), .AW(AW), .IDW(IDW), .MEMP(MEMP),
              .MODEL(MODEL), .GRID_LO(GRID_LO), .GRID_HI(GRID_HI)) u_mesh (
        .clk(clk), .rst(rst),
        .sm_awaddr(sm_awaddr), .sm_awlen(sm_awlen), .sm_awvalid(sm_awvalid),
        .sm_wdata(sm_wdata), .sm_wlast(sm_wlast), .sm_wvalid(sm_wvalid),
        .sc_awaddr(sc_awaddr), .sc_awvalid(sc_awvalid), .sc_awready(sc_awready),
        .sc_wdata(sc_wdata), .sc_wvalid(sc_wvalid), .sc_wready(sc_wready),
        .sc_bvalid(sc_bvalid), .sc_araddr(sc_araddr), .sc_arvalid(sc_arvalid),
        .sc_rdata(sc_rdata), .sc_rvalid(sc_rvalid),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done),
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
        .ext_in_data(ext_in_data), .ext_in_valid(ext_in_valid),
        .ext_in_busy(ext_in_busy),
        .ext_out_data(ext_out_data), .ext_out_valid(ext_out_valid),
        .ext_out_busy(ext_out_busy),
        .dbg_cluster(dbg_cluster), .dbg_vec_cycles(dbg_vec_cycles),
        .dbg_vec_fault(dbg_vec_fault), .obs(obs)
    );

    wire [NCH-1:0]      q_valid, q_ready, q_write, w_valid, w_ready;
    wire [NCH*AW-1:0]   q_addr;
    wire [NCH*16-1:0]   q_len;
    wire [NCH*DW-1:0]   w_data, r_data;
    wire [NCH-1:0]      r_valid, r_ready, r_last, b_done;

    genvar i;
    generate for (i = 0; i < NCH; i = i + 1) begin : g_shim
        // WRITE WINS when a port offers both: its W beats are already queued
        // behind the AW, and a read can wait one burst.
        wire aw = x_awvalid[i];
        wire ar = x_arvalid[i] && !aw;
        assign q_valid[i]           = aw || ar;
        assign q_write[i]           = aw;
        assign q_addr[i*AW +: AW]   = aw ? x_awaddr[i*AW +: AW]
                                         : x_araddr[i*AW +: AW];
        assign q_len[i*16 +: 16]    = aw ? {8'd0, x_awlen[i*8 +: 8]}
                                         : {8'd0, x_arlen[i*8 +: 8]};
        assign x_awready[i]         = q_ready[i] && aw;
        assign x_arready[i]         = q_ready[i] && ar;
        assign w_valid[i]           = x_wvalid[i];
        assign w_data[i*DW +: DW]   = x_wdata[i*DW +: DW];
        assign x_wready[i]          = w_ready[i];
        assign x_rvalid[i]          = r_valid[i];
        assign x_rdata[i*DW +: DW]  = r_data[i*DW +: DW];
        assign x_rlast[i]           = r_last[i];
        assign x_rid[i*IDW +: IDW]  = {IDW{1'b0}};
        assign x_rresp[i*2 +: 2]    = 2'b00;
        assign r_ready[i]           = x_rready[i];
        assign x_bvalid[i]          = b_done[i];
        assign x_bid[i*IDW +: IDW]  = {IDW{1'b0}};
        assign x_bresp[i*2 +: 2]    = 2'b00;
    end endgenerate

    mag_dram_port #(.N(NCH), .ADDR_W(AW), .SW(DW), .MW(MW), .ID_W(IDW)) u_dp (
        .s_aclk(clk), .s_aresetn(!rst),
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
