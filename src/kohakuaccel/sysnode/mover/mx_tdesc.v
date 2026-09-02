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
    parameter integer XW   = 16,    // axis position width (signed)
    parameter integer LOWB = 5,     // width of the low-bit test on `addr`
    // 1: the element comes from a two-entry queue the walker fills on its
    // own count, so `next` pops AW+3 flops; 0: the walker's registers direct.
    parameter integer OREG = 0
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
    output wire [AW-1:0]   addr,          // current element's byte address
    output wire            low_nz         // addr's low LOWB bits are non-zero
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
        if (g == NDIM-1) begin : g_inner
            assign carry[g] = 1'b1;
        end else begin : g_outer
            assign carry[g] = &at_max[NDIM-1:g+1];
        end
    end
    endgenerate

    // The walker's own element: presented directly at OREG 0, through the
    // queue at the end of the module at OREG 1.
    wire w_last = &at_max;
    wire step;

    // Element 0's `last`, read straight off the descriptor: at OREG 1 the
    // queue takes element 0 from `start` itself and the walker steps past it.
    wire [NDIM-1:0] at_max0;
    generate
    for (g = 0; g < NDIM; g = g + 1) begin : g_zero
        assign at_max0[g] = !dim_live[g]
                          || (eff_count[g] == {{(CW-1){1'b0}}, 1'b1});
    end
    endgenerate
    wire last0 = &at_max0;

    always @(posedge clk) begin
        // Only `run`: `start` clears the walker below at :163.
        if (rst) begin
            run <= 1'b0;
        end else if (start) begin
            run <= 1'b1;
            for (i = 0; i < NDIM; i = i + 1) begin
                idx[i] <= {CW{1'b0}}; psum[i] <= {SW{1'b0}}; apsum[i] <= {XW{1'b0}};
            end
        end else if (run && step) begin
            if (w_last) begin
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
    // BALANCED REDUCTIONS, and they have to be written out. Both of these were
    // `acc = acc + term[i]` over NDIM, which is an adder CHAIN NDIM deep, and
    // for ax_val the tool cannot re-associate it because every link carries a
    // mux. Together they were 7 of the ship top's 10 worst paths.
    //
    // Padded to a power of two so the in-place pairwise fold is ceil(log2)
    // levels: at NDIM=6 that is 3 instead of 6.
    localparam integer AN = 1 << $clog2(NDIM + 1);

    integer oi, on2, ok;
    reg signed [SW-1:0] os_t [0:AN-1];
    reg signed [SW-1:0] off_sum;
    always @(*) begin
        for (oi = 0; oi < AN;   oi = oi + 1) begin
            os_t[oi] = {SW{1'b0}};
        end
        for (oi = 0; oi < NDIM; oi = oi + 1) begin
            os_t[oi] = psum[oi];
        end
        for (on2 = AN >> 1; on2 > 0; on2 = on2 >> 1) begin
            for (ok = 0; ok < on2; ok = ok + 1) begin
                os_t[ok] = os_t[ok] + os_t[ok + on2];
            end
        end
        off_sum = os_t[0];
    end
    wire [AW-1:0] w_addr = d_base + {{(AW-SW){off_sum[SW-1]}}, off_sum};

    // An alignment test reads only the low LOWB bits, which carry nothing from
    // above: folded narrow it is one CARRY8, not a slice of the 40-bit sum.
    reg [LOWB-1:0] ls_t [0:AN-1];
    integer li, ln2, lk;
    always @(*) begin
        for (li = 0; li < AN;   li = li + 1) begin
            ls_t[li] = {LOWB{1'b0}};
        end
        for (li = 0; li < NDIM; li = li + 1) begin
            ls_t[li] = psum[li][LOWB-1:0];
        end
        for (ln2 = AN >> 1; ln2 > 0; ln2 = ln2 >> 1) begin
            for (lk = 0; lk < ln2; lk = lk + 1) begin
                ls_t[lk] = ls_t[lk] + ls_t[lk + ln2];
            end
        end
    end
    wire w_low_nz = |(d_base[LOWB-1:0] + ls_t[0]);
`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (!rst && (low_nz !== |addr[LOWB-1:0])) begin
            $display("%0t ERROR mx_tdesc: low_nz %b != |addr[%0d:0] %b (addr %h)",
                     $time, low_nz, LOWB-1, |addr[LOWB-1:0], addr);
        end
    end
`endif

    // ONE SET OF LOOP VARIABLES PER always BLOCK -- sharing them between two
    // combinational blocks is a simulation race, not a style point.
    integer xi, xn2, xk;
    reg signed [XW-1:0] a0_t [0:AN-1];
    reg signed [XW-1:0] a1_t [0:AN-1];
    reg signed [XW-1:0] ax_val [0:1];
    reg [1:0]           ax_ok;
    always @(*) begin
        for (xi = 0; xi < AN; xi = xi + 1) begin
            a0_t[xi] = {XW{1'b0}};
            a1_t[xi] = {XW{1'b0}};
        end
        a0_t[0] = d_abase[0];
        a1_t[0] = d_abase[1];
        // Mask first, THEN reduce: the compare gated every link of the chain.
        for (xi = 0; xi < NDIM; xi = xi + 1) begin
            if (d_axis[xi] == 2'd1) begin
                a0_t[xi+1] = apsum[xi];
            end
            if (d_axis[xi] == 2'd2) begin
                a1_t[xi+1] = apsum[xi];
            end
        end
        for (xn2 = AN >> 1; xn2 > 0; xn2 = xn2 >> 1) begin
            for (xk = 0; xk < xn2; xk = xk + 1) begin
                a0_t[xk] = a0_t[xk] + a0_t[xk + xn2];
                a1_t[xk] = a1_t[xk] + a1_t[xk + xn2];
            end
        end
        ax_val[0] = a0_t[0];
        ax_val[1] = a1_t[0];
        for (xi = 0; xi < 2; xi = xi + 1) begin
            // extent 0 disables the axis, so a descriptor with no padding needs
            // no axis fields set at all
            ax_ok[xi] = (
                (d_aext[xi] == {XW{1'b0}})
                || (
                    (ax_val[xi] >= 0)
                    && (ax_val[xi] < $signed({1'b0, d_aext[xi]}))
                )
            );
        end
    end
    wire w_valid = &ax_ok;

    // ---- the element presented -------------------------------------------
    generate if (OREG == 0) begin : g_direct
        assign step   = next;
        assign active = run;
        assign last   = w_last;
        assign valid  = w_valid;
        assign addr   = w_addr;
        assign low_nz = w_low_nz;
    end else begin : g_oreg
        // A two-entry queue the walker fills on its own count: `next` pops it,
        // so the caller's step reaches AW+3 flops and not the walker's
        // enables (2,700 endpoints, 79% route, at the v8t5 route). `start`
        // loads element 0 itself and the walker's first step (`q_pend`)
        // passes it unpushed, so element 1 is pushed two cycles after start:
        // the caller's first pop must wait that long (a muxed start state
        // that had element 1 ready a cycle earlier cost 190 LUT a walker).
        reg [AW+2:0]  q_e0, q_e1;             // {low_nz, last, valid, addr}
        reg [1:0]     q_cnt;
        reg           q_rd, q_pend;
        wire          q_pop  = next && (q_cnt != 2'd0);
        wire          q_push = run && !q_pend && (q_cnt != 2'd2);
        wire          q_wr   = q_rd ^ q_cnt[0];
        wire [AW+2:0] q_in   = {w_low_nz, w_last, w_valid, w_addr};
        // element 0's bounds: every axis sum is its base
        reg [1:0] ok0;
        integer zi;
        always @(*) begin
            for (zi = 0; zi < 2; zi = zi + 1) begin
                ok0[zi] = (d_aext[zi] == {XW{1'b0}})
                       || ((d_abase[zi] >= 0)
                           && (d_abase[zi] < $signed({1'b0, d_aext[zi]})));
            end
        end
        wire [AW+2:0] q_e0v = {|d_base[LOWB-1:0], last0, &ok0, d_base};
        assign step = q_push || q_pend;
        always @(posedge clk) begin
            if (rst) begin
                q_cnt <= 2'd0; q_rd <= 1'b0; q_pend <= 1'b0;
            end else if (start) begin
                q_cnt <= 2'd1; q_rd <= 1'b0; q_e0 <= q_e0v; q_pend <= 1'b1;
            end else begin
                q_pend <= 1'b0;
                q_cnt  <= q_cnt + {1'b0, q_push} - {1'b0, q_pop};
                if (q_pop) begin
                    q_rd <= !q_rd;
                end
                if (q_push) begin
                    if (q_wr) begin q_e1 <= q_in; end
                    else begin q_e0 <= q_in; end
                end
            end
        end
        wire [AW+2:0] q_hd = q_rd ? q_e1 : q_e0;
        assign active = (q_cnt != 2'd0);
        assign addr   = q_hd[AW-1:0];
        assign valid  = q_hd[AW];
        assign last   = q_hd[AW+1];
        assign low_nz = q_hd[AW+2];
    end endgenerate

endmodule

`default_nettype wire
