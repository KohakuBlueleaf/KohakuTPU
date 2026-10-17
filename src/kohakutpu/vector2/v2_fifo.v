// A first-word-fall-through FIFO: `dout` is the head whenever `!empty`.
// Storage has a synchronous write and an asynchronous read, so it maps to
// LUTRAM; depth is a power of two. OREG 1: `dout`/`dout1` are registers,
// loaded from the entries one and two behind the head (or `din`), so a
// consumer of the head does not see the read pointer through the LUTRAM.

`default_nettype none

module v2_fifo #(
    parameter integer W = 8,
    parameter integer D = 8,
    parameter integer OREG = 0
)(
    input  wire         clk,
    input  wire         rst,
    input  wire         push,
    input  wire [W-1:0] din,
    input  wire         pop,
    output wire [W-1:0] dout,
    output wire [W-1:0] dout1,      // the entry behind the head (count >= 2)
    output wire         empty,
    output wire         full,
    output reg  [$clog2(D):0] count
);
    localparam integer AW = $clog2(D);
    reg [W-1:0]  mem [0:D-1];
    reg [AW-1:0] wp, rp;

    generate if (OREG == 0) begin : g_comb
        assign dout  = mem[rp];
        assign dout1 = mem[rp + 1'b1];
    end else begin : g_oreg
        reg  [W-1:0] h0, h1;
        wire [AW-1:0] rp1 = rp + 1'b1;
        wire [AW-1:0] rp2 = rp + 2'd2;
        wire [W-1:0]  c1  = mem[rp1];
        wire [W-1:0]  c2  = mem[rp2];
        always @(posedge clk) begin
            if (pop) begin
                h0 <= (push && count == 1) ? din : c1;
                h1 <= (push && count == 2) ? din : c2;
            end else begin
                if (push && count == 0) h0 <= din;
                if (push && count == 1) h1 <= din;
            end
        end
        assign dout  = h0;
        assign dout1 = h1;
    end endgenerate
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
