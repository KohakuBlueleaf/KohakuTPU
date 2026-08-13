// mx_acu_fp stage 1 measured 2.111 ns logic / 0.229 ns route over 7 levels, all
// inside one DSP48E2: depth, not placement.

// DEPTH moves register placement only; the arithmetic is identical.
//   0 as it ships   1 + AREG/BREG   2 + MREG

// Under test: 1 and 2 land INSIDE the DSP, so they cost ~0 fabric FF and LUT.

`default_nettype none

module dsp_pipe_poc #(
    parameter integer DEPTH = 0,
    parameter integer LANES = 16,
    parameter integer XW    = 23,   // {1'b0, x} at the wider of the two halves
    parameter integer MW    = 8     // ma * mb, 64..225
)(
    input  wire                    clk,
    input  wire                    rst,
    input  wire [LANES*XW-1:0]     x_in,
    input  wire [LANES-1:0]        s_in,
    input  wire [LANES*MW-1:0]     m_in,
    output reg  [LANES*(XW+MW)-1:0] p_out
);
    genvar g;
    generate
    for (g = 0; g < LANES; g = g + 1) begin : g_lane
        wire [XW-1:0] x = x_in[g*XW +: XW];
        wire [MW-1:0] m = m_in[g*MW +: MW];
        wire          s = s_in[g];

        if (DEPTH == 0) begin : g_flat
            wire [XW+MW-1:0] p = (x + {{(XW-1){1'b0}}, s}) * m;
            always @(posedge clk) begin
                if (rst) p_out[g*(XW+MW) +: XW+MW] <= {(XW+MW){1'b0}};
                else     p_out[g*(XW+MW) +: XW+MW] <= p;
            end
        end else begin : g_piped
            reg [XW-1:0]     a_r;
            reg [MW-1:0]     b_r;
            reg [XW+MW-1:0]  m_r;
            wire [XW+MW-1:0] prod = a_r * b_r;
            always @(posedge clk) begin
                if (rst) begin
                    a_r <= {XW{1'b0}}; b_r <= {MW{1'b0}};
                    m_r <= {(XW+MW){1'b0}};
                    p_out[g*(XW+MW) +: XW+MW] <= {(XW+MW){1'b0}};
                end else begin
                    a_r <= x + {{(XW-1){1'b0}}, s};
                    b_r <= m;
                    if (DEPTH >= 2) begin
                        m_r <= prod;
                        p_out[g*(XW+MW) +: XW+MW] <= m_r;
                    end else begin
                        p_out[g*(XW+MW) +: XW+MW] <= prod;
                    end
                end
            end
        end
    end
    endgenerate
endmodule

`default_nettype wire
