// A toggle from any clock (rv64_syscore CTRL 0x38) -> an XDMA user interrupt:
// pipeline flops and the 2-flop crossing keep each flip one flip; events closer
// than the path merge, as the host reads the completion queue, not a count.
// `req` is PG195's usr_irq_req: held until `ack`, dropped, raised again if due.
// Two flips inside one `clk` period cancel; the firmware flips once a package.

`default_nettype none

module kohaku_irq_req #(
    parameter integer SYNC = 2          // crossing flops, at least 2
)(
    input  wire clk,          // the PCIe core's user clock
    input  wire resetn,
    input  wire evt,          // the toggle, from any clock
    output reg  req,
    input  wire ack
);
    (* ASYNC_REG = "TRUE" *) reg [SYNC-1:0] sync;
    reg seen, pend;

    always @(posedge clk) begin
        if (!resetn) begin
            sync <= {SYNC{1'b0}};
            seen <= 1'b0;
            pend <= 1'b0;
            req  <= 1'b0;
        end else begin
            sync <= {sync[SYNC-2:0], evt};
            if (sync[SYNC-1] != seen) begin
                seen <= sync[SYNC-1];
                pend <= 1'b1;
            end
            if (req) begin
                if (ack) begin
                    req <= 1'b0;
                end
            end else if (pend && (sync[SYNC-1] == seen)) begin
                req  <= 1'b1;
                pend <= 1'b0;
            end
        end
    end
endmodule

`default_nettype wire
