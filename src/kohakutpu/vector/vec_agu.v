// The strided address generator: 8 descriptors, each base + 4 (stride, bound).
//
//   addr = base + offset + sum_i ( idx_i * stride_i ),   idx_i < bound_i
//
// docs/compute/vector-core.md s10 is why this exists at all: reshape, permute,
// expand, pad, slice and broadcast are VIEWS, not arithmetic. A permute is a
// permutation of the stride list, a broadcast is stride 0, pad and slice are a
// bound and an offset. Without strides every one of them is a physical copy.
//
// One walker serves the whole core -- a descriptor is selected per instruction,
// not per lane -- so the four multipliers are paid once, not sixteen times.
// `addr` is combinational from the live counters; the consumer registers it.
// A bound of 0 reads as 1, so an unused dimension needs no special encoding.

`default_nettype none

module vec_agu (
    input  wire               clk,
    input  wire               rst,

    input  wire               wr_en,
    input  wire [2:0]         wr_ad,
    input  wire [2:0]         wr_fld,     // 0 = base, 1..4 = dim 0..3
    input  wire [33:0]        wr_val,     // dim: {stride[17:0], bound[15:0]}

    input  wire               start,
    input  wire [2:0]         sel,
    input  wire signed [17:0] off,
    input  wire               step,

    output wire [33:0]        addr,
    output wire               last,
    output wire [31:0]        total,
    output reg                busy
);
    reg [33:0] base  [0:7];
    reg [17:0] strd  [0:7][0:3];
    reg [15:0] bnd   [0:7][0:3];

    reg [15:0] idx0, idx1, idx2, idx3;
    reg [2:0]  cur;

    integer d, k;
    always @(posedge clk) begin
        if (rst) begin
            for (d = 0; d < 8; d = d + 1) begin
                base[d] <= 34'd0;
                for (k = 0; k < 4; k = k + 1) begin
                    strd[d][k] <= 18'd0;
                    bnd[d][k]  <= 16'd1;
                end
            end
        end else if (wr_en) begin
            if (wr_fld == 3'd0) base[wr_ad] <= wr_val;
            else if (wr_fld <= 3'd4) begin
                strd[wr_ad][wr_fld - 3'd1] <= wr_val[33:16];
                bnd[wr_ad][wr_fld - 3'd1]  <= wr_val[15:0];
            end
        end
    end

    wire [15:0] b0 = (bnd[cur][0] == 16'd0) ? 16'd1 : bnd[cur][0];
    wire [15:0] b1 = (bnd[cur][1] == 16'd0) ? 16'd1 : bnd[cur][1];
    wire [15:0] b2 = (bnd[cur][2] == 16'd0) ? 16'd1 : bnd[cur][2];
    wire [15:0] b3 = (bnd[cur][3] == 16'd0) ? 16'd1 : bnd[cur][3];

    wire signed [33:0] p0 = $signed({18'd0, idx0}) * $signed(strd[cur][0]);
    wire signed [33:0] p1 = $signed({18'd0, idx1}) * $signed(strd[cur][1]);
    wire signed [33:0] p2 = $signed({18'd0, idx2}) * $signed(strd[cur][2]);
    wire signed [33:0] p3 = $signed({18'd0, idx3}) * $signed(strd[cur][3]);

    assign addr  = base[cur] + {{16{off[17]}}, off} + p0 + p1 + p2 + p3;
    assign last  = (idx0 == b0 - 16'd1) && (idx1 == b1 - 16'd1)
                && (idx2 == b2 - 16'd1) && (idx3 == b3 - 16'd1);
    assign total = ({16'd0, b0} * {16'd0, b1}) * ({16'd0, b2} * {16'd0, b3});

    // Ripple carry across the four counters. Written as nested else-branches so
    // exactly one dimension advances and every lower one clears.
    always @(posedge clk) begin
        if (rst) begin
            idx0 <= 16'd0; idx1 <= 16'd0; idx2 <= 16'd0; idx3 <= 16'd0;
            cur  <= 3'd0;  busy <= 1'b0;
        end else if (start) begin
            idx0 <= 16'd0; idx1 <= 16'd0; idx2 <= 16'd0; idx3 <= 16'd0;
            cur  <= sel;   busy <= 1'b1;
        end else if (step && busy) begin
            if (last) busy <= 1'b0;
            else if (idx0 + 16'd1 < b0) idx0 <= idx0 + 16'd1;
            else begin
                idx0 <= 16'd0;
                if (idx1 + 16'd1 < b1) idx1 <= idx1 + 16'd1;
                else begin
                    idx1 <= 16'd0;
                    if (idx2 + 16'd1 < b2) idx2 <= idx2 + 16'd1;
                    else begin
                        idx2 <= 16'd0;
                        idx3 <= idx3 + 16'd1;
                    end
                end
            end
        end
    end

endmodule

`default_nettype wire
