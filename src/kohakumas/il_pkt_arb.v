// Packet-stream plumbing for the interlink: one 2:1 merge and one 1:4 split.
//
// An interlink packet is a header plus its beats, carried on two channels that
// handshake independently. Every merge and split therefore has to LOCK for the
// duration of a packet -- a mux that re-arbitrates per beat interleaves two
// packets on one stream, and the receiver, which frames by TLAST and holds one
// header, has no way to tell that happened.
//
// Both are here rather than inline in mag_switch because the switch needs three
// of them, and three hand-written copies of a per-packet lock is three chances
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
// One packet stream to one of four, chosen from the header. Output 3 is a sink
// that always accepts and discards: a packet with nowhere legal to go has to
// leave the stream, or it blocks every packet behind it for good.
module il_pkt_demux4 #(
    parameter integer LINK_W  = 288,
    parameter integer TUSER_W = 96
)(
    input  wire                 clk,
    input  wire                 resetn,

    input  wire [1:0]           sel_in,
    input  wire [TUSER_W-1:0]   i_hdr,
    input  wire                 i_hvalid,
    output wire                 i_hready,
    input  wire [LINK_W-1:0]    i_dat,
    input  wire                 i_dlast,
    input  wire                 i_dvalid,
    output wire                 i_dready,

    output wire [TUSER_W-1:0]   o_hdr,
    output wire [2:0]           o_hvalid,
    input  wire [2:0]           o_hready,
    output wire [LINK_W-1:0]    o_dat,
    output wire                 o_dlast,
    output wire [2:0]           o_dvalid,
    input  wire [2:0]           o_dready,

    output wire                 dropped
);
    reg       busy;
    reg [1:0] sel_r;

    wire [1:0] sel = busy ? sel_r : sel_in;
    wire       to_sink = (sel == 2'd3);

    assign o_hdr   = i_hdr;
    assign o_dat   = i_dat;
    assign o_dlast = i_dlast;

    assign o_hvalid[0] = !busy && i_hvalid && (sel == 2'd0);
    assign o_hvalid[1] = !busy && i_hvalid && (sel == 2'd1);
    assign o_hvalid[2] = !busy && i_hvalid && (sel == 2'd2);
    assign o_dvalid[0] =  busy && i_dvalid && (sel == 2'd0);
    assign o_dvalid[1] =  busy && i_dvalid && (sel == 2'd1);
    assign o_dvalid[2] =  busy && i_dvalid && (sel == 2'd2);

    assign i_hready = !busy && (to_sink ? 1'b1 : o_hready[sel[1:0]]);
    assign i_dready =  busy && (to_sink ? 1'b1 : o_dready[sel[1:0]]);
    assign dropped  = busy && to_sink && i_dvalid && i_dlast;

    always @(posedge clk) begin
        if (!resetn) begin
            busy <= 1'b0; sel_r <= 2'd0;
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
