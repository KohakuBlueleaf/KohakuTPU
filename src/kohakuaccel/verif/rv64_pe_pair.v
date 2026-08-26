// Two SysCores on one fabric, for the multi-unit bench.
//
// Ports are per-unit rather than a flattened vector because the harness drives
// each one as its own NoC stream, which is the point: at this phase the units
// share nothing, so anything that couples them is a bug and this wrapper must
// not be where it comes from.
//
// The distinct POS_X is the only difference between them -- a unit's coordinate
// is what the endpoint replies from, so two units at the same coordinate would
// each answer the other's dispatcher.

`default_nettype none

module rv64_pe_pair #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer IMEM_WORDS = 4096,
    parameter integer SPAD_WORDS = 2048,
    parameter         MEM_PRIM   = "block"
)(
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  a_in_data,
    input  wire                   a_in_valid,
    output wire                   a_in_busy,
    output wire [FLIT_WIDTH-1:0]  a_out_data,
    output wire                   a_out_valid,
    input  wire                   a_out_busy,
    output wire                   a_busy,
    output wire                   a_console_we,
    output wire [7:0]             a_console,
    output wire [31:0]            a_cycles,
    output wire [31:0]            a_retired,

    input  wire [FLIT_WIDTH-1:0]  b_in_data,
    input  wire                   b_in_valid,
    output wire                   b_in_busy,
    output wire [FLIT_WIDTH-1:0]  b_out_data,
    output wire                   b_out_valid,
    input  wire                   b_out_busy,
    output wire                   b_busy,
    output wire                   b_console_we,
    output wire [7:0]             b_console,
    output wire [31:0]            b_cycles,
    output wire [31:0]            b_retired
);
    rv64_sys_pe #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(2), .POS_Y(2),
        .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS), .MEM_PRIM(MEM_PRIM)
    ) u_a (
        .clk(clk), .resetn(resetn),
        .noc_in_data(a_in_data), .noc_in_valid(a_in_valid),
        .noc_in_busy(a_in_busy),
        .noc_out_data(a_out_data), .noc_out_valid(a_out_valid),
        .noc_out_busy(a_out_busy),
        .halt_req(1'b0), .busy(a_busy),
        .dbg_cycles(a_cycles), .dbg_retired(a_retired),
        .dbg_console_we(a_console_we), .dbg_console(a_console)
    );

    rv64_sys_pe #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(3), .POS_Y(2),
        .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS), .MEM_PRIM(MEM_PRIM)
    ) u_b (
        .clk(clk), .resetn(resetn),
        .noc_in_data(b_in_data), .noc_in_valid(b_in_valid),
        .noc_in_busy(b_in_busy),
        .noc_out_data(b_out_data), .noc_out_valid(b_out_valid),
        .noc_out_busy(b_out_busy),
        .halt_req(1'b0), .busy(b_busy),
        .dbg_cycles(b_cycles), .dbg_retired(b_retired),
        .dbg_console_we(b_console_we), .dbg_console(b_console)
    );

endmodule

`default_nettype wire
