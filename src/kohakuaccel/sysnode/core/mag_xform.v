// The transform slot, beside the memory read path rather than inside a port.
//
//   mem / L2 --> [ transform ] --> port  (to a NoC compute unit)
//   mem      --> [ transform ] --> L2    (pre-convert on card, once)
//
// ONE BANK PER MEMORY AGENT, not one per port, and that is structural rather
// than a workload measurement. A port's transform is fed from that port's AXI R
// channel, mag_1m converges every port master onto ONE M_AXI_DRAM, and a staged
// read never transforms (mag_mem_port.v: staging holds operand words verbatim).
// So every transformed byte comes from the one DRAM master and N transforms can
// consume one beat per cycle between them -- N-1 idle by construction, not by
// workload. Measured 4,490 LUT and 32 DSP each.
//
// SELECTION IS AN ID, NOT A BITMASK: 0 is bypass, k routes to occupant k. The
// occupants are all resident -- fabric does not reconfigure per request -- so
// XFORM_SLOTS>1 costs N occupants of area and buys a choice, not concurrency.
//
// Grant is held for a whole RUN and a requester must not issue its AXI read
// until it holds one; that is what makes it impossible for a beat to arrive
// with nowhere to go. Per-entry grant would be finer, but a port issues the
// next entry's AR while the current entry is still in the occupant, so its
// beats can land before it could re-acquire.

`default_nettype none

module mag_xform #(
    parameter integer DATA_W = 256,
    parameter integer NREQ   = 2,
    parameter integer SLOTS  = 1,
    parameter integer ID_W   = 1,
    parameter integer MODE_W = 1,
    // THE GEOMETRY CONTRACT (integrate/addon-slots.md s2): the agent's address
    // arithmetic needs the occupant's shape before the occupant has run. These
    // are what mag_mem_port's Q_ENTRY_BITS / P_ENTRY_BITS stop hardcoding.
    parameter integer IN_BITS   = 2048,
    parameter integer OUT_WORDS = 4
)(
    input  wire                    clk,
    input  wire                    rst,

    input  wire [NREQ-1:0]         req,
    output wire [NREQ-1:0]         gnt,

    input  wire [NREQ-1:0]         start,
    input  wire [NREQ*ID_W-1:0]    id,
    input  wire [NREQ*MODE_W-1:0]  mode,
    input  wire [NREQ*DATA_W-1:0]  beat,
    input  wire [NREQ-1:0]         beat_valid,

    // Broadcast; a requester qualifies these with its own `gnt`.
    output wire                    done,
    output wire [DATA_W-1:0]       word0, word1, word2, word3,

    // ---- the occupant register space ------------------------------------
    // Carried, never interpreted: which registers exist is the occupant's
    // business, exactly as `mode` is. Reached by the control processor.
    input  wire                    cfg_en,
    input  wire [ID_W-1:0]         cfg_id,
    input  wire [7:0]              cfg_addr,
    input  wire [31:0]             cfg_data,
    output wire [31:0]             cfg_rdata,
    output wire [3:0]              fault
);
    // Round-robin over a held grant: the winner stays granted while it keeps
    // `req` asserted, so a run is never split.
    reg  [NREQ-1:0] hold;
    wire            busy = |hold;
    wire            release_now = busy && !(|(hold & req));

    integer i;
    reg [NREQ-1:0] pick;
    reg [NREQ-1:0] rr;
    reg            found;
    always @(*) begin
        pick  = {NREQ{1'b0}};
        found = 1'b0;
        for (i = 0; i < NREQ; i = i + 1) begin
            if (!found && req[i] && !rr[i]) begin
                pick[i] = 1'b1;
                found   = 1'b1;
            end
        end
        if (!found) begin
            for (i = NREQ-1; i >= 0; i = i - 1) begin
                if (req[i]) begin
                    pick    = {NREQ{1'b0}};
                    pick[i] = 1'b1;
                end
            end
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            hold <= {NREQ{1'b0}};
            rr   <= {NREQ{1'b0}};
        end else if (!busy || release_now) begin
            hold <= (|req) ? pick : {NREQ{1'b0}};
            if (|req) begin
                rr <= (&(rr | pick)) ? {NREQ{1'b0}} : (rr | pick);
            end
        end
    end

    assign gnt = hold;

    // The bank sees ONE requester's stream. Registered here: the mux adds a
    // level in front of what was already the port's worst path -- the read
    // FIFO's BRAM output into the DSP control was 9 levels / 4.399 ns and set
    // the WNS on every SLR1 probe. Costs one cycle per ENTRY, not one per beat.
    reg              s_start, s_bv;
    reg [ID_W-1:0]   s_id;
    reg [MODE_W-1:0] s_mode;
    reg [DATA_W-1:0] s_beat;

    integer m;
    reg              n_start, n_bv;
    reg [ID_W-1:0]   n_id;
    reg [MODE_W-1:0] n_mode;
    reg [DATA_W-1:0] n_beat;
    always @(*) begin
        n_start = 1'b0; n_bv = 1'b0;
        n_id    = {ID_W{1'b0}};
        n_mode  = {MODE_W{1'b0}};
        n_beat  = {DATA_W{1'b0}};
        for (m = 0; m < NREQ; m = m + 1) begin
            if (hold[m]) begin
                n_start = start[m];
                n_bv    = beat_valid[m];
                n_id    = id  [m*ID_W   +: ID_W];
                n_mode  = mode[m*MODE_W +: MODE_W];
                n_beat  = beat[m*DATA_W +: DATA_W];
            end
        end
    end

    always @(posedge clk) begin
        s_beat <= n_beat;
        s_mode <= n_mode;
        s_id   <= n_id;
        if (rst) begin
            s_start <= 1'b0;
            s_bv    <= 1'b0;
        end else begin
            s_start <= n_start;
            s_bv    <= n_bv;
        end
    end

    // THE ONE MODULE THE FRAMEWORK NAMES. `xform_bank` holds the project's
    // occupants and demuxes `id` internally; the framework never names a
    // transform. A project with none instantiates the identity bank, where
    // every id is bypass, and the read path is a wire.
    xform_bank #(.DATA_W(DATA_W), .SLOTS(SLOTS), .ID_W(ID_W),
                 .MODE_W(MODE_W), .IN_BITS(IN_BITS), .OUT_WORDS(OUT_WORDS))
    u_bank (
        .clk(clk), .rst(rst),
        .start(s_start), .id(s_id), .mode(s_mode),
        .beat(s_beat), .beat_valid(s_bv),
        .need_beat(), .done(done),
        .word0(word0), .word1(word1), .word2(word2), .word3(word3),
        .cfg_en(cfg_en), .cfg_id(cfg_id), .cfg_addr(cfg_addr),
        .cfg_data(cfg_data), .cfg_rdata(cfg_rdata), .fault(fault)
    );

`ifndef SYNTHESIS
    integer w;
    always @(posedge clk) begin
        if (!rst) begin
            for (w = 0; w < NREQ; w = w + 1) begin
                if (beat_valid[w] && !hold[w]) begin
                    $display("%0t ERROR mag_xform: requester %0d presented a beat without a grant -- it is being DROPPED",
                             $time, w);
                end
            end
        end
    end
`endif
endmodule

`default_nettype wire
