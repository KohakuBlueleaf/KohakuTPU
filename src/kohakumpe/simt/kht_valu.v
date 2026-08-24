// kht_valu -- LANES copies of the base core's RV32I integer ALU, driven by one
// decoded operation.
//
// This is the whole of SIMT's arithmetic claim: the control is decoded ONCE and
// broadcast, and only the datapath is replicated. So the array costs almost
// exactly LANES times one lane, and frequency barely moves as LANES grows --
// widening adds copies, not depth.
//
// The arithmetic is rv_ex's, operation for operation, including the one barrel
// shifter shared by SLL/SRL/SRA between two bit reversals -- but a lane shares
// ONE carry chain across add/sub/slt/sltu where rv_ex builds four. It is copied
// rather than instantiated because rv_ex also carries the branch comparator, the
// address adder and the store-data mux, none of which a lane has: a lane never
// computes a branch (the wave does) and never forms a store address from its
// own ALU output alone.
//
// NO MULTIPLIER HERE. RV32M `mul` is kht_imul's, on its own multi-cycle retire
// slot beside the float tier, so this array stays single-cycle. A packed integer
// datapath is the SIMD PE's, and 09F is explicit that the SIMT PE should not pay
// for SIMD-only machinery -- if int8 dot products are ever wanted here they
// arrive as an extension with their own measurement.

`default_nettype none

module kht_valu #(
    parameter integer LANES = 8
)(
    input  wire [32*LANES-1:0] a,
    input  wire [32*LANES-1:0] b,
    // The decoded operation, shared by every lane. Registered by the caller, so
    // no decoder sits between the instruction word and this array.
    input  wire [3:0]          op,
    output wire [32*LANES-1:0] y
);
    localparam [3:0] A_ADD = 4'd0, A_SUB = 4'd1, A_SLL = 4'd2, A_SLT = 4'd3;
    localparam [3:0] A_SLTU = 4'd4, A_XOR = 4'd5, A_SRL = 4'd6, A_SRA = 4'd7;
    localparam [3:0] A_OR = 4'd8, A_AND = 4'd9;

    // ONE CARRY CHAIN FOR FOUR OPERATIONS: `la - lb` is `la + ~lb + 1` and its
    // carry-out IS the unsigned compare. As four separate expressions the tool
    // built four chains a lane and the array measured 2,909 LUT.
    wire is_sll = (op == A_SLL);
    wire is_sra = (op == A_SRA);
    wire is_xor = (op == A_XOR);
    wire is_or  = (op == A_OR);
    wire subm   = (op == A_SUB) || (op == A_SLT) || (op == A_SLTU);

    function [31:0] rev32;
        input [31:0] v;
        integer k;
        begin
            for (k = 0; k < 32; k = k + 1) begin
                rev32[k] = v[31-k];
            end
        end
    endfunction

    genvar L;
    generate
    for (L = 0; L < LANES; L = L + 1) begin : g_lane
        wire [31:0] la = a[32*L +: 32];
        wire [31:0] lb = b[32*L +: 32];
        wire [4:0]  sh = lb[4:0];

        wire [32:0] sum = {1'b0, la} + {1'b0, (subm ? ~lb : lb)} + {32'd0, subm};
        // Only read under SLT/SLTU, which force `subm`, so the carry is a borrow.
        wire        ltu = ~sum[32];
        wire        lt  = (la[31] ^ lb[31]) ? la[31] : ltu;

        // ONE DIRECTION BETWEEN TWO BIT REVERSALS, which are free wiring. Built
        // instead as a bidirectional barrel -- a stage picking in[i-w], in[i] or
        // in[i+w], five inputs and so nominally one LUT6 a bit either way -- the
        // array measured +193 LUT: the tool packs its own inferred shifter
        // tighter than the direction select allows.
        wire        sh_sign = is_sra && la[31];
        wire [32:0] sh_in   = {sh_sign, is_sll ? rev32(la) : la};
        wire [32:0] sh_out  = $signed(sh_in) >>> sh;
        wire [31:0] shift_r = is_sll ? rev32(sh_out[31:0]) : sh_out[31:0];

        // Five inputs a bit, so the three logic ops are one LUT6 rather than
        // three case arms the final mux then has to choose between.
        wire [31:0] logic_r = is_xor ? (la ^ lb) : is_or ? (la | lb) : (la & lb);

        reg [31:0] r;
        always @(*) begin
            case (op)
                A_SLT:   r = {31'd0, lt};
                A_SLTU:  r = {31'd0, ltu};
                A_XOR,
                A_OR,
                A_AND:   r = logic_r;
                A_SLL,
                A_SRL,
                A_SRA:   r = shift_r;
                default: r = sum[31:0];          // ADD and SUB
            endcase
        end
        assign y[32*L +: 32] = r;
    end
    endgenerate

endmodule

`default_nettype wire
