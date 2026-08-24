// MAG -- Memory Access Gateway.
//
// The single point where a partition touches everything outside it: the host's
// AXI, its own memory, and its NoC mesh.
//
//   NoC port 0 ──┬─►  mag_mem_port ──► AXI master 0 ─┐
//   NoC port 1 ──┼─►  mag_mem_port ──► AXI master 1 ─┤
//        ...     │                                   ├──► memory
//   AXI slave, memory ──► upload ────► AXI master N ─┤
//   (host, verbatim)                                 │
//   mover ──(read return)──► mag_xform ──► master M ─┤
//   control processor ─────────────────► master CP ──┘
//                 │
//   AXI slave, control ──► agent (noc_orchestrator)
//
// THE AGENT HAS NO PORT OF ITS OWN. It shares the NoC ports above, which is
// what the fork on the left means: each port's inbound flits are demuxed BY
// TYPE -- memory requests to that port's engine, everything else to the agent
// -- and outbound the agent leaves by the port on its destination's row.
//
// MAG is a SLAVE on the main interconnect, not a master; the memory moved one
// level down, behind an adapter. See docs/arch-design.md s6.4.
//
// SEVERAL MEMORY PORTS, AND THAT IS THE ARCHITECTURE RATHER THAN AN OPTION. A
// port serves ~2 clusters. A single read engine is what stopped the machine
// scaling -- and it stopped while nothing was saturated, so the constraint was
// the server, not the bandwidth. Each port owns its intake queues, read engine,
// write slots and AXI channel -- but NOT a transform: that is one shared bank
// ON THE MOVER'S READ RETURN, reached only through descriptor mode 5. See
// mag_mem_port.v, mag_xform.v and docs/arch/sysnode/.
//
// The ports are placed at DIFFERENT mesh nodes on purpose. Routing is X-then-Y
// on clamped coordinates, so a port at (0,y) draws traffic to router (1,y) --
// putting two ports on one router splits the server and not the funnel.
//
// v1 SCOPE: no cache, no TLB. Addresses are physical, and the memory window is
// a straight offset into the attached RAM. Both are additive later and neither
// changes this interface -- docs/mas/spec.md s7a.

`default_nettype none

module mag #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer DATA_W     = 256,     // AXI memory width == flit payload
    parameter integer ADDR_W     = 40,
    parameter integer ID_W       = 4,
    // How many NoC memory endpoints this MAG presents, and where each sits.
    // Named per port rather than packed: a packed field is one shift away from
    // pointing a whole port at the wrong node, and it would elaborate cleanly.
    parameter integer MEM_PORTS  = 1,
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
    // The MAG control processor's memory channel (docs/arch/sysnode/control-processor.md
    // s3.1). ZERO GENERATES NONE OF IT, like ILINK: the ports fold to constants
    // and MP1 is unchanged, so the shipping bitstream stays identical.
    parameter integer CTRL_PE    = 0,
    // The transform slot. Selection is an ID, not a bit per transform, so the
    // field is sized for a design with several occupants even though the
    // reference project ships one -- widening it later is a protocol change.
    parameter integer XFORM_SLOTS     = 1,
    parameter integer XID_W           = 4,
    parameter integer XMODE_W         = 4,
    // Declared by the occupant, needed by the engine before it has run.
    parameter integer XFORM_IN_BITS   = 2048,
    parameter integer XFORM_OUT_WORDS = 4,
    // INTERNAL requesters, never leaving MAG: memory ports, host upload, mover,
    // and with the interlink the one inbound remote writes land through.
    parameter integer MP1        = MEM_PORTS + 2 + ((ILINK != 0) ? 1 : 0)
                                                 + ((CTRL_PE != 0) ? 1 : 0),
    // mag_dram_port packs DATA_W -> MW, so at 512 an 8-beat 256-bit burst
    // becomes 4 beats. Defaults EQUAL, which is the R=1 no-sub-beat case.
    parameter integer MW         = DATA_W,
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
    // and costing MEM_PORTS x 64 URAM. 1 = one store on the converged path.
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

    // ---- NoC: memory ports, flattened ------------------------------------
    input  wire [MEM_PORTS*FLIT_WIDTH-1:0] mem_in_data,
    input  wire [MEM_PORTS-1:0]            mem_in_valid,
    output wire [MEM_PORTS-1:0]            mem_in_busy,
    output wire [MEM_PORTS*FLIT_WIDTH-1:0] mem_out_data,
    output wire [MEM_PORTS-1:0]            mem_out_valid,
    input  wire [MEM_PORTS-1:0]            mem_out_busy,

    // The agent has no port of its own; it shares these -- see the share layer.

    output wire [15:0]           mem_rd_count,
    output wire [15:0]           mem_wr_count,

    // ---- the memory mover: status ----------------------------------------
    // Its COMMAND path is arch.md s2's next step, now taken: a slice of the
    // control window, not a boundary port. Loose sideband ports never get wired
    // in a block design, which left the shipped mover commandable by nothing.
    output wire                  mv_busy,
    output wire [3:0]            mv_fault,
    output wire [31:0]           mv_done,

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

    // ---- the control processor's memory channel, CTRL_PE only -------------
    // A slave on MAG; the processor is the master. Present at any CTRL_PE for
    // the same reason the link ports are: Verilog has no conditional port.
    // The control processor commanding the mover directly: inside the node this
    // is a wire, which is the whole point of putting it here rather than at a
    // mesh port. It wins over the host window when both pulse.
    input  wire                  pe_cfg_en,
    input  wire [7:0]            pe_cfg_addr,
    input  wire [63:0]           pe_cfg_data,

    // The transform slot's occupant registers, reached by the processor as
    // ordinary loads and stores. The HOST has no path to them.
    input  wire                  pe_xcfg_en,
    input  wire [XID_W-1:0]      pe_xcfg_id,
    input  wire [7:0]            pe_xcfg_addr,
    input  wire [31:0]           pe_xcfg_data,
    output wire [31:0]           pe_xcfg_rdata,
    output wire [3:0]            xf_fault,

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
    localparam integer UP = MEM_PORTS;           // its channel index
    localparam integer MV = MEM_PORTS + 1;
    // Only reachable when ILINK is set; at 0 it aliases MV and nothing drives
    // it, which is why every use of LK is inside the same generate.
    localparam integer LK = (ILINK != 0) ? MEM_PORTS + 2 : MEM_PORTS + 1;
    // Same aliasing rule as LK: at CTRL_PE=0 this names MV and nothing drives
    // it, so every use below sits inside the same generate.
    localparam integer CP = (CTRL_PE != 0) ? (MEM_PORTS + 2 + ((ILINK != 0) ? 1 : 0)) : (MEM_PORTS + 1);

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

    // ---- interlink nets, declared here because the demux above reads them --
    // At ILINK=0 the generate at the bottom ties every one of these to a
    // constant, so each use folds and nothing survives synthesis.
    wire [1:0]            il_mesh;
    wire [FLIT_WIDTH-1:0] enc_in_data;
    wire                  enc_in_valid, enc_in_busy;
    wire [FLIT_WIDTH-1:0] inj_out_data;
    wire                  inj_out_valid, inj_out_busy;
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
        .aux_cfg_en(mv_cfg_en), .aux_cfg_addr(mv_cfg_addr),
        .aux_cfg_data(mv_cfg_data), .aux_stat(mv_stat),
        .aux_stat_sel(il_stat_sel), .aux_stat_q(il_stat_q),
        .noc_out_data(agt_out_data), .noc_out_valid(agt_out_valid),
        .noc_out_busy(agt_out_busy),
        .noc_in_data(agt_in_data), .noc_in_valid(agt_in_valid),
        .noc_in_busy(agt_in_busy)
    );

    // The mover's own offsets pass through unchanged, so a driver writes its
    // 0x38 seed at A_AUX_CFG + 0x38. Readable in one 64-bit load.
    wire        mv_cfg_en;
    wire [7:0]  mv_cfg_addr;
    wire [63:0] mv_cfg_data;
    // Declared here rather than beside their always block: xvlog rejects a
    // variable used before declaration, and mv_stat below reads them.
    reg [15:0] rd_sum, wr_sum;

    // A_AUX_STAT. Memory traffic rides in the padding: rd/wr were summed across
    // ports, routed to every top and read by nothing. mv_done narrows to 24 to
    // make room. The traffic counters are 16-bit, so read deltas not totals.
    wire [63:0] mv_stat = {mv_done[23:0], rd_sum, wr_sum,
                           mv_fault, 3'd0, mv_busy};

    // =====================================================================
    // THE SHARE LAYER: the agent rides the memory ports. MAG presents
    // MEM_PORTS NoC attachments and no more.
    //
    // INBOUND is a demux by TYPE, per port: a memory request is the engine's,
    // anything else the agent's. The agent has one input, so the ports
    // round-robin into it, and a port that is not granted holds `busy`.
    //
    // OUTBOUND is steered by DESTINATION ROW -- a flit for row y leaves from the
    // port on row y -- so dispatch spreads across all of them.
    //
    // The agent WINS outbound arbitration. Its traffic is a handful of control
    // flits against a stream of operand words, so the cost to memory is
    // negligible; engine priority would let a busy port starve dispatch exactly
    // when the machine is busiest.
    // =====================================================================
    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_WR_DATA = 4'h4;
    localparam integer TY_LSB = FLIT_WIDTH - 4*POS_WIDTH - 4;
    localparam integer DY_LSB = FLIT_WIDTH - 2*POS_WIDTH;
    // NOC_RSVD. Bit 2 marks a flit for another mesh and is zero on every flit a
    // single-mesh build ever produces, which is what makes one compiler serve
    // both -- docs/interlink/boundary.md s5.
    localparam integer RS_LSB = FLIT_WIDTH - 4*POS_WIDTH - 16;
    // NOC_MEM_ADDR is [255:216], so addr[37:36] -- the mesh -- is at [253:252].
    // At 254 this read {special, rsvd} and faulted every LOCAL aperture access.
    localparam integer RQ_MESH_LSB = 252;

    wire [FLIT_WIDTH-1:0] agt_out_data;
    wire                  agt_out_valid;
    wire [FLIT_WIDTH-1:0] agt_in_data;
    wire                  agt_in_valid;
    wire                  agt_in_busy;
    wire                  agt_out_busy;

    wire [FLIT_WIDTH-1:0] eng_out_data  [0:MEM_PORTS-1];
    wire                  eng_out_valid [0:MEM_PORTS-1];
    wire                  eng_in_busy   [0:MEM_PORTS-1];
    wire [POS_WIDTH-1:0]  port_y        [0:MEM_PORTS-1];

    // ---- inbound: the engine's, the interlink's, or the agent's? ----------
    // Three consumers now, told apart by the same flit: a memory type is the
    // engine's, a remote marker is the interlink's, and what is left is the
    // agent's. At ILINK=0 the middle case is a constant false and this is the
    // two-way demux it was before.
    wire [MEM_PORTS-1:0] in_is_mem, in_is_req, in_is_rem, in_to_agt, in_to_enc;
    wire [MEM_PORTS-1:0] rq_rem;
    genvar gd;
    generate
    for (gd = 0; gd < MEM_PORTS; gd = gd + 1) begin : g_demux
        wire [3:0] ty = mem_in_data[gd*FLIT_WIDTH + TY_LSB +: 4];
        wire       rm = mem_in_data[gd*FLIT_WIDTH + RS_LSB + 2];
        wire [1:0] rq = mem_in_data[gd*FLIT_WIDTH + RQ_MESH_LSB +: 2];
        assign in_is_req[gd] = (ty == T_MEM_RD_REQ) || (ty == T_MEM_WR_REQ);
        assign in_is_mem[gd] = in_is_req[gd] || (ty == T_MEM_WR_DATA);
        // A MEMORY flit may be remote too. The sender marks every flit of the
        // burst, so the encapsulator stays stateless -- mx_cluster_cu.v:996.
        assign in_is_rem[gd] = (ILINK != 0) && rm;
        assign in_to_agt[gd] = mem_in_valid[gd] && !in_is_mem[gd] && !in_is_rem[gd];
        assign in_to_enc[gd] = mem_in_valid[gd] && in_is_rem[gd];
        // An address naming another mesh on a flit NOT marked remote. That is
        // the one case still aliased onto local DRAM, and the compiler's bug.
        assign rq_rem[gd] = (
            mem_in_valid[gd]
            && in_is_req[gd]
            && !in_is_rem[gd]
            && (rq != il_mesh)
        );
    end
    endgenerate

    // ---- inbound arbitration into the agent's single input ---------------
    // Round-robin, so no port can hold the agent against the others. The
    // pointer only moves on an accepted flit; moving it every cycle would let
    // a port lose its turn to one that had nothing to send.
    reg  [$clog2(MEM_PORTS > 1 ? MEM_PORTS : 2)-1:0] agt_rr;
    integer ai;
    reg  [31:0] agt_sel;
    reg         agt_any;
    always @(*) begin
        agt_sel = 32'd0;
        agt_any = 1'b0;
        for (ai = MEM_PORTS - 1; ai >= 0; ai = ai - 1) begin
            // scan downward from the pointer so the lowest-priority match is
            // overwritten by a higher-priority one
            if (in_to_agt[(ai + agt_rr) % MEM_PORTS]) begin
                agt_sel = (ai + agt_rr) % MEM_PORTS;
                agt_any = 1'b1;
            end
        end
    end
    assign agt_in_data  = mem_in_data[agt_sel*FLIT_WIDTH +: FLIT_WIDTH];
    assign agt_in_valid = agt_any;

    // THE AGENT MUST NEVER BLOCK MEMORY. It raises `noc_in_busy` when its RX
    // FIFO is full (noc_orchestrator: `rx_full && !in_is_sig`), and a host that
    // does not drain the mailbox leaves it full indefinitely -- holding the port
    // busy for that would stop the MEMORY flits behind it on the same link, for
    // good, because nothing clears the condition.
    //
    // So a control flit that CANNOT be delivered is accepted, dropped, and
    // reported. That trades a loss on a path nothing uses (CU_SIGNAL bypasses RX
    // and the driver does not use the mailbox) for removing an unbounded stall
    // on the path everything uses.
    //
    // Waiting one's TURN is different and still holds the port: bounded by
    // MEM_PORTS cycles, and the round-robin working as intended.
    wire agt_grant = agt_any && !agt_in_busy;   // taken this cycle
    wire agt_drop  = agt_any &&  agt_in_busy;   // cannot be taken at all

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (resetn && agt_drop) begin
            $display("%0t ERROR mag: agent RX full -- control flit DROPPED at port %0d. The mailbox is not being drained; memory on this port was kept running instead.",
                     $time, agt_sel);
        end
    end
`endif

    always @(posedge clk) begin
        if (!resetn) begin
            agt_rr <= 0;
        end
        else if (agt_any && !agt_in_busy) begin
            agt_rr <= (agt_sel + 1) % MEM_PORTS;
        end
    end

    // ---- inbound arbitration into the interlink's encapsulator -----------
    // The same round-robin as the agent's, and separate from it: a flit bound
    // for another mesh and a control flit are different consumers, and sharing
    // the arbiter would make a stalled link hold up dispatch.
    localparam integer PSEL_W = $clog2(MEM_PORTS > 1 ? MEM_PORTS : 2);
    reg  [PSEL_W-1:0] enc_rr;
    integer ei;
    reg  [31:0] enc_sel;
    reg         enc_any;
    always @(*) begin
        enc_sel = 32'd0;
        enc_any = 1'b0;
        for (ei = MEM_PORTS - 1; ei >= 0; ei = ei - 1) begin
            if (in_to_enc[(ei + enc_rr) % MEM_PORTS]) begin
                enc_sel = (ei + enc_rr) % MEM_PORTS;
                enc_any = 1'b1;
            end
        end
    end
    wire [31:0] enc_rr_nxt = (enc_sel + 32'd1) % MEM_PORTS;

    // A SKID, and the reason is measured. mag_ilink's `enc_busy` is
    // combinational in `enc_data` -- `acc_match` compares the flit's mesh, txn
    // and source against the open packet's -- so the NoC router's OWN
    // backpressure was a function of the encoder's field compare. All 24
    // failing paths of the m62+processor build started at one router's
    // `west_out_switch/out_valid` and ended at the NEXT router's block-RAM
    // enable: 11 levels, 3.111 ns, 76% of it route because the chain zig-zags
    // router -> MAG -> router -> MAG -> router across three hierarchies.
    // sb_skid's `i_ready` is `!hold_valid`, so the ports' backpressure now
    // comes off a flop and the compare starts inside the skid.
    //
    // The extra cycle costs nothing here: cross-mesh traffic is push-only and
    // synchronised on the doorbell, never on a producer going idle.
    wire                  enc_skid_rdy;
    wire [FLIT_WIDTH-1:0] enc_skid_data;
    wire                  enc_skid_valid;

    sb_skid #(.W(FLIT_WIDTH)) u_enc_skid (
        .clk(clk),
        .rst(!resetn),
        .i_valid(enc_any),
        .i_ready(enc_skid_rdy),
        .i_data(mem_in_data[enc_sel*FLIT_WIDTH +: FLIT_WIDTH]),
        .o_valid(enc_skid_valid),
        .o_ready(!enc_in_busy),
        .o_data(enc_skid_data)
    );

    assign enc_in_data  = enc_skid_data;
    assign enc_in_valid = enc_skid_valid;

    always @(posedge clk) begin
        if (!resetn) begin
            enc_rr <= 0;
        end else if (enc_any && enc_skid_rdy) begin
            // Sliced EXPLICITLY: `% MEM_PORTS` computes 32 bits, and assigning
            // that straight to enc_rr reads as a latent overflow on a counter
            // that provably cannot have one.
            enc_rr <= enc_rr_nxt[PSEL_W-1:0];
        end
    end

    // ---- outbound: which port does a flit leave from? --------------------
    wire [POS_WIDTH-1:0] agt_dy = agt_out_data[DY_LSB +: POS_WIDTH];
    wire [POS_WIDTH-1:0] inj_dy = inj_out_data[DY_LSB +: POS_WIDTH];
    integer ao, io;
    reg [31:0] agt_port, inj_port;
    always @(*) begin
        agt_port = 32'd0;                       // port 0 unless a row matches
        for (ao = 0; ao < MEM_PORTS; ao = ao + 1) begin
            if (port_y[ao] == agt_dy) begin
                agt_port = ao;
            end
        end
        inj_port = 32'd0;
        for (io = 0; io < MEM_PORTS; io = io + 1) begin
            if (port_y[io] == inj_dy) begin
                inj_port = io;
            end
        end
    end

    // =====================================================================
    // The memory ports.
    // =====================================================================
    wire [15:0] p_rd [0:MEM_PORTS-1];
    wire [15:0] p_wr [0:MEM_PORTS-1];

    genvar gp;
    generate
    for (gp = 0; gp < MEM_PORTS; gp = gp + 1) begin : g_port
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
            // The engine sees only what the demux gave it. A control flit
            // offered to this port is NOT valid here, so the engine never has
            // to know the agent exists.
            .mem_in_data(mem_in_data[gp*FLIT_WIDTH +: FLIT_WIDTH]),
            .mem_in_valid(mem_in_valid[gp] && in_is_mem[gp] && !in_is_rem[gp]),
            .mem_in_busy(eng_in_busy[gp]),
            .mem_out_data(eng_out_data[gp]),
            .mem_out_valid(eng_out_valid[gp]),
            // Held off while the agent or the interlink has the link, which the
            // engine already handles: it holds valid and data until a cycle
            // with busy low.
            .mem_out_busy(mem_out_busy[gp] || (agt_out_valid && agt_port == gp)
                                           || (inj_out_valid && inj_port == gp)),
            .mem_rd_count(p_rd[gp]), .mem_wr_count(p_wr[gp])
        );

        assign port_y[gp] = PY[POS_WIDTH-1:0];

        // Inbound busy: whichever consumer this flit belongs to. A port with a
        // control flit the agent has not taken stays busy, and the sender holds.
        // Taken, or dropped because it can never be taken -- either way the
        // port is freed, so memory behind it keeps moving. Only "not your turn
        // yet" holds, and that is bounded by the port count.
        // REMOTE FIRST, because a memory flit can now be both, and the engine
        // is not the consumer of one that is leaving this mesh.
        // The remote arm reads the SKID's ready, not the encoder's busy -- that
        // is the whole point of the skid above. `ILINK != 0` keeps the ILINK=0
        // behaviour bit-exact: there `enc_in_busy` folds to 1, the skid can
        // never drain, and without this term the first remote flit would be
        // accepted into a buffer nothing empties and silently lost instead of
        // holding the port as it does today.
        assign mem_in_busy[gp] = in_is_rem[gp]
                               ? !((enc_sel == gp) && enc_skid_rdy
                                   && (ILINK != 0))
                               : in_is_mem[gp]
                                 ? eng_in_busy[gp]
                                 : !((agt_sel == gp) && (agt_grant || agt_drop));

        // Outbound: the agent wins, then the interlink, then the engine. The
        // agent's traffic is a handful of control flits; the interlink's is a
        // burst that a far mesh is already waiting on, and it is bounded by the
        // credit the far end granted. Neither can hold the engine indefinitely.
        assign mem_out_data[gp*FLIT_WIDTH +: FLIT_WIDTH] = (
            (agt_out_valid && agt_port == gp) ? agt_out_data
            : (inj_out_valid && inj_port == gp) ? inj_out_data
            : eng_out_data[gp]
        );
        assign mem_out_valid[gp] = (
            (agt_out_valid && agt_port == gp) ? 1'b1
            : (inj_out_valid && inj_port == gp) ? 1'b1
            : eng_out_valid[gp]
        );
    end
    endgenerate

    assign agt_out_busy = mem_out_busy[agt_port];
    assign inj_out_busy = (
        mem_out_busy[inj_port]
        || (agt_out_valid && (agt_port == inj_port))
    );

    // Counters summed across ports, so the AXI-level totals stay one number
    // whatever the port count is.
    integer pc;
    always @(*) begin
        rd_sum = 16'd0; wr_sum = 16'd0;
        for (pc = 0; pc < MEM_PORTS; pc = pc + 1) begin
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
    // ONE ENGINE. The converting move used to be a second engine muxed onto this
    // channel; the slot now sits on the mover's own read-return path, so the mux
    // is gone and the converged arbiter sees one requester fewer -- which is the
    // direction slack went at Gate 0 (+0.088 -> -0.372 for one EXTRA requester
    // at two ports).
    wire [ID_W-1:0]   mv_awid, mv_arid;
    wire [ADDR_W-1:0] mv_awaddr, mv_araddr;
    wire [7:0]        mv_awlen, mv_arlen;
    wire [2:0]        mv_awsize, mv_arsize;
    wire [1:0]        mv_awburst, mv_arburst;
    wire              mv_awvalid, mv_wvalid, mv_wlast, mv_arvalid;
    wire [DATA_W-1:0] mv_wdata;
    wire [DATA_W/8-1:0] mv_wstrb;
    wire              mv_bready, mv_rready;

    // The aux config window is split at offset 0x80: below is the mover's, at
    // or above is the interlink's. At ILINK=0 the gate is a constant and the
    // mover sees every write, as it always has -- and the offsets above 0x50
    // it ignored then are the ones the interlink claims now.
    wire host_cfg_mine = mv_cfg_en && ((ILINK == 0) || !mv_cfg_addr[7]);
    // Gated on the PARAMETER, not the port: every existing top leaves pe_cfg_en
    // unconnected, and an unconnected input is Z -- which made the mux X and the
    // mover silently take no descriptor at all.
    wire pe_cfg_live   = (CTRL_PE != 0) && pe_cfg_en;
    wire mv_cfg_mine   = pe_cfg_live || host_cfg_mine;
    wire [7:0]  mv_cfg_a = pe_cfg_live ? pe_cfg_addr : mv_cfg_addr;
    wire [63:0] mv_cfg_d = pe_cfg_live ? pe_cfg_data : mv_cfg_data;

    // ---- the transform slot, on the mover's read-return path -------------
    wire                x_req, x_gnt, x_start, x_bv, x_done;
    wire [XID_W-1:0]    x_id;
    wire [XMODE_W-1:0]  x_mode;
    wire [DATA_W-1:0]   x_beat, x_w0, x_w1, x_w2, x_w3;

    mm_mover #(.DATA_W(DATA_W), .ADDR_W(ADDR_W), .ID_W(ID_W),
               .XID_W(XID_W), .XMODE_W(XMODE_W),
               .XF_IN_BITS(XFORM_IN_BITS),
               .XF_OUT_WORDS(XFORM_OUT_WORDS)) u_mover (
        .clk(clk), .resetn(resetn),
        .cfg_en(mv_cfg_mine), .cfg_addr(mv_cfg_a), .cfg_data(mv_cfg_d),
        .stat_busy(mv_busy), .stat_fault(mv_fault), .stat_done(mv_done),
        .m_awid(mv_awid), .m_awaddr(mv_awaddr), .m_awlen(mv_awlen),
        .m_awsize(mv_awsize), .m_awburst(mv_awburst),
        .m_awvalid(mv_awvalid), .m_awready(mvx_awready),
        .m_wdata(mv_wdata), .m_wstrb(mv_wstrb), .m_wlast(mv_wlast),
        .m_wvalid(mv_wvalid), .m_wready(mvx_wready),
        .m_bid(m_bid[MV*ID_W +: ID_W]), .m_bresp(mvx_bresp),
        .m_bvalid(mvx_bvalid), .m_bready(mv_bready),
        .m_arid(mv_arid), .m_araddr(mv_araddr), .m_arlen(mv_arlen),
        .m_arsize(mv_arsize), .m_arburst(mv_arburst),
        .m_arvalid(mv_arvalid), .m_arready(m_arready[MV]),
        .m_rid(m_rid[MV*ID_W +: ID_W]), .m_rdata(m_rdata[MV*DATA_W +: DATA_W]),
        .m_rresp(m_rresp[MV*2 +: 2]), .m_rlast(m_rlast[MV]),
        .m_rvalid(m_rvalid[MV]), .m_rready(mv_rready),
        .x_req(x_req), .x_gnt(x_gnt), .x_start(x_start),
        .x_id(x_id), .x_mode(x_mode),
        .x_beat(x_beat), .x_beat_valid(x_bv),
        .x_done(x_done), .x_w0(x_w0), .x_w1(x_w1), .x_w2(x_w2), .x_w3(x_w3)
    );

    mag_xform #(.DATA_W(DATA_W), .NREQ(1), .SLOTS(XFORM_SLOTS),
                .ID_W(XID_W), .MODE_W(XMODE_W),
                .IN_BITS(XFORM_IN_BITS), .OUT_WORDS(XFORM_OUT_WORDS))
    u_xform (
        .clk(clk), .rst(!resetn),
        .req(x_req), .gnt(x_gnt),
        .start(x_start), .id(x_id), .mode(x_mode),
        .beat(x_beat), .beat_valid(x_bv),
        .done(x_done), .word0(x_w0), .word1(x_w1), .word2(x_w2), .word3(x_w3),
        // Gated on the PARAMETER, not the port: every top that predates the
        // processor leaves pe_xcfg_en unconnected, and an unconnected input is
        // Z -- which would make the write strobe X and clear a fault at random.
        .cfg_en((CTRL_PE != 0) && pe_xcfg_en), .cfg_id(pe_xcfg_id),
        .cfg_addr(pe_xcfg_addr), .cfg_data(pe_xcfg_data),
        .cfg_rdata(pe_xcfg_rdata), .fault(xf_fault)
    );

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
            .cfg_en(mv_cfg_en), .cfg_addr(mv_cfg_addr), .cfg_data(mv_cfg_data),
            .stat_sel(il_stat_sel), .stat_q(il_stat_q), .my_mesh(il_mesh),

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

            .enc_data(enc_in_data), .enc_valid(enc_in_valid),
            .enc_busy(enc_in_busy),
            .inj_data(inj_out_data), .inj_valid(inj_out_valid),
            .inj_busy(inj_out_busy),

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
            .bad_remote_req(|rq_rem)
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

        assign il_mesh       = MESH_ID[1:0];
        assign il_stat_q     = 64'd0;
        assign enc_in_busy   = 1'b1;
        assign inj_out_data  = {FLIT_WIDTH{1'b0}};
        assign inj_out_valid = 1'b0;

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

    // ---- the control processor's channel ----------------------------------
    generate
    if (CTRL_PE != 0) begin : g_ctrl_pe
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
    end else begin : g_no_ctrl_pe
        assign cp_awready = 1'b0;
        assign cp_wready  = 1'b0;
        assign cp_bvalid  = 1'b0;
        assign cp_arready = 1'b0;
        assign cp_rdata   = {DATA_W{1'b0}};
        assign cp_rlast   = 1'b0;
        assign cp_rvalid  = 1'b0;
    end
    endgenerate

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
                    .ID_W(ID_W)) u_dram (
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
