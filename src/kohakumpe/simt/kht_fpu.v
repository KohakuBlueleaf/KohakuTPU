// kht_fpu -- the SIMT PE's float units (G9).
//
// THE ARITHMETIC IS THE SIMD TIER'S AND NOTHING HERE IS NEW. Every unit is one
// `khs_float_lane`: FP32 or FP16 in, `wide` picking the conversion, E8M15
// through `vec_alu`'s FMA, E8M15 out. This file selects operands and converts
// the result back; it does not compute. Single-sourced arithmetic is what makes
// a SIMT float number comparable to a SIMD float number.
//
// THE REGISTER LAYOUT, which is architecture and not an implementation choice:
//
//     FP32 (vfma, vfmul, vfadd, vfsub -- the default)
//     vreg[31:0]   one element
//
//     FP16 (vfma_h, vfmul_h, vfadd_h, vfsub_h)
//     vreg[31:16]  element 1  RESERVED, must be written zero, reads undefined
//     vreg[15:0]   element 0  FP16
//
// Reserved rather than unused. Undefined bits become somebody's undefined
// behaviour, and packed 2xFP16 later turns element 1 live WITHOUT changing the
// layout -- so it is an opcode addition, not a migration. Both conversions into
// E8M15 keep the exponent field verbatim, so the golden model is the SIMD tier's
// model rather than a new emulation of a new rounding mode.
//
// LATENCY IS 15 AND II IS 1. `vec_alu` is 14 deep, 15 at PIPE_MUX=1 which is
// what ships. The core does not stall for it: a wave with a float in flight is
// skipped by the scheduler, which restores the barrel-scheduling invariant that
// a 15-cycle unit breaks. See kht_core's `fpend`.
//
// TWO INDEPENDENT UNIT COUNTS, AND THE ISA KNOWS NEITHER.
//
//   FLANES      how many FMA units are built     0,1,2,4,8
//   FSFU_UNITS  how many of them are SEED units  0,1,2,4,8, <= FLANES
//
// A thread count above either is served by PASSES = LANES/units, sequenced by
// kht_unit and counted here only through `pass`. Real GPUs provision
// transcendentals at 1/4 rate (NVIDIA ~1 SFU per 4 FP32 lanes, AMD GCN/RDNA and
// Mali Bifrost/Valhall the same), which is what FSFU_UNITS < FLANES buys: a seed
// unit carries vec_alu's HAS_POLY -- range reduction, coefficient ROM, DSP-P --
// and a plain FMA unit does not.
//
// `vy` IS ONE SLOT PER UNIT, NOT PER THREAD. The caller places it, because where
// a result belongs depends on the pass that is retiring rather than on the pass
// being issued -- khs_falu's contract, for the same reason.

`default_nettype none

module kht_fpu #(
    parameter integer LANES      = 8,
    parameter integer FLANES     = 8,
    parameter integer FSFU_UNITS = 0,
    // Derived, and in the parameter list because the port list needs it: a
    // localparam in the body is declared too late to size a port.
    parameter integer PSW = ((LANES / (((FSFU_UNITS != 0) && (FSFU_UNITS < FLANES))
                                       ? FSFU_UNITS : FLANES)) > 1)
                          ? $clog2(LANES / (((FSFU_UNITS != 0) && (FSFU_UNITS < FLANES))
                                             ? FSFU_UNITS : FLANES)) : 1,
    parameter integer PIPE_MUX = 1,
    parameter integer MODEL    = 0,
    // WHICH MEMORY FORMATS THE UNITS CARRY. A thread is a whole 32-bit slot in
    // EITHER format, so `e_idx` does not move with this -- only the converters
    // and the two muxes go, which is NOT what the SIMD tier's packed index does.
    parameter integer HAS_F16  = 1,
    parameter integer HAS_F32  = 1
)(
    input  wire                clk,
    input  wire                rst,

    input  wire                in_valid,
    input  wire [3:0]          op,
    // Which units-sized slice of the thread array this pass drives. The active
    // unit count differs between a seed and an FMA, so the caller owns the count
    // and this file owns the placement of one pass.
    input  wire [PSW-1:0]      pass,
    input  wire [32*LANES-1:0] va,      // vs1
    input  wire [32*LANES-1:0] vb,      // vs2
    input  wire [32*LANES-1:0] vc,      // vd, read back as the FMA's addend

    output wire                 out_valid,
    output wire [32*FLANES-1:0] vy
);
`include "kht_isa.vh"

    // 1.0 and 0.0 in each format. `vfadd` is the unit with its multiplier
    // forced to one and `vfmul` is the unit with its addend forced to zero --
    // one datapath, never a second adder or a second multiplier.
    localparam [15:0] F16_ONE = 16'h3C00;
    localparam [31:0] F32_ONE = 32'h3F80_0000;
    // vec_alu's opcodes. Spelled here because this file includes the SIMT
    // header, not the SIMD one that generates KHS_FOP_*.
    localparam [4:0] FOP_FMA = 5'd6, FOP_EXP2 = 5'd16, FOP_LOG2 = 5'd17;
    localparam [4:0] FOP_INV = 5'd18, FOP_RSQRT = 5'd19;

    localparam integer LNW = (LANES > 1) ? $clog2(LANES) : 1;

    // f7[3]: 0 = FP32 (the default), 1 = FP16 in the low half. The format is
    // the TOP bit so that adding an operation never disturbs it.
    localparam integer FMT_DUAL = ((HAS_F16 != 0) && (HAS_F32 != 0)) ? 1 : 0;
    wire       half = FMT_DUAL ? op[3] : (HAS_F16 != 0);
    wire [2:0] fop  = op[2:0];

    // 4..7 are the seeds, which vec_alu computes with its own opcodes. The
    // arithmetic four keep FMA and force operands instead -- one datapath.
    wire       is_seed = (FSFU_UNITS != 0) && fop[2];
    reg  [4:0] seed_op;
    always @(*) begin
        case (fop[1:0])
            2'd0:    seed_op = FOP_EXP2;
            2'd1:    seed_op = FOP_LOG2;
            2'd2:    seed_op = FOP_INV;
            default: seed_op = FOP_RSQRT;
        endcase
    end

    // THE FORMAT MUST ARRIVE WITH THE RESULT, NOT WITH THE OPERANDS. `y_e8`
    // emerges ALAT cycles after launch, by which time `op` belongs to whatever
    // the scheduler picked next -- selecting the output conversion from the
    // live `op` writes FP16 into an FP32 destination for any wave not alone.
    localparam integer ALAT = 14 + ((PIPE_MUX != 0) ? 1 : 0);
    wire y_half;
    generate
    if (FMT_DUAL) begin : g_hpipe
        reg [ALAT-1:0] hpipe;
        always @(posedge clk) begin
            if (rst) begin
                hpipe <= {ALAT{1'b0}};
            end
            else begin
                hpipe <= {hpipe[ALAT-2:0], half};
            end
        end
        assign y_half = hpipe[ALAT-1];
    end else begin : g_hfix
        assign y_half = (HAS_F16 != 0);
    end
    endgenerate

    // THE ONE INDEX EXPRESSION, per unit. `pass * units` is the thread this
    // unit serves, and `units` is the seed count on a seed and the FMA count
    // otherwise. At FLANES == LANES with no separate seed count both products
    // are zero and every index folds to the constant L.
    localparam integer SEED_U = (FSFU_UNITS != 0) ? FSFU_UNITS : FLANES;
    // THE FMA WALK'S OWN PASS WIDTH. `pass` is sized for the SEED walk, which is
    // longer whenever there are fewer seed units, and an FMA unit indexing with
    // all of it builds an 8-way thread select where two are reachable. The SIMD
    // PE measured that costing 260 LUT -- more than quarter rate saved there.
    localparam integer PSW_A = ((LANES / FLANES) > 1) ? $clog2(LANES / FLANES) : 1;

    genvar L;
    generate
    for (L = 0; L < FLANES; L = L + 1) begin : g_funit
        // A unit past the seed count is an FMA unit: its opcode is the constant
        // FMA, so vec_alu's tables and range reduction fold away exactly as they
        // do in a build with no seeds at all.
        localparam integer IS_SEED_UNIT = (L < FSFU_UNITS);

        // THE PASS INDEX is narrowed, never the unit COUNT. `FLANES[PSW-1:0]` is
        // 8 truncated to two bits, which is ZERO -- every pass would then read
        // element L and the walk would cover one slice forever. So the constants
        // stay full width, `pass` is sliced to its own walk's width, and only the
        // SUM is truncated.
        wire [LNW-1:0] e_idx = (IS_SEED_UNIT && is_seed)
                    ? (pass * SEED_U + L)
                    : (pass[PSW_A-1:0] * FLANES + L);

        wire [31:0] a32 = va[32*e_idx +: 32];
        wire [31:0] b32 = vb[32*e_idx +: 32];
        wire [31:0] d32 = vc[32*e_idx +: 32];

        // vfsub inverts vs2's SIGN BIT rather than subtracting: the unit has no
        // subtract, and negating a float is one bit -- bit 31 or bit 15.
        wire [31:0] nb32 = half ? {16'd0, ~b32[15], b32[14:0]}
                                : {~b32[31], b32[30:0]};
        wire [31:0] one  = half ? {16'd0, F16_ONE} : F32_ONE;

        // Hoisting the four selects out of the loop and folding the sign flip
        // into the addend's own mux -- one LUT6 a bit rather than a 4:1 and a
        // separate negate -- measured +0 LUT. This form stays.
        reg [31:0] sa, sb, sc;
        always @(*) begin
            if (IS_SEED_UNIT && is_seed) begin
                // vec_alu reads only `a` for a seed and forces the other two
                // itself, so this hands it the operand and nothing else.
                sa = a32; sb = one; sc = 32'd0;
            end else begin
                case (fop[1:0])
                    KHT_FLT_VFMUL[1:0]: begin
                        sa = a32; sb = b32; sc = 32'd0;
                    end
                    KHT_FLT_VFADD[1:0]: begin
                        sa = a32; sb = one; sc = b32;
                    end
                    KHT_FLT_VFSUB[1:0]: begin
                        sa = a32; sb = one; sc = nb32;
                    end
                    default: begin
                        sa = a32; sb = b32; sc = d32;
                    end
                endcase
            end
        end

        wire [4:0] unit_op = (IS_SEED_UNIT && is_seed) ? seed_op : FOP_FMA;

        // The addend crosses into the datapath format the same way the operands
        // do, and by the same conversions.
        //
        // BOTH CONVERTERS AND A MUX, not one dual-format converter. Merged into
        // a module picking on `wide` inside -- bit-exact, vec_cvt_tb checked it
        // over all 65,536 FP16 patterns -- the assembled PE measured +429 LUT at
        // `none` and +578 at `rebuilt`: 168 LUT a unit against 46 + 46 and a
        // 24-bit mux. The two conversions share nothing, so selecting between
        // their RESULTS is one mux and selecting inside them is three.
        wire [23:0] c_e8;
        if (FMT_DUAL) begin : g_c_dual
            wire [23:0] ch8, cw8;
            vec_cvt_f16_to_e8 u_cc   (.f16(sc[15:0]), .e8(ch8));
            vec_cvt_f32_to_e8 u_cc32 (.f32(sc),       .e8(cw8));
            assign c_e8 = half ? ch8 : cw8;
        end else if (HAS_F16 != 0) begin : g_c_narrow
            vec_cvt_f16_to_e8 u_cc (.f16(sc[15:0]), .e8(c_e8));
        end else begin : g_c_wide
            vec_cvt_f32_to_e8 u_cc32 (.f32(sc), .e8(c_e8));
        end

        wire [23:0] y_e8;
        wire        unit_ov;
        // HAS_POLY IS THE SEED HARDWARE and it is per unit: a plain FMA unit
        // loses vec_alu's range reduction, coefficient ROM, DSP-P and their
        // delay lines -- a DSP48E2 and 1.5 BRAM apiece, measured there.
        // The other three gates were measured on the assembled PE at HAS_FSFU=0
        // against 20,026: gating HAS_UNARY/HAS_FNMA/HAS_SEL off is +78 even
        // though `unit_op` is already a constant FMA here, and DLY_FF=16 is -56
        // (lut_srl 180 -> 124, ff +508) -- together +22. At HAS_FSFU=1 the
        // gating turns -40, so it is worth passing only if the seeds are built.
        khs_float_lane #(.PIPE_MUX(PIPE_MUX), .MODEL(MODEL),
                         .HAS_POLY(IS_SEED_UNIT),
                         .HAS_F16(HAS_F16), .HAS_F32(HAS_F32)) u_lane (
            .clk(clk), .rst(rst),
            .in_valid(in_valid), .op(unit_op), .wide(!half),
            .a(sa), .b(sb), .c(c_e8),
            // The fold path is the accumulator's, not a shader's: this tier
            // never feeds a raw partial back in.
            .raw_e8(1'b0), .a_e8(24'd0),
            .out_valid(unit_ov), .out(y_e8), .out_pred()
        );

        // On the FP16 path element 1 is RESERVED and reads back zero, so a
        // shader that ignores the contract still gets a defined value rather
        // than whatever the integer lanes last left there.
        if (FMT_DUAL) begin : g_y_dual
            wire [15:0] y16;
            wire [31:0] y32;
            vec_cvt_e8_to_f16 u_cy   (.e8(y_e8), .f16(y16));
            vec_cvt_e8_to_f32 u_cy32 (.e8(y_e8), .f32(y32));
            assign vy[32*L +: 32] = y_half ? {16'd0, y16} : y32;
        end else if (HAS_F16 != 0) begin : g_y_narrow
            wire [15:0] y16;
            vec_cvt_e8_to_f16 u_cy (.e8(y_e8), .f16(y16));
            assign vy[32*L +: 32] = {16'd0, y16};
        end else begin : g_y_wide
            wire [31:0] y32;
            vec_cvt_e8_to_f32 u_cy32 (.e8(y_e8), .f32(y32));
            assign vy[32*L +: 32] = y32;
        end

        if (L == 0) begin : g_ov
            assign out_valid = unit_ov;
        end
    end
    endgenerate

    // THE SHAPE CONSTRAINTS, ENFORCED AT ELABORATION. A unit count that does not
    // divide the thread count leaves threads unserved by a walk that still
    // elaborates, synthesises and reports an Fmax -- the failure khs_unit records
    // costing a bench 10 of 66. Instantiating a module that does not exist kills
    // the build with a name that says which rule broke.
    generate
    if ((FLANES != 0) && ((LANES / FLANES) * FLANES != LANES)) begin : g_bad_fl
        kht_fpu_requires_FLANES_to_divide_LANES u_bad ();
    end
    if ((FSFU_UNITS != 0)
        && (((LANES / FSFU_UNITS) * FSFU_UNITS != LANES) || (FSFU_UNITS > FLANES)))
    begin : g_bad_sf
        kht_fpu_requires_FSFU_UNITS_to_divide_LANES_and_not_exceed_FLANES u_bad ();
    end
    if ((HAS_F16 == 0) && (HAS_F32 == 0)) begin : g_bad_fmt
        kht_fpu_needs_HAS_F16_or_HAS_F32 u_bad ();
    end
    endgenerate

`ifndef SYNTHESIS
    // THE RESERVED HALF IS CHECKED, NOT ASSUMED. A float read whose upper half
    // is dirty means an integer instruction wrote that register and a shader
    // then used it as a float -- a wrong answer with no witness otherwise.
    integer ck;
    always @(posedge clk) begin
        if (!rst && in_valid && half) begin
            for (ck = 0; ck < LANES; ck = ck + 1) begin
                if (
                    (va[32*ck + 16 +: 16] != 16'd0)
                    || (vb[32*ck + 16 +: 16] != 16'd0)
                ) begin
                    $display("%0t ERROR kht_fpu: lane %0d read a float operand with vreg[31:16] = %04h/%04h -- the reserved half must be written zero",
                             $time, ck, va[32*ck + 16 +: 16], vb[32*ck + 16 +: 16]);
                end
            end
        end
    end
`endif

endmodule

`default_nettype wire
