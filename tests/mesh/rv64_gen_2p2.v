// A Verilator wrapper around the GENERATED RV64 mesh (ktpu_2p2_rv64, emitted by
// gen_mesh.py --cpu-rv64 --mat-pump). The mesh internals -- router, 2 mat, 2 vec,
// the RV64 sysnode -- are the generator's; this only supplies the test scaffold:
// axi_ram as DRAM on M_AXI_DRAM, one clock fanned to every mesh clock (clk2x for
// the matmul pump), the hs_ host-load window, and the DRAM backdoor.

`default_nettype none

module rv64_gen_2p2 #(
    parameter integer AW  = 40,
    parameter integer IDW = 4,
    parameter integer MW  = 512,
    parameter integer MODEL = 1
)(
    input  wire        clk,
    input  wire        clk2x,
    input  wire        resetn,

    input  wire [31:0] hs_addr,
    input  wire        hs_wr,
    input  wire [63:0] hs_wdata,
    input  wire [7:0]  hs_wstrb,
    input  wire        hs_rd,
    output wire [63:0] hs_rdata,
    output wire        hs_console_we,
    output wire [7:0]  hs_console,

    input  wire        bd_we,
    input  wire [15:0] bd_addr,
    input  wire [MW-1:0] bd_wdata,
    output wire [MW-1:0] bd_rdata
);
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

    ktpu_2p2_rv64 #(.MODEL(MODEL)) u_mesh (
        .axi_aclk(clk), .axi_aresetn(resetn), .noc_clk(clk),
        .mat_clk(clk), .vec_clk(clk), .mat_clk2x(clk2x),
        .dram_aclk(clk), .dram_aresetn(resetn),
        // host-load window
        .hs_addr(hs_addr), .hs_wr(hs_wr), .hs_wdata(hs_wdata), .hs_wstrb(hs_wstrb),
        .hs_rd(hs_rd), .hs_rdata(hs_rdata),
        .hs_console_we(hs_console_we), .hs_console(hs_console),
        // S_AXI_MEM tied off
        .S_AXI_MEM_awid('0), .S_AXI_MEM_awaddr('0), .S_AXI_MEM_awlen('0),
        .S_AXI_MEM_awvalid(1'b0), .S_AXI_MEM_awready(),
        .S_AXI_MEM_wdata('0), .S_AXI_MEM_wstrb('0), .S_AXI_MEM_wlast(1'b0),
        .S_AXI_MEM_wvalid(1'b0), .S_AXI_MEM_wready(),
        .S_AXI_MEM_bid(), .S_AXI_MEM_bresp(), .S_AXI_MEM_bvalid(),
        .S_AXI_MEM_bready(1'b1),
        .S_AXI_MEM_arid('0), .S_AXI_MEM_araddr('0), .S_AXI_MEM_arlen('0),
        .S_AXI_MEM_arvalid(1'b0), .S_AXI_MEM_arready(),
        .S_AXI_MEM_rid(), .S_AXI_MEM_rdata(), .S_AXI_MEM_rresp(),
        .S_AXI_MEM_rlast(), .S_AXI_MEM_rvalid(), .S_AXI_MEM_rready(1'b1),
        // S_AXI_CTRL tied off
        .S_AXI_CTRL_awid('0), .S_AXI_CTRL_awaddr('0), .S_AXI_CTRL_awlen('0),
        .S_AXI_CTRL_awvalid(1'b0), .S_AXI_CTRL_awready(),
        .S_AXI_CTRL_wdata('0), .S_AXI_CTRL_wstrb('0), .S_AXI_CTRL_wlast(1'b0),
        .S_AXI_CTRL_wvalid(1'b0), .S_AXI_CTRL_wready(),
        .S_AXI_CTRL_bid(), .S_AXI_CTRL_bresp(), .S_AXI_CTRL_bvalid(),
        .S_AXI_CTRL_bready(1'b1),
        .S_AXI_CTRL_arid('0), .S_AXI_CTRL_araddr('0), .S_AXI_CTRL_arlen('0),
        .S_AXI_CTRL_arvalid(1'b0), .S_AXI_CTRL_arready(),
        .S_AXI_CTRL_rid(), .S_AXI_CTRL_rdata(), .S_AXI_CTRL_rresp(),
        .S_AXI_CTRL_rlast(), .S_AXI_CTRL_rvalid(), .S_AXI_CTRL_rready(1'b1),
        // M_AXI_DRAM -> axi_ram
        .M_AXI_DRAM_awid(dram_awid), .M_AXI_DRAM_awaddr(dram_awaddr),
        .M_AXI_DRAM_awlen(dram_awlen), .M_AXI_DRAM_awsize(dram_awsize),
        .M_AXI_DRAM_awburst(dram_awburst), .M_AXI_DRAM_awvalid(dram_awvalid),
        .M_AXI_DRAM_awready(dram_awready),
        .M_AXI_DRAM_wdata(dram_wdata), .M_AXI_DRAM_wstrb(dram_wstrb),
        .M_AXI_DRAM_wlast(dram_wlast), .M_AXI_DRAM_wvalid(dram_wvalid),
        .M_AXI_DRAM_wready(dram_wready),
        .M_AXI_DRAM_bid(dram_bid), .M_AXI_DRAM_bresp(dram_bresp),
        .M_AXI_DRAM_bvalid(dram_bvalid), .M_AXI_DRAM_bready(dram_bready),
        .M_AXI_DRAM_arid(dram_arid), .M_AXI_DRAM_araddr(dram_araddr),
        .M_AXI_DRAM_arlen(dram_arlen), .M_AXI_DRAM_arsize(dram_arsize),
        .M_AXI_DRAM_arburst(dram_arburst), .M_AXI_DRAM_arvalid(dram_arvalid),
        .M_AXI_DRAM_arready(dram_arready),
        .M_AXI_DRAM_rid(dram_rid), .M_AXI_DRAM_rdata(dram_rdata),
        .M_AXI_DRAM_rresp(dram_rresp), .M_AXI_DRAM_rlast(dram_rlast),
        .M_AXI_DRAM_rvalid(dram_rvalid), .M_AXI_DRAM_rready(dram_rready)
    );

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

endmodule

`default_nettype wire
