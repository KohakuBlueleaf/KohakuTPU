// N register stages on a surface, both wires. Pure registers: the shape an SLR
// crossing wants on each side of the SLL, and what makes the surface's length
// a number the credits absorb rather than a timing path.

`default_nettype none

module kts_pipe #(
    parameter integer W    = 288,
    parameter integer VCW  = 1,
    parameter integer CN_W = 4,
    parameter integer N    = 2                 // 0 = wires
)(
    input  wire              clk,
    input  wire              rst,

    input  wire              i_valid,
    input  wire [VCW-1:0]    i_vc,
    input  wire              i_last,
    input  wire [W-1:0]      i_flit,
    output wire              o_valid,
    output wire [VCW-1:0]    o_vc,
    output wire              o_last,
    output wire [W-1:0]      o_flit,

    input  wire              i_crd_valid,
    input  wire [VCW-1:0]    i_crd_vc,
    input  wire [CN_W-1:0]   i_crd_n,
    output wire              o_crd_valid,
    output wire [VCW-1:0]    o_crd_vc,
    output wire [CN_W-1:0]   o_crd_n
);
    localparam integer FW = 1 + VCW + 1 + W;
    localparam integer BW = 1 + VCW + CN_W;

    wire [FW-1:0] f_in = {i_valid, i_vc, i_last, i_flit};
    wire [BW-1:0] b_in = {i_crd_valid, i_crd_vc, i_crd_n};
    wire [FW-1:0] f_out;
    wire [BW-1:0] b_out;

    generate if (N == 0) begin : g_wire
        assign f_out = f_in;
        assign b_out = b_in;
    end else begin : g_reg
        // srl_style: left alone a shift chain infers an SRL, which is a LUT
        // site and puts every stage in ONE of them -- so the crossing this
        // carrier exists to pipeline ends up with no pipelining at all.
        (* srl_style = "register" *) reg [FW-1:0] f_q [0:N-1];
        (* srl_style = "register" *) reg [BW-1:0] b_q [0:N-1];
        integer s;
        always @(posedge clk) begin
            for (s = 0; s < N; s = s + 1) begin
                if (rst) begin
                    f_q[s][FW-1] <= 1'b0;
                    b_q[s][BW-1] <= 1'b0;
                end
                else begin
                    f_q[s][FW-1] <= (s == 0) ? f_in[FW-1] : f_q[s-1][FW-1];
                    b_q[s][BW-1] <= (s == 0) ? b_in[BW-1] : b_q[s-1][BW-1];
                end
                f_q[s][FW-2:0] <= (s == 0) ? f_in[FW-2:0] : f_q[s-1][FW-2:0];
                b_q[s][BW-2:0] <= (s == 0) ? b_in[BW-2:0] : b_q[s-1][BW-2:0];
            end
        end
        assign f_out = f_q[N-1];
        assign b_out = b_q[N-1];
    end endgenerate

    assign {o_valid, o_vc, o_last, o_flit}   = f_out;
    assign {o_crd_valid, o_crd_vc, o_crd_n} = b_out;

endmodule

`default_nettype wire
