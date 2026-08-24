// rv_noc_req_tb -- the NoC requestor's own bench, field by field on the wire.
// It had none: every check came from rv_front or rv_sys, where a wrong header
// field shows up as a hung fill three modules away.
//
// THE VERDICT STARTS AT COLUMN 0 -- xsim.py matches PASS/FAIL on a stripped line
// start, so an indented verdict exits 1 however well the bench ran.

`timescale 1ns/1ps
`default_nettype none

module rv_noc_req_tb;
    localparam integer FW  = 288;
    localparam integer PW  = 4;
    localparam integer PAY = FW - 4*PW - 16;      // 256

    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_WR_DATA = 4'h4, T_CU_INST = 4'h5, T_CU_DATA = 4'h8;

    reg clk = 1'b0, resetn = 1'b0;
    always begin
        #2 clk = ~clk;
    end

    integer errors = 0, checks = 0;
    task chk(input [127:0] got, input [127:0] want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                $display("  FAIL %0s: got %h want %h", what, got, want);
                errors = errors + 1;
            end
        end
    endtask

    reg          fill_valid = 1'b0;
    wire         fill_ready;
    reg  [30:0]  fill_addr  = 31'd0;
    wire         resp_valid;
    wire [255:0] resp_data;

    reg          wb_valid = 1'b0;
    wire         wb_ready;
    reg  [30:0]  wb_addr  = 31'd0;
    reg  [255:0] wb_data  = 256'd0;

    reg          push_valid = 1'b0;
    wire         push_ready;
    reg  [PW-1:0] push_dx = 4'd0, push_dy = 4'd0;
    reg          push_win  = 1'b0;
    reg  [13:0]  push_gran = 14'd0;
    reg  [2:0]   push_sel  = 3'd0;
    reg  [3:0]   push_be   = 4'd0;
    reg  [31:0]  push_data = 32'd0;

    reg          disp_valid = 1'b0;
    wire         disp_ready;
    reg  [PW-1:0] disp_dx = 4'd0, disp_dy = 4'd0;
    reg  [7:0]   disp_txn  = 8'd0;
    reg          disp_last = 1'b0;
    reg  [2:0]   disp_rsvd = 3'd0;
    reg  [7:0]   disp_op   = 8'd0;
    reg  [31:0]  disp_pc   = 32'd0;
    reg  [31:0]  disp_arg  = 32'd0;

    wire [FW-1:0] send_flit;
    wire          send_valid;
    reg           send_ready = 1'b1;

    reg          rx_rd_resp = 1'b0;
    reg  [7:0]   rx_txn     = 8'd0;
    reg  [255:0] rx_data    = 256'd0;
    reg          rx_wr_ack  = 1'b0;

    reg          rx_sig      = 1'b0;
    reg  [7:0]   rx_sig_id   = 8'd0;
    reg  [7:0]   rx_sig_code = 8'd0;
    reg  [31:0]  rx_sig_arg  = 32'd0;
    reg          sig_pop     = 1'b0;
    wire [7:0]   sig_cnt;
    wire         sig_ovf;
    wire [7:0]   sig_code, sig_id;
    wire [31:0]  sig_arg;

    wire [15:0]  wr_out;
    wire         idle;

    rv_noc_req #(
        .FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(2), .POS_Y(2),
        .MEM_X(0), .MEM_Y(1), .DRAM_BASE(40'h00_0000_0000), .WR_MAX(1)
    ) dut (
        .clk(clk), .resetn(resetn),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data),
        .wb_valid(wb_valid), .wb_ready(wb_ready), .wb_addr(wb_addr),
        .wb_data(wb_data),
        .push_valid(push_valid), .push_ready(push_ready),
        .push_dx(push_dx), .push_dy(push_dy), .push_win(push_win),
        .push_gran(push_gran), .push_sel(push_sel), .push_be(push_be),
        .push_data(push_data),
        .disp_valid(disp_valid), .disp_ready(disp_ready),
        .disp_dx(disp_dx), .disp_dy(disp_dy), .disp_txn(disp_txn),
        .disp_last(disp_last), .disp_rsvd(disp_rsvd),
        .disp_op(disp_op), .disp_pc(disp_pc), .disp_arg(disp_arg),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .rx_rd_resp(rx_rd_resp), .rx_txn(rx_txn), .rx_data(rx_data),
        .rx_wr_ack(rx_wr_ack),
        .rx_sig(rx_sig), .rx_sig_id(rx_sig_id), .rx_sig_code(rx_sig_code),
        .rx_sig_arg(rx_sig_arg),
        .sig_pop(sig_pop), .sig_cnt(sig_cnt), .sig_ovf(sig_ovf),
        .sig_code(sig_code), .sig_id(sig_id), .sig_arg(sig_arg),
        .wr_out(wr_out), .idle(idle)
    );

    // EVERY FIELD IS AN ARGUMENT: in xsim a function called from a continuous
    // assign is sensitised by its ARGUMENT LIST alone, so a module-scope read
    // inside the body goes stale a cycle and reads like an addressing bug.
    reg [FW-1:0] cap [0:15];
    integer ncap = 0;
    always @(posedge clk) if (resetn && send_valid && send_ready) begin
        cap[ncap[3:0]] = send_flit;
        ncap = ncap + 1;
    end

    function [3:0]    f_dx;   input [FW-1:0] f; f_dx   = f[FW-1        -: PW]; endfunction
    function [3:0]    f_dy;   input [FW-1:0] f; f_dy   = f[FW-PW-1     -: PW]; endfunction
    function [3:0]    f_sx;   input [FW-1:0] f; f_sx   = f[FW-2*PW-1   -: PW]; endfunction
    function [3:0]    f_sy;   input [FW-1:0] f; f_sy   = f[FW-3*PW-1   -: PW]; endfunction
    function [3:0]    f_ty;   input [FW-1:0] f; f_ty   = f[FW-4*PW-1   -: 4];  endfunction
    function [7:0]    f_txn;  input [FW-1:0] f; f_txn  = f[FW-4*PW-5   -: 8];  endfunction
    function          f_last; input [FW-1:0] f; f_last = f[FW-4*PW-13];        endfunction
    function [2:0]    f_rsvd; input [FW-1:0] f; f_rsvd = f[FW-4*PW-14  -: 3];  endfunction
    function [39:0]   f_addr; input [FW-1:0] f; f_addr = f[PAY-1       -: 40]; endfunction
    function [7:0]    f_flag; input [FW-1:0] f; f_flag = f[PAY-49      -: 8];  endfunction
    function [7:0]    f_cnt;  input [FW-1:0] f; f_cnt  = f[PAY-57      -: 8];  endfunction
    function [7:0]    f_buf;  input [FW-1:0] f; f_buf  = f[PAY-1       -: 8];  endfunction
    function [15:0]   f_off;  input [FW-1:0] f; f_off  = f[PAY-9       -: 16]; endfunction
    function [7:0]    f_iop;  input [FW-1:0] f; f_iop  = f[PAY-1       -: 8];  endfunction
    function [31:0]   f_ipc;  input [FW-1:0] f; f_ipc  = f[PAY-9       -: 32]; endfunction
    function [31:0]   f_iarg; input [FW-1:0] f; f_iarg = f[PAY-41      -: 32]; endfunction

    task wait_flits(input integer n);
        integer g;
        begin
            g = 0;
            while ((ncap < n) && (g < 200)) begin @(posedge clk); g = g + 1; end
            if (ncap < n) begin
                $display("  FAIL timeout waiting for %0d flits, saw %0d", n, ncap);
                errors = errors + 1;
            end
        end
    endtask

    task clear; begin ncap = 0; end endtask

    integer w;

    initial begin
        repeat (4) @(posedge clk);
        resetn = 1'b1;
        @(posedge clk);

        // ---- 1. a line fill is one MEM_RD_REQ, entry-read shaped ------------
        clear;
        fill_addr  = 31'h0000_1234 & ~31'h1F;
        fill_valid = 1'b1;
        @(posedge clk);
        while (!fill_ready) begin
            @(posedge clk);
        end
        fill_valid = 1'b0;
        wait_flits(1);
        chk(f_ty(cap[0]),   T_MEM_RD_REQ, "fill type");
        chk(f_dx(cap[0]),   4'd0,         "fill dst x");
        chk(f_dy(cap[0]),   4'd1,         "fill dst y");
        chk(f_sx(cap[0]),   4'd2,         "fill src x");
        chk(f_sy(cap[0]),   4'd2,         "fill src y");
        chk(f_last(cap[0]), 1'b0,         "fill last");
        chk(f_rsvd(cap[0]), 3'd0,         "fill rsvd");
        chk(f_addr(cap[0]), {9'd0, fill_addr}, "fill addr");
        chk(f_flag(cap[0]), 8'h40,        "fill flags STREAM");
        chk(f_cnt(cap[0]),  8'd1,         "fill count");
        chk(f_txn(cap[0]),  8'd0,         "fill txn");

        // the response releases it, and the tag rotates
        rx_txn     = 8'd0;
        rx_data    = {8{32'hA5A5_0001}};
        rx_rd_resp = 1'b1;
        @(posedge clk);
        rx_rd_resp = 1'b0;
        @(posedge clk);
        chk(resp_data, {8{32'hA5A5_0001}}, "fill resp data");

        clear;
        fill_addr  = 31'h0000_2000;
        fill_valid = 1'b1;
        @(posedge clk);
        while (!fill_ready) begin
            @(posedge clk);
        end
        fill_valid = 1'b0;
        wait_flits(1);
        chk(f_txn(cap[0]), 8'd1, "fill txn rotated");
        rx_txn = 8'd1; rx_rd_resp = 1'b1; @(posedge clk); rx_rd_resp = 1'b0;
        @(posedge clk);

        // ---- 2. a writeback is a descriptor and its data, adjacent ----------
        clear;
        wb_addr  = 31'h0000_4000;
        wb_data  = {8{32'hDEAD_0002}};
        wb_valid = 1'b1;
        @(posedge clk);
        while (!wb_ready) begin
            @(posedge clk);
        end
        wb_valid = 1'b0;
        wait_flits(2);
        chk(f_ty(cap[0]),   T_MEM_WR_REQ,  "wb desc type");
        chk(f_txn(cap[0]),  8'h20,         "wb desc txn");
        chk(f_last(cap[0]), 1'b0,          "wb desc last");
        chk(f_addr(cap[0]), {9'd0, wb_addr}, "wb desc addr");
        chk(f_ty(cap[1]),   T_MEM_WR_DATA, "wb data type");
        chk(f_last(cap[1]), 1'b1,          "wb data last");
        chk(cap[1][255:0],  {8{32'hDEAD_0002}}, "wb data payload");
        chk(wr_out,         16'd1,         "wr_out after wb");
        rx_wr_ack = 1'b1; @(posedge clk); rx_wr_ack = 1'b0; @(posedge clk);
        chk(wr_out, 16'd0, "wr_out after ack");

        // ---- 3. a peer push is CU_DATA, descriptor then word ----------------
        clear;
        push_dx = 4'd3; push_dy = 4'd1; push_win = 1'b0;
        push_gran = 14'd9; push_sel = 3'd5; push_be = 4'hF;
        push_data = 32'hCAFE_0003;
        push_valid = 1'b1;
        @(posedge clk);
        while (!push_ready) begin
            @(posedge clk);
        end
        push_valid = 1'b0;
        wait_flits(2);
        chk(f_ty(cap[0]),   T_CU_DATA, "push desc type");
        chk(f_dx(cap[0]),   4'd3,      "push dst x");
        chk(f_dy(cap[0]),   4'd1,      "push dst y");
        chk(f_buf(cap[0]),  8'd4,      "push buf spad word");
        chk(f_off(cap[0]),  16'd9,     "push offset");
        chk(f_last(cap[0]), 1'b0,      "push desc last");
        chk(f_last(cap[1]), 1'b1,      "push data last");
        chk(cap[1][38:0],   {3'd5, 4'hF, 32'hCAFE_0003}, "push data word");

        // the instruction window takes the other buf id
        clear;
        push_win = 1'b1; push_gran = 14'd2; push_valid = 1'b1;
        @(posedge clk);
        while (!push_ready) begin
            @(posedge clk);
        end
        push_valid = 1'b0;
        wait_flits(2);
        chk(f_buf(cap[0]), 8'd5, "push buf imem word");

        // ---- 4. CU_INST: one flit, fields where a CU reads them -------------
        clear;
        disp_dx = 4'd1; disp_dy = 4'd3; disp_txn = 8'h5A; disp_last = 1'b1;
        disp_rsvd = 3'd0;
        disp_op = 8'd1; disp_pc = 32'h0000_0040; disp_arg = 32'h1234_5678;
        disp_valid = 1'b1;
        @(posedge clk);
        while (!disp_ready) begin
            @(posedge clk);
        end
        disp_valid = 1'b0;
        wait_flits(1);
        chk(f_ty(cap[0]),   T_CU_INST,       "inst type");
        chk(f_dx(cap[0]),   4'd1,            "inst dst x");
        chk(f_dy(cap[0]),   4'd3,            "inst dst y");
        chk(f_sx(cap[0]),   4'd2,            "inst src x");
        chk(f_sy(cap[0]),   4'd2,            "inst src y");
        chk(f_txn(cap[0]),  8'h5A,           "inst txn");
        chk(f_last(cap[0]), 1'b1,            "inst last");
        chk(f_rsvd(cap[0]), 3'd0,            "inst rsvd local");
        chk(f_iop(cap[0]),  8'd1,            "inst op");
        chk(f_ipc(cap[0]),  32'h0000_0040,   "inst pc");
        chk(f_iarg(cap[0]), 32'h1234_5678,   "inst arg");
        chk(ncap,           1,               "inst is ONE flit");

        // ---- 5. rsvd is drivable: the remote bit and the mesh id ------------
        clear;
        disp_rsvd = 3'b101;        // leaves the mesh, destination mesh 1
        disp_txn  = 8'h27;         // `fin` when remote, not a program id
        disp_valid = 1'b1;
        @(posedge clk);
        while (!disp_ready) begin
            @(posedge clk);
        end
        disp_valid = 1'b0;
        wait_flits(1);
        chk(f_rsvd(cap[0]), 3'b101, "inst rsvd remote");
        chk(f_txn(cap[0]),  8'h27,  "inst txn carries fin when remote");

        // ---- 6. CU_SIGNAL completions queue, and drain in order -------------
        chk(sig_cnt, 8'd0, "sig empty at start");
        chk(sig_ovf, 1'b0, "sig no overflow at start");
        rx_sig = 1'b1;
        rx_sig_id = 8'h11; rx_sig_code = 8'h01; rx_sig_arg = 32'hAAAA_0001;
        @(posedge clk);
        rx_sig_id = 8'h22; rx_sig_code = 8'h04; rx_sig_arg = 32'hBBBB_0002;
        @(posedge clk);
        rx_sig = 1'b0;
        @(posedge clk);
        chk(sig_cnt,  8'd2,          "sig count 2");
        chk(sig_id,   8'h11,         "sig head id");
        chk(sig_code, 8'h01,         "sig head code");
        chk(sig_arg,  32'hAAAA_0001, "sig head arg");
        sig_pop = 1'b1; @(posedge clk); sig_pop = 1'b0; @(posedge clk);
        chk(sig_cnt,  8'd1,          "sig count 1 after pop");
        chk(sig_id,   8'h22,         "sig second id");
        chk(sig_code, 8'h04,         "sig second code");
        chk(sig_arg,  32'hBBBB_0002, "sig second arg");
        sig_pop = 1'b1; @(posedge clk); sig_pop = 1'b0; @(posedge clk);
        chk(sig_cnt,  8'd0,          "sig drained");

        // ---- 7. a dropped completion is DETECTABLE, not silent --------------
        rx_sig = 1'b1;
        rx_sig_code = 8'h00;
        for (w = 0; w < 12; w = w + 1) begin
            rx_sig_id  = w[7:0];
            rx_sig_arg = {24'd0, w[7:0]};
            @(posedge clk);
        end
        rx_sig = 1'b0;
        @(posedge clk);
        chk(sig_ovf, 1'b1, "sig overflow is sticky and visible");

        // ---- 8. idle is false while anything is owed ------------------------
        while (sig_cnt != 8'd0) begin
            sig_pop = 1'b1; @(posedge clk);
        end
        sig_pop = 1'b0;
        @(posedge clk);
        chk(idle, 1'b1, "idle with nothing outstanding");

        if (errors == 0) begin
            $display("PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $finish;
    end

    initial begin
        #200000;
        $display("FAIL -- timeout");
        $finish;
    end

endmodule

`default_nettype wire
