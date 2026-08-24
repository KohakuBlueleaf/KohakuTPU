// sn_hub -- the system node's NoC hub. THE NODE HAS PORTS; NOTHING INSIDE IT
// DOES. MAG and the control processor are a division of DESIGN, not of
// component -- MAG alone needs a host round trip to start work, the processor
// alone cannot reach memory or another mesh -- so neither gets a fabric
// attachment. Both are clients here.
//
//        MAG engines ─┐
//        MAG agent    ├─  sn_hub  ─┬─ port 0 .. PORTS-1
//        interlink    │            │
//        ctrl PE     ─┘            └─
//
// Inbound claims in order -- remote, processor, memory, agent -- because one
// flit can satisfy two tests: a MEMORY flit may also be remote, and the engine
// is not the consumer of one leaving this mesh.
//
// Outbound steers by DESTINATION ROW so dispatch spreads across every port.
// Priority agent > ctrl PE > interlink > engine: the first two are a handful of
// control flits against a stream of operand words, and a stalled dispatch
// stalls the graph, while the interlink's burst is already bounded by credit.
//
// THE PROCESSOR'S COORDINATE (0,0) IS DERIVED, NOT CHOSEN. Routers occupy
// (1..NX, 1..NY) with edge endpoints just outside; a CORNER touches no router,
// and gen_mesh rejects a non-empty one. So (0,0) is free in every mesh of every
// shape, and X-then-Y delivers a flit for it westward onto that row's port.

`default_nettype none

module sn_hub #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    // The node's port count. THIS is the "configurable amount of port".
    parameter integer PORTS      = 1,
    parameter integer ILINK      = 0,
    // PARAMETERS, not a port: a port's mesh row is a build-time constant that
    // the outbound steer compares against a flit field. As a wire it cannot
    // fold across the boundary, and that cost 155 LUT.
    parameter integer MEM_Y      = 1,
    parameter integer MEM_Y1     = 3,
    parameter integer MEM_Y2     = 4,
    parameter integer MEM_Y3     = 5
)(
    input  wire clk,
    input  wire resetn,

    // ---- the node's fabric attachments. The only ones that exist. ---------
    input  wire [PORTS*FLIT_WIDTH-1:0] mem_in_data,
    input  wire [PORTS-1:0]            mem_in_valid,
    output wire [PORTS-1:0]            mem_in_busy,
    output wire [PORTS*FLIT_WIDTH-1:0] mem_out_data,
    output wire [PORTS-1:0]            mem_out_valid,
    input  wire [PORTS-1:0]            mem_out_busy,

    // This mesh's id, for the misrouted-request check.
    input  wire [1:0]                  my_mesh,

    // ---- client: the per-port memory engines ------------------------------
    // The engine sees the port's flit and a valid the demux has already
    // qualified, so it never has to know the other clients exist.
    output wire [PORTS-1:0]            eng_rx_valid,
    input  wire [PORTS-1:0]            eng_rx_busy,
    input  wire [PORTS*FLIT_WIDTH-1:0] eng_tx_data,
    input  wire [PORTS-1:0]            eng_tx_valid,
    output wire [PORTS-1:0]            eng_tx_busy,

    // ---- client: the control agent (one input, one output) ----------------
    output wire [FLIT_WIDTH-1:0]       agt_rx_data,
    output wire                        agt_rx_valid,
    input  wire                        agt_rx_busy,
    input  wire [FLIT_WIDTH-1:0]       agt_tx_data,
    input  wire                        agt_tx_valid,
    output wire                        agt_tx_busy,

    // ---- client: the control processor ------------------------------------
    output wire [FLIT_WIDTH-1:0]       pe_rx_data,
    output wire                        pe_rx_valid,
    input  wire                        pe_rx_busy,
    input  wire [FLIT_WIDTH-1:0]       pe_tx_data,
    input  wire                        pe_tx_valid,
    output wire                        pe_tx_busy,

    // ---- client: the interlink -------------------------------------------
    output wire [FLIT_WIDTH-1:0]       enc_data,
    output wire                        enc_valid,
    input  wire                        enc_busy,
    input  wire [FLIT_WIDTH-1:0]       inj_data,
    input  wire                        inj_valid,
    output wire                        inj_busy,

    // A request naming another mesh on a flit NOT marked remote -- the one
    // case still aliased onto local DRAM, and the compiler's bug.
    output wire                        bad_remote_req
);
    localparam [3:0] T_MEM_RD_REQ  = 4'h0;
    localparam [3:0] T_MEM_WR_REQ  = 4'h1;
    localparam [3:0] T_MEM_WR_DATA = 4'h4;

    localparam integer TY_LSB = FLIT_WIDTH - 4*POS_WIDTH - 4;
    localparam integer DX_LSB = FLIT_WIDTH - POS_WIDTH;
    localparam integer DY_LSB = FLIT_WIDTH - 2*POS_WIDTH;
    // NOC_RSVD bit 2 marks a flit for another mesh. Zero on every flit a
    // single-mesh build produces, which is what makes one compiler serve both.
    localparam integer RS_LSB = FLIT_WIDTH - 4*POS_WIDTH - 16;
    // NOC_MEM_ADDR is [255:216], so addr[37:36] -- the mesh -- is at [253:252].
    localparam integer RQ_MESH_LSB = 252;

    // A corner. See the header: this is derived, not chosen.
    localparam [POS_WIDTH-1:0] PE_X = {POS_WIDTH{1'b0}};
    localparam [POS_WIDTH-1:0] PE_Y = {POS_WIDTH{1'b0}};

    localparam integer PSEL_W = $clog2(PORTS > 1 ? PORTS : 2);

    // ---- inbound: whose flit is this? -------------------------------------
    wire [PORTS-1:0] in_is_req, in_is_mem, in_is_rem, in_is_pe;
    wire [PORTS-1:0] to_agt, to_enc, to_pe, rq_rem;

    genvar gd;
    generate
    for (gd = 0; gd < PORTS; gd = gd + 1) begin : g_demux
        wire [3:0] ty = mem_in_data[gd*FLIT_WIDTH + TY_LSB +: 4];
        wire       rm = mem_in_data[gd*FLIT_WIDTH + RS_LSB + 2];
        wire [1:0] rq = mem_in_data[gd*FLIT_WIDTH + RQ_MESH_LSB +: 2];
        wire [POS_WIDTH-1:0] dx = mem_in_data[gd*FLIT_WIDTH + DX_LSB +: POS_WIDTH];
        wire [POS_WIDTH-1:0] dy = mem_in_data[gd*FLIT_WIDTH + DY_LSB +: POS_WIDTH];

        assign in_is_req[gd] = (ty == T_MEM_RD_REQ) || (ty == T_MEM_WR_REQ);
        assign in_is_mem[gd] = in_is_req[gd] || (ty == T_MEM_WR_DATA);
        assign in_is_rem[gd] = (ILINK != 0) && rm;
        assign in_is_pe[gd]  = (dx == PE_X) && (dy == PE_Y);

        // Remote first: a memory flit may be remote too, and the engine is not
        // the consumer of one that is leaving this mesh. The processor next,
        // so a CU_DATA burst addressed to it is not mistaken for the agent's.
        assign to_enc[gd] = mem_in_valid[gd] && in_is_rem[gd];
        assign to_pe[gd]  = mem_in_valid[gd] && !in_is_rem[gd] && in_is_pe[gd];
        assign to_agt[gd] = mem_in_valid[gd] && !in_is_rem[gd] && !in_is_pe[gd]
                            && !in_is_mem[gd];

        assign eng_rx_valid[gd] = mem_in_valid[gd] && !in_is_rem[gd]
                                  && !in_is_pe[gd] && in_is_mem[gd];

        assign rq_rem[gd] = mem_in_valid[gd] && in_is_req[gd] && !in_is_rem[gd]
                            && (rq != my_mesh);
    end
    endgenerate

    assign bad_remote_req = |rq_rem;

    // ---- three single-input clients, three round-robins --------------------
    // Separate arbiters on purpose. Sharing one would make a stalled interlink
    // hold up dispatch, and a busy processor hold up the agent.
    //
    // The pointer moves only on an ACCEPTED flit; moving it every cycle would
    // let a port lose its turn to one that had nothing to send.
    reg  [PSEL_W-1:0] agt_rr, pe_rr, enc_rr;
    reg  [31:0]       agt_sel, pe_sel, enc_sel;
    reg               agt_any, pe_any, enc_any;
    integer ai, pi, ei;

    always @(*) begin
        agt_sel = 32'd0; agt_any = 1'b0;
        // Scan DOWNWARD from the pointer so the lowest-priority match is
        // overwritten by a higher-priority one.
        for (ai = PORTS - 1; ai >= 0; ai = ai - 1) begin
            if (to_agt[(ai + agt_rr) % PORTS]) begin
                agt_sel = (ai + agt_rr) % PORTS;
                agt_any = 1'b1;
            end
        end
        pe_sel = 32'd0; pe_any = 1'b0;
        for (pi = PORTS - 1; pi >= 0; pi = pi - 1) begin
            if (to_pe[(pi + pe_rr) % PORTS]) begin
                pe_sel = (pi + pe_rr) % PORTS;
                pe_any = 1'b1;
            end
        end
        enc_sel = 32'd0; enc_any = 1'b0;
        for (ei = PORTS - 1; ei >= 0; ei = ei - 1) begin
            if (to_enc[(ei + enc_rr) % PORTS]) begin
                enc_sel = (ei + enc_rr) % PORTS;
                enc_any = 1'b1;
            end
        end
    end

    assign agt_rx_data  = mem_in_data[agt_sel*FLIT_WIDTH +: FLIT_WIDTH];
    assign agt_rx_valid = agt_any;
    assign pe_rx_data   = mem_in_data[pe_sel*FLIT_WIDTH +: FLIT_WIDTH];
    assign pe_rx_valid  = pe_any;

    // THE AGENT MUST NEVER BLOCK MEMORY. It raises busy when its RX FIFO is
    // full, and a host that does not drain the mailbox leaves it full
    // indefinitely -- holding the port for that would stop the MEMORY flits
    // behind it on the same link, for good, because nothing clears it. So a
    // control flit that CANNOT be delivered is accepted, dropped, and reported.
    //
    // THE PROCESSOR IS THE OPPOSITE CASE AND HOLDS INSTEAD. Its inbound is a
    // program image, a kick and completions; dropping a CU_DATA beat is silent
    // corruption of the program about to run. It is safe to hold because the
    // processor DRAINS ITSELF -- it is a running core with its own FIFO, not a
    // mailbox waiting on a host that may never read.
    wire agt_grant = agt_any && !agt_rx_busy;
    wire agt_drop  = agt_any &&  agt_rx_busy;

    // Sliced EXPLICITLY at the assignment: `% PORTS` computes 32 bits, and
    // assigning that straight to a pointer reads as a latent overflow.
    wire [31:0] a_nxt = (agt_sel + 32'd1) % PORTS;
    wire [31:0] p_nxt = (pe_sel + 32'd1) % PORTS;

    always @(posedge clk) begin
        if (!resetn) begin
            agt_rr <= 0;
            pe_rr  <= 0;
        end else begin
            if (agt_any && !agt_rx_busy) begin
                agt_rr <= a_nxt[PSEL_W-1:0];
            end
            if (pe_any && !pe_rx_busy) begin
                pe_rr <= p_nxt[PSEL_W-1:0];
            end
        end
    end

    // A SKID on the encapsulator, and the reason is measured. mag_ilink's
    // `enc_busy` is combinational in `enc_data` -- it compares the flit's mesh,
    // txn and source against the open packet's -- so the NoC router's OWN
    // backpressure was a function of the encoder's field compare. All 24
    // failing paths of the m62+processor build started at one router's
    // west_out_switch/out_valid and ended at the NEXT router's block-RAM
    // enable: 11 levels, 3.111 ns, 76% of it route, because the chain zig-zags
    // router -> node -> router -> node -> router across three hierarchies.
    // sb_skid's i_ready is !hold_valid, so the ports' backpressure comes off a
    // flop and the compare starts inside the skid.
    //
    // The extra cycle costs nothing: cross-mesh traffic is push-only and
    // synchronised on the doorbell, never on a producer going idle.
    wire enc_skid_rdy;

    sb_skid #(.W(FLIT_WIDTH)) u_enc_skid (
        .clk(clk),
        .rst(!resetn),
        .i_valid(enc_any),
        .i_ready(enc_skid_rdy),
        .i_data(mem_in_data[enc_sel*FLIT_WIDTH +: FLIT_WIDTH]),
        .o_valid(enc_valid),
        .o_ready(!enc_busy),
        .o_data(enc_data)
    );

    wire [31:0] enc_rr_nxt = (enc_sel + 32'd1) % PORTS;

    always @(posedge clk) begin
        if (!resetn) begin
            enc_rr <= 0;
        end else if (enc_any && enc_skid_rdy) begin
            // Sliced EXPLICITLY: `% PORTS` computes 32 bits, and assigning that
            // straight to enc_rr reads as a latent overflow on a counter that
            // provably cannot have one.
            enc_rr <= enc_rr_nxt[PSEL_W-1:0];
        end
    end

    // ---- outbound: which port does a flit leave from? ---------------------
    wire [POS_WIDTH-1:0] agt_dy = agt_tx_data[DY_LSB +: POS_WIDTH];
    wire [POS_WIDTH-1:0] pe_dy  = pe_tx_data [DY_LSB +: POS_WIDTH];
    wire [POS_WIDTH-1:0] inj_dy = inj_data   [DY_LSB +: POS_WIDTH];

    function [POS_WIDTH-1:0] row;
        input integer p;
        begin
            row = (p == 0) ? MEM_Y[POS_WIDTH-1:0]
                : (p == 1) ? MEM_Y1[POS_WIDTH-1:0]
                : (p == 2) ? MEM_Y2[POS_WIDTH-1:0]
                :            MEM_Y3[POS_WIDTH-1:0];
        end
    endfunction

    reg [31:0] agt_port, pe_port, inj_port;
    integer ao, po, io;
    always @(*) begin
        agt_port = 32'd0;                       // port 0 unless a row matches
        for (ao = 0; ao < PORTS; ao = ao + 1) begin
            if (row(ao) == agt_dy) begin
                agt_port = ao;
            end
        end
        pe_port = 32'd0;
        for (po = 0; po < PORTS; po = po + 1) begin
            if (row(po) == pe_dy) begin
                pe_port = po;
            end
        end
        inj_port = 32'd0;
        for (io = 0; io < PORTS; io = io + 1) begin
            if (row(io) == inj_dy) begin
                inj_port = io;
            end
        end
    end

    wire agt_here = agt_tx_valid;
    wire pe_here  = pe_tx_valid;
    wire inj_here = inj_valid;

    genvar gp;
    generate
    for (gp = 0; gp < PORTS; gp = gp + 1) begin : g_port
        wire a_on = agt_here && (agt_port == gp);
        wire p_on = pe_here  && (pe_port  == gp);
        wire i_on = inj_here && (inj_port == gp);

        // A PRIORITY CHAIN, left alone. Hand-encoding this into a 2-bit select
        // to "make it a real 4:1" measured 138 LUT WORSE, and narrowing the
        // port indices another 18: Vivado's own mux inference beat both.
        // agent > ctrl PE > interlink > engine
        assign mem_out_data[gp*FLIT_WIDTH +: FLIT_WIDTH] =
              a_on ? agt_tx_data
            : p_on ? pe_tx_data
            : i_on ? inj_data
            :        eng_tx_data[gp*FLIT_WIDTH +: FLIT_WIDTH];
        assign mem_out_valid[gp] = a_on || p_on || i_on || eng_tx_valid[gp];

        // The engine holds valid and data until a cycle with busy low, so
        // simply telling it the link is taken is enough.
        assign eng_tx_busy[gp] = mem_out_busy[gp] || a_on || p_on || i_on;

        // Inbound busy: whichever consumer this flit belongs to. Remote first,
        // then the processor, then the engine, then the agent -- the same order
        // the demux claims in. The remote arm reads the SKID's ready, not the
        // encoder's busy; the `ILINK != 0` term keeps an ILINK=0 build exact,
        // where enc_busy folds to 1, the skid never drains, and without it the
        // first remote flit would be accepted into a buffer nothing empties.
        assign mem_in_busy[gp] =
              in_is_rem[gp] ? !((enc_sel == gp) && enc_skid_rdy && (ILINK != 0))
            : in_is_pe[gp]  ? !((pe_sel == gp) && !pe_rx_busy)
            : in_is_mem[gp] ? eng_rx_busy[gp]
            :                 !((agt_sel == gp) && (agt_grant || agt_drop));
    end
    endgenerate

    assign agt_tx_busy = mem_out_busy[agt_port];
    // Each lower-priority client waits for every higher one aimed at its port.
    assign pe_tx_busy  = mem_out_busy[pe_port]
                       || (agt_here && (agt_port == pe_port));
    assign inj_busy    = mem_out_busy[inj_port]
                       || (agt_here && (agt_port == inj_port))
                       || (pe_here  && (pe_port  == inj_port));

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (resetn && agt_drop) begin
            $display("%0t ERROR sn_hub: agent RX full -- control flit DROPPED at port %0d. The mailbox is not being drained; memory on this port was kept running instead.",
                     $time, agt_sel);
        end
    end
`endif
endmodule

`default_nettype wire
