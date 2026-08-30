// sysnode -- THE system node, and one component rather than an assembly.
//
// MAG and the control processor are a division of DESIGN, not of component.
// MAG is memory access and cross-mesh communication; the processor is command
// dispatch, small compute, and the memory mover with its transform slot.
// NEITHER SHIPS ALONE and neither is separable: MAG on its own cannot start
// work without a host round trip, and the processor on its own cannot reach
// memory or another mesh. There is no parameter that removes either.
//
//        MAG      ─┐
//        ctrl PE  ─┴─ sn_hub ─── PORTS attachments
//
// The hub owns every fabric port; nothing inside owns one. `PORTS` is the only
// thing that varies, and the node presents exactly that many.
//
// The processor answers at (0,0) -- a corner, which touches no router and which
// gen_mesh forbids any map from filling, so the coordinate is free by
// construction in every mesh of every shape. The host reaches it and another
// mesh's processor reaches it over the interlink; an on-mesh unit cannot
// address it, because a unit names memory by descriptor and never by node.
//
// STANDALONE-WORKABLE. A system node with no compute units on its mesh is
// still a machine: it has a processor, instruction memory, a scratchpad, an L1
// onto DRAM, and the mover. `tests/sysnode/` runs programs on exactly that.

`default_nettype none

module sysnode #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer DATA_W     = 256,
    parameter integer ADDR_W     = 40,
    parameter integer ID_W       = 4,
    // The node's attachment count. The one shape knob.
    parameter integer PORTS      = 1,
    parameter integer ILINK      = 0,
    parameter integer MESH_ID    = 0,
    parameter integer LINK_W     = 288,
    parameter integer TUSER_W    = 96,
    parameter integer MW         = DATA_W,
    // 0: dram_aclk IS clk and the DRAM port's queues are synchronous.
    parameter integer DRAM_CDC   = 1,
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
    // ---- the control processor. NOT OPTIONAL: there is no CTRL_PE. ----
    // CPU_RV64 swaps it for the RV64 complex: same mover, same transform slot,
    // different processor. It has no NoC compute-unit shell (decisions.md D1),
    // so the host reaches it through `hs_*` instead of CU_DATA/CU_INST.
    parameter integer CPU_RV64   = 0,
    parameter integer PE_IMEM    = 2048,
    parameter integer PE_SPAD    = 2048,
    parameter integer PE_L1_LINES = 128,
    parameter         PE_MEM_PRIM = "block",
    // ---- the transform slot, the mover's extension ----
    parameter integer XFORM_SLOTS     = 1,
    parameter integer XID_W           = 4,
    parameter integer XMODE_W         = 4,
    parameter integer XFORM_IN_BITS   = 2048,
    parameter integer XFORM_OUT_WORDS = 4
)(
    input  wire                clk,
    input  wire                resetn,

    // ---- the host's two windows ----
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

    // ---- THE NODE'S ATTACHMENTS. The only ones it has. ----
    input  wire [PORTS*FLIT_WIDTH-1:0] mem_in_data,
    input  wire [PORTS-1:0]            mem_in_valid,
    output wire [PORTS-1:0]            mem_in_busy,
    output wire [PORTS*FLIT_WIDTH-1:0] mem_out_data,
    output wire [PORTS-1:0]            mem_out_valid,
    input  wire [PORTS-1:0]            mem_out_busy,

    output wire [15:0]           mem_rd_count,
    output wire [15:0]           mem_wr_count,
    output wire                  mv_busy,
    output wire [3:0]            mv_fault,
    output wire [31:0]           mv_done,
    input  wire                  pe_halt_req,
    output wire [63:0]           pe_status,
    output wire                  pe_busy,

    // The RV64 processor's host window. Unused when CPU_RV64 = 0.
    input  wire [31:0]           hs_addr,
    input  wire                  hs_wr,
    input  wire [63:0]           hs_wdata,
    input  wire [7:0]            hs_wstrb,
    input  wire                  hs_rd,
    output wire [63:0]           hs_rdata,
    output wire                  hs_console_we,
    output wire [7:0]            hs_console,

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
    // ---- hub <-> MAG's engines ----
    wire [PORTS-1:0]            eng_rx_valid, eng_rx_busy;
    wire [PORTS*FLIT_WIDTH-1:0] eng_tx_data;
    wire [PORTS-1:0]            eng_tx_valid, eng_tx_busy;
    wire [PORTS*POS_WIDTH-1:0]  port_y;

    // ---- hub <-> MAG's agent ----
    wire [FLIT_WIDTH-1:0] agt_rx_data, agt_tx_data;
    wire                  agt_rx_valid, agt_rx_busy;
    wire                  agt_tx_valid, agt_tx_busy;

    // ---- hub <-> MAG's interlink ----
    wire [FLIT_WIDTH-1:0] enc_data, inj_data;
    wire                  enc_valid, enc_busy, inj_valid, inj_busy;
    wire                  bad_remote_req;
    wire [1:0]            my_mesh;

    // ---- hub <-> the control processor ----
    wire [FLIT_WIDTH-1:0] pe_rx_data, pe_tx_data;
    wire                  pe_rx_valid, pe_rx_busy;
    wire                  pe_tx_valid, pe_tx_busy;

    // ---- the processor's two channels into MAG ----
    wire [ADDR_W-1:0]   cp_awaddr, cp_araddr;
    wire [7:0]          cp_awlen, cp_arlen;
    wire                cp_awvalid, cp_awready, cp_wvalid, cp_wready, cp_wlast;
    wire [DATA_W-1:0]   cp_wdata, cp_rdata;
    wire [DATA_W/8-1:0] cp_wstrb;
    wire                cp_bvalid, cp_bready, cp_arvalid, cp_arready;
    wire                cp_rvalid, cp_rlast, cp_rready;

    wire [ID_W-1:0]     mv_awid, mv_arid, mv_bid, mv_rid;
    wire [ADDR_W-1:0]   mv_awaddr, mv_araddr;
    wire [7:0]          mv_awlen, mv_arlen;
    wire [2:0]          mv_awsize, mv_arsize;
    wire [1:0]          mv_awburst, mv_arburst, mv_bresp, mv_rresp;
    wire                mv_awvalid, mv_awready, mv_arvalid, mv_arready;
    wire [DATA_W-1:0]   mv_wdata, mv_rdata;
    wire [DATA_W/8-1:0] mv_wstrb;
    wire                mv_wlast, mv_wvalid, mv_wready;
    wire                mv_bvalid, mv_bready, mv_rlast, mv_rvalid, mv_rready;

    wire        aux_cfg_en;
    wire [7:0]  aux_cfg_addr;
    wire [63:0] aux_cfg_data;

    // The control processor's own path into the interlink. Only the RV64
    // complex drives it; the RV32 one has no such window and ties it off.
    wire        cpu_il_en;
    wire [7:0]  cpu_il_addr;
    wire [63:0] cpu_il_data;
    wire [63:0] cpu_dbell_counts;

    sn_hub #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .PORTS(PORTS), .ILINK(ILINK),
        .MEM_Y(MEM_Y), .MEM_Y1(MEM_Y1), .MEM_Y2(MEM_Y2), .MEM_Y3(MEM_Y3)
    ) u_hub (
        .clk(clk), .resetn(resetn),
        .mem_in_data(mem_in_data), .mem_in_valid(mem_in_valid),
        .mem_in_busy(mem_in_busy),
        .mem_out_data(mem_out_data), .mem_out_valid(mem_out_valid),
        .mem_out_busy(mem_out_busy),
        .my_mesh(my_mesh),
        .eng_rx_valid(eng_rx_valid), .eng_rx_busy(eng_rx_busy),
        .eng_tx_data(eng_tx_data), .eng_tx_valid(eng_tx_valid),
        .eng_tx_busy(eng_tx_busy),
        .agt_rx_data(agt_rx_data), .agt_rx_valid(agt_rx_valid),
        .agt_rx_busy(agt_rx_busy),
        .agt_tx_data(agt_tx_data), .agt_tx_valid(agt_tx_valid),
        .agt_tx_busy(agt_tx_busy),
        .pe_rx_data(pe_rx_data), .pe_rx_valid(pe_rx_valid),
        .pe_rx_busy(pe_rx_busy),
        .pe_tx_data(pe_tx_data), .pe_tx_valid(pe_tx_valid),
        .pe_tx_busy(pe_tx_busy),
        .enc_data(enc_data), .enc_valid(enc_valid), .enc_busy(enc_busy),
        .inj_data(inj_data), .inj_valid(inj_valid), .inj_busy(inj_busy),
        .bad_remote_req(bad_remote_req)
    );

    mag #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH), .DATA_W(DATA_W),
        .ADDR_W(ADDR_W), .ID_W(ID_W), .PORTS(PORTS), .ILINK(ILINK),
        .MESH_ID(MESH_ID), .LINK_W(LINK_W), .TUSER_W(TUSER_W), .MW(MW),
        .DRAM_CDC(DRAM_CDC),
        .MEM_X(MEM_X), .MEM_Y(MEM_Y), .MEM_X1(MEM_X1), .MEM_Y1(MEM_Y1),
        .MEM_X2(MEM_X2), .MEM_Y2(MEM_Y2), .MEM_X3(MEM_X3), .MEM_Y3(MEM_Y3),
        .GRID_LO(GRID_LO), .GRID_HI(GRID_HI), .STAGE_FLITS(STAGE_FLITS),
        .WR_SLOTS(WR_SLOTS), .STAGE(STAGE), .STAGE_BANKS(STAGE_BANKS),
        .STAGE_ENTRIES(STAGE_ENTRIES), .STAGE_PIPE(STAGE_PIPE),
        .STAGE_AT_PORT(STAGE_AT_PORT)
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
        .hub_data(mem_in_data),
        .eng_rx_valid(eng_rx_valid), .eng_rx_busy(eng_rx_busy),
        .eng_tx_data(eng_tx_data), .eng_tx_valid(eng_tx_valid),
        .eng_tx_busy(eng_tx_busy), .port_y(port_y),
        .agt_rx_data(agt_rx_data), .agt_rx_valid(agt_rx_valid),
        .agt_rx_busy(agt_rx_busy),
        .agt_tx_data(agt_tx_data), .agt_tx_valid(agt_tx_valid),
        .agt_tx_busy(agt_tx_busy),
        .enc_data(enc_data), .enc_valid(enc_valid), .enc_busy(enc_busy),
        .inj_data(inj_data), .inj_valid(inj_valid), .inj_busy(inj_busy),
        .bad_remote_req(bad_remote_req), .my_mesh(my_mesh),
        .mem_rd_count(mem_rd_count), .mem_wr_count(mem_wr_count),
        .mv_awid(mv_awid), .mv_awaddr(mv_awaddr), .mv_awlen(mv_awlen),
        .mv_awsize(mv_awsize), .mv_awburst(mv_awburst),
        .mv_awvalid(mv_awvalid), .mv_awready(mv_awready),
        .mv_wdata(mv_wdata), .mv_wstrb(mv_wstrb), .mv_wlast(mv_wlast),
        .mv_wvalid(mv_wvalid), .mv_wready(mv_wready),
        .mv_bid(mv_bid), .mv_bresp(mv_bresp), .mv_bvalid(mv_bvalid),
        .mv_bready(mv_bready),
        .mv_arid(mv_arid), .mv_araddr(mv_araddr), .mv_arlen(mv_arlen),
        .mv_arsize(mv_arsize), .mv_arburst(mv_arburst),
        .mv_arvalid(mv_arvalid), .mv_arready(mv_arready),
        .mv_rid(mv_rid), .mv_rdata(mv_rdata), .mv_rresp(mv_rresp),
        .mv_rlast(mv_rlast), .mv_rvalid(mv_rvalid), .mv_rready(mv_rready),
        .aux_cfg_en(aux_cfg_en), .aux_cfg_addr(aux_cfg_addr),
        .aux_cfg_data(aux_cfg_data),
        .cpu_il_en(cpu_il_en), .cpu_il_addr(cpu_il_addr),
        .cpu_il_data(cpu_il_data), .cpu_dbell_counts(cpu_dbell_counts),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done),
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
if (CPU_RV64 != 0) begin : g_rv64
    // A LEVEL, NOT AN EDGE: an inbound doorbell stays pending until the
    // handler clears the counts through the config window, so a ring taken
    // while another is being serviced is not lost. Registered: 64 inputs.
    reg dbell_pend;
    always @(posedge clk) begin
        dbell_pend <= |cpu_dbell_counts;
    end

    // No CU shell, but the hub port is a client all the same: the processor
    // dispatches through a control-region mailbox instead of a kick-and-report
    // lifecycle, which is what lets a runtime command units it did not start.
    rv64_mag_pe #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W),
        .IMEM_WORDS(PE_IMEM), .SPAD_WORDS(PE_SPAD),
        .L1_LINES(PE_L1_LINES), .MEM_PRIM(PE_MEM_PRIM),
        .XFORM_SLOTS(XFORM_SLOTS), .XID_W(XID_W), .XMODE_W(XMODE_W),
        .XFORM_IN_BITS(XFORM_IN_BITS), .XFORM_OUT_WORDS(XFORM_OUT_WORDS)
    ) u_pe (
        .clk(clk), .resetn(resetn),
        .hs_addr(hs_addr), .hs_wr(hs_wr), .hs_wdata(hs_wdata),
        .hs_wstrb(hs_wstrb), .hs_rd(hs_rd), .hs_rdata(hs_rdata),
        .hs_ready(),
        // (0,0), the same corner `sn_hub` decodes the PE at.
        .my_x({POS_WIDTH{1'b0}}), .my_y({POS_WIDTH{1'b0}}),
        .noc_in_data(pe_rx_data), .noc_in_valid(pe_rx_valid),
        .noc_in_busy(pe_rx_busy),
        .noc_out_data(pe_tx_data), .noc_out_valid(pe_tx_valid),
        .noc_out_busy(pe_tx_busy),
        .cp_awaddr(cp_awaddr), .cp_awlen(cp_awlen),
        .cp_awvalid(cp_awvalid), .cp_awready(cp_awready),
        .cp_wdata(cp_wdata), .cp_wstrb(cp_wstrb), .cp_wlast(cp_wlast),
        .cp_wvalid(cp_wvalid), .cp_wready(cp_wready),
        .cp_bvalid(cp_bvalid), .cp_bready(cp_bready),
        .cp_araddr(cp_araddr), .cp_arlen(cp_arlen),
        .cp_arvalid(cp_arvalid), .cp_arready(cp_arready),
        .cp_rdata(cp_rdata), .cp_rlast(cp_rlast), .cp_rvalid(cp_rvalid),
        .cp_rready(cp_rready),
        .mv_awid(mv_awid), .mv_awaddr(mv_awaddr), .mv_awlen(mv_awlen),
        .mv_awsize(mv_awsize), .mv_awburst(mv_awburst),
        .mv_awvalid(mv_awvalid), .mv_awready(mv_awready),
        .mv_wdata(mv_wdata), .mv_wstrb(mv_wstrb), .mv_wlast(mv_wlast),
        .mv_wvalid(mv_wvalid), .mv_wready(mv_wready),
        .mv_bid(mv_bid), .mv_bresp(mv_bresp), .mv_bvalid(mv_bvalid),
        .mv_bready(mv_bready),
        .mv_arid(mv_arid), .mv_araddr(mv_araddr), .mv_arlen(mv_arlen),
        .mv_arsize(mv_arsize), .mv_arburst(mv_arburst),
        .mv_arvalid(mv_arvalid), .mv_arready(mv_arready),
        .mv_rid(mv_rid), .mv_rdata(mv_rdata), .mv_rresp(mv_rresp),
        .mv_rlast(mv_rlast), .mv_rvalid(mv_rvalid), .mv_rready(mv_rready),
        .aux_cfg_en(aux_cfg_en), .aux_cfg_addr(aux_cfg_addr),
        .aux_cfg_data(aux_cfg_data), .ilink_on(ILINK != 0),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done),
        // The processor's own reach into the interlink: it rings a doorbell in
        // another mesh through the config window, and reads the four inbound
        // counts back. Left dangling this was a control region that answered
        // writes and changed nothing.
        .db_status(cpu_dbell_counts),
        .db_en(cpu_il_en), .db_addr(cpu_il_addr), .db_data(cpu_il_data),
        // The node conditions a runtime must react to rather than poll: a
        // mover descriptor that failed, the host asking it to stop, and a
        // doorbell rung from another mesh. Tied low this line existed but
        // could never fire, so `mie[11]` was dead.
        .irq_summary((|mv_fault) || pe_halt_req || dbell_pend), .busy(pe_busy),
        .dbg_console_we(hs_console_we), .dbg_console(hs_console)
    );
    // Enough for a host to tell a running node from a stopped one without the
    // control window. The RV32 branch reports its own; this mirrors the shape.
    assign pe_status = {62'd0, |mv_fault, pe_busy};
end
else begin : g_rv32
    assign hs_rdata     = 64'd0;
    assign hs_console_we = 1'b0;
    assign hs_console    = 8'd0;
    assign cpu_il_en    = 1'b0;
    assign cpu_il_addr  = 8'd0;
    assign cpu_il_data  = 64'd0;

    rv_mag_pe #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W),
        .IMEM_WORDS(PE_IMEM), .SPAD_WORDS(PE_SPAD),
        .L1_LINES(PE_L1_LINES), .MEM_PRIM(PE_MEM_PRIM),
        .XFORM_SLOTS(XFORM_SLOTS), .XID_W(XID_W), .XMODE_W(XMODE_W),
        .XFORM_IN_BITS(XFORM_IN_BITS), .XFORM_OUT_WORDS(XFORM_OUT_WORDS)
    ) u_pe (
        .clk(clk), .resetn(resetn),
        .noc_in_data(pe_rx_data), .noc_in_valid(pe_rx_valid),
        .noc_in_busy(pe_rx_busy),
        .noc_out_data(pe_tx_data), .noc_out_valid(pe_tx_valid),
        .noc_out_busy(pe_tx_busy),
        .cp_awaddr(cp_awaddr), .cp_awlen(cp_awlen),
        .cp_awvalid(cp_awvalid), .cp_awready(cp_awready),
        .cp_wdata(cp_wdata), .cp_wstrb(cp_wstrb), .cp_wlast(cp_wlast),
        .cp_wvalid(cp_wvalid), .cp_wready(cp_wready),
        .cp_bvalid(cp_bvalid), .cp_bready(cp_bready),
        .cp_araddr(cp_araddr), .cp_arlen(cp_arlen),
        .cp_arvalid(cp_arvalid), .cp_arready(cp_arready),
        .cp_rdata(cp_rdata), .cp_rlast(cp_rlast), .cp_rvalid(cp_rvalid),
        .cp_rready(cp_rready),
        .mv_awid(mv_awid), .mv_awaddr(mv_awaddr), .mv_awlen(mv_awlen),
        .mv_awsize(mv_awsize), .mv_awburst(mv_awburst),
        .mv_awvalid(mv_awvalid), .mv_awready(mv_awready),
        .mv_wdata(mv_wdata), .mv_wstrb(mv_wstrb), .mv_wlast(mv_wlast),
        .mv_wvalid(mv_wvalid), .mv_wready(mv_wready),
        .mv_bid(mv_bid), .mv_bresp(mv_bresp), .mv_bvalid(mv_bvalid),
        .mv_bready(mv_bready),
        .mv_arid(mv_arid), .mv_araddr(mv_araddr), .mv_arlen(mv_arlen),
        .mv_arsize(mv_arsize), .mv_arburst(mv_arburst),
        .mv_arvalid(mv_arvalid), .mv_arready(mv_arready),
        .mv_rid(mv_rid), .mv_rdata(mv_rdata), .mv_rresp(mv_rresp),
        .mv_rlast(mv_rlast), .mv_rvalid(mv_rvalid), .mv_rready(mv_rready),
        .aux_cfg_en(aux_cfg_en), .aux_cfg_addr(aux_cfg_addr),
        .aux_cfg_data(aux_cfg_data), .ilink_on(ILINK != 0),
        .mv_busy(mv_busy), .mv_fault(mv_fault), .mv_done(mv_done),
        .halt_req(pe_halt_req), .pe_status(pe_status), .busy(pe_busy)
    );
end
endgenerate
endmodule

`default_nettype wire
