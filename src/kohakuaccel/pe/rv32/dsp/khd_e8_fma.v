// khd_e8_fma -- one E8M15 fused multiply-add: the shipped vector-core lane with
// everything except the FMA made unreachable.
//
// `op` is tied to OP_FMA, so constant propagation removes the four
// transcendental seeds, their tables, the Horner path and the exp2 range
// reduction. NOTHING IN vec_alu IS MODIFIED -- it is another project's shipped,
// verified module and this only instantiates it. What a stripped lane costs is
// then a measurement rather than a subtraction done on paper.
//
// Registered on both sides so an out-of-context run measures the lane and not
// its pins. Latency is vec_alu's 14 plus those two registers; II is 1.

`default_nettype none

module khd_e8_fma (
    input  wire        clk,
    input  wire        rst,

    input  wire        in_valid,
    input  wire [23:0] a,          // S1 E8 M15
    input  wire [23:0] b,
    input  wire [23:0] c,          // the addend: a*b + c

    output reg         out_valid,
    output reg  [23:0] out
);
    localparam [4:0] OP_FMA = 5'd6;

    reg        v_q;
    reg [23:0] a_q, b_q, c_q;
    always @(posedge clk) begin
        v_q <= in_valid;
        a_q <= a;
        b_q <= b;
        c_q <= c;
    end

    wire        alu_v;
    wire [23:0] alu_o;

    vec_alu #(.MODEL(0)) u_alu (
        .clk(clk), .rst(rst),
        .in_valid(v_q), .op(OP_FMA),
        .a(a_q), .b(b_q), .c(c_q),
        .out_valid(alu_v), .out(alu_o), .out_pred()
    );

    always @(posedge clk) begin
        out_valid <= alu_v;
        out       <= alu_o;
    end

endmodule

`default_nettype wire
