// khs_dot_probe -- does moving vdot's sum and accumulate INTO the DSP48 column
// pay for itself? A probe, not a datapath: it is never instantiated by a PE.
//
// The lane today multiplies in DSP48s and then leaves the column -- `sum_r`
// (p0+p1+hi) and `acc` (acc +- dot) are both fabric, and the census puts them
// at 792 and 1,541 LUT across eight lanes. A DSP48E2's post-adder and P
// register are exactly those two operations, with PCIN/PCOUT to chain them.
//
// FORM 0 is what ships: registered products, fabric sum, fabric accumulator.
// FORM 1 writes the whole dot-accumulate as ONE expression so the tool infers
// a MACC chain, and exposes the individual products separately because `vmul`
// needs them -- which is the reason the column is left in the first place, and
// the cost this probe is really measuring.
//
//   vivado -mode batch -source ooc_dot_probe.tcl -tclargs <FORM>

`default_nettype none

module khs_dot_probe #(
    parameter integer FORM  = 0,
    parameter integer LANES = 8
)(
    input  wire        clk,
    input  wire        rst,
    input  wire        en,
    input  wire        clr,
    input  wire        neg,
    input  wire [32*LANES-1:0] a,
    input  wire [32*LANES-1:0] b,
    output wire [32*LANES-1:0] acc_o,
    output wire [32*LANES-1:0] mul_lo_o
);
    genvar L;
    generate
    for (L = 0; L < LANES; L = L + 1) begin : g_lane
        wire signed [16:0] a0 = {a[32*L+15], a[32*L    +: 16]};
        wire signed [16:0] b0 = {b[32*L+15], b[32*L    +: 16]};
        wire signed [16:0] a1 = {a[32*L+31], a[32*L+16 +: 16]};
        wire signed [16:0] b1 = {b[32*L+31], b[32*L+16 +: 16]};

        if (FORM == 0) begin : g_fabric
            (* use_dsp = "yes" *) reg signed [33:0] p0, p1;
            always @(posedge clk) if (en) begin p0 <= a0*b0; p1 <= a1*b1; end

            reg signed [33:0] sum_r;
            always @(posedge clk) begin
                sum_r <= p0 + p1;
            end

            reg [31:0] acc;
            always @(posedge clk) begin
                if (clr) begin
                    acc <= 32'd0;
                end
                else if (en) begin
                    acc <= neg ? (acc - sum_r[31:0]) : (acc + sum_r[31:0]);
                end
            end
            assign acc_o[32*L +: 32]    = acc;
            assign mul_lo_o[32*L +: 32] = {p1[15:0], p0[15:0]};
        end else begin : g_macc
            // One expression, so the post-adder and P register stay inside the
            // column instead of being rebuilt in LUTs.
            (* use_dsp = "yes" *) reg signed [33:0] acc;
            always @(posedge clk) begin
                if (clr) begin
                    acc <= 34'sd0;
                end
                else if (en) begin
                    acc <= neg ? (acc - (a0*b0 + a1*b1))
                               : (acc + (a0*b0 + a1*b1));
                end
            end

            // vmul still needs the individual low halves, and THIS is the
            // duplicate the column costs: two more multipliers per lane.
            (* use_dsp = "yes" *) reg signed [33:0] q0, q1;
            always @(posedge clk) if (en) begin q0 <= a0*b0; q1 <= a1*b1; end

            assign acc_o[32*L +: 32]    = acc[31:0];
            assign mul_lo_o[32*L +: 32] = {q1[15:0], q0[15:0]};
        end
    end
    endgenerate

    wire _unused = &{1'b0, rst, 1'b0};

endmodule

`default_nettype wire
