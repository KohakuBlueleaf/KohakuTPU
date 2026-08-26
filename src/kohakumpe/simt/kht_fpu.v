// kht_fpu -- the SIMT PE's float units (G9).
//
// THE ARITHMETIC IS THE SIMD TIER'S AND NOTHING HERE IS NEW. Every unit is one
// `rv_fpu`, IEEE binary32 in and out, and a seed unit carries `khs_fp32_sfu`
// beside it. Single-sourced arithmetic is what makes a SIMT float number
// comparable to a SIMD float number, element for element.
//
// FP32 IS THE ONLY COMPUTE TYPE, so a thread is a whole 32-bit slot and there
// is no format bit, no conversion and no reserved half. KohakuMPE holds no
// E8M15 anywhere; KohakuTPU's vector core keeps its own and is untouched.
//
// TWO INDEPENDENT UNIT COUNTS, AND THE ISA KNOWS NEITHER.
//
//   FLANES      how many FMA units are built     0,1,2,4,8
//   FSFU_UNITS  how many of them are SEED units  0,1,2,4,8, <= FLANES
//
// A thread count above either is served by PASSES = LANES/units, sequenced by
// kht_unit and counted here only through `pass`. Real GPUs provision
// transcendentals at 1/4 rate (NVIDIA ~1 SFU per 4 FP32 lanes, AMD GCN/RDNA and
// Mali Bifrost/Valhall the same), which is what FSFU_UNITS < FLANES buys.
//
// LATENCY IS ALAT AND II IS 1: 6 with no seeds, 9 with them, because the seed
// is three stages deeper and the FMA path pads to match. The core does not
// stall for it -- a wave with a float in flight is skipped by the scheduler,
// which restores the barrel-scheduling invariant. See kht_core's `fpend`.
//
// `vy` IS ONE SLOT PER UNIT, NOT PER THREAD. The caller places it, because
// where a result belongs depends on the pass that is retiring rather than on
// the pass being issued.

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
    // The caller's own shadow depth. It must equal this array's or a result
    // lands on the wrong register with no witness, so it is checked below.
    parameter integer ALAT = 6
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

    // rv_fpu's opcodes. Spelled here because this file includes the SIMT
    // header, not the SIMD one that generates KHS_FOP_*.
    localparam [4:0] FOP_ADD = 5'd3, FOP_SUB = 5'd4;
    localparam [4:0] FOP_MUL = 5'd5, FOP_FMA = 5'd6;

    localparam integer LNW = (LANES > 1) ? $clog2(LANES) : 1;
    localparam integer FMA_LAT = 6;
    localparam integer SFU_LAT = 10;
    localparam integer LAT = (FSFU_UNITS != 0) ? SFU_LAT : FMA_LAT;
    localparam integer PAD = LAT - FMA_LAT;

    wire [2:0] fop = op[2:0];
    // 4..7 are the seeds and their low two bits ARE khs_fp32_sfu's function
    // select, so nothing translates between the ISA and the unit.
    wire       is_seed = (FSFU_UNITS != 0) && fop[2];

    // EVERY ARITHMETIC FORM IS AN rv_fpu OPCODE, not an operand mux built here:
    // `vfadd` is the FMA with its multiplier forced to one and `vfmul` the FMA
    // with its addend forced to zero, and rv_fpu already does both.
    reg [4:0] arith_op;
    always @(*) begin
        case (fop[1:0])
            KHT_FLT_VFMUL[1:0]: arith_op = FOP_MUL;
            KHT_FLT_VFADD[1:0]: arith_op = FOP_ADD;
            KHT_FLT_VFSUB[1:0]: arith_op = FOP_SUB;
            default:            arith_op = FOP_FMA;
        endcase
    end
    // add and sub take their second operand on the ADDEND port; fma takes the
    // destination there.
    wire wants_b = (fop[1:0] == KHT_FLT_VFADD[1:0])
                || (fop[1:0] == KHT_FLT_VFSUB[1:0]);

    // WHICH WALK THIS RESULT CAME OFF must arrive WITH the result: by then `op`
    // belongs to whatever the scheduler picked next.
    reg [LAT:1] sfu_q;
    integer ci;
    always @(posedge clk) begin
        if (rst) begin
            sfu_q <= {LAT{1'b0}};
        end
        else begin
            sfu_q[1] <= is_seed && in_valid;
            for (ci = 2; ci <= LAT; ci = ci + 1) begin
                sfu_q[ci] <= sfu_q[ci-1];
            end
        end
    end

    // THE ONE INDEX EXPRESSION, per unit. `pass * units` is the thread this
    // unit serves, and `units` is the seed count on a seed and the FMA count
    // otherwise.
    localparam integer SEED_U = (FSFU_UNITS != 0) ? FSFU_UNITS : FLANES;
    // THE FMA WALK'S OWN PASS WIDTH. `pass` is sized for the SEED walk, which is
    // longer whenever there are fewer seed units, and an FMA unit indexing with
    // all of it builds an 8-way thread select where two are reachable. The SIMD
    // PE measured that costing 260 LUT -- more than quarter rate saved there.
    localparam integer PSW_A = ((LANES / FLANES) > 1) ? $clog2(LANES / FLANES) : 1;

    wire [FLANES-1:0] unit_ov;

    genvar L;
    generate
    for (L = 0; L < FLANES; L = L + 1) begin : g_funit
        localparam integer IS_SEED_UNIT = (L < FSFU_UNITS);

        // THE PASS INDEX is narrowed, never the unit COUNT. `FLANES[PSW-1:0]` is
        // 8 truncated to two bits, which is ZERO -- every pass would then read
        // element L and the walk would cover one slice forever.
        wire [LNW-1:0] e_idx = (IS_SEED_UNIT && is_seed)
                    ? (pass * SEED_U + L)
                    : (pass[PSW_A-1:0] * FLANES + L);

        wire [31:0] a32 = va[32*e_idx +: 32];
        wire [31:0] b32 = vb[32*e_idx +: 32];
        wire [31:0] d32 = vc[32*e_idx +: 32];

        wire [31:0] ly;
        rv_fpu u_lane (
            .clk(clk), .rst(rst),
            .in_valid(in_valid), .op(arith_op),
            .a(a32), .b(b32), .c(wants_b ? b32 : d32),
            .out_valid(unit_ov[L]), .y(ly), .out_pred()
        );

        wire [31:0] ly_d;
        if (PAD > 0) begin : g_pad
            // FLOPS, NOT AN SRL. An SRL16E is one LUT per bit at any depth and
            // this PE is LUT-bound; the pad belongs in the idle half of the CLB.
            (* srl_style = "register" *) reg [32*PAD-1:0] dly;
            always @(posedge clk) begin
                dly <= {dly[32*(PAD-1)-1:0], ly};
            end
            assign ly_d = dly[32*(PAD-1) +: 32];
        end else begin : g_nopad
            assign ly_d = ly;
        end

        if (IS_SEED_UNIT) begin : g_sfu
            wire [31:0] sy;
            khs_fp32_sfu u_sfu (
                .clk(clk), .rst(rst),
                .in_valid(in_valid && is_seed), .fsel(fop[1:0]), .a(a32),
                .out_valid(), .y(sy)
            );
            assign vy[32*L +: 32] = sfu_q[LAT] ? sy : ly_d;
        end else begin : g_fma
            assign vy[32*L +: 32] = ly_d;
        end
    end
    endgenerate

    generate
    if (PAD > 0) begin : g_ovd
        reg [PAD:1] ov_q;
        integer oi;
        always @(posedge clk) begin
            if (rst) begin
                ov_q <= {PAD{1'b0}};
            end
            else begin
                ov_q[1] <= unit_ov[0];
                for (oi = 2; oi <= PAD; oi = oi + 1) begin
                    ov_q[oi] <= ov_q[oi-1];
                end
            end
        end
        assign out_valid = ov_q[PAD];
    end else begin : g_ovz
        assign out_valid = unit_ov[0];
    end
    endgenerate

    // THE SHAPE CONSTRAINTS, ENFORCED AT ELABORATION. A unit count that does not
    // divide the thread count leaves threads unserved by a walk that still
    // elaborates, synthesises and reports an Fmax -- the failure khs_unit records
    // costing a bench 10 of 66.
    generate
    if ((FLANES != 0) && ((LANES / FLANES) * FLANES != LANES)) begin : g_bad_fl
        kht_fpu_requires_FLANES_to_divide_LANES u_bad ();
    end
    if ((FSFU_UNITS != 0)
        && (((LANES / FSFU_UNITS) * FSFU_UNITS != LANES) || (FSFU_UNITS > FLANES)))
    begin : g_bad_sf
        kht_fpu_requires_FSFU_UNITS_to_divide_LANES_and_not_exceed_FLANES u_bad ();
    end
    if (ALAT != LAT) begin : g_bad_alat
        kht_fpu_ALAT_must_match_the_arrays_own_depth u_bad ();
    end
    // -1 IS RESOLVED BY THE CALLER. Left negative here no seed unit is built
    // while `is_seed` still fires, so a seed returns the FMA's answer.
    if (FSFU_UNITS < 0) begin : g_bad_sfneg
        kht_fpu_FSFU_UNITS_must_be_resolved_before_it_arrives u_bad ();
    end
    endgenerate

endmodule

`default_nettype wire
