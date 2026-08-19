// Bench for the CU template: the bench is the orchestrator, the way
// tests/vector/vec_cu_tb.v drives a real core. It proves the whole contract:
// discovery, a dispatched batch with per-instruction and batch completions,
// unknown-flit disposal, and hold-until-taken under backpressure (checked by
// kh_port_check, mounted on the port).
`timescale 1ns / 1ps
`default_nettype none

module kh_cu_template_tb;
    localparam FW = 288;
    localparam PW = 4;
    localparam CX = 2, CY = 2;   // the CU
    localparam HX = 0, HY = 0;   // us

    localparam [3:0] T_MEM_RD_RESP = 4'h2, T_CU_INST = 4'h5,
                     T_CU_SIGNAL = 4'h6, T_CU_CTRL = 4'h7;
    localparam [7:0] SIG_INST = 8'h00, SIG_BATCH = 8'h01, SIG_FAULT = 8'h04;

    reg clk = 0, resetn = 0;
    always #2 clk = ~clk;

    reg  [FW-1:0] in_data;
    reg           in_valid = 0;
    wire          in_busy;
    wire [FW-1:0] out_data;
    wire          out_valid;
    reg           out_busy = 0;
    wire          cu_busy;

    kh_cu_template #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(CX), .POS_Y(CY),
                     .INST_DEPTH(32)) dut (
        .clk(clk), .resetn(resetn),
        .noc_in_data(in_data), .noc_in_valid(in_valid), .noc_in_busy(in_busy),
        .noc_out_data(out_data), .noc_out_valid(out_valid),
        .noc_out_busy(out_busy), .busy(cu_busy)
    );

    kh_port_check #(.FLIT_WIDTH(FW), .NAME("cu"), .MAX_BUSY(2000)) u_check (
        .clk(clk), .rst(!resetn),
        .in_data(in_data), .in_valid(in_valid), .in_busy(in_busy),
        .out_data(out_data), .out_valid(out_valid), .out_busy(out_busy)
    );

    integer errors = 0, checks = 0;
    task chk(input [63:0] got, input [63:0] want, input [511:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                $display("%0t ERROR %0s: got %0h want %0h", $time, what, got, want);
            end
        end
    endtask

    function [FW-1:0] hdr(input [3:0] ty, input [7:0] id, input last,
                          input [255:0] payload);
        hdr = {CX[PW-1:0], CY[PW-1:0], HX[PW-1:0], HY[PW-1:0],
               ty, id, last, 3'b000, payload};
    endfunction

    // The port consumes on every posedge with valid && !busy, so valid must
    // cover EXACTLY ONE such edge: busy sampled at a negedge gates the NEXT
    // posedge. Clearing valid the moment busy reads low drops the flit.
    reg put_took;
    task put(input [FW-1:0] f);
        begin
            @(negedge clk);
            in_data  = f;
            in_valid = 1;
            put_took = 0;
            while (!put_took) begin
                put_took = !in_busy;
                @(negedge clk);
            end
            in_valid = 0;
        end
    endtask

    // Every outbound flit lands here; the initial block reads the logs.
    integer n_sig, n_ctrl;
    reg [7:0]  sig_code [0:15];
    reg [31:0] sig_arg  [0:15];
    reg [7:0]  sig_id   [0:15];
    reg [63:0] ctrl_val;
    wire [3:0] o_ty = out_data[FW-4*PW-1 -: 4];

    always @(posedge clk) begin
        if (!resetn) begin
            n_sig <= 0; n_ctrl <= 0;
        end else if (out_valid && !out_busy) begin
            if (o_ty == T_CU_SIGNAL) begin
                sig_code[n_sig] <= out_data[255 -: 8];
                sig_arg[n_sig]  <= out_data[247 -: 32];
                sig_id[n_sig]   <= out_data[FW-4*PW-5 -: 8];
                n_sig <= n_sig + 1;
            end else if (o_ty == T_CU_CTRL) begin
                // payload: op[255:248], idx[247:240], value[239:176]
                ctrl_val <= out_data[239 -: 64];
                n_ctrl <= n_ctrl + 1;
            end
        end
    end

    integer spin;
    task wait_sigs(input integer want);
        begin
            spin = 0;
            while (n_sig < want && spin < 3000) begin
                spin = spin + 1;
                @(negedge clk);
            end
            chk(n_sig, want, "expected completions arrived");
        end
    endtask

    initial begin
        repeat (6) @(negedge clk);
        resetn = 1;
        repeat (2) @(negedge clk);

        // 1. discovery: CU_CTRL index 0 is caps, answered by the base.
        put(hdr(T_CU_CTRL, 8'h11, 1'b0, {8'd1 /*read*/, 8'd0 /*idx*/, 240'd0}));
        spin = 0;
        while (n_ctrl < 1 && spin < 200) begin spin = spin + 1; @(negedge clk); end
        chk(n_ctrl, 1, "caps reply arrived");
        chk(ctrl_val[63:48], 16'h4B48, "caps CU_TYPE");
        chk(ctrl_val[47:40], 8'h01, "caps CU_VERSION");
        chk(ctrl_val[35:20], 16'd32, "caps INST_DEPTH");

        // 2. a batch: 5 + 7 + 30 accumulated, reported, then a last-marked op.
        put(hdr(T_CU_INST, 8'h20, 1'b0, {4'd0, 220'd0, 32'd5}));
        put(hdr(T_CU_INST, 8'h21, 1'b0, {4'd0, 220'd0, 32'd7}));
        put(hdr(T_CU_INST, 8'h22, 1'b0, {4'd0, 220'd0, 32'd30}));
        put(hdr(T_CU_INST, 8'h23, 1'b0, {4'd1, 220'd0, 32'd0}));
        put(hdr(T_CU_INST, 8'h77, 1'b1, {4'd0, 220'd0, 32'd0}));
        wait_sigs(5);
        chk(sig_code[0], SIG_INST, "acc #1 ordinary completion");
        chk(sig_code[3], SIG_INST, "report ordinary completion");
        chk(sig_arg[3], 32'd42, "report carries the accumulator");
        chk(sig_code[4], SIG_BATCH, "last instruction retires as batch");
        chk(sig_arg[4], 32'h77, "batch report carries the program id");

        // 3. an unknown type must be absorbed, not wedge the queue.
        put(hdr(T_MEM_RD_RESP, 8'h30, 1'b0, 256'hDEAD));
        repeat (10) @(negedge clk);

        // 4. backpressure: refuse the link while a completion is emitted;
        //    kh_port_check proves the flit is held unchanged.
        out_busy = 1;
        put(hdr(T_CU_INST, 8'h40, 1'b0, {4'd0, 220'd0, 32'd1}));
        repeat (12) @(negedge clk);
        out_busy = 0;
        wait_sigs(6);
        chk(sig_code[5], SIG_INST, "completion delivered after backpressure");

        spin = 0;
        while (cu_busy && spin < 200) begin spin = spin + 1; @(negedge clk); end
        chk(cu_busy, 1'b0, "unit drains to idle");

        u_check.report;
        if (errors == 0 && u_check.violations == 0)
            $display("PASS kh_cu_template_tb: %0d checks", checks);
        else
            $display("FAIL kh_cu_template_tb: %0d errors", errors);
        $finish;
    end
endmodule

`default_nettype wire
