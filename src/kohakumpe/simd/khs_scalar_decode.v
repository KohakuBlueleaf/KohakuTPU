// khs_scalar_decode -- the three things the SCALAR pipeline needs to know about
// a vector instruction, and nothing else.
//
// The base core's hazard unit tracks scalar registers. A vector instruction
// touches them in only two ways, and getting either wrong is silent: `rs1` is a
// real scalar source for the two memory forms and the broadcast, so a load-use
// against it must stall; and `rd` is a real scalar destination for the three
// that reduce a vector to a word, so a consumer must forward from it.
// Everything else in the extension reads and writes the vector file, which the
// scalar hazard unit neither sees nor should.
//
// IT LIVES HERE, NOT IN rv_id, so the base core carries no dependency on the
// generated decode header: rv_id instantiates this under its SIMD_EN generate
// and a build without the extension does not include khs_isa.vh at all.

`default_nettype none

module khs_scalar_decode #(
    // Custom-1 is only claimed by a build that carries the float tier. Without
    // it the major is unmapped and a float instruction faults as an illegal
    // encoding, which is what makes "this build has no float" checkable.
    parameter integer HAS_FLOAT = 0
)(
    input  wire [31:0] instr,
    output wire        is_khd,
    output wire        use_rs1,     // rs1 is a scalar source
    output wire        wr_rd,       // rd is a scalar destination
    output wire        is_vld       // wants the scratchpad's address port in EX
);
`include "khs_isa.vh"

    wire [2:0] f3 = instr[14:12];
    wire [6:0] f7 = instr[31:25];

    wire is_int_maj = (instr[6:0] == KHS_OPCODE);
    wire is_flt_maj = (HAS_FLOAT != 0) && (instr[6:0] == KHF_OPCODE);

    assign is_khd = is_int_maj || is_flt_maj;

    // vld and vst take a base address; vsplat broadcasts a scalar. The float
    // tier reads no scalar registers at all -- its operands are vector
    // registers and accumulators -- so only its reduction appears below.
    assign use_rs1 = is_int_maj && ((f3 == KHS_F3_VLD) || (f3 == KHS_F3_VST)
                                || ((f3 == KHS_F3_VMOV) && (f7 == KHS_MOV_SPLAT)));

    assign is_vld = is_int_maj && (f3 == KHS_F3_VLD);

    assign wr_rd = (is_int_maj && (f3 == KHS_F3_VMOV)
                    && ((f7 == KHS_MOV_EXTR) || (f7 == KHS_MOV_REDSUM)
                        || (f7 == KHS_MOV_REDMAX)))
                 || (is_flt_maj && (f3 == KHF_F3_FRED));

endmodule

`default_nettype wire
