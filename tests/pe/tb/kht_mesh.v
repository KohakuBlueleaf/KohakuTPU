// kht_mesh -- the SIMT PE integration vehicle: one SIMT PE, a real router, the
// real MAG, and an AXI RAM.
//
//        MAG (0,1) --- R(1,1) --- (bench agent at (1,0), north)
//                        |
//                     kht_pe, on the LOCAL port, coordinate (1,1)
//
// NOTHING HERE IS A STUB. A shader's load really does become a MEM_RD_REQ,
// cross the router, get served by MAG's read engine as a one-entry streaming
// fetch, come back as a MEM_RD_RESP and fill a line; a dirty eviction really
// does become a MEM_WR_REQ and a beat in the RAM. A coalescer verified against
// a memory that answers instantly is not verified, which is why the vehicle is
// the real agent rather than a model of one.
//
// ONE ROUTER IS ENOUGH FOR ONE PE. XY routing clamps a destination into the
// grid, so the agent north of the router and MAG west of it are both reachable
// without the rest of a mesh existing. More PEs would need more routers; this
// bench is about whether one PE's traffic is right.

`default_nettype none

module kht_mesh #(
    parameter integer FW         = 288,
    parameter integer PW         = 4,
    parameter integer DW         = 256,
    parameter integer AW         = 40,
    parameter integer IDW        = 4,
    parameter integer IMEM_WORDS = 2048,
    parameter integer SPAD_WORDS = 2048,
    parameter integer L1_LINES   = 128,
    parameter integer LANES      = 8,
    parameter integer WAVES      = 16,
    parameter integer HAS_MASK   = 1,
    parameter integer HAS_IPDOM  = 1,
    parameter integer HAS_LDSBANK = 1,
    parameter integer HAS_SHFL   = 1,
    // The three float/multiply unit counts, each its own, 0 = not built. A bench
    // that runs a shader at FLANES < LANES is testing the pass walk, which is
    // the only place it can be tested against real kernels.
    parameter integer FLANES     = 8,
    parameter integer FSFU_UNITS = 0,
    parameter integer MUL_UNITS  = 8,
    parameter integer HAS_F16    = 1,
    parameter integer HAS_F32    = 1,
    parameter integer SHFL_UNITS = 0,
    parameter integer LDS_BANKS  = 0,
    // 0, MATCHING kht_pe, and that is the guard rather than a preference:
    // kht_unit's FMODEL "defaults to the SYNTHESIS value, so a bench that
    // forgets it fails to elaborate rather than quietly measuring a different
    // multiplier". Defaulting to 1 here defeated exactly that -- every SIMT
    // float shader ran vec_dsp's behavioural model, not DSP48E2. The two do
    // agree (vec_alu at --model 0: 26,897 checks, every group's worst case
    // bit-identical to MODEL=1), so nothing was hidden; the guard was.
    parameter integer FMODEL     = 0,
    parameter integer IPDOM_D    = 8,
    parameter         VREG_PRIM  = "block",
    parameter integer RAM_DEPTH  = 4096
)(
    input  wire clk,
    input  wire rstn,

    input  wire [FW-1:0] ext_in_data,
    input  wire          ext_in_valid,
    output wire          ext_in_busy,
    output wire [FW-1:0] ext_out_data,
    output wire          ext_out_valid,
    input  wire          ext_out_busy,

    output wire          pe_run,
    output wire          pe_halted,
    output wire          pe_busy,
    output wire [31:0]   pe_reqs,
    output wire [31:0]   pe_gathers,
    output wire [LANES-1:0] pe_mask
);
    localparam integer GLO = 1, GHI = 2;
    wire rst = !rstn;

    wire [FW-1:0] mag_i, mag_o, lo_i, lo_o;
    wire          mag_iv, mag_ib, mag_ov, mag_ob;
    wire          lo_iv, lo_ib, lo_ov, lo_ob;

    NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("block"),
                .POS_WIDTH(PW), .POS_X(1), .POS_Y(1),
                .GRID_LO(GLO), .GRID_HI(GHI)) r11 (
        .clk(clk), .rst(rst),
        .west_in_data(mag_o),   .west_in_valid(mag_ov),   .west_in_busy(mag_ob),
        .west_out_data(mag_i),  .west_out_valid(mag_iv),  .west_out_busy(mag_ib),
        .north_in_data(ext_in_data), .north_in_valid(ext_in_valid),
        .north_in_busy(ext_in_busy),
        .north_out_data(ext_out_data), .north_out_valid(ext_out_valid),
        .north_out_busy(ext_out_busy),
        .east_in_data({FW{1'b0}}), .east_in_valid(1'b0), .east_in_busy(),
        .east_out_data(), .east_out_valid(), .east_out_busy(1'b0),
        .south_in_data({FW{1'b0}}), .south_in_valid(1'b0), .south_in_busy(),
        .south_out_data(), .south_out_valid(), .south_out_busy(1'b0),
        .local_in_data(lo_o),  .local_in_valid(lo_ov),  .local_in_busy(lo_ob),
        .local_out_data(lo_i), .local_out_valid(lo_iv), .local_out_busy(lo_ib)
    );

    kht_pe #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
             .POS_X(1), .POS_Y(1), .MEM_X(0), .MEM_Y(1),
             // Software DRAM at 0x8000_0000 maps to physical 0: the low 31 bits
             // pass through and no aperture bit is set.
             .DRAM_BASE(40'h00_0000_0000),
             .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS),
             .L1_LINES(L1_LINES), .LANES(LANES), .WAVES(WAVES),
             .HAS_MASK(HAS_MASK), .HAS_IPDOM(HAS_IPDOM),
             .HAS_LDSBANK(HAS_LDSBANK), .HAS_SHFL(HAS_SHFL),
             .FLANES(FLANES), .FSFU_UNITS(FSFU_UNITS), .MUL_UNITS(MUL_UNITS),
             .HAS_F16(HAS_F16), .HAS_F32(HAS_F32),
             .SHFL_UNITS(SHFL_UNITS), .LDS_BANKS(LDS_BANKS),
             .FMODEL(FMODEL),
             .IPDOM_D(IPDOM_D),
             .MEM_PRIM("block"), .VREG_PRIM(VREG_PRIM),
             .INST_DEPTH(16), .RECV_DEPTH(512)) u_pe (
        .clk(clk), .resetn(rstn),
        .noc_in_data(lo_i), .noc_in_valid(lo_iv), .noc_in_busy(lo_ib),
        .noc_out_data(lo_o), .noc_out_valid(lo_ov), .noc_out_busy(lo_ob),
        .busy(pe_busy),
        .dbg_run(pe_run), .dbg_halted(pe_halted),
        .dbg_retire_pc(), .dbg_retire_valid(), .dbg_mask(pe_mask),
        .dbg_reqs(pe_reqs), .dbg_gathers(pe_gathers)
    );

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
        .sm_awid({IDW{1'b0}}), .sm_awaddr({AW{1'b0}}), .sm_awlen(8'd0),
        .sm_awvalid(1'b0), .sm_awready(),
        .sm_wdata({DW{1'b0}}), .sm_wstrb({(DW/8){1'b1}}), .sm_wlast(1'b0),
        .sm_wvalid(1'b0), .sm_wready(),
        .sm_bid(), .sm_bresp(), .sm_bvalid(), .sm_bready(1'b1),
        .sm_arid({IDW{1'b0}}), .sm_araddr({AW{1'b0}}), .sm_arlen(8'd0),
        .sm_arvalid(1'b0), .sm_arready(),
        .sm_rid(), .sm_rdata(), .sm_rresp(), .sm_rlast(), .sm_rvalid(),
        .sm_rready(1'b1),
        .sc_awid({IDW{1'b0}}), .sc_awaddr(32'd0), .sc_awlen(8'd0),
        .sc_awvalid(1'b0), .sc_awready(),
        .sc_wdata(64'd0), .sc_wstrb(8'hFF), .sc_wlast(1'b1), .sc_wvalid(1'b0),
        .sc_wready(),
        .sc_bid(), .sc_bresp(), .sc_bvalid(), .sc_bready(1'b1),
        .sc_arid({IDW{1'b0}}), .sc_araddr(32'd0), .sc_arlen(8'd0),
        .sc_arvalid(1'b0), .sc_arready(),
        .sc_rid(), .sc_rdata(), .sc_rresp(), .sc_rlast(), .sc_rvalid(),
        .sc_rready(1'b1),
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
