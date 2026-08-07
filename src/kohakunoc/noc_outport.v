// Output port: round-robin arbitration across the five input ports' head flits,
// and the register that drives the outbound link.
//
// The input ports present their heads from a REGISTER, one flit each, with the
// output direction already computed (noc_inport.v). So the only combinational
// path into port_out is arbitration plus the 5:1 mux -- FIFO read and route
// computation happened a cycle earlier. Driving the arbiter straight from the
// FIFO output instead saves ~1.5k flip-flops per router and measured 347 MHz
// against a 300 MHz target, which is not slack that survives placement.
//
// The link is a busy/valid pair WITH RETRY: a flit stays asserted until a cycle
// in which the receiver is not busy, and grants are withheld while it waits.
//
// It did not used to retry -- `busy` cleared out_valid and the flit was simply
// gone. That is only safe if every receiver accepts unconditionally, and none
// of them do: each one refuses while its own busy is high, so a flit committed
// against busy at T and presented at T+1 into a receiver that raised busy at
// T+1 was destroyed. Silent, and invisible below four clusters because nothing
// filled. See docs/noc/spec.md s2.1.

module OutPortSwitch #(
    parameter DATA_WIDTH = 288
)(
    input clk,
    input rst,

    // In Port Signals -- indexed by SOURCE input port: 0:N 1:E 2:S 3:W 4:L
    input  wire [4:0][DATA_WIDTH-1:0] in_heads,
    input  wire [4:0]                 in_reqs,
    output wire [4:0]                 grants,

    // Output Signals
    output reg [DATA_WIDTH-1:0] port_out,
    output reg out_valid,
    input wire busy
);
    reg  [2:0] port_rr;
    wire [2:0] pr1, pr2, pr3, pr4, pr5;
    assign pr1 = port_rr;
    assign pr2 = (port_rr+1)%5;
    assign pr3 = (port_rr+2)%5;
    assign pr4 = (port_rr+3)%5;
    assign pr5 = (port_rr+4)%5;

    reg [4:0] sel;
    always @(*) begin
        sel = 5'b0;
        if      (in_reqs[pr1]) sel[pr1] = 1'b1;
        else if (in_reqs[pr2]) sel[pr2] = 1'b1;
        else if (in_reqs[pr3]) sel[pr3] = 1'b1;
        else if (in_reqs[pr4]) sel[pr4] = 1'b1;
        else if (in_reqs[pr5]) sel[pr5] = 1'b1;
    end

    // The register can take a new flit unless it is holding one the receiver has
    // not taken. One flit per cycle is sustained while the link is free, because
    // `room` is also true on the cycle the held flit is being accepted.
    wire room = !(out_valid && busy);

    // Nothing is granted while the register is occupied, so the flit stays queued
    // in the input port rather than being popped on top of one that has not left.
    assign grants = room ? sel : 5'b00000;

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            port_rr   <= 3'd0;
            port_out  <= {DATA_WIDTH{1'b0}};
            out_valid <= 1'b0;
        end else begin
            port_rr <= pr2;
            if (room) begin
                case (sel)
                    5'b00001: port_out <= in_heads[0];
                    5'b00010: port_out <= in_heads[1];
                    5'b00100: port_out <= in_heads[2];
                    5'b01000: port_out <= in_heads[3];
                    5'b10000: port_out <= in_heads[4];
                    default: ;
                endcase
                out_valid <= sel != 5'b00000;
            end
        end
    end
endmodule
