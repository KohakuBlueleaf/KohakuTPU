// Bench for the saxpy example: a memory model serving plain reads and burst
// writes, and an agent driving the sw/isa.py encoding — discovery, a run with
// a partial tail line, n=0, a fault, a full 8-beat batch run, then CU_DBG.
// Values are whole-valued float32, the domain the datapath is exact on.
`timescale 1ns / 1ps
`default_nettype none

module saxpy_cu_tb;
    localparam FW = 288;
    localparam PW = 4;
    localparam CX = 2, CY = 2;   // the CU
    localparam MX = 0, MY = 1;   // the memory agent port
    localparam HX = 0, HY = 0;   // us

    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_RD_RESP = 4'h2, T_MEM_WR_ACK = 4'h3;
    localparam [3:0] T_MEM_WR_DATA = 4'h4, T_CU_INST = 4'h5, T_CU_SIGNAL = 4'h6;
    localparam [3:0] T_CU_CTRL = 4'h7;
    localparam [7:0] SIG_INST = 8'h00, SIG_BATCH = 8'h01, SIG_FAULT = 8'h04;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg  [FW-1:0] in_data;
    reg           in_valid = 0;
    wire          in_busy;
    wire [FW-1:0] out_data;
    wire          out_valid;
    reg           out_busy = 0;
    wire          cu_busy;

    saxpy_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(CX), .POS_Y(CY),
               .MEM_X(MX), .MEM_Y(MY), .INST_DEPTH(32)) dut (
        .clk(clk), .resetn(resetn),
        .noc_in_data(in_data), .noc_in_valid(in_valid), .noc_in_busy(in_busy),
        .noc_out_data(out_data), .noc_out_valid(out_valid),
        .noc_out_busy(out_busy), .busy(cu_busy)
    );

    kh_port_check #(.FLIT_WIDTH(FW), .NAME("saxpy"), .MAX_BUSY(2000)) u_check (
        .clk(clk), .rst(!resetn),
        .in_data(in_data), .in_valid(in_valid), .in_busy(in_busy),
        .out_data(out_data), .out_valid(out_valid), .out_busy(out_busy)
    );

    // exact whole-value float32; the reference the datapath must hit bit-for-bit
    function [31:0] i2f(input signed [31:0] v);
        reg [31:0] mag, sh;
        reg [7:0]  e;
        integer    i, p;
        begin
            mag = v[31] ? -v : v;
            p = 0;
            for (i = 0; i < 24; i = i + 1) begin
                if (mag[i]) begin
                    p = i;
                end
            end
            e  = 8'd127 + p;
            sh = mag << (23 - p);
            i2f = (mag == 32'd0) ? 32'd0 : {v[31], e, sh[22:0]};
        end
    endfunction

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

    // random outbound backpressure the whole run, updated at posedge
    always @(posedge clk) begin
        out_busy <= ($random % 4 == 0);
    end

    // =============================================== the memory + DRAM model
    reg [255:0] dram [0:1023];

    // accepted outbound flits
    wire [3:0]  o_ty   = out_data[FW-4*PW-1 -: 4];
    wire [7:0]  o_txn  = out_data[FW-4*PW-5 -: 8];
    wire [39:0] o_addr = out_data[255 -: 40];
    wire [7:0]  o_len  = out_data[215 -: 8];

    // read-request ring and one write slot
    reg [39:0] mq_addr [0:15];
    reg [7:0]  mq_len  [0:15], mq_tag [0:15];
    reg [4:0]  mq_h, mq_t;
    reg [39:0] wr_addr;
    reg [7:0]  wr_left;
    reg        wr_open;

    // inbound-to-CU response ring, drained by the negedge driver below
    reg [FW-1:0] rq_q [0:31];
    reg [5:0]    rq_h = 0, rq_t = 0;

    reg        rserv;
    reg [39:0] r_addr;
    reg [7:0]  r_len, r_tag, r_beat;
    reg [1:0]  gap;

    always @(posedge clk) begin
        if (!resetn) begin
            mq_h <= 0; mq_t <= 0; rq_t <= 0;
            wr_open <= 0; rserv <= 0;
        end else begin
            if (out_valid && !out_busy) begin
                case (o_ty)
                    T_MEM_RD_REQ: begin
                        mq_addr[mq_t[3:0]] <= o_addr;
                        mq_len[mq_t[3:0]]  <= o_len;
                        mq_tag[mq_t[3:0]]  <= o_txn;
                        mq_t <= mq_t + 5'd1;
                    end
                    T_MEM_WR_REQ: begin
                        wr_addr <= o_addr;
                        wr_left <= o_len + 8'd1;
                        wr_open <= 1'b1;
                    end
                    T_MEM_WR_DATA: if (wr_open) begin
                        dram[wr_addr[14:5]] <= out_data[255:0];
                        wr_addr <= wr_addr + 40'd32;
                        wr_left <= wr_left - 8'd1;
                        if (wr_left == 8'd1) begin
                            wr_open <= 1'b0;
                            // the ack the CU must drop (memory-protocol.md s5)
                            rq_q[rq_t[4:0]] <= {CX[PW-1:0], CY[PW-1:0],
                                                MX[PW-1:0], MY[PW-1:0],
                                                T_MEM_WR_ACK, o_txn, 1'b1, 3'b000,
                                                256'd0};
                            rq_t <= rq_t + 6'd1;
                        end
                    end
                    default: ;
                endcase
            end

            // serve one read at a time, a couple of idle cycles between beats
            if (!rserv) begin
                if (mq_h != mq_t) begin
                    r_addr <= mq_addr[mq_h[3:0]];
                    r_len  <= mq_len[mq_h[3:0]];
                    r_tag  <= mq_tag[mq_h[3:0]];
                    mq_h   <= mq_h + 5'd1;
                    r_beat <= 8'd0;
                    gap    <= 2'd2;
                    rserv  <= 1'b1;
                end
            end else if (gap != 2'd0) begin
                gap <= gap - 2'd1;
            end
            else begin
                rq_q[rq_t[4:0]] <= {CX[PW-1:0], CY[PW-1:0],
                                    MX[PW-1:0], MY[PW-1:0],
                                    T_MEM_RD_RESP, r_tag, (r_beat == r_len),
                                    3'b000, dram[r_addr[14:5] + r_beat[3:0]]};
                rq_t <= rq_t + 6'd1;
                if (r_beat == r_len) begin
                    rserv <= 1'b0;
                end
                else begin r_beat <= r_beat + 8'd1; gap <= 2'd1; end
            end
        end
    end

    // ====================================== single inbound driver, negedge
    // One owner for in_valid/in_data: responses first, then the agent ring.
    // Hold-until-taken: busy sampled at a negedge gates the NEXT posedge.
    reg [FW-1:0] aq_q [0:15];
    reg [4:0]    aq_h = 0, aq_t = 0;   // aq_t is task-written: an X here sticks

    reg drv_v = 0, drv_took = 0;
    always @(negedge clk) begin
        if (!resetn) begin
            in_valid = 0; drv_v = 0; drv_took = 0; rq_h = 0; aq_h = 0;
        end else begin
            if (drv_v && drv_took) begin in_valid = 0; drv_v = 0; end
            if (!drv_v) begin
                if (rq_h != rq_t) begin
                    in_data = rq_q[rq_h[4:0]]; rq_h = rq_h + 6'd1;
                    in_valid = 1; drv_v = 1;
                end else if (aq_h != aq_t) begin
                    in_data = aq_q[aq_h[3:0]]; aq_h = aq_h + 5'd1;
                    in_valid = 1; drv_v = 1;
                end
            end
            drv_took = drv_v && !in_busy;
        end
    end

    // agent tasks enqueue at posedge, so they never race the negedge driver
    task put(input [FW-1:0] f);
        begin
            @(posedge clk);
            aq_q[aq_t[3:0]] = f;
            aq_t = aq_t + 5'd1;
        end
    endtask

    function [FW-1:0] hdr(input [3:0] ty, input [7:0] id, input last,
                          input [255:0] payload);
        hdr = {CX[PW-1:0], CY[PW-1:0], HX[PW-1:0], HY[PW-1:0],
               ty, id, last, 3'b000, payload};
    endfunction

    // the sw/isa.py encoding: op, n, a, x_addr, y_addr, byte-aligned
    function [255:0] saxpy_inst(input [23:0] n, input [31:0] a_bits,
                                input [63:0] xa, input [63:0] ya);
        saxpy_inst = {8'h01, n, a_bits, xa, ya, 64'd0};
    endfunction

    // ====================================================== outbound monitor
    integer n_sig, n_ctrl;
    reg [7:0]  sig_code [0:15];
    reg [31:0] sig_arg  [0:15];
    reg [63:0] ctrl_val;

    always @(posedge clk) begin
        if (!resetn) begin
            n_sig <= 0; n_ctrl <= 0;
        end else if (out_valid && !out_busy) begin
            if (o_ty == T_CU_SIGNAL) begin
                sig_code[n_sig] <= out_data[255 -: 8];
                sig_arg[n_sig]  <= out_data[247 -: 32];
                n_sig <= n_sig + 1;
            end else if (o_ty == T_CU_CTRL) begin
                ctrl_val <= out_data[239 -: 64];
                n_ctrl <= n_ctrl + 1;
            end
        end
    end

    integer spin;
    task wait_sigs(input integer want);
        begin
            spin = 0;
            while (n_sig < want && spin < 8000) begin
                spin = spin + 1;
                @(negedge clk);
            end
            chk(n_sig, want, "expected completions arrived");
        end
    endtask
    task wait_ctrl(input integer want);
        begin
            spin = 0;
            while (n_ctrl < want && spin < 500) begin
                spin = spin + 1;
                @(negedge clk);
            end
            chk(n_ctrl, want, "expected ctrl replies arrived");
        end
    endtask

    integer i, l, s;
    reg [255:0] line;
    initial begin
        repeat (6) @(negedge clk);
        resetn = 1;
        repeat (2) @(negedge clk);

        // 1. discovery: caps must publish the type the driver registered
        put(hdr(T_CU_CTRL, 8'h11, 1'b0, {8'd0, 8'd0, 240'd0}));
        wait_ctrl(1);
        chk(ctrl_val[63:48], 16'h5358, "caps CU_TYPE is 'SX'");
        chk(ctrl_val[47:40], 8'h01, "caps CU_VERSION");
        chk(ctrl_val[35:20], 16'd32, "caps INST_DEPTH");

        // 2. n=23, a=3.0: a partial tail line, x at 0x400, y at 0x800
        for (l = 0; l < 3; l = l + 1) begin
            line = 256'd0;
            for (s = 0; s < 8; s = s + 1) begin
                if (l*8 + s < 23) begin
                    line[s*32 +: 32] = i2f(l*8 + s - 13);
                end
            end
            dram[32 + l] = line;
        end
        for (l = 0; l < 3; l = l + 1) begin
            line = 256'd0;
            for (s = 0; s < 8; s = s + 1) begin
                if (l*8 + s < 23) begin
                    line[s*32 +: 32] = i2f(100 - 7*(l*8 + s));
                end
                else begin
                    line[s*32 +: 32] = 32'hDEAD_BEEF;
                end
            end
            dram[64 + l] = line;
        end
        put(hdr(T_CU_INST, 8'h31, 1'b0,
                saxpy_inst(24'd23, i2f(3), 64'h400, 64'h800)));
        wait_sigs(1);
        chk(sig_code[0], SIG_INST, "run1 ordinary completion");
        chk(sig_arg[0], 32'd23, "run1 reports n elements");
        for (i = 0; i < 23; i = i + 1) begin
            line = dram[64 + i/8];
            if (line[(i%8)*32 +: 32] !== i2f(3*(i-13) + (100 - 7*i))) begin
                chk(line[(i%8)*32 +: 32], i2f(3*(i-13) + (100 - 7*i)), "run1 y");
            end
        end
        line = dram[66];
        chk(line[7*32 +: 32], 32'hDEAD_BEEF, "tail of a partial line preserved");

        // 3. n=0 completes without touching memory (sw model does the same)
        put(hdr(T_CU_INST, 8'h32, 1'b0, saxpy_inst(24'd0, i2f(3), 64'h0, 64'h0)));
        wait_sigs(2);
        chk(sig_code[1], SIG_INST, "n=0 ordinary completion");
        chk(sig_arg[1], 32'd0, "n=0 reports zero elements");

        // 4. a wrong opcode is a fault, not a hang
        put(hdr(T_CU_INST, 8'h33, 1'b0, {8'h7F, 248'd0}));
        wait_sigs(3);
        chk(sig_code[2], SIG_FAULT, "bad opcode faults");
        chk(sig_arg[2], 32'hBAD0_0001, "fault carries the code");

        // 5. n=64: the full 8-beat write burst, last-marked -> batch report
        for (l = 0; l < 8; l = l + 1) begin
            line = 256'd0;
            for (s = 0; s < 8; s = s + 1) begin
                line[s*32 +: 32] = i2f(l*8 + s);
            end
            dram[128 + l] = line;
        end
        for (l = 0; l < 8; l = l + 1) begin
            line = 256'd0;
            for (s = 0; s < 8; s = s + 1) begin
                line[s*32 +: 32] = i2f(1000 + l*8 + s);
            end
            dram[256 + l] = line;
        end
        put(hdr(T_CU_INST, 8'h5A, 1'b1,
                saxpy_inst(24'd64, 32'hC000_0000 /* -2.0 */, 64'h1000, 64'h2000)));
        wait_sigs(4);
        chk(sig_code[3], SIG_BATCH, "last instruction retires as batch");
        chk(sig_arg[3], 32'h5A, "batch report carries the program id");
        for (i = 0; i < 64; i = i + 1) begin
            line = dram[256 + i/8];
            if (line[(i%8)*32 +: 32] !== i2f(1000 - i)) begin
                chk(line[(i%8)*32 +: 32], i2f(1000 - i), "run2 y");
            end
        end

        // 6. CU_DBG: cumulative elements in the low half (sw decode_dbg)
        put(hdr(T_CU_CTRL, 8'h12, 1'b0, {8'd0, 8'd3, 240'd0}));
        wait_ctrl(2);
        chk(ctrl_val[31:0], 32'd87, "dbg counts 23+64 elements");

        spin = 0;
        while (cu_busy && spin < 200) begin spin = spin + 1; @(negedge clk); end
        chk(cu_busy, 1'b0, "unit drains to idle");

        u_check.report;
        if (errors == 0 && u_check.violations == 0) begin
            $display("PASS saxpy_cu_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL saxpy_cu_tb: %0d errors", errors);
        end
        $finish;
    end
endmodule

`default_nettype wire
