// Tensor descriptor walker: an N-dimensional affine address generator with
// bound axes. This is the piece that makes a convolution a memory request.
//
// See docs/compute/tensor-isa.md s3. A descriptor is a set of nested loops;
// walking it produces one (addr, valid) per step:
//
//    addr     = base + SUM over i of  idx[i] * stride[i]
//    axis[a]  = abase[a] + SUM over i where axis[i]==a of  idx[i] * astep[i]
//    valid    = for all a:  0 <= axis[a] < aext[a]
//
// `valid` low means the element is outside the tensor -- padding. The caller
// injects zeros and issues no memory request, so a padded convolution needs no
// border handling anywhere else in the machine.
//
// NO MULTIPLIERS. The obvious implementation keeps a running address and
// rewinds by stride[i]*(count[i]-1) when a counter wraps, which needs a
// multiply per dimension. Instead each dimension carries its own partial sum:
//
//    psum[i] += stride[i]   on increment
//    psum[i]  = 0           on wrap
//    addr     = base + SUM psum[i]
//
// so the products are maintained incrementally and the only arithmetic is an
// adder tree. Same trick for the axis accumulators.
//
// Dimension NDIM-1 is innermost (fastest). Loading a descriptor is one
// dimension per cycle; walking is one element per `next`.

`default_nettype none

module mx_tdesc #(
    parameter integer NDIM = 6,     // loop levels
    parameter integer AW   = 40,    // byte address width
    parameter integer CW   = 16,    // loop count width
    parameter integer SW   = 32,    // stride width (signed)
    parameter integer XW   = 16     // axis position width (signed)
)(
    input  wire            clk,
    input  wire            rst,

    // ---- descriptor load: one dimension per cycle ----------------------
    input  wire            ld_dim_en,
    input  wire [2:0]      ld_dim,
    input  wire [CW-1:0]   ld_count,      // 0 is treated as 1
    input  wire signed [SW-1:0] ld_stride,
    input  wire [1:0]      ld_axis,       // 0 = contributes to no axis
    input  wire signed [XW-1:0] ld_astep,

    input  wire            ld_hdr_en,
    input  wire [AW-1:0]   ld_base,
    input  wire [2:0]      ld_ndim,

    input  wire            ld_ax_en,
    input  wire            ld_ax_sel,     // which of the two bound axes
    input  wire signed [XW-1:0] ld_abase,
    input  wire [XW-1:0]   ld_aext,

    // ---- walk ----------------------------------------------------------
    input  wire            start,         // restart at element 0
    input  wire            next,          // advance to the next element
    output wire            active,        // a walk is in progress
    output wire            last,          // current element is the final one
    output wire            valid,         // current element is inside bounds
    output wire [AW-1:0]   addr           // current element's byte address
);
    integer i;
    genvar  g;

    // ---- descriptor storage --------------------------------------------
    reg [CW-1:0]        d_count  [0:NDIM-1];
    reg signed [SW-1:0] d_stride [0:NDIM-1];
    reg [1:0]           d_axis   [0:NDIM-1];
    reg signed [XW-1:0] d_astep  [0:NDIM-1];
    reg [AW-1:0]        d_base;
    reg [2:0]           d_ndim;
    reg signed [XW-1:0] d_abase [0:1];
    reg [XW-1:0]        d_aext  [0:1];

    always @(posedge clk) begin
        if (rst) begin
            d_base <= {AW{1'b0}};
            d_ndim <= 3'd1;
            for (i = 0; i < NDIM; i = i + 1) begin
                d_count[i]  <= {CW{1'b0}};
                d_stride[i] <= {SW{1'b0}};
                d_axis[i]   <= 2'd0;
                d_astep[i]  <= {XW{1'b0}};
            end
            d_abase[0] <= {XW{1'b0}}; d_aext[0] <= {XW{1'b0}};
            d_abase[1] <= {XW{1'b0}}; d_aext[1] <= {XW{1'b0}};
        end else begin
            if (ld_dim_en) begin
                d_count[ld_dim]  <= ld_count;
                d_stride[ld_dim] <= ld_stride;
                d_axis[ld_dim]   <= ld_axis;
                d_astep[ld_dim]  <= ld_astep;
            end
            if (ld_hdr_en) begin
                d_base <= ld_base;
                d_ndim <= ld_ndim;
            end
            if (ld_ax_en) begin
                d_abase[ld_ax_sel] <= ld_abase;
                d_aext[ld_ax_sel]  <= ld_aext;
            end
        end
    end

    // A count of 0 would make the walk never terminate; treat it as 1 so a
    // partially-initialised descriptor degenerates to a single element rather
    // than hanging the fill engine.
    wire [CW-1:0] eff_count [0:NDIM-1];
    generate
    for (g = 0; g < NDIM; g = g + 1) begin : g_cnt
        assign eff_count[g] = (d_count[g] == {CW{1'b0}}) ? {{(CW-1){1'b0}}, 1'b1}
                                                        : d_count[g];
    end
    endgenerate

    // A dimension beyond ndim is inert: it holds index 0 and never carries.
    wire [NDIM-1:0] dim_live;
    generate
    for (g = 0; g < NDIM; g = g + 1) begin : g_live
        assign dim_live[g] = (g < NDIM) && ({29'd0, d_ndim} > g[31:0]);
    end
    endgenerate

    // ---- walk state ------------------------------------------------------
    reg [CW-1:0]        idx   [0:NDIM-1];
    reg signed [SW-1:0] psum  [0:NDIM-1];   // idx[i] * stride[i], maintained
    reg signed [XW-1:0] apsum [0:NDIM-1];   // idx[i] * astep[i],  maintained
    reg                 run;

    // `at_max[i]` is the innermost-first carry chain. Written as a loop over
    // dimensions rather than a ripple through a flag, so it stays shallow --
    // NDIM is 6, which is a 6-input AND, not a 6-level chain.
    wire [NDIM-1:0] at_max;
    generate
    for (g = 0; g < NDIM; g = g + 1) begin : g_max
        assign at_max[g] = !dim_live[g] || (idx[g] == eff_count[g] - 1'b1);
    end
    endgenerate

    // carry[i] : dimension i advances this step (all inner dims are at max)
    wire [NDIM-1:0] carry;
    generate
    for (g = 0; g < NDIM; g = g + 1) begin : g_carry
        if (g == NDIM-1) assign carry[g] = 1'b1;                 // innermost
        else             assign carry[g] = &at_max[NDIM-1:g+1];
    end
    endgenerate

    assign last   = &at_max;
    assign active = run;

    always @(posedge clk) begin
        if (rst) begin
            run <= 1'b0;
            for (i = 0; i < NDIM; i = i + 1) begin
                idx[i] <= {CW{1'b0}}; psum[i] <= {SW{1'b0}}; apsum[i] <= {XW{1'b0}};
            end
        end else if (start) begin
            run <= 1'b1;
            for (i = 0; i < NDIM; i = i + 1) begin
                idx[i] <= {CW{1'b0}}; psum[i] <= {SW{1'b0}}; apsum[i] <= {XW{1'b0}};
            end
        end else if (run && next) begin
            if (last) begin
                run <= 1'b0;
            end else begin
                for (i = 0; i < NDIM; i = i + 1) begin
                    if (carry[i] && dim_live[i]) begin
                        if (at_max[i]) begin
                            idx[i]   <= {CW{1'b0}};
                            psum[i]  <= {SW{1'b0}};
                            apsum[i] <= {XW{1'b0}};
                        end else begin
                            idx[i]   <= idx[i] + 1'b1;
                            psum[i]  <= psum[i] + d_stride[i];
                            apsum[i] <= apsum[i] + d_astep[i];
                        end
                    end
                end
            end
        end
    end

    // ---- address and bounds ---------------------------------------------
    // Adder tree over the per-dimension partial sums. No multipliers: the
    // products were maintained incrementally above.
    reg signed [SW-1:0] off_sum;
    always @(*) begin
        off_sum = {SW{1'b0}};
        for (i = 0; i < NDIM; i = i + 1) off_sum = off_sum + psum[i];
    end
    assign addr = d_base + {{(AW-SW){off_sum[SW-1]}}, off_sum};

    reg signed [XW-1:0] ax_val [0:1];
    reg [1:0]           ax_ok;
    always @(*) begin
        for (i = 0; i < 2; i = i + 1) ax_val[i] = d_abase[i];
        for (i = 0; i < NDIM; i = i + 1) begin
            if (d_axis[i] == 2'd1) ax_val[0] = ax_val[0] + apsum[i];
            if (d_axis[i] == 2'd2) ax_val[1] = ax_val[1] + apsum[i];
        end
        for (i = 0; i < 2; i = i + 1)
            // extent 0 disables the axis, so a descriptor with no padding needs
            // no axis fields set at all
            ax_ok[i] = (d_aext[i] == {XW{1'b0}}) ||
                       ((ax_val[i] >= 0) && (ax_val[i] < $signed({1'b0, d_aext[i]})));
    end
    assign valid = &ax_ok;

endmodule

`default_nettype wire
