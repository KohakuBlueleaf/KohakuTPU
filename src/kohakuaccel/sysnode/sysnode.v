// node -- the SYSTEM NODE: the memory gateway, the mover, and the control
// processor that owns both.
//
// `mag` stays what its name says -- the memory gateway. This is the enclosure
// the owner named: MAG + CPU + mover. At CTRL_PE=0 the processor is not
// generated and this module is `mag` with its ports passed through, so the
// shipping bitstream is unchanged.
//
// The processor is a COMPUTE UNIT from outside: its own NoC port, its own
// coordinate, loaded and kicked like any other. What it gains by living here is
// a WIRE to the mover's config port and a requester channel straight onto the
// converged path, neither of which a unit at a mesh port can have.

`default_nettype none

module sysnode #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer DATA_W     = 256,
    parameter integer ADDR_W     = 40,
    parameter integer ID_W       = 4,
    parameter integer MEM_PORTS  = 1,
    parameter integer ILINK      = 0,
    parameter integer MESH_ID    = 0,
    parameter integer LINK_W     = 288,
    parameter integer TUSER_W    = 96,
    parameter integer MW         = DATA_W,
    parameter integer MEM_X      = 0,
    parameter integer MEM_Y      = 1,
    parameter integer MEM_X1     = 0,
    parameter integer MEM_Y1     = 3,
    parameter integer MEM_X2     = 0,
    parameter integer MEM_Y2     = 4,
    parameter integer MEM_X3     = 0,
    parameter integer MEM_Y3     = 5,
    parameter integer GRID_LO    = 1,
    parameter integer GRID_HI    = 2,
    parameter integer STAGE_FLITS = 128,
    parameter integer WR_SLOTS   = 16,
    parameter integer STAGE        = 0,
    parameter integer STAGE_BANKS  = 4,
    parameter integer STAGE_ENTRIES = 16384,
    parameter integer STAGE_PIPE  = 1,
    parameter integer STAGE_AT_PORT = 0,
    // ---- the control processor. 0 generates none of it. ----
    parameter integer CTRL_PE    = 0,
    parameter integer PE_X       = 1,
    parameter integer PE_Y       = 0,
    parameter integer PE_IMEM    = 2048,
    parameter integer PE_SPAD    = 2048,
    parameter integer PE_L1_LINES = 128,
    parameter         PE_MEM_PRIM = "block"
)(
    input  wire                clk,
    input  wire                resetn,

    // ---- the host's two windows, unchanged ----
    input  wire [ID_W-1:0]     sm_awid,
    input  wire [ADDR_W-1:0]   sm_awaddr,
    input  wire [7:0]          sm_awlen,
    input  wire                sm_awvalid,
    output wire                sm_awready,
    input  wire [DATA_W-1:0]   sm_wdata,
    input  wire [DATA_W/8-1:0] sm_wstrb,
    input  wire                sm_wlast,
    input  wire                sm_wvalid,
    output wire                sm_wready,
    output wire [ID_W-1:0]     sm_bid,
    output wire [1:0]          sm_bresp,
    output wire                sm_bvalid,
    input  wire                sm_bready,
    input  wire [ID_W-1:0]     sm_arid,
    input  wire [ADDR_W-1:0]   sm_araddr,
    input  wire [7:0]          sm_arlen,
    input  wire                sm_arvalid,
    output wire                sm_arready,
    output wire [ID_W-1:0]     sm_rid,
    output wire [DATA_W-1:0]   sm_rdata,
    output wire [1:0]          sm_rresp,
    output wire                sm_rlast,
    output wire                sm_rvalid,
    input  wire                sm_rready,

    input  wire [ID_W-1:0]     sc_awid,
    input  wire [31:0]         sc_awaddr,
    input  wire [7:0]          sc_awlen,
    input  wire                sc_awvalid,
    output wire                sc_awready,
    input  wire [63:0]         sc_wdata,
    input  wire [7:0]          sc_wstrb,
    input  wire                sc_wlast,
    input  wire                sc_wvalid,
    output wire                sc_wready,
    output wire [ID_W-1:0]     sc_bid,
    output wire [1:0]          sc_bresp,
    output wire                sc_bvalid,
    input  wire                sc_bready,
    input  wire [ID_W-1:0]     sc_arid,
    input  wire [31:0]         sc_araddr,
    input  wire [7:0]          sc_arlen,
    input  wire                sc_arvalid,
    output wire                sc_arready,
    output wire [ID_W-1:0]     sc_rid,
    output wire [63:0]         sc_rdata,
    output wire [1:0]          sc_rresp,
    output wire                sc_rlast,
    output wire                sc_rvalid,
    input  wire                sc_rready,

    input  wire                    dram_aclk,
    input  wire                    dram_aresetn,
    output wire [ID_W-1:0]         dram_awid,
    output wire [ADDR_W-1:0]       dram_awaddr,
    output wire [7:0]              dram_awlen,
    output wire [2:0]              dram_awsize,
    output wire [1:0]              dram_awburst,
    output wire                    dram_awvalid,
    input  wire                    dram_awready,
    output wire [MW-1:0]           dram_wdata,
    output wire [MW/8-1:0]         dram_wstrb,
    output wire                    dram_wlast,
    output wire                    dram_wvalid,
    input  wire                    dram_wready,
    input  wire [ID_W-1:0]         dram_bid,
    input  wire [1:0]              dram_bresp,
    input  wire                    dram_bvalid,
    output wire                    dram_bready,
    output wire [ID_W-1:0]         dram_arid,
    output wire [ADDR_W-1:0]       dram_araddr,
    output wire [7:0]              dram_arlen,
    output wire [2:0]              dram_arsize,
    output wire [1:0]              dram_arburst,
    output wire                    dram_arvalid,
    input  wire                    dram_arready,
    input  wire [ID_W-1:0]         dram_rid,
    input  wire [MW-1:0]           dram_rdata,
    input  wire [1:0]              dram_rresp,
    input  wire                    dram_rlast,
    input  wire                    dram_rvalid,
    output wire                    dram_rready,

    // ---- MAG's mesh attachments ----
    input  wire [MEM_PORTS*FLIT_WIDTH-1:0] mem_in_data,
    input  wire [MEM_PORTS-1:0]            mem_in_valid,
    output wire [MEM_PORTS-1:0]            mem_in_busy,
    output wire [MEM_PORTS*FLIT_WIDTH-1:0] mem_out_data,
    output wire [MEM_PORTS-1:0]            mem_out_valid,
    input  wire [MEM_PORTS-1:0]            mem_out_busy,

    // ---- the processor's OWN mesh port. Tied off at CTRL_PE=0. ----
    input  wire [FLIT_WIDTH-1:0]  pe_in_data,
    input  wire                   pe_in_valid,
    output wire                   pe_in_busy,
    output wire [FLIT_WIDTH-1:0]  pe_out_data,
    output wire                   pe_out_valid,
    input  wire                   pe_out_busy,

    output wire [15:0]           mem_rd_count,
    output wire [15:0]           mem_wr_count,
    output wire                  mv_busy,
    output wire [3:0]            mv_fault,
    output wire [31:0]           mv_done,
    // One 64-bit read tells the host what the processor is doing, so polling
    // costs no flit. Zero when the processor is not generated.
    input  wire                  pe_halt_req,
    output wire [63:0]           pe_status,
    output wire                  pe_busy,

    output wire [LINK_W-1:0]     link0_out_tdata,
    output wire [TUSER_W-1:0]    link0_out_tuser,
    output wire                  link0_out_tlast,
    output wire                  link0_out_tvalid,
    input  wire                  link0_out_tready,
    input  wire [LINK_W-1:0]     link0_in_tdata,
    input  wire [TUSER_W-1:0]    link0_in_tuser,
    input  wire                  link0_in_tlast,
    input  wire                  link0_in_tvalid,
    output wire                  link0_in_tready,

    output wire [LINK_W-1:0]     link1_out_tdata,
    output wire [TUSER_W-1:0]    link1_out_tuser,
    output wire                  link1_out_tlast,
    output wire                  link1_out_tvalid,
    input  wire                  link1_out_tready,
    input  wire [LINK_W-1:0]     link1_in_tdata,
    input  wire [TUSER_W-1:0]    link1_in_tuser,
    input  wire                  link1_in_tlast,
    input  wire                  link1_in_tvalid,
    output wire                  link1_in_tready
);
    wire [ADDR_W-1:0]   cp_awaddr, cp_araddr;
    wire [7:0]          cp_awlen, cp_arlen;
    wire                cp_awvalid, cp_awready, cp_wvalid, cp_wready, cp_wlast;
    wire [DATA_W-1:0]   cp_wdata, cp_rdata;
    wire [DATA_W/8-1:0] cp_wstrb;
    wire                cp_bvalid, cp_bready, cp_arvalid, cp_arready;
    wire                cp_rvalid, cp_rlast, cp_rready;
    wire                pe_cfg_en;
    wire [7:0]          pe_cfg_addr;
    wire [63:0]         pe_cfg_data;
    wire                pe_xcfg_en;
    wire [3:0]          pe_xcfg_id;
    wire [7:0]          pe_xcfg_addr;
    wire [31:0]         pe_xcfg_data, pe_xcfg_rdata;
    wire [3:0]          xf_fault;

    mag #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH), .DATA_W(DATA_W),
        .ADDR_W(ADDR_W), .ID_W(ID_W), .MEM_PORTS(MEM_PORTS), .ILINK(ILINK),
        .MESH_ID(MESH_ID), .LINK_W(LINK_W), .TUSER_W(TUSER_W), .MW(MW),
        .MEM_X(MEM_X), .MEM_Y(MEM_Y), .MEM_X1(MEM_X1), .MEM_Y1(MEM_Y1),
        .MEM_X2(MEM_X2), .MEM_Y2(MEM_Y2), .MEM_X3(MEM_X3), .MEM_Y3(MEM_Y3),
        .GRID_LO(GRID_LO), .GRID_HI(GRID_HI), .STAGE_FLITS(STAGE_FLITS),
        .WR_SLOTS(WR_SLOTS), .STAGE(STAGE), .STAGE_BANKS(STAGE_BANKS),
        .STAGE_ENTRIES(STAGE_ENTRIES), .STAGE_PIPE(STAGE_PIPE),
        .STAGE_AT_PORT(STAGE_AT_PORT), .CTRL_PE(CTRL_PE)
    ) u_mag (
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
        .dram_aclk(dram_aclk), .dram_aresetn(dram_aresetn),
        .dram_awid(dram_awid), .dram_awaddr(dram_awaddr),
        .dram_awlen(dram_awlen), .dram_awsize(dram_awsize),
        .dram_awburst(dram_awburst), .dram_awvalid(dram_awvalid),
        .dram_awready(dram_awready),
        .dram_wdata(dram_wdata), .dram_wstrb(dram_wstrb),
        .dram_wlast(dram_wlast), .dram_wvalid(dram_wvalid),
        .dram_wready(dram_wready),
        .dram_bid(dram_bid), .dram_bresp(dram_bresp),
        .dram_bvalid(dram_bvalid), .dram_bready(dram_bready),
        .dram_arid(dram_arid), .dram_araddr(dram_araddr),
        .dram_arlen(dram_arlen), .dram_arsize(dram_arsize),
        .dram_arburst(dram_arburst), .dram_arvalid(dram_arvalid),
        .dram_arready(dram_arready),
        .dram_rid(dram_rid), .dram_rdata(dram_rdata), .dram_rresp(dram_rresp),
        .dram_rlast(dram_rlast), .dram_rvalid(dram_rvalid),
        .dram_rready(dram_rready),
        .mem_in_data(mem_in_data), .mem_in_valid(mem_in_valid),
        .mem_in_busy(mem_in_busy),
        .mem_out_data(mem_out_data), .mem_out_valid(mem_out_valid),
        .mem_out_busy(mem_out_busy),
        .mem_rd_count(mem_rd_count), .mem_wr_count(mem_wr_count),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done),
        .pe_cfg_en(pe_cfg_en), .pe_cfg_addr(pe_cfg_addr),
        .pe_cfg_data(pe_cfg_data),
        .pe_xcfg_en(pe_xcfg_en), .pe_xcfg_id(pe_xcfg_id),
        .pe_xcfg_addr(pe_xcfg_addr), .pe_xcfg_data(pe_xcfg_data),
        .pe_xcfg_rdata(pe_xcfg_rdata), .xf_fault(xf_fault),
        .cp_awaddr(cp_awaddr), .cp_awlen(cp_awlen), .cp_awvalid(cp_awvalid),
        .cp_awready(cp_awready),
        .cp_wdata(cp_wdata), .cp_wstrb(cp_wstrb), .cp_wlast(cp_wlast),
        .cp_wvalid(cp_wvalid), .cp_wready(cp_wready),
        .cp_bvalid(cp_bvalid), .cp_bready(cp_bready),
        .cp_araddr(cp_araddr), .cp_arlen(cp_arlen), .cp_arvalid(cp_arvalid),
        .cp_arready(cp_arready),
        .cp_rdata(cp_rdata), .cp_rlast(cp_rlast), .cp_rvalid(cp_rvalid),
        .cp_rready(cp_rready),
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

    generate
    if (CTRL_PE != 0) begin : g_pe
        rv_mag_pe #(
            .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
            .POS_X(PE_X), .POS_Y(PE_Y),
            .ADDR_W(ADDR_W), .DATA_W(DATA_W),
            .IMEM_WORDS(PE_IMEM), .SPAD_WORDS(PE_SPAD),
            .L1_LINES(PE_L1_LINES), .MEM_PRIM(PE_MEM_PRIM)
        ) u_pe (
            .clk(clk), .resetn(resetn),
            .noc_in_data(pe_in_data), .noc_in_valid(pe_in_valid),
            .noc_in_busy(pe_in_busy),
            .noc_out_data(pe_out_data), .noc_out_valid(pe_out_valid),
            .noc_out_busy(pe_out_busy),
            .cp_awaddr(cp_awaddr), .cp_awlen(cp_awlen),
            .cp_awvalid(cp_awvalid), .cp_awready(cp_awready),
            .cp_wdata(cp_wdata), .cp_wstrb(cp_wstrb), .cp_wlast(cp_wlast),
            .cp_wvalid(cp_wvalid), .cp_wready(cp_wready),
            .cp_bvalid(cp_bvalid), .cp_bready(cp_bready),
            .cp_araddr(cp_araddr), .cp_arlen(cp_arlen),
            .cp_arvalid(cp_arvalid), .cp_arready(cp_arready),
            .cp_rdata(cp_rdata), .cp_rlast(cp_rlast), .cp_rvalid(cp_rvalid),
            .cp_rready(cp_rready),
            .mv_cfg_en(pe_cfg_en), .mv_cfg_addr(pe_cfg_addr),
            .mv_cfg_data(pe_cfg_data),
            .mv_busy(mv_busy), .mv_fault(mv_fault),
            .xcfg_en(pe_xcfg_en), .xcfg_id(pe_xcfg_id),
            .xcfg_addr(pe_xcfg_addr), .xcfg_data(pe_xcfg_data),
            .xcfg_rdata(pe_xcfg_rdata), .xf_fault(xf_fault),
            .halt_req(pe_halt_req), .pe_status(pe_status), .busy(pe_busy)
        );
    end else begin : g_no_pe
        assign cp_awaddr  = {ADDR_W{1'b0}};
        assign cp_awlen   = 8'd0;
        assign cp_awvalid = 1'b0;
        assign cp_wdata   = {DATA_W{1'b0}};
        assign cp_wstrb   = {(DATA_W/8){1'b0}};
        assign cp_wlast   = 1'b0;
        assign cp_wvalid  = 1'b0;
        assign cp_bready  = 1'b1;
        assign cp_araddr  = {ADDR_W{1'b0}};
        assign cp_arlen   = 8'd0;
        assign cp_arvalid = 1'b0;
        assign cp_rready  = 1'b1;
        assign pe_cfg_en   = 1'b0;
        assign pe_cfg_addr = 8'd0;
        assign pe_cfg_data = 64'd0;
        assign pe_xcfg_en   = 1'b0;
        assign pe_xcfg_id   = 4'd0;
        assign pe_xcfg_addr = 8'd0;
        assign pe_xcfg_data = 32'd0;
        assign pe_in_busy  = 1'b0;
        assign pe_out_data  = {FLIT_WIDTH{1'b0}};
        assign pe_out_valid = 1'b0;
        assign pe_status    = 64'd0;
        assign pe_busy      = 1'b0;
    end
    endgenerate
endmodule

`default_nettype wire
