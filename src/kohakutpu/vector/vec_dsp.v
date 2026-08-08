// One DSP48E2 in the exact configuration all three vector-ALU DSPs use. They
// differ only in what feeds their ports, so this is one wrapper, not three:
//
//   DSP-E   PREADD=1   (A+D)*B   exponent sum and alignment shift   see s3.1
//   DSP-M   PREADD=0    A*B      significand FMA / Horner stage 2   see s3.2
//   DSP-P   PREADD=0    A*B      Horner stage 1                     see s4.3
//
// LATENCY CONTRACT -- vec_alu.v's whole pipeline is built on this:
//
//   A, B, D driven in cycle k   ->  P readable in cycle k+3
//   C, ALUMODE driven in k+1    ->  P readable in cycle k+3
//
// so an operand set targeting a result in cycle N is driven at N-3 on A/B/D and
// at N-2 on C and ALUMODE. ALUMODE travels WITH C, which is what ALUMODEREG=1
// buys: without it the mode would arrive a cycle later than the addend it
// applies to -- right on stable operands, wrong on a stream.
//
// ADREG=0 on purpose. With ADREG=1 the A/D path reaches the multiplier through
// two registers and B through one, so B arrives a cycle early and multiplies
// against the wrong operand -- the failure mx_mac.v solves the other way, with
// BREG=2. Here A/D and B are both single-registered, which keeps every port at
// the same 3-cycle depth and needs no compensation.
//
// OPMODE is fixed at X=M, Y=M, Z=C, W=0; nothing in the ALU needs the cascade
// or the P feedback. MODEL = 1 swaps the primitive for a behavioural equivalent
// with identical latency, so a failure stays attributable to either the maths
// or the DSP configuration, never both.

`default_nettype none

module vec_dsp #(
    parameter integer PREADD = 0,    // 1 = M is (A+D)*B, 0 = M is A*B
    parameter integer MODEL  = 0     // 0 = DSP48E2, 1 = behavioural
)(
    input  wire               clk,
    input  wire               rst,
    input  wire               en,

    input  wire signed [29:0] a,
    input  wire signed [17:0] b,
    input  wire signed [47:0] c,
    input  wire signed [26:0] d,
    input  wire        [3:0]  alumode,   // 0000 = C+M, 0011 = C-M

    output wire signed [47:0] p
);
    // OPMODE = { W[1:0], Z[2:0], Y[1:0], X[1:0] }
    //   X=M(01), Y=M(01)  -- both halves of the product enter the ALU
    //   Z=C(011)          -- the addend
    //   W=0(00)
    localparam [8:0] OPMODE = 9'b000_011_0101;
    // INMODE 00100 = pre-adder forms (A+D); 00000 = A alone.
    localparam [4:0] INMODE = (PREADD == 1) ? 5'b00100 : 5'b00000;

    generate
    if (MODEL == 0) begin : g_prim

        DSP48E2 #(
            .AMULTSEL((PREADD == 1) ? "AD" : "A"),
            .AREG(1), .ACASCREG(1),
            .BREG(1), .BCASCREG(1),
            .DREG(1),
            .ADREG(0),
            .MREG(1),
            .CREG(1),
            .PREG(1),
            .INMODEREG(0),
            .OPMODEREG(0),
            .ALUMODEREG(1),
            .USE_MULT("MULTIPLY"),
            .USE_SIMD("ONE48")
        ) u_dsp (
            .CLK(clk),
            .RSTA(rst), .RSTB(rst), .RSTC(rst), .RSTD(rst),
            .RSTM(rst), .RSTP(rst), .RSTALLCARRYIN(rst),
            .RSTALUMODE(rst), .RSTCTRL(rst), .RSTINMODE(rst),
            .CEA1(en), .CEA2(en), .CEB1(en), .CEB2(en),
            .CEC(en),  .CED(en),  .CEAD(en), .CEM(en), .CEP(en),
            .CECARRYIN(en), .CECTRL(en), .CEALUMODE(en), .CEINMODE(en),
            .A(a), .B(b), .C(c), .D(d),
            .PCIN(48'd0),
            .OPMODE(OPMODE), .INMODE(INMODE), .ALUMODE(alumode),
            .CARRYIN(1'b0), .CARRYINSEL(3'b000),
            .P(p), .PCOUT()
        );

    end else begin : g_model

        reg signed [29:0] a_r;
        reg signed [17:0] b_r;
        reg signed [47:0] c_r;
        reg signed [26:0] d_r;
        reg        [3:0]  am_r;
        reg signed [47:0] m_r;
        reg signed [47:0] p_r;

        // The pre-adder is 27-bit, and saying so matters: it is the width that
        // makes mx_mac's packing offset S=19 rather than 20.
        wire signed [26:0] ad = (PREADD == 1) ? (a_r[26:0] + d_r) : a_r[26:0];

        always @(posedge clk) begin
            if (rst) begin
                a_r <= 0; b_r <= 0; c_r <= 0; d_r <= 0;
                am_r <= 0; m_r <= 0; p_r <= 0;
            end else if (en) begin
                a_r  <= a;
                b_r  <= b;
                c_r  <= c;
                d_r  <= d;
                am_r <= alumode;
                m_r  <= ad * b_r;
                // ALUMODE 0011 is Z-(W+X+Y+CIN); with W=0 and CIN=0 that is C-M.
                p_r  <= (am_r == 4'b0011) ? (c_r - m_r) : (c_r + m_r);
            end
        end

        assign p = p_r;

    end
    endgenerate

endmodule

`default_nettype wire
