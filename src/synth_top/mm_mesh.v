// The minimal machine: 1 MAG, 1 matmul cluster, 1 vector core, 2 routers.
//
// Small enough to synthesise in one go and complete enough that nothing in it
// is a stub -- MAG carries the agent, its memory ports and the memory mover;
// the cluster is the real endpoint; the vector core is the real sequencer and
// lane array. If this closes timing, the parts are compatible at the assembly
// level, which one-module-at-a-time synthesis cannot say.
//
//        vec(1,0)
//            |
//   MAG(0,1)-R(1,1)-------R(2,1)
//                          |
//                       cluster, local
//
// TWO ROUTERS FOR ONE CLUSTER, still. The cluster's second local went away and
// r21's north with it, but r11 is where the bench injects and r21 is what makes
// the link between them a real hop -- collapsing to one router would delete the
// hop this bench exists to exercise, not the router the merge saves.
//
// Coordinates outside GRID_LO..GRID_HI are reached by the router's clamp, which
// is how a border endpoint like MAG at (0,1) is addressed -- the same scheme
// tests/mas/mag_system_tb.v uses.
//
// Every AXI master leaves the module and every status counter is observable, so
// nothing here is eligible for pruning.

`default_nettype none

module mm_mesh #(
    parameter integer FW      = 288,
    parameter integer PW      = 4,
    parameter integer DW      = 256,
    parameter integer AW      = 34,
    parameter integer IDW     = 4,
    parameter integer MEMP    = 1,
    parameter integer NCH     = MEMP + 2,
    parameter integer MODEL   = 0,
    parameter integer GRID_LO = 1,
    parameter integer GRID_HI = 2
)(
    input  wire clk,
    input  wire rst,

    // ---- host windows into MAG ----
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

    // ---- the mover's status. Its COMMANDS arrive through sc_*, at the
    // orchestrator's A_AUX_CFG window, so there is no sideband port to wire.
    output wire           mv_busy,
    output wire [3:0]     mv_fault,
    output wire [31:0]    mv_done,

    // ---- AXI masters out to memory ----
    output wire [NCH*IDW-1:0]  m_awid,
    output wire [NCH*AW-1:0]   m_awaddr,
    output wire [NCH*8-1:0]    m_awlen,
    output wire [NCH*3-1:0]    m_awsize,
    output wire [NCH*2-1:0]    m_awburst,
    output wire [NCH-1:0]      m_awvalid,
    input  wire [NCH-1:0]      m_awready,
    output wire [NCH*DW-1:0]   m_wdata,
    output wire [NCH*DW/8-1:0] m_wstrb,
    output wire [NCH-1:0]      m_wlast,
    output wire [NCH-1:0]      m_wvalid,
    input  wire [NCH-1:0]      m_wready,
    input  wire [NCH*IDW-1:0]  m_bid,
    input  wire [NCH*2-1:0]    m_bresp,
    input  wire [NCH-1:0]      m_bvalid,
    output wire [NCH-1:0]      m_bready,
    output wire [NCH*IDW-1:0]  m_arid,
    output wire [NCH*AW-1:0]   m_araddr,
    output wire [NCH*8-1:0]    m_arlen,
    output wire [NCH*3-1:0]    m_arsize,
    output wire [NCH*2-1:0]    m_arburst,
    output wire [NCH-1:0]      m_arvalid,
    input  wire [NCH-1:0]      m_arready,
    input  wire [NCH*IDW-1:0]  m_rid,
    input  wire [NCH*DW-1:0]   m_rdata,
    input  wire [NCH*2-1:0]    m_rresp,
    input  wire [NCH-1:0]      m_rlast,
    input  wire [NCH-1:0]      m_rvalid,
    output wire [NCH-1:0]      m_rready,

    // ---- a NoC injection port on r11's local, for a testbench to act as the
    // agent. Tied off it would be dead logic; brought out it is both the
    // stimulus path for mm_mesh_tb and one more endpoint synthesis must keep.
    input  wire [FW-1:0]  ext_in_data,
    input  wire           ext_in_valid,
    output wire           ext_in_busy,
    output wire [FW-1:0]  ext_out_data,
    output wire           ext_out_valid,
    input  wire           ext_out_busy,

    // ---- observable, so nothing prunes ----
    output wire [47:0]    dbg_cluster,     // {fills, gemms, drains}
    output wire [31:0]    dbg_vec_cycles,
    output wire           dbg_vec_fault,
    output wire [31:0]    obs
);
    wire rstn = !rst;

    // router <-> endpoint links
    wire [FW-1:0] mag_i, mag_o, vec_i, vec_o, cu_i, cu_o;
    wire mag_iv, mag_ov, mag_ib, mag_ob;
    wire vec_iv, vec_ov, vec_ib, vec_ob;
    wire cu_iv, cu_ov, cu_ib, cu_ob;

    // inter-router link
    wire [FW-1:0] e11_21, e21_11;
    wire e11_21v, e21_11v, e11_21b, e21_11b;

    NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("block"),
                .POS_WIDTH(PW), .POS_X(1), .POS_Y(1),
                .GRID_LO(GRID_LO), .GRID_HI(GRID_HI)) r11 (
        .clk(clk), .rst(rst),
        .west_in_data(mag_o),  .west_in_valid(mag_ov),  .west_in_busy(mag_ob),
        .west_out_data(mag_i), .west_out_valid(mag_iv), .west_out_busy(mag_ib),
        .north_in_data(vec_o),  .north_in_valid(vec_ov),  .north_in_busy(vec_ob),
        .north_out_data(vec_i), .north_out_valid(vec_iv), .north_out_busy(vec_ib),
        .east_in_data(e21_11),  .east_in_valid(e21_11v),  .east_in_busy(e21_11b),
        .east_out_data(e11_21), .east_out_valid(e11_21v), .east_out_busy(e11_21b),
        .south_in_data({FW{1'b0}}), .south_in_valid(1'b0), .south_in_busy(),
        .south_out_data(), .south_out_valid(), .south_out_busy(1'b0),
        .local_in_data(ext_in_data), .local_in_valid(ext_in_valid),
        .local_in_busy(ext_in_busy),
        .local_out_data(ext_out_data), .local_out_valid(ext_out_valid),
        .local_out_busy(ext_out_busy)
    );

    NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("block"),
                .POS_WIDTH(PW), .POS_X(2), .POS_Y(1),
                .GRID_LO(GRID_LO), .GRID_HI(GRID_HI)) r21 (
        .clk(clk), .rst(rst),
        .west_in_data(e11_21),  .west_in_valid(e11_21v),  .west_in_busy(e11_21b),
        .west_out_data(e21_11), .west_out_valid(e21_11v), .west_out_busy(e21_11b),
        .north_in_data({FW{1'b0}}), .north_in_valid(1'b0), .north_in_busy(),
        .north_out_data(), .north_out_valid(), .north_out_busy(1'b0),
        .east_in_data({FW{1'b0}}), .east_in_valid(1'b0), .east_in_busy(),
        .east_out_data(), .east_out_valid(), .east_out_busy(1'b0),
        .south_in_data({FW{1'b0}}), .south_in_valid(1'b0), .south_in_busy(),
        .south_out_data(), .south_out_valid(), .south_out_busy(1'b0),
        .local_in_data(cu_o),  .local_in_valid(cu_ov),  .local_in_busy(cu_ob),
        .local_out_data(cu_i), .local_out_valid(cu_iv), .local_out_busy(cu_ib)
    );

    wire [15:0] mag_rd, mag_wr;

    mag #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW), .ID_W(IDW),
          .MEM_PORTS(MEMP), .MEM_X(0), .MEM_Y(1),
          .GRID_LO(GRID_LO), .GRID_HI(GRID_HI), .STAGE_FLITS(128)) u_mag (
        .clk(clk), .resetn(rstn),
        .sm_awid({IDW{1'b0}}), .sm_awaddr(sm_awaddr), .sm_awlen(sm_awlen),
        .sm_awvalid(sm_awvalid), .sm_awready(),
        .sm_wdata(sm_wdata), .sm_wstrb({(DW/8){1'b1}}), .sm_wlast(sm_wlast),
        .sm_wvalid(sm_wvalid), .sm_wready(),
        .sm_bid(), .sm_bresp(), .sm_bvalid(), .sm_bready(1'b1),
        .sm_arid({IDW{1'b0}}), .sm_araddr({AW{1'b0}}), .sm_arlen(8'd0),
        .sm_arvalid(1'b0), .sm_arready(), .sm_rid(), .sm_rdata(), .sm_rresp(),
        .sm_rlast(), .sm_rvalid(), .sm_rready(1'b1),
        .sc_awid({IDW{1'b0}}), .sc_awaddr(sc_awaddr), .sc_awlen(8'd0),
        .sc_awvalid(sc_awvalid), .sc_awready(sc_awready),
        .sc_wdata(sc_wdata), .sc_wstrb(8'hFF), .sc_wlast(1'b1),
        .sc_wvalid(sc_wvalid), .sc_wready(sc_wready),
        .sc_bid(), .sc_bresp(), .sc_bvalid(sc_bvalid), .sc_bready(1'b1),
        .sc_arid({IDW{1'b0}}), .sc_araddr(sc_araddr), .sc_arlen(8'd0),
        .sc_arvalid(sc_arvalid), .sc_arready(),
        .sc_rid(), .sc_rdata(sc_rdata), .sc_rresp(), .sc_rlast(),
        .sc_rvalid(sc_rvalid), .sc_rready(1'b1),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen),
        .m_awsize(m_awsize), .m_awburst(m_awburst),
        .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid),
        .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen),
        .m_arsize(m_arsize), .m_arburst(m_arburst),
        .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_rvalid(m_rvalid), .m_rready(m_rready),
        .mem_in_data(mag_i), .mem_in_valid(mag_iv), .mem_in_busy(mag_ib),
        .mem_out_data(mag_o), .mem_out_valid(mag_ov), .mem_out_busy(mag_ob),
        .mem_rd_count(mag_rd), .mem_wr_count(mag_wr),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done)
    );

    wire [15:0] cf, cg, cd;

    mx_cluster_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                    .CU_X(2), .CU_Y(1),
                    .MEM_X(0), .MEM_Y(1), .MODEL(MODEL)) u_cluster (
        .clk(clk), .resetn(rstn),
        .noc_in_data(cu_i), .noc_in_valid(cu_iv), .noc_in_busy(cu_ib),
        .noc_out_data(cu_o), .noc_out_valid(cu_ov), .noc_out_busy(cu_ob),
        .fills_done(cf), .gemms_done(cg), .drains_done(cd)
    );

    wire [31:0] vcyc;
    wire        vflt;

    vec_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(1), .POS_Y(0),
             .MEM_X(0), .MEM_Y(1), .MODEL(MODEL), .L1_DEPTH(512),
             .L1_PRIM("block")) u_vec (
        .clk(clk), .resetn(rstn),
        .noc_in_data(vec_i), .noc_in_valid(vec_iv), .noc_in_busy(vec_ib),
        .noc_out_data(vec_o), .noc_out_valid(vec_ov), .noc_out_busy(vec_ob),
        .dbg_cycles(vcyc), .dbg_fault(vflt)
    );

    assign dbg_cluster    = {cf, cg, cd};
    assign dbg_vec_cycles = vcyc;
    assign dbg_vec_fault  = vflt;

    assign obs = {15'd0, vflt} ^ vcyc ^ {mag_rd, mag_wr}
               ^ {cf ^ cg, cd} ^ mv_done;

endmodule

`default_nettype wire
