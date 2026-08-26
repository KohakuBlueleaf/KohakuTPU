// SysCore's integer ALU: RV64I's whole arithmetic, plus the `W` forms.
//
// ONE ADDER, ONE SHIFTER. Subtract is add-with-inverted-operand and the carry
// out of that same adder IS the unsigned compare, so `SLTU` costs a wire rather
// than a second 64-bit comparator. `SLT` differs from it only in the sign
// correction. On this part LUT is the objective, and a duplicated 64-bit adder
// is the easiest way to spend it by accident.
//
// THE `W` FORMS ARE THE SAME HARDWARE. `ADDW`/`SUBW`/`SLLW`/`SRLW`/`SRAW`
// operate on the low 32 bits and sign-extend bit 31. The only structural cost is
// feeding the shifter a 32-bit operand and a 5-bit amount, then extending -- so
// the shifter is one instance, not two.

`default_nettype none

module rv64_alu (
    input  wire [3:0]  op,          // RV64_ALU_*
    input  wire        word,        // the W form: operate on 32, sign-extend
    input  wire [63:0] a,
    input  wire [63:0] b,
    output reg  [63:0] y
);
`include "rv64_defs.vh"

    // ---- the one adder ------------------------------------------------------
    wire sub = (op == RV64_ALU_SUB) || (op == RV64_ALU_SLT)
            || (op == RV64_ALU_SLTU);
    wire [63:0] bx = sub ? ~b : b;
    wire [64:0] sum = {1'b0, a} + {1'b0, bx} + {64'd0, sub};

    // CARRY OUT IS THE UNSIGNED COMPARE. With sub asserted the adder computes
    // a - b, and its carry is set exactly when a >= b, so `a < b` is its
    // inverse. No second comparator anywhere in this file.
    wire ltu = ~sum[64];
    // Signed differs only when the operands' signs differ, in which case the
    // negative one is the smaller.
    wire lt = (a[63] ^ b[63]) ? a[63] : ltu;

    // ---- the one shifter, and it really is one -------------------------------
    // MEASURED: `<<`, `>>` and `>>>` as three expressions built THREE 64-bit
    // barrel shifters -- 1,038 LUT for this module against ~600 estimated.
    //
    // A left shift is a right shift between two bit reversals, and reversal is
    // wiring. Arithmetic versus logical is then just which bit feeds the vacated
    // positions, so one arithmetic right shifter covers SLL, SRL, SRA and all
    // three W forms.
    wire [5:0]  sh  = word ? {1'b0, b[4:0]} : b[5:0];
    wire [63:0] sl  = word ? {32'd0, a[31:0]} : a;
    wire [63:0] sra_in = word ? {{32{a[31]}}, a[31:0]} : a;

    function [63:0] rev64;
        input [63:0] v;
        integer i;
        begin
            for (i = 0; i < 64; i = i + 1) begin
                rev64[i] = v[63 - i];
            end
        end
    endfunction

    wire is_left  = (op == RV64_ALU_SLL);
    wire is_arith = (op == RV64_ALU_SRA);

    wire [63:0] sh_in  = is_left  ? rev64(sl)
                       : is_arith ? sra_in
                                  : sl;
    wire [64:0] sh_ext = {is_arith ? sra_in[63] : 1'b0, sh_in};
    wire [64:0] sh_q   = $signed(sh_ext) >>> sh;
    wire [63:0] shifted = is_left ? rev64(sh_q[63:0]) : sh_q[63:0];

    reg [63:0] raw;
    always @(*) begin
        case (op)
            RV64_ALU_ADD, RV64_ALU_SUB: raw = sum[63:0];
            RV64_ALU_SLL:               raw = shifted;
            RV64_ALU_SLT:               raw = {63'd0, lt};
            RV64_ALU_SLTU:              raw = {63'd0, ltu};
            RV64_ALU_XOR:               raw = a ^ b;
            RV64_ALU_SRL:               raw = shifted;
            RV64_ALU_SRA:               raw = shifted;
            RV64_ALU_OR:                raw = a | b;
            RV64_ALU_AND:               raw = a & b;
            // LUI and AUIPC arrive with the whole value already on `b`, so the
            // ALU is a pass rather than a special case in the decode.
            RV64_ALU_PASSB:             raw = b;
            default:                    raw = sum[63:0];
        endcase
    end

    // A `W` RESULT IS ALWAYS SIGN-EXTENDED FROM BIT 31, including SLTU's, which
    // is zero or one and so extends to itself -- stating it once here is
    // cheaper and safer than deciding per operation.
    always @(*) begin
        y = word ? {{32{raw[31]}}, raw[31:0]} : raw;
    end

endmodule

`default_nettype wire
