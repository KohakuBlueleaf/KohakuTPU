// khs_facc_fixed -- the OTHER float accumulator: align into a wide fixed-point
// field and add there, so the loop is an integer add.
//
// The float accumulate loop measures 25 logic levels and 152.3 MHz standalone
// (ooc_facc.tcl), which is what forces the matmul cluster's three-stage split
// and, with it, an accumulator-rotation contract for `vfmacc`. This is the
// alternative the contract exists to avoid: the alignment shift moves OUTSIDE
// the loop, and what remains inside is `acc <= acc + x` on a wide integer --
// one carry chain, the same shape tier 1's int32 accumulate already closes at.
//
// It is also EXACT: nothing rounds until the accumulator is read, so a dot
// product's error does not grow with K at all.
//
// WIDTH IS SET BY THE SOURCE FORMAT, not chosen. An FP16 product's exponent
// spans roughly -48..+30, so 78 bits of range plus the 17-bit significand plus
// guard is the field; AW = 96 covers it with room and lands on a whole number
// of CARRY8s.

`default_nettype none

module khs_facc_fixed #(
    parameter integer MW = 16,          // the product's mantissa, S1 E7 M<MW>
    parameter integer AW = 96,          // the fixed-point accumulator
    parameter integer EMIN = 15         // e7 of the smallest product worth keeping
)(
    input  wire            clk,
    input  wire            rst,
    input  wire            en,
    input  wire [MW+7:0]   p,
    output wire [AW-1:0]   acc_o
);
    wire           sgn = p[MW+7];
    wire [6:0]     e   = p[MW+6 -: 7];
    wire [MW:0]    sig = (e == 7'd0) ? {(MW+1){1'b0}} : {1'b1, p[MW-1:0]};

    // The align is a plain left shift because EMIN is the field's origin: a
    // product below it contributes nothing an FP16 result could see.
    wire [6:0]     sh  = (e <= EMIN[6:0]) ? 7'd0 : (e - EMIN[6:0]);
    wire [AW-1:0]  ext = {{(AW-MW-1){1'b0}}, sig} << sh;

    // Registered BEFORE the loop: this is the whole point -- the shifter is not
    // in the recurrence.
    reg [AW-1:0] add_q;
    reg          sgn_q;
    reg          en_q;
    always @(posedge clk) begin
        add_q <= ext;
        sgn_q <= sgn;
        en_q  <= en;
    end

    reg [AW-1:0] acc;
    always @(posedge clk) begin
        if (rst) begin
            acc <= {AW{1'b0}};
        end
        else if (en_q) begin
            acc <= sgn_q ? (acc - add_q) : (acc + add_q);
        end
    end

    assign acc_o = acc;

endmodule

`default_nettype wire
