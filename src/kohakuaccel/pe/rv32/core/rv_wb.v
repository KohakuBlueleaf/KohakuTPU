// rv_wb -- architectural writeback, and the only place the machine's state
// changes in a way software can observe.
//
// It is combinational: the writeback register itself lives at the output of
// rv_mem, because that is the stage that produced the value. What is here is
// the commit decision and the retirement probe.
//
// THE RETIREMENT PROBE IS NOT DEBUG. tests/pe co-simulates this core against a
// Python RV32I model one instruction at a time, and the probe is the interface
// it compares on: PC, destination and value for every instruction that commits,
// in order. A core that is wrong for one spacing of one hazard shows up here on
// the instruction it happens to, not five thousand cycles later as a wrong
// answer.

`default_nettype none

module rv_wb (
    input  wire        w_valid,
    input  wire [4:0]  w_rd,
    input  wire        w_wen,
    input  wire [31:0] w_val,
    input  wire [31:0] w_pc,

    output wire        rf_we,
    output wire [4:0]  rf_wa,
    output wire [31:0] rf_wd,

    output wire        retire_valid,
    output wire [31:0] retire_pc,
    output wire [4:0]  retire_rd,
    output wire [31:0] retire_val
);
    assign rf_we = w_valid && w_wen;
    assign rf_wa = w_rd;
    assign rf_wd = w_val;

    assign retire_valid = w_valid;
    assign retire_pc    = w_pc;
    assign retire_rd    = (w_valid && w_wen) ? w_rd : 5'd0;
    assign retire_val   = (w_valid && w_wen) ? w_val : 32'd0;

endmodule

`default_nettype wire
