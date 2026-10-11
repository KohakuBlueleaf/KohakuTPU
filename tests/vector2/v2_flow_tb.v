// The V2 core's unit-to-unit flow control through vec_cu: a VSYNC on the
// landed-granule count waits for a CU_DATA burst, one on the credit count
// waits for CU_SIGNAL CREDITs (one of them sent before the RUN), and a VPOST
// leaves as a CREDIT after the drain before it. +prog= holds the CU_INST
// payloads (scripts/py/v2_flow.py builds them).

`default_nettype none
`timescale 1ns/1ps

module v2_flow_tb;
    localparam FW = 288;
    localparam PW = 4;
    localparam CX = 3, CY = 3;
    localparam HX = 0, HY = 0;
    localparam MX = 1, MY = 1;
    localparam SX = 5, SY = 6;          // the peer that sends data and credits
    localparam WORDS = 4096;
    localparam MAXP = 256;
    localparam DATA_AT = 2000, CRED_AT = 5000;

    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_RD_RESP = 4'h2, T_MEM_WR_ACK = 4'h3, T_MEM_WR_DATA = 4'h4;
    localparam [3:0] T_CU_INST = 4'h5, T_CU_SIGNAL = 4'h6, T_CU_DATA = 4'h8;
    localparam [7:0] SIG_FAULT = 8'h04, SIG_CREDIT = 8'h05;

    reg clk = 0, resetn = 0;
    always #2 clk = ~clk;

    reg  [FW-1:0] in_data;
    reg           in_valid;
    wire          in_busy;
    wire [FW-1:0] out_data;
    wire          out_valid;
    wire [31:0]   dbg_cycles;
    wire          dbg_fault;

    vec_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(CX), .POS_Y(CY),
             .MEM_X(MX), .MEM_Y(MY), .INST_DEPTH(512), .MODEL(1),
             .L1_DEPTH(512), .L1_PRIM("block"), .RF_PAD(24), .RF_PACK(1)) dut (
        .clk(clk), .unit_clk(1'b0), .resetn(resetn),
        .noc_in_data(in_data), .noc_in_valid(in_valid), .noc_in_busy(in_busy),
        .noc_out_data(out_data), .noc_out_valid(out_valid), .noc_out_busy(1'b0),
        .dbg_cycles(dbg_cycles), .dbg_fault(dbg_fault)
    );

    integer clk_n = 0;
    always @(posedge clk) clk_n <= clk_n + 1;

    // ------------------------------------------------ memory: writes only
    reg [255:0] dram [0:WORDS-1];
    reg [39:0]  wr_addr_l;
    reg         wr_open;
    reg [8:0]   wr_left;
    integer     ack_owed;
    reg  [FW-1:0] mem_flit;
    reg           mem_valid;

    wire [3:0]  o_type = out_data[FW-4*PW-1 -: 4];
    wire [39:0] o_addr = out_data[255 -: 40];
    wire [7:0]  o_sig  = out_data[255 -: 8];
    wire [3:0]  o_dx   = out_data[FW-1 -: 4];
    wire [3:0]  o_dy   = out_data[FW-5 -: 4];
    wire wd_now = out_valid && (o_type == T_MEM_WR_DATA) && wr_open && (wr_left == 9'd1);
    wire ack_go = (ack_owed > 0) && !in_busy;

    integer completions = 0, faults = 0, posts = 0, errors = 0;
    integer t_wr1 = 0, t_wr2 = 0, t_post = 0;
    reg [31:0] post_arg;

    always @(posedge clk) begin
        if (!resetn) begin
            ack_owed <= 0; wr_open <= 1'b0; wr_left <= 9'd0; mem_valid <= 1'b0;
        end else begin
            if (!(mem_valid && in_busy)) mem_valid <= 1'b0;
            if (out_valid) begin
                case (o_type)
                    T_MEM_RD_REQ: begin
                        $display("ERROR v2_flow_tb: unexpected read request");
                        errors <= errors + 1;
                    end
                    T_MEM_WR_REQ: begin
                        wr_addr_l <= o_addr;
                        wr_open   <= 1'b1;
                        wr_left   <= {1'b0, out_data[215 -: 8]} + 9'd1;
                        if (o_addr == 40'h1000 && t_wr1 == 0) t_wr1 <= clk_n;
                        if (o_addr == 40'h2000 && t_wr2 == 0) t_wr2 <= clk_n;
                    end
                    T_MEM_WR_DATA: if (wr_open) begin
                        dram[wr_addr_l[16:5]] <= out_data[255:0];
                        wr_addr_l <= wr_addr_l + 40'd32;
                        wr_left   <= wr_left - 9'd1;
                        if (wr_left == 9'd1) wr_open <= 1'b0;
                    end
                    T_CU_SIGNAL: begin
                        if (o_sig == SIG_CREDIT) begin
                            posts    <= posts + 1;
                            post_arg <= out_data[247 -: 32];
                            t_post   <= clk_n;
                            if (o_dx != SX[3:0] || o_dy != SY[3:0]) begin
                                $display("ERROR v2_flow_tb: CREDIT sent to (%0d,%0d)", o_dx, o_dy);
                                errors <= errors + 1;
                            end
                        end else begin
                            completions <= completions + 1;
                            if (o_sig == SIG_FAULT) faults <= faults + 1;
                        end
                    end
                    default: ;
                endcase
            end
            if (!(mem_valid && in_busy) && ack_go) begin
                mem_flit  <= { CX[3:0], CY[3:0], MX[3:0], MY[3:0],
                               T_MEM_WR_ACK, 8'h00, 1'b1, 3'b000, 256'd0 };
                mem_valid <= 1'b1;
            end
            ack_owed <= ack_owed + (wd_now ? 1 : 0)
                      - ((!(mem_valid && in_busy) && ack_go) ? 1 : 0);
        end
    end

    // ------------------------------------------------ agent and peer flits
    reg  [FW-1:0] agent_flit;
    reg           agent_valid;
    always @(*) begin
        in_valid = mem_valid || agent_valid;
        in_data  = mem_valid ? mem_flit : agent_flit;
    end

    task put(input [FW-1:0] f);
        begin
            @(negedge clk);
            while (in_busy || mem_valid) @(negedge clk);
            agent_flit  = f;
            agent_valid = 1'b1;
            @(negedge clk);
            agent_valid = 1'b0;
        end
    endtask

    task send_cu(input [255:0] payload);
        put({CX[3:0], CY[3:0], HX[3:0], HY[3:0], T_CU_INST, 8'h20, 1'b0, 3'b000, payload});
    endtask

    task credit(input [15:0] n);
        put({CX[3:0], CY[3:0], SX[3:0], SY[3:0], T_CU_SIGNAL, 8'h00, 1'b1, 3'b000,
             SIG_CREDIT, 16'd0, n, 216'd0});
    endtask

    function [255:0] pattern(input integer w);
        pattern = {8{w[15:0], 16'hA5C3}};
    endfunction

    // 8 granules into L1 words `at`.., one burst; flags[1] COUNT when `count`.
    task burst(input [15:0] at, input count);
        integer w;
        begin
            put({CX[3:0], CY[3:0], SX[3:0], SY[3:0], T_CU_DATA, 8'h00, 1'b0, 3'b000,
                 8'd0, at, 8'd7, {6'd0, count, 1'b0}, 4'd0, 4'd0, 208'd0});
            for (w = 0; w < 8; w = w + 1)
                put({CX[3:0], CY[3:0], SX[3:0], SY[3:0], T_CU_DATA, 8'h00, (w == 7), 3'b000,
                     pattern(w)});
        end
    endtask

    reg [255:0] prog [0:MAXP-1];
    reg [1023:0] f_prog;
    integer n, i, t_run, t_data, t_cred, spin;

    initial begin
        agent_valid = 1'b0;
        for (i = 0; i < WORDS; i = i + 1) dram[i] = 256'd0;
        for (i = 0; i < MAXP; i = i + 1) prog[i] = {256{1'b1}};
        if (!$value$plusargs("prog=%s", f_prog)) begin
            $display("@@@ need +prog=");
            $finish;
        end
        $readmemh(f_prog, prog);
        n = 0;
        while (n < MAXP && prog[n] !== {256{1'b1}}) n = n + 1;
        repeat (10) @(negedge clk);
        resetn = 1'b1;
        repeat (10) @(negedge clk);
        // Three credits before the RUN: the counter must keep them.
        credit(16'd3);
        for (i = 0; i < n; i = i + 1) send_cu(prog[i]);
        t_run = clk_n;
        // An uncounted burst elsewhere first: it must not release the wait.
        while (clk_n - t_run < DATA_AT / 2) @(negedge clk);
        burst(16'd300, 1'b0);
        while (clk_n - t_run < DATA_AT) @(negedge clk);
        t_data = clk_n;
        burst(16'd256, 1'b1);
        while (clk_n - t_run < CRED_AT) @(negedge clk);
        t_cred = clk_n;
        credit(16'd1);
        spin = 0;
        while (completions < n && spin < 200000) begin
            @(negedge clk);
            spin = spin + 1;
        end
        repeat (100) @(negedge clk);

        if (completions != n || faults != 0) begin
            $display("ERROR v2_flow_tb: %0d of %0d completions, %0d faults", completions, n, faults);
            errors = errors + 1;
        end
        for (i = 0; i < 8; i = i + 1) begin
            if (dram[(40'h1000 >> 5) + i] !== pattern(i)) begin
                $display("ERROR v2_flow_tb: drained word %0d is %h, landed %h", i,
                         dram[(40'h1000 >> 5) + i], pattern(i));
                errors = errors + 1;
            end
        end
        if (t_wr1 <= t_data) begin
            $display("ERROR v2_flow_tb: first drain at %0d, before the burst at %0d", t_wr1, t_data);
            errors = errors + 1;
        end
        if (t_wr2 <= t_cred) begin
            $display("ERROR v2_flow_tb: credit-gated drain at %0d, before the 4th credit at %0d",
                     t_wr2, t_cred);
            errors = errors + 1;
        end
        if (posts != 1 || post_arg != 32'd300 || t_post <= t_wr2) begin
            $display("ERROR v2_flow_tb: %0d CREDITs, arg %0d at %0d (drain at %0d)",
                     posts, post_arg, t_post, t_wr2);
            errors = errors + 1;
        end
        $display("@@@ burst %0d drain %0d credit %0d drain %0d post %0d", t_data, t_wr1,
                 t_cred, t_wr2, t_post);
        if (errors == 0) $display("  PASS -- v2_flow_tb");
        else $display("  FAIL -- v2_flow_tb, %0d errors", errors);
        $finish;
    end
endmodule

`default_nettype wire
