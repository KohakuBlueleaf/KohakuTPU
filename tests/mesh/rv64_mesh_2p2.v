// rv64_mesh_2p2 -- a Verilator-drivable whole mesh for validation: the RV64
// system node, one router, two matmul clusters and two vector cores, with an
// axi_ram standing in for DRAM. One clock. The host loads and boots the RV64
// through hs_*, the same window the standalone syscore bench uses; the processor
// then reaches DRAM through MAG and dispatches compute to the units over the
// real router.
//
//   xxx vec xxx      vec0 at (x=1,y=0) north
//   mag mat mat      node=west(0,1), mat0 at (1,1) local, mat1 at (2,1) east
//   xxx vec xxx      vec1 at (x=1,y=2) south

`default_nettype none

module rv64_mesh_2p2 #(
    parameter integer FW  = 288,
    parameter integer PW  = 4,
    parameter integer DW  = 256,
    parameter integer AW  = 40,
    parameter integer IDW = 4,
    parameter integer MW  = 512,
    parameter integer MODEL = 1
)(
    input  wire        clk,
    input  wire        resetn,

    input  wire [31:0] hs_addr,
    input  wire        hs_wr,
    input  wire [63:0] hs_wdata,
    input  wire [7:0]  hs_wstrb,
    input  wire        hs_rd,
    output wire [63:0] hs_rdata,
    output wire        hs_console_we,
    output wire [7:0]  hs_console,
    output wire [63:0] pe_status,
    output wire        pe_busy,

    // DRAM backdoor: the harness preloads operands and reads results by word.
    input  wire        bd_we,
    input  wire [15:0] bd_addr,
    input  wire [MW-1:0] bd_wdata,
    output wire [MW-1:0] bd_rdata
);
    // ---- DRAM AXI: node master <-> axi_ram --------------------------------
    wire [IDW-1:0]  dram_awid, dram_arid, dram_bid, dram_rid;
    wire [AW-1:0]   dram_awaddr, dram_araddr;
    wire [7:0]      dram_awlen, dram_arlen;
    wire [2:0]      dram_awsize, dram_arsize;
    wire [1:0]      dram_awburst, dram_arburst, dram_bresp, dram_rresp;
    wire            dram_awvalid, dram_awready, dram_wvalid, dram_wready, dram_wlast;
    wire            dram_bvalid, dram_bready, dram_arvalid, dram_arready;
    wire            dram_rvalid, dram_rready, dram_rlast;
    wire [MW-1:0]   dram_wdata, dram_rdata;
    wire [MW/8-1:0] dram_wstrb;

    // ---- node mesh port <-> router west -----------------------------------
    wire [FW-1:0] mag_i_d, mag_o_d;
    wire          mag_i_v, mag_i_b, mag_o_v, mag_o_b;

    // ---- router <-> units (f = unit->router, r = router->unit) ------------
    wire [FW-1:0] l001_fd, l001_rd, l002_fd, l002_rd;
    wire [FW-1:0] l003_fd, l003_rd, l004_fd, l004_rd;
    wire l001_fv, l001_fb, l001_rv, l001_rb;
    wire l002_fv, l002_fb, l002_rv, l002_rb;
    wire l003_fv, l003_fb, l003_rv, l003_rb;
    wire l004_fv, l004_fb, l004_rv, l004_rb;

    wire [15:0] mem_rd_count, mem_wr_count;
    wire        mv_busy;
    wire [3:0]  mv_fault;
    wire [31:0] mv_done;

    // ---- the RV64 system node ---------------------------------------------
    sysnode #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW),
              .ID_W(IDW), .PORTS(1), .MEM_X(0), .MEM_Y(1),
              .GRID_LO(1), .GRID_HI(1), .STAGE_FLITS(128), .MW(MW),
              .STAGE(1), .STAGE_AT_PORT(1), .ILINK(0),
              .PE_IMEM(8192), .PE_SPAD(4096),
              .PE_L1_LINES(64)) u_node (
        .clk(clk), .resetn(resetn),
        .sm_awid('0), .sm_awaddr('0), .sm_awlen('0), .sm_awvalid(1'b0), .sm_awready(),
        .sm_wdata('0), .sm_wstrb('0), .sm_wlast(1'b0), .sm_wvalid(1'b0), .sm_wready(),
        .sm_bid(), .sm_bresp(), .sm_bvalid(), .sm_bready(1'b1),
        .sm_arid('0), .sm_araddr('0), .sm_arlen('0), .sm_arvalid(1'b0), .sm_arready(),
        .sm_rid(), .sm_rdata(), .sm_rresp(), .sm_rlast(), .sm_rvalid(), .sm_rready(1'b1),
        .sc_awid('0), .sc_awaddr('0), .sc_awlen('0), .sc_awvalid(1'b0), .sc_awready(),
        .sc_wdata('0), .sc_wstrb('0), .sc_wlast(1'b0), .sc_wvalid(1'b0), .sc_wready(),
        .sc_bid(), .sc_bresp(), .sc_bvalid(), .sc_bready(1'b1),
        .sc_arid('0), .sc_araddr('0), .sc_arlen('0), .sc_arvalid(1'b0), .sc_arready(),
        .sc_rid(), .sc_rdata(), .sc_rresp(), .sc_rlast(), .sc_rvalid(), .sc_rready(1'b1),
        .dram_aclk(clk), .dram_aresetn(resetn),
        .dram_awid(dram_awid), .dram_awaddr(dram_awaddr), .dram_awlen(dram_awlen),
        .dram_awsize(dram_awsize), .dram_awburst(dram_awburst),
        .dram_awvalid(dram_awvalid), .dram_awready(dram_awready),
        .dram_wdata(dram_wdata), .dram_wstrb(dram_wstrb), .dram_wlast(dram_wlast),
        .dram_wvalid(dram_wvalid), .dram_wready(dram_wready),
        .dram_bid(dram_bid), .dram_bresp(dram_bresp), .dram_bvalid(dram_bvalid),
        .dram_bready(dram_bready),
        .dram_arid(dram_arid), .dram_araddr(dram_araddr), .dram_arlen(dram_arlen),
        .dram_arsize(dram_arsize), .dram_arburst(dram_arburst),
        .dram_arvalid(dram_arvalid), .dram_arready(dram_arready),
        .dram_rid(dram_rid), .dram_rdata(dram_rdata), .dram_rresp(dram_rresp),
        .dram_rlast(dram_rlast), .dram_rvalid(dram_rvalid), .dram_rready(dram_rready),
        .mem_in_data(mag_i_d), .mem_in_valid(mag_i_v), .mem_in_busy(mag_i_b),
        .mem_out_data(mag_o_d), .mem_out_valid(mag_o_v), .mem_out_busy(mag_o_b),
        .mem_rd_count(mem_rd_count), .mem_wr_count(mem_wr_count),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done),
        .pe_halt_req(1'b0), .pe_status(pe_status), .pe_busy(pe_busy),
        .hs_addr(hs_addr), .hs_wr(hs_wr), .hs_wdata(hs_wdata), .hs_wstrb(hs_wstrb),
        .hs_rd(hs_rd), .hs_rdata(hs_rdata),
        .hs_console_we(hs_console_we), .hs_console(hs_console),
        .link0_out_valid(), .link0_out_vc(), .link0_out_last(),
        .link0_out_flit(), .link0_out_crd_valid(1'b0), .link0_out_crd_vc(1'b0),
        .link0_out_crd_n('0),
        .link0_in_valid(1'b0), .link0_in_vc(1'b0), .link0_in_last(1'b0),
        .link0_in_flit('0), .link0_in_crd_valid(), .link0_in_crd_vc(),
        .link0_in_crd_n(),
        .link1_out_valid(), .link1_out_vc(), .link1_out_last(),
        .link1_out_flit(), .link1_out_crd_valid(1'b0), .link1_out_crd_vc(1'b0),
        .link1_out_crd_n('0),
        .link1_in_valid(1'b0), .link1_in_vc(1'b0), .link1_in_last(1'b0),
        .link1_in_flit('0), .link1_in_crd_valid(), .link1_in_crd_vc(),
        .link1_in_crd_n()
    );

    // ---- DRAM -------------------------------------------------------------
    axi_ram #(.DATA_W(MW), .ADDR_W(AW), .ID_W(IDW), .WORDS(16384), .PORTS(1)) u_dram (
        .clk(clk), .resetn(resetn),
        .s_awid(dram_awid), .s_awaddr(dram_awaddr), .s_awlen(dram_awlen),
        .s_awsize(dram_awsize), .s_awburst(dram_awburst),
        .s_awvalid(dram_awvalid), .s_awready(dram_awready),
        .s_wdata(dram_wdata), .s_wstrb(dram_wstrb), .s_wlast(dram_wlast),
        .s_wvalid(dram_wvalid), .s_wready(dram_wready),
        .s_bid(dram_bid), .s_bresp(dram_bresp), .s_bvalid(dram_bvalid),
        .s_bready(dram_bready),
        .s_arid(dram_arid), .s_araddr(dram_araddr), .s_arlen(dram_arlen),
        .s_arsize(dram_arsize), .s_arburst(dram_arburst),
        .s_arvalid(dram_arvalid), .s_arready(dram_arready),
        .s_rid(dram_rid), .s_rdata(dram_rdata), .s_rresp(dram_rresp),
        .s_rlast(dram_rlast), .s_rvalid(dram_rvalid), .s_rready(dram_rready),
        .bd_we(bd_we), .bd_addr(bd_addr), .bd_wdata(bd_wdata), .bd_rdata(bd_rdata)
    );

    // ---- router at (1,1) --------------------------------------------------
    NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(512), .MEMORY_TYPE("block"),
                .POS_WIDTH(PW), .POS_X(1), .POS_Y(1), .GRID_LO(1),
                .GRID_HI(1), .GRID_X_HI(1), .GRID_Y_HI(1)) r1_1 (
        .clk(clk), .rst(!resetn),
        .west_in_data(mag_o_d),   .west_in_valid(mag_o_v),   .west_in_busy(mag_o_b),
        .west_out_data(mag_i_d),  .west_out_valid(mag_i_v),  .west_out_busy(mag_i_b),
        .east_in_data(l001_rd),   .east_in_valid(l001_rv),   .east_in_busy(l001_rb),
        .east_out_data(l001_fd),  .east_out_valid(l001_fv),  .east_out_busy(l001_fb),
        .north_in_data(l002_fd),  .north_in_valid(l002_fv),  .north_in_busy(l002_fb),
        .north_out_data(l002_rd), .north_out_valid(l002_rv), .north_out_busy(l002_rb),
        .south_in_data(l003_rd),  .south_in_valid(l003_rv),  .south_in_busy(l003_rb),
        .south_out_data(l003_fd), .south_out_valid(l003_fv), .south_out_busy(l003_fb),
        .local_in_data(l004_fd),  .local_in_valid(l004_fv),  .local_in_busy(l004_fb),
        .local_out_data(l004_rd), .local_out_valid(l004_rv), .local_out_busy(l004_rb)
    );

    // ---- the compute units ------------------------------------------------
    mx_cluster_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .CU_X(1), .CU_Y(1),
                    .TILES(4096), .GA(512), .GB(512), .L1_PRIM("block"),
                    .TILE_PRIM("ultra"), .INST_DEPTH(512), .RECV_DEPTH(512),
                    .UNIT_CDC(0), .CDC_DEPTH(16), .MEM_X(0), .MEM_Y(1),
                    .MODEL(MODEL)) u_mat0 (
        .clk(clk), .clk2x(1'b0), .unit_clk(clk), .resetn(resetn),
        .noc_out_data(l004_fd), .noc_out_valid(l004_fv), .noc_out_busy(l004_fb),
        .noc_in_data(l004_rd), .noc_in_valid(l004_rv), .noc_in_busy(l004_rb),
        .fills_done(), .gemms_done(), .drains_done()
    );
    mx_cluster_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .CU_X(2), .CU_Y(1),
                    .TILES(4096), .GA(512), .GB(512), .L1_PRIM("block"),
                    .TILE_PRIM("ultra"), .INST_DEPTH(512), .RECV_DEPTH(512),
                    .UNIT_CDC(0), .CDC_DEPTH(16), .MEM_X(0), .MEM_Y(1),
                    .MODEL(MODEL)) u_mat1 (
        .clk(clk), .clk2x(1'b0), .unit_clk(clk), .resetn(resetn),
        .noc_out_data(l001_rd), .noc_out_valid(l001_rv), .noc_out_busy(l001_rb),
        .noc_in_data(l001_fd), .noc_in_valid(l001_fv), .noc_in_busy(l001_fb),
        .fills_done(), .gemms_done(), .drains_done()
    );
    vec_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(1), .POS_Y(0),
             .MEM_X(0), .MEM_Y(1), .MODEL(MODEL), .INST_DEPTH(512), .RECV_DEPTH(512),
             .UNIT_CDC(0), .CDC_DEPTH(16), .L1_DEPTH(512), .L1_PRIM("block")) u_vec0 (
        .clk(clk), .unit_clk(clk), .resetn(resetn),
        .noc_out_data(l002_fd), .noc_out_valid(l002_fv), .noc_out_busy(l002_fb),
        .noc_in_data(l002_rd), .noc_in_valid(l002_rv), .noc_in_busy(l002_rb),
        .dbg_cycles(), .dbg_fault()
    );
    vec_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(1), .POS_Y(2),
             .MEM_X(0), .MEM_Y(1), .MODEL(MODEL), .INST_DEPTH(512), .RECV_DEPTH(512),
             .UNIT_CDC(0), .CDC_DEPTH(16), .L1_DEPTH(512), .L1_PRIM("block")) u_vec1 (
        .clk(clk), .unit_clk(clk), .resetn(resetn),
        .noc_out_data(l003_rd), .noc_out_valid(l003_rv), .noc_out_busy(l003_rb),
        .noc_in_data(l003_fd), .noc_in_valid(l003_fv), .noc_in_busy(l003_fb),
        .dbg_cycles(), .dbg_fault()
    );

endmodule

`default_nettype wire
