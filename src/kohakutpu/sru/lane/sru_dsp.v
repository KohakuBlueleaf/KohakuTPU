// One DSP48E2 as the SRU lanes use it: P = C +/- A*B (A, B signed 17-bit), with the A and C
// registers clearable on the cycle they load, so a lane zeroes its product or
// its addend through the primitive's own reset pins instead of a LUT gate.
//
// LATENCY CONTRACT (as vec_dsp):
//   A, B, zero_a driven in cycle k           ->  P readable in cycle k+3
//   C, sub, zero_c driven in cycle k+1       ->  P readable in cycle k+3
//
// sub = 1 is ALUMODE 0011, Z - (X + Y) = C - A*B; sub = 0 is C + A*B.
// MODEL = 1 is a behavioural copy with the same registers and latency.

`default_nettype none

module sru_dsp #(
    parameter integer MODEL = 0
)(
    input  wire               clk,
    input  wire               rst,

    input  wire signed [16:0] a,
    input  wire signed [16:0] b,
    input  wire               zero_a,   // load 0 into the A register
    input  wire        [47:0] c,
    input  wire               zero_c,   // load 0 into the C register
    input  wire               sub,

    output wire        [47:0] p
);
    // OPMODE {W, Z, Y, X} = {0, C, M, M}.
    localparam [8:0] OPMODE = 9'b000_011_0101;

    generate
    if (MODEL == 0) begin : g_prim

        wire [47:0] p_w;
        DSP48E2 #(
            .AMULTSEL("A"),
            .AREG(1), .ACASCREG(1),
            .BREG(1), .BCASCREG(1),
            .DREG(0), .ADREG(0),
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
            .RSTA(zero_a), .RSTB(1'b0), .RSTC(zero_c), .RSTD(1'b0),
            .RSTM(1'b0), .RSTP(1'b0), .RSTALLCARRYIN(1'b0),
            .RSTALUMODE(rst), .RSTCTRL(1'b0), .RSTINMODE(1'b0),
            .CEA1(1'b1), .CEA2(1'b1), .CEB1(1'b1), .CEB2(1'b1),
            .CEC(1'b1), .CED(1'b0), .CEAD(1'b0), .CEM(1'b1), .CEP(1'b1),
            .CECARRYIN(1'b1), .CECTRL(1'b1), .CEALUMODE(1'b1), .CEINMODE(1'b1),
            .A({{13{a[16]}}, a}), .B({b[16], b}), .C(c), .D(27'd0),
            .PCIN(48'd0),
            .OPMODE(OPMODE), .INMODE(5'b00000),
            .ALUMODE(sub ? 4'b0011 : 4'b0000),
            .CARRYIN(1'b0), .CARRYINSEL(3'b000),
            .P(p_w), .PCOUT()
        );
        assign p = p_w;

    end else begin : g_model

        reg signed [16:0] a_r, b_r;
        reg        [47:0] c_r, m_r, p_r;
        reg               s_r;
        wire signed [33:0] m_w = a_r * b_r;

        always @(posedge clk) begin
            a_r <= zero_a ? 17'sd0 : a;
            b_r <= b;
            m_r <= {{14{m_w[33]}}, m_w};
            c_r <= zero_c ? 48'd0 : c;
            if (rst) begin
                s_r <= 1'b0;
            end else begin
                s_r <= sub;
            end
            p_r <= s_r ? (c_r - m_r) : (c_r + m_r);
        end
        assign p = p_r;

    end
    endgenerate

endmodule

`default_nettype wire
