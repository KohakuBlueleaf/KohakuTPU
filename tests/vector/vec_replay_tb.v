// vec_cu replaying compiler-emitted CU_INST payloads (+prog) against a memory
// image (+mem), writing the memory back to +out; scripts/py/vec_replay.py
// builds the inputs and checks the result.

`default_nettype none
`timescale 1ns/1ps

module vec_replay_tb;
    localparam FW = 288;
    localparam PW = 4;
    localparam CX = 3, CY = 3;
    localparam HX = 0, HY = 0;
    localparam MX = 1, MY = 1;
    localparam WORDS = 4096;            // 128 KB of memory, 256-bit lines
    localparam MAXP = 4096;

    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1;
    localparam [3:0] T_MEM_RD_RESP = 4'h2, T_MEM_WR_ACK = 4'h3, T_MEM_WR_DATA = 4'h4;
    localparam [3:0] T_CU_INST = 4'h5, T_CU_SIGNAL = 4'h6;
    localparam [7:0] SIG_FAULT = 8'h04;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg  [FW-1:0] in_data;
    reg           in_valid;
    wire          in_busy;
    wire [FW-1:0] out_data;
    wire          out_valid;
    wire [31:0]   dbg_cycles;
    wire          dbg_fault;

    vec_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X(CX), .POS_Y(CY),
             .MEM_X(MX), .MEM_Y(MY), .INST_DEPTH(512), .MODEL(1),
             .L1_DEPTH(512), .L1_PRIM("block"),
             .RF_PAD(24), .RF_PACK(1)) dut (
        .clk(clk), .resetn(resetn),
        .noc_in_data(in_data), .noc_in_valid(in_valid), .noc_in_busy(in_busy),
        .noc_out_data(out_data), .noc_out_valid(out_valid),
        .noc_out_busy(1'b0),
        .dbg_cycles(dbg_cycles), .dbg_fault(dbg_fault)
    );

    // ------------------------------------------------ memory, as vec_cu_tb.v
    reg [255:0] dram [0:WORDS-1];
    reg [39:0]  rq_addr [0:63];
    reg [7:0]   rq_tag  [0:63];
    reg [7:0]   rq_cnt  [0:63];   // words of a STREAM run (ew = 1 assumed), 1 for a plain read
    reg [7:0]   rq_sub;
    integer     rq_head, rq_tail, rq_wait, ack_owed;
    reg [39:0]  wr_addr_l;
    reg         wr_open;
    reg [8:0]   wr_left;          // beats the open write burst still owes
    reg  [FW-1:0] mem_flit;
    reg           mem_valid;

    wire [3:0]  o_type = out_data[FW-4*PW-1 -: 4];
    wire [7:0]  o_txn  = out_data[FW-4*PW-5 -: 8];
    wire [39:0] o_addr = out_data[255 -: 40];
    wire [7:0]  o_sig  = out_data[255 -: 8];
    wire wd_now = out_valid && (o_type == T_MEM_WR_DATA) && wr_open && (wr_left == 9'd1);
    wire ack_go = (ack_owed > 0) && (rq_head == rq_tail) && !in_busy;

    integer sig_count, faults;
    reg [31:0] fault_arg;

    always @(posedge clk) begin
        if (!resetn) begin
            rq_head <= 0; rq_tail <= 0; rq_wait <= 0; ack_owed <= 0; rq_sub <= 8'd0;
            wr_open <= 1'b0; wr_left <= 9'd0; mem_valid <= 1'b0; sig_count <= 0; faults <= 0;
        end else begin
            // A response the link refuses is HELD, not dropped: a long program
            // keeps the instruction FIFO full and `in_busy` high for whole RUNs.
            if (!(mem_valid && in_busy)) mem_valid <= 1'b0;
            if (out_valid) begin
                case (o_type)
                    T_MEM_RD_REQ: begin
                        rq_addr[rq_tail[5:0]] <= o_addr;
                        rq_tag[rq_tail[5:0]]  <= o_txn;
                        rq_cnt[rq_tail[5:0]]  <= (out_data[206] && (out_data[199 -: 8] != 8'd0))
                                                 ? out_data[199 -: 8] : 8'd1;
                        rq_tail <= rq_tail + 1;
                    end
                    T_MEM_WR_REQ: begin
                        wr_addr_l <= o_addr;
                        wr_open   <= 1'b1;
                        wr_left   <= {1'b0, out_data[215 -: 8]} + 9'd1;
                    end
                    T_MEM_WR_DATA: if (wr_open) begin
                        dram[wr_addr_l[16:5]] <= out_data[255:0];
                        wr_addr_l <= wr_addr_l + 40'd32;
                        wr_left   <= wr_left - 9'd1;
                        if (wr_left == 9'd1) begin
                            wr_open <= 1'b0;
                        end
                    end
                    T_CU_SIGNAL: begin
                        sig_count <= sig_count + 1;
                        if (o_sig == SIG_FAULT) begin
                            faults    <= faults + 1;
                            fault_arg <= out_data[247 -: 32];
                        end
                    end
                    default: ;
                endcase
            end
            if (mem_valid && in_busy) begin
                // the held response goes first
            end else if (rq_head != rq_tail) begin
                if (rq_wait < 3) begin
                    rq_wait <= rq_wait + 1;
                end else begin
                    rq_wait  <= 0;
                    mem_flit <= { CX[3:0], CY[3:0], MX[3:0], MY[3:0],
                                  T_MEM_RD_RESP, rq_tag[rq_head[5:0]] + rq_sub, 1'b1, 3'b000,
                                  dram[rq_addr[rq_head[5:0]][16:5] + {4'd0, rq_sub}] };
                    mem_valid <= 1'b1;
                    if (rq_sub + 8'd1 == rq_cnt[rq_head[5:0]]) begin
                        rq_head <= rq_head + 1;
                        rq_sub  <= 8'd0;
                    end else begin
                        rq_sub  <= rq_sub + 8'd1;
                    end
                end
            end else if (ack_go) begin
                mem_flit  <= { CX[3:0], CY[3:0], MX[3:0], MY[3:0],
                               T_MEM_WR_ACK, 8'h00, 1'b1, 3'b000, 256'd0 };
                mem_valid <= 1'b1;
            end
            ack_owed <= ack_owed + (wd_now ? 1 : 0) - (ack_go ? 1 : 0);
        end
    end

    reg  [FW-1:0] agent_flit;
    reg           agent_valid;

    always @(*) begin
        in_valid = 1'b0;
        in_data  = {FW{1'b0}};
        if (mem_valid) begin
            in_valid = 1'b1;
            in_data  = mem_flit;
        end else if (agent_valid) begin
            in_valid = 1'b1;
            in_data  = agent_flit;
        end
    end

    task send_cu(input [255:0] payload);
        begin
            @(negedge clk);
            while (in_busy || mem_valid) begin
                @(negedge clk);
            end
            agent_flit  = { CX[3:0], CY[3:0], HX[3:0], HY[3:0], T_CU_INST,
                            8'h20, 1'b1, 3'b000, payload };
            agent_valid = 1'b1;
            @(negedge clk);
            agent_valid = 1'b0;
        end
    endtask

    reg [255:0] prog [0:MAXP-1];
    reg [1023:0] f_prog, f_mem, f_out;
    integer n, i, spin;
    integer clk_n = 0, t_go = 0, t_done = 0;
    always @(posedge clk) clk_n <= clk_n + 1;
    // A wedged unit stalls `send_cu` on `in_busy` forever; report where it stopped.
    always @(posedge clk) begin
        if (t_go != 0 && t_done == 0 && clk_n - t_go > 4000000) begin
            $display("@@@ TIMEOUT sent %0d of %0d, completions %0d, faults %0d, pc %0d st %0d",
                     i, n, sig_count, faults, dut.u_core.pc, dut.u_core.st);
            $finish;
        end
    end

    initial begin
        agent_valid = 1'b0;
        for (i = 0; i < MAXP; i = i + 1) prog[i] = {256{1'b1}};
        if (!$value$plusargs("prog=%s", f_prog) || !$value$plusargs("mem=%s", f_mem)
            || !$value$plusargs("out=%s", f_out)) begin
            $display("@@@ need +prog= +mem= +out=");
            $finish;
        end
        $readmemh(f_prog, prog);
        $readmemh(f_mem, dram);
        n = 0;
        while (n < MAXP && prog[n] !== {256{1'b1}}) n = n + 1;
        repeat (10) @(negedge clk);
        resetn = 1'b1;
        repeat (10) @(negedge clk);
        t_go = clk_n;
        for (i = 0; i < n; i = i + 1) send_cu(prog[i]);
        spin = 0;
        while (sig_count < n && spin < 2000000) begin
            @(negedge clk);
            spin = spin + 1;
        end
        t_done = clk_n;
        repeat (200) @(negedge clk);
        $writememh(f_out, dram);
        $display("@@@ REPLAY payloads %0d completions %0d faults %0d fault_arg %0h cycles %0d",
                 n, sig_count, faults, fault_arg, spin);
        // First payload offered to the last completion, in core clocks.
        $display("@@@ ELAPSED %0d", t_done - t_go);
        // The four predicate registers as the lanes hold them after the run.
        for (i = 0; i < 4; i = i + 1)
            $display("@@@ PREG %0d %032h", i, dut.u_core.u_lanes.preg[i]);
        $finish;
    end
endmodule

`default_nettype wire
