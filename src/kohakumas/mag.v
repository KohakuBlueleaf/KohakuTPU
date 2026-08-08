// MAG -- Memory Access Gateway.
//
// The single point where a partition touches everything outside it: the host's
// AXI, its own memory, and its NoC mesh.
//
//   NoC port 0 ──┬─►  mag_mem_port ──► AXI master 0 ─┐
//   NoC port 1 ──┼─►  mag_mem_port ──► AXI master 1 ─┤
//        ...     │                                   ├──► memory
//   AXI slave, memory ──► upload ────► AXI master N ─┘
//   (host, FP16 in)       (quantise on the way in)
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
// the server, not the bandwidth. Each port now owns its intake queues, read
// engine, quantiser, write slots and AXI channel; see mag_mem_port.v and
// docs/mas/spec.md.
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
    parameter integer ADDR_W     = 34,
    parameter integer ID_W       = 4,
    // How many NoC memory endpoints this MAG presents, and where each sits.
    // Named per port rather than packed: a packed field is one shift away from
    // pointing a whole port at the wrong node, and it would elaborate cleanly.
    parameter integer MEM_PORTS  = 1,
    // AXI master channels: one per memory port, plus one for the host upload.
    // Declared here rather than as a body localparam because the port list
    // needs it -- do not override it.
    parameter integer MP1        = MEM_PORTS + 1,
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
    parameter integer WR_SLOTS   = 16
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

    // ---- AXI4 masters: MEM_PORTS engines, then the upload ----------------
    // Flattened, channel N being the host upload. One channel per port is the
    // point: sharing them would leave the read beats of eight clusters on one
    // AR/R pair, which is the constraint the ports exist to remove.
    output wire [MP1*ID_W-1:0]     m_awid,
    output wire [MP1*ADDR_W-1:0]   m_awaddr,
    output wire [MP1*8-1:0]        m_awlen,
    output wire [MP1*3-1:0]        m_awsize,
    output wire [MP1*2-1:0]        m_awburst,
    output wire [MP1-1:0]          m_awvalid,
    input  wire [MP1-1:0]          m_awready,
    output wire [MP1*DATA_W-1:0]   m_wdata,
    output wire [MP1*DATA_W/8-1:0] m_wstrb,
    output wire [MP1-1:0]          m_wlast,
    output wire [MP1-1:0]          m_wvalid,
    input  wire [MP1-1:0]          m_wready,
    input  wire [MP1*ID_W-1:0]     m_bid,
    input  wire [MP1*2-1:0]        m_bresp,
    input  wire [MP1-1:0]          m_bvalid,
    output wire [MP1-1:0]          m_bready,
    output wire [MP1*ID_W-1:0]     m_arid,
    output wire [MP1*ADDR_W-1:0]   m_araddr,
    output wire [MP1*8-1:0]        m_arlen,
    output wire [MP1*3-1:0]        m_arsize,
    output wire [MP1*2-1:0]        m_arburst,
    output wire [MP1-1:0]          m_arvalid,
    input  wire [MP1-1:0]          m_arready,
    input  wire [MP1*ID_W-1:0]     m_rid,
    input  wire [MP1*DATA_W-1:0]   m_rdata,
    input  wire [MP1*2-1:0]        m_rresp,
    input  wire [MP1-1:0]          m_rlast,
    input  wire [MP1-1:0]          m_rvalid,
    output wire [MP1-1:0]          m_rready,

    // ---- NoC: memory ports, flattened ------------------------------------
    input  wire [MEM_PORTS*FLIT_WIDTH-1:0] mem_in_data,
    input  wire [MEM_PORTS-1:0]            mem_in_valid,
    output wire [MEM_PORTS-1:0]            mem_in_busy,
    output wire [MEM_PORTS*FLIT_WIDTH-1:0] mem_out_data,
    output wire [MEM_PORTS-1:0]            mem_out_valid,
    input  wire [MEM_PORTS-1:0]            mem_out_busy,

    // The agent has no port of its own; it shares these -- see the share layer.

    output wire [15:0]           mem_rd_count,
    output wire [15:0]           mem_wr_count
);
    // The upload rides one channel past the engines.
    localparam integer UP = MEM_PORTS;           // its channel index

    localparam integer LSB = $clog2(DATA_W/8);

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
        .noc_out_data(agt_out_data), .noc_out_valid(agt_out_valid),
        .noc_out_busy(agt_out_busy),
        .noc_in_data(agt_in_data), .noc_in_valid(agt_in_valid),
        .noc_in_busy(agt_in_busy)
    );

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
    localparam [3:0] T_MEM_RD_REQ  = 4'h0,
                     T_MEM_WR_REQ  = 4'h1,
                     T_MEM_WR_DATA = 4'h4;
    localparam integer TY_LSB = FLIT_WIDTH - 4*POS_WIDTH - 4;
    localparam integer DY_LSB = FLIT_WIDTH - 2*POS_WIDTH;

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

    // ---- inbound: is this port's offered flit the engine's or the agent's?
    wire [MEM_PORTS-1:0] in_is_mem, in_to_agt;
    genvar gd;
    generate
    for (gd = 0; gd < MEM_PORTS; gd = gd + 1) begin : g_demux
        wire [3:0] ty = mem_in_data[gd*FLIT_WIDTH + TY_LSB +: 4];
        assign in_is_mem[gd] = (ty == T_MEM_RD_REQ) || (ty == T_MEM_WR_REQ)
                            || (ty == T_MEM_WR_DATA);
        assign in_to_agt[gd] = mem_in_valid[gd] && !in_is_mem[gd];
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
    always @(posedge clk)
        if (resetn && agt_drop)
            $display("%0t ERROR mag: agent RX full -- control flit DROPPED at port %0d. The mailbox is not being drained; memory on this port was kept running instead.",
                     $time, agt_sel);
`endif

    always @(posedge clk) begin
        if (!resetn) agt_rr <= 0;
        else if (agt_any && !agt_in_busy)
            agt_rr <= (agt_sel + 1) % MEM_PORTS;
    end

    // ---- outbound: which port does the agent's flit leave from? ----------
    wire [POS_WIDTH-1:0] agt_dy = agt_out_data[DY_LSB +: POS_WIDTH];
    integer ao;
    reg [31:0] agt_port;
    always @(*) begin
        agt_port = 32'd0;                       // port 0 unless a row matches
        for (ao = 0; ao < MEM_PORTS; ao = ao + 1)
            if (port_y[ao] == agt_dy) agt_port = ao;
    end

    // =====================================================================
    // The memory ports.
    // =====================================================================
    wire [15:0] p_rd [0:MEM_PORTS-1];
    wire [15:0] p_wr [0:MEM_PORTS-1];

    genvar gp;
    generate
    for (gp = 0; gp < MEM_PORTS; gp = gp + 1) begin : g_port
        localparam integer PX = (gp == 0) ? MEM_X : (gp == 1) ? MEM_X1 :
                                (gp == 2) ? MEM_X2 : MEM_X3;
        localparam integer PY = (gp == 0) ? MEM_Y : (gp == 1) ? MEM_Y1 :
                                (gp == 2) ? MEM_Y2 : MEM_Y3;

        mag_mem_port #(
            .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
            .DATA_W(DATA_W), .ADDR_W(ADDR_W), .ID_W(ID_W),
            .MEM_X(PX), .MEM_Y(PY), .WR_SLOTS(WR_SLOTS)
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
            .mem_in_valid(mem_in_valid[gp] && in_is_mem[gp]),
            .mem_in_busy(eng_in_busy[gp]),
            .mem_out_data(eng_out_data[gp]),
            .mem_out_valid(eng_out_valid[gp]),
            // Held off while the agent has the link, which the engine already
            // handles: it holds valid and data until a cycle with busy low.
            .mem_out_busy(mem_out_busy[gp] || (agt_out_valid && agt_port == gp)),
            .mem_rd_count(p_rd[gp]), .mem_wr_count(p_wr[gp])
        );

        assign port_y[gp] = PY[POS_WIDTH-1:0];

        // Inbound busy: whichever consumer this flit belongs to. A port with a
        // control flit the agent has not taken stays busy, and the sender holds.
        // Taken, or dropped because it can never be taken -- either way the
        // port is freed, so memory behind it keeps moving. Only "not your turn
        // yet" holds, and that is bounded by the port count.
        assign mem_in_busy[gp] = in_is_mem[gp]
                               ? eng_in_busy[gp]
                               : !((agt_sel == gp) && (agt_grant || agt_drop));

        // Outbound: the agent wins, the engine holds.
        assign mem_out_data[gp*FLIT_WIDTH +: FLIT_WIDTH] =
            (agt_out_valid && agt_port == gp) ? agt_out_data : eng_out_data[gp];
        assign mem_out_valid[gp] =
            (agt_out_valid && agt_port == gp) ? 1'b1 : eng_out_valid[gp];
    end
    endgenerate

    assign agt_out_busy = mem_out_busy[agt_port];

    // Counters summed across ports, so the AXI-level totals stay one number
    // whatever the port count is.
    integer pc;
    reg [15:0] rd_sum, wr_sum;
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
    localparam [2:0] HS_IDLE = 3'd0, HS_WR = 3'd1, HS_RD = 3'd2, HS_WQ = 3'd3;
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

    // ---- quantise on the way IN: the upload path -------------------------
    // The same conversion, moved to where the tensor is WRITTEN. A weight matrix
    // is uploaded once and read once per output tile, so paying the quantiser
    // per upload divides its cost by the number of passes and permanently halves
    // what the fetch path moves.
    //
    // The marker is on the REQUEST (address bits, HW_QUANT below), never a range
    // MAG remembers: the driver decides which tensors are pre-quantised.
    localparam [2:0] HQ_IDLE = 3'd0, HQ_FILL = 3'd1, HQ_PACK = 3'd2,
                     HQ_AW   = 3'd3, HQ_W    = 3'd4, HQ_B    = 3'd5,
                     HQ_DONE = 3'd6;
    reg [2:0]  hs;
    reg [33:0] hq_dst;      // destination of the entry being built
    reg [15:0] hq_left;     // source beats not yet accepted
    reg [3:0]  hq_bcnt;     // beats into the current entry, 0..7
    reg [1:0]  hq_wsel;     // output word on the bus
    reg        hq_blay, hq_b, q_start_h;

    wire         hq_done;
    wire [255:0] hq_w0, hq_w1, hq_w2, hq_w3;

    // The upload's OWN quantiser, not shared with the read path under a mutex:
    // the mutex was the only reason an upload had to wait for a fetch.
    mx_quant u_hquant (
        .clk(clk), .rst(!resetn),
        .start(q_start_h),
        .b_layout(hq_blay),
        .beat(sm_wdata),
        .beat_valid(sm_wvalid && sm_wready),
        .need_beat(), .done(hq_done),
        .word0(hq_w0), .word1(hq_w1), .word2(hq_w2), .word3(hq_w3)
    );

    wire [1:0]   hq_nxt = hq_wsel + 2'd1;
    wire [255:0] hq_dw  = (hq_wsel == 2'd0) ? hq_w0 : (hq_wsel == 2'd1) ? hq_w1 :
                          (hq_wsel == 2'd2) ? hq_w2 : hq_w3;
    wire [255:0] hq_dn  = (hq_nxt  == 2'd0) ? hq_w0 : (hq_nxt  == 2'd1) ? hq_w1 :
                          (hq_nxt  == 2'd2) ? hq_w2 : hq_w3;

    // One L1 entry is 4 lanes x 32 elements, whatever the bus is wide: 2048
    // bits as FP16 and 1024 bits as MXFP7. Derived, not written as 3 -- the
    // burst length is a consequence of the entry size and DATA_W.
    localparam integer P_ENTRY_BITS  = 1024;
    localparam [33:0]  P_ENTRY_BYTES = P_ENTRY_BITS / 8;             // 128
    localparam [7:0]   P_ARLEN       = P_ENTRY_BITS / DATA_W - 1;    // 3 at 256b

    // Address bits that mark an upload for quantisation. AXI carries no field
    // for this and XDMA exposes none, so the marker rides where a host can
    // always put it: the two top bits of the window, which is why the window is
    // 32 bits of the 34-bit space rather than all of it.
    localparam integer HW_QUANT = ADDR_W - 1;
    localparam integer HW_BLAY  = ADDR_W - 2;

    reg host_b;   // write response seen, still waiting for the host to take it

    assign sm_awready = (hst == HS_IDLE);
    assign sm_arready = (hst == HS_IDLE) && !sm_awvalid;

    // Host write data flows straight through to the master when granted, or
    // into the quantiser when the request asked for it. `q_start_h` gates the
    // second because mx_quant's start branch has priority over its store, so a
    // beat accepted on that cycle would be consumed and dropped.
    assign sm_wready  = ((hst == HS_WR) && (!h_wvalid || m_wready[UP])) ||
                        ((hst == HS_WQ) && (hs == HQ_FILL) && !q_start_h);
    assign sm_bid     = m_bid[UP*ID_W +: ID_W];
    assign sm_bresp   = m_bresp[UP*2 +: 2];
    // m_bready is tied high, so the slave's write response is consumed the cycle
    // it appears. Passed straight through it would be offered for exactly one
    // cycle, and a host that raises BREADY after BVALID -- legal AXI, and what a
    // pipelined master does -- would never see it, hence `host_b`. A quantising
    // upload answers once the LAST entry is in memory, not once the last source
    // beat was taken.
    assign sm_bvalid  = ((hst == HS_WR) && (h_bvalid || host_b)) ||
                        ((hst == HS_WQ) && (hs == HQ_DONE));
    assign sm_rid     = m_rid[UP*ID_W +: ID_W];
    assign sm_rdata   = h_rdata;
    assign sm_rresp   = m_rresp[UP*2 +: 2];
    assign sm_rlast   = h_rlast;
    assign sm_rvalid  = (hst == HS_RD) && h_rvalid;
    assign m_rready[UP] = (hst == HS_RD) && sm_rready;

`ifndef SYNTHESIS
    // A quantising upload converts whole entries, so a burst that is not a
    // multiple of 8 source beats leaves the last entry short and the handshake
    // waiting for beats that will never come. Silent otherwise: it presents as
    // the host AXI hanging, several modules from the cause.
    always @(posedge clk)
        if (resetn && sm_awvalid && sm_awready && sm_awaddr[HW_QUANT] &&
            (({8'd0, sm_awlen} + 16'd1) % 16'd8 != 16'd0))
            $display("%0t ERROR mag: quantising upload of %0d beats is not a whole number of entries",
                     $time, {8'd0, sm_awlen} + 16'd1);
`endif

    always @(posedge clk) begin
        if (!resetn) begin
            hst <= HS_IDLE;
            h_awvalid <= 1'b0; h_arvalid <= 1'b0; h_wvalid <= 1'b0;
            h_wlast <= 1'b0; h_awlen <= 8'd0; h_arlen <= 8'd0;
            h_awaddr <= {ADDR_W{1'b0}}; h_araddr <= {ADDR_W{1'b0}};
            h_awid <= {ID_W{1'b0}}; h_arid <= {ID_W{1'b0}};
            h_wdata <= {DATA_W{1'b0}};
            host_b <= 1'b0;
            hs <= HQ_IDLE; hq_dst <= 34'd0; hq_left <= 16'd0;
            hq_bcnt <= 4'd0; hq_wsel <= 2'd0; hq_blay <= 1'b0;
            hq_b <= 1'b0; q_start_h <= 1'b0;
        end else begin
            if (h_awvalid && m_awready[UP]) h_awvalid <= 1'b0;
            if (h_arvalid && m_arready[UP]) h_arvalid <= 1'b0;
            if (h_bvalid && (hst == HS_WQ)) hq_b <= 1'b1;

            case (hst)
            HS_IDLE: begin
                if (sm_awvalid && sm_awready && sm_awaddr[HW_QUANT]) begin
                    // Quantise as it lands. The host sends FP16 and names the
                    // MXFP7 destination; MAG issues its own write bursts, so
                    // the source and destination burst lengths are unrelated
                    // and neither side has to know the other's entry size.
                    hq_dst    <= {2'b00, sm_awaddr[ADDR_W-3:0]};
                    hq_blay   <= sm_awaddr[HW_BLAY];
                    hq_left   <= {8'd0, sm_awlen} + 16'd1;
                    hq_bcnt   <= 4'd0;
                    q_start_h <= 1'b1;
                    hs  <= HQ_FILL;
                    hst <= HS_WQ;
                end else if (sm_awvalid && sm_awready) begin
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
                if (h_bvalid) host_b <= 1'b1;
                if ((h_bvalid || host_b) && sm_bready) begin
                    host_b <= 1'b0;
                    hst <= HS_IDLE;
                end
            end

            HS_RD: if (h_rvalid && h_rlast && sm_rready) hst <= HS_IDLE;

            // One host burst becomes N entry writes. Each entry is 8 source
            // beats collected into the quantiser and 4 words written back as
            // one P_ARLEN burst, so a destination burst is 128 bytes and can
            // never cross a 4 KB boundary from a 128-byte-aligned base.
            HS_WQ: begin
                q_start_h <= 1'b0;
                case (hs)
                HQ_FILL: if (sm_wvalid && sm_wready) begin
                    hq_left <= hq_left - 16'd1;
                    if (hq_bcnt == 4'd7) begin
                        hq_bcnt <= 4'd0;
                        hs <= HQ_PACK;
                    end else hq_bcnt <= hq_bcnt + 4'd1;
                end
                HQ_PACK: if (hq_done) hs <= HQ_AW;
                HQ_AW: if (!h_awvalid) begin
                    h_awaddr  <= hq_dst[ADDR_W-1:0];
                    h_awlen   <= P_ARLEN;
                    h_awid    <= {ID_W{1'b0}};
                    h_awvalid <= 1'b1;
                    hq_wsel   <= 2'd0;
                    hs <= HQ_W;
                end
                HQ_W: begin
                    if (!h_wvalid) begin
                        h_wdata  <= hq_dw;
                        h_wlast  <= (hq_wsel == 2'd3);
                        h_wvalid <= 1'b1;
                    end else if (m_wready[UP]) begin
                        if (h_wlast) begin
                            h_wvalid <= 1'b0; h_wlast <= 1'b0;
                            hs <= HQ_B;
                        end else begin
                            hq_wsel <= hq_nxt;
                            h_wdata <= hq_dn;
                            h_wlast <= (hq_nxt == 2'd3);
                        end
                    end
                end
                HQ_B: if (h_bvalid || hq_b) begin
                    hq_b   <= 1'b0;
                    hq_dst <= hq_dst + P_ENTRY_BYTES;
                    if (hq_left == 16'd0) hs <= HQ_DONE;
                    else begin
                        q_start_h <= 1'b1;
                        hs <= HQ_FILL;
                    end
                end
                HQ_DONE: if (sm_bready) begin
                    hs  <= HQ_IDLE;
                    hst <= HS_IDLE;
                end
                default: hs <= HQ_DONE;
                endcase
            end

            default: hst <= HS_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
