// mag_link_pipe -- extra register stages in one direction of an interlink.
//
// A long SLL route wants more than the single register mag_link already drives
// its outputs from, and those stages have to exist in the RTL: the placer will
// pull a register into a Laguna site, but retiming will not invent one that was
// never written.
//
// A PLAIN SHIFT REGISTER IS CORRECT HERE, and only because flow control is
// credit-based. There is no handshake to preserve, no elastic buffer, no skid --
// which is the whole return on paying for credits, and the reason inserting
// latency is free rather than a redesign.
//
// `tap` selects the depth at run time so one bench can sweep the crossing
// latency it cannot predict; tie it to DEPTH for synthesis and it folds away.

`default_nettype none

module mag_link_pipe #(
    parameter integer LINK_W  = 288,
    parameter integer TUSER_W = 96,
    parameter integer DEPTH   = 2
)(
    input  wire                 clk,
    input  wire                 resetn,
    input  wire [4:0]           tap,

    input  wire [LINK_W-1:0]    i_tdata,
    input  wire [TUSER_W-1:0]   i_tuser,
    input  wire                 i_tlast,
    input  wire                 i_tvalid,

    output wire [LINK_W-1:0]    o_tdata,
    output wire [TUSER_W-1:0]   o_tuser,
    output wire                 o_tlast,
    output wire                 o_tvalid
);
    localparam integer W = LINK_W + TUSER_W + 2;

    reg [W-1:0] stage [0:DEPTH-1];

    integer s;
    // ONLY THE VALID BIT. A Laguna register has no reset, and this module exists
    // to be placed in one -- resetting the 384-bit payload forbids that site.
    always @(posedge clk) begin
        stage[0] <= {i_tvalid, i_tlast, i_tuser, i_tdata};
        for (s = 1; s < DEPTH; s = s + 1) begin
            stage[s] <= stage[s-1];
        end
        if (!resetn) begin
            for (s = 0; s < DEPTH; s = s + 1) begin
                stage[s][W-1] <= 1'b0;
            end
        end
    end

    wire [W-1:0] sel = stage[(tap == 5'd0) ? 0 : (tap - 5'd1)];

    assign o_tdata  = sel[LINK_W-1:0];
    assign o_tuser  = sel[LINK_W +: TUSER_W];
    assign o_tlast  = sel[LINK_W + TUSER_W];
    assign o_tvalid = sel[LINK_W + TUSER_W + 1];
endmodule

`default_nettype wire
