// noc_cu_base under output backpressure -- no mesh, no orchestrator.
//
// The mesh benches cannot reach this case: they never hold a CU's outbound link
// busy long enough for completions to pile up. Here the testbench drives
// noc_out_busy directly, so the framework's signal path is tested in isolation.
//
// What is at stake: SIG_INST_COMPLETE returns a dispatch credit. A signal that is
// generated but never transmitted is a credit that never comes back, and the
// orchestrator stalls later with nothing to point at. "Every instruction produces
// exactly one signal" therefore has to hold under *any* backpressure pattern, not
// just an idle link.

`default_nettype none
`timescale 1ns/1ps

module cu_base_tb;
    localparam FW = 288;
    localparam PW = 4;
    localparam CX = 2, CY = 2;     // the CU under test
    localparam HX = 1, HY = 1;     // us

    localparam [3:0] T_CU_INST   = 4'h5;
    localparam [3:0] T_CU_SIGNAL = 4'h6;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg  [FW-1:0] in_data;
    reg           in_valid;
    wire          in_busy;
    wire [FW-1:0] out_data;
    wire          out_valid;
    reg           out_busy;
    wire [31:0]   retired, faults, acc;

    cu_alu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(CX), .POS_Y(CY),
             .INST_DEPTH(32)) dut (
        .clk(clk), .resetn(resetn),
        .noc_in_data(in_data),   .noc_in_valid(in_valid),   .noc_in_busy(in_busy),
        .noc_out_data(out_data), .noc_out_valid(out_valid), .noc_out_busy(out_busy),
        .retired(retired), .faults(faults), .acc(acc)
    );

    integer errors = 0, checks = 0;
    task chk(input [63:0] got, input [63:0] want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                $display("  FAIL %0s: got %0d want %0d", what, got, want);
                errors = errors + 1;
            end
        end
    endtask

    // Count every CU_SIGNAL the CU DELIVERS, and accumulate its arg so a lost
    // signal is distinguishable from a duplicated one.
    //
    // `!out_busy` is the whole handshake: noc_cu_base holds a flit asserted
    // until a cycle the receiver is not busy (noc_cu_base.v s"Hold the flit"),
    // so counting `out_valid` alone counts one delivered signal once per cycle
    // it waited -- which is 198 for a signal held across this bench's first
    // 200-cycle stall, and reads as 198 duplicated signals.
    integer sig_count = 0;
    reg [63:0] sig_arg_sum = 0;
    always @(posedge clk) begin
        if (
            resetn
            && out_valid
            && !out_busy
            && out_data[FW-4*PW-1 -: 4] == T_CU_SIGNAL
        ) begin
            sig_count   <= sig_count + 1;
            sig_arg_sum <= sig_arg_sum + out_data[247 -: 32];
        end
    end

    // Push a CU_INST. Respects in_busy, and holds valid for exactly one cycle --
    // the NoC link is busy/valid, so a sender checks busy *then* asserts valid.
    task push_inst(input [7:0] txn, input last, input [7:0] op,
                   input [31:0] a, input [23:0] b);
        begin
            while (in_busy) begin
                @(posedge clk);
            end
            in_data  <= {CX[3:0], CY[3:0], HX[3:0], HY[3:0], T_CU_INST, txn,
                         last, 3'b000, 192'd0, op, a, b};
            in_valid <= 1'b1;
            @(posedge clk);
            in_valid <= 1'b0;
        end
    endtask

    // The RTL's own view, so a discrepancy names which side lost the signal.
    // Both failures this bench once reported were on THIS side: it counted
    // valid-cycles rather than transfers, and it drove `out_busy` blockingly
    // from a process woken by the same edge the counter samples on -- so the
    // count depended on process order within one timestep.
    integer n_wr = 0, n_done = 0, n_acc = 0;
    always @(posedge clk) if (resetn) begin
        if (dut.base.u_sig.wr_en) begin
            n_wr   <= n_wr + 1;
        end
        if (dut.exec_done) begin
            n_done <= n_done + 1;
        end
        if (dut.inst_valid && dut.inst_ready) begin
            n_acc  <= n_acc + 1;
        end
        if (dut.exec_done && dut.inst_ready) begin
            $display("  FAIL the datapath raised exec_done and inst_ready together; noc_cu_base drops in_flight and the new instruction's completion is never queued");
        end
    end

    integer i, expect_sum;
    initial begin
        in_data = 0; in_valid = 0; out_busy <= 0;
        repeat (8) @(posedge clk);
        resetn = 1;
        repeat (4) @(posedge clk);

        // ---------------------------------------------------------------
        $display("--- 1. link busy while 8 instructions retire ---");
        // op 0 takes 1 cycle, so completions arrive far faster than a blocked
        // link can drain them. Every one must still be delivered.
        out_busy <= 1'b1;
        expect_sum = 0;
        for (i = 0; i < 8; i = i + 1) begin
            push_inst(8'h10 + i[7:0], (i == 7), 8'd0, 32'd100 + i, 24'd1);
            expect_sum = expect_sum + (100 + i + 1);
        end
        repeat (200) @(posedge clk);
        chk(retired, 32'd8, "instructions retired");
        chk(sig_count, 32'd0, "signals sent while link busy");

        out_busy <= 1'b0;
        repeat (300) @(posedge clk);
        chk(n_acc,  32'd8, "instructions accepted by the datapath");
        chk(n_done, 32'd8, "completions raised by the datapath");
        chk(n_wr,   32'd8, "completions queued by the framework");
        chk(sig_count, 32'd8, "signals after link freed");
        // the last one is a batch report, so it carries the program id, not a
        // result -- account for that when checking the args survived
        chk(sig_arg_sum, expect_sum - (100 + 7 + 1) + 8'h17, "signal args");

        // ---------------------------------------------------------------
        $display("--- 2. link stuttering during a longer program ---");
        sig_count = 0;
        for (i = 0; i < 24; i = i + 1) begin
            push_inst(8'h20 + i[7:0], (i == 23), i[7:0] % 8'd7, 32'd7 + i, 24'd3);
        end
        // hold busy in bursts while the program runs
        repeat (12) begin
            out_busy <= 1'b1;  repeat (7) @(posedge clk);
            out_busy <= 1'b0;  repeat (3) @(posedge clk);
        end
        out_busy <= 1'b0;
        repeat (600) @(posedge clk);
        chk(retired,   32'd32, "instructions retired, total");
        chk(sig_count, 32'd24, "signals from the stuttered program");

        // ---------------------------------------------------------------
        $display("--- 3. link busy permanently: the CU must stop, not drop ---");
        // With no way to signal, the CU must apply backpressure rather than
        // execute into the void. in_busy going high is the correct response.
        sig_count = 0;
        out_busy  = 1'b1;
        for (i = 0; i < 40; i = i + 1) begin
            in_data  <= {CX[3:0], CY[3:0], HX[3:0], HY[3:0], T_CU_INST,
                         8'h30 + i[7:0], 1'b0, 3'b000, 192'd0, 8'd0, 32'd1, 24'd1};
            in_valid <= !in_busy;
            @(posedge clk);
            in_valid <= 1'b0;
        end
        repeat (100) @(posedge clk);
        chk(sig_count, 32'd0, "no signals escaped a busy link");
        out_busy <= 1'b0;
        repeat (800) @(posedge clk);
        // whatever it accepted, it must eventually signal for all of it
        chk(sig_count, retired - 32'd32, "one signal per accepted instruction");

        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #400000;
        $display("WATCHDOG TIMEOUT -- signals never drained");
        $finish;
    end

endmodule

`default_nettype wire
