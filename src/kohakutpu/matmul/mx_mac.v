// One DSP48E2 carrying two int7 MACs that share an activation.
//
// The pre-adder builds the packed operand, so forming it costs no fabric:
//
//     A = w_hi << 19      pure wiring
//     D = w_lo            pure wiring
//     (A + D) * B  =  w_hi*a*2^19  +  w_lo*a
//
// S = 19 is the maximum, not 20. The packed operand must fit 27 bits signed and
// the worst case w_hi = w_lo = -64 gives -64*2^20 - 64 = -67,108,928 against a
// limit of -67,108,864 -- over by exactly 64. At S = 19 the guard is 5 bits,
// which is cascade depth 32, exactly one K=32 block. See
// docs/compute/matmul-circuit.md s2.
//
// The DSP ALU computes  Z + W + X + Y, and all four slots are used:
//
//     X,Y = M      the packed product
//     Z          = PCIN, the cascade from the DSP above  (or zero at stage 0)
//     W          = C,    the partial from the previous TCU (last stage only)
//
// The cross-TCU partial is on W, not Z: on Z it would enter at stage 0, so the
// upstream TCU's result would have to be ready before this TCU starts -- 8
// cycles of operand skew per TCU. On W at the last stage the two results are
// contemporary and need two cycles of alignment (C registered, then the ALU).
//
// MODEL = 1 swaps the primitive for a behavioural equivalent with identical
// latency, so the arithmetic can be verified without unisims.

`default_nettype none

module mx_mac #(
    parameter integer S     = 19,   // packing offset
    parameter integer Z_SEL = 0,    // 0 = PCIN, 1 = zero
    parameter integer W_SEL = 0,    // 0 = zero, 1 = C
    parameter integer MODEL = 0     // 0 = DSP48E2, 1 = behavioural
)(
    input  wire               clk,
    input  wire               rst,
    input  wire               en,

    input  wire signed [6:0]  w_hi,   // weight -> upper field
    input  wire signed [6:0]  w_lo,   // weight -> lower field
    input  wire signed [6:0]  act,    // shared activation

    input  wire signed [47:0] pcin,
    input  wire signed [47:0] cin,

    output wire signed [47:0] p,
    output wire signed [47:0] pcout
);
    wire signed [29:0] a_port = $signed({{23{w_hi[6]}}, w_hi}) <<< S;
    wire signed [26:0] d_port = {{20{w_lo[6]}}, w_lo};
    wire signed [17:0] b_port = {{11{act[6]}}, act};

    // OPMODE = { W[1:0], Z[2:0], Y[1:0], X[1:0] }
    //   X=M,Y=M -> 4'b0101 ;  Z: 001 = PCIN, 000 = 0 ;  W: 11 = C, 00 = 0
    localparam [2:0] ZBITS = (Z_SEL == 0) ? 3'b001 : 3'b000;
    localparam [1:0] WBITS = (W_SEL == 1) ? 2'b11  : 2'b00;
    localparam [8:0] OPMODE = {WBITS, ZBITS, 4'b0101};

    // INMODE 00100 = pre-adder computes (A + D), A from A2, B from B2.
    localparam [4:0] INMODE  = 5'b00100;
    localparam [3:0] ALUMODE = 4'b0000;      // Z + W + X + Y + CIN

    generate
    if (MODEL == 0) begin : g_prim

        DSP48E2 #(
            .AMULTSEL("AD"),          // multiplier sees the pre-adder output
            .AREG(1), .ACASCREG(1),
            // BREG=2, not 1. With AMULTSEL="AD" the A/D path reaches the
            // multiplier through AREG *and* ADREG, so a single-stage B arrives a
            // cycle early and multiplies against the wrong operand. Stable
            // operands hide this completely; streaming does not.
            .BREG(2), .BCASCREG(2),
            .DREG(1), .ADREG(1),
            .MREG(1),
            .CREG(1),
            .PREG(1),
            .INMODEREG(0), .OPMODEREG(0), .ALUMODEREG(0),
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
            .A(a_port), .B(b_port), .C(cin), .D(d_port),
            .PCIN(pcin),
            .OPMODE(OPMODE), .INMODE(INMODE), .ALUMODE(ALUMODE),
            .CARRYIN(1'b0), .CARRYINSEL(3'b000),
            .P(p), .PCOUT(pcout)
        );

    end else begin : g_model

        // Same pipeline depth as the primitive:
        //   operand -> P  is 4 cycles   (A/D/B reg, AD reg, M reg, P reg)
        //   PCIN    -> P  is 1 cycle    (PCIN enters at the ALU, then P reg)
        //   C       -> P  is 2 cycles   (C reg, then the ALU, then P reg)
        reg signed [29:0] a_r;
        reg signed [26:0] d_r;
        reg signed [17:0] b_r, b_r2;
        reg signed [47:0] c_r;
        reg signed [26:0] ad_r;
        reg signed [44:0] m_r;
        reg signed [47:0] p_r;

        wire signed [47:0] z = (Z_SEL == 0) ? pcin : 48'sd0;
        wire signed [47:0] w = (W_SEL == 1) ? c_r  : 48'sd0;

        always @(posedge clk) begin
            if (rst) begin
                a_r <= 0; d_r <= 0; b_r <= 0; b_r2 <= 0; c_r <= 0;
                ad_r <= 0; m_r <= 0; p_r <= 0;
            end else if (en) begin
                a_r  <= a_port;
                d_r  <= d_port;
                b_r  <= b_port;
                c_r  <= cin;
                ad_r <= a_r[26:0] + d_r;    // pre-adder, 27-bit as in the DSP
                b_r2 <= b_r;
                m_r  <= ad_r * b_r2;
                p_r  <= m_r + z + w;
            end
        end

        assign p     = p_r;
        assign pcout = p_r;

    end
    endgenerate

endmodule

`default_nettype wire
