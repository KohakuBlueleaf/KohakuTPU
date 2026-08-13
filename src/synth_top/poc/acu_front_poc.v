// The mx_acu_fp front end alone, from mx_acu_fp.v:199-215 with its hardcoded
// 19 lifted to SPK. DUP=0 times fix C at 2x; DUP=1 prices fix A.

`default_nettype none

module acu_front_poc #(
    parameter integer SPK = 19,     // packing offset, S in mx_mac
    parameter integer HW  = 22,     // high field width
    parameter integer DUP = 0,
    // ALIGN=1 adds the two-input exponent align and add that fix C needs at 2x
    // and fix A needs at 1x. This is the only fabric in either chain.
    parameter integer ALIGN = 0,
    parameter integer PIPE  = 0
)(
    input  wire         clk,
    input  wire         rst,
    input  wire [383:0] part_in,
    input  wire [383:0] part_in2,
    input  wire [127:0] mm_in,      // 16 x 8, the scale-mantissa product
    input  wire [127:0] mm_in2,
    input  wire [159:0] exp_in,     // 16 x 10 signed
    input  wire [159:0] exp_in2,
    output reg  [511:0] o_mag,      // 16 x 32, flat: OOC ports cannot be arrays
    output reg  [15:0]  o_sgn
);
    reg [383:0] part_q, part_q2;
    reg [127:0] mm_q, mm_q2;

    always @(posedge clk) begin
        part_q <= part_in;  mm_q  <= mm_in;
        part_q2 <= part_in2; mm_q2 <= mm_in2;
    end

    wire [31:0] mag_a [0:15];
    wire [31:0] mag_b [0:15];
    wire        sgn_a [0:15];

    genvar gs, gt;
    generate
    for (gs = 0; gs < 4; gs = gs + 1) begin : g_r
        for (gt = 0; gt < 4; gt = gt + 1) begin : g_c
            localparam integer F = ((gs/2)*4 + gt) * 48;
            localparam integer L = gs*4 + gt;
            wire [7:0] m1 = mm_q [L*8 +: 8];
            wire [7:0] m2 = mm_q2[L*8 +: 8];

            if ((gs % 2) == 0) begin : g_lo
                wire          s = part_q[F+SPK-1];
                wire [SPK-1:0] x = part_q[F +: SPK] ^ {SPK{s}};
                assign sgn_a[L] = s;
                assign mag_a[L] = ({1'b0, x} + {{SPK{1'b0}}, s}) * m1;

                wire           s2 = part_q2[F+SPK-1];
                wire [SPK-1:0] x2 = part_q2[F +: SPK] ^ {SPK{s2}};
                assign mag_b[L] = ({1'b0, x2} + {{SPK{1'b0}}, s2}) * m2;
            end else begin : g_hi
                wire         s = part_q[F+SPK+HW-1];
                wire         r = part_q[F+SPK-1];
                wire [HW-1:0] x = part_q[F+SPK +: HW] ^ {HW{s}};
                assign sgn_a[L] = s;
                assign mag_a[L] = ({1'b0, x} + {{HW{1'b0}}, (s ^ r)}) * m1;

                wire          s2 = part_q2[F+SPK+HW-1];
                wire          r2 = part_q2[F+SPK-1];
                wire [HW-1:0] x2 = part_q2[F+SPK +: HW] ^ {HW{s2}};
                assign mag_b[L] = ({1'b0, x2} + {{HW{1'b0}}, (s2 ^ r2)}) * m2;
            end
        end
    end
    endgenerate

    reg [159:0] exp_q, exp_q2;
    always @(posedge clk) begin exp_q <= exp_in; exp_q2 <= exp_in2; end

    // Align the smaller exponent down and add. Shift capped at 31: past that the
    // term is below the other's LSB and contributes nothing.
    wire [31:0] merged [0:15];
    genvar gm;
    generate for (gm = 0; gm < 16; gm = gm + 1) begin : g_al
        wire signed [9:0] ea = exp_q [gm*10 +: 10];
        wire signed [9:0] eb = exp_q2[gm*10 +: 10];
        wire signed [10:0] d = ea - eb;
        wire [4:0] shw = (d[10] ? -d : d) > 31 ? 5'd31 : (d[10] ? -d : d);
        wire [31:0] hiw = d[10] ? mag_b[gm] : mag_a[gm];
        wire [31:0] low = d[10] ? mag_a[gm] : mag_b[gm];

        // PIPE>=1 cuts unpack+scale off the align; PIPE>=2 splits the barrel
        // shifter itself, which is the 17-level part.
        if (PIPE == 0) begin : g_p0
            assign merged[gm] = hiw + (low >> shw);
        end else if (PIPE == 1) begin : g_p1
            reg [4:0] sh_q; reg [31:0] hi_q, lo_q;
            always @(posedge clk) begin
                sh_q <= shw; hi_q <= hiw; lo_q <= low;
            end
            assign merged[gm] = hi_q + (lo_q >> sh_q);
        end else if (PIPE == 3) begin : g_p3
            // Registers the DSP OUTPUTS: at PIPE 1/2 the compare-and-swap mux
            // dragged the product into fabric and pinned the path at 10 levels.
            reg [31:0] ma_q, mb_q;
            reg signed [9:0] ea_q, eb_q;
            always @(posedge clk) begin
                ma_q <= mag_a[gm]; mb_q <= mag_b[gm];
                ea_q <= ea;        eb_q <= eb;
            end
            wire signed [10:0] d2 = ea_q - eb_q;
            wire [4:0] sh2 = (d2[10] ? -d2 : d2) > 31 ? 5'd31 : (d2[10] ? -d2 : d2);
            wire [31:0] hi2 = d2[10] ? mb_q : ma_q;
            wire [31:0] lo2 = d2[10] ? ma_q : mb_q;
            assign merged[gm] = hi2 + (lo2 >> sh2);
        end else begin : g_p2
            reg [4:0] sh_q; reg [31:0] hi_q, lo_q;
            reg [31:0] hi_q2, sft_q;
            always @(posedge clk) begin
                sh_q <= shw; hi_q <= hiw; lo_q <= low;
                sft_q <= lo_q >> sh_q;
                hi_q2 <= hi_q;
            end
            assign merged[gm] = hi_q2 + sft_q;
        end
    end endgenerate

    integer i;
    always @(posedge clk) begin
        if (rst) begin
            o_mag <= 512'd0; o_sgn <= 16'd0;
        end else begin
            for (i = 0; i < 16; i = i + 1) begin
                o_mag[i*32 +: 32] <= (ALIGN != 0) ? merged[i]
                                   : (DUP   != 0) ? (mag_a[i] + mag_b[i])
                                                  : mag_a[i];
                o_sgn[i] <= sgn_a[i];
            end
        end
    end

endmodule

`default_nettype wire
