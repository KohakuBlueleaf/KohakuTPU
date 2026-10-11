// The operand crossbar on Z: lane j of `xq` is lane src(j) of `z`.
//
//   xor k, merge k   src = j ^ k
//   rot k            src = (j + k) mod 16
//   bcast k          src = k
//   none             src = j
//
// `xm`/`xk` arrive a cycle before `z`; the per-lane selects are registered
// from them, so `xq` is `z` through the network alone.
//
// XNET 0: one 16:1 select per lane.
// XNET 1: two 4:1 levels, lane j taking lane j ^ x, then lane j ^ 4y. Rot
//   settles the low lane bits first: after the first level lane p holds the
//   element bound for low bits p[1:0], so x = p[1:0] ^ (p + k)[1:0] and
//   y = j[3:2] ^ (j + k)[3:2]. 74 LUT fewer than XNET 0 with the b/c selects
//   (xcvu13p-2L, same slack at 3.0 ns).

`default_nettype none

module v2_xbar #(
    parameter integer XNET = 1,
    parameter integer W    = 24
)(
    input  wire          clk,
    input  wire [2:0]    xm,
    input  wire [3:0]    xk,
    input  wire [16*W-1:0] z,
    output reg  [16*W-1:0] xq
);
    localparam [2:0] XM_XOR = 3'd1, XM_ROT = 3'd2, XM_BC = 3'd3, XM_MRG = 3'd4;

    integer i, j;
    reg [63:0] sel;
    reg [4:0]  t;

    generate
    if (XNET == 0) begin : g_flat
        always @(posedge clk) begin
            for (i = 0; i < 16; i = i + 1) begin
                case (xm)
                    XM_XOR, XM_MRG: sel[i*4 +: 4] <= i[3:0] ^ xk;
                    XM_ROT:         sel[i*4 +: 4] <= i[3:0] + xk;
                    XM_BC:          sel[i*4 +: 4] <= xk;
                    default:        sel[i*4 +: 4] <= i[3:0];
                endcase
            end
        end
        always @(*) begin
            for (j = 0; j < 16; j = j + 1) begin
                xq[j*W +: W] = z[sel[j*4 +: 4]*W +: W];
            end
        end
    end else begin : g_two
        // sel[4i +: 2] = x, sel[4i+2 +: 2] = y.
        always @(posedge clk) begin
            for (i = 0; i < 16; i = i + 1) begin
                case (xm)
                    XM_XOR, XM_MRG: sel[i*4 +: 4] <= xk;
                    XM_ROT: begin
                        t = {1'b0, i[3:0]} + {1'b0, xk};
                        sel[i*4 +: 2]     <= i[1:0] ^ t[1:0];
                        sel[i*4 + 2 +: 2] <= i[3:2] ^ t[3:2];
                    end
                    XM_BC:          sel[i*4 +: 4] <= i[3:0] ^ xk;
                    default:        sel[i*4 +: 4] <= 4'd0;
                endcase
            end
        end
        reg [16*W-1:0] l;
        always @(*) begin
            for (j = 0; j < 16; j = j + 1) begin
                l[j*W +: W] = z[(j[3:0] ^ {2'b00, sel[j*4 +: 2]})*W +: W];
            end
            for (j = 0; j < 16; j = j + 1) begin
                xq[j*W +: W] = l[(j[3:0] ^ {sel[j*4 + 2 +: 2], 2'b00})*W +: W];
            end
        end
    end
    endgenerate

endmodule

`default_nettype wire
