// MAG -- Memory Access Gateway. The memory and cross-mesh HALF of the system
// node, and half is meant literally: MAG has no fabric port and does not ship
// alone. `sysnode.v` composes it with the control processor behind `sn_hub`,
// and the hub owns every attachment. What MAG presents here are CLIENT streams
// into that hub, never a mesh port.
//
//   hub ──►  mag_mem_port x PORTS ──► AXI master 0..N ─┐
//   hub ◄──  agent (noc_orchestrator)                  ├──► memory
//   AXI slave, memory ──► upload ─────► AXI master N+1 ┤
//   ctrl PE's L1  ────────────────────► master CP      ┤
//   ctrl PE's mover ─────────────────► master MV       ┘
//
// THE MOVER AND THE TRANSFORM SLOT ARE NOT MAG'S. They belong to the control
// processor -- the mover is its SIMD memory unit and the slot is that unit's
// extension. MV arrives here as an ordinary external requester, which is all
// MAG ever knew about it.
//
// MAG is a SLAVE on the main interconnect, not a master; the memory moved one
// level down, behind an adapter. See docs/arch-design.md s6.4.
//
// SEVERAL MEMORY PORTS, AND THAT IS THE ARCHITECTURE RATHER THAN AN OPTION. A
// port serves ~2 clusters. A single read engine is what stopped the machine
// scaling -- and it stopped while nothing was saturated, so the constraint was
// the server, not the bandwidth. Each port owns its intake queues, read engine,
// write slots and AXI channel.
//
// The ports are placed at DIFFERENT mesh nodes on purpose. Routing is X-then-Y
// on clamped coordinates, so a port at (0,y) draws traffic to router (1,y) --
// putting two ports on one router splits the server and not the funnel.
//
// v1 SCOPE: no cache, no TLB. Addresses are physical, and the memory window is
// a straight offset into the attached RAM. Both are additive later and neither
// changes this interface -- docs/mas/spec.md s7a.

`default_nettype none

`ifndef KOHAKU_DRAM_RD_OUT
  `define KOHAKU_DRAM_RD_OUT 1
`endif

module mag #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer DATA_W     = 256,     // AXI memory width == flit payload
    parameter integer ADDR_W     = 40,
    parameter integer ID_W       = 4,
    // How many memory engines, one per hub port. Coordinates are named per
    // port rather than packed: a packed field is one shift away from pointing a
    // whole port at the wrong node, and it would elaborate cleanly.
    parameter integer PORTS      = 1,
    // The interlink, docs/interlink/. ZERO GENERATES NONE OF IT: no switch, no
    // links, no extra AXI master, and the remote address decode is a constant
    // false. The shipping bitstream is ILINK=0 and has to stay identical, so
    // every addition below is inside a generate or gated by this parameter.
    parameter integer ILINK      = 0,
    parameter integer MESH_ID    = 0,
    parameter integer LINK_W     = 288,
    parameter integer TUSER_W    = 96,
    parameter integer IL_RX_BEATS  = 64,
    parameter integer IL_MAX_BEATS = 32,
    // Requesters onto the one AXI master: the engines, the host upload, the
    // processor's mover, the processor's L1, and with the interlink the channel
    // inbound remote writes land through. The processor is not optional, so
    // neither is its pair -- there is no configuration in which this is smaller.
    parameter integer MP1        = PORTS + 3 + ((ILINK != 0) ? 1 : 0),
    // mag_dram_port packs DATA_W -> MW, so at 512 an 8-beat 256-bit burst
    // becomes 4 beats. Defaults EQUAL, which is the R=1 no-sub-beat case.
    parameter integer MW         = DATA_W,
    // DRAM reads one requester may hold in flight (mag_dram_port RD_OUT). The
    // macro lets a bench set it under a generated top whose parameters it
    // cannot reach; the default is the shipped value.
    parameter integer DRAM_RD_OUT = `KOHAKU_DRAM_RD_OUT,
    // 0: dram_aclk IS clk and the DRAM port's queues are synchronous.
    parameter integer DRAM_CDC   = 1,
    parameter integer MEM_X      = 0,       // port 0
    parameter integer MEM_Y      = 1,
    parameter integer MEM_X1     = 0,       // port 1
    parameter integer MEM_Y1     = 3,
    parameter integer MEM_X2     = 0,       // port 2
    parameter integer MEM_Y2     = 4,
    parameter integer MEM_X3     = 0,       // port 3
    parameter integer MEM_Y3     = 5,
    // AGT_X/AGT_Y are GONE. The agent shares the memory ports and answers at
    // port 0's coordinate; there is no separate node to place.
    parameter integer GRID_LO    = 1,
    parameter integer GRID_HI    = 2,
    parameter integer STAGE_FLITS = 128,
    parameter integer WR_SLOTS   = 16,
    // MAG L2 staging, special aperture 0. 0 generates none of it.
    parameter integer STAGE         = 0,
    parameter integer STAGE_BANKS   = 4,      // 64 URAM, 2 MB
    parameter integer STAGE_ENTRIES = 16384,
    parameter integer STAGE_PIPE    = 1,
    // 0 = a store inside each mag_mem_port, unreachable by mover and interlink
    // and costing PORTS x 64 URAM. 1 = one store on the converged path.
    parameter integer STAGE_AT_PORT = 0
)(
    input  wire                clk,
    input  wire                resetn,

    // ---- AXI4 slave: memory window (host upload / readback) --------------
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

    // ---- AXI4 slave: control window (64-bit, narrow by design) -----------
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

    // ---- ONE AXI4 master, at the DRAM's own width ------------------------
    // MP1 internal requesters converge in mag_dram_port; AXI exists once, here.
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

    // ---- hub client: the per-port memory engines --------------------------
    // The flit itself is broadcast from the hub; `eng_rx_valid` is the arm the
    // hub's demux already qualified, so an engine never learns the other
    // clients exist.
    input  wire [PORTS*FLIT_WIDTH-1:0] hub_data,
    input  wire [PORTS-1:0]            eng_rx_valid,
    output wire [PORTS-1:0]            eng_rx_busy,
    output wire [PORTS*FLIT_WIDTH-1:0] eng_tx_data,
    output wire [PORTS-1:0]            eng_tx_valid,
    input  wire [PORTS-1:0]            eng_tx_busy,
    output wire [PORTS*POS_WIDTH-1:0]  port_y,

    // ---- hub client: the control agent ------------------------------------
    input  wire [FLIT_WIDTH-1:0]       agt_rx_data,
    input  wire                        agt_rx_valid,
    output wire                        agt_rx_busy,
    output wire [FLIT_WIDTH-1:0]       agt_tx_data,
    output wire                        agt_tx_valid,
    input  wire                        agt_tx_busy,

    // ---- hub client: the interlink ----------------------------------------
    input  wire [FLIT_WIDTH-1:0]       enc_data,
    input  wire                        enc_valid,
    output wire                        enc_busy,
    output wire [FLIT_WIDTH-1:0]       inj_data,
    output wire                        inj_valid,
    input  wire                        inj_busy,
    input  wire                        bad_remote_req,
    output wire [1:0]                  my_mesh,

    output wire [15:0]           mem_rd_count,
    output wire [15:0]           mem_wr_count,

    // ---- the interlink, docs/interlink/topology.md s1 ---------------------
    // Present on the module at any ILINK because Verilog has no conditional
    // port. At ILINK=0 every output here is a constant and every input is
    // unread, so synthesis removes them and the generated top does not expose
    // them at all -- which is the form the block design sees.
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

    // ---- the control processor's mover, as requester MV -------------------
    // The mover lives in the processor now. MAG sees what it always saw: one
    // more AXI-shaped requester on the converged path. The interlink's address
    // splitter still sits on this channel's write side, which is why it arrives
    // here rather than going straight to the arbiter.
    input  wire [ID_W-1:0]       mv_awid,
    input  wire [ADDR_W-1:0]     mv_awaddr,
    input  wire [7:0]            mv_awlen,
    input  wire [2:0]            mv_awsize,
    input  wire [1:0]            mv_awburst,
    input  wire                  mv_awvalid,
    output wire                  mv_awready,
    input  wire [DATA_W-1:0]     mv_wdata,
    input  wire [DATA_W/8-1:0]   mv_wstrb,
    input  wire                  mv_wlast,
    input  wire                  mv_wvalid,
    output wire                  mv_wready,
    output wire [ID_W-1:0]       mv_bid,
    output wire [1:0]            mv_bresp,
    output wire                  mv_bvalid,
    input  wire                  mv_bready,
    input  wire [ID_W-1:0]       mv_arid,
    input  wire [ADDR_W-1:0]     mv_araddr,
    input  wire [7:0]            mv_arlen,
    input  wire [2:0]            mv_arsize,
    input  wire [1:0]            mv_arburst,
    input  wire                  mv_arvalid,
    output wire                  mv_arready,
    output wire [ID_W-1:0]       mv_rid,
    output wire [DATA_W-1:0]     mv_rdata,
    output wire [1:0]            mv_rresp,
    output wire                  mv_rlast,
    output wire                  mv_rvalid,
    input  wire                  mv_rready,

    // The host's AUX_CFG window, forwarded to whoever owns the register. The
    // mover's half leaves the node; the interlink's half stays here.
    output wire                  aux_cfg_en,
    output wire [7:0]            aux_cfg_addr,
    output wire [63:0]           aux_cfg_data,
    // The node's own processor reaching the interlink's config window, so it
    // can ring a doorbell without a host round trip. The host wins a same-cycle
    // collision: it is a debug path and the processor retries.
    input  wire                  cpu_il_en,
    input  wire [7:0]            cpu_il_addr,
    input  wire [63:0]           cpu_il_data,
    output wire [63:0]           cpu_dbell_counts,
    // Mirrored into AUX_STAT so the host reads liveness in one 64-bit load.
    input  wire                  mv_busy,
    input  wire [3:0]            mv_fault,
    input  wire [31:0]           mv_done,

    input  wire [ADDR_W-1:0]     cp_awaddr,
    input  wire [7:0]            cp_awlen,
    input  wire                  cp_awvalid,
    output wire                  cp_awready,
    input  wire [DATA_W-1:0]     cp_wdata,
    input  wire [DATA_W/8-1:0]   cp_wstrb,
    input  wire                  cp_wlast,
    input  wire                  cp_wvalid,
    output wire                  cp_wready,
    output wire                  cp_bvalid,
    input  wire                  cp_bready,
    input  wire [ADDR_W-1:0]     cp_araddr,
    input  wire [7:0]            cp_arlen,
    input  wire                  cp_arvalid,
    output wire                  cp_arready,
    output wire [DATA_W-1:0]     cp_rdata,
    output wire                  cp_rlast,
    output wire                  cp_rvalid,
    input  wire                  cp_rready,

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
    // The upload rides one channel past the engines, the mover one past that.
    localparam integer UP = PORTS;           // its channel index
    localparam integer MV = PORTS + 1;
    // Only reachable when ILINK is set; at 0 it aliases MV and nothing drives
    // it, which is why every use of LK is inside the same generate.
    localparam integer LK = (ILINK != 0) ? PORTS + 2 : PORTS + 1;
    localparam integer CP = PORTS + 2 + ((ILINK != 0) ? 1 : 0);

    localparam integer LSB = $clog2(DATA_W/8);

    // ---- the internal requesters, converged at the foot of this file -------
    wire [MP1*ID_W-1:0]     m_awid;
    wire [MP1*ADDR_W-1:0]   m_awaddr;
    wire [MP1*8-1:0]        m_awlen;
    wire [MP1*3-1:0]        m_awsize;
    wire [MP1*2-1:0]        m_awburst;
    wire [MP1-1:0]          m_awvalid;
    wire [MP1-1:0]          m_awready;
    wire [MP1*DATA_W-1:0]   m_wdata;
    wire [MP1*DATA_W/8-1:0] m_wstrb;
    wire [MP1-1:0]          m_wlast;
    wire [MP1-1:0]          m_wvalid;
    wire [MP1-1:0]          m_wready;
    wire [MP1*ID_W-1:0]     m_bid;
    wire [MP1*2-1:0]        m_bresp;
    wire [MP1-1:0]          m_bvalid;
    wire [MP1-1:0]          m_bready;
    wire [MP1*ID_W-1:0]     m_arid;
    wire [MP1*ADDR_W-1:0]   m_araddr;
    wire [MP1*8-1:0]        m_arlen;
    wire [MP1*3-1:0]        m_arsize;
    wire [MP1*2-1:0]        m_arburst;
    wire [MP1-1:0]          m_arvalid;
    wire [MP1-1:0]          m_arready;
    wire [MP1*ID_W-1:0]     m_rid;
    wire [MP1*DATA_W-1:0]   m_rdata;
    wire [MP1*2-1:0]        m_rresp;
    wire [MP1-1:0]          m_rlast;
    wire [MP1-1:0]          m_rvalid;
    wire [MP1-1:0]          m_rready;

    // At ILINK=0 the generate at the bottom ties every one of these to a
    // constant, so each use folds and nothing survives synthesis.
    wire [1:0]            il_mesh;
    wire [63:0]           il_stat_q;
    wire [3:0]            il_stat_sel;
    // The mover's write channel, between the mover and whatever owns it.
    wire                  mvx_awready, mvx_wready, mvx_bvalid;
    wire [1:0]            mvx_bresp;

    // =====================================================================
    // The agent. The existing, tested orchestrator: staging RAM, dispatcher,
    // credits, raw-flit mailbox and the NODE_STATUS mirror. It moves inside MAG
    // rather than being reimplemented, and the control AXI window is wired
    // straight to it.
    // =====================================================================
    noc_orchestrator #(
        .DATA_WIDTH(64), .ADDR_WIDTH(32), .ID_WIDTH(ID_W),
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .GRID_LO(GRID_LO), .GRID_HI(GRID_HI),
        // THE AGENT ANSWERS AT PORT 0's ADDRESS. A CU replying to the source of
        // its CU_INST addresses (MEM_X, MEM_Y); the flit arrives at port 0 and
        // the demux hands it to the agent because its type is not a memory type.
        // One address, two consumers, told apart by what the flit is.
        .ORC_X(MEM_X), .ORC_Y(MEM_Y), .STAGE_FLITS(STAGE_FLITS)
    ) u_agent (
        .clk(clk), .resetn(resetn),
        .s_axi_awid(sc_awid), .s_axi_awaddr(sc_awaddr), .s_axi_awlen(sc_awlen),
        .s_axi_awsize(3'd3), .s_axi_awburst(2'b01), .s_axi_awvalid(sc_awvalid),
        .s_axi_awready(sc_awready),
        .s_axi_wdata(sc_wdata), .s_axi_wstrb(sc_wstrb), .s_axi_wlast(sc_wlast),
        .s_axi_wvalid(sc_wvalid), .s_axi_wready(sc_wready),
        .s_axi_bid(sc_bid), .s_axi_bresp(sc_bresp), .s_axi_bvalid(sc_bvalid),
        .s_axi_bready(sc_bready),
        .s_axi_arid(sc_arid), .s_axi_araddr(sc_araddr), .s_axi_arlen(sc_arlen),
        .s_axi_arsize(3'd3), .s_axi_arburst(2'b01), .s_axi_arvalid(sc_arvalid),
        .s_axi_arready(sc_arready),
        .s_axi_rid(sc_rid), .s_axi_rdata(sc_rdata), .s_axi_rresp(sc_rresp),
        .s_axi_rlast(sc_rlast), .s_axi_rvalid(sc_rvalid), .s_axi_rready(sc_rready),
        .aux_cfg_en(aux_cfg_en), .aux_cfg_addr(aux_cfg_addr),
        .aux_cfg_data(aux_cfg_data), .aux_stat(mv_stat),
        .aux_stat_sel(il_stat_sel), .aux_stat_q(il_stat_q),
        .noc_out_data(agt_tx_data), .noc_out_valid(agt_tx_valid),
        .noc_out_busy(agt_tx_busy),
        .noc_in_data(agt_rx_data), .noc_in_valid(agt_rx_valid),
        .noc_in_busy(agt_rx_busy)
    );

    // Declared here rather than beside their always block: xvlog rejects a
    // variable used before declaration, and mv_stat below reads them.
    reg [15:0] rd_sum, wr_sum;

    // A_AUX_STAT. Memory traffic rides in the padding: rd/wr were summed across
    // ports, routed to every top and read by nothing. mv_done narrows to 24 to
    // make room. The traffic counters are 16-bit, so read deltas not totals.
    wire [63:0] mv_stat = {mv_done[23:0], rd_sum, wr_sum,
                           mv_fault, 3'd0, mv_busy};

    // The demux, the three arbiters and the outbound steer are `sn_hub`'s.
    // MAG is one of its clients and sees only its own two streams.

    // =====================================================================
    // The memory ports.
    // =====================================================================
    wire [15:0] p_rd [0:PORTS-1];
    wire [15:0] p_wr [0:PORTS-1];

    genvar gp;
    generate
    for (gp = 0; gp < PORTS; gp = gp + 1) begin : g_port
        localparam integer PX = (gp == 0) ? MEM_X : (gp == 1) ? MEM_X1 : (gp == 2) ? MEM_X2 : MEM_X3;
        localparam integer PY = (gp == 0) ? MEM_Y : (gp == 1) ? MEM_Y1 : (gp == 2) ? MEM_Y2 : MEM_Y3;

        mag_mem_port #(
            .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
            .DATA_W(DATA_W), .ADDR_W(ADDR_W), .ID_W(ID_W),
            .MEM_X(PX), .MEM_Y(PY), .WR_SLOTS(WR_SLOTS),
            .STAGE((STAGE_AT_PORT != 0) ? 0 : STAGE), .AP_DECODE(STAGE),
            .STAGE_BANKS(STAGE_BANKS),
            .STAGE_ENTRIES(STAGE_ENTRIES), .STAGE_PIPE(STAGE_PIPE),
            .MESH_ID(MESH_ID[1:0])
        ) u_eng (
            .clk(clk), .resetn(resetn),
            .m_awid(m_awid[gp*ID_W +: ID_W]),
            .m_awaddr(m_awaddr[gp*ADDR_W +: ADDR_W]),
            .m_awlen(m_awlen[gp*8 +: 8]),
            .m_awsize(m_awsize[gp*3 +: 3]),
            .m_awburst(m_awburst[gp*2 +: 2]),
            .m_awvalid(m_awvalid[gp]), .m_awready(m_awready[gp]),
            .m_wdata(m_wdata[gp*DATA_W +: DATA_W]),
            .m_wstrb(m_wstrb[gp*(DATA_W/8) +: DATA_W/8]),
            .m_wlast(m_wlast[gp]), .m_wvalid(m_wvalid[gp]),
            .m_wready(m_wready[gp]),
            .m_bid(m_bid[gp*ID_W +: ID_W]), .m_bresp(m_bresp[gp*2 +: 2]),
            .m_bvalid(m_bvalid[gp]), .m_bready(m_bready[gp]),
            .m_arid(m_arid[gp*ID_W +: ID_W]),
            .m_araddr(m_araddr[gp*ADDR_W +: ADDR_W]),
            .m_arlen(m_arlen[gp*8 +: 8]),
            .m_arsize(m_arsize[gp*3 +: 3]),
            .m_arburst(m_arburst[gp*2 +: 2]),
            .m_arvalid(m_arvalid[gp]), .m_arready(m_arready[gp]),
            .m_rid(m_rid[gp*ID_W +: ID_W]),
            .m_rdata(m_rdata[gp*DATA_W +: DATA_W]),
            .m_rresp(m_rresp[gp*2 +: 2]), .m_rlast(m_rlast[gp]),
            .m_rvalid(m_rvalid[gp]), .m_rready(m_rready[gp]),
            // The engine sees the port's flit and a valid the hub's demux has
            // already qualified, so it never learns the other clients exist.
            .mem_in_data(hub_data[gp*FLIT_WIDTH +: FLIT_WIDTH]),
            .mem_in_valid(eng_rx_valid[gp]),
            .mem_in_busy(eng_rx_busy[gp]),
            .mem_out_data(eng_tx_data[gp*FLIT_WIDTH +: FLIT_WIDTH]),
            .mem_out_valid(eng_tx_valid[gp]),
            .mem_out_busy(eng_tx_busy[gp]),
            .mem_rd_count(p_rd[gp]), .mem_wr_count(p_wr[gp])
        );

        assign port_y[gp*POS_WIDTH +: POS_WIDTH] = PY[POS_WIDTH-1:0];
    end
    endgenerate

    // Counters summed across ports, so the AXI-level totals stay one number
    // whatever the port count is.
    integer pc;
    always @(*) begin
        rd_sum = 16'd0; wr_sum = 16'd0;
        for (pc = 0; pc < PORTS; pc = pc + 1) begin
            rd_sum = rd_sum + p_rd[pc];
            wr_sum = wr_sum + p_wr[pc];
        end
    end
    assign mem_rd_count = rd_sum;
    assign mem_wr_count = wr_sum;

    // =====================================================================
    // The host memory window, on its own AXI channel: it is bursty and rare
    // against a steady state that is neither, and sharing an FSM with the NoC
    // write path stops a long upload and a cluster's write overlapping.
    // =====================================================================
    localparam [1:0] HS_IDLE = 2'd0, HS_WR = 2'd1, HS_RD = 2'd2;
    reg [2:0] hst;

    reg  [ID_W-1:0]   h_awid, h_arid;
    reg  [ADDR_W-1:0] h_awaddr, h_araddr;
    reg  [7:0]        h_awlen, h_arlen;
    reg               h_awvalid, h_arvalid, h_wvalid, h_wlast;
    reg  [DATA_W-1:0] h_wdata;

    wire h_bvalid = m_bvalid[UP];
    wire h_rvalid = m_rvalid[UP];
    wire h_rlast  = m_rlast[UP];
    wire [DATA_W-1:0] h_rdata = m_rdata[UP*DATA_W +: DATA_W];

    assign m_awid[UP*ID_W +: ID_W]           = h_awid;
    assign m_awaddr[UP*ADDR_W +: ADDR_W]     = h_awaddr;
    assign m_awlen[UP*8 +: 8]                = h_awlen;
    assign m_awsize[UP*3 +: 3]               = LSB[2:0];
    assign m_awburst[UP*2 +: 2]              = 2'b01;
    assign m_awvalid[UP]                     = h_awvalid;
    assign m_wdata[UP*DATA_W +: DATA_W]      = h_wdata;
    assign m_wstrb[UP*(DATA_W/8) +: DATA_W/8] = {(DATA_W/8){1'b1}};
    assign m_wlast[UP]                       = h_wlast;
    assign m_wvalid[UP]                      = h_wvalid;
    assign m_bready[UP]                      = 1'b1;
    assign m_arid[UP*ID_W +: ID_W]           = h_arid;
    assign m_araddr[UP*ADDR_W +: ADDR_W]     = h_araddr;
    assign m_arlen[UP*8 +: 8]                = h_arlen;
    assign m_arsize[UP*3 +: 3]               = LSB[2:0];
    assign m_arburst[UP*2 +: 2]              = 2'b01;
    assign m_arvalid[UP]                     = h_arvalid;

    // =====================================================================
    // The memory mover, on its own AXI channel. It never touches a port's
    // state; the only thing it shares is the address space on the far side.
    // =====================================================================
    // The mover and its transform slot are the CONTROL PROCESSOR'S, not MAG's.
    // What arrives here is channel MV, an ordinary requester, which is all MAG
    // ever knew about it.
    assign mv_arready = m_arready[MV];
    assign mv_rid     = m_rid[MV*ID_W +: ID_W];
    assign mv_rdata   = m_rdata[MV*DATA_W +: DATA_W];
    assign mv_rresp   = m_rresp[MV*2 +: 2];
    assign mv_rlast   = m_rlast[MV];
    assign mv_rvalid  = m_rvalid[MV];
    assign mv_bid     = m_bid[MV*ID_W +: ID_W];
    assign mv_awready = mvx_awready;
    assign mv_wready  = mvx_wready;
    assign mv_bvalid  = mvx_bvalid;
    assign mv_bresp   = mvx_bresp;

    assign m_awid[MV*ID_W +: ID_W]            = mv_awid;
    assign m_awlen[MV*8 +: 8]                 = mv_awlen;
    assign m_awsize[MV*3 +: 3]                = mv_awsize;
    assign m_awburst[MV*2 +: 2]               = mv_awburst;
    assign m_wstrb[MV*(DATA_W/8) +: DATA_W/8] = mv_wstrb;
    assign m_wlast[MV]                        = mv_wlast;
    assign m_bready[MV]                       = mv_bready;
    // AWADDR / AWVALID / WDATA / WVALID are driven by the generate at the
    // bottom: with the interlink they pass through the address split, without
    // it they are the mover's own.
    assign m_arid[MV*ID_W +: ID_W]            = mv_arid;
    assign m_araddr[MV*ADDR_W +: ADDR_W]      = mv_araddr;
    assign m_arlen[MV*8 +: 8]                 = mv_arlen;
    assign m_arsize[MV*3 +: 3]                = mv_arsize;
    assign m_arburst[MV*2 +: 2]               = mv_arburst;
    assign m_arvalid[MV]                      = mv_arvalid;
    assign m_rready[MV]                       = mv_rready;

    // THE HOST UPLOAD NO LONGER TRANSFORMS. Aperture 0x4/0x5 and its packing
    // address bit are retired with it. A tensor that needs converting is either
    // converted by the host or uploaded raw and converted on card by the mover
    // through the shared transform slot: mem/L2 -> slot -> mem/L2.
    reg host_b;   // write response seen, still waiting for the host to take it

    assign sm_awready = (hst == HS_IDLE);
    assign sm_arready = (hst == HS_IDLE) && !sm_awvalid;

    assign sm_wready  = (hst == HS_WR) && (!h_wvalid || m_wready[UP]);
    assign sm_bid     = m_bid[UP*ID_W +: ID_W];
    assign sm_bresp   = m_bresp[UP*2 +: 2];
    // m_bready is tied high, so the slave's write response is consumed the cycle
    // it appears. Passed straight through it would be offered for exactly one
    // cycle, and a host that raises BREADY after BVALID -- legal AXI, and what a
    // pipelined master does -- would never see it, hence `host_b`.
    assign sm_bvalid  = (hst == HS_WR) && (h_bvalid || host_b);
    assign sm_rid     = m_rid[UP*ID_W +: ID_W];
    assign sm_rdata   = h_rdata;
    assign sm_rresp   = m_rresp[UP*2 +: 2];
    assign sm_rlast   = h_rlast;
    assign sm_rvalid  = (hst == HS_RD) && h_rvalid;
    assign m_rready[UP] = (hst == HS_RD) && sm_rready;

`ifndef SYNTHESIS
`endif

    always @(posedge clk) begin
        if (!resetn) begin
            hst <= HS_IDLE;
            h_awvalid <= 1'b0; h_arvalid <= 1'b0; h_wvalid <= 1'b0;
            h_wlast <= 1'b0; h_awlen <= 8'd0; h_arlen <= 8'd0;
            // The AR/AW/W payloads are qualified by the three valids above.
            host_b <= 1'b0;
        end else begin
            if (h_awvalid && m_awready[UP]) begin
                h_awvalid <= 1'b0;
            end
            if (h_arvalid && m_arready[UP]) begin
                h_arvalid <= 1'b0;
            end

            case (hst)
                HS_IDLE: begin
                    if (sm_awvalid && sm_awready) begin
                        h_awaddr  <= sm_awaddr;
                        h_awlen   <= sm_awlen;
                        h_awid    <= sm_awid;
                        h_awvalid <= 1'b1;
                        hst <= HS_WR;
                    end else if (sm_arvalid && sm_arready) begin
                        h_araddr  <= sm_araddr;
                        h_arlen   <= sm_arlen;
                        h_arid    <= sm_arid;
                        h_arvalid <= 1'b1;
                        hst <= HS_RD;
                    end
                end

                HS_WR: begin
                    // Sample the host beat only when the AXI side can take one.
                    // Re-registering every cycle regardless replays the beat that
                    // was not yet accepted, so every burst longer than one beat is
                    // written twice and one word late -- silent corruption on the
                    // operand-upload path, which is what XDMA will use.
                    if (!h_wvalid || m_wready[UP]) begin
                        h_wdata  <= sm_wdata;
                        h_wlast  <= sm_wlast;
                        h_wvalid <= sm_wvalid;
                    end
                    if (h_bvalid) begin
                        host_b <= 1'b1;
                    end
                    if ((h_bvalid || host_b) && sm_bready) begin
                        host_b <= 1'b0;
                        hst <= HS_IDLE;
                    end
                end

                HS_RD: begin
                    if (h_rvalid && h_rlast && sm_rready) begin
                        hst <= HS_IDLE;
                    end
                end

                default: hst <= HS_IDLE;
            endcase
        end
    end

    // =====================================================================
    // The interlink. docs/interlink/, and boundary.md s2 for what this costs
    // when it is absent: nothing, because the else arm is all constants.
    // =====================================================================
    generate
    if (ILINK != 0) begin : g_ilink
        wire [TUSER_W-1:0] ltx_hdr, lrx_hdr;
        wire [LINK_W-1:0]  ltx_dat, lrx_dat;
        wire ltx_hvalid, ltx_hready, ltx_dvalid, ltx_dready, ltx_dlast;
        wire lrx_hvalid, lrx_hready, lrx_dvalid, lrx_dready, lrx_dlast;
        wire [63:0] sw_tx0, sw_rx0, sw_st0, sw_tx1, sw_rx1, sw_st1;
        wire [63:0] sw_fwd, sw_lblk;
        wire [31:0] sw_c0, sw_c1;
        wire [3:0]  sw_flt;

        mag_ilink #(
            .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH), .DATA_W(DATA_W),
            .ADDR_W(ADDR_W), .LINK_W(LINK_W), .TUSER_W(TUSER_W),
            .MESH_ID(MESH_ID), .MAX_BEATS(IL_MAX_BEATS),
            .MEM_X(MEM_X), .MEM_Y(MEM_Y)
        ) u_il (
            .clk(clk), .resetn(resetn),
            .cfg_en(aux_cfg_en || cpu_il_en),
            .cfg_addr(aux_cfg_en ? aux_cfg_addr : cpu_il_addr),
            .cfg_data(aux_cfg_en ? aux_cfg_data : cpu_il_data),
            .stat_sel(il_stat_sel), .stat_q(il_stat_q), .my_mesh(il_mesh),
            .dbell_counts(cpu_dbell_counts),

            .s_awaddr(mv_awaddr), .s_awlen(mv_awlen), .s_awvalid(mv_awvalid),
            .s_awready(mvx_awready),
            .s_wdata(mv_wdata), .s_wstrb(mv_wstrb), .s_wvalid(mv_wvalid),
            .s_wready(mvx_wready),
            .s_bvalid(mvx_bvalid), .s_bresp(mvx_bresp), .s_bready(mv_bready),

            .m_awaddr(m_awaddr[MV*ADDR_W +: ADDR_W]), .m_awvalid(m_awvalid[MV]),
            .m_awready(m_awready[MV]),
            .m_wdata(m_wdata[MV*DATA_W +: DATA_W]), .m_wstrb(),
            .m_wlast(), .m_wvalid(m_wvalid[MV]), .m_wready(m_wready[MV]),
            .m_bvalid(m_bvalid[MV]), .m_bresp(m_bresp[MV*2 +: 2]), .m_bready(),

            .lk_awaddr(lks_awaddr), .lk_awvalid(lks_awvalid),
            .lk_awready(lks_awready),
            .lk_wdata(lks_wdata), .lk_wstrb(lks_wstrb),
            .lk_wlast(lks_wlast), .lk_wvalid(lks_wvalid),
            .lk_wready(lks_wready),
            .lk_bvalid(m_bvalid[LK]), .lk_bresp(m_bresp[LK*2 +: 2]),
            .lk_bready(m_bready[LK]),

            .enc_data(enc_data), .enc_valid(enc_valid),
            .enc_busy(enc_busy),
            .inj_data(inj_data), .inj_valid(inj_valid),
            .inj_busy(inj_busy),

            .ltx_hdr(ltx_hdr), .ltx_hvalid(ltx_hvalid), .ltx_hready(ltx_hready),
            .ltx_dat(ltx_dat), .ltx_dlast(ltx_dlast), .ltx_dvalid(ltx_dvalid),
            .ltx_dready(ltx_dready),
            .lrx_hdr(lrx_hdr), .lrx_hvalid(lrx_hvalid), .lrx_hready(lrx_hready),
            .lrx_dat(lrx_dat), .lrx_dlast(lrx_dlast), .lrx_dvalid(lrx_dvalid),
            .lrx_dready(lrx_dready),

            .sw_tx0(sw_tx0), .sw_rx0(sw_rx0), .sw_stall0(sw_st0),
            .sw_tx1(sw_tx1), .sw_rx1(sw_rx1), .sw_stall1(sw_st1),
            .sw_fwd(sw_fwd), .sw_lblock(sw_lblk),
            .sw_cred0(sw_c0), .sw_cred1(sw_c1), .sw_fault(sw_flt),
            .bad_remote_req(bad_remote_req)
        );

        // THE INTERLINK'S LANDING CHANNEL IS SKIDDED, and this is a timing fix
        // with a measured cause. Wired straight through, `mag_ilink`'s decision
        // to accept an inbound remote packet depended on the converged path's
        // readiness, so the m62 mesh's worst path ran
        //
        //   u_dram/rr_rd -> the round-robin scan -> u_l2's ready
        //     -> u_il's m_awready/lk_awready -> lk_free -> busy
        //     -> u_sw/u_lmux's ready -> a switch FIFO's read enable
        //
        // at 12 logic levels and -0.413 ns, 421 failing endpoints, 78% route.
        // sb_skid's i_ready does not depend on o_ready, so the interlink now
        // sees a local register and the span stops at the skid. Both channels
        // get one, so AW and W keep the same latency and their order with it.
        wire [ADDR_W-1:0]   lks_awaddr;
        wire                lks_awvalid, lks_awready;
        wire [DATA_W-1:0]   lks_wdata;
        wire [DATA_W/8-1:0] lks_wstrb;
        wire                lks_wlast, lks_wvalid, lks_wready;

        sb_skid #(.W(ADDR_W)) u_lk_awskid (
            .clk(clk), .rst(!resetn),
            .i_valid(lks_awvalid), .i_ready(lks_awready), .i_data(lks_awaddr),
            .o_valid(m_awvalid[LK]), .o_ready(m_awready[LK]),
            .o_data(m_awaddr[LK*ADDR_W +: ADDR_W])
        );

        sb_skid #(.W(DATA_W + DATA_W/8 + 1)) u_lk_wskid (
            .clk(clk), .rst(!resetn),
            .i_valid(lks_wvalid), .i_ready(lks_wready),
            .i_data({lks_wlast, lks_wstrb, lks_wdata}),
            .o_valid(m_wvalid[LK]), .o_ready(m_wready[LK]),
            .o_data({m_wlast[LK], m_wstrb[LK*(DATA_W/8) +: DATA_W/8],
                     m_wdata[LK*DATA_W +: DATA_W]})
        );

        mag_switch #(
            .LINK_W(LINK_W), .TUSER_W(TUSER_W), .RX_BEATS(IL_RX_BEATS),
            .MAX_BEATS(IL_MAX_BEATS)
        ) u_sw (
            .clk(clk), .resetn(resetn), .my_mesh(il_mesh),
            .ltx_hdr(ltx_hdr), .ltx_hvalid(ltx_hvalid), .ltx_hready(ltx_hready),
            .ltx_dat(ltx_dat), .ltx_dlast(ltx_dlast), .ltx_dvalid(ltx_dvalid),
            .ltx_dready(ltx_dready),
            .lrx_hdr(lrx_hdr), .lrx_hvalid(lrx_hvalid), .lrx_hready(lrx_hready),
            .lrx_dat(lrx_dat), .lrx_dlast(lrx_dlast), .lrx_dvalid(lrx_dvalid),
            .lrx_dready(lrx_dready),
            .m0_tdata(link0_out_tdata), .m0_tuser(link0_out_tuser),
            .m0_tlast(link0_out_tlast), .m0_tvalid(link0_out_tvalid),
            .m0_tready(link0_out_tready),
            .s0_tdata(link0_in_tdata), .s0_tuser(link0_in_tuser),
            .s0_tlast(link0_in_tlast), .s0_tvalid(link0_in_tvalid),
            .s0_tready(link0_in_tready),
            .m1_tdata(link1_out_tdata), .m1_tuser(link1_out_tuser),
            .m1_tlast(link1_out_tlast), .m1_tvalid(link1_out_tvalid),
            .m1_tready(link1_out_tready),
            .s1_tdata(link1_in_tdata), .s1_tuser(link1_in_tuser),
            .s1_tlast(link1_in_tlast), .s1_tvalid(link1_in_tvalid),
            .s1_tready(link1_in_tready),
            .ctr_tx0(sw_tx0), .ctr_rx0(sw_rx0), .ctr_stall0(sw_st0),
            .ctr_tx1(sw_tx1), .ctr_rx1(sw_rx1), .ctr_stall1(sw_st1),
            .ctr_fwd(sw_fwd), .ctr_lblock(sw_lblk),
            .cred0_state(sw_c0), .cred1_state(sw_c1), .fault(sw_flt)
        );
        assign my_mesh = il_mesh;

        assign m_arid[LK*ID_W +: ID_W]   = {ID_W{1'b0}};
        assign m_araddr[LK*ADDR_W +: ADDR_W] = {ADDR_W{1'b0}};
        assign m_arlen[LK*8 +: 8]        = 8'd0;
        assign m_arsize[LK*3 +: 3]       = LSB[2:0];
        assign m_arburst[LK*2 +: 2]      = 2'b01;
        assign m_arvalid[LK]             = 1'b0;
        assign m_rready[LK]              = 1'b1;
        assign m_awid[LK*ID_W +: ID_W]   = {ID_W{1'b0}};
        assign m_awlen[LK*8 +: 8]        = 8'd0;
        assign m_awsize[LK*3 +: 3]       = LSB[2:0];
        assign m_awburst[LK*2 +: 2]      = 2'b01;
    end else begin : g_no_ilink
        // The mover owns its channel outright, exactly as before.
        assign m_awaddr[MV*ADDR_W +: ADDR_W] = mv_awaddr;
        assign m_awvalid[MV]                 = mv_awvalid;
        assign m_wdata[MV*DATA_W +: DATA_W]  = mv_wdata;
        assign m_wvalid[MV]                  = mv_wvalid;
        assign mvx_awready = m_awready[MV];
        assign mvx_wready  = m_wready[MV];
        assign mvx_bvalid  = m_bvalid[MV];
        assign mvx_bresp   = m_bresp[MV*2 +: 2];

        assign il_mesh    = MESH_ID[1:0];
        assign my_mesh    = MESH_ID[1:0];
        assign il_stat_q  = 64'd0;
        assign cpu_dbell_counts = 64'd0;   // no interlink, no doorbells
        assign enc_busy   = 1'b1;
        assign inj_data   = {FLIT_WIDTH{1'b0}};
        assign inj_valid  = 1'b0;

        assign link0_out_tdata  = {LINK_W{1'b0}};
        assign link0_out_tuser  = {TUSER_W{1'b0}};
        assign link0_out_tlast  = 1'b0;
        assign link0_out_tvalid = 1'b0;
        assign link0_in_tready  = 1'b1;
        assign link1_out_tdata  = {LINK_W{1'b0}};
        assign link1_out_tuser  = {TUSER_W{1'b0}};
        assign link1_out_tlast  = 1'b0;
        assign link1_out_tvalid = 1'b0;
        assign link1_in_tready  = 1'b1;
    end
    endgenerate

    // ---- the control processor's L1 channel, always present ---------------
    assign m_awid   [CP*ID_W   +: ID_W]   = {ID_W{1'b0}};
        assign m_awaddr [CP*ADDR_W +: ADDR_W] = cp_awaddr;
        assign m_awlen  [CP*8      +: 8]      = cp_awlen;
        assign m_awsize [CP*3      +: 3]      = LSB[2:0];
        assign m_awburst[CP*2      +: 2]      = 2'b01;
        assign m_awvalid[CP]                  = cp_awvalid;
        assign cp_awready                     = m_awready[CP];
        assign m_wdata  [CP*DATA_W +: DATA_W]     = cp_wdata;
        assign m_wstrb  [CP*(DATA_W/8) +: DATA_W/8] = cp_wstrb;
        assign m_wlast  [CP]                  = cp_wlast;
        assign m_wvalid [CP]                  = cp_wvalid;
        assign cp_wready                      = m_wready[CP];
        assign cp_bvalid                      = m_bvalid[CP];
        assign m_bready [CP]                  = cp_bready;
        assign m_arid   [CP*ID_W   +: ID_W]   = {ID_W{1'b0}};
        assign m_araddr [CP*ADDR_W +: ADDR_W] = cp_araddr;
        assign m_arlen  [CP*8      +: 8]      = cp_arlen;
        assign m_arsize [CP*3      +: 3]      = LSB[2:0];
        assign m_arburst[CP*2      +: 2]      = 2'b01;
        assign m_arvalid[CP]                  = cp_arvalid;
        assign cp_arready                     = m_arready[CP];
        assign cp_rdata                       = m_rdata[CP*DATA_W +: DATA_W];
        assign cp_rlast                       = m_rlast[CP];
        assign cp_rvalid                      = m_rvalid[CP];
        assign m_rready [CP]                  = cp_rready;

    // ---- the internal requesters onto ONE AXI master ----------------------
    // id/resp die here as they did in mag_1m.v:238's shim; nothing reads them.
    wire [MP1-1:0]        q_valid, q_ready, q_write;
    wire [MP1*ADDR_W-1:0] q_addr;
    wire [MP1*16-1:0]     q_len;
    wire [MP1-1:0]        w_valid_i, w_ready_i;
    wire [MP1*DATA_W-1:0] w_data_i;
    wire [MP1*DATA_W/8-1:0] w_strb_i;
    wire [MP1-1:0]        r_valid_i, r_ready_i, r_last_i;
    wire [MP1*DATA_W-1:0] r_data_i;
    wire [MP1-1:0]        b_done;

    genvar rq;
    generate for (rq = 0; rq < MP1; rq = rq + 1) begin : g_req
        // WRITE WINS when both are offered -- but only at FIRST offer. The
        // choice HOLDS until grant: the DRAM port arbitrates on a registered
        // request vector and samples the bus live, so a presentation that
        // switches mid-wait issues the read at the write's address, and the
        // one-wire grant then pops the wrong channel (measured in rv_mc4:
        // a writeback of line 641 landed at 787 and the fill returned 641).
        reg sel_h, sel_w;
        always @(posedge clk) begin
            if (!resetn) begin
                sel_h <= 1'b0;
            end
            else if (q_valid[rq] && !q_ready[rq]) begin
                if (!sel_h) begin
                    sel_w <= m_awvalid[rq];
                end
                sel_h <= 1'b1;
            end
            else begin
                sel_h <= 1'b0;
            end
        end
        wire use_w = sel_h ? sel_w : m_awvalid[rq];
        wire aw_r = m_awvalid[rq] && use_w;
        wire ar_r = m_arvalid[rq] && !use_w;
        assign q_valid[rq] = aw_r || ar_r;
        assign q_write[rq] = aw_r;
        assign q_addr[rq*ADDR_W +: ADDR_W] = aw_r ? m_awaddr[rq*ADDR_W +: ADDR_W]
                                                  : m_araddr[rq*ADDR_W +: ADDR_W];
        assign q_len[rq*16 +: 16] = aw_r ? {8'd0, m_awlen[rq*8 +: 8]}
                                         : {8'd0, m_arlen[rq*8 +: 8]};
        assign m_awready[rq] = q_ready[rq] && aw_r;
        assign m_arready[rq] = q_ready[rq] && ar_r;

        assign w_valid_i[rq] = m_wvalid[rq];
        assign w_data_i[rq*DATA_W +: DATA_W] = m_wdata[rq*DATA_W +: DATA_W];
        assign w_strb_i[rq*DATA_W/8 +: DATA_W/8] = m_wstrb[rq*DATA_W/8 +: DATA_W/8];
        assign m_wready[rq] = w_ready_i[rq];

        assign m_rvalid[rq] = r_valid_i[rq];
        assign m_rdata[rq*DATA_W +: DATA_W] = r_data_i[rq*DATA_W +: DATA_W];
        assign m_rlast[rq] = r_last_i[rq];
        assign m_rid[rq*ID_W +: ID_W] = {ID_W{1'b0}};
        assign m_rresp[rq*2 +: 2] = 2'b00;
        assign r_ready_i[rq] = m_rready[rq];

        assign m_bvalid[rq] = b_done[rq];
        assign m_bid[rq*ID_W +: ID_W] = {ID_W{1'b0}};
        assign m_bresp[rq*2 +: 2] = 2'b00;
    end endgenerate

    // ---- L2 on the converged path, before the DRAM port ------------------
    wire [MP1-1:0]          dq_valid, dq_ready, dq_write;
    wire [MP1*ADDR_W-1:0]   dq_addr;
    wire [MP1*16-1:0]       dq_len;
    wire [MP1-1:0]          dw_valid, dw_ready;
    wire [MP1*DATA_W-1:0]   dw_data;
    wire [MP1*DATA_W/8-1:0] dw_strb;
    wire [MP1-1:0]          dr_valid, dr_ready, dr_last;
    wire [MP1*DATA_W-1:0]   dr_data;
    wire [MP1-1:0]          db_done;

    mag_stage_port #(.N(MP1), .ADDR_W(ADDR_W), .SW(DATA_W),
                     .STAGE((STAGE_AT_PORT != 0) ? STAGE : 0),
                     .BANKS(STAGE_BANKS), .ENTRIES(STAGE_ENTRIES),
                     .PIPE(STAGE_PIPE), .MESH_ID(MESH_ID[1:0])) u_l2 (
        .clk(clk), .rst(!resetn),
        .q_valid(q_valid), .q_ready(q_ready), .q_addr(q_addr),
        .q_len(q_len), .q_write(q_write),
        .w_valid(w_valid_i), .w_ready(w_ready_i), .w_data(w_data_i),
        .w_strb(w_strb_i),
        .r_valid(r_valid_i), .r_ready(r_ready_i), .r_data(r_data_i),
        .r_last(r_last_i), .b_valid(b_done),
        .dq_valid(dq_valid), .dq_ready(dq_ready), .dq_addr(dq_addr),
        .dq_len(dq_len), .dq_write(dq_write),
        .dw_valid(dw_valid), .dw_ready(dw_ready), .dw_data(dw_data),
        .dw_strb(dw_strb),
        .dr_valid(dr_valid), .dr_ready(dr_ready), .dr_data(dr_data),
        .dr_last(dr_last), .db_valid(db_done)
    );

    mag_dram_port #(.N(MP1), .ADDR_W(ADDR_W), .SW(DATA_W), .MW(MW),
                    .ID_W(ID_W), .RD_OUT(DRAM_RD_OUT), .DRAM_CDC(DRAM_CDC)) u_dram (
        .s_aclk(clk), .s_aresetn(resetn),
        .q_valid(dq_valid), .q_ready(dq_ready), .q_addr(dq_addr),
        .q_len(dq_len), .q_write(dq_write),
        .w_valid(dw_valid), .w_ready(dw_ready), .w_data(dw_data),
        .w_strb(dw_strb),
        .r_valid(dr_valid), .r_ready(dr_ready), .r_data(dr_data),
        .r_last(dr_last), .b_valid(db_done),
        .m_aclk(dram_aclk), .m_aresetn(dram_aresetn),
        .m_awid(dram_awid), .m_awaddr(dram_awaddr), .m_awlen(dram_awlen),
        .m_awsize(dram_awsize), .m_awburst(dram_awburst),
        .m_awvalid(dram_awvalid), .m_awready(dram_awready),
        .m_wdata(dram_wdata), .m_wstrb(dram_wstrb), .m_wlast(dram_wlast),
        .m_wvalid(dram_wvalid), .m_wready(dram_wready),
        .m_bid(dram_bid), .m_bresp(dram_bresp), .m_bvalid(dram_bvalid),
        .m_bready(dram_bready),
        .m_arid(dram_arid), .m_araddr(dram_araddr), .m_arlen(dram_arlen),
        .m_arsize(dram_arsize), .m_arburst(dram_arburst),
        .m_arvalid(dram_arvalid), .m_arready(dram_arready),
        .m_rid(dram_rid), .m_rdata(dram_rdata), .m_rresp(dram_rresp),
        .m_rlast(dram_rlast), .m_rvalid(dram_rvalid), .m_rready(dram_rready)
    );

endmodule

`default_nettype wire
