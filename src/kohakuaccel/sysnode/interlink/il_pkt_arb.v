// Packet-stream plumbing for the interlink: one 2:1 merge and one 1:4 split.
//
// An interlink packet is a header plus its beats, carried on two channels that
// handshake independently. Every merge and split therefore has to LOCK for the
// duration of a packet -- a mux that re-arbitrates per beat interleaves two
// packets on one stream, and the receiver, which frames by TLAST and holds one
// header, has no way to tell that happened.
//
// Both are here rather than inline in mag_switch because the switch needs seven
// of them, and seven hand-written copies of a per-packet lock is seven chances
// to write the deadlock this module exists to not have.

`default_nettype none

// ----------------------------------------------------------------------------
// Two packet streams into one, round-robin at packet granularity.
module il_pkt_mux2 #(
    parameter integer LINK_W  = 288,
    parameter integer TUSER_W = 96
)(
    input  wire                 clk,
    input  wire                 resetn,

    input  wire [TUSER_W-1:0]   a_hdr,
    input  wire                 a_hvalid,
    output wire                 a_hready,
    input  wire [LINK_W-1:0]    a_dat,
    input  wire                 a_dlast,
    input  wire                 a_dvalid,
    output wire                 a_dready,

    input  wire [TUSER_W-1:0]   b_hdr,
    input  wire                 b_hvalid,
    output wire                 b_hready,
    input  wire [LINK_W-1:0]    b_dat,
    input  wire                 b_dlast,
    input  wire                 b_dvalid,
    output wire                 b_dready,

    output wire [TUSER_W-1:0]   o_hdr,
    output wire                 o_hvalid,
    input  wire                 o_hready,
    output wire [LINK_W-1:0]    o_dat,
    output wire                 o_dlast,
    output wire                 o_dvalid,
    input  wire                 o_dready
);
    reg busy, sel, rr;

    // `rr` holds the loser of the last grant, so a stream that keeps offering
    // cannot take every turn.
    wire pick_b = busy ? sel : (b_hvalid && (rr || !a_hvalid));
    wire pick_a = !busy && a_hvalid && !pick_b;

    assign o_hdr    = pick_b ? b_hdr : a_hdr;
    assign o_hvalid = !busy && (pick_a || pick_b);
    assign a_hready = !busy && pick_a && o_hready;
    assign b_hready = !busy && pick_b && o_hready;

    assign o_dat    = sel ? b_dat : a_dat;
    assign o_dlast  = sel ? b_dlast : a_dlast;
    assign o_dvalid = busy && (sel ? b_dvalid : a_dvalid);
    assign a_dready = busy && !sel && o_dready;
    assign b_dready = busy &&  sel && o_dready;

    always @(posedge clk) begin
        if (!resetn) begin
            busy <= 1'b0; sel <= 1'b0; rr <= 1'b0;
        end else if (!busy) begin
            if (o_hvalid && o_hready) begin
                busy <= 1'b1;
                sel  <= pick_b;
                rr   <= !pick_b;
            end
        end else if (o_dvalid && o_dready && o_dlast) begin
            busy <= 1'b0;
        end
    end
endmodule

// ----------------------------------------------------------------------------
// One packet stream to one of N_OUT, chosen from the header. `sel_in == N_OUT`
// is a sink that always accepts and discards: a packet with nowhere legal to go
// has to leave the stream, or it blocks every packet behind it for good.
module il_pkt_demux #(
    parameter integer LINK_W  = 288,
    parameter integer TUSER_W = 96,
    // Real outputs. SEL_W is derived and must not be overridden.
    parameter integer N_OUT   = 3,
    parameter integer SEL_W   = $clog2(N_OUT + 1)
)(
    input  wire                 clk,
    input  wire                 resetn,

    input  wire [SEL_W-1:0]     sel_in,
    input  wire [TUSER_W-1:0]   i_hdr,
    input  wire                 i_hvalid,
    output wire                 i_hready,
    input  wire [LINK_W-1:0]    i_dat,
    input  wire                 i_dlast,
    input  wire                 i_dvalid,
    output wire                 i_dready,

    output wire [TUSER_W-1:0]   o_hdr,
    output wire [N_OUT-1:0]     o_hvalid,
    input  wire [N_OUT-1:0]     o_hready,
    output wire [LINK_W-1:0]    o_dat,
    output wire                 o_dlast,
    output wire [N_OUT-1:0]     o_dvalid,
    input  wire [N_OUT-1:0]     o_dready,

    output wire                 dropped
);
    reg             busy;
    reg [SEL_W-1:0] sel_r;

    wire [SEL_W-1:0] sel = busy ? sel_r : sel_in;
    wire             to_sink = (sel == N_OUT[SEL_W-1:0]);

    // The sink's index is one past the last real output, so it is forced in
    // range before it reaches one -- an out-of-range read returns x.
    wire [SEL_W-1:0] sel_q = to_sink ? {SEL_W{1'b0}} : sel;

    assign o_hdr   = i_hdr;
    assign o_dat   = i_dat;
    assign o_dlast = i_dlast;

    genvar gi;
    generate
    for (gi = 0; gi < N_OUT; gi = gi + 1) begin : o
        assign o_hvalid[gi] = !busy && i_hvalid && (sel == gi[SEL_W-1:0]);
        assign o_dvalid[gi] =  busy && i_dvalid && (sel == gi[SEL_W-1:0]);
    end
    endgenerate

    assign i_hready = !busy && (to_sink ? 1'b1 : o_hready[sel_q]);
    assign i_dready =  busy && (to_sink ? 1'b1 : o_dready[sel_q]);
    assign dropped  = busy && to_sink && i_dvalid && i_dlast;

    always @(posedge clk) begin
        if (!resetn) begin
            busy <= 1'b0; sel_r <= {SEL_W{1'b0}};
        end else if (!busy) begin
            if (i_hvalid && i_hready) begin
                busy  <= 1'b1;
                sel_r <= sel_in;
            end
        end else if (i_dvalid && i_dready && i_dlast) begin
            busy <= 1'b0;
        end
    end
endmodule

`default_nettype wire
