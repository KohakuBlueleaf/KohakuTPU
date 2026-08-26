// rv_mesh -- the PE integration vehicle: up to four RV32 PEs, four real
// routers, the real MAG, and an AXI RAM.
//
//                    bench agent (1,0)
//                          |
//        MAG (0,1) --- R(1,1) --- R(2,1)
//                        |          |
//                      R(1,2) --- R(2,2)
//
// PE i sits on router i's LOCAL port and therefore carries that router's
// coordinate: (1,1) (2,1) (1,2) (2,2). Nothing here is a stub. The memory path
// is MAG's own -- its agent, its memory port, its write slots, its converged
// DRAM port -- so a program's load really does become a MEM_RD_REQ, cross two
// routers, become an AXI burst, and come back.
//
// The bench attaches at r11's NORTH, outside the router grid, and plays the
// controller: it writes program images in as CU_DATA, sends the CU_INST kick,
// and receives the CU_SIGNAL completion. MAG's own orchestrator answers at the
// memory port's coordinate and is not used here, because the question at this
// level is whether the PE's memory traffic is right, not whose dispatcher sent
// the instruction.
//
// ONE MAG AND ONE NoC IS THE CEILING FOR FOUR PEs (design note s16.9). The
// parameter goes no higher on purpose.

`default_nettype none

module rv_mesh #(
    parameter integer FW           = 288,
    parameter integer PW           = 4,
    parameter integer DW           = 256,
    parameter integer AW           = 40,
    parameter integer IDW          = 4,
    parameter integer NPE          = 1,
    parameter integer IMEM_WORDS   = 2048,
    parameter integer SPAD_WORDS   = 2048,
    parameter integer L1_LINES     = 128,
    parameter integer BTB_ENTRIES  = 32,
    parameter         REGFILE_PRIM = "distributed",
    parameter integer FWD_X        = 1,
    parameter integer RAM_DEPTH    = 4096,      // 256-bit words
    // The DSP extension, off by default so every existing bench builds the
    // shipped controller PE and nothing about them moves.
    parameter integer SIMD_EN       = 0,
    parameter integer SIMD_LANES     = 8,
    parameter integer SIMD_VREGS    = 8,
    parameter integer SIMD_NACC     = 2,
    parameter integer SIMD_VSPAD    = 1024,
    // COMPUTE WIDTHS: 0 is not built. Defaults are the full-width machine, and
    // the names match `rv_pe`'s so a caller does not have to learn two.
    parameter integer SIMD_ILANES      = 8,
    parameter integer SIMD_SHIFT_UNITS = 8,
    parameter integer SIMD_PERM_UNITS  = 8,
    parameter integer SIMD_WB       = 0
)(
    input  wire clk,
    input  wire rstn,

    input  wire [FW-1:0] ext_in_data,
    input  wire          ext_in_valid,
    output wire          ext_in_busy,
    output wire [FW-1:0] ext_out_data,
    output wire          ext_out_valid,
    input  wire          ext_out_busy,

    output wire [3:0]    pe_run,
    output wire [3:0]    pe_halted,
    output wire [3:0]    pe_busy
);
    localparam integer GLO = 1, GHI = 2;
    wire rst = !rstn;

    wire [FW-1:0] mag_i, mag_o;
    wire          mag_iv, mag_ib, mag_ov, mag_ob;

    wire [FW-1:0] lo_i [0:3];
    wire [FW-1:0] lo_o [0:3];
    wire          lo_iv [0:3], lo_ib [0:3], lo_ov [0:3], lo_ob [0:3];

    // inter-router links, named after the pair they join
    wire [FW-1:0] a_11_21, a_21_11, a_11_12, a_12_11;
    wire [FW-1:0] a_21_22, a_22_21, a_12_22, a_22_12;
    wire a_11_21v, a_21_11v, a_11_12v, a_12_11v;
    wire a_21_22v, a_22_21v, a_12_22v, a_22_12v;
    wire a_11_21b, a_21_11b, a_11_12b, a_12_11b;
    wire a_21_22b, a_22_21b, a_12_22b, a_22_12b;

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
        .east_in_data(a_21_11),  .east_in_valid(a_21_11v),  .east_in_busy(a_21_11b),
        .east_out_data(a_11_21), .east_out_valid(a_11_21v), .east_out_busy(a_11_21b),
        .south_in_data(a_12_11),  .south_in_valid(a_12_11v),  .south_in_busy(a_12_11b),
        .south_out_data(a_11_12), .south_out_valid(a_11_12v), .south_out_busy(a_11_12b),
        .local_in_data(lo_o[0]),  .local_in_valid(lo_ov[0]),  .local_in_busy(lo_ob[0]),
        .local_out_data(lo_i[0]), .local_out_valid(lo_iv[0]), .local_out_busy(lo_ib[0])
    );

    NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("block"),
                .POS_WIDTH(PW), .POS_X(2), .POS_Y(1),
                .GRID_LO(GLO), .GRID_HI(GHI)) r21 (
        .clk(clk), .rst(rst),
        .west_in_data(a_11_21),  .west_in_valid(a_11_21v),  .west_in_busy(a_11_21b),
        .west_out_data(a_21_11), .west_out_valid(a_21_11v), .west_out_busy(a_21_11b),
        .north_in_data({FW{1'b0}}), .north_in_valid(1'b0), .north_in_busy(),
        .north_out_data(), .north_out_valid(), .north_out_busy(1'b0),
        .east_in_data({FW{1'b0}}), .east_in_valid(1'b0), .east_in_busy(),
        .east_out_data(), .east_out_valid(), .east_out_busy(1'b0),
        .south_in_data(a_22_21),  .south_in_valid(a_22_21v),  .south_in_busy(a_22_21b),
        .south_out_data(a_21_22), .south_out_valid(a_21_22v), .south_out_busy(a_21_22b),
        .local_in_data(lo_o[1]),  .local_in_valid(lo_ov[1]),  .local_in_busy(lo_ob[1]),
        .local_out_data(lo_i[1]), .local_out_valid(lo_iv[1]), .local_out_busy(lo_ib[1])
    );

    NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("block"),
                .POS_WIDTH(PW), .POS_X(1), .POS_Y(2),
                .GRID_LO(GLO), .GRID_HI(GHI)) r12 (
        .clk(clk), .rst(rst),
        .west_in_data({FW{1'b0}}), .west_in_valid(1'b0), .west_in_busy(),
        .west_out_data(), .west_out_valid(), .west_out_busy(1'b0),
        .north_in_data(a_11_12),  .north_in_valid(a_11_12v),  .north_in_busy(a_11_12b),
        .north_out_data(a_12_11), .north_out_valid(a_12_11v), .north_out_busy(a_12_11b),
        .east_in_data(a_22_12),  .east_in_valid(a_22_12v),  .east_in_busy(a_22_12b),
        .east_out_data(a_12_22), .east_out_valid(a_12_22v), .east_out_busy(a_12_22b),
        .south_in_data({FW{1'b0}}), .south_in_valid(1'b0), .south_in_busy(),
        .south_out_data(), .south_out_valid(), .south_out_busy(1'b0),
        .local_in_data(lo_o[2]),  .local_in_valid(lo_ov[2]),  .local_in_busy(lo_ob[2]),
        .local_out_data(lo_i[2]), .local_out_valid(lo_iv[2]), .local_out_busy(lo_ib[2])
    );

    NoCRouter #(.DATA_WIDTH(FW), .FIFO_DEPTH(32), .MEMORY_TYPE("block"),
                .POS_WIDTH(PW), .POS_X(2), .POS_Y(2),
                .GRID_LO(GLO), .GRID_HI(GHI)) r22 (
        .clk(clk), .rst(rst),
        .west_in_data(a_12_22),  .west_in_valid(a_12_22v),  .west_in_busy(a_12_22b),
        .west_out_data(a_22_12), .west_out_valid(a_22_12v), .west_out_busy(a_22_12b),
        .north_in_data(a_21_22),  .north_in_valid(a_21_22v),  .north_in_busy(a_21_22b),
        .north_out_data(a_22_21), .north_out_valid(a_22_21v), .north_out_busy(a_22_21b),
        .east_in_data({FW{1'b0}}), .east_in_valid(1'b0), .east_in_busy(),
        .east_out_data(), .east_out_valid(), .east_out_busy(1'b0),
        .south_in_data({FW{1'b0}}), .south_in_valid(1'b0), .south_in_busy(),
        .south_out_data(), .south_out_valid(), .south_out_busy(1'b0),
        .local_in_data(lo_o[3]),  .local_in_valid(lo_ov[3]),  .local_in_busy(lo_ob[3]),
        .local_out_data(lo_i[3]), .local_out_valid(lo_iv[3]), .local_out_busy(lo_ib[3])
    );

    genvar g;
    generate
    for (g = 0; g < 4; g = g + 1) begin : g_pe
        localparam integer PXV = ((g == 0) || (g == 2)) ? 1 : 2;
        localparam integer PYV = (g < 2) ? 1 : 2;
        if (g < NPE) begin : g_have
            rv_pe #(.FLIT_WIDTH(FW), .POS_WIDTH(PW),
                    .POS_X(PXV), .POS_Y(PYV), .MEM_X(0), .MEM_Y(1),
                    // Software DRAM at 0x8000_0000 maps to physical 0: the low
                    // 31 bits pass through and no aperture bit is set.
                    .DRAM_BASE(40'h00_0000_0000),
                    .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS),
                    .L1_LINES(L1_LINES), .BTB_ENTRIES(BTB_ENTRIES),
                    .REGFILE_PRIM(REGFILE_PRIM),
                    .FWD_X(FWD_X), .MEM_PRIM("block"),
                    .INST_DEPTH(16), .RECV_DEPTH(32),
                    .SIMD_EN(SIMD_EN), .SIMD_LANES(SIMD_LANES),
                    .SIMD_VREGS(SIMD_VREGS), .SIMD_NACC(SIMD_NACC),
                    .SIMD_VSPAD(SIMD_VSPAD), .SIMD_ILANES(SIMD_ILANES),
                    .SIMD_SHIFT_UNITS(SIMD_SHIFT_UNITS),
                    .SIMD_PERM_UNITS(SIMD_PERM_UNITS),
                    .SIMD_WB(SIMD_WB)) u_pe (
                .clk(clk), .resetn(rstn),
                .noc_in_data(lo_i[g]), .noc_in_valid(lo_iv[g]),
                .noc_in_busy(lo_ib[g]),
                .noc_out_data(lo_o[g]), .noc_out_valid(lo_ov[g]),
                .noc_out_busy(lo_ob[g]),
                .busy(pe_busy[g]),
                .dbg_run(pe_run[g]), .dbg_halted(pe_halted[g]),
                .dbg_retire_pc(), .dbg_retire_valid(), .dbg_retire_rd(),
                .dbg_retire_val()
            );
        end else begin : g_none
            assign lo_o[g]     = {FW{1'b0}};
            assign lo_ov[g]    = 1'b0;
            assign lo_ib[g]    = 1'b0;
            assign pe_busy[g]  = 1'b0;
            assign pe_run[g]   = 1'b0;
            assign pe_halted[g]= 1'b0;
        end
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
