// N:1 mux over W bits, built as a tree of LUT6 primitives.
//
// EXPLICIT, NOT INFERRED -- for the same reason kohaku_sdpram names its
// primitive. A 4:1 IS one LUT6 (four data + two select = six inputs), but the
// mapper only builds that when the select comes from a register: with a
// combinational select it inlines the select's own cone into every data bit
// and costs two LUT a bit. This tree is one LUT a bit at any select.
//
// KEEP 1 puts DONT_TOUCH on every LUT6, for a select shared by two register
// cones (sb_hub). At 0 the mapper may re-fold the tree, which is what a
// single-sender trunk's padded sources need to fold away.
//
// Sources are packed {N-1, ..., 0} and chosen by a binary index. N need not be
// a power of four -- the last source repeats into the padding, and a select bit
// the width does not reach is tied low, so the padded inputs are unreachable.

`default_nettype none

module kohaku_mux #(
    parameter integer W  = 8,
    parameter integer N  = 4,
    parameter integer SW = (N <= 1) ? 1 : $clog2(N),
    parameter integer KEEP = 0      // 1: DONT_TOUCH on every LUT6; 0: re-mappable
)(
    input  wire [N*W-1:0] d,
    input  wire [SW-1:0]  sel,
    output wire [W-1:0]   o
);
    localparam integer LV = (SW + 1) / 2;       // 4:1 levels
    localparam integer NP = 1 << (2 * LV);      // sources after padding
    localparam [63:0] MUX4 = 64'hFF00_F0F0_CCCC_AAAA;   // O = I[{I5,I4}]

    wire [(LV + 1) * NP * W - 1:0] nd;
    genvar i, l, b;
    generate
        for (i = 0; i < NP; i = i + 1) begin : g_in
            localparam integer SRC = (i < N) ? i : (N - 1);
            assign nd[i*W +: W] = d[SRC*W +: W];
        end
        for (l = 0; l < LV; l = l + 1) begin : g_lv
            localparam integer NOUT = NP >> (2 * (l + 1));
            for (i = 0; i < NOUT; i = i + 1) begin : g_n
                for (b = 0; b < W; b = b + 1) begin : g_b
                    if (KEEP != 0) begin : g_k
                        (* DONT_TOUCH = "yes" *)
                        LUT6 #(.INIT(MUX4)) u_m (
                            .O (nd[((l + 1)*NP + i)*W + b]),
                            .I0(nd[(l*NP + i*4 + 0)*W + b]),
                            .I1(nd[(l*NP + i*4 + 1)*W + b]),
                            .I2(nd[(l*NP + i*4 + 2)*W + b]),
                            .I3(nd[(l*NP + i*4 + 3)*W + b]),
                            .I4(sel[2*l]),
                            .I5((2*l + 1 < SW) ? sel[(2*l + 1) % SW] : 1'b0));
                    end else begin : g_f
                        LUT6 #(.INIT(MUX4)) u_m (
                            .O (nd[((l + 1)*NP + i)*W + b]),
                            .I0(nd[(l*NP + i*4 + 0)*W + b]),
                            .I1(nd[(l*NP + i*4 + 1)*W + b]),
                            .I2(nd[(l*NP + i*4 + 2)*W + b]),
                            .I3(nd[(l*NP + i*4 + 3)*W + b]),
                            .I4(sel[2*l]),
                            .I5((2*l + 1 < SW) ? sel[(2*l + 1) % SW] : 1'b0));
                    end
                end
            end
        end
    endgenerate
    assign o = nd[LV*NP*W +: W];
endmodule

`default_nettype wire
