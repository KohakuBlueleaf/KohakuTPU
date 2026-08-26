// Leading-one search: smear, then encode. KohakuMPE's own, so nothing on the
// float path reaches into KohakuTPU for it.
//
// A `found`-flag search loop synthesises as a W-deep LUT chain, the shape
// mx_fpacc.v records as costing this design ~68 MHz once already.

`default_nettype none

module khs_lead1 #(
    parameter integer W = 40,
    // Derived, and in the parameter list because the port list needs it.
    parameter integer PW = (W > 1) ? $clog2(W) : 1
)(
    input  wire [W-1:0]  x,
    output reg  [PW-1:0] pos,
    output wire          nz
);
    reg [W-1:0] sm;
    integer i;
    always @(*) begin
        sm = x;
        sm = sm | (sm >> 1);
        sm = sm | (sm >> 2);
        sm = sm | (sm >> 4);
        sm = sm | (sm >> 8);
        sm = sm | (sm >> 16);
        sm = sm | (sm >> 32);
        pos = {PW{1'b0}};
        for (i = 0; i < W; i = i + 1) begin
            if (sm[i]) begin
                pos = i[PW-1:0];
            end
        end
    end
    assign nz = |x;

    generate
    if (W > 64) begin : g_too_wide
        khs_lead1_smear_covers_64_bits_at_most u_bad ();
    end
    endgenerate
endmodule

`default_nettype wire
