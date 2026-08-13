// Double-pumped MAC: one DSP48E2 at 2x doing the work of two at 1x.

// mx_cluster_cu's profile is FLAT: splitting its worst path moved 354.5 ->
// 361.7 MHz because the next was 0.056 ns behind. Throughput, not Fmax.

// Multiplexes existing operand wires instead of fanning out to a second DSP,
// which is what makes it cheap at 88% CLB.

// MODE 0 = two DSPs at 1x, 1 = one DSP at 2x, 2 = 1 without the 1x recapture
// (p0_r/p1_r are already stable all 1x period; it was 62 of MODE 1's 124 FF).

`default_nettype none

module dsp_pump_poc #(
    parameter integer MODE  = 0,
    parameter integer LANES = 16,
    parameter integer XW    = 23,
    parameter integer MW    = 8
)(
    input  wire                        clk1x,
    input  wire                        clk2x,
    input  wire                        rst,
    input  wire [LANES*2*XW-1:0]       x_in,
    input  wire [LANES*2*MW-1:0]       m_in,
    output reg  [LANES*2*(XW+MW)-1:0]  p_out
);
    localparam integer PW = XW + MW;

    // 1x data is stable across both 2x halves, so no CDC: that is why the two
    // clocks must come from ONE MMCM at an integer ratio, phase aligned.
    reg phase;
    always @(posedge clk2x) if (rst) phase <= 1'b0; else phase <= ~phase;

    genvar g;
    generate
    for (g = 0; g < LANES; g = g + 1) begin : g_lane
        wire [XW-1:0] x0 = x_in[(g*2+0)*XW +: XW];
        wire [XW-1:0] x1 = x_in[(g*2+1)*XW +: XW];
        wire [MW-1:0] m0 = m_in[(g*2+0)*MW +: MW];
        wire [MW-1:0] m1 = m_in[(g*2+1)*MW +: MW];

        if (MODE == 0) begin : g_two
            reg [XW-1:0] a0_r, a1_r;
            reg [MW-1:0] b0_r, b1_r;
            reg [PW-1:0] m0_r, m1_r;
            always @(posedge clk1x) begin
                if (rst) begin
                    a0_r <= {XW{1'b0}}; a1_r <= {XW{1'b0}};
                    b0_r <= {MW{1'b0}}; b1_r <= {MW{1'b0}};
                    m0_r <= {PW{1'b0}}; m1_r <= {PW{1'b0}};
                    p_out[g*2*PW +: 2*PW] <= {(2*PW){1'b0}};
                end else begin
                    a0_r <= x0; a1_r <= x1;
                    b0_r <= m0; b1_r <= m1;
                    m0_r <= a0_r * b0_r;
                    m1_r <= a1_r * b1_r;
                    p_out[g*2*PW +: 2*PW] <= {m1_r, m0_r};
                end
            end
        end else begin : g_pump
            reg [XW-1:0] a_r;
            reg [MW-1:0] b_r;
            reg [PW-1:0] m_r, p0_r, p1_r;
            always @(posedge clk2x) begin
                if (rst) begin
                    a_r <= {XW{1'b0}}; b_r <= {MW{1'b0}};
                    m_r <= {PW{1'b0}};
                    p0_r <= {PW{1'b0}}; p1_r <= {PW{1'b0}};
                end else begin
                    a_r <= phase ? x1 : x0;
                    b_r <= phase ? m1 : m0;
                    m_r <= a_r * b_r;
                    if (phase) p0_r <= m_r;
                    else       p1_r <= m_r;
                end
            end
            if (MODE == 1) begin : g_recap
                always @(posedge clk1x) begin
                    if (rst) p_out[g*2*PW +: 2*PW] <= {(2*PW){1'b0}};
                    else     p_out[g*2*PW +: 2*PW] <= {p1_r, p0_r};
                end
            end else begin : g_direct
                always @(*) p_out[g*2*PW +: 2*PW] = {p1_r, p0_r};
            end
        end
    end
    endgenerate
endmodule

`default_nettype wire
