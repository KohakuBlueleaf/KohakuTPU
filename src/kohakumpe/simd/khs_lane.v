// khs_lane -- one 32-bit lane of the SIMD datapath: the packed ALU, the packed
// shifter, the bitwise unit, and the multipliers.
//
// ONE IM UNIT, NOT AN ALU BESIDE A MULTIPLIER. RV32IM is the instruction set,
// not an option, so a lane adds, compares, masks and multiplies through one
// operand path and one result path. Two units behind one issue port would need
// an operand mux in front and a result mux behind, and on this fabric a mux is
// the expensive primitive -- wider than the logic it arbitrates.
//
// FOUR MULTIPLIERS, AND ONLY TWO OF THEM ARE WIDE. `vmul` on int8 needs four
// products per 32-bit lane; int16 needs two, and those two need 16x16. So
// multipliers 0 and 1 are 16x16 with a mux on their operands, and 2 and 3 are
// 8x8 and exist only for int8. int32 `vmul` is not an encoding this unit has.
//
// THE ROUNDING SHIFT HAS ITS OWN ADDER, and that is the second-largest timing
// decision in the unit. `vsrari` is (x >>> s) plus bit s-1 per element, and
// since it reads only one source vector the main adder's second input looked
// free -- so it borrowed it. Measured, that put the shifter in series with the
// adder on EVERY instruction, because the mux in front of the adder is in its
// cone whether or not a shift is issued: ~0.8 ns of a 4.72 ns path, paid by
// add, min, max and every compare. A second SWAR adder is four CARRY8 and a
// handful of LUTs, which is cheaper than what sharing cost.

`default_nettype none

module khs_lane #(
    // Refusing the ENCODING is not removing the hardware: with the shifter
    // still instantiated, a build "without" it measured 32 LUT LARGER than the
    // one with it, because the only thing that changed was a decode term.
    parameter integer HAS_SHIFT = 1,
    // `vsrari`'s round increment: a second SWAR adder per lane, rounding the
    // shifter's output. Rides on HAS_SHIFT and is separately removable.
    parameter integer HAS_SHROUND = 1,
    parameter         USE_DSP   = "yes"
)(
    input  wire        clk,

    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire [1:0]  et,

    // ---- combinational ALU, one cycle ----
    input  wire [2:0]  alu_op,      // see the localparams below
    input  wire        cmp_sub,     // subtract, INCLUDING for a min/max compare
    input  wire        alu_sat,
    input  wire [4:0]  sh_rot,
    input  wire [31:0] sh_keep,
    input  wire [31:0] sh_rmask,
    input  wire        sh_arith,
    input  wire        sh_left,
    input  wire        sh_round,
    // Each element's MSB, built once in EX and registered: deriving it per lane
    // would put a mux in front of the adder in the cycle that decides Fmax.
    input  wire [31:0] el_mask,
    output wire [31:0] y,

    // ---- the multipliers: packed low halves, valid ONE cycle later ----
    input  wire        mul_en,
    output wire [31:0] mul_lo
);
    localparam [2:0] OP_ADD = 3'd0, OP_MIN = 3'd1, OP_MAX = 3'd2, OP_AND = 3'd3;
    localparam [2:0] OP_OR = 3'd4, OP_XOR = 3'd5, OP_ANDN = 3'd6, OP_SH = 3'd7;

    localparam [1:0] ET_S8 = 2'd0;

    // ---- the shifter -----------------------------------------------------
    wire [31:0] sh_y, sh_round_bit;
    generate
    if (HAS_SHIFT != 0) begin : g_shift
        khs_pshift32 u_sh (
            .x(a), .et(et), .rot(sh_rot), .keep(sh_keep), .rmask(sh_rmask),
            .arith(sh_arith), .left(sh_left),
            .y(sh_y), .round_bit(sh_round_bit)
        );
    end else begin : g_noshift
        assign sh_y         = 32'd0;
        assign sh_round_bit = 32'd0;
    end
    endgenerate

    // ---- the main adder --------------------------------------------------
    // Its inputs are the OPERANDS and nothing else; see the header.
    // `cmp_sub` arrives ALREADY DECIDED. Deriving it here -- min and max need
    // a - b whatever the opcode asked for -- put a LUT between the decode
    // register and the carry chain, per lane, at the head of the binding path.
    // It is the same argument the shift masks are built in EX for.
    wire [31:0] add_y;
    wire [3:0]  lt, lt_s, top;
    khs_padd32 u_add (
        .a(a), .b(b), .sub(cmp_sub), .et(et), .mask(el_mask),
        .sat(alu_sat),
        .y(add_y), .lt(lt), .lt_s(lt_s), .top(top)
    );

    // ---- the rounding shift's own increment ------------------------------
    // THE ROUND BIT IS GATED, NOT THE RESULT. `sh_round_bit` is at most one bit
    // per element and the rest of it is a constant zero the tool has already
    // folded, so masking it is four LUTs -- where choosing between `rnd_y` and
    // `sh_y` afterwards is a 32-bit mux in the lane's output chain.
    wire [31:0] rnd_in = sh_round ? sh_round_bit : 32'd0;
    wire [31:0] rnd_y;
    generate
    if ((HAS_SHIFT != 0) && (HAS_SHROUND != 0)) begin : g_rnd
        khs_padd32 u_rnd (
            .a(sh_y), .b(rnd_in), .sub(1'b0), .et(et), .mask(el_mask),
            .sat(1'b0),
            .y(rnd_y), .lt(), .lt_s(), .top()
        );
    end else begin : g_no_rnd
        assign rnd_y = sh_y;
    end
    endgenerate

    // `sel_y` picks b where the flag is set, so a MIN takes b where a is NOT
    // less than b. Written the other way round it computes max for min and min
    // for max, which agrees with itself on every equal-operand test.
    wire [3:0] pick_b = (alu_op == OP_MIN) ? ~lt_s : lt_s;
    reg [31:0] sel_y;
    integer i;
    always @(*) begin
        for (i = 0; i < 4; i = i + 1) begin
            sel_y[i*8 +: 8] = pick_b[i] ? b[i*8 +: 8] : a[i*8 +: 8];
        end
    end

    reg [31:0] y_r;
    always @(*) begin
        case (alu_op)
            OP_MIN, OP_MAX: y_r = sel_y;
            OP_AND:         y_r = a & b;
            OP_OR:          y_r = a | b;
            OP_XOR:         y_r = a ^ b;
            OP_ANDN:        y_r = a & ~b;
            OP_SH:          y_r = rnd_y;
            default:        y_r = add_y;
        endcase
    end
    assign y = y_r;

    // ---- the multipliers -------------------------------------------------
    // 0 and 1 are wide and muxed; 2 and 3 exist only for int8.
    wire signed [16:0] m0a = (et == ET_S8) ? {{9{a[7]}},  a[7:0]}  : {a[15], a[15:0]};
    wire signed [16:0] m0b = (et == ET_S8) ? {{9{b[7]}},  b[7:0]}  : {b[15], b[15:0]};
    wire signed [16:0] m1a = (et == ET_S8) ? {{9{a[15]}}, a[15:8]} : {a[31], a[31:16]};
    wire signed [16:0] m1b = (et == ET_S8) ? {{9{b[15]}}, b[15:8]} : {b[31], b[31:16]};
    wire signed [8:0]  m2a = {a[23], a[23:16]};
    wire signed [8:0]  m2b = {b[23], b[23:16]};
    wire signed [8:0]  m3a = {a[31], a[31:24]};
    wire signed [8:0]  m3b = {b[31], b[31:24]};

    wire signed [33:0] p0, p1;
    wire signed [17:0] p2, p3;

    khs_mul #(.AW(17), .BW(17), .PW(34), .USE_DSP(USE_DSP)) u_m0 (
        .clk(clk), .en(mul_en), .a(m0a), .b(m0b), .p(p0));
    khs_mul #(.AW(17), .BW(17), .PW(34), .USE_DSP(USE_DSP)) u_m1 (
        .clk(clk), .en(mul_en), .a(m1a), .b(m1b), .p(p1));
    khs_mul #(.AW(9), .BW(9), .PW(18), .USE_DSP(USE_DSP)) u_m2 (
        .clk(clk), .en(mul_en), .a(m2a), .b(m2b), .p(p2));
    khs_mul #(.AW(9), .BW(9), .PW(18), .USE_DSP(USE_DSP)) u_m3 (
        .clk(clk), .en(mul_en), .a(m3a), .b(m3b), .p(p3));

    // The element type must reach the packing with the products, not with the
    // operands: by then the decode belongs to whatever issued a cycle later.
    reg [1:0] et_r;
    always @(posedge clk) begin
        if (mul_en) begin
            et_r <= et;
        end
    end

    // `vmul` keeps the low half of each element's product.
    assign mul_lo = (et_r == ET_S8)
                  ? {p3[7:0], p2[7:0], p1[7:0], p0[7:0]}
                  : {p1[15:0], p0[15:0]};

endmodule

`default_nettype wire
