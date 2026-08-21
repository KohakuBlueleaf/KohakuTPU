// khd_reduce -- horizontal sum and signed maximum over the 32-bit lanes.
//
// A TREE, NOT A LOOP. A reduction written as a loop carrying a value between
// iterations synthesises as exactly that serial chain -- SIMD adders deep in
// one stage -- which is the shape that cost the accumulator ~68 MHz once and is
// called out in mx_fpacc.v's header for the same reason. Here it is log2(SIMD)
// deep: three levels at SIMD 8. Nodes are indexed heap-style, leaves at
// SIMD..2*SIMD-1 and node n combining 2n and 2n+1, so the depth is structural
// rather than something the tool has to find.
//
// AND EVEN log DEPTH IS TOO MUCH FOR ONE CYCLE. Measured once the lane adder
// stopped being the limit: the max tree became the critical path at **12 logic
// levels and 3.43 ns**, five CARRY8 in series, because every node is a 32-bit
// signed compare and a mux. At PIPE = 1 the level below the root is registered,
// which halves the depth for one cycle of latency on an instruction that runs
// ONCE PER REDUCTION rather than once per element -- so it costs nothing a
// kernel can measure.

`default_nettype none

module khd_reduce #(
    parameter integer SIMD = 8,
    parameter integer PIPE = 1
)(
    input  wire                clk,
    input  wire [32*SIMD-1:0]  v,
    output wire [31:0]         sum,
    output wire [31:0]         max
);
    wire [31:0] s  [1:2*SIMD-1];
    wire [31:0] mx [1:2*SIMD-1];

    genvar i;
    generate
    for (i = 0; i < SIMD; i = i + 1) begin : g_leaf
        assign s[SIMD + i]  = v[32*i +: 32];
        assign mx[SIMD + i] = v[32*i +: 32];
    end
    // Everything below the root's own children, combinational.
    for (i = 2; i < SIMD; i = i + 1) begin : g_node
        assign s[i]  = s[2*i] + s[2*i + 1];
        assign mx[i] = ($signed(mx[2*i]) > $signed(mx[2*i + 1]))
                     ? mx[2*i] : mx[2*i + 1];
    end

    if ((PIPE != 0) && (SIMD > 2)) begin : g_pipe
        // The root, one cycle later, from registered children.
        reg [31:0] s2_r, s3_r, m2_r, m3_r;
        always @(posedge clk) begin
            s2_r <= s[2];  s3_r <= s[3];
            m2_r <= mx[2]; m3_r <= mx[3];
        end
        assign s[1]  = s2_r + s3_r;
        assign mx[1] = ($signed(m2_r) > $signed(m3_r)) ? m2_r : m3_r;
    end else begin : g_flat
        assign s[1]  = s[2] + s[3];
        assign mx[1] = ($signed(mx[2]) > $signed(mx[3])) ? mx[2] : mx[3];
    end
    endgenerate

    assign sum = s[1];
    assign max = mx[1];

endmodule

`default_nettype wire
