// Two-entry skid buffer: full-throughput valid/ready register stage.
// `i_ready` is never a function of `o_ready`, so no path spans a station.

`default_nettype none

module sb_skid #(
    parameter integer W = 8
)(
    input  wire         clk,
    input  wire         rst,          // active high

    input  wire         i_valid,
    output wire         i_ready,
    input  wire [W-1:0] i_data,

    output wire         o_valid,
    input  wire         o_ready,
    output wire [W-1:0] o_data
);
    reg [W-1:0] hold_data, out_data;
    reg         hold_valid, out_valid;

    // `rst`, not `o_ready`, so no path still spans a station: the reset branch
    // below captures nothing, so a beat taken while held is accepted and lost.
    assign i_ready = !hold_valid && !rst;
    assign o_valid = out_valid;
    assign o_data  = out_data;

    always @(posedge clk) begin
        if (rst) begin
            out_valid  <= 1'b0;
            hold_valid <= 1'b0;
        end else if (!out_valid || o_ready) begin
            // Drain the hold slot first, so beats leave in arrival order.
            if (hold_valid) begin
                out_data   <= hold_data;
                out_valid  <= 1'b1;
                hold_valid <= 1'b0;
            end else begin
                out_data  <= i_data;
                out_valid <= i_valid;
            end
        end else if (i_valid && i_ready) begin
            hold_data  <= i_data;
            hold_valid <= 1'b1;
        end
    end
endmodule

`default_nettype wire
