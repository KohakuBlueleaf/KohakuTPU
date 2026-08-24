// mv_exec alone: does `mv.go ptr` turn a descriptor in the scratchpad into the
// same cfg pulses the driver's seven writes produce?
//
// The descriptor here is mover.py's copy() program for
//   copy(Walker(0x100000,[(64,32)]), Walker(0x200000,[(64,32)]))
// so a pass means the executor reproduces byte for byte what the host writes
// today through AUX_CFG.

`timescale 1ns / 1ps
`default_nettype none

module mv_exec_tb;
    localparam integer SAW = 11;

    integer errors = 0, checks = 0, spin, i;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg           go = 0;
    reg [SAW-1:0] ptr = 0;
    wire          busy;

    wire          sp_req;
    wire [SAW-1:0] sp_addr;
    reg  [31:0]   sp_data;

    wire        cfg_en;
    wire [7:0]  cfg_addr;
    wire [63:0] cfg_data;

    reg mv_busy = 0;

    mv_exec #(.SAW(SAW)) dut (
        .clk(clk), .resetn(resetn),
        .go(go), .ptr(ptr), .busy(busy),
        .sp_req(sp_req), .sp_addr(sp_addr), .sp_data(sp_data),
        .cfg_en(cfg_en), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
        .mv_busy(mv_busy)
    );

    // A scratchpad: synchronous read, data one cycle after the address, which is
    // what rv_spad does.
    reg [31:0] spad [0:2047];
    always @(posedge clk) begin
        if (sp_req) begin
            sp_data <= spad[sp_addr];
        end
    end

    // ---- what came out ------------------------------------------------------
    reg [7:0]  got_a [0:15];
    reg [63:0] got_d [0:15];
    integer    n = 0;
    always @(posedge clk) if (resetn && cfg_en) begin
        got_a[n] = cfg_addr;
        got_d[n] = cfg_data;
        n = n + 1;
    end

    task chk(input cond, input [8*40-1:0] what, input [63:0] got,
             input [63:0] want);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                $display("  FAIL %0s: got %h want %h", what, got, want);
            end
        end
    endtask

    // The driver's program, as {offset, value} pairs.
    localparam integer NW_PROG = 7;
    reg [7:0]  p_off [0:6];
    reg [63:0] p_val [0:6];

    integer w;
    task load_descriptor(input [SAW-1:0] at);
        integer k, a;
        begin
            spad[at] = NW_PROG;
            a = at + 1;
            for (k = 0; k < NW_PROG; k = k + 1) begin
                spad[a]     = {24'd0, p_off[k]};
                spad[a + 1] = p_val[k][31:0];
                spad[a + 2] = p_val[k][63:32];
                a = a + 3;
            end
        end
    endtask

    task fire(input [SAW-1:0] at);
        begin
            n = 0;
            @(negedge clk); ptr = at; go = 1;
            @(negedge clk); go = 0;
            // The engine picks the GO write up; the bench plays mm_mover.
            spin = 0;
            while (n < NW_PROG && spin < 400) begin
                @(negedge clk); spin = spin + 1;
            end
            @(negedge clk); mv_busy = 1;
            repeat (4) @(negedge clk);
            chk(busy === 1'b1, "busy spans the walk", {63'd0, busy}, 1);
            mv_busy = 0;
            repeat (4) @(negedge clk);
        end
    endtask

    initial begin
        p_off[0] = 8'h10; p_val[0] = 64'h0000_1000_0100_0000;
        p_off[1] = 8'h18; p_val[1] = 64'h0000_0000_0200_0400;
        p_off[2] = 8'h20; p_val[2] = 64'd0;
        p_off[3] = 8'h10; p_val[3] = 64'h0000_1000_0200_0001;
        p_off[4] = 8'h18; p_val[4] = 64'h0000_0000_0200_0401;
        p_off[5] = 8'h20; p_val[5] = 64'd0;
        p_off[6] = 8'h00; p_val[6] = 64'h0000_0000_0001_0808;

        for (i = 0; i < 2048; i = i + 1) begin
            spad[i] = 32'hDEAD_BEEF;
        end
        load_descriptor(11'd64);

        repeat (10) @(negedge clk);
        resetn = 1;
        repeat (5) @(negedge clk);

        chk(busy === 1'b0, "idle at rest", {63'd0, busy}, 0);

        $display("--- the driver's seven writes, from a pointer ---");
        fire(11'd64);
        chk(n == NW_PROG, "pulse count", n, NW_PROG);
        for (i = 0; i < NW_PROG && i < n; i = i + 1) begin
            chk(got_a[i] === p_off[i], "cfg offset", {56'd0, got_a[i]},
                {56'd0, p_off[i]});
            chk(got_d[i] === p_val[i], "cfg data", got_d[i], p_val[i]);
        end
        chk(busy === 1'b0, "idle after the walk", {63'd0, busy}, 0);

        // A second descriptor from a different pointer: the walk must not carry
        // state, which is what makes program order the queue.
        $display("--- a second descriptor, different pointer ---");
        p_off[0] = 8'h38; p_val[0] = 64'hFEED_FACE_1234_5678;
        load_descriptor(11'd512);
        fire(11'd512);
        chk(n == NW_PROG, "second pulse count", n, NW_PROG);
        chk(got_a[0] === 8'h38, "second first offset", {56'd0, got_a[0]}, 8'h38);
        chk(got_d[0] === 64'hFEED_FACE_1234_5678, "second first data",
            got_d[0], 64'hFEED_FACE_1234_5678);

        if (errors == 0) begin
            $display("PASS mv_exec_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL mv_exec_tb: %0d errors, %0d checks", errors, checks);
        end
        $finish;
    end

    initial begin
        #200000;
        $display("FAIL mv_exec_tb: watchdog");
        $finish;
    end
endmodule

`default_nettype wire
