// Accumulator CU: floating-point accumulation into a resident output tile,
// with peer transfer so a matmul can span clusters.
//
// The exact fixed-point mx_acu.v remains the bring-up instrument; it is what
// lets mx_cluster_tb check the datapath bit-for-bit. This is what ships.
//
//   stage 1   unpack the two chains, take |value| and apply the block scale,
//             both in one DSP
//   stage 2a  leading-one search and shift
//   stage 2b  round and assemble -> accumulator float
//   stage 3   read the tile, compare exponents, align        <- reads the tile
//   stage 4   add, leading-one search, shift
//   stage 5   round, assemble, write back                    <- writes the tile
//   stage 6   (EMIT only) convert to FP16
//
// Depth is not the constraint -- throughput is. Every stage outside the
// accumulate loop is a pure feed and can be split freely. Only the loop itself
// (stage 3 read -> stage 5 write) costs anything to lengthen, and what it costs
// is banks.
//
// ONE BANK, AND THE LOOP ORDER IS WHY.
//
// A pipelined adder cannot close a single-cycle accumulate loop: the result of
// cycle N is not available to cycle N+1. When K was the INNER loop that was the
// common case -- a stream of back-to-back accumulations into one address -- and
// it forced three rotating banks plus a fold on EMIT to put them back together.
//
// The ISA now sweeps K OUTERMOST (docs/compute/tensor-isa.md s5.1):
//
//     for kb:  for g:  for h:      <- an address recurs every Gm*Gn cycles
//
// which is 64 at the smallest useful tiling and 512 at the balanced one. There
// is no tight recurrence left, so the loop can be pipelined as deeply as the
// memory wants and ONE bank suffices. That removes the banks, the fold, the
// zero mask, and three quarters of the tile memory.
//
// What replaces them is a CONTRACT: consecutive commands to the same tile
// address must be at least REUSE_MIN cycles apart. It is checked in simulation
// rather than left implicit, so a caller that sweeps K on the inside fails
// loudly instead of quietly accumulating into stale data.
//
// ACC_MW sets the mantissa: 16 is FP24, 14 is FP22, 12 is FP20. E7 is fixed;
// range is not the tunable. See docs/compute/accumulator.md -- MW=14 measures
// identically to MW=16 and costs less, and is the default.
//
// TIMING: this block still sets the CU's critical path, and the CU now closes
// at 325.6 MHz out of context -- WNS +0.155 ns against a 310 MHz target,
// unplaced, so an upper bound. The binding path is stage 1 -> stage 2a,
// val_r -> b_sig, at 9 logic levels. Four rules keep it there, and breaking any
// of them has cost 20-60 MHz:
//
//   1. The magnitude is taken BEFORE the multiply, in the DSP. Taking it after
//      put a 30-bit two's-complement carry chain between the DSP's output
//      register and the leading-one search: 0.952 ns of a 3.401 ns path.
//   2. Nothing combinational in front of the tile address. Every select that
//      steers stage 3 is registered a stage early (a_sel, b_sel, addr_r2).
//   3. One mux level on the operands, not a chain of ternaries.
//   4. Zero-ness travels as one control bit per operand, never as a mask on
//      the 384-bit data.
//
// The same applies to the primitives in mx_fpacc.v: the leading-one search and
// the sticky bits must stay tree-shaped. Written as loops that carry a flag
// they became 25-deep LUT chains inside a single stage, which no amount of
// pipelining around the outside can fix.

`default_nettype none

module mx_acu_fp #(
    parameter integer DEPTH  = 16,          // resident output sub-tiles (4x4 each)
    parameter integer ACC_MW = 14,
    // Storage for the resident tile, named rather than inferred. "block" is
    // 5 BRAM36 for any depth up to 512 and 0 LUT; "distributed" is ~4,000 LUT
    // at depth 512 and misses 300 MHz by depth 64. See
    // .plan/measurements/memory-primitives.md.
    parameter         TILE_PRIM = "block"
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         en,

    input  wire [383:0] part_in,
    input  wire [31:0]  sa,
    input  wire [31:0]  sb,
    input  wire [7:0]   anchor,

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
                     // ADD AND HAND THE RESULT OUT, in one command.
                     //
                     // A sub-tile's last accumulation already computes its
                     // finished value at stage 5; a separate OP_EMIT reads the
                     // same address back and passes it through the same
                     // pipeline to recover it. That second pass is what a DRAIN
                     // is, and it costs a command slot per sub-tile -- which
                     // the sweep has none of, because it issues one command
                     // every cycle. So a drain could never overlap a sweep and
                     // was measured at 24% of the machine's time.
                     //
                     // Fused, the emit is free: same command, same cycle, no
                     // re-read. What it needs instead is somewhere to put the
                     // results, which is a queue and a backpressure signal
                     // rather than a stage of the run.
                     OP_ADD_EMIT = 3'd7;

    localparam [1:0] OUT_NONE = 2'd0, OUT_EMIT = 2'd1, OUT_SEND = 2'd2;

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
    wire [3:0] ma [0:3];
    wire [3:0] mb [0:3];
    wire signed [9:0] ea [0:3];
    wire signed [9:0] eb [0:3];
    // The product is declared 8 bits WIDE and on its own. Written inline as
    // `{1'b0, ma[i]*mb[j]}` the multiply is self-determined to its 4-bit
    // operand width inside the concatenation, so 8*8 = 64 truncates to 0 --
    // silently, and every result comes out zero.
    wire [7:0] mm [0:3][0:3];
    genvar gs, gt;
    generate
    for (gs = 0; gs < 4; gs = gs + 1) begin : g_scale
        assign ma[gs] = {1'b1, sa[gs*8 +: 3]};              // 8..15
        assign mb[gs] = {1'b1, sb[gs*8 +: 3]};
        assign ea[gs] = $signed({5'b0, sa[gs*8+3 +: 5]});   // biased exponent
        assign eb[gs] = $signed({5'b0, sb[gs*8+3 +: 5]});
        for (gt = 0; gt < 4; gt = gt + 1) begin : g_mm
            assign mm[gs][gt] = {4'd0, ma[gs]} * {4'd0, mb[gt]};   // 64..225
        end
    end
    endgenerate

    // ---- lane magnitude, taken BEFORE the multiply -----------------------
    // `mm` is unsigned, so the product's sign is the chain value's, and
    //
    //     |v + r| * mm  =  ((v ^ {W{s}}) + (s ^ r)) * mm,      s = v[W-1]
    //
    // which is the same `(v + bit) * mm` shape the odd lanes already had, plus
    // one XOR level on a register output. Taking the magnitude AFTER the
    // multiply instead put a
    // 30-bit two's-complement carry chain between the DSP's output register and
    // the leading-one search -- 0.952 ns of a 3.401 ns path, four logic levels,
    // and the reason the CU sat at 294.9 MHz. See docs/compute/accumulator.md.
    //
    // `r` is a rounding bit, so `v + r` can only reach zero from v = -1: the
    // one case where `s` disagrees with the true sign of the sum. The magnitude
    // is zero there and `is_zero` discards the sign, so it does not matter.
    wire [VWM-1:0] lane_mag [0:15];
    wire           lane_sgn [0:15];

    generate
    for (gs = 0; gs < 4; gs = gs + 1) begin : g_lane_r
        for (gt = 0; gt < 4; gt = gt + 1) begin : g_lane_c
            localparam integer F = ((gs/2)*4 + gt) * 48;
            if ((gs % 2) == 0) begin : g_lo
                wire        s = part_in[F+18];
                wire [18:0] x = part_in[F +: 19] ^ {19{s}};
                assign lane_sgn[gs*4+gt] = s;
                assign lane_mag[gs*4+gt] = ({1'b0, x} + {19'd0, s}) * mm[gs][gt];
            end else begin : g_hi
                wire        s = part_in[F+40];
                wire        r = part_in[F+18];
                wire [21:0] x = part_in[F+19 +: 22] ^ {22{s}};
                assign lane_sgn[gs*4+gt] = s;
                assign lane_mag[gs*4+gt] = ({1'b0, x} + {22'd0, (s ^ r)})
                                         * mm[gs][gt];
            end
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
            v1     <= cmd_valid;
            op_r   <= op;
            addr_r <= tile_addr;
            peer_r <= peer_in;
            for (i = 0; i < 4; i = i + 1)
                for (j = 0; j < 4; j = j + 1) begin
                    val_r[i*4+j] <= lane_mag[i*4+j];
                    sgn_r[i*4+j] <= lane_sgn[i*4+j];
                    // -6 undoes the /64 the mantissa product carries
                    exp_r[i*4+j] <= ea[i] + eb[j]
                                  - $signed({2'b0, anchor}) - 10'sd6;
                end
        end
    end

    // ================================================ stage 2a: leading one
    wire [ACC_MW:0] n_sig  [0:15];
    wire [5:0]      n_msb  [0:15];
    wire            n_g    [0:15], n_s [0:15], n_z [0:15];

    genvar g;
    generate
    for (g = 0; g < 16; g = g + 1) begin : g_norm_a
        mx_fpacc_norm_a #(.MW(ACC_MW), .VW(VWM)) u_na (
            .mag(val_r[g]),
            .sig(n_sig[g]), .msb(n_msb[g]), .guard(n_g[g]),
            .sticky(n_s[g]), .is_zero(n_z[g])
        );
    end
    endgenerate

    reg [ACC_MW:0]   b_sig [0:15];
    reg [5:0]        b_msb [0:15];
    reg              b_g [0:15], b_s [0:15], b_z [0:15], b_sgn [0:15];
    reg signed [9:0] b_exp [0:15];
    reg [2:0]        op_1b;
    reg [TAW-1:0]    addr_b;
    reg              v1b;
    reg [TW-1:0]     peer_b;

    always @(posedge clk) begin
        if (rst) begin
            v1b <= 1'b0; op_1b <= OP_NOP; addr_b <= {TAW{1'b0}};
            peer_b <= {TW{1'b0}};
            for (i = 0; i < 16; i = i + 1) begin
                b_sig[i] <= {(ACC_MW+1){1'b0}}; b_msb[i] <= 6'd0;
                b_g[i] <= 1'b0; b_s[i] <= 1'b0; b_z[i] <= 1'b0;
                b_sgn[i] <= 1'b0; b_exp[i] <= 10'sd0;
            end
        end else if (en) begin
            v1b    <= v1;
            op_1b  <= op_r;
            addr_b <= addr_r;
            peer_b <= peer_r;
            for (i = 0; i < 16; i = i + 1) begin
                b_sig[i] <= n_sig[i]; b_msb[i] <= n_msb[i];
                b_g[i]   <= n_g[i];   b_s[i]   <= n_s[i];
                b_z[i]   <= n_z[i];   b_sgn[i] <= sgn_r[i];
                b_exp[i] <= exp_r[i];
            end
        end
    end

    // ================================================ stage 2b: assemble
    wire [AW-1:0] normed [0:15];
    wire [TW-1:0] chain_fp;
    generate
    for (g = 0; g < 16; g = g + 1) begin : g_norm_b
        mx_fpacc_norm_b #(.MW(ACC_MW)) u_nb (
            .sig(b_sig[g]), .msb(b_msb[g]), .guard(b_g[g]), .sticky(b_s[g]),
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

    // Every select that steers the align stage is REGISTERED, not decoded in
    // the align cycle. The align stage starts at a LUTRAM read, so anything
    // combinational in front of the address or the operand mux lands ahead of
    // the RAM, the exponent compare and the barrel shift -- the longest chain
    // in the block. All of these are knowable one stage early, so none of them
    // has to be there.
    reg [2:0] a_sel;    // align operand A source: 0 = tile, 1 = zero
    reg [2:0] b_sel;    // align operand B source: 0 = chain, 1 = peer, 2 = zero

    always @(posedge clk) begin
        if (rst) begin
            chain_r <= {TW{1'b0}}; peer_r2 <= {TW{1'b0}};
            op_r2 <= OP_NOP; addr_r2 <= {TAW{1'b0}}; v2 <= 1'b0;
            a_sel <= 3'd1; b_sel <= 3'd2;
        end else if (en) begin
            chain_r <= chain_fp;
            peer_r2 <= peer_b;
            op_r2   <= op_1b;
            v2      <= v1b;

            // only updated on a valid command; everything downstream of it is
            // gated by v2, so it holds rather than clears
            if (v1b) addr_r2 <= addr_b;

            // Operand sources, resolved one stage ahead of the align cycle so
            // nothing combinational sits in front of the tile read.
            if (v1b && (op_1b == OP_LOAD)) begin
                a_sel <= 3'd1; b_sel <= 3'd0;           // zero + chain
            end else if (v1b && (op_1b == OP_ADD_PEER)) begin
                a_sel <= 3'd0; b_sel <= 3'd1;           // tile + peer
            end else if (v1b && ((op_1b == OP_ADD) ||
                                 (op_1b == OP_ADD_EMIT))) begin
                a_sel <= 3'd0; b_sel <= 3'd0;           // tile + chain
            end else if (v1b && ((op_1b == OP_EMIT) || (op_1b == OP_SEND))) begin
                a_sel <= 3'd0; b_sel <= 3'd2;           // tile + zero: pass out
            end
        end
    end

    // ================================================ stage 3: read + align
    // ONE bank, and the tile is an explicitly named memory with its OUTPUT
    // REGISTER enabled.
    //
    // The banks are gone. They existed to close a single-cycle accumulate loop
    // when K was the inner loop, so the same tile address was hit on
    // back-to-back cycles. The ISA now sweeps K OUTERMOST
    // (docs/compute/tensor-isa.md s5.1), so an address recurs only every
    // Gm*Gn cycles -- 64 at the smallest useful tiling. There is no tight
    // recurrence left to close, so read latency is free and one bank suffices.
    // That deletes 3/4 of the tile memory, the EMIT fold, and the zero mask.
    //
    // READ_LAT=2 uses the block RAM's own output register. Without it the path
    // starts at the RAM array access (~1.2 ns clock-to-out) instead of a
    // flip-flop, which cost ~70 MHz. The same memory measures 837 MHz in
    // isolation -- see .plan/measurements/memory-primitives.md -- so anything
    // slower than that is this module's logic, not the RAM.
    //
    // With READ_LAT=2 the address must lead the data by two cycles: sampled at
    // the end of stage 2a, valid during stage 3.
    // THE SOURCE OF TRUTH for the reuse distance. Two callers encode it
    // independently and CANNOT see this value: mx_cluster_mgr's pacing buckets
    // and mx_cluster_cu's `i_wide` both expand `Gm*Gn < 5` into small tests on
    // Gm and Gn, because computing the product cost the CU 26 MHz. Each carries
    // an elaboration check that fires if its own copy of 5 stops matching, but
    // nothing links them to this line -- so changing it means changing all
    // three.
    localparam integer REUSE_MIN = 5;   // cycles between commands to one address

    wire [TW-1:0]  t0;
    wire [TW-1:0]  wr_data;
    wire [TAW-1:0] wr_addr;
    wire           wr_bank_en;

    kohaku_sdpram #(.WIDTH(TW), .DEPTH(DEPTH), .MEM_PRIM(TILE_PRIM), .READ_LAT(2))
    u_tile (.clk(clk), .wr_en(wr_bank_en), .wr_addr(wr_addr), .wr_data(wr_data),
            .rd_en(en), .rd_addr(addr_r), .rd_data(t0));

    wire norm_iss = v2 && ((op_r2 == OP_LOAD) || (op_r2 == OP_ADD) ||
                           (op_r2 == OP_ADD_PEER) || (op_r2 == OP_ADD_EMIT));
    wire fwd_iss  = v2 && (op_r2 == OP_FWD);
    wire sum_iss  = v2 && ((op_r2 == OP_EMIT) || (op_r2 == OP_SEND));
    // ADD_EMIT both writes the tile back and hands the value out, so it is in
    // norm_iss (the write) and drives OUT_EMIT (the output) at once.
    wire out_emit = v2 && ((op_r2 == OP_EMIT) || (op_r2 == OP_ADD_EMIT));

    // ONE mux level, on registered selects.
    //   a_sel  0 = tile   1 = zero (LOAD adds the chain to nothing)
    //   b_sel  0 = chain  1 = peer   2 = zero (EMIT/SEND pass the tile out)
    wire [TW-1:0] op_a  = (a_sel == 3'd0) ? t0 : {TW{1'b0}};
    wire [TW-1:0] op_bb = (b_sel == 3'd0) ? chain_r :
                          (b_sel == 3'd1) ? peer_r2 : {TW{1'b0}};

    wire a_zero = (a_sel == 3'd1);
    wire b_zero = (b_sel == 3'd2);

    wire       iss_wr  = norm_iss;
    wire [1:0] iss_out = (v2 && (op_r2 == OP_SEND)) ? OUT_SEND
                       : out_emit                   ? OUT_EMIT
                                                    : OUT_NONE;

`ifndef SYNTHESIS
    // The reuse distance is now a CONTRACT, not a structure. Check it, so a
    // caller that sweeps K on the inside fails loudly instead of quietly
    // accumulating into stale data.
    // A separate valid bit, not a sentinel address. Using {TAW{1'b1}} to mean
    // "no command" collides with the real top address -- at TAW=4 that is
    // address 15, which produced a stream of false violations on a design that
    // was correct.
    reg [TAW-1:0] last_addr [0:REUSE_MIN-1];
    reg           last_vld  [0:REUSE_MIN-1];
    integer rq;
    always @(posedge clk) begin
        if (rst) begin
            for (rq = 0; rq < REUSE_MIN; rq = rq + 1) last_vld[rq] <= 1'b0;
        end else if (en) begin
            if (v2 && (norm_iss || sum_iss)) begin
                for (rq = 0; rq < REUSE_MIN-1; rq = rq + 1)
                    if (last_vld[rq] && last_addr[rq] == addr_r2)
                        $display("%0t ERROR mx_acu_fp: address %0d reused within %0d cycles -- sweep K outermost",
                                 $time, addr_r2, REUSE_MIN);
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

    // Write side of the tile memory. There is no reset here and there cannot be
    // -- block RAM has none -- so an address holds x until something writes it.
    // Nothing reads it before then, and that is a contract on the caller: a tile
    // must be opened with OP_LOAD, whose align operand A is forced to zero
    // rather than read from the RAM. OP_ADD into a never-loaded tile reads x.
    //
    // What `busy` has to mean is "not safe to take the control mux yet", and
    // that is strictly more than "a command is in the pipeline". Taking the mux
    // means issuing an EMIT, which reads a tile address the command still in
    // flight may be about to write -- so this has to cover the write AND the
    // REUSE_MIN gap after it, or the read beats the write to the same address.
    //
    // A pipeline-only version reads correct and fails only when the whole GEMM
    // is short enough that its tail has not cleared when DRAIN asks. Every
    // sub-tile then drains as zero, which looks like a compute bug.
    reg [3:0] busy_tail;
    wire      in_flight = cmd_valid || v1 || v1b || v2 || v3 || v4;
    always @(posedge clk) begin
        if (rst)            busy_tail <= 4'd0;
        else if (!en)       busy_tail <= busy_tail;
        else if (in_flight) busy_tail <= REUSE_MIN[3:0];
        else if (|busy_tail) busy_tail <= busy_tail - 4'd1;
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
