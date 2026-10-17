// v2_fifo at OREG 0 and OREG 1 against a queue model: a random push/pop
// stream that runs the count through every level, empty and full included.
// While the count is at least 1, dout is the model's head; at least 2, dout1
// is the entry behind it. Both instances see the same stream.

`default_nettype none
`timescale 1ns/1ps

module v2_fifo_tb;
    localparam integer W = 16;
    localparam integer D = 8;
    localparam integer N = 40000;          // cycles

    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

    reg          push = 0, pop = 0;
    reg  [W-1:0] din = 0;
    wire [W-1:0] d0_c, d1_c, d0_r, d1_r;
    wire         e_c, f_c, e_r, f_r;
    wire [3:0]   n_c, n_r;

    v2_fifo #(.W(W), .D(D), .OREG(0)) u_c (.clk(clk), .rst(rst), .push(push), .din(din),
        .pop(pop), .dout(d0_c), .dout1(d1_c), .empty(e_c), .full(f_c), .count(n_c));
    v2_fifo #(.W(W), .D(D), .OREG(1)) u_r (.clk(clk), .rst(rst), .push(push), .din(din),
        .pop(pop), .dout(d0_r), .dout1(d1_r), .empty(e_r), .full(f_r), .count(n_r));

    reg  [W-1:0] q [0:D-1];
    integer qn, i, cyc, errs, checks, mode;
    integer seen [0:D];

    task chk(input [W-1:0] got, input [W-1:0] want, input [127:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errs = errs + 1;
                if (errs <= 10) $display("  FAIL cyc %0d %0s got %h want %h (count %0d)", cyc, what, got, want, qn);
            end
        end
    endtask

    initial begin
        qn = 0; errs = 0; checks = 0;
        for (i = 0; i <= D; i = i + 1) seen[i] = 0;
        repeat (4) @(posedge clk);
        rst = 0;
        for (cyc = 0; cyc < N; cyc = cyc + 1) begin
            @(negedge clk);
            // the outputs of this cycle against the model
            chk({{(W-4){1'b0}}, n_c}, qn, "count oreg0");
            chk({{(W-4){1'b0}}, n_r}, qn, "count oreg1");
            if (qn >= 1) begin
                chk(d0_c, q[0], "dout oreg0");
                chk(d0_r, q[0], "dout oreg1");
            end
            if (qn >= 2) begin
                chk(d1_c, q[1], "dout1 oreg0");
                chk(d1_r, q[1], "dout1 oreg1");
            end
            seen[qn] = seen[qn] + 1;
            // phases bias the stream toward filling, draining or churning
            mode = (cyc / 500) % 3;
            push = (qn < D) && (($random & 7) < (mode == 0 ? 6 : mode == 1 ? 2 : 4));
            pop  = (qn > 0) && (($random & 7) < (mode == 0 ? 2 : mode == 1 ? 6 : 4));
            din  = $random;
            @(posedge clk);
            #0;
            if (pop) begin
                for (i = 0; i < D - 1; i = i + 1) q[i] = q[i + 1];
                qn = qn - 1;
            end
            if (push) begin
                q[qn] = din;
                qn = qn + 1;
            end
        end
        for (i = 0; i <= D; i = i + 1) begin
            checks = checks + 1;
            if (seen[i] == 0) begin
                errs = errs + 1;
                $display("  FAIL count level %0d never reached", i);
            end
        end
        $display("  count levels 0..%0d reached %0d/%0d/%0d/... cycles", D, seen[0], seen[1], seen[2]);
        if (errs == 0) $display("  PASS -- v2_fifo OREG 0 and 1: %0d checks", checks);
        else           $display("  FAIL -- v2_fifo: %0d errors of %0d checks", errs, checks);
        $finish;
    end
endmodule

`default_nettype wire
