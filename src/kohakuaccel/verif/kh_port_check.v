// Bindable checker for the six-signal NoC port (docs/spec/compute-unit-port.md).
// Simulation only; instantiate beside any endpoint. Makes the FORCED
// conventions executable: silent flit loss is the hazard every rule protects
// against (integrate/what-you-own.md s3).
`timescale 1ns / 1ps
`default_nettype none

module kh_port_check #(
    parameter FLIT_WIDTH = 288,
    parameter NAME       = "port",
    // busy held this many cycles reads as a wedge -- the queued-signal /
    // undrained-ack failure presents exactly this way.
    parameter integer MAX_BUSY = 5000
)(
    input wire                  clk,
    input wire                  rst,
    input wire [FLIT_WIDTH-1:0] in_data,
    input wire                  in_valid,
    input wire                  in_busy,
    input wire [FLIT_WIDTH-1:0] out_data,
    input wire                  out_valid,
    input wire                  out_busy
);
    reg                  v_q;
    reg                  b_q;
    reg [FLIT_WIDTH-1:0] d_q;
    integer              busy_run = 0;
    integer              violations = 0;

    always @(posedge clk) begin
        v_q <= out_valid;
        b_q <= out_busy;
        d_q <= out_data;
        if (!rst) begin
            // Hold-until-taken: a refused flit must stay, unchanged. Dropping
            // it here is the lost-flit bug that wedged 4- and 8-cluster meshes.
            if (v_q && b_q && !out_valid) begin
                violations = violations + 1;
                $display("%0t ERROR %0s: out_valid dropped while busy held the flit",
                         $time, NAME);
            end
            if (v_q && b_q && out_valid && (out_data !== d_q)) begin
                violations = violations + 1;
                $display("%0t ERROR %0s: out_data changed while busy held the flit",
                         $time, NAME);
            end
            // Wedge watch: inbound busy that never releases is the undrained
            // ack / full signal-queue signature, and it stalls silently.
            if (in_busy) busy_run = busy_run + 1;
            else         busy_run = 0;
            if (busy_run == MAX_BUSY) begin
                violations = violations + 1;
                $display("%0t ERROR %0s: in_busy held for %0d cycles -- wedged?",
                         $time, NAME, MAX_BUSY);
            end
        end
    end

    task report;
        begin
            if (violations == 0)
                $display("  kh_port_check(%0s): clean", NAME);
            else
                $display("  FAIL kh_port_check(%0s): %0d violation(s)", NAME,
                         violations);
        end
    endtask

endmodule

`default_nettype wire
