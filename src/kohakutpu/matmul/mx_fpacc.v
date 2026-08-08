// Accumulator float, parameterised: S1 E7 M<MW>.
//
//     MW = 16  ->  24 bits (FP24)
//     MW = 14  ->  22 bits
//     MW = 12  ->  20 bits
//     MW = 11  ->  19 bits   (TF32-ish, but E7 rather than E8)
//     MW = 10  ->  18 bits
//
// E7 is fixed and is NOT the thing to trim: the accumulator holds
// int * 2^(sa+sb), and for FP16 sources that exponent sum spans roughly
// -48..+30, so an E5 field overflows on ordinary data. Mantissa is the tunable
// -- see docs/compute/accumulator.md.
//
// LAYOUT OF THIS FILE
//
//   mx_fpacc_norm       int + scale -> float, unsplit REFERENCE
//   mx_lead1            leading-one position, log depth
//   mx_fpacc_norm_a/_b  magnitude -> float, split for pipelining
//   mx_fpacc_align      exponent compare and alignment shift   (ACU stage 3)
//   mx_fpacc_round_a/_b add, normalise, round                  (ACU stages 4-5)
//   mx_fpacc_add        float add, unsplit REFERENCE
//   mx_fpacc_to_fp16    accumulator float -> FP16, on EMIT only
//
// The two REFERENCE modules are not instantiated by the ACU; mx_fpacc_tb checks
// THEM against a real-number model. Nothing cross-checks them against the split
// versions, which are covered end to end by mx_acu_fp_tb. Changing a split
// module and running only mx_fpacc_tb proves nothing.
//
// EVERYTHING ELSE MUST STAY TREE-SHAPED. These blocks sit in the CU's critical
// path, and a search or reduction written as a loop that carries a value
// between iterations -- `if (!found && x[i]) found = 1`, `lost = lost | x[i]`
// -- synthesises as exactly that serial chain: 25 LUT levels in one stage, most
// of a 3.33 ns period, and it cost this design ~68 MHz. Use smear-isolate-encode
// for searches and mask-then-reduce for sticky bits; see mx_lead1.

`default_nettype none

// ---------------------------------------------------------------------------
// Integer + power-of-two scale -> accumulator float.
//
// REFERENCE: serial search loops, not instantiated by the ACU. mx_fpacc_norm_a
// and _b are what ship.
// ---------------------------------------------------------------------------
module mx_fpacc_norm #(
    parameter integer MW   = 16,
    parameter integer VW   = 22,
    parameter integer BIAS = 63
)(
    input  wire signed [VW-1:0] val,
    input  wire signed [9:0]    exp_in,
    output reg  [MW+7:0]        fp
);
    localparam integer SW = MW + 1;          // 1.f

    reg [VW-1:0]      mag;
    reg [VW+SW-1:0]   shifted;
    reg [5:0]         msb;
    reg               found;
    reg signed [10:0] e;
    reg [SW-1:0]      sig;
    reg               guard, sticky, round_up;
    reg [SW:0]        sig_r;
    integer b;

    always @(*) begin
        mag = val[VW-1] ? (~val + 1'b1) : val;

        msb = 6'd0; found = 1'b0;
        for (b = VW-1; b >= 0; b = b - 1)
            if (!found && mag[b]) begin
                msb = b[5:0]; found = 1'b1;
            end

        if (!found) begin
            fp = {(MW+8){1'b0}};
        end else begin
            // Both shift directions are needed. A single `<< (MW - msb)` wraps
            // the count to a huge unsigned value whenever msb > MW, discarding
            // the mantissa and leaving a clean power of two -- plausible and
            // wrong by up to 2x.
            if (msb <= MW[5:0]) shifted = {{SW{1'b0}}, mag} << (MW[5:0] - msb);
            else                shifted = {{SW{1'b0}}, mag} >> (msb - MW[5:0]);
            sig = shifted[SW-1:0];

            guard  = (msb > MW[5:0]) ? mag[msb - MW[5:0] - 6'd1] : 1'b0;
            sticky = 1'b0;
            for (b = 0; b < VW; b = b + 1)
                if (msb > (MW[5:0] + 6'd1) && b < (msb - MW[5:0] - 6'd1))
                    sticky = sticky | mag[b];

            round_up = guard & (sticky | sig[0]);
            sig_r    = {1'b0, sig} + {{SW{1'b0}}, round_up};

            e = $signed({1'b0, msb}) + exp_in + BIAS[10:0];

            // On a rounding carry the leading one moves to bit MW, so the
            // fraction is sig_r[MW:1] -- MW bits. sig_r[SW:1] is MW+1 bits and
            // overflows the concatenation, pushing the sign out. MW=16 never
            // reaches this path, so it only appears once MW is narrowed.
            if (sig_r[SW]) fp = {val[VW-1], (e + 11'sd1) & 11'h7F, sig_r[MW:1]};
            else           fp = {val[VW-1], e[6:0], sig_r[MW-1:0]};
        end
    end
endmodule


// ---------------------------------------------------------------------------
// Leading-one position, logarithmic depth.
//
// Smear-isolate-encode, NOT a search loop: log2(W) levels of OR to smear the
// leading one rightwards, one AND to isolate it, six OR reductions to encode
// it. A `found`-flag loop would be a W-deep LUT chain instead, and at W=25 that
// chain alone is the whole clock period.
// ---------------------------------------------------------------------------
module mx_lead1 #(
    parameter integer W = 32
)(
    input  wire [W-1:0] x,
    output reg  [5:0]   pos,        // index of the most significant set bit
    output wire         nz
);
    reg [W-1:0] y;
    reg [31:0]  bb;
    integer     s, b, k;

    always @(*) begin
        y = x;
        for (s = 1; s < W; s = s * 2) y = y | (y >> s);
    end

    wire [W-1:0] oh = y & ~(y >> 1);    // exactly one bit: the leading one
    assign nz = y[0];                   // smeared, so bit 0 is the OR of all

    always @(*) begin
        for (k = 0; k < 6; k = k + 1) begin
            pos[k] = 1'b0;
            for (b = 0; b < W; b = b + 1) begin
                bb = b;
                if (bb[k]) pos[k] = pos[k] | oh[b];
            end
        end
    end
endmodule


// ---------------------------------------------------------------------------
// The normaliser, split so the ACU can register between the halves. The seam is
// after the shift: search and shift on one side, round and assemble on the other.
//
// IT TAKES A MAGNITUDE, NOT A SIGNED VALUE. The sign travels beside the data and
// belongs to the caller; taking `|val|` here puts a two's-complement carry chain
// at the head of the path -- see docs/compute/accumulator.md s4. The ACU forms
// the magnitude inside the DSP that was already multiplying by the block scale.
// ---------------------------------------------------------------------------
module mx_fpacc_norm_a #(
    parameter integer MW = 16,
    parameter integer VW = 22
)(
    input  wire [VW-1:0] mag,             // unsigned magnitude
    output wire [MW:0]   sig,             // 1.f, unrounded
    output wire [5:0]    msb,
    output wire          guard,
    output wire          sticky,
    output wire          is_zero
);
    localparam integer SW = MW + 1;

    wire nz;

    mx_lead1 #(.W(VW)) u_l1 (.x(mag), .pos(msb), .nz(nz));

    assign is_zero = !nz;

    wire       big = (msb > MW[5:0]);
    wire [5:0] rsh = msb - MW[5:0];         // value wider than the mantissa
    wire [5:0] lsh = MW[5:0] - msb;

    wire [VW+SW-1:0] wide    = {{SW{1'b0}}, mag};
    wire [VW+SW-1:0] shifted = big ? (wide >> rsh) : (wide << lsh);
    assign sig = shifted[SW-1:0];

    // Sticky runs alongside the shift, not after it: mask the bits that fall off
    // the bottom and reduce once. The loop form is a VW-deep serial chain with a
    // comparator on every link.
    wire [5:0]    ssh   = (big && rsh >= 6'd1) ? (rsh - 6'd1) : 6'd0;
    wire [VW-1:0] smask = ~({VW{1'b1}} << ssh);

    assign guard  = big ? mag[ssh] : 1'b0;
    assign sticky = |(mag & smask);
endmodule


module mx_fpacc_norm_b #(
    parameter integer MW   = 16,
    parameter integer BIAS = 63
)(
    input  wire [MW:0]       sig,
    input  wire [5:0]        msb,
    input  wire              guard,
    input  wire              sticky,
    input  wire              is_zero,
    input  wire              sign,
    input  wire signed [9:0] exp_in,
    output reg  [MW+7:0]     fp
);
    localparam integer SW = MW + 1;

    reg signed [10:0] e;
    reg [SW:0]        sig_r;
    reg               round_up;

    always @(*) begin
        if (is_zero) begin
            fp = {(MW+8){1'b0}};
        end else begin
            round_up = guard & (sticky | sig[0]);
            sig_r    = {1'b0, sig} + {{SW{1'b0}}, round_up};
            e        = $signed({1'b0, msb}) + exp_in + BIAS[10:0];

            // On a rounding carry the leading one moves to bit MW, so the
            // fraction is sig_r[MW:1] -- MW bits, not MW+1.
            if (sig_r[SW]) fp = {sign, (e + 11'sd1) & 11'h7F, sig_r[MW:1]};
            else           fp = {sign, e[6:0], sig_r[MW-1:0]};
        end
    end
endmodule


// ---------------------------------------------------------------------------
// Accumulator float add, round to nearest even.
//
// Significands are carried as 1.f plus GUARD spare bits so alignment, the add
// and the rounding all happen at ONE width; mixing widths here loses the
// significand entirely on same-sign operands.
// ---------------------------------------------------------------------------
// Split into ALIGN and ROUND so the ACU can register between them. The seam is
// after the add: alignment before, normalisation after.
module mx_fpacc_align #(
    parameter integer MW = 16
)(
    input  wire [MW+7:0]   a,
    input  wire [MW+7:0]   b,
    // Zero-ness as CONTROL rather than data: LOAD and EMIT/SEND know one operand
    // is zero a stage early, and forcing 384 bits of tile data to zero to say so
    // would put a mux on the widest, latest signal in the block.
    input  wire            a_zero,
    input  wire            b_zero,
    output reg  [MW+9:0]   bg_o,     // SW+1: the larger operand, 1.f + guard
    output reg  [MW+9:0]   sh_o,     // SW+1: the smaller, aligned
    output reg             sub_o,    // signs differ -> subtract
    output reg  [6:0]      e_big_o,
    output reg             s_big_o,
    output reg             lost_o,
    output reg             zero_o,   // both operands zero, or exact cancellation
    output reg             pass_o,   // one operand zero: pass the other through
    output reg  [MW+7:0]   pass_val
);
    localparam integer GUARD = 8;
    localparam integer SW    = MW + 1 + GUARD;
    localparam integer W     = MW + 8;

    wire        sa = a[W-1], sb = b[W-1];
    wire [6:0]  ea = a[W-2 -: 7], eb = b[W-2 -: 7];
    wire [MW-1:0] ma = a[MW-1:0], mb = b[MW-1:0];
    wire        za = a_zero || (ea == 7'd0);
    wire        zb = b_zero || (eb == 7'd0);

    // One unsigned compare on the concatenation: {e,m} >= {e,m} is exactly
    // (ea > eb) || (ea == eb && ma >= mb), but maps to a single carry chain
    // instead of a 7-bit compare feeding a 16-bit compare feeding an AND/OR.
    wire        a_ge    = {ea, ma} >= {eb, mb};
    wire [6:0]  e_big   = a_ge ? ea : eb;
    wire        s_big   = a_ge ? sa : sb;
    wire        s_small = a_ge ? sb : sa;
    wire [7:0]  diff    = a_ge ? (ea - eb) : (eb - ea);

    wire [SW-1:0] bg  = a_ge ? {1'b1, ma, {GUARD{1'b0}}} : {1'b1, mb, {GUARD{1'b0}}};
    wire [SW-1:0] sml = a_ge ? {1'b1, mb, {GUARD{1'b0}}} : {1'b1, ma, {GUARD{1'b0}}};

    // Clamping the shift to SW makes the "shifted out entirely" case fall out
    // of the same expressions: `sml >> SW` is zero and `{SW{1'b1}} << SW` is
    // zero, so the mask becomes all-ones and sticky becomes |sml.
    wire [7:0]    sw8 = SW;
    wire [7:0]    dcl = (diff >= SW) ? sw8 : diff;
    wire [SW-1:0] shifted = sml >> dcl;

    // The sticky mask is a shift running in PARALLEL with the alignment shift,
    // reduced in one OR tree: a couple of LUT levels against SW for a loop that
    // chains an OR through a comparator per bit.
    wire [SW-1:0] lmask = ~({SW{1'b1}} << dcl);
    wire          lost  = |(sml & lmask);

    always @(*) begin
        e_big_o  = e_big;
        s_big_o  = s_big;
        pass_val = za ? b : a;
        zero_o   = 1'b0;
        pass_o   = 1'b0;
        bg_o     = {(SW+1){1'b0}};
        sh_o     = {(SW+1){1'b0}};
        sub_o    = (s_big != s_small);

        if (za && zb)      zero_o = 1'b1;
        else if (za || zb) pass_o = 1'b1;
        else begin
            // The add itself belongs to the round stage: left here it puts a
            // 26-bit carry chain after the tile mux, the exponent compare and
            // the barrel shift, all in one cycle.
            bg_o = {1'b0, bg};
            sh_o = {1'b0, shifted};
        end
        lost_o = (za || zb) ? 1'b0 : lost;
    end
endmodule


// ---------------------------------------------------------------------------
// The round half, split again. The seam goes after the shift, matching the
// normaliser split.
//
// This split sits INSIDE the accumulate loop, so it pushes the read-after-write
// distance from 2 cycles to 3. Free only because K is swept outermost and there
// is no tight recurrence left.
// ---------------------------------------------------------------------------
module mx_fpacc_round_a #(
    parameter integer MW = 16
)(
    input  wire [MW+9:0] bg_i,
    input  wire [MW+9:0] sh_i,
    input  wire          sub_i,
    output wire [MW+9:0] norm_o,
    output wire [5:0]    lz_o,
    output wire          sum_zero_o
);
    localparam integer SW = MW + 9;

    wire [SW:0] sum = sub_i ? (bg_i - sh_i) : (bg_i + sh_i);
    wire [5:0]  p;
    wire        nz;

    mx_lead1 #(.W(SW+1)) u_l1 (.x(sum), .pos(p), .nz(nz));

    assign sum_zero_o = !nz;
    assign lz_o       = SW[5:0] - p;
    assign norm_o     = sum << lz_o;
endmodule


module mx_fpacc_round_b #(
    parameter integer MW = 16
)(
    input  wire [MW+9:0] norm_i,
    input  wire [5:0]    lz_i,
    input  wire          sum_zero_i,
    input  wire [6:0]    e_big,
    input  wire          s_big,
    input  wire          lost,
    input  wire          zero_i,
    input  wire          pass_i,
    input  wire [MW+7:0] pass_val,
    output reg  [MW+7:0] s
);
    localparam integer GUARD = 8;
    localparam integer SW    = MW + 1 + GUARD;
    localparam integer W     = MW + 8;

    reg signed [9:0] e_out;
    reg  [MW:0]      sig;
    reg              g, st, up;
    integer          i;

    always @(*) begin
        if (zero_i)          s = {W{1'b0}};
        else if (pass_i)     s = pass_val;
        else if (sum_zero_i) s = {W{1'b0}};
        else begin
            e_out = $signed({3'b0, e_big}) + 10'sd1 - $signed({4'b0, lz_i});

            sig = norm_i[SW -: (MW+1)];
            g   = norm_i[SW-MW-1];
            st  = lost;
            for (i = 0; i < SW-MW-1; i = i + 1) st = st | norm_i[i];
            up  = g & (st | sig[0]);

            if (up && (sig == {(MW+1){1'b1}})) begin
                sig   = {1'b1, {MW{1'b0}}};
                e_out = e_out + 10'sd1;
            end else begin
                sig = sig + {{MW{1'b0}}, up};
            end

            if (e_out <= 0)        s = {W{1'b0}};
            else if (e_out >= 127) s = {s_big, 7'h7F, {MW{1'b1}}};
            else                   s = {s_big, e_out[6:0], sig[MW-1:0]};
        end
    end
endmodule


// REFERENCE. Unsplit float add for the primitives bench; not instantiated by
// the ACU.
module mx_fpacc_add #(
    parameter integer MW = 16
)(
    input  wire [MW+7:0] a,
    input  wire [MW+7:0] b,
    output reg  [MW+7:0] s
);
    localparam integer GUARD = 8;
    localparam integer SW    = MW + 1 + GUARD;
    localparam integer W     = MW + 8;

    wire        sa = a[W-1], sb = b[W-1];
    wire [6:0]  ea = a[W-2 -: 7], eb = b[W-2 -: 7];
    wire [MW-1:0] ma = a[MW-1:0], mb = b[MW-1:0];
    wire        za = (ea == 7'd0), zb = (eb == 7'd0);

    wire        a_ge    = (ea > eb) || (ea == eb && ma >= mb);
    wire [6:0]  e_big   = a_ge ? ea : eb;
    wire        s_big   = a_ge ? sa : sb;
    wire        s_small = a_ge ? sb : sa;
    wire [7:0]  diff    = a_ge ? (ea - eb) : (eb - ea);

    wire [SW-1:0] bg  = a_ge ? {1'b1, ma, {GUARD{1'b0}}} : {1'b1, mb, {GUARD{1'b0}}};
    wire [SW-1:0] sml = a_ge ? {1'b1, mb, {GUARD{1'b0}}} : {1'b1, ma, {GUARD{1'b0}}};

    reg  [SW-1:0]    shifted;
    reg              lost;
    reg  [SW:0]      sum;
    reg  [5:0]       lz;
    reg              found;
    reg  [SW:0]      norm;
    reg signed [9:0] e_out;
    reg  [MW:0]      sig;
    reg              g, st, up;
    integer          i;

    always @(*) begin
        if (za && zb)      s = {W{1'b0}};
        else if (za)       s = b;
        else if (zb)       s = a;
        else begin
            if (diff >= SW) begin
                shifted = {SW{1'b0}};
                lost    = |sml;
            end else begin
                shifted = sml >> diff;
                lost    = 1'b0;
                for (i = 0; i < SW; i = i + 1)
                    if (i < diff) lost = lost | sml[i];
            end

            sum = (s_big == s_small) ? ({1'b0, bg} + {1'b0, shifted})
                                     : ({1'b0, bg} - {1'b0, shifted});

            if (sum == {(SW+1){1'b0}}) begin
                s = {W{1'b0}};
            end else begin
                lz = 6'd0; found = 1'b0;
                for (i = SW; i >= 0; i = i - 1)
                    if (!found && sum[i]) begin
                        lz = SW[5:0] - i[5:0]; found = 1'b1;
                    end
                norm  = sum << lz;
                e_out = $signed({3'b0, e_big}) + 10'sd1 - $signed({4'b0, lz});

                sig = norm[SW -: (MW+1)];
                g   = norm[SW-MW-1];
                st  = lost;
                for (i = 0; i < SW-MW-1; i = i + 1) st = st | norm[i];
                up  = g & (st | sig[0]);

                if (up && (sig == {(MW+1){1'b1}})) begin
                    sig   = {1'b1, {MW{1'b0}}};
                    e_out = e_out + 10'sd1;
                end else begin
                    sig = sig + {{MW{1'b0}}, up};
                end

                if (e_out <= 0)        s = {W{1'b0}};
                else if (e_out >= 127) s = {s_big, 7'h7F, {MW{1'b1}}};
                else                   s = {s_big, e_out[6:0], sig[MW-1:0]};
            end
        end
    end
endmodule


// ---------------------------------------------------------------------------
// Accumulator float -> FP16 S1E5M10, round to nearest even, saturating.
// Only a final result is converted; partials keep the accumulator width.
// ---------------------------------------------------------------------------
module mx_fpacc_to_fp16 #(
    parameter integer MW = 16
)(
    input  wire [MW+7:0] fpa,
    output reg  [15:0]   fp16
);
    localparam integer W = MW + 8;

    wire          s = fpa[W-1];
    wire [6:0]    e = fpa[W-2 -: 7];
    wire [MW-1:0] m = fpa[MW-1:0];

    reg signed [8:0] e16;
    reg  [10:0]      m11;

    // Must be a generate, not a runtime `if`: both arms elaborate, and
    // {(10-MW){1'b0}} is an illegal replication when MW > 10 even though that
    // arm can never run.
    wire [10:0] m11_w;
    generate
    if (MW > 10) begin : g_round
        wire        gd = m[MW-11];
        wire        st = |m[(MW-11 > 0 ? MW-12 : 0):0] & (MW > 11);
        wire        up = gd & (st | m[MW-10]);
        assign m11_w = {1'b0, m[MW-1 -: 10]} + {10'd0, up};
    end else if (MW == 10) begin : g_exact
        assign m11_w = {1'b0, m};
    end else begin : g_pad
        assign m11_w = {1'b0, m, {(10-MW){1'b0}}};
    end
    endgenerate

    always @(*) begin
        if (e == 7'd0) begin
            fp16 = {s, 15'd0};
        end else begin
            e16 = $signed({2'b0, e}) - 9'sd48;      // bias 63 -> 15
            m11 = m11_w;

            // Rounding carried out of the fraction. m11 is the STORED fraction,
            // not the significand, so the carry means 1.111..1 rounded to
            // 10.000..0: fraction to zero, exponent up. Shifting m11 right
            // instead leaves the carry bit in the fraction and gives
            // 1.5*2^(e+1) where 1.0*2^(e+1) was meant -- a 50% error.
            if (m11[10]) begin
                e16 = e16 + 9'sd1;
                m11 = 11'd0;
            end

            if (e16 <= 0)       fp16 = {s, 15'd0};
            else if (e16 >= 31) fp16 = {s, 5'h1E, 10'h3FF};
            else                fp16 = {s, e16[4:0], m11[9:0]};
        end
    end
endmodule

`default_nettype wire
