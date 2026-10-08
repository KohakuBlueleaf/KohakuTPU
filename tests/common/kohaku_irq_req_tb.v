// kohaku_irq_req: a toggle from an unrelated clock, through a pipe of flops,
// becomes one usr_irq_req per isolated event, held until ack; a burst while a
// request is outstanding coalesces into one or two more requests, never none.

`timescale 1ns/1ps
`default_nettype none

module kohaku_irq_req_tb;
    reg sclk = 0, dclk = 0;
    always #3.7 sclk = ~sclk;          // the node's clock
    always #2.0 dclk = ~dclk;          // the PCIe core's user clock

    reg drst = 1;
    reg evt = 0;
    reg [2:0] pipe = 3'd0;              // the long route: three registers
    always @(posedge sclk) pipe <= {pipe[1:0], evt};

    wire req;
    reg  ack = 0;
    kohaku_irq_req dut (.clk(dclk), .resetn(!drst), .evt(pipe[2]), .req(req), .ack(ack));

    // The host side: acknowledge a request a few cycles after it rises.
    integer reqs = 0, errors = 0, held = 0;
    reg req_q = 0;
    always @(posedge dclk) begin
        req_q <= req;
        ack   <= 1'b0;
        if (req && !req_q) reqs = reqs + 1;
        if (req) begin
            held = held + 1;
            if (held == 4) ack <= 1'b1;
        end else begin
            held = 0;
        end
        if (ack && !req) begin
            errors = errors + 1;
            $display("FAIL ack without a request");
        end
    end

    task flip;
        begin
            @(posedge sclk);
            evt <= ~evt;
        end
    endtask

    task settle;
        begin
            repeat (60) @(posedge dclk);
        end
    endtask

    initial begin
        repeat (5) @(posedge sclk);
        drst = 0;
        settle;
        if (reqs != 0) begin
            errors = errors + 1;
            $display("FAIL %0d requests out of reset", reqs);
        end

        // Isolated events: one request each.
        flip; settle;
        flip; settle;
        flip; settle;
        if (reqs != 3) begin
            errors = errors + 1;
            $display("FAIL three isolated events gave %0d requests", reqs);
        end

        // Four flips on consecutive node cycles, each still seen (the receiver
        // samples faster), but landing while a request is up: they coalesce.
        flip; flip; flip; flip;
        settle; settle;
        if (reqs < 5 || reqs > 6) begin
            errors = errors + 1;
            $display("FAIL a 4-event burst after 3 gave %0d requests in total", reqs);
        end
        if (req) begin
            errors = errors + 1;
            $display("FAIL a request still up after its ack");
        end

        if (errors == 0) $display("PASS kohaku_irq_req: %0d requests", reqs);
        else $display("FAIL kohaku_irq_req: %0d errors", errors);
        $finish;
    end
endmodule

`default_nettype wire
