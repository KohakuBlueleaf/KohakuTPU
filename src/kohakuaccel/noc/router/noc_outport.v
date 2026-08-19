// Output port: round-robin arbitration across the five input ports' head flits,
// and the register that drives the outbound link.
//
// The input ports present their heads from a REGISTER with the output direction
// already computed (noc_inport.v), so the only combinational path into port_out
// is arbitration plus the 5:1 mux. Driving the arbiter from the FIFO output
// instead saves flops but does not clear 300 MHz with placement-proof slack.
//
// The link is a busy/valid pair WITH RETRY: a flit stays asserted until a cycle
// in which the receiver is not busy, and grants are withheld while it waits.
// Clearing out_valid on `busy` instead destroys the flit -- every receiver
// refuses while its own busy is high, so one committed against busy at T and
// presented at T+1 into a receiver busy at T+1 is gone, silently, and invisibly
// below four clusters because nothing fills. See docs/noc/spec.md s2.1.

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

    // Rotate so `port_rr` is bit 0, take the lowest set bit, rotate back. The
    // else-if chain this replaces was five variable-index muxes in series, each
    // writing through a decoder, and it sat under `grants`: 888 paths at 11-14.
    wire [2:0] rr_n  = 3'd5 - port_rr;
    wire [4:0] rot   = (in_reqs >> port_rr) | (in_reqs << rr_n);
    wire [4:0] low   = rot & (~rot + 5'd1);
    wire [4:0] sel   = (low << port_rr) | (low >> rr_n);

    // The register can take a new flit unless it is holding one the receiver has
    // not taken. One flit per cycle is sustained while the link is free, because
    // `room` is also true on the cycle the held flit is being accepted.
    wire room = !(out_valid && busy);

    // Nothing is granted while the register is occupied, so the flit stays queued
    // in the input port rather than being popped on top of one that has not left.
    assign grants = room ? sel : 5'b00000;

    always @(posedge clk or posedge rst) begin
        // `port_out` is not reset: `out_valid` says whether it means anything.
        if (rst) begin
            port_rr   <= 3'd0;
            out_valid <= 1'b0;
        end else begin
            port_rr <= (port_rr == 3'd4) ? 3'd0 : port_rr + 3'd1;
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
