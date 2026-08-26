// The FP32 elementwise vector ALU: FLANES units walking a 32-bit-per-element
// vector, FLANES elements a pass, with FSFU_UNITS of them seed-capable.
//
// PROJECT-OWNED AND FP32 THROUGHOUT. KohakuTPU's `vec_alu` and `vec_cvt` are
// not instantiated here and are not modified anywhere -- the vector core keeps
// computing in E8M15, and KohakuMPE holds no E8M15 at all.
//
// ONE ELEMENT WIDTH. With FP32 the only compute type there are no narrow slots,
// no `wide` select and no 2*SIMD packing: a VW-bit vector is VW/32 elements and
// the walk is ELEMS/FLANES passes.
//
// TWO WALKS, ONE ARRAY. A seed unit is `khs_fp32_sfu` beside the FMA rather
// than inside it, and there are FSFU_UNITS of them, so a seed walks
// ELEMS/FSFU_UNITS passes where an FMA walks ELEMS/FLANES. The caller places
// the results, because where one belongs depends on the pass that is retiring.
//
// THE SEED IS 9 DEEP AND THE FMA IS 6, so the FMA is padded to match whenever
// seeds are built and by nothing when they are not. The pad is FLIP-FLOPS: LUT
// is what binds this PE, an SRL16E is one LUT per bit at any depth.

`default_nettype none

module khs_fp32_alu #(
    parameter integer VW     = 256,
    parameter integer FLANES = 8,
    parameter integer FSFU_UNITS = 0,
    // Derived, and in the parameter list because the port list needs it.
    parameter integer PSW = (((VW / 32) / ((FLANES == 0) ? 1 : FLANES)) > 1)
                          ? $clog2((VW / 32) / ((FLANES == 0) ? 1 : FLANES)) : 1,
    // The caller's own depth. It must equal this array's or a result lands on
    // the wrong register with no witness, so it is checked at elaboration.
    parameter integer ALAT = 6
)(
    input  wire                  clk,
    input  wire                  rst,
    input  wire                  in_valid,
    input  wire [4:0]            op,
    input  wire                  is_cmp,
    input  wire [PSW-1:0]        pass,
    input  wire [VW-1:0]         v1,
    input  wire [VW-1:0]         v2,
    input  wire [VW-1:0]         vd,
    output wire                  out_valid,
    output wire [32*FLANES-1:0]  out
);
    localparam integer ELEMS = VW / 32;
    localparam integer FL    = (FLANES == 0) ? 1 : FLANES;
    localparam integer EIW   = (ELEMS > 1) ? $clog2(ELEMS) : 1;
    localparam integer SEED_U = (FSFU_UNITS != 0) ? FSFU_UNITS : FL;
    // THE FMA WALK'S OWN PASS WIDTH. `pass` is sized for the SEED walk, which
    // is longer whenever there are fewer seed units, and an FMA unit indexing
    // with all of it builds a wider element select than it can reach.
    localparam integer PSW_A = ((ELEMS / FL) > 1) ? $clog2(ELEMS / FL) : 1;

    localparam integer FMA_LAT = 6;
    localparam integer SFU_LAT = 10;
    localparam integer LAT = (FSFU_UNITS != 0) ? SFU_LAT : FMA_LAT;
    localparam integer PAD = LAT - FMA_LAT;

    // The four seeds are 16..19, so bit 4 names the group -- one bit, not four
    // compares -- and its low two bits are khs_fp32_sfu's own function select.
    wire is_seed = (FSFU_UNITS != 0) && op[4];

    // ADD AND SUB TAKE THEIR SECOND OPERAND ON THE ADDEND PORT. rv_fpu computes
    // `a * 1.0 + c` for both, so wiring `c` to the destination -- which is right
    // for `vfma` and only for `vfma` -- makes `vfadd vd, vs1, vs2` add vd rather
    // than vs2. It answers a plausible finite, and it cost khs_unit 6 checks
    // across three cases before the operand select came back.
    localparam [4:0] FOP_ADD = 5'd3, FOP_SUB = 5'd4;
    wire wants_c = (op == FOP_ADD) || (op == FOP_SUB);

    // A compare's mask and a seed's result both belong to the instruction that
    // ISSUED, not to whatever is issuing when they emerge.
    reg [LAT:1] cmp_q, sfu_q;
    integer ci;
    always @(posedge clk) begin
        if (rst) begin
            cmp_q <= {LAT{1'b0}};
            sfu_q <= {LAT{1'b0}};
        end
        else begin
            cmp_q[1] <= is_cmp && in_valid;
            sfu_q[1] <= is_seed && in_valid;
            for (ci = 2; ci <= LAT; ci = ci + 1) begin
                cmp_q[ci] <= cmp_q[ci-1];
                sfu_q[ci] <= sfu_q[ci-1];
            end
        end
    end

    wire [FL-1:0] lane_ov;

    genvar L;
    generate
    for (L = 0; L < FL; L = L + 1) begin : g_lane
        localparam integer IS_SEED_UNIT = (L < FSFU_UNITS);

        // NOT `FL[PSW-1:0]`: a parameter sliced to the pass index's own width
        // is 8 truncated to two bits, which is ZERO, and every pass would then
        // read element L. The constants stay full width.
        wire [EIW-1:0] el = (IS_SEED_UNIT && is_seed)
                          ? (pass * SEED_U + L)
                          : (pass[PSW_A-1:0] * FL + L);
        wire [31:0] a = v1[32*el +: 32];
        wire [31:0] b = v2[32*el +: 32];
        wire [31:0] d = vd[32*el +: 32];

        wire [31:0] ly;
        wire        lp;
        rv_fpu u_alu (
            .clk(clk), .rst(rst),
            .in_valid(in_valid), .op(op),
            .a(a), .b(b), .c(wants_c ? b : d),
            .out_valid(lane_ov[L]), .y(ly), .out_pred(lp)
        );

        wire [31:0] ly_d;
        wire        lp_d;
        if (PAD > 0) begin : g_pad
            (* srl_style = "register" *) reg [33*PAD-1:0] dly;
            always @(posedge clk) begin
                dly <= {dly[33*(PAD-1)-1:0], lp, ly};
            end
            assign ly_d = dly[33*(PAD-1) +: 32];
            assign lp_d = dly[33*(PAD-1) + 32];
        end else begin : g_nopad
            assign ly_d = ly;
            assign lp_d = lp;
        end

        if (IS_SEED_UNIT) begin : g_sfu
            wire [31:0] sy;
            khs_fp32_sfu u_sfu (
                .clk(clk), .rst(rst),
                .in_valid(in_valid && is_seed), .fsel(op[1:0]), .a(a),
                .out_valid(), .y(sy)
            );
            assign out[32*L +: 32] = cmp_q[LAT] ? {32{lp_d}}
                                   : sfu_q[LAT] ? sy : ly_d;
        end else begin : g_fma
            assign out[32*L +: 32] = cmp_q[LAT] ? {32{lp_d}} : ly_d;
        end
    end
    if (ALAT != LAT) begin : g_bad_alat
        khs_fp32_alu_ALAT_must_match_the_arrays_own_depth u_bad ();
    end
    if ((FSFU_UNITS != 0) && (FSFU_UNITS > FL)) begin : g_bad_sf
        khs_fp32_alu_FSFU_UNITS_cannot_exceed_FLANES u_bad ();
    end
    // -1 IS RESOLVED BY THE CALLER. Left negative here `L < FSFU_UNITS` builds
    // no seed unit while `is_seed` still fires, so a seed returns the FMA's
    // answer for `a * b + vd` -- a plausible wrong number.
    if (FSFU_UNITS < 0) begin : g_bad_sfneg
        khs_fp32_alu_FSFU_UNITS_must_be_resolved_before_it_arrives u_bad ();
    end
    endgenerate

    // Every unit is the same depth, so one valid speaks for all of them, and it
    // travels the pad with the result it qualifies.
    generate
    if (PAD > 0) begin : g_ovd
        reg [PAD:1] ov_q;
        integer oi;
        always @(posedge clk) begin
            if (rst) begin
                ov_q <= {PAD{1'b0}};
            end
            else begin
                ov_q[1] <= lane_ov[0];
                for (oi = 2; oi <= PAD; oi = oi + 1) begin
                    ov_q[oi] <= ov_q[oi-1];
                end
            end
        end
        assign out_valid = ov_q[PAD];
    end else begin : g_ovz
        assign out_valid = lane_ov[0];
    end
    endgenerate
endmodule

`default_nettype wire
