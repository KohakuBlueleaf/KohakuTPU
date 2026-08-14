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
    parameter integer AW      = 40,
    parameter integer IDW     = 4,
    parameter integer MEMP    = 1,
    parameter integer NCH     = MEMP + 2,
    parameter integer MW      = DW,
    parameter integer MODEL   = 0,
    parameter integer GRID_LO = 1,
    parameter integer GRID_HI = 2,
    // L2 staging in the cluster's local link. 0 generates the straight wire
    // the mesh had before, so every other bench is bit for bit unchanged.
    parameter integer L2_CU       = 0,
    parameter integer L2_CU_DEPTH = 8192,
    parameter [39:0]  L2_CU_BASE  = 40'h00_F000_0000,
    parameter integer L2_CU_BITS  = 18,
    parameter integer L2_CU_A34   = 0,
    // Same slot in the VECTOR core's local link. A separate base, or the two
    // adapters claim one aperture and the second to decode it never sees a flit.
    parameter integer L2_VEC       = 0,
    parameter integer L2_VEC_DEPTH = 8192,
    parameter [39:0]  L2_VEC_BASE  = 40'h00_F100_0000,
    parameter integer L2_VEC_BITS  = 18,
    parameter integer L2_VEC_A34   = 0,
    // MAG-side staging, special aperture 0. 0 generates none of it.
    parameter integer L2_MAG         = 0,
    parameter integer L2_MAG_BANKS   = 4,      // 64 URAM, 2 MB
    parameter integer L2_MAG_ENTRIES = 16384,
    parameter integer L2_MAG_MESH    = 0,
    // 1 moves the store off the NoC memory port onto MAG's converged internal
    // path, where the mover and the interlink can also reach it.
    parameter integer L2_MAG_AT_PORT = 0,
    // ONE RATE PER TYPE: cluster on `mat_clk`, vector core on `vec_clk`.
    // Routers, MAG and the L2 slot stay on `clk`; only the two CU ports cross.
    parameter integer UNIT_CDC  = 0,
    parameter integer CDC_DEPTH = 16
)(
    input  wire clk,
    // Tie both to `clk` when UNIT_CDC is 0. They reach nothing.
    input  wire mat_clk,
    input  wire vec_clk,
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
    // ONE AXI master, out of MAG's own mag_dram_port. NCH survives only as the
    // count of INTERNAL requesters behind it.
    input  wire                dram_aclk,
    input  wire                dram_aresetn,
    output wire [IDW-1:0]      dram_awid,
    output wire [AW-1:0]       dram_awaddr,
    output wire [7:0]          dram_awlen,
    output wire [2:0]          dram_awsize,
    output wire [1:0]          dram_awburst,
    output wire                dram_awvalid,
    input  wire                dram_awready,
    output wire [MW-1:0]       dram_wdata,
    output wire [MW/8-1:0]     dram_wstrb,
    output wire                dram_wlast,
    output wire                dram_wvalid,
    input  wire                dram_wready,
    input  wire [IDW-1:0]      dram_bid,
    input  wire [1:0]          dram_bresp,
    input  wire                dram_bvalid,
    output wire                dram_bready,
    output wire [IDW-1:0]      dram_arid,
    output wire [AW-1:0]       dram_araddr,
    output wire [7:0]          dram_arlen,
    output wire [2:0]          dram_arsize,
    output wire [1:0]          dram_arburst,
    output wire                dram_arvalid,
    input  wire                dram_arready,
    input  wire [IDW-1:0]      dram_rid,
    input  wire [MW-1:0]       dram_rdata,
    input  wire [1:0]          dram_rresp,
    input  wire                dram_rlast,
    input  wire                dram_rvalid,
    output wire                dram_rready,

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
          .MW(MW),
          .MEM_PORTS(MEMP), .MEM_X(0), .MEM_Y(1),
          .GRID_LO(GRID_LO), .GRID_HI(GRID_HI), .STAGE_FLITS(128),
          .MESH_ID(L2_MAG_MESH),
          .STAGE(L2_MAG), .STAGE_BANKS(L2_MAG_BANKS),
          .STAGE_ENTRIES(L2_MAG_ENTRIES), .STAGE_PIPE(1),
          .STAGE_AT_PORT(L2_MAG_AT_PORT)) u_mag (
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
        .dram_aclk(dram_aclk), .dram_aresetn(dram_aresetn),
        .dram_awid(dram_awid), .dram_awaddr(dram_awaddr),
        .dram_awlen(dram_awlen), .dram_awsize(dram_awsize),
        .dram_awburst(dram_awburst),
        .dram_awvalid(dram_awvalid), .dram_awready(dram_awready),
        .dram_wdata(dram_wdata), .dram_wstrb(dram_wstrb),
        .dram_wlast(dram_wlast),
        .dram_wvalid(dram_wvalid), .dram_wready(dram_wready),
        .dram_bid(dram_bid), .dram_bresp(dram_bresp),
        .dram_bvalid(dram_bvalid), .dram_bready(dram_bready),
        .dram_arid(dram_arid), .dram_araddr(dram_araddr),
        .dram_arlen(dram_arlen), .dram_arsize(dram_arsize),
        .dram_arburst(dram_arburst),
        .dram_arvalid(dram_arvalid), .dram_arready(dram_arready),
        .dram_rid(dram_rid), .dram_rdata(dram_rdata), .dram_rresp(dram_rresp),
        .dram_rlast(dram_rlast),
        .dram_rvalid(dram_rvalid), .dram_rready(dram_rready),
        .mem_in_data(mag_i), .mem_in_valid(mag_iv), .mem_in_busy(mag_ib),
        .mem_out_data(mag_o), .mem_out_valid(mag_ov), .mem_out_busy(mag_ob),
        .mem_rd_count(mag_rd), .mem_wr_count(mag_wr),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done)
    );

    wire [15:0] cf, cg, cd;

    // ---- the addon slot in the cluster's local link ------------------------
    // The cluster sees cl_*, the router cu_*; at L2_CU=0 they are one net.
    wire [FW-1:0] cl_i, cl_o;
    wire          cl_iv, cl_ib, cl_ov, cl_ob;

    noc_l2_adapter #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DEPTH(L2_CU_DEPTH),
                     .L2_BASE(L2_CU_BASE), .L2_BITS(L2_CU_BITS),
                     .ADDR34(L2_CU_A34),
                     .PASS((L2_CU != 0) ? 0 : 1)) u_l2_cu (
        .clk(clk), .rst(rst),
        .rt_data(cu_i), .rt_valid(cu_iv), .rt_busy(cu_ib),
        .ru_data(cu_o), .ru_valid(cu_ov), .ru_busy(cu_ob),
        .ep_data(cl_i), .ep_valid(cl_iv), .ep_busy(cl_ib),
        .eu_data(cl_o), .eu_valid(cl_ov), .eu_busy(cl_ob)
    );

    mx_cluster_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                    .CU_X(2), .CU_Y(1), .UNIT_CDC(UNIT_CDC),
                    .CDC_DEPTH(CDC_DEPTH),
                    .MEM_X(0), .MEM_Y(1), .MODEL(MODEL)) u_cluster (
        .clk(clk), .clk2x(1'b0), .unit_clk(mat_clk), .resetn(rstn),
        .noc_in_data(cl_i), .noc_in_valid(cl_iv), .noc_in_busy(cl_ib),
        .noc_out_data(cl_o), .noc_out_valid(cl_ov), .noc_out_busy(cl_ob),
        .fills_done(cf), .gemms_done(cg), .drains_done(cd)
    );

    wire [31:0] vcyc;
    wire        vflt;

    // The vector core sees vl_*, the router vec_*; at L2_VEC=0 they are one net.
    wire [FW-1:0] vl_i, vl_o;
    wire          vl_iv, vl_ib, vl_ov, vl_ob;

    noc_l2_adapter #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DEPTH(L2_VEC_DEPTH),
                     .L2_BASE(L2_VEC_BASE), .L2_BITS(L2_VEC_BITS),
                     .ADDR34(L2_VEC_A34),
                     .PASS((L2_VEC != 0) ? 0 : 1)) u_l2_vec (
        .clk(clk), .rst(rst),
        .rt_data(vec_i), .rt_valid(vec_iv), .rt_busy(vec_ib),
        .ru_data(vec_o), .ru_valid(vec_ov), .ru_busy(vec_ob),
        .ep_data(vl_i), .ep_valid(vl_iv), .ep_busy(vl_ib),
        .eu_data(vl_o), .eu_valid(vl_ov), .eu_busy(vl_ob)
    );

    vec_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(1), .POS_Y(0),
             .MEM_X(0), .MEM_Y(1), .MODEL(MODEL), .L1_DEPTH(512),
             .UNIT_CDC(UNIT_CDC), .CDC_DEPTH(CDC_DEPTH),
             .L1_PRIM("block")) u_vec (
        .clk(clk), .unit_clk(vec_clk), .resetn(rstn),
        .noc_in_data(vl_i), .noc_in_valid(vl_iv), .noc_in_busy(vl_ib),
        .noc_out_data(vl_o), .noc_out_valid(vl_ov), .noc_out_busy(vl_ob),
        .dbg_cycles(vcyc), .dbg_fault(vflt)
    );

    assign dbg_cluster    = {cf, cg, cd};
    assign dbg_vec_cycles = vcyc;
    assign dbg_vec_fault  = vflt;

    assign obs = {15'd0, vflt} ^ vcyc ^ {mag_rd, mag_wr}
               ^ {cf ^ cg, cd} ^ mv_done;

endmodule

`default_nettype wire
