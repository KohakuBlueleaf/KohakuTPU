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

// 40 MATCHES vec_core's default DELIBERATELY: a width differing from its
// driver's leaves cbase x, and x makes `addr` all x -- silence, not a bad addr.
module vec_agu #(
    parameter integer AW = 40
)(
    input  wire               clk,
    input  wire               rst,

    input  wire               wr_en,
    input  wire [2:0]         wr_ad,
    input  wire [2:0]         wr_fld,     // 0 = base, 1..4 = dim 0..3
    // A BASE is AW wide; a DIM is {stride[17:0], bound[15:0]} at those exact
    // bits regardless of AW, so widening the port cannot move the dim encoding.
    input  wire [AW-1:0]      wr_val,

    input  wire               start,
    input  wire [2:0]         sel,
    input  wire signed [17:0] off,
    input  wire               step,

    output wire [AW-1:0]      addr,
    output wire               last,
    output wire [31:0]        total,
    output reg                busy
);
    reg [AW-1:0] base  [0:7];
    reg [17:0] strd  [0:7][0:3];
    reg [15:0] bnd   [0:7][0:3];

    reg [15:0] idx0, idx1, idx2, idx3;
    reg [2:0]  cur;

    // 2 bits, because the array is [0:3] and the write is guarded on
    // `wr_fld <= 4`. A 3-bit `wr_fld - 1` indexes it correctly and verilator
    // still reports the width, which buries the reports that matter.
    wire [1:0] fld_sel = wr_fld[1:0] - 2'd1;

    integer d, k;
    always @(posedge clk) begin
        if (rst) begin
            for (d = 0; d < 8; d = d + 1) begin
                base[d] <= {AW{1'b0}};
                for (k = 0; k < 4; k = k + 1) begin
                    strd[d][k] <= 18'd0;
                    bnd[d][k]  <= 16'd1;
                end
            end
        end else if (wr_en) begin
            if (wr_fld == 3'd0) begin
                base[wr_ad] <= wr_val;
            end else if (wr_fld <= 3'd4) begin
                strd[wr_ad][fld_sel] <= wr_val[33:16];
                bnd[wr_ad][fld_sel]  <= wr_val[15:0];
            end
        end
    end

    // The SELECTED descriptor is latched on `start`. Reading `bnd[cur]` and
    // `strd[cur]` where they are used puts an eight-way mux between a register
    // and a multiplier input, and between a register and the address adder.
    // A walk cannot change descriptor once started, so latching costs nothing.
    reg [AW-1:0] cbase;
    reg [15:0] cb0, cb1, cb2, cb3;
    reg signed [17:0] cs0, cs1, cs2, cs3;

    wire [15:0] b0 = cb0, b1 = cb1, b2 = cb2, b3 = cb3;

    always @(posedge clk) begin
        if (rst) begin
            cbase <= {AW{1'b0}};
            cb0 <= 16'd1; cb1 <= 16'd1; cb2 <= 16'd1; cb3 <= 16'd1;
            cs0 <= 18'd0; cs1 <= 18'd0; cs2 <= 18'd0; cs3 <= 18'd0;
        end else if (start) begin
            cbase <= base[sel];
            cb0 <= (bnd[sel][0] == 16'd0) ? 16'd1 : bnd[sel][0];
            cb1 <= (bnd[sel][1] == 16'd0) ? 16'd1 : bnd[sel][1];
            cb2 <= (bnd[sel][2] == 16'd0) ? 16'd1 : bnd[sel][2];
            cb3 <= (bnd[sel][3] == 16'd0) ? 16'd1 : bnd[sel][3];
            cs0 <= strd[sel][0]; cs1 <= strd[sel][1];
            cs2 <= strd[sel][2]; cs3 <= strd[sel][3];
        end
    end

    // NO MULTIPLIERS. `idx_i * stride_i` recomputed every cycle is four
    // multiplies and a five-input add between a stride register and whatever
    // consumes the address -- measured at 178 MHz in the assembled mesh.
    // Instead each dimension carries a running partial sum, advanced by its
    // stride on increment and cleared on wrap, so the only arithmetic left is
    // an adder tree. This is the technique mx_tdesc.v already uses and the
    // reason it carries none either.
    reg signed [AW-1:0] ps0, ps1, ps2, ps3;

    assign addr  = cbase + {{(AW-18){off[17]}}, off} + ps0 + ps1 + ps2 + ps3;
    assign last  = (idx0 == b0 - 16'd1) && (idx1 == b1 - 16'd1)
                && (idx2 == b2 - 16'd1) && (idx3 == b3 - 16'd1);

    // PIPELINED, in three stages. Combinational it is three chained 32-bit
    // multiplies from the bound registers, and the sequencer compares the
    // result to decide a state -- bound register to FSM state through three
    // multipliers, which measured 129 MHz in the assembled mesh. `total` is
    // therefore valid three cycles after a descriptor is selected, and
    // vec_core.v waits those cycles before reading it.
    //
    // The partials are SATURATED to 16 bits, so the second multiply is 16x16
    // and fits one DSP rather than cascading. Any partial above 65535 exceeds
    // a legal descriptor and vec_core faults over 256, so saturating cannot
    // make an illegal walk legal. The saturate gets its own stage: shared with
    // the multiply it left vec_cu and the mesh at 19 ps, which is noise in an
    // unplaced estimate rather than margin.
    reg [31:0] t01, t23, total_r;
    reg [15:0] s01, s23;
    always @(posedge clk) begin
        // No reset: recomputed unconditionally every cycle, so it is correct one
        // cycle after release, and `total_r` was the measured PIPE_MUX endpoint.
        begin
            t01     <= {16'd0, b0} * {16'd0, b1};
            t23     <= {16'd0, b2} * {16'd0, b3};
            s01     <= (t01[31:16] != 16'd0) ? 16'hFFFF : t01[15:0];
            s23     <= (t23[31:16] != 16'd0) ? 16'hFFFF : t23[15:0];
            total_r <= s01 * s23;
        end
    end
    assign total = total_r;

    // Ripple carry across the four counters. Written as nested else-branches so
    // exactly one dimension advances and every lower one clears.
    always @(posedge clk) begin
        if (rst) begin
            idx0 <= 16'd0; idx1 <= 16'd0; idx2 <= 16'd0; idx3 <= 16'd0;
            ps0 <= {AW{1'b0}}; ps1 <= {AW{1'b0}};
            ps2 <= {AW{1'b0}}; ps3 <= {AW{1'b0}};
            cur  <= 3'd0;  busy <= 1'b0;
        end else if (start) begin
            idx0 <= 16'd0; idx1 <= 16'd0; idx2 <= 16'd0; idx3 <= 16'd0;
            ps0 <= {AW{1'b0}}; ps1 <= {AW{1'b0}};
            ps2 <= {AW{1'b0}}; ps3 <= {AW{1'b0}};
            cur  <= sel;   busy <= 1'b1;
        end else if (step && busy) begin
            if (last) begin
                busy <= 1'b0;
            end
            else if (idx0 + 16'd1 < b0) begin
                idx0 <= idx0 + 16'd1;
                ps0  <= ps0 + {{(AW-18){cs0[17]}}, cs0};
            end else begin
                idx0 <= 16'd0; ps0 <= {AW{1'b0}};
                if (idx1 + 16'd1 < b1) begin
                    idx1 <= idx1 + 16'd1;
                    ps1  <= ps1 + {{(AW-18){cs1[17]}}, cs1};
                end else begin
                    idx1 <= 16'd0; ps1 <= {AW{1'b0}};
                    if (idx2 + 16'd1 < b2) begin
                        idx2 <= idx2 + 16'd1;
                        ps2  <= ps2 + {{(AW-18){cs2[17]}}, cs2};
                    end else begin
                        idx2 <= 16'd0; ps2 <= {AW{1'b0}};
                        idx3 <= idx3 + 16'd1;
                        ps3  <= ps3 + {{(AW-18){cs3[17]}}, cs3};
                    end
                end
            end
        end
    end

endmodule

`default_nettype wire
