// Output port: round-robin arbitration across the five input ports' head flits,
// and the register that drives the outbound link.
//
// This register is the router's only copy of a flit in flight. The input ports
// present their heads combinationally, so the path from FIFO output through route
// computation and arbitration into port_out is a single cycle -- one flit per
// cycle per output, sustained, with no holding registers anywhere upstream.
//
// grant is combinational and is withheld while the downstream link is busy, so an
// input port only pops a flit that is genuinely leaving. The NoC link is a
// busy/valid pair, not AXI valid/ready: valid must never be asserted into a full
// receiver, and there is no retry if it is.

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

    // Nothing is granted while the link is busy, so the flit stays queued in the
    // input port rather than being popped into a register that cannot drain.
    assign grants = busy ? 5'b00000 : sel;

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            port_rr   <= 3'd0;
            port_out  <= {DATA_WIDTH{1'b0}};
            out_valid <= 1'b0;
        end else begin
            port_rr <= pr2;
            if (~busy) begin
                case (sel)
                    5'b00001: port_out <= in_heads[0];
                    5'b00010: port_out <= in_heads[1];
                    5'b00100: port_out <= in_heads[2];
                    5'b01000: port_out <= in_heads[3];
                    5'b10000: port_out <= in_heads[4];
                    default: ;
                endcase
                out_valid <= sel != 5'b00000;
            end else begin
                out_valid <= 1'b0;
            end
        end
    end
endmodule
