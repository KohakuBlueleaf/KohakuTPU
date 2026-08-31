// ktpu_node_v8t -- one system node with NO mesh: the fabric side of sysnode
// tied off, its two AXI slaves and its DRAM master brought to the boundary
// exactly as a generated ship top brings them. What multimesh_v8t puts on each
// die. The generator cannot emit this (a map needs a router and a mag tile),
// which is why it is written by hand and lives with the device wrappers.
//
// ILINK 0: no interlink, so no AXIS link ports and nothing crosses a die from
// inside the node. PORTS 2 as the ship's nodes carry, so the probe prices a
// full node's engines even though nothing asks them for memory.

`default_nettype none

module ktpu_node_v8t #(
    parameter integer FW       = 288,
    parameter integer PW       = 4,
    parameter integer DW       = 256,
    parameter integer AW       = 40,
    parameter integer IDW      = 4,
    parameter integer MW       = 512,
    parameter integer PORTS    = 2,
    parameter integer MESH_ID  = 0,
    parameter integer L2_MAG_BANKS   = 4,      // 4 x 4096 x 1024 b = 64 single URAM, 2 MB
    parameter integer L2_MAG_ENTRIES = 16384,
    // 0: dram_aclk MUST be axi_aclk and the DRAM master's queues are
    // synchronous -- the one-clock memory path into the Xache.
    parameter integer DRAM_CDC  = 1,
    parameter integer CDC_DEPTH = 16
)(
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 axi_aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF S_AXI_MEM:S_AXI_CTRL, ASSOCIATED_RESET axi_aresetn" *)
    input  wire axi_aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 axi_aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire axi_aresetn,
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 dram_aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF M_AXI_DRAM, ASSOCIATED_RESET dram_aresetn" *)
    input  wire dram_aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 dram_aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire dram_aresetn,

    input  wire [IDW-1:0]   S_AXI_MEM_awid,
    input  wire [AW-1:0]    S_AXI_MEM_awaddr,
    input  wire [7:0]       S_AXI_MEM_awlen,
    input  wire             S_AXI_MEM_awvalid,
    output wire             S_AXI_MEM_awready,
    input  wire [DW-1:0]    S_AXI_MEM_wdata,
    input  wire [DW/8-1:0]  S_AXI_MEM_wstrb,
    input  wire             S_AXI_MEM_wlast,
    input  wire             S_AXI_MEM_wvalid,
    output wire             S_AXI_MEM_wready,
    output wire [IDW-1:0]   S_AXI_MEM_bid,
    output wire [1:0]       S_AXI_MEM_bresp,
    output wire             S_AXI_MEM_bvalid,
    input  wire             S_AXI_MEM_bready,
    input  wire [IDW-1:0]   S_AXI_MEM_arid,
    input  wire [AW-1:0]    S_AXI_MEM_araddr,
    input  wire [7:0]       S_AXI_MEM_arlen,
    input  wire             S_AXI_MEM_arvalid,
    output wire             S_AXI_MEM_arready,
    output wire [IDW-1:0]   S_AXI_MEM_rid,
    output wire [DW-1:0]    S_AXI_MEM_rdata,
    output wire [1:0]       S_AXI_MEM_rresp,
    output wire             S_AXI_MEM_rlast,
    output wire             S_AXI_MEM_rvalid,
    input  wire             S_AXI_MEM_rready,

    input  wire [IDW-1:0]   S_AXI_CTRL_awid,
    input  wire [31:0]      S_AXI_CTRL_awaddr,
    input  wire [7:0]       S_AXI_CTRL_awlen,
    input  wire             S_AXI_CTRL_awvalid,
    output wire             S_AXI_CTRL_awready,
    input  wire [63:0]      S_AXI_CTRL_wdata,
    input  wire [7:0]       S_AXI_CTRL_wstrb,
    input  wire             S_AXI_CTRL_wlast,
    input  wire             S_AXI_CTRL_wvalid,
    output wire             S_AXI_CTRL_wready,
    output wire [IDW-1:0]   S_AXI_CTRL_bid,
    output wire [1:0]       S_AXI_CTRL_bresp,
    output wire             S_AXI_CTRL_bvalid,
    input  wire             S_AXI_CTRL_bready,
    input  wire [IDW-1:0]   S_AXI_CTRL_arid,
    input  wire [31:0]      S_AXI_CTRL_araddr,
    input  wire [7:0]       S_AXI_CTRL_arlen,
    input  wire             S_AXI_CTRL_arvalid,
    output wire             S_AXI_CTRL_arready,
    output wire [IDW-1:0]   S_AXI_CTRL_rid,
    output wire [63:0]      S_AXI_CTRL_rdata,
    output wire [1:0]       S_AXI_CTRL_rresp,
    output wire             S_AXI_CTRL_rlast,
    output wire             S_AXI_CTRL_rvalid,
    input  wire             S_AXI_CTRL_rready,

    output wire [IDW-1:0]   M_AXI_DRAM_awid,
    output wire [AW-1:0]    M_AXI_DRAM_awaddr,
    output wire [7:0]       M_AXI_DRAM_awlen,
    output wire [2:0]       M_AXI_DRAM_awsize,
    output wire [1:0]       M_AXI_DRAM_awburst,
    output wire             M_AXI_DRAM_awvalid,
    input  wire             M_AXI_DRAM_awready,
    output wire [MW-1:0]    M_AXI_DRAM_wdata,
    output wire [MW/8-1:0]  M_AXI_DRAM_wstrb,
    output wire             M_AXI_DRAM_wlast,
    output wire             M_AXI_DRAM_wvalid,
    input  wire             M_AXI_DRAM_wready,
    input  wire [IDW-1:0]   M_AXI_DRAM_bid,
    input  wire [1:0]       M_AXI_DRAM_bresp,
    input  wire             M_AXI_DRAM_bvalid,
    output wire             M_AXI_DRAM_bready,
    output wire [IDW-1:0]   M_AXI_DRAM_arid,
    output wire [AW-1:0]    M_AXI_DRAM_araddr,
    output wire [7:0]       M_AXI_DRAM_arlen,
    output wire [2:0]       M_AXI_DRAM_arsize,
    output wire [1:0]       M_AXI_DRAM_arburst,
    output wire             M_AXI_DRAM_arvalid,
    input  wire             M_AXI_DRAM_arready,
    input  wire [IDW-1:0]   M_AXI_DRAM_rid,
    input  wire [MW-1:0]    M_AXI_DRAM_rdata,
    input  wire [1:0]       M_AXI_DRAM_rresp,
    input  wire             M_AXI_DRAM_rlast,
    input  wire             M_AXI_DRAM_rvalid,
    output wire             M_AXI_DRAM_rready
);
    localparam integer LKW = 288;
    localparam integer LKU = 96;

    // The same entry a generated top gives the node: async assert, release on
    // the node's own clock.
    wire rstn_mag;
    kh_rst_sync u_rs_mag (.clk(axi_aclk), .arstn(axi_aresetn), .rstn(rstn_mag));

    // Nothing on the fabric side: no flit ever arrives, and a flit the node
    // emits (a kick from its processor) is taken and dropped rather than held.
    wire [PORTS*FW-1:0] mem_out_data_unused;
    wire [PORTS-1:0]    mem_out_valid_unused, mem_in_busy_unused;
    wire [15:0]         mem_rd_unused, mem_wr_unused;
    wire                mv_busy_unused;
    wire [3:0]          mv_fault_unused;
    wire [31:0]         mv_done_unused;
    wire [63:0]         pe_status_unused, hs_rdata_unused;
    wire                pe_busy_unused, hs_console_we_unused;
    wire [7:0]          hs_console_unused;
    wire [LKW-1:0]      l0_flit_unused, l1_flit_unused;
    wire [3:0]          l0_crdn_unused, l1_crdn_unused;
    wire                l0_v_unused, l0_vc_unused, l0_l_unused;
    wire                l0_cv_unused, l0_cvc_unused;
    wire                l1_v_unused, l1_vc_unused, l1_l_unused;
    wire                l1_cv_unused, l1_cvc_unused;

    sysnode #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW),
              .ID_W(IDW), .PORTS(PORTS), .MEM_X(0), .MEM_Y(1), .MEM_X1(0), .MEM_Y1(2),
              .GRID_LO(1), .GRID_HI(2), .STAGE_FLITS(128),
              .ILINK(0), .MESH_ID(MESH_ID), .LINK_W(LKW), .TUSER_W(LKU),
              .MW(MW), .DRAM_CDC(DRAM_CDC),
              .STAGE(1), .STAGE_BANKS(L2_MAG_BANKS),
              .STAGE_ENTRIES(L2_MAG_ENTRIES), .STAGE_AT_PORT(1)) u_mag (
        .clk(axi_aclk), .resetn(rstn_mag),
        .dram_aclk(dram_aclk), .dram_aresetn(dram_aresetn),
        .sm_awid(S_AXI_MEM_awid), .sm_awaddr(S_AXI_MEM_awaddr),
        .sm_awlen(S_AXI_MEM_awlen), .sm_awvalid(S_AXI_MEM_awvalid),
        .sm_awready(S_AXI_MEM_awready),
        .sm_wdata(S_AXI_MEM_wdata), .sm_wstrb(S_AXI_MEM_wstrb),
        .sm_wlast(S_AXI_MEM_wlast), .sm_wvalid(S_AXI_MEM_wvalid),
        .sm_wready(S_AXI_MEM_wready),
        .sm_bid(S_AXI_MEM_bid), .sm_bresp(S_AXI_MEM_bresp),
        .sm_bvalid(S_AXI_MEM_bvalid), .sm_bready(S_AXI_MEM_bready),
        .sm_arid(S_AXI_MEM_arid), .sm_araddr(S_AXI_MEM_araddr),
        .sm_arlen(S_AXI_MEM_arlen), .sm_arvalid(S_AXI_MEM_arvalid),
        .sm_arready(S_AXI_MEM_arready),
        .sm_rid(S_AXI_MEM_rid), .sm_rdata(S_AXI_MEM_rdata),
        .sm_rresp(S_AXI_MEM_rresp), .sm_rlast(S_AXI_MEM_rlast),
        .sm_rvalid(S_AXI_MEM_rvalid), .sm_rready(S_AXI_MEM_rready),
        .sc_awid(S_AXI_CTRL_awid), .sc_awaddr(S_AXI_CTRL_awaddr),
        .sc_awlen(S_AXI_CTRL_awlen), .sc_awvalid(S_AXI_CTRL_awvalid),
        .sc_awready(S_AXI_CTRL_awready),
        .sc_wdata(S_AXI_CTRL_wdata), .sc_wstrb(S_AXI_CTRL_wstrb),
        .sc_wlast(S_AXI_CTRL_wlast), .sc_wvalid(S_AXI_CTRL_wvalid),
        .sc_wready(S_AXI_CTRL_wready),
        .sc_bid(S_AXI_CTRL_bid), .sc_bresp(S_AXI_CTRL_bresp),
        .sc_bvalid(S_AXI_CTRL_bvalid), .sc_bready(S_AXI_CTRL_bready),
        .sc_arid(S_AXI_CTRL_arid), .sc_araddr(S_AXI_CTRL_araddr),
        .sc_arlen(S_AXI_CTRL_arlen), .sc_arvalid(S_AXI_CTRL_arvalid),
        .sc_arready(S_AXI_CTRL_arready),
        .sc_rid(S_AXI_CTRL_rid), .sc_rdata(S_AXI_CTRL_rdata),
        .sc_rresp(S_AXI_CTRL_rresp), .sc_rlast(S_AXI_CTRL_rlast),
        .sc_rvalid(S_AXI_CTRL_rvalid), .sc_rready(S_AXI_CTRL_rready),
        .dram_awid(M_AXI_DRAM_awid), .dram_awaddr(M_AXI_DRAM_awaddr),
        .dram_awlen(M_AXI_DRAM_awlen), .dram_awsize(M_AXI_DRAM_awsize),
        .dram_awburst(M_AXI_DRAM_awburst), .dram_awvalid(M_AXI_DRAM_awvalid),
        .dram_awready(M_AXI_DRAM_awready),
        .dram_wdata(M_AXI_DRAM_wdata), .dram_wstrb(M_AXI_DRAM_wstrb),
        .dram_wlast(M_AXI_DRAM_wlast), .dram_wvalid(M_AXI_DRAM_wvalid),
        .dram_wready(M_AXI_DRAM_wready),
        .dram_bid(M_AXI_DRAM_bid), .dram_bresp(M_AXI_DRAM_bresp),
        .dram_bvalid(M_AXI_DRAM_bvalid), .dram_bready(M_AXI_DRAM_bready),
        .dram_arid(M_AXI_DRAM_arid), .dram_araddr(M_AXI_DRAM_araddr),
        .dram_arlen(M_AXI_DRAM_arlen), .dram_arsize(M_AXI_DRAM_arsize),
        .dram_arburst(M_AXI_DRAM_arburst), .dram_arvalid(M_AXI_DRAM_arvalid),
        .dram_arready(M_AXI_DRAM_arready),
        .dram_rid(M_AXI_DRAM_rid), .dram_rdata(M_AXI_DRAM_rdata),
        .dram_rresp(M_AXI_DRAM_rresp), .dram_rlast(M_AXI_DRAM_rlast),
        .dram_rvalid(M_AXI_DRAM_rvalid), .dram_rready(M_AXI_DRAM_rready),
        .mem_in_data({PORTS*FW{1'b0}}), .mem_in_valid({PORTS{1'b0}}),
        .mem_in_busy(mem_in_busy_unused),
        .mem_out_data(mem_out_data_unused), .mem_out_valid(mem_out_valid_unused),
        .mem_out_busy({PORTS{1'b0}}),
        .mem_rd_count(mem_rd_unused), .mem_wr_count(mem_wr_unused),
        .mv_busy(mv_busy_unused), .mv_fault(mv_fault_unused), .mv_done(mv_done_unused),
        .pe_halt_req(1'b0), .pe_status(pe_status_unused), .pe_busy(pe_busy_unused),
        .hs_addr(32'd0), .hs_wr(1'b0), .hs_wdata(64'd0), .hs_wstrb(8'd0), .hs_rd(1'b0),
        .hs_rdata(hs_rdata_unused), .hs_console_we(hs_console_we_unused),
        .hs_console(hs_console_unused),
        .link0_out_valid(l0_v_unused), .link0_out_vc(l0_vc_unused),
        .link0_out_last(l0_l_unused), .link0_out_flit(l0_flit_unused),
        .link0_out_crd_valid(1'b0), .link0_out_crd_vc(1'b0),
        .link0_out_crd_n(4'd0),
        .link0_in_valid(1'b0), .link0_in_vc(1'b0), .link0_in_last(1'b0),
        .link0_in_flit({LKW{1'b0}}), .link0_in_crd_valid(l0_cv_unused),
        .link0_in_crd_vc(l0_cvc_unused), .link0_in_crd_n(l0_crdn_unused),
        .link1_out_valid(l1_v_unused), .link1_out_vc(l1_vc_unused),
        .link1_out_last(l1_l_unused), .link1_out_flit(l1_flit_unused),
        .link1_out_crd_valid(1'b0), .link1_out_crd_vc(1'b0),
        .link1_out_crd_n(4'd0),
        .link1_in_valid(1'b0), .link1_in_vc(1'b0), .link1_in_last(1'b0),
        .link1_in_flit({LKW{1'b0}}), .link1_in_crd_valid(l1_cv_unused),
        .link1_in_crd_vc(l1_cvc_unused), .link1_in_crd_n(l1_crdn_unused)
    );
endmodule

`default_nettype wire
