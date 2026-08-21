// khd_mul -- one registered signed multiplier, with the primitive NAMED.
//
// It exists as its own module for one reason: `use_dsp` takes a string LITERAL,
// not a parameter, so choosing between a DSP48 and fabric has to be a generate
// somewhere. Doing it once here keeps the choice out of khd_lane's datapath and
// makes `MUL_PRIM` a real configuration-matrix row rather than a synthesis
// guess -- which matters because the two answers are far apart: a DSP column is
// the cheap resource on this device (the vector core uses 12.5% of them against
// 37% of an SLR's LUTs) while LUT is the binding one.
//
// The output register is the DSP's own MREG when the tool takes it, so the
// latency is one cycle either way and the lane's timing does not move with the
// primitive.

`default_nettype none

module khd_mul #(
    parameter integer AW      = 17,
    parameter integer BW      = 17,
    parameter integer PW      = 34,
    parameter         USE_DSP = "yes"
)(
    input  wire                    clk,
    input  wire                    en,
    input  wire signed [AW-1:0]    a,
    input  wire signed [BW-1:0]    b,
    output wire signed [PW-1:0]    p
);
    generate
    if (USE_DSP == "yes") begin : g_dsp
        (* use_dsp = "yes" *) reg signed [PW-1:0] r;
        always @(posedge clk) if (en) r <= a * b;
        assign p = r;
    end else begin : g_lut
        (* use_dsp = "no" *) reg signed [PW-1:0] r;
        always @(posedge clk) if (en) r <= a * b;
        assign p = r;
    end
    endgenerate

endmodule

`default_nettype wire
