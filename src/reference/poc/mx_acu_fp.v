// Accumulator CU: floating-point accumulation into a resident output tile,
// with peer transfer so a matmul can span clusters.
//
// The exact fixed-point mx_acu.v remains the bring-up instrument; it is what
// lets mx_cluster_tb check the datapath bit-for-bit. This is what ships.
//
//   stage 1   unpack the two chains, take |value| and apply the block scale,
//             both in one DSP
//   stage 2a  leading-one search -> the one-hot the shift multiplies by
//   stage 2a2 the normalising shift, as two DSP multiplies per lane
//   stage 2b  round and assemble -> accumulator float
//   stage 3   read the tile, compare exponents, align        <- reads the tile
//   stage 4   add, leading-one search, shift
//   stage 5   round, assemble, write back                    <- writes the tile
//   stage 6   (EMIT only) convert to FP16
//
// Depth is not the constraint -- throughput is. Only the accumulate loop
// (stage 3 read -> stage 5 write) costs anything to lengthen, and the price is
// banks. It runs on ONE bank because the ISA sweeps K OUTERMOST, which replaces
// the structure with a CONTRACT: consecutive commands to the same tile address
// must be at least REUSE_MIN cycles apart. See stage 3 below.
//
// ACC_MW sets the mantissa: 16 is FP24, 14 is FP22, 12 is FP20. E7 is fixed;
// range is not the tunable. MW=14 measures identically to MW=16 and costs less,
// so it is the default -- see docs/compute/accumulator.md.
//
// TIMING: this block sets the CU's critical path. It bound on stage 1 -> stage
// 2a (val_r -> b_sig) until the normalising shift moved into DSPs; the stage now
// carries the leading-one search alone. Five rules, each worth 20-60 MHz:
//
//   1. The magnitude is taken BEFORE the multiply, in the DSP. Taking it after
//      puts a 30-bit two's-complement carry chain between the DSP's output
//      register and the leading-one search.
//   2. Nothing combinational in front of the tile address. Every select that
//      steers stage 3 is registered a stage early, and registered ALREADY
//      DECODED (a_zero_r, b_peer_r, b_zero_r, addr_r2) -- an encoded id would
//      put its equality test back in front of the mux this rule empties.
//   3. One mux level on the operands, not a chain of ternaries.
//   4. Zero-ness travels as one control bit per operand, never as a mask on
//      the 384-bit data.
//   5. Nothing but the search between val_r and the shift DSPs' input registers.
//      The one-hot is mx_lead1's isolated bit reversed, so it adds no levels.
//
// The leading-one search and the sticky bits in mx_fpacc.v must stay
// tree-shaped for the same reason.

`default_nettype none

module mx_acu_fp #(
    parameter integer DEPTH  = 16,          // resident output sub-tiles (4x4 each)
    parameter integer ACC_MW = 14,
    // Storage for the resident tile, named rather than inferred. "block" is
    // 5 BRAM36 for any depth up to 512 and 0 LUT; "distributed" is ~4,000 LUT
    // at depth 512 and misses 300 MHz by depth 64.
    parameter         TILE_PRIM = "block",
    // DUAL=1 is fix A: a second partial with its OWN scale pair enters every
    // cycle and the two are aligned and merged before stage 1's register.
    parameter integer DUAL = 0,
    // TMUX=1 is fix C: ONE front end on clk2x, alternating between the two
    // partials, instead of DUAL's two at clk. The merge is identical.
    parameter integer TMUX = 0
)(
    input  wire         clk,
    input  wire         clk2x,
    input  wire         rst,
    input  wire         en,

    input  wire [383:0] part_in,
    input  wire [31:0]  sa,
    input  wire [31:0]  sb,
    input  wire [7:0]   anchor,

    input  wire [383:0] part_in2,
    input  wire [31:0]  sa2,
    input  wire [31:0]  sb2,
    input  wire         single,     // the pair's second block is past NK

    input  wire [2:0]   op,
    // width derived from DEPTH -- see the TAW localparam below
    input  wire [((DEPTH <= 1) ? 1 : $clog2(DEPTH))-1:0] tile_addr,
    input  wire         cmd_valid,

    input  wire [16*(ACC_MW+8)-1:0] peer_in,
    output reg  [16*(ACC_MW+8)-1:0] peer_out,
    output reg                      peer_valid,

    output reg  [255:0] emit_out,
    output reg          emit_valid,

    output wire         busy
);
    // DERIVED, never passed in. As two independent parameters these silently
    // disagreed: DEPTH=512 with TAW=4 addressed 16 entries and synthesis
    // correctly optimised the other 496 away, with no error anywhere.
    localparam integer TAW = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    localparam integer AW = ACC_MW + 8;
    localparam integer TW = 16 * AW;
    localparam integer SW = ACC_MW + 10;    // align stage sum width

    localparam [2:0] OP_NOP      = 3'd0,
                     OP_LOAD     = 3'd1,
                     OP_ADD      = 3'd2,
                     OP_ADD_PEER = 3'd3,
                     OP_SEND     = 3'd4,
                     OP_EMIT     = 3'd5,
                     OP_FWD      = 3'd6,
                     // ADD AND HAND THE RESULT OUT, in one command. A sub-tile's
                     // last accumulation already computes its finished value at
                     // stage 5, so a separate OP_EMIT would re-read the address
                     // and spend a command slot the sweep does not have. Fused,
                     // the emit needs a queue and backpressure instead of a
                     // drain phase.
                     OP_ADD_EMIT = 3'd7;

    localparam [1:0] OUT_NONE = 2'd0, OUT_EMIT = 2'd1, OUT_SEND = 2'd2;

    // ================================================ stage 0: register in
    // Without this the inferred DSP has no AREG/DREG/BREG: measured 2.111 ns.

    // EVERY input, so data and command shift together and nothing else moves.
    reg [383:0]  part_q;
    reg [31:0]   sa_q, sb_q;
    reg [7:0]    anchor_q;
    reg [2:0]    op_q;
    reg [TAW-1:0] addr_q;
    reg          cmdv_q;
    reg [TW-1:0] peer_q;

    always @(posedge clk) begin
        if (rst) begin
            part_q <= 384'd0; sa_q <= 32'd0; sb_q <= 32'd0; anchor_q <= 8'd0;
            op_q <= OP_NOP; addr_q <= {TAW{1'b0}}; cmdv_q <= 1'b0;
            peer_q <= {TW{1'b0}};
        end else if (en) begin
            part_q <= part_in; sa_q <= sa; sb_q <= sb; anchor_q <= anchor;
            op_q <= op; addr_q <= tile_addr; cmdv_q <= cmd_valid;
            peer_q <= peer_in;
        end
    end

    reg [383:0] part_q2;
    reg [31:0]  sa_q2, sb_q2;
    reg         sing_q;
    always @(posedge clk) begin
        if (rst) begin
            part_q2 <= 384'd0; sa_q2 <= 32'd0; sb_q2 <= 32'd0;
        end else if (en) begin
            part_q2 <= part_in2; sa_q2 <= sa2; sb_q2 <= sb2;
            // Stage 0 like every other command field: without this `single` is
            // one delay shallower than addr_q and carries the NEXT command's.
            sing_q  <= single;
        end
    end

    // ================================================ stage 1: extract
    // VW is 22, not 29. A K=32 block can only reach +/-131,072 -- 18 bits and a
    // sign. A 29-bit normaliser would build a 29-bit leading-one search and
    // shifter per lane, sixteen times, for range that cannot occur.
    localparam integer VW  = 22;
    localparam integer VWM = VW + 8;    // widened by the scale mantissa product

    // MAGNITUDE, not a signed value, and the sign beside it. See the lane
    // generate below.
    reg  [VWM-1:0]      val_r [0:15];
    reg                 sgn_r [0:15];
    reg  [2:0]          op_r;
    reg  [TAW-1:0]      addr_r;
    reg                 v1;
    reg  [TW-1:0]       peer_r;
    reg signed [9:0]    exp_r [0:15];

    // ---- E5M3 block scale ------------------------------------------------
    // The scale field is {E[4:0], M[2:0]} and the scale is 2^(E-SBIAS)*(1+M/8).
    // The exponent halves still just add. The mantissas have to MULTIPLY:
    //
    //   (1+Ma/8)(1+Mb/8) = (m8a * m8b) / 64,   m8 = 8+M, so m8a*m8b in [64,225]
    //
    // Multiplying the partial sum by m8a*m8b and taking the /64 off the
    // exponent keeps it EXACT -- no shifter, no rounding, and no precision
    // lost. The product is 8 bits wider, which is why VWM exists.
    //
    // A per-tile output scale was evaluated here and CANCELLED: folding the
    // constant into Q on the host is exactly equivalent and free, because a
    // uniform factor lands entirely in MXFP7's block exponents. See
    // docs/compute/accumulator.md s4.6.
    wire [3:0] ma [0:3];
    wire [3:0] mb [0:3];
    wire signed [9:0] ea [0:3];
    wire signed [9:0] eb [0:3];
    // Declared WIDE and on their own. Inline as `{1'b0, ma[i]*mb[j]}` the
    // multiply is self-determined to its 4-bit operand width inside the
    // concatenation, so 8*8 = 64 truncates to 0 and every result comes out zero.
    wire [7:0] mm [0:3][0:3];
    wire [3:0] ma2 [0:3];
    wire [3:0] mb2 [0:3];
    wire signed [9:0] ea2 [0:3];
    wire signed [9:0] eb2 [0:3];
    wire [7:0] mm2 [0:3][0:3];
    genvar gs, gt;
    generate
    for (gs = 0; gs < 4; gs = gs + 1) begin : g_scale
        assign ma[gs] = {1'b1, sa_q[gs*8 +: 3]};              // 8..15
        assign mb[gs] = {1'b1, sb_q[gs*8 +: 3]};
        assign ea[gs] = $signed({5'b0, sa_q[gs*8+3 +: 5]});   // biased exponent
        assign eb[gs] = $signed({5'b0, sb_q[gs*8+3 +: 5]});
        assign ma2[gs] = {1'b1, sa_q2[gs*8 +: 3]};
        assign mb2[gs] = {1'b1, sb_q2[gs*8 +: 3]};
        assign ea2[gs] = $signed({5'b0, sa_q2[gs*8+3 +: 5]});
        assign eb2[gs] = $signed({5'b0, sb_q2[gs*8+3 +: 5]});
        for (gt = 0; gt < 4; gt = gt + 1) begin : g_mm
            assign mm[gs][gt]  = {4'd0, ma[gs]}  * {4'd0, mb[gt]};  // 64..225
            assign mm2[gs][gt] = {4'd0, ma2[gs]} * {4'd0, mb2[gt]};
        end
    end
    endgenerate

    // ---- lane magnitude, taken BEFORE the multiply -----------------------
    // `mm` is unsigned, so the product's sign is the chain value's, and
    //
    //     |v + r| * mm  =  ((v ^ {W{s}}) + (s ^ r)) * mm,      s = v[W-1]
    //
    // which is one XOR level on a register output. Taking the magnitude AFTER
    // the multiply instead puts a 30-bit two's-complement carry chain between
    // the DSP's output register and the leading-one search -- see
    // docs/compute/accumulator.md.
    //
    // `r` is a rounding bit, so `v + r` can only reach zero from v = -1: the one
    // case where `s` disagrees with the true sign of the sum. The magnitude is
    // zero there and `is_zero` discards the sign.
    wire [VWM-1:0] lane_mag [0:15];
    wire           lane_sgn [0:15];
    wire [VWM-1:0] lane_mag2 [0:15];
    wire           lane_sgn2 [0:15];

    generate
    for (gs = 0; gs < 4; gs = gs + 1) begin : g_lane2_r
        for (gt = 0; gt < 4; gt = gt + 1) begin : g_lane2_c
            localparam integer F2 = ((gs/2)*4 + gt) * 48;
            if ((gs % 2) == 0) begin : g_lo2
                wire        s = part_q2[F2+18];
                wire [18:0] x = part_q2[F2 +: 19] ^ {19{s}};
                assign lane_sgn2[gs*4+gt] = s;
                assign lane_mag2[gs*4+gt] = ({1'b0, x} + {19'd0, s}) * mm2[gs][gt];
            end else begin : g_hi2
                wire        s = part_q2[F2+40];
                wire        r = part_q2[F2+18];
                wire [21:0] x = part_q2[F2+19 +: 22] ^ {22{s}};
                assign lane_sgn2[gs*4+gt] = s;
                assign lane_mag2[gs*4+gt] = ({1'b0, x} + {22'd0, (s ^ r)})
                                          * mm2[gs][gt];
            end
        end
    end
    endgenerate

    generate
    for (gs = 0; gs < 4; gs = gs + 1) begin : g_lane_r
        for (gt = 0; gt < 4; gt = gt + 1) begin : g_lane_c
            localparam integer F = ((gs/2)*4 + gt) * 48;
            if ((gs % 2) == 0) begin : g_lo
                wire        s = part_q[F+18];
                wire [18:0] x = part_q[F +: 19] ^ {19{s}};
                assign lane_sgn[gs*4+gt] = s;
                assign lane_mag[gs*4+gt] = ({1'b0, x} + {19'd0, s}) * mm[gs][gt];
            end else begin : g_hi
                wire        s = part_q[F+40];
                wire        r = part_q[F+18];
                wire [21:0] x = part_q[F+19 +: 22] ^ {22{s}};
                assign lane_sgn[gs*4+gt] = s;
                assign lane_mag[gs*4+gt] = ({1'b0, x} + {22'd0, (s ^ r)})
                                         * mm[gs][gt];
            end
        end
    end
    endgenerate

    // ---- fix C: one front end on clk2x, alternating -----------------------
    // tmm is REGISTERED between the scale mux and the lane multiply: combined
    // they were 9 levels / 3.040 ns and held clk2x to 329 MHz.
    reg          tph, tph_d;
    reg [383:0]  tp_q, tp_d;
    reg [31:0]   tsa_q, tsb_q;
    reg [7:0]    tmm_q [0:3][0:3], tmm_qq [0:3][0:3];
    reg          tph_dd, tph_ddd;
    reg [VWM-1:0] tl_q [0:15];
    reg           tls_q [0:15];
    reg [VWM-1:0] tlm0 [0:15], tlm1 [0:15];
    reg           tls0 [0:15], tls1 [0:15];

    wire [3:0] tma [0:3];
    wire [3:0] tmb [0:3];
    wire [7:0] tmm [0:3][0:3];
    wire [VWM-1:0] tlane [0:15];
    wire           tlsgn [0:15];

    generate
    for (gs = 0; gs < 4; gs = gs + 1) begin : g_tsc
        assign tma[gs] = {1'b1, tsa_q[gs*8 +: 3]};
        assign tmb[gs] = {1'b1, tsb_q[gs*8 +: 3]};
        for (gt = 0; gt < 4; gt = gt + 1) begin : g_tmm
            assign tmm[gs][gt] = {4'd0, tma[gs]} * {4'd0, tmb[gt]};
        end
    end
    for (gs = 0; gs < 4; gs = gs + 1) begin : g_tl_r
        for (gt = 0; gt < 4; gt = gt + 1) begin : g_tl_c
            localparam integer FT = ((gs/2)*4 + gt) * 48;
            // Magnitude registered BEFORE the multiply, or the XOR/increment
            // sits in front of the DSP and the product lands in fabric.
            if ((gs % 2) == 0) begin : g_tlo
                wire        s = tp_d[FT+18];
                wire [18:0] x = tp_d[FT +: 19] ^ {19{s}};
                reg  [19:0] mag_r; reg sgn_rr;
                always @(posedge clk2x) begin
                    mag_r  <= {1'b0, x} + {19'd0, s};
                    sgn_rr <= s;
                end
                assign tlsgn[gs*4+gt] = sgn_rr;
                assign tlane[gs*4+gt] = mag_r * tmm_qq[gs][gt];
            end else begin : g_thi
                wire        s = tp_d[FT+40];
                wire        r = tp_d[FT+18];
                wire [21:0] x = tp_d[FT+19 +: 22] ^ {22{s}};
                reg  [22:0] mag_r; reg sgn_rr;
                always @(posedge clk2x) begin
                    mag_r  <= {1'b0, x} + {22'd0, (s ^ r)};
                    sgn_rr <= s;
                end
                assign tlsgn[gs*4+gt] = sgn_rr;
                assign tlane[gs*4+gt] = mag_r * tmm_qq[gs][gt];
            end
        end
    end
    endgenerate

    integer tq, tr;
    always @(posedge clk2x) begin
        for (tq = 0; tq < 4; tq = tq + 1) begin
            for (tr = 0; tr < 4; tr = tr + 1) begin
                tmm_q[tq][tr]  <= tmm[tq][tr];
                tmm_qq[tq][tr] <= tmm_q[tq][tr];
            end
        end
    end

    integer t;
    always @(posedge clk2x) begin
        if (rst) begin
            tph <= 1'b0; tp_q <= 384'd0; tsa_q <= 32'd0; tsb_q <= 32'd0;
        end else begin
            tph   <= ~tph;
            tp_q  <= tph ? part_q2 : part_q;
            tsa_q <= tph ? sa_q2   : sa_q;
            tsb_q <= tph ? sb_q2   : sb_q;
            tp_d    <= tp_q;
            tph_d   <= tph;
            tph_dd  <= tph_d;
            tph_ddd <= tph_dd;
            // Register on the DSP output, THEN demux: fanning the product into
            // two arrays put fabric on the DSP output and pinned clk2x at 508.
            for (t = 0; t < 16; t = t + 1) begin
                tl_q[t]  <= tlane[t];
                tls_q[t] <= tlsgn[t];
                if (tph_ddd) begin tlm1[t] <= tl_q[t]; tls1[t] <= tls_q[t]; end
                else         begin tlm0[t] <= tl_q[t]; tls0[t] <= tls_q[t]; end
            end
        end
    end

    // ---- fix A: align the two partials and merge before stage 1 -----------

    // Products registered first: fused with the align it was 17 levels / 4.516 ns.
    reg [VWM-1:0] lm_a [0:15], lm_b [0:15];
    reg           ls_a [0:15], ls_b [0:15];
    reg [7:0]     anc_d;
    reg signed [9:0] ea_d [0:3], eb_d [0:3], ea2_d [0:3], eb2_d [0:3];
    // The merge adds TWO register stages to the data path (lm_*, then bm_r), so
    // the command must be delayed by two or val_r samples a stale mrg_val.
    reg [2:0]     op_d1, op_d2;
    reg [TAW-1:0] addr_d1, addr_d2;
    reg           cmdv_d1, cmdv_d2;
    reg [TW-1:0]  peer_d1, peer_d2;
    // TWO delays, matching lm_a/lm_b: at one, `single` led the data by a cycle
    // and only showed up when it changed between issues, i.e. odd NK > 1.
    reg           sing_d1, sing_d2;
    always @(posedge clk) if (en) begin
        op_d1   <= op_q;    op_d2   <= op_d1;
        addr_d1 <= addr_q;  addr_d2 <= addr_d1;
        cmdv_d1 <= cmdv_q;  cmdv_d2 <= cmdv_d1;
        peer_d1 <= peer_q;  peer_d2 <= peer_d1;
        sing_d1 <= sing_q;  sing_d2 <= sing_d1;
    end

    integer aq;
    always @(posedge clk) begin
        for (aq = 0; aq < 16; aq = aq + 1) begin
            lm_a[aq] <= lane_mag[aq];  ls_a[aq] <= lane_sgn[aq];
            lm_b[aq] <= lane_mag2[aq]; ls_b[aq] <= lane_sgn2[aq];
        end
        for (aq = 0; aq < 4; aq = aq + 1) begin
            ea_d[aq] <= ea[aq];   eb_d[aq]  <= eb[aq];
            ea2_d[aq] <= ea2[aq]; eb2_d[aq] <= eb2[aq];
        end
        anc_d <= anchor_q;
    end

    // Shift capped at VWM -- beyond that the smaller term is under the larger's LSB.
    wire [VWM-1:0]     mrg_val [0:15];
    wire               mrg_sgn [0:15];
    wire signed [9:0]  mrg_exp [0:15];
    generate
    for (gs = 0; gs < 4; gs = gs + 1) begin : g_mrg_r
        for (gt = 0; gt < 4; gt = gt + 1) begin : g_mrg_c
            localparam integer L = gs*4 + gt;
            wire signed [9:0] e1 = ea_d[gs]  + eb_d[gt]  - $signed({2'b0, anc_d}) - 10'sd6;
            wire signed [9:0] e2 = ea2_d[gs] + eb2_d[gt] - $signed({2'b0, anc_d}) - 10'sd6;
            wire signed [10:0] d = e1 - e2;
            wire [10:0] ad = d[10] ? -d : d;
            wire [5:0]  sh = (ad > VWM) ? VWM[5:0] : ad[5:0];
            wire big2 = d[10];
            // TMUX feeds the SAME merge from the time-shared front end.
            wire [VWM-1:0] m0 = (TMUX != 0) ? tlm0[L] : lm_a[L];
            wire [VWM-1:0] m1 = (TMUX != 0) ? tlm1[L] : lm_b[L];
            wire           g0 = (TMUX != 0) ? tls0[L] : ls_a[L];
            wire           g1 = (TMUX != 0) ? tls1[L] : ls_b[L];
            wire [VWM-1:0] bmag = big2 ? m1 : m0;
            wire [VWM-1:0] smag = big2 ? m0 : m1;
            wire           bsgn = big2 ? g1 : g0;
            wire           ssgn = big2 ? g0 : g1;
            // Compare-and-swap registered off the shift-and-add: fused they were
            // 15 levels and held clk1x to 271 MHz.
            reg [VWM-1:0] bm_r, sm_r;
            reg [5:0] sh_r;
            reg bs_r, ss_r;
            reg signed [9:0] me_r;
            reg sing_r;
            always @(posedge clk) begin
                // A lone final block is PHASE 0, which is m1/part_in2 -- phase 1
                // read one entry past the operand.
                bm_r <= sing_d2 ? m1 : bmag;
                sm_r <= sing_d2 ? {VWM{1'b0}} : smag;
                sh_r <= sh;
                bs_r <= sing_d2 ? g1 : bsgn;
                ss_r <= sing_d2 ? g1 : ssgn;
                me_r <= sing_d2 ? e2 : (big2 ? e2 : e1);
                sing_r <= sing_d1;
            end
            wire [VWM-1:0] sal = sing_r ? {VWM{1'b0}} : (sm_r >> sh_r);
            // Larger-by-EXPONENT is not larger-by-MAGNITUDE at equal exponents:
            // the unsigned borrow wrapped to a huge value with the wrong sign.
            wire same = (bs_r == ss_r);
            wire [VWM:0] sum = {1'b0, bm_r} + {1'b0, sal};
            wire [VWM:0] dif = {1'b0, bm_r} - {1'b0, sal};
            wire         bor = dif[VWM];
            wire [VWM:0] mag = same ? sum
                             : (bor ? ({1'b0, sal} - {1'b0, bm_r}) : dif);
            assign mrg_val[L] = mag[VWM] ? mag[VWM:1] : mag[VWM-1:0];
            assign mrg_sgn[L] = same ? bs_r : (bor ? ss_r : bs_r);
            assign mrg_exp[L] = mag[VWM] ? (me_r + 10'sd1) : me_r;
`ifndef SYNTHESIS
`ifndef SYNTHESIS
            // ONE stage only -- a trace mixing stages printed numbers that
            // failed their own arithmetic and cost an hour.
            if (L == 0) begin : g_mrgdbg
                always @(posedge clk) if (DUAL != 0 && !rst && cmdv_d2)
                    $display("MRG a=%0d sing=%0b e1=%0d e2=%0d sh=%0d big2=%0b bmag=%0d smag=%0d",
                             addr_d2, sing_d2, e1, e2, sh, big2, bmag, smag);
            end
`endif
`endif
        end
    end
    endgenerate

    integer i, j;
    always @(posedge clk) begin
        if (rst) begin
            v1 <= 1'b0; op_r <= OP_NOP; addr_r <= {TAW{1'b0}};
            for (i = 0; i < 16; i = i + 1) begin
                val_r[i] <= {VWM{1'b0}}; sgn_r[i] <= 1'b0; exp_r[i] <= 10'sd0;
            end
            peer_r <= {TW{1'b0}};
        end else if (en) begin
            v1     <= (DUAL != 0) ? cmdv_d2 : cmdv_q;
            op_r   <= (DUAL != 0) ? op_d2   : op_q;
            addr_r <= (DUAL != 0) ? addr_d2 : addr_q;
            peer_r <= (DUAL != 0) ? peer_d2 : peer_q;
            for (i = 0; i < 4; i = i + 1) begin
                for (j = 0; j < 4; j = j + 1) begin
                    val_r[i*4+j] <= (DUAL != 0) ? mrg_val[i*4+j]
                                                : lane_mag[i*4+j];
                    sgn_r[i*4+j] <= (DUAL != 0) ? mrg_sgn[i*4+j]
                                                : lane_sgn[i*4+j];
                    // -6 undoes the /64 the mantissa product carries
                    exp_r[i*4+j] <= (DUAL != 0) ? mrg_exp[i*4+j]
                                  : ea[i] + eb[j]
                                  - $signed({2'b0, anchor_q}) - 10'sd6;
                end
            end
        end
    end

`ifndef SYNTHESIS
    // First point past the ACU inputs, printed identically for DUAL=0 and 1 so
    // the two builds' stage-1 streams can be diffed directly.
    always @(posedge clk) if (!rst && v1)
        $display("STG1 addr=%0d op=%0d v0=%0d s0=%0b e0=%0d v5=%0d s5=%0b e5=%0d",
                 addr_r, op_r, val_r[0], sgn_r[0], exp_r[0],
                 val_r[5], sgn_r[5], exp_r[5]);
`endif

    // ================================================ stage 2a: leading one
    // The search only, now: it hands on the one-hot 2^k that stage 2a2's DSPs
    // multiply by, so the barrel shifter that used to sit behind it -- the whole
    // machine's critical path, val_r -> b_sig -- is gone from the fabric.
    localparam integer NS = VWM - ACC_MW - 1;   // magnitude split, see norm_p

    // val_r -> this search -> the shift DSP's input reg is the in-context worst
    // path: 7 levels, 354 MHz. Split after the SMEAR: 313 -> 427 in isolation.
    wire [VWM-1:0] smear [0:15];
    genvar g, r, kk, bb;
    generate
    for (g = 0; g < 16; g = g + 1) begin : g_smear
        reg [VWM-1:0] sm;
        integer sh;
        always @(*) begin
            sm = val_r[g];
            for (sh = 1; sh < VWM; sh = sh * 2) begin
                sm = sm | (sm >> sh);
            end
        end
        assign smear[g] = sm;
    end
    endgenerate

    // ---- stage 1b: the smear, registered ---------------------------------
    reg [VWM-1:0]    y_r   [0:15];
    reg [VWM-1:0]    val_s [0:15];
    reg              sgn_s [0:15];
    reg signed [9:0] exp_s [0:15];
    reg [2:0]        op_s;
    reg [TAW-1:0]    addr_s;
    reg              v1_s;
    reg [TW-1:0]     peer_s;

    integer q;
    always @(posedge clk) begin
        if (rst) begin
            v1_s <= 1'b0; op_s <= OP_NOP; addr_s <= {TAW{1'b0}};
            peer_s <= {TW{1'b0}};
            for (q = 0; q < 16; q = q + 1) begin
                y_r[q] <= {VWM{1'b0}}; val_s[q] <= {VWM{1'b0}};
                sgn_s[q] <= 1'b0; exp_s[q] <= 10'sd0;
            end
        end else if (en) begin
            v1_s <= v1; op_s <= op_r; addr_s <= addr_r; peer_s <= peer_r;
            for (q = 0; q < 16; q = q + 1) begin
                y_r[q]   <= smear[q]; val_s[q] <= val_r[q];
                sgn_s[q] <= sgn_r[q]; exp_s[q] <= exp_r[q];
            end
        end
    end

    // ---- isolate and encode, now from the registered smear ---------------
    wire [15:0] n_ohf [0:15];
    wire        n_hi  [0:15];
    wire [5:0]  n_msb [0:15];
    wire        n_z   [0:15];

    generate
    for (g = 0; g < 16; g = g + 1) begin : g_norm_a
        wire [VWM-1:0] oh  = y_r[g] & ~(y_r[g] >> 1);
        wire [VWM-1:0] ohk;
        assign n_z[g] = !y_r[g][0];
        for (r = 0; r < VWM; r = r + 1) begin : g_rev
            assign ohk[r] = oh[VWM-1-r];
        end
        for (r = 0; r < 16; r = r + 1) begin : g_fold
            assign n_ohf[g][r] = (r < VWM)
                ? (ohk[r] | ((r+16 < VWM) ? ohk[r+16] : 1'b0)) : 1'b0;
        end
        assign n_hi[g] = (VWM > 16) ? |ohk[VWM-1:16] : 1'b0;
        for (kk = 0; kk < 6; kk = kk + 1) begin : g_enc
            wire [VWM-1:0] sel;
            for (bb = 0; bb < VWM; bb = bb + 1) begin : g_bit
                assign sel[bb] = ((bb >> kk) & 1) ? oh[bb] : 1'b0;
            end
            assign n_msb[g][kk] = |sel;
        end
    end
    endgenerate

    reg [15:0]       a_ohf [0:15];
    reg [NS-1:0]     a_lo  [0:15];
    reg [ACC_MW:0]   a_hi  [0:15];
    reg              a_h [0:15], a_z [0:15], a_sgn [0:15];
    reg [5:0]        a_msb [0:15];
    reg signed [9:0] a_exp [0:15];
    reg [2:0]        op_1a;
    reg [TAW-1:0]    addr_a;
    reg              v1a;
    reg [TW-1:0]     peer_a;

    always @(posedge clk) begin
        if (rst) begin
            v1a <= 1'b0; op_1a <= OP_NOP; addr_a <= {TAW{1'b0}};
            peer_a <= {TW{1'b0}};
            for (i = 0; i < 16; i = i + 1) begin
                a_ohf[i] <= 16'd0; a_lo[i] <= {NS{1'b0}};
                a_hi[i] <= {(ACC_MW+1){1'b0}}; a_h[i] <= 1'b0;
                a_msb[i] <= 6'd0; a_z[i] <= 1'b0;
                a_sgn[i] <= 1'b0; a_exp[i] <= 10'sd0;
            end
        end else if (en) begin
            v1a    <= v1_s;
            op_1a  <= op_s;
            addr_a <= addr_s;
            peer_a <= peer_s;
            for (i = 0; i < 16; i = i + 1) begin
                a_ohf[i] <= n_ohf[i];
                a_lo[i]  <= val_s[i][NS-1:0];
                a_hi[i]  <= val_s[i][VWM-1:NS];
                a_h[i]   <= n_hi[i];   a_msb[i] <= n_msb[i];
                a_z[i]   <= n_z[i];    a_sgn[i] <= sgn_s[i];
                a_exp[i] <= exp_s[i];
            end
        end
    end

    // ================================================ stage 2a2: the shift
    // Two DSPs per lane, because one 27x18 cannot hold VWM bits shifted over VWM
    // positions. Both halves take the SAME one-hot and the halves of `mag` are
    // disjoint, so mx_fpacc_norm_p reassembles them with an OR rather than an add.
    reg [NS+15:0]     b_plo [0:15];
    reg [ACC_MW+16:0] b_phi [0:15];
    reg               b_h [0:15], b_z [0:15], b_sgn [0:15];
    reg [5:0]         b_msb [0:15];
    reg signed [9:0]  b_exp [0:15];
    reg [2:0]         op_1b;
    reg [TAW-1:0]     addr_b;
    reg               v1b;
    reg [TW-1:0]      peer_b;

    always @(posedge clk) begin
        if (rst) begin
            v1b <= 1'b0; op_1b <= OP_NOP; addr_b <= {TAW{1'b0}};
            peer_b <= {TW{1'b0}};
            for (i = 0; i < 16; i = i + 1) begin
                b_plo[i] <= {(NS+16){1'b0}}; b_phi[i] <= {(ACC_MW+17){1'b0}};
                b_h[i] <= 1'b0; b_msb[i] <= 6'd0; b_z[i] <= 1'b0;
                b_sgn[i] <= 1'b0; b_exp[i] <= 10'sd0;
            end
        end else if (en) begin
            v1b    <= v1a;
            op_1b  <= op_1a;
            addr_b <= addr_a;
            peer_b <= peer_a;
            for (i = 0; i < 16; i = i + 1) begin
                b_plo[i] <= a_lo[i] * a_ohf[i];
                b_phi[i] <= a_hi[i] * a_ohf[i];
                b_h[i]   <= a_h[i];   b_msb[i] <= a_msb[i];
                b_z[i]   <= a_z[i];   b_sgn[i] <= a_sgn[i];
                b_exp[i] <= a_exp[i];
            end
        end
    end

    // ================================================ stage 2b: assemble
    wire [ACC_MW:0] p_sig [0:15];
    wire            p_g [0:15], p_s [0:15];
    wire [AW-1:0]   normed [0:15];
    wire [TW-1:0]   chain_fp;
    generate
    for (g = 0; g < 16; g = g + 1) begin : g_norm_b
        mx_fpacc_norm_p #(.MW(ACC_MW), .VW(VWM)) u_np (
            .p_lo(b_plo[g]), .p_hi(b_phi[g]), .hi(b_h[g]),
            .sig(p_sig[g]), .guard(p_g[g]), .sticky(p_s[g])
        );
        mx_fpacc_norm_b #(.MW(ACC_MW)) u_nb (
            .sig(p_sig[g]), .msb(b_msb[g]), .guard(p_g[g]), .sticky(p_s[g]),
            .is_zero(b_z[g]), .sign(b_sgn[g]), .exp_in(b_exp[g]),
            .fp(normed[g])
        );
        assign chain_fp[g*AW +: AW] = normed[g];
    end
    endgenerate

    reg [TW-1:0]  chain_r, peer_r2;
    reg [2:0]     op_r2;
    reg [TAW-1:0] addr_r2;
    reg           v2;

    // Every select that steers the align stage is REGISTERED, not decoded in the
    // align cycle: anything combinational here lands ahead of the tile read, the
    // exponent compare and the barrel shift, the longest chain in the block.
    // Stored DECODED, not as an encoded source id: the equality test that turned
    // an id into these bits sat in front of the 384-bit mux and the zero flags.
    reg a_zero_r;       // align operand A is zero rather than the tile
    reg b_peer_r;       // align operand B is the peer rather than the chain
    reg b_zero_r;       // align operand B is zero

    always @(posedge clk) begin
        if (rst) begin
            chain_r <= {TW{1'b0}}; peer_r2 <= {TW{1'b0}};
            op_r2 <= OP_NOP; addr_r2 <= {TAW{1'b0}}; v2 <= 1'b0;
            a_zero_r <= 1'b1; b_peer_r <= 1'b0; b_zero_r <= 1'b1;
        end else if (en) begin
            chain_r <= chain_fp;
            peer_r2 <= peer_b;
            op_r2   <= op_1b;
            v2      <= v1b;

            // only updated on a valid command; everything downstream of it is
            // gated by v2, so it holds rather than clears
            if (v1b) begin
                addr_r2 <= addr_b;
            end

            // Operand sources, resolved one stage ahead of the align cycle so
            // nothing combinational sits in front of the tile read.
            if (v1b && (op_1b == OP_LOAD)) begin
                a_zero_r <= 1'b1; b_peer_r <= 1'b0; b_zero_r <= 1'b0;  // zero+chain
            end else if (v1b && (op_1b == OP_ADD_PEER)) begin
                a_zero_r <= 1'b0; b_peer_r <= 1'b1; b_zero_r <= 1'b0;  // tile+peer
            end else if (
                v1b && ((op_1b == OP_ADD) || (op_1b == OP_ADD_EMIT))
            ) begin
                a_zero_r <= 1'b0; b_peer_r <= 1'b0; b_zero_r <= 1'b0;  // tile+chain
            end else if (v1b && ((op_1b == OP_EMIT) || (op_1b == OP_SEND))) begin
                a_zero_r <= 1'b0; b_peer_r <= 1'b0; b_zero_r <= 1'b1;  // pass out
            end
        end
    end

    // ================================================ stage 3: read + align
    // ONE bank. Banks existed to close a single-cycle accumulate loop when K was
    // the inner loop; the ISA now sweeps K OUTERMOST
    // (docs/compute/tensor-isa.md s5.1), so an address recurs only every Gm*Gn
    // cycles -- 64 at the smallest useful tiling -- and read latency is free.
    //
    // READ_LAT=2 uses the block RAM's own output register. Without it the path
    // starts at the RAM array access (~1.2 ns clock-to-out) instead of a
    // flip-flop, which cost ~70 MHz. The address must therefore lead the data by
    // two cycles: sampled at the end of stage 2a, valid during stage 2a2.
    //
    // REUSE_MIN counts read to write, so the stages AHEAD of the read do not
    // enter it -- stage 2a2 lengthened the block without touching this number.
    //
    // THE SOURCE OF TRUTH for the reuse distance. Two callers encode it
    // independently and CANNOT see this value: mx_cluster_mgr's pacing buckets
    // and mx_cluster_cu's `i_wide` both expand `Gm*Gn < 5` into small tests on
    // Gm and Gn, because computing the product cost the CU 26 MHz. Each carries
    // its own elaboration check, but nothing links them to this line -- changing
    // it means changing all three.
    localparam integer REUSE_MIN = 5;   // cycles between commands to one address

    wire [TW-1:0]  t0;
    wire [TW-1:0]  wr_data;
    wire [TAW-1:0] wr_addr;
    wire           wr_bank_en;

    // addr_a, not addr_r: READ_LAT=2 means the address must be presented two
    // stages ahead of the align cycle, and stage 2a2 put one more stage between.
    kohaku_sdpram #(.WIDTH(TW), .DEPTH(DEPTH), .MEM_PRIM(TILE_PRIM), .READ_LAT(2))
    u_tile (.clk(clk), .wr_en(wr_bank_en), .wr_addr(wr_addr), .wr_data(wr_data),
            .rd_en(en), .rd_addr(addr_a), .rd_data(t0));

    wire norm_iss = v2 && (
        (op_r2 == OP_LOAD)
        || (op_r2 == OP_ADD)
        || (op_r2 == OP_ADD_PEER)
        || (op_r2 == OP_ADD_EMIT)
    );
    wire fwd_iss  = v2 && (op_r2 == OP_FWD);
    wire sum_iss  = v2 && ((op_r2 == OP_EMIT) || (op_r2 == OP_SEND));
    // ADD_EMIT both writes the tile back and hands the value out, so it is in
    // norm_iss (the write) and drives OUT_EMIT (the output) at once.
    wire out_emit = v2 && ((op_r2 == OP_EMIT) || (op_r2 == OP_ADD_EMIT));

    // ONE mux level, on registered selects, and NOTHING in front of it: these
    // are the stage-2b flops themselves, not a decode of an encoded source id.
    wire [TW-1:0] op_a  = t0;
    wire [TW-1:0] op_bb = b_peer_r ? peer_r2 : chain_r;

    wire a_zero = a_zero_r;
    wire b_zero = b_zero_r;

    wire       iss_wr  = norm_iss;
    wire [1:0] iss_out = (v2 && (op_r2 == OP_SEND)) ? OUT_SEND
                       : out_emit                   ? OUT_EMIT
                                                    : OUT_NONE;

`ifndef SYNTHESIS
    // The reuse distance is a CONTRACT, not a structure, so check it: a caller
    // that sweeps K on the inside fails loudly instead of quietly accumulating
    // into stale data.
    //
    // A separate valid bit, not a sentinel address: {TAW{1'b1}} meaning "no
    // command" collides with the real top address, which at TAW=4 is 15.
    reg [TAW-1:0] last_addr [0:REUSE_MIN-1];
    reg           last_vld  [0:REUSE_MIN-1];
    integer rq;
    always @(posedge clk) begin
        if (rst) begin
            for (rq = 0; rq < REUSE_MIN; rq = rq + 1) begin
                last_vld[rq] <= 1'b0;
            end
        end else if (en) begin
            if (v2 && (norm_iss || sum_iss)) begin
                for (rq = 0; rq < REUSE_MIN-1; rq = rq + 1) begin
                    if (last_vld[rq] && last_addr[rq] == addr_r2) begin
                        $display("%0t ERROR mx_acu_fp: address %0d reused within %0d cycles -- sweep K outermost",
                                 $time, addr_r2, REUSE_MIN);
                    end
                end
            end
            for (rq = REUSE_MIN-1; rq > 0; rq = rq - 1) begin
                last_addr[rq] <= last_addr[rq-1];
                last_vld[rq]  <= last_vld[rq-1];
            end
            last_addr[0] <= addr_r2;
            last_vld[0]  <= v2 && (norm_iss || sum_iss);
        end
    end
`endif

    wire [SW-1:0] al_bg   [0:15];
    wire [SW-1:0] al_sh   [0:15];
    wire          al_sub  [0:15];
    wire [6:0]    al_ebig [0:15];
    wire          al_sbig [0:15], al_lost [0:15], al_zero [0:15], al_pass [0:15];
    wire [AW-1:0] al_pval [0:15];

    generate
    for (g = 0; g < 16; g = g + 1) begin : g_align
        mx_fpacc_align #(.MW(ACC_MW)) u_al (
            .a(op_a[g*AW +: AW]), .b(op_bb[g*AW +: AW]),
            .a_zero(a_zero), .b_zero(b_zero),
            .bg_o(al_bg[g]), .sh_o(al_sh[g]), .sub_o(al_sub[g]),
            .e_big_o(al_ebig[g]), .s_big_o(al_sbig[g]),
            .lost_o(al_lost[g]), .zero_o(al_zero[g]),
            .pass_o(al_pass[g]), .pass_val(al_pval[g])
        );
    end
    endgenerate

    reg [SW-1:0]  s3_bg   [0:15];
    reg [SW-1:0]  s3_sh   [0:15];
    reg           s3_sub  [0:15];
    reg [6:0]     s3_ebig [0:15];
    reg           s3_sbig [0:15], s3_lost [0:15], s3_zero [0:15], s3_pass [0:15];
    reg [AW-1:0]  s3_pval [0:15];
    reg [TW-1:0]  s3_chain;
    reg [TAW-1:0] s3_addr;
    reg           s3_wr, s3_fwd, v3;
    reg [1:0]     s3_out;

    always @(posedge clk) begin
        if (rst) begin
            v3 <= 1'b0; s3_addr <= {TAW{1'b0}}; s3_chain <= {TW{1'b0}};
            s3_wr <= 1'b0; s3_out <= OUT_NONE; s3_fwd <= 1'b0;
            for (i = 0; i < 16; i = i + 1) begin
                s3_bg[i] <= {SW{1'b0}}; s3_sh[i] <= {SW{1'b0}};
                s3_sub[i] <= 1'b0; s3_ebig[i] <= 7'd0;
                s3_sbig[i] <= 1'b0; s3_lost[i] <= 1'b0;
                s3_zero[i] <= 1'b0; s3_pass[i] <= 1'b0; s3_pval[i] <= {AW{1'b0}};
            end
        end else if (en) begin
            // sum_iss is EMIT/SEND: they produce an output but no tile write,
            // so they must be counted valid separately from iss_wr
            v3       <= iss_wr || fwd_iss || sum_iss;
            s3_addr  <= addr_r2;
            s3_chain <= chain_r;
            s3_wr    <= iss_wr;
            s3_out   <= iss_out;
            s3_fwd   <= fwd_iss;
            for (i = 0; i < 16; i = i + 1) begin
                s3_bg[i]   <= al_bg[i];   s3_sh[i]   <= al_sh[i];
                s3_sub[i]  <= al_sub[i];  s3_ebig[i] <= al_ebig[i];
                s3_sbig[i] <= al_sbig[i]; s3_lost[i] <= al_lost[i];
                s3_zero[i] <= al_zero[i]; s3_pass[i] <= al_pass[i];
                s3_pval[i] <= al_pval[i];
            end
        end
    end

    // ================================================ stage 4: add + leading one
    wire [SW-1:0] ra_norm [0:15];
    wire [5:0]    ra_lz   [0:15];
    wire          ra_sz   [0:15];

    generate
    for (g = 0; g < 16; g = g + 1) begin : g_round_a
        mx_fpacc_round_a #(.MW(ACC_MW)) u_ra (
            .bg_i(s3_bg[g]), .sh_i(s3_sh[g]), .sub_i(s3_sub[g]),
            .norm_o(ra_norm[g]), .lz_o(ra_lz[g]), .sum_zero_o(ra_sz[g])
        );
    end
    endgenerate

    reg [SW-1:0]  s4_norm [0:15];
    reg [5:0]     s4_lz   [0:15];
    reg           s4_sz   [0:15];
    reg [6:0]     s4_ebig [0:15];
    reg           s4_sbig [0:15], s4_lost [0:15], s4_zero [0:15], s4_pass [0:15];
    reg [AW-1:0]  s4_pval [0:15];
    reg [TW-1:0]  s4_chain;
    reg [TAW-1:0] s4_addr;
    reg           s4_wr, s4_fwd, v4;
    reg [1:0]     s4_out;

    always @(posedge clk) begin
        if (rst) begin
            v4 <= 1'b0; s4_addr <= {TAW{1'b0}}; s4_chain <= {TW{1'b0}};
            s4_wr <= 1'b0; s4_out <= OUT_NONE; s4_fwd <= 1'b0;
            for (i = 0; i < 16; i = i + 1) begin
                s4_norm[i] <= {SW{1'b0}}; s4_lz[i] <= 6'd0; s4_sz[i] <= 1'b0;
                s4_ebig[i] <= 7'd0; s4_sbig[i] <= 1'b0; s4_lost[i] <= 1'b0;
                s4_zero[i] <= 1'b0; s4_pass[i] <= 1'b0; s4_pval[i] <= {AW{1'b0}};
            end
        end else if (en) begin
            v4       <= v3;
            s4_addr  <= s3_addr;
            s4_chain <= s3_chain;
            s4_wr    <= s3_wr;
            s4_out   <= s3_out;
            s4_fwd   <= s3_fwd;
            for (i = 0; i < 16; i = i + 1) begin
                s4_norm[i] <= ra_norm[i]; s4_lz[i]   <= ra_lz[i];
                s4_sz[i]   <= ra_sz[i];   s4_ebig[i] <= s3_ebig[i];
                s4_sbig[i] <= s3_sbig[i]; s4_lost[i] <= s3_lost[i];
                s4_zero[i] <= s3_zero[i]; s4_pass[i] <= s3_pass[i];
                s4_pval[i] <= s3_pval[i];
            end
        end
    end

    // ================================================ stage 5: round + write
    wire [TW-1:0] rounded;
    generate
    for (g = 0; g < 16; g = g + 1) begin : g_round_b
        mx_fpacc_round_b #(.MW(ACC_MW)) u_rb (
            .norm_i(s4_norm[g]), .lz_i(s4_lz[g]), .sum_zero_i(s4_sz[g]),
            .e_big(s4_ebig[g]), .s_big(s4_sbig[g]), .lost(s4_lost[g]),
            .zero_i(s4_zero[g]), .pass_i(s4_pass[g]), .pass_val(s4_pval[g]),
            .s(rounded[g*AW +: AW])
        );
    end
    endgenerate

    // The tile memory has no reset -- block RAM has none -- so an address holds
    // x until written. CONTRACT: a tile must be opened with OP_LOAD, whose align
    // operand A is forced to zero rather than read from the RAM. OP_ADD into a
    // never-loaded tile reads x.
    //
    // `busy` means "not safe to take the control mux yet", which is MORE than "a
    // command is in the pipeline": taking the mux issues an EMIT, which reads a
    // tile address the in-flight command may be about to write, so busy must
    // cover the write AND the REUSE_MIN gap after it. A pipeline-only version
    // fails only when the GEMM is short enough that its tail has not cleared
    // when DRAIN asks, and then every sub-tile drains as zero.
    reg [3:0] busy_tail;
    // cmdv_q as well: stage 0 is a pipeline stage and busy must cover it, or a
    // DRAIN takes the mux while the new command is still one stage from v1.
    // cmdv_d1/d2 too under DUAL, or busy drops mid-command and the DRAIN reads
    // a half-written tile: some lanes right, others off by a power of two.
    wire in_flight = (
        cmd_valid
        || cmdv_q
        || ((DUAL != 0) && (cmdv_d1 || cmdv_d2))
        || v1
        || v1_s
        || v1a
        || v1b
        || v2
        || v3
        || v4
    );
    always @(posedge clk) begin
        if (rst) begin
            busy_tail <= 4'd0;
        end
        else if (!en) begin
            busy_tail <= busy_tail;
        end
        else if (in_flight) begin
            busy_tail <= REUSE_MIN[3:0];
        end
        else if (|busy_tail) begin
            busy_tail <= busy_tail - 4'd1;
        end
    end
    assign busy = in_flight || (|busy_tail);

    wire wr_en = en && v4 && s4_wr;
    assign wr_addr    = s4_addr;
    assign wr_data    = rounded;
    assign wr_bank_en = wr_en;

    reg [TW-1:0] emit_acc;
    reg          emit_pend;

    // ================================================ stage 6: FP16 convert
    wire [255:0] emit_fp16;
    generate
    for (g = 0; g < 16; g = g + 1) begin : g_cvt
        mx_fpacc_to_fp16 #(.MW(ACC_MW)) u_c (
            .fpa(emit_acc[g*AW +: AW]), .fp16(emit_fp16[g*16 +: 16])
        );
    end
    endgenerate

    always @(posedge clk) begin
        if (rst) begin
            peer_valid <= 1'b0; emit_valid <= 1'b0;
            peer_out <= {TW{1'b0}}; emit_out <= 256'd0;
            emit_acc <= {TW{1'b0}}; emit_pend <= 1'b0;
        end else if (en) begin
            peer_valid <= 1'b0;
            emit_valid <= 1'b0;
            emit_pend  <= 1'b0;

            if (v4) begin
                if (s4_out == OUT_SEND) begin
                    peer_out   <= rounded;
                    peer_valid <= 1'b1;
                end else if (s4_out == OUT_EMIT) begin
                    emit_acc  <= rounded;
                    emit_pend <= 1'b1;
                end else if (s4_fwd) begin
                    peer_out   <= s4_chain;
                    peer_valid <= 1'b1;
                end
            end

            if (emit_pend) begin
                emit_out   <= emit_fp16;
                emit_valid <= 1'b1;
            end
        end
    end

endmodule

`default_nettype wire
