// A first-word-fall-through FIFO: `dout` is the head whenever `!empty`.
// Storage has a synchronous write and an asynchronous read, so it maps to
// LUTRAM; depth is a power of two.

`default_nettype none

module v2_fifo #(
    parameter integer W = 8,
    parameter integer D = 8
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         push,
    input  wire [W-1:0] din,
    input  wire         pop,
    output wire [W-1:0] dout,
    output wire         empty,
    output wire         full,
    output reg  [$clog2(D):0] count
);
    localparam integer AW = $clog2(D);
    reg [W-1:0]  mem [0:D-1];
    reg [AW-1:0] wp, rp;

    assign dout  = mem[rp];
    assign empty = (count == 0);
    assign full  = (count == D[AW:0]);

    always @(posedge clk) begin
        if (push) begin
            mem[wp] <= din;
        end
        if (rst) begin
            wp <= {AW{1'b0}};
            rp <= {AW{1'b0}};
            count <= {(AW+1){1'b0}};
        end else begin
            if (push) wp <= wp + 1'b1;
            if (pop)  rp <= rp + 1'b1;
            count <= count + (push ? 1'b1 : 1'b0) - (pop ? 1'b1 : 1'b0);
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (!rst && ((push && full && !pop) || (pop && empty))) begin
            $display("V2_FIFO_MISUSE %m t=%0t push %0d pop %0d count %0d", $time, push, pop, count);
        end
    end
`endif

endmodule

`default_nettype wire
