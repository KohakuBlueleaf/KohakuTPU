// mx_cluster_cu's limiter, isolated: DSP output -> leading-one search -> DSP
// input. Measured 354 MHz at 7 levels, val_r_reg -> b_phi_reg.

// DEPTH moves the register only; the arithmetic is identical.
//   0 as it ships   1 register after the smear   2 also register the product

`default_nettype none

module search_pipe_poc #(
    parameter integer DEPTH = 0,
    parameter integer LANES = 16,
    parameter integer VW    = 30,   // VWM, the lane magnitude
    parameter integer MW    = 18    // the shift DSP's B port
)(
    input  wire                     clk,
    input  wire                     rst,
    input  wire [LANES*VW-1:0]      val_in,
    output reg  [LANES*(VW+16)-1:0] p_out
);
    genvar g;
    integer s;
    generate
    for (g = 0; g < LANES; g = g + 1) begin : g_lane
        // Models val_r, which synthesis maps into the stage-1 DSP's MREG.
        reg [VW-1:0] val_r;
        always @(posedge clk) begin
            if (rst) begin
                val_r <= {VW{1'b0}};
            end
            else begin
                val_r <= val_in[g*VW +: VW];
            end
        end

        // smear: log2(VW) levels of OR
        reg [VW-1:0] y;
        always @(*) begin
            y = val_r;
            for (s = 1; s < VW; s = s * 2) begin
                y = y | (y >> s);
            end
        end

        if (DEPTH == 0) begin : g_flat
            wire [VW-1:0]    oh   = y & ~(y >> 1);
            wire [VW+15:0]   prod = val_r * oh[15:0];
            always @(posedge clk) begin
                if (rst) begin
                    p_out[g*(VW+16) +: VW+16] <= {(VW+16){1'b0}};
                end
                else begin
                    p_out[g*(VW+16) +: VW+16] <= prod;
                end
            end
        end else begin : g_piped
            reg [VW-1:0]   y_r, v_r2;
            reg [VW+15:0]  m_r;
            wire [VW-1:0]  oh   = y_r & ~(y_r >> 1);
            wire [VW+15:0] prod = v_r2 * oh[15:0];
            always @(posedge clk) begin
                if (rst) begin
                    y_r <= {VW{1'b0}}; v_r2 <= {VW{1'b0}};
                    m_r <= {(VW+16){1'b0}};
                    p_out[g*(VW+16) +: VW+16] <= {(VW+16){1'b0}};
                end else begin
                    y_r  <= y;
                    v_r2 <= val_r;
                    if (DEPTH >= 2) begin
                        m_r <= prod;
                        p_out[g*(VW+16) +: VW+16] <= m_r;
                    end else begin
                        p_out[g*(VW+16) +: VW+16] <= prod;
                    end
                end
            end
        end
    end
    endgenerate
endmodule

`default_nettype wire
