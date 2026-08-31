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

    // THE DATA REGISTERS CARRY CLOCK ENABLES, NOT FEEDBACK MUXES. Written in
    // the control `else if` chain, each bit's D input becomes a mux back onto
    // itself: a LUT per bit per register, at W = 256, twice per memory port.
    // Split out, `hold_data` has one source under an enable and costs no logic,
    // and `out_data` keeps the one 2:1 select it needs. Same behaviour.
    wire out_en  = !out_valid || o_ready;
    wire hold_en = !out_en && i_valid && i_ready;

    always @(posedge clk) begin
        if (rst) begin
            out_valid  <= 1'b0;
            hold_valid <= 1'b0;
        end
        else if (out_en) begin
            // Drain the hold slot first, so beats leave in arrival order.
            out_valid  <= hold_valid || i_valid;
            hold_valid <= 1'b0;
        end
        else if (hold_en) begin
            hold_valid <= 1'b1;
        end
    end

    // The skid itself is W LUT: `out_data`'s 2:1. The 700-odd the hierarchy
    // report shows for the memory ports' instance is the port's own
    // `mem_out_data` select parked at this level by the rebuilt hierarchy
    // (LUT census of the node's checkpoint: 259 + 256 mem_out_data, 257
    // out_data); registering the source measured -100 on the node, not the
    // -577 the split suggested. Forcing one materialisation with `keep` was
    // MEASURED at +845 LUT. A hierarchy row is not a cost; census it.
    always @(posedge clk) begin
        if (out_en) begin
            out_data <= hold_valid ? hold_data : i_data;
        end
        if (hold_en) begin
            hold_data <= i_data;
        end
    end
endmodule

`default_nettype wire
