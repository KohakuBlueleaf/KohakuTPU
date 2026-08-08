// The load and store edges against real arithmetic.
//
// FP16 is checked EXHAUSTIVELY -- all 65,536 patterns -- because it is cheap
// and because two of the claims are exactness claims, which sampling cannot
// establish:
//
//   FP16 -> E8M15         exact for every input, subnormals included
//   FP16 -> E8M15 -> FP16 the identity, bit for bit
//   E8M15 -> FP32         exact
//
// The lossy directions get a half-ulp bound instead, and the saturating one
// gets its boundary walked: 65520 is exactly half an ulp above the largest
// finite FP16, so everything below it must ROUND to 65504 and everything at or
// above it must SATURATE to the same value for a different reason.

`default_nettype none
`timescale 1ns/1ps

module vec_cvt_tb;

    integer errors = 0;
    integer checks = 0;

    reg  [15:0] f16_i;
    wire [23:0] f16_o;
    vec_cvt_f16_to_e8 u_a (.f16(f16_i), .e8(f16_o));

    reg  [23:0] e8_i;
    wire [15:0] e8_f16;
    wire [31:0] e8_f32;
    vec_cvt_e8_to_f16 u_b (.e8(e8_i), .f16(e8_f16));
    vec_cvt_e8_to_f32 u_c (.e8(e8_i), .f32(e8_f32));

    reg  [31:0] f32_i;
    wire [23:0] f32_o;
    vec_cvt_f32_to_e8 u_d (.f32(f32_i), .e8(f32_o));

    reg  [2:0]  dt;
    reg  [31:0] raw_i;
    wire [23:0] in_e8;
    wire        in_bad;
    wire [31:0] out_raw;
    wire        out_bad;
    vec_cvt_in  u_e (.dtype(dt), .raw(raw_i),  .e8(in_e8),   .bad(in_bad));
    vec_cvt_out u_f (.dtype(dt), .e8(e8_i),    .raw(out_raw), .bad(out_bad));

    // ---- reference values, computed rather than trusted to a system task ----
    function real f16_val(input [15:0] b);
        reg [4:0] e; reg [9:0] m;
        begin
            e = b[14:10]; m = b[9:0];
            if (e == 0)          f16_val = (m * 1.0) * (2.0 ** -24);
            else if (e == 31)    f16_val = 0.0;
            else f16_val = (1.0 + (m * 1.0) / 1024.0) * (2.0 ** ($signed({1'b0, e}) - 15));
            if (b[15] && e != 31) f16_val = -f16_val;
        end
    endfunction

    function real e8_val(input [23:0] b);
        reg [7:0] e; reg [14:0] m;
        begin
            e = b[22:15]; m = b[14:0];
            if (e == 0)        e8_val = 0.0;
            else if (e == 255) e8_val = 0.0;
            else e8_val = (1.0 + (m * 1.0) / 32768.0) * (2.0 ** ($signed({1'b0, e}) - 127));
            if (b[23] && e != 255) e8_val = -e8_val;
        end
    endfunction

    function real f32_val(input [31:0] b);
        reg [7:0] e; reg [22:0] m;
        begin
            e = b[30:23]; m = b[22:0];
            if (e == 0)        f32_val = (m * 1.0) * (2.0 ** -149);
            else if (e == 255) f32_val = 0.0;
            else f32_val = (1.0 + (m * 1.0) / 8388608.0) * (2.0 ** ($signed({1'b0, e}) - 127));
            if (b[31] && e != 255) f32_val = -f32_val;
        end
    endfunction

    function real f16_ulp(input [15:0] b);
        reg [4:0] e;
        begin
            e = b[14:10];
            if (e == 0) f16_ulp = 2.0 ** -24;
            else        f16_ulp = 2.0 ** ($signed({1'b0, e}) - 25);
        end
    endfunction

    function real e8_ulp(input [23:0] b);
        reg [7:0] e;
        begin
            e = b[22:15];
            if (e == 0) e8_ulp = 2.0 ** -142;
            else        e8_ulp = 2.0 ** ($signed({1'b0, e}) - 142);
        end
    endfunction

    function real fabs(input real x);
        begin fabs = (x < 0.0) ? -x : x; end
    endfunction

    task chk(input cond, input [255:0] what, input [31:0] ctx);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 15) $display("  FAIL %0s  ctx=%h", what, ctx);
            end
        end
    endtask

    integer i, e, seed;
    reg [15:0] rt;
    reg [23:0] ev;
    real want, got, lim;

    initial begin
        seed = 32'h1234_abcd;

        // ============ 1. FP16 -> E8M15, EXHAUSTIVE, exact ============
        $display("--- 1. FP16 -> E8M15, all 65536 patterns ---");
        for (i = 0; i < 65536; i = i + 1) begin
            f16_i = i[15:0];
            #1;
            if (f16_i[14:10] == 5'h1F) begin
                // inf stays inf, NaN stays NaN -- the payload must not vanish
                chk(f16_o[22:15] == 8'hFF, "f16 special exponent", {16'd0, f16_i});
                chk((|f16_i[9:0]) == (|f16_o[14:0]), "f16 nan-ness preserved",
                    {16'd0, f16_i});
            end else begin
                chk(e8_val(f16_o) == f16_val(f16_i), "f16->e8 not exact",
                    {16'd0, f16_i});
                chk(f16_o[23] == f16_i[15], "f16->e8 sign", {16'd0, f16_i});
            end
        end

        // ============ 2. FP16 -> E8M15 -> FP16, EXHAUSTIVE, identity ============
        $display("--- 2. FP16 round trip, all 65536 patterns ---");
        for (i = 0; i < 65536; i = i + 1) begin
            f16_i = i[15:0];
            #1;
            e8_i = f16_o;
            #1;
            rt = e8_f16;
            if (f16_i[14:10] == 5'h1F)
                chk(rt[14:10] == 5'h1F && ((|rt[9:0]) == (|f16_i[9:0])),
                    "round trip special", {16'd0, f16_i});
            else
                chk(rt === f16_i, "round trip not identity", {16'd0, f16_i});
        end

        // ============ 3. E8M15 -> FP16, half ulp and the saturation edge ======
        $display("--- 3. E8M15 -> FP16 ---");
        for (i = 0; i < 40000; i = i + 1) begin
            e8_i = {$random(seed)} & 24'h7FFFFF;
            e8_i[23] = i[0];
            if (e8_i[22:15] == 8'hFF) e8_i[22:15] = 8'hFE;
            #1;
            want = e8_val(e8_i);
            if (fabs(want) >= 65520.0) begin
                chk(e8_f16[14:0] === 15'h7BFF, "no saturation to 0x7BFF",
                    e8_i[23:0]);
                chk(e8_f16[15] === e8_i[23], "saturation sign", e8_i[23:0]);
            end else begin
                got = f16_val(e8_f16);
                lim = f16_ulp(e8_f16) * 0.5000001;
                chk(fabs(want - got) <= lim, "e8->f16 over half ulp", e8_i[23:0]);
            end
        end

        // walk the saturation boundary itself
        e8_i = 24'h47FFE0;                       // 65504 exactly
        #1 chk(e8_f16 === 16'h7BFF, "65504 must encode, not saturate", 0);
        e8_i = 24'h47FFF0;                       // 65520, the tie
        #1 chk(e8_f16 === 16'h7BFF, "65520 saturates", 0);
        e8_i = 24'h7F8000;                       // +inf
        #1 chk(e8_f16 === 16'h7C00, "inf must stay inf, not saturate", 0);
        e8_i = 24'hFF8000;                       // -inf
        #1 chk(e8_f16 === 16'hFC00, "-inf must stay -inf", 0);
        e8_i = 24'h7FC000;                       // qNaN
        #1 chk(e8_f16[14:10] === 5'h1F && (|e8_f16[9:0]), "NaN must stay NaN", 0);
        e8_i = 24'h800000;                       // -0
        #1 chk(e8_f16 === 16'h8000, "-0 must stay -0", 0);

        // ============ 4. E8M15 -> FP32, exact, and back ============
        $display("--- 4. E8M15 <-> FP32 ---");
        // The identity holds for CANONICAL values. E == 0 is zero whatever M
        // says, so a non-canonical zero is not a distinct value to return to.
        for (i = 0; i < 20000; i = i + 1) begin
            e8_i = {$random(seed)} & 24'hFFFFFF;
            if (e8_i[22:15] == 8'hFF) e8_i[22:15] = 8'hFE;
            if (e8_i[22:15] == 8'h00) e8_i[14:0]  = 15'd0;
            #1;
            chk(f32_val(e8_f32) == e8_val(e8_i), "e8->f32 not exact", e8_i[23:0]);
            f32_i = e8_f32;
            #1;
            chk(f32_o === e8_i, "f32 round trip not identity", e8_i[23:0]);
        end

        // A non-canonical zero must become a ZERO, not an FP32 subnormal: the
        // mantissa carries no value once the exponent is zero.
        e8_i = 24'h0054CA;
        #1 chk(e8_f32 === 32'h0000_0000, "non-canonical +0 leaked a subnormal", 0);
        #1 chk(e8_f16 === 16'h0000, "non-canonical +0 leaked into fp16", 0);
        e8_i = 24'h8076AD;
        #1 chk(e8_f32 === 32'h8000_0000, "non-canonical -0 leaked a subnormal", 0);
        #1 chk(e8_f16 === 16'h8000, "non-canonical -0 leaked into fp16", 0);

        // ============ 5. FP32 -> E8M15, half ulp, subnormal flush ============
        $display("--- 5. FP32 -> E8M15 ---");
        for (i = 0; i < 40000; i = i + 1) begin
            f32_i = {$random(seed)};
            if (f32_i[30:23] == 8'hFF) f32_i[30:23] = 8'hFE;
            if (f32_i[30:23] == 8'h00) f32_i[30:23] = 8'h01;
            #1;
            want = f32_val(f32_i);
            got  = e8_val(f32_o);
            lim  = e8_ulp(f32_o) * 0.5000001;
            chk(fabs(want - got) <= lim, "f32->e8 over half ulp", f32_i);
        end

        // an FP32 subnormal is below 2^-126 and has to flush, keeping its sign
        f32_i = 32'h0000_0001;
        #1 chk(f32_o === 24'h000000, "+subnormal must flush to +0", 0);
        f32_i = 32'h8000_0001;
        #1 chk(f32_o === 24'h800000, "-subnormal must flush to -0", 0);
        f32_i = 32'h7F7F_FFFF;                   // largest finite FP32
        #1 chk(f32_o === 24'h7F8000, "largest finite rounds up to inf", 0);
        f32_i = 32'h7F80_0000;
        #1 chk(f32_o === 24'h7F8000, "+inf", 0);
        f32_i = 32'h7FC0_0000;
        #1 chk(f32_o[22:15] === 8'hFF && (|f32_o[14:0]), "NaN stays NaN", 0);

        // ============ 6. the dtype mux, and what it REFUSES ============
        $display("--- 6. dtype gating ---");
        for (e = 0; e < 8; e = e + 1) begin
            dt    = e[2:0];
            raw_i = 32'h3F80_0000;               // 1.0f
            e8_i  = 24'h3F8000;                  // 1.0 in E8M15
            #1;
            if (e == 1 || e == 2) begin
                chk(!in_bad,  "supported dtype flagged bad",  {29'd0, dt});
                chk(!out_bad, "supported dtype flagged bad",  {29'd0, dt});
            end else begin
                chk(in_bad,  "unsupported load dtype not refused",  {29'd0, dt});
                chk(out_bad, "unsupported store dtype not refused", {29'd0, dt});
            end
        end

        dt = 3'd2; raw_i = 32'h4040_0000; e8_i = 24'h404000;
        #1 chk(in_e8 === 24'h404000, "fp32 3.0 through the mux", 0);
        dt = 3'd1; raw_i = 32'h0000_4200;
        #1 chk(in_e8 === 24'h404000, "fp16 3.0 through the mux", 0);
        dt = 3'd1; e8_i = 24'h404000;
        #1 chk(out_raw[15:0] === 16'h4200, "store 3.0 as fp16", 0);
        dt = 3'd2; e8_i = 24'h404000;
        #1 chk(out_raw === 32'h4040_0000, "store 3.0 as fp32", 0);

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #20000000;
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire
