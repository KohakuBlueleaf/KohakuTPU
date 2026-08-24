// het_mesh -- ONE router, one MAG, and three kinds of PE on it at once.
//
//                 DSP-N (1,0)
//                      | north
//   MAG (0,1) ---- R (1,1) ---- GPU (2,1)
//            west     |  east
//                 DSP-S (1,2)
//                 CPU (1,1), on the LOCAL port
//
// FIVE ENDPOINTS, ONE ROUTER. A PE does not need the local port -- MAG has
// always hung off west -- so the four directions carry four endpoints and local
// carries the fifth. Every destination clamps to the only node and leaves by
// the port its true coordinate lies through, so no second router can help.
//
// THERE IS NO AGENT ON THE NoC. On the card the host reaches the mesh through
// MAG's AXI slaves and nothing else, so those are what this module exposes and
// a bench-side AXI master stands in for the host. An agent wired onto a router
// port would be testing a machine that does not exist.
//
// A TEST VEHICLE. Nothing here is synthesised or shipped.

`default_nettype none

module het_mesh #(
    parameter integer FW         = 288,
    parameter integer PW         = 4,
    parameter integer DW         = 256,
    parameter integer AW         = 40,
    parameter integer IDW        = 4,
    parameter integer IMEM_WORDS = 2048,
    parameter integer SPAD_WORDS = 2048,
    parameter integer L1_LINES   = 128,
    parameter integer BTB_ENTRIES = 32,
    parameter         REGFILE_PRIM = "distributed",
    parameter integer FWD_X      = 1,
    parameter integer SIMD_LANES   = 8,
    parameter integer SIMD_VREGS  = 8,
    parameter integer SIMD_NACC   = 2,
    parameter integer SIMD_VSPAD  = 1024,
    parameter integer SIMD_MULS   = 4,
    // 0 FOR NOW, and the reason is scheduling rather than design: the float
    // tier's accumulator and fold are being reworked for FP32 input and may not
    // survive in their present form, and `FLOAT_MODEL` is not plumbed through
    // `rv_pe` so a float SIMD PE only elaborates at --model 0. The DSP's role
    // here is the INTEGER SIMD tier, `vdot`, which that rework does not touch.
    parameter integer SIMD_FLOAT    = 0,
    parameter integer SIMD_FLOAT_LANES = 4,
    parameter integer LANES      = 8,
    parameter integer WAVES      = 16,
    parameter integer FMODEL     = 1,
    parameter integer RAM_DEPTH  = 1024
)(
    input  wire clk,
    input  wire rstn,

    // ---- S_AXI_MEM: the host's window onto memory, 256-bit ----
    input  wire [IDW-1:0]  sm_awid,
    input  wire [AW-1:0]   sm_awaddr,
    input  wire [7:0]      sm_awlen,
    input  wire            sm_awvalid,
    output wire            sm_awready,
    input  wire [DW-1:0]   sm_wdata,
    input  wire [DW/8-1:0] sm_wstrb,
    input  wire            sm_wlast,
    input  wire            sm_wvalid,
    output wire            sm_wready,
    output wire            sm_bvalid,
    input  wire            sm_bready,
    input  wire [IDW-1:0]  sm_arid,
    input  wire [AW-1:0]   sm_araddr,
    input  wire [7:0]      sm_arlen,
    input  wire            sm_arvalid,
    output wire            sm_arready,
    output wire [DW-1:0]   sm_rdata,
    output wire            sm_rlast,
    output wire            sm_rvalid,
    input  wire            sm_rready,

    // ---- S_AXI_CTRL: the orchestrator, 64-bit ----
    input  wire [IDW-1:0]  sc_awid,
    input  wire [31:0]     sc_awaddr,
    input  wire            sc_awvalid,
    output wire            sc_awready,
    input  wire [63:0]     sc_wdata,
    input  wire            sc_wvalid,
    output wire            sc_wready,
    output wire            sc_bvalid,
    input  wire            sc_bready,
    input  wire [IDW-1:0]  sc_arid,
    input  wire [31:0]     sc_araddr,
    input  wire            sc_arvalid,
    output wire            sc_arready,
    output wire [63:0]     sc_rdata,
    output wire            sc_rvalid,
    input  wire            sc_rready,

    output wire [3:0]      pe_run,
    output wire [3:0]      pe_halted,
    output wire [3:0]      pe_busy
);
    // GHI 2 so (2,1) and (1,2) are IN the grid and route east/south; GLO 1 so
    // (0,1) and (1,0) fall outside and clamp back to the only node.
    localparam integer GLO = 1, GHI = 2;
    wire rst = !rstn;

    wire [FW-1:0] mag_i, mag_o;
    wire          mag_iv, mag_ib, mag_ov, mag_ob;

    // 0 CPU (local), 1 GPU (east), 2 DSP-N (north), 3 DSP-S (south)
    wire [FW-1:0] lo_i [0:3];
    wire [FW-1:0] lo_o [0:3];
    wire          lo_iv [0:3], lo_ib [0:3], lo_ov [0:3], lo_ob [0:3];

    NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("block"),
                .POS_WIDTH(PW), .POS_X(1), .POS_Y(1),
                .GRID_LO(GLO), .GRID_HI(GHI)) r11 (
        .clk(clk), .rst(rst),
        .west_in_data(mag_o),   .west_in_valid(mag_ov),   .west_in_busy(mag_ob),
        .west_out_data(mag_i),  .west_out_valid(mag_iv),  .west_out_busy(mag_ib),
        .north_in_data(lo_o[2]),  .north_in_valid(lo_ov[2]),  .north_in_busy(lo_ob[2]),
        .north_out_data(lo_i[2]), .north_out_valid(lo_iv[2]), .north_out_busy(lo_ib[2]),
        .east_in_data(lo_o[1]),   .east_in_valid(lo_ov[1]),   .east_in_busy(lo_ob[1]),
        .east_out_data(lo_i[1]),  .east_out_valid(lo_iv[1]),  .east_out_busy(lo_ib[1]),
        .south_in_data(lo_o[3]),  .south_in_valid(lo_ov[3]),  .south_in_busy(lo_ob[3]),
        .south_out_data(lo_i[3]), .south_out_valid(lo_iv[3]), .south_out_busy(lo_ib[3]),
        .local_in_data(lo_o[0]),  .local_in_valid(lo_ov[0]),  .local_in_busy(lo_ob[0]),
        .local_out_data(lo_i[0]), .local_out_valid(lo_iv[0]), .local_out_busy(lo_ib[0])
    );

    // ---- the controller PE, on the local port ------------------------------
    rv_pe #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
            .POS_X(1), .POS_Y(1), .MEM_X(0), .MEM_Y(1),
            .DRAM_BASE(40'h00_0000_0000),
            .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS),
            .L1_LINES(L1_LINES), .BTB_ENTRIES(BTB_ENTRIES),
            .REGFILE_PRIM(REGFILE_PRIM), .FWD_X(FWD_X), .MEM_PRIM("block"),
            .INST_DEPTH(16), .RECV_DEPTH(32),
            .SIMD_EN(0)) u_cpu (
        .clk(clk), .resetn(rstn),
        .noc_in_data(lo_i[0]), .noc_in_valid(lo_iv[0]), .noc_in_busy(lo_ib[0]),
        .noc_out_data(lo_o[0]), .noc_out_valid(lo_ov[0]), .noc_out_busy(lo_ob[0]),
        .busy(pe_busy[0]),
        .dbg_run(pe_run[0]), .dbg_halted(pe_halted[0]),
        .dbg_retire_pc(), .dbg_retire_valid(), .dbg_retire_rd(), .dbg_retire_val()
    );

    // ---- the SIMT PE, east ---------------------------------------------------
    kht_pe #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
             .POS_X(2), .POS_Y(1), .MEM_X(0), .MEM_Y(1),
             .DRAM_BASE(40'h00_0000_0000),
             .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS),
             .L1_LINES(L1_LINES), .LANES(LANES), .WAVES(WAVES),
             .HAS_MASK(1), .HAS_IPDOM(1), .HAS_LDSBANK(1), .HAS_SHFL(1),
             .FLANES(LANES), .MUL_UNITS(LANES), .FMODEL(FMODEL),
             .IPDOM_D(8), .MEM_PRIM("block"), .VREG_PRIM("block"),
             .INST_DEPTH(16), .RECV_DEPTH(512)) u_gpu (
        .clk(clk), .resetn(rstn),
        .noc_in_data(lo_i[1]), .noc_in_valid(lo_iv[1]), .noc_in_busy(lo_ib[1]),
        .noc_out_data(lo_o[1]), .noc_out_valid(lo_ov[1]), .noc_out_busy(lo_ob[1]),
        .busy(pe_busy[1]),
        .dbg_run(pe_run[1]), .dbg_halted(pe_halted[1]),
        .dbg_retire_pc(), .dbg_retire_valid(), .dbg_mask(),
        .dbg_reqs(), .dbg_gathers()
    );

    // ---- the two SIMD PEs, north and south -----------------------------------
    genvar g;
    generate
    for (g = 2; g < 4; g = g + 1) begin : g_simd
        localparam integer PYV = (g == 2) ? 0 : 2;
        rv_pe #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                .POS_X(1), .POS_Y(PYV), .MEM_X(0), .MEM_Y(1),
                .DRAM_BASE(40'h00_0000_0000),
                .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS),
                .L1_LINES(L1_LINES), .BTB_ENTRIES(BTB_ENTRIES),
                .REGFILE_PRIM(REGFILE_PRIM), .FWD_X(FWD_X), .MEM_PRIM("block"),
                .INST_DEPTH(16), .RECV_DEPTH(32),
                .SIMD_EN(1), .SIMD_LANES(SIMD_LANES), .SIMD_VREGS(SIMD_VREGS),
                .SIMD_NACC(SIMD_NACC), .SIMD_VSPAD(SIMD_VSPAD),
                .SIMD_MULS(SIMD_MULS), .SIMD_FLOAT(SIMD_FLOAT),
                .SIMD_FLOAT_LANES(SIMD_FLOAT_LANES)) u_dsp (
            .clk(clk), .resetn(rstn),
            .noc_in_data(lo_i[g]), .noc_in_valid(lo_iv[g]), .noc_in_busy(lo_ib[g]),
            .noc_out_data(lo_o[g]), .noc_out_valid(lo_ov[g]),
            .noc_out_busy(lo_ob[g]),
            .busy(pe_busy[g]),
            .dbg_run(pe_run[g]), .dbg_halted(pe_halted[g]),
            .dbg_retire_pc(), .dbg_retire_valid(), .dbg_retire_rd(),
            .dbg_retire_val()
        );
    end
    endgenerate

    // ---- MAG and the memory behind it --------------------------------------
    wire [IDW-1:0]   d_awid, d_arid, d_bid, d_rid;
    wire [AW-1:0]    d_awaddr, d_araddr;
    wire [7:0]       d_awlen, d_arlen;
    wire [2:0]       d_awsize, d_arsize;
    wire [1:0]       d_awburst, d_arburst, d_bresp, d_rresp;
    wire             d_awvalid, d_awready, d_arvalid, d_arready;
    wire [DW-1:0]    d_wdata, d_rdata;
    wire [DW/8-1:0]  d_wstrb;
    wire             d_wlast, d_wvalid, d_wready;
    wire             d_bvalid, d_bready, d_rlast, d_rvalid, d_rready;

    sysnode #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW),
          .ID_W(IDW), .MW(DW), .PORTS(1), .MEM_X(0), .MEM_Y(1),
          .GRID_LO(GLO), .GRID_HI(GHI), .STAGE_FLITS(128),
          .MESH_ID(0), .STAGE(0)) u_mag (
        .clk(clk), .resetn(rstn),
        .sm_awid(sm_awid), .sm_awaddr(sm_awaddr), .sm_awlen(sm_awlen),
        .sm_awvalid(sm_awvalid), .sm_awready(sm_awready),
        .sm_wdata(sm_wdata), .sm_wstrb(sm_wstrb), .sm_wlast(sm_wlast),
        .sm_wvalid(sm_wvalid), .sm_wready(sm_wready),
        .sm_bid(), .sm_bresp(), .sm_bvalid(sm_bvalid), .sm_bready(sm_bready),
        .sm_arid(sm_arid), .sm_araddr(sm_araddr), .sm_arlen(sm_arlen),
        .sm_arvalid(sm_arvalid), .sm_arready(sm_arready),
        .sm_rid(), .sm_rdata(sm_rdata), .sm_rresp(), .sm_rlast(sm_rlast),
        .sm_rvalid(sm_rvalid), .sm_rready(sm_rready),
        .sc_awid(sc_awid), .sc_awaddr(sc_awaddr), .sc_awlen(8'd0),
        .sc_awvalid(sc_awvalid), .sc_awready(sc_awready),
        .sc_wdata(sc_wdata), .sc_wstrb(8'hFF), .sc_wlast(1'b1),
        .sc_wvalid(sc_wvalid), .sc_wready(sc_wready),
        .sc_bid(), .sc_bresp(), .sc_bvalid(sc_bvalid), .sc_bready(sc_bready),
        .sc_arid(sc_arid), .sc_araddr(sc_araddr), .sc_arlen(8'd0),
        .sc_arvalid(sc_arvalid), .sc_arready(sc_arready),
        .sc_rid(), .sc_rdata(sc_rdata), .sc_rresp(), .sc_rlast(),
        .sc_rvalid(sc_rvalid), .sc_rready(sc_rready),
        .dram_aclk(clk), .dram_aresetn(rstn),
        .dram_awid(d_awid), .dram_awaddr(d_awaddr), .dram_awlen(d_awlen),
        .dram_awsize(d_awsize), .dram_awburst(d_awburst),
        .dram_awvalid(d_awvalid), .dram_awready(d_awready),
        .dram_wdata(d_wdata), .dram_wstrb(d_wstrb), .dram_wlast(d_wlast),
        .dram_wvalid(d_wvalid), .dram_wready(d_wready),
        .dram_bid(d_bid), .dram_bresp(d_bresp), .dram_bvalid(d_bvalid),
        .dram_bready(d_bready),
        .dram_arid(d_arid), .dram_araddr(d_araddr), .dram_arlen(d_arlen),
        .dram_arsize(d_arsize), .dram_arburst(d_arburst),
        .dram_arvalid(d_arvalid), .dram_arready(d_arready),
        .dram_rid(d_rid), .dram_rdata(d_rdata), .dram_rresp(d_rresp),
        .dram_rlast(d_rlast), .dram_rvalid(d_rvalid), .dram_rready(d_rready),
        .mem_in_data(mag_i), .mem_in_valid(mag_iv), .mem_in_busy(mag_ib),
        .mem_out_data(mag_o), .mem_out_valid(mag_ov), .mem_out_busy(mag_ob),
        .mem_rd_count(), .mem_wr_count(),
        .pe_halt_req(1'b0), .pe_status(), .pe_busy(),
        .mv_busy(), .mv_fault(), .mv_done(),
        .link0_out_tdata(), .link0_out_tuser(), .link0_out_tlast(),
        .link0_out_tvalid(), .link0_out_tready(1'b1),
        .link0_in_tdata({FW{1'b0}}), .link0_in_tuser(96'd0),
        .link0_in_tlast(1'b0), .link0_in_tvalid(1'b0), .link0_in_tready(),
        .link1_out_tdata(), .link1_out_tuser(), .link1_out_tlast(),
        .link1_out_tvalid(), .link1_out_tready(1'b1),
        .link1_in_tdata({FW{1'b0}}), .link1_in_tuser(96'd0),
        .link1_in_tlast(1'b0), .link1_in_tvalid(1'b0), .link1_in_tready()
    );

    axi4_ram #(.DATA_WIDTH(DW), .ADDR_WIDTH(AW), .ID_WIDTH(IDW),
               .DEPTH(RAM_DEPTH)) u_ram (
        .clk(clk), .resetn(rstn),
        .s_axi_awid(d_awid), .s_axi_awaddr(d_awaddr), .s_axi_awlen(d_awlen),
        .s_axi_awsize(d_awsize), .s_axi_awburst(d_awburst),
        .s_axi_awvalid(d_awvalid), .s_axi_awready(d_awready),
        .s_axi_wdata(d_wdata), .s_axi_wstrb(d_wstrb), .s_axi_wlast(d_wlast),
        .s_axi_wvalid(d_wvalid), .s_axi_wready(d_wready),
        .s_axi_bid(d_bid), .s_axi_bresp(d_bresp), .s_axi_bvalid(d_bvalid),
        .s_axi_bready(d_bready),
        .s_axi_arid(d_arid), .s_axi_araddr(d_araddr), .s_axi_arlen(d_arlen),
        .s_axi_arsize(d_arsize), .s_axi_arburst(d_arburst),
        .s_axi_arvalid(d_arvalid), .s_axi_arready(d_arready),
        .s_axi_rid(d_rid), .s_axi_rdata(d_rdata), .s_axi_rresp(d_rresp),
        .s_axi_rlast(d_rlast), .s_axi_rvalid(d_rvalid), .s_axi_rready(d_rready)
    );

endmodule

`default_nettype wire
