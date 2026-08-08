// Memory dtype <-> E8M15, the vector core's load and store edges.
//
// docs/compute/vector-core.md s1.1 is the argument for E8: an 8-bit exponent
// covers FP32's range exactly, so nothing here range-checks on the way IN.
// FP32 -> E8M15 keeps the exponent field verbatim; FP16 -> E8M15 is EXACT.
//
// ONLY FP16 AND FP32 ARE MEMORY DTYPES -- vector-core.md s1, "software sees
// FP32 or FP16 in memory and nothing else". The other four codes in
// isa/vector.md s2 raise `bad` rather than converting: E8M15-raw and ACC24 are
// internal formats that would put 24-bit words in RAM, and ACC24 specifically
// needs a peer port rather than a load (s2's note); INT8/INT16 have no settled
// semantics. `bad` is a decode fault, not a silent zero.

`default_nettype none

module vec_cvt_f16_to_e8 (
    input  wire [15:0] f16,
    output wire [23:0] e8
);
    wire        s   = f16[15];
    wire [4:0]  e5  = f16[14:10];
    wire [9:0]  m10 = f16[9:0];

    wire is_zero = (e5 == 5'd0) && (m10 == 10'd0);
    wire is_sub  = (e5 == 5'd0) && (m10 != 10'd0);
    wire is_max  = (e5 == 5'h1F);

    // A subnormal normalises into an ordinary E8M15 value, so it is a
    // leading-one count and a shift -- vector-core.md s1.3. Nothing is lost.
    wire [5:0] p;
    wire       nz;
    mx_lead1 #(.W(10)) u_l1 (.x(m10), .pos(p), .nz(nz));

    // value = m10 * 2^-24 = 2^(p-24) * 1.f, so the biased exponent is p + 103
    // and shifting the leading one up to bit 15 leaves the fraction at [14:0].
    wire [7:0]  sub_e  = 8'd103 + {2'b0, p};
    wire [15:0] sub_sh = {6'b0, m10} << (6'd15 - p);

    assign e8 = is_zero ? {s, 23'd0}
              : is_max  ? {s, 8'hFF, m10, 5'd0}
              : is_sub  ? {s, sub_e, sub_sh[14:0]}
                        : {s, ({3'b0, e5} + 8'd112), m10, 5'd0};

endmodule


// E8M15 -> FP16, the only direction that is both lossy and range-limited.
//
// A FINITE OVERFLOW SATURATES to the largest finite FP16, matching
// mx_fpacc_to_fp16 -- vector-core.md s11.2 quotes that line as the place a
// GEMM's range dies, and two different answers for one overflow is worse than
// the loss itself. A genuine infinity passes through as an infinity.
module vec_cvt_e8_to_f16 (
    input  wire [23:0] e8,
    output wire [15:0] f16
);
    wire        s   = e8[23];
    wire [7:0]  e   = e8[22:15];
    wire [14:0] m   = e8[14:0];
    wire [15:0] sig = {1'b1, m};

    wire is_zero = (e == 8'd0);
    wire is_max  = (e == 8'hFF);
    wire is_nan  = is_max && (m != 15'd0);

    // ---- normal path: 16 significand bits rounded down to 11 ----
    wire [10:0] keep  = sig[15:5];
    wire        guard = m[4];
    wire        stick = |m[3:0];
    wire [11:0] rnd   = {1'b0, keep} + {11'b0, (guard & (stick | keep[0]))};
    wire        carry = rnd[11];

    wire signed [9:0] e5_raw = $signed({2'b0, e}) - 10'sd112;
    wire signed [9:0] e5_adj = e5_raw + (carry ? 10'sd1 : 10'sd0);
    wire [9:0]        n_man  = carry ? 10'd0 : rnd[9:0];

    wire over  = (e5_adj >= 10'sd31);
    wire under = (e5_adj <= 10'sd0);

    // ---- subnormal path: m10 = sig * 2^(e-118), rounded ----
    // A rounding carry lands on bit 10, which IS the exponent field's LSB, so
    // the concatenation becomes the smallest normal with no fixup.
    wire [7:0]  sh_raw = 8'd118 - e;
    wire [5:0]  sh     = (sh_raw > 8'd31) ? 6'd31 : sh_raw[5:0];
    wire [47:0] wide   = {sig, 32'b0} >> sh;
    wire [10:0] sub_k  = wide[42:32];
    wire        sub_g  = wide[31];
    wire        sub_s  = |wide[30:0];
    wire [11:0] sub_r  = {1'b0, sub_k} + {11'b0, (sub_g & (sub_s | sub_k[0]))};

    assign f16 = is_zero ? {s, 15'd0}
               : is_nan  ? {s, 5'h1F, 1'b1, m[13:5]}
               : is_max  ? {s, 5'h1F, 10'd0}
               : over    ? {s, 5'h1E, 10'h3FF}
               : under   ? {s, 4'd0, sub_r[10:0]}
                         : {s, e5_adj[4:0], n_man};

endmodule


module vec_cvt_f32_to_e8 (
    input  wire [31:0] f32,
    output wire [23:0] e8
);
    wire        s = f32[31];
    wire [7:0]  e = f32[30:23];
    wire [22:0] m = f32[22:0];

    // E8M15's smallest normal is 2^-126, so an FP32 subnormal has nowhere to go
    // and is flushed -- vector-core.md s1.3.
    wire is_zero = (e == 8'd0);
    wire is_max  = (e == 8'hFF);
    wire is_nan  = is_max && (m != 23'd0);

    wire [14:0] keep  = m[22:8];
    wire        guard = m[7];
    wire        stick = |m[6:0];
    wire [15:0] rnd   = {1'b0, keep} + {15'b0, (guard & (stick | keep[0]))};
    wire        carry = rnd[15];

    wire [8:0]  e_adj = {1'b0, e} + {8'b0, carry};
    wire [14:0] n_man = carry ? 15'd0 : rnd[14:0];

    // Rounding up from the largest finite FP32 is the one overflow: E8 has no
    // wider exponent to carry into, so it becomes an infinity.
    wire ovf = (e_adj >= 9'd255);

    assign e8 = is_zero ? {s, 23'd0}
              : is_nan  ? {s, 8'hFF, 1'b1, m[21:8]}
              : (is_max || ovf) ? {s, 8'hFF, 15'd0}
                        : {s, e_adj[7:0], n_man};

endmodule


module vec_cvt_e8_to_f32 (
    input  wire [23:0] e8,
    output wire [31:0] f32
);
    // Exact: E8 is FP32's exponent field verbatim and the significand only
    // gains bits. E == 0 is ZERO whatever M holds -- E8M15 has no subnormals --
    // so the mantissa is dropped rather than copied into an FP32 subnormal,
    // which would turn a zero into a small nonzero value.
    assign f32 = (e8[22:15] == 8'd0) ? {e8[23], 31'd0}
                                     : {e8[23], e8[22:15], e8[14:0], 8'd0};

endmodule


// The load edge. `raw` is one element, right-justified out of an L1 word.
// Dtype codes follow isa/vector.md s2; they are redeclared rather than
// included, as mx_cluster_cu.v does with the packet types.
module vec_cvt_in (
    input  wire [2:0]  dtype,
    input  wire [31:0] raw,
    output reg  [23:0] e8,
    output wire        bad
);
    localparam [2:0] DT_FP16 = 3'd1, DT_FP32 = 3'd2;

    wire [23:0] from16, from32;
    vec_cvt_f16_to_e8 u_16 (.f16(raw[15:0]), .e8(from16));
    vec_cvt_f32_to_e8 u_32 (.f32(raw),       .e8(from32));

    assign bad = !((dtype == DT_FP16) || (dtype == DT_FP32));

    always @(*) begin
        case (dtype)
            DT_FP16: e8 = from16;
            DT_FP32: e8 = from32;
            default: e8 = 24'd0;
        endcase
    end

endmodule


// The store edge. `raw` is right-justified; the caller places it in the word.
module vec_cvt_out (
    input  wire [2:0]  dtype,
    input  wire [23:0] e8,
    output reg  [31:0] raw,
    output wire        bad
);
    localparam [2:0] DT_FP16 = 3'd1, DT_FP32 = 3'd2;

    wire [15:0] to16;
    wire [31:0] to32;
    vec_cvt_e8_to_f16 u_16 (.e8(e8), .f16(to16));
    vec_cvt_e8_to_f32 u_32 (.e8(e8), .f32(to32));

    assign bad = !((dtype == DT_FP16) || (dtype == DT_FP32));

    always @(*) begin
        case (dtype)
            DT_FP16: raw = {16'd0, to16};
            DT_FP32: raw = to32;
            default: raw = 32'd0;
        endcase
    end

endmodule

`default_nettype wire
