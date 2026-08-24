// L1 in the 2x domain, feeding a 2x DSP. No operand mux at all.

// dsp_pump_poc needed a 2:1 mux only because operands came from 1x fabric. Read
// at 2x, L1 delivers a fresh entry natively: width stays, the clock changes.

// MODE 0 = 1x BRAM + 1x DSP (baseline), 1 = both at 2x, no mux.

`default_nettype none

module l1_pump_poc #(
    parameter integer MODE  = 0,
    parameter integer LANES = 16,
    parameter integer XW    = 23,
    parameter integer MW    = 8,
    parameter integer DEPTH = 512
)(
    input  wire                       clk1x,
    input  wire                       clk2x,
    input  wire                       rst,
    input  wire                       we,
    input  wire [8:0]                 waddr,
    input  wire [LANES*(XW+MW)-1:0]   wdata,
    input  wire [8:0]                 raddr,
    output reg  [LANES*(XW+MW)-1:0]   p_out
);
    localparam integer EW = LANES*(XW+MW);   // one L1 entry
    localparam integer PW = XW + MW;

    wire rclk = (MODE == 0) ? clk1x : clk2x;

    // The L1 entry: one BRAM-shaped memory, unchanged width in both modes.
    (* ram_style = "block" *) reg [EW-1:0] mem [0:DEPTH-1];
    reg [EW-1:0] rd_r;
    always @(posedge clk1x) begin
        if (we) begin
            mem[waddr] <= wdata;
        end
    end
    always @(posedge rclk) begin
        rd_r <= mem[raddr];
    end

    genvar g;
    generate
    for (g = 0; g < LANES; g = g + 1) begin : g_lane
        wire [XW-1:0] x = rd_r[g*PW +: XW];
        wire [MW-1:0] m = rd_r[g*PW+XW +: MW];
        reg  [XW-1:0] a_r;
        reg  [MW-1:0] b_r;
        reg  [PW-1:0] m_r;
        always @(posedge rclk) begin
            if (rst) begin
                a_r <= {XW{1'b0}}; b_r <= {MW{1'b0}}; m_r <= {PW{1'b0}};
            end else begin
                a_r <= x; b_r <= m; m_r <= a_r * b_r;
            end
        end
        always @(posedge rclk) begin
            if (rst) begin
                p_out[g*PW +: PW] <= {PW{1'b0}};
            end
            else begin
                p_out[g*PW +: PW] <= m_r;
            end
        end
    end
    endgenerate
endmodule

`default_nettype wire
