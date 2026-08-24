// khs_falu -- the elementwise float unit: mul, add, sub, fma, min, max,
// compare, and the four seeds. Register to register, no accumulator.
//
// THE ARITHMETIC IS THE LANE'S AND NOTHING HERE IS NEW. Every lane is one
// `khs_float_lane`, which is `vec_alu` with the operand conversion at its edge.
// vec_alu already routes operands per opcode -- ADD and SUB force the
// multiplier to 1.0 and read the addend on `c`, MUL forces the addend to zero,
// MIN/MAX/CMP select at cycle 1 and pass the winner through as winner*1.0+0 --
// so this file slices the register, drives the opcode, and packs the result.
//
// PACKED, WHICH IS WHAT MAKES THIS A SIMD UNIT AND NOT A THREAD ARRAY. A
// 256-bit register is 16 FP16 elements or 8 FP32 elements, exactly as the
// integer tier is 32 int8 / 16 int16 / 8 int32. FLANES lanes therefore walk
// ELEMS/FLANES passes, and FP16 gets twice FP32's throughput for the cost of
// the operand mux rather than for lanes. The SIMT PE places one element per
// 32-bit slot instead, because there a slot is a thread -- same lane, same
// E8M15, same conversions, so the two agree element for element.
//
// A COMPARE RETURNS A MASK, NOT A FLOAT. vec_alu's compare result is 1.0 or
// 0.0 and its `out_pred` carries the bit; the ISA promises all-ones or
// all-zeros so that the integer tier's vand/vandn/vor do the blend. Splatting
// `out_pred` is that conversion, and it is why the lane exposes the bit.

`default_nettype none

// TWO UNIT COUNTS, INDEPENDENT. FLANES is how many FMA units are built;
// SEED_UNITS is how many of them carry vec_alu's HAS_POLY -- the range
// reduction, the coefficient ROM and DSP-P. A unit without it is a plain FMA
// unit, one DSP48E2 and 1.5 BRAM cheaper. Real GPUs provision transcendentals
// at 1/4 rate (NVIDIA ~1 SFU per 4 FP32 lanes, AMD GCN/RDNA, Mali Valhall), so
// SEED_UNITS = FLANES/4 is the shape a shader is actually built for.
//
// The caller walks a seed over NARROW_ELEMS/SEED_UNITS passes and an FMA over
// NARROW_ELEMS/FLANES, and the ISA carries neither number.
module khs_falu #(
    parameter integer VW         = 256,
    parameter integer FLANES     = 4,
    parameter integer SEED_UNITS = 0,
    // Derived, and in the parameter list because the port list needs it: a
    // localparam in the body is declared too late to size a port. Sized for the
    // NARROWER of the two counts, which is the one that needs the most passes.
    parameter integer PSW = (
        ((VW / 16) / (
            ((SEED_UNITS != 0) && (SEED_UNITS < FLANES)) ? SEED_UNITS : FLANES
        )) > 1
        ? $clog2((VW / 16) / (
            ((SEED_UNITS != 0) && (SEED_UNITS < FLANES)) ? SEED_UNITS : FLANES
        ))
        : 1
    ),
    parameter integer PIPE_MUX = 1,
    parameter integer MODEL    = 0,
    // khs_unit's `d_fop` can only produce MUL ADD SUB FMA MIN MAX the three
    // compares and the four seeds -- never MOV, NEG, ABS, FNMA or SEL -- so
    // vec_alu's operand network loses the sources only those five named.
    parameter integer HAS_UNARY = 0,
    parameter integer HAS_FNMA  = 0,
    parameter integer HAS_SEL   = 0,
    // Forwarded to every lane; at one format `wide` is a constant here.
    parameter integer HAS_F16   = 1,
    parameter integer HAS_F32   = 1,
    // LUT is what binds this PE and flops are not, so every control delay is
    // flops: an SRL16E is one LUT per bit at any depth.
    parameter integer DLY_FF    = 16
)(
    input  wire                  clk,
    input  wire                  rst,

    input  wire                  in_valid,
    input  wire [4:0]            op,      // a vec_alu opcode, KHS_FOP_*
    input  wire                  is_cmp,  // splat out_pred instead of the value
    // 1 = FP32 elements, 0 = FP16. Per instruction, never a build option.
    input  wire                  wide,
    // Which FLANES-sized slice of the register this pass drives. The element
    // count differs by format, so the caller owns the pass count.
    input  wire [PSW-1:0]        pass,

    input  wire [VW-1:0]         v1,
    input  wire [VW-1:0]         v2,
    input  wire [VW-1:0]         vd,      // fma's addend, read from the dest

    output wire                  out_valid,
    // One 32-bit slot per lane: FP32 fills it, FP16 sits in the low half. The
    // caller places them, because where they land depends on the pass that is
    // retiring rather than on the pass being issued.
    output wire [32*FLANES-1:0]  out
);
    localparam integer NARROW_ELEMS = VW / 16;
    localparam integer EIW          = $clog2(NARROW_ELEMS);
    localparam integer ALAT         = 14 + ((PIPE_MUX != 0) ? 1 : 0);

    localparam [23:0] E8_ZERO = 24'd0;

    // vec_alu's own opcodes, for the addend select below.
    localparam [4:0] FOP_ADD = 5'd3, FOP_SUB = 5'd4, FOP_FMA = 5'd6;

    wire        wants_c = (op == FOP_ADD) || (op == FOP_SUB);
    wire [FLANES-1:0] lane_ov, lane_pred;

    // The four seeds are 16..19, so bit 4 names the group -- one bit, not four
    // compares. At SEED_UNITS = 0 this is a constant and the walk is one number.
    localparam integer SEED_U = (SEED_UNITS != 0) ? SEED_UNITS : FLANES;
    wire        is_sfu = (SEED_UNITS != 0) && op[4];
    // THE FMA WALK'S OWN PASS WIDTH. `pass` is sized for the SEED walk, which is
    // longer whenever there are fewer seed units -- and an FMA lane indexing
    // with all of it builds a 16-way element select where four are reachable.
    // Measured on the assembled SIMD PE: FSFU_UNITS 1 came out 260 LUT ABOVE
    // FSFU_UNITS 4, which is the whole quarter-rate saving and then some.
    localparam integer PSW_F = ((NARROW_ELEMS / FLANES) > 1) ? $clog2(NARROW_ELEMS / FLANES) : 1;

    // THE FORMAT AND THE COMPARE FLAG MUST ARRIVE WITH THE RESULT. `wide` and
    // `is_cmp` belong to whatever issued next by the time a result emerges, so
    // both travel the lane's depth beside it -- the trap kht_fpu records as
    // writing FP16 into an FP32 destination for any wave not running alone.
    // DECLARED BEFORE THE GENERATE THAT READS THEM: xvlog rejects the
    // use-before-declare that synthesis accepts silently.
    localparam integer DUAL = ((HAS_F16 != 0) && (HAS_F32 != 0)) ? 1 : 0;
    // The format the datapath actually runs at. A single-format build ignores the
    // port, so nothing downstream of here has a width select in it.
    wire w_eff = DUAL ? wide : ((HAS_F32 != 0) ? 1'b1 : 1'b0);

    reg [ALAT-1:0] cpipe;
    always @(posedge clk) begin
        if (rst) begin
            cpipe <= {ALAT{1'b0}};
        end
        else begin
            cpipe <= {cpipe[ALAT-2:0], is_cmp};
        end
    end
    wire y_cmp  = cpipe[ALAT-1];

    wire y_wide;
    generate
    if (DUAL) begin : g_wpipe
        reg [ALAT-1:0] wpipe;
        always @(posedge clk) begin
            if (rst) begin
                wpipe <= {ALAT{1'b0}};
            end
            else begin
                wpipe <= {wpipe[ALAT-2:0], wide};
            end
        end
        assign y_wide = wpipe[ALAT-1];
    end else begin : g_wfix
        assign y_wide = (HAS_F32 != 0);
    end
    endgenerate

    genvar L;
    generate
    for (L = 0; L < FLANES; L = L + 1) begin : g_lane
        // The element this lane owns on this pass. One index expression for
        // both formats: the slice width is what changes, not the ordering.
        //
        // FLANES IS NOT SLICED. `FLANES[PSW-1:0]` is 4 truncated to two bits,
        // which is ZERO -- every pass then read element L and the instruction
        // wrote pass 0's four results into all sixteen slots. The placement
        // above used the untruncated constant, so the slots varied while the
        // data did not, which is what the bench caught.
        // A SEED WALKS ITS OWN UNITS. `pass * units + L`, and the constants stay
        // full width -- `FLANES[PSW-1:0]` is 4 truncated to two bits, which is
        // ZERO, and every pass then reads element L.
        wire [EIW-1:0] e_idx = (is_sfu && (L < SEED_U))
                             ? (pass * SEED_U + L)
                             : (pass[PSW_F-1:0] * FLANES + L);
        // LEAVE THE WIDE SELECT ON `e_idx`. It is sized for the FP16 view, so
        // `v1[32*e_idx +: 32]` reads as a 16-way select over an 8-entry view --
        // and narrowing it to a 3-bit index measured 16,457 -> 16,489 LUT. The
        // tool already prunes the out-of-range half; the explicit slice only
        // adds one.
        wire [31:0] a_el = w_eff ? v1[32*e_idx +: 32]
                                 : {16'd0, v1[16*e_idx +: 16]};
        wire [31:0] b_el = w_eff ? v2[32*e_idx +: 32]
                                 : {16'd0, v2[16*e_idx +: 16]};
        wire [31:0] d_el = w_eff ? vd[32*e_idx +: 32]
                                 : {16'd0, vd[16*e_idx +: 16]};

        // ADD and SUB take their second operand on `c`, FMA takes the
        // destination there, and everything else leaves it at zero -- which is
        // the addend vec_alu wants for a bare product or a selection.
        wire [31:0] c_raw = wants_c ? b_el : d_el;
        wire [23:0] c_e8;
        wire        wants_e8 = wants_c || (op == FOP_FMA);
        if (DUAL) begin : g_c_dual
            wire [23:0] ch8, cw8;
            vec_cvt_f16_to_e8 u_cc   (.f16(c_raw[15:0]), .e8(ch8));
            vec_cvt_f32_to_e8 u_cc32 (.f32(c_raw),       .e8(cw8));
            assign c_e8 = wants_e8 ? (wide ? cw8 : ch8) : E8_ZERO;
        end else if (HAS_F16 != 0) begin : g_c_narrow
            wire [23:0] ch8;
            vec_cvt_f16_to_e8 u_cc (.f16(c_raw[15:0]), .e8(ch8));
            assign c_e8 = wants_e8 ? ch8 : E8_ZERO;
        end else begin : g_c_wide
            wire [23:0] cw8;
            vec_cvt_f32_to_e8 u_cc32 (.f32(c_raw), .e8(cw8));
            assign c_e8 = wants_e8 ? cw8 : E8_ZERO;
        end

        wire [23:0] y_e8;
        // ONLY THE SEED UNITS BUILD THE SEED HARDWARE. Everything above
        // SEED_UNITS is a plain FMA unit: vec_alu's `is_poly` is a constant
        // there, so the range reduction, vec_tables and DSP-P are not
        // elaborated at all rather than left for constant propagation to find.
        khs_float_lane #(.PIPE_MUX(PIPE_MUX), .MODEL(MODEL),
                         .HAS_UNARY(HAS_UNARY), .HAS_FNMA(HAS_FNMA),
                         .HAS_SEL(HAS_SEL), .HAS_POLY(L < SEED_UNITS),
                         .HAS_F16(HAS_F16), .HAS_F32(HAS_F32),
                         .DLY_FF(DLY_FF)) u_lane (
            .clk(clk), .rst(rst),
            .in_valid(in_valid), .op(op), .wide(w_eff),
            .a(a_el), .b(b_el), .c(c_e8),
            // The raw-E8 path belongs to the accumulator's fold; nothing here
            // feeds a partial back in.
            .raw_e8(1'b0), .a_e8(24'd0),
            .out_valid(lane_ov[L]), .out(y_e8), .out_pred(lane_pred[L])
        );

        if (DUAL) begin : g_y_dual
            wire [15:0] y16;
            wire [31:0] y32;
            vec_cvt_e8_to_f16 u_cy   (.e8(y_e8), .f16(y16));
            vec_cvt_e8_to_f32 u_cy32 (.e8(y_e8), .f32(y32));
            assign out[32*L +: 32] = y_cmp ? {32{lane_pred[L]}}
                                           : (y_wide ? y32 : {16'd0, y16});
        end else if (HAS_F16 != 0) begin : g_y_narrow
            wire [15:0] y16;
            vec_cvt_e8_to_f16 u_cy (.e8(y_e8), .f16(y16));
            assign out[32*L +: 32] = y_cmp ? {32{lane_pred[L]}} : {16'd0, y16};
        end else begin : g_y_wide
            wire [31:0] y32;
            vec_cvt_e8_to_f32 u_cy32 (.e8(y_e8), .f32(y32));
            assign out[32*L +: 32] = y_cmp ? {32{lane_pred[L]}} : y32;
        end
    end
    // A count that does not divide the element count leaves elements unserved by
    // a walk that still elaborates and reports an Fmax: khs_unit records that
    // costing a bench 10 of 66.
    if ((SEED_UNITS != 0)
        && ((((NARROW_ELEMS / SEED_UNITS) * SEED_UNITS) != NARROW_ELEMS)
            || (SEED_UNITS > FLANES))) begin : g_bad_su
        khs_falu_requires_SEED_UNITS_to_divide_2xSIMD_and_not_exceed_FLANES u_bad ();
    end
    endgenerate

    // Every lane is the same depth, so one valid speaks for all of them.
    assign out_valid = lane_ov[0];

endmodule

`default_nettype wire
