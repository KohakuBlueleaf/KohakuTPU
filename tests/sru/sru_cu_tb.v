// sru_cu through its mesh port (scripts/py/sru/cu_vectors.py): the bench is
// the memory agent (STREAM reads, write bursts, acks), a CU_DATA producer
// that spends the credits the SRU posts, and a peer consumer that posts
// credits back. Instructions go one at a time, each waited for; then the
// written memory and the peer stream are checked word by word.

`default_nettype none
`timescale 1ns/1ps

`ifndef SRU_DIR
  `define SRU_DIR "build/sru"
`endif

module sru_cu_tb;
    localparam integer FW = 288, NMAX = 8192;
    localparam [3:0] T_RD_REQ = 4'h0, T_WR_REQ = 4'h1, T_RD_RESP = 4'h2, T_WR_ACK = 4'h3;
    localparam [3:0] T_WR_DATA = 4'h4, T_INST = 4'h5, T_SIG = 4'h6, T_CU_DATA = 4'h8;
    localparam [3:0] SX = 4'd3, SY = 4'd3, BX = 4'd1, BY = 4'd1;

    reg clk = 1'b0, resetn = 1'b0;
    always #2 clk = ~clk;

    // ---- the command file ----
    reg [3:0]   cop [0:NMAX-1];
    reg [31:0]  cad [0:NMAX-1];
    reg [255:0] cda [0:NMAX-1];
    integer n, fd, got;
    reg [3:0]   t_op;
    reg [31:0]  t_ad;
    reg [255:0] t_da;
    reg [255:0] mem [0:262143];         // 8 MB of 32-byte words

    initial begin
        fd = $fopen($sformatf("%s/cu.cmd", `SRU_DIR), "r");
        if (fd == 0) begin
            $display("FAIL sru_cu: no command file -- run scripts/py/sru/cu_vectors.py");
            $finish;
        end
        n = 0;
        got = 3;
        while (got == 3 && n < NMAX) begin
            got = $fscanf(fd, "%h %h %h\n", t_op, t_ad, t_da);
            if (got == 3) begin
                cop[n] = t_op; cad[n] = t_ad; cda[n] = t_da;
                if (t_op == 4'h0) mem[t_ad[17:0]] = t_da;
                n = n + 1;
            end
        end
        $fclose(fd);
    end

    // ---- DUT ----
    reg  [FW-1:0] in_d;
    reg           in_v;
    wire          in_busy;
    wire [FW-1:0] out_d;
    wire          out_v;
    reg           out_busy;

    // An 8-word input FIFO: the walkers' runs are cut by room, not only by 32.
    sru_cu #(.POS_X(SX), .POS_Y(SY), .MEM_X(BX), .MEM_Y(BY), .MODEL(`MX_MODEL),
             .FIFO_DEPTH(8)) dut (
        .clk(clk), .resetn(resetn),
        .noc_in_data(in_d), .noc_in_valid(in_v), .noc_in_busy(in_busy),
        .noc_out_data(out_d), .noc_out_valid(out_v), .noc_out_busy(out_busy));

    function [FW-1:0] flit;
        input [3:0]   ty;
        input [7:0]   id;
        input         last;
        input [1:0]   rsv;
        input [255:0] pay;
        flit = {SX, SY, BX, BY, ty, id, last, 1'b0, rsv, pay};
    endfunction

    // ---- the bench's send queue (to the DUT) ----
    reg [FW-1:0] sq [0:16383];
    integer sq_w, sq_r;
    integer granted;                    // consumer credits the DUT has taken in
    initial granted = 0;
    always @(posedge clk) begin
        if (!resetn) begin
            in_v <= 1'b0;
        end else begin
            if (in_v && !in_busy) begin
                in_v <= 1'b0;
                if (in_d[FW-17 -: 4] == T_SIG && in_d[255:248] == 8'h05) granted = granted + in_d[231:216];
            end
            if ((!in_v || !in_busy) && sq_r < sq_w) begin
                in_d <= sq[sq_r % 16384];
                in_v <= 1'b1;
                sq_r = sq_r + 1;
            end
        end
    end

    // ---- what the DUT sends ----
    integer i, wr_left, done_cnt, peer_n, peer_err, credit_p, cons_got, cred_err;
    integer cyc, post_due, post_owed;
    initial begin
        cyc = 0; post_due = 0; post_owed = 0;
    end
    always @(posedge clk) begin
        cyc = cyc + 1;
        if (post_owed > 0 && cyc >= post_due) begin
            sq[sq_w % 16384] = flit(T_SIG, 8'h00, 1'b1, 2'd0, {8'h05, 16'd0, 16'd8, 216'd0});
            sq_w = sq_w + 1;
            post_owed = post_owed - 1;
        end
    end
    reg [17:0] wr_ptr;
    reg [255:0] peer_got [0:4095];
    reg [3:0] oty;
    // Read runs: inside a sub-row an A or B run is at least RUN_MIN words
    // (FIFO_DEPTH 8: 4), or the sub-row's rest. NW from the CFG sent, the
    // position in the sub-row from 0 at each RUN.
    localparam integer RUN_MIN = 4;
    integer tb_nw, pos_a, pos_b, rq_n, rq_need, run_err;
    initial begin
        tb_nw = 1; pos_a = 0; pos_b = 0; run_err = 0;
    end
    always @(posedge clk) begin
        out_busy <= ($random & 7) == 0;
        if (resetn && out_v && !out_busy) begin
            oty = out_d[FW-17 -: 4];
            case (oty)
                T_RD_REQ: begin
                    rq_n = (out_d[199:192] == 8'd0) ? 1 : out_d[199:192];
                    if (out_d[FW-21 -: 2] != 2'd2) begin
                        rq_need = tb_nw - (out_d[FW-22] ? pos_b : pos_a);
                        if (rq_need > RUN_MIN) rq_need = RUN_MIN;
                        if (rq_n < rq_need) begin
                            $display("  ERROR a %0d-word run where %0d are due", rq_n, rq_need);
                            run_err = run_err + 1;
                        end
                        if (out_d[FW-22]) pos_b = (pos_b + rq_n) % tb_nw;
                        else pos_a = (pos_a + rq_n) % tb_nw;
                    end
                    // STREAM, 1-word entries: answer `count` words in order
                    for (i = 0; i < ((out_d[199:192] == 8'd0) ? 1 : out_d[199:192]); i = i + 1) begin
                        sq[sq_w % 16384] = flit(T_RD_RESP, out_d[FW-21 -: 8] + i, 1'b1, 2'd0,
                                               mem[(out_d[255:216] >> 5) + i]);
                        sq_w = sq_w + 1;
                    end
                end
                T_WR_REQ: begin
                    wr_ptr  = out_d[255:216] >> 5;
                    wr_left = out_d[215:208] + 1;
                end
                T_WR_DATA: begin
                    mem[wr_ptr] = out_d[255:0];
                    wr_ptr = wr_ptr + 1;
                    wr_left = wr_left - 1;
                    if (wr_left == 0) begin
                        sq[sq_w % 16384] = flit(T_WR_ACK, 8'h01, 1'b1, 2'd0, 256'd0);
                        sq_w = sq_w + 1;
                    end
                end
                T_SIG: begin
                    if (out_d[255:248] == 8'h05) begin
                        credit_p = credit_p + out_d[231:216];       // the producer's credits
                        if (out_d[231:216] == 16'd0) begin
                            $display("  ERROR an empty CREDIT");
                            cred_err = cred_err + 1;
                        end
                    end else begin
                        done_cnt = done_cnt + 1;                    // a completion
                    end
                end
                T_CU_DATA: begin
                    // a peer burst: a descriptor (cons_got counts data only)
                    if (out_d[FW-29] == 1'b0 && cons_got == 0) begin
                        cons_got = out_d[231:224] + 1;
                    end else begin
                        peer_got[peer_n] = out_d[255:0];
                        peer_n = peer_n + 1;
                        cons_got = cons_got - 1;
                        // never more than the credits the DUT has been handed
                        if (peer_n > granted) begin
                            $display("  ERROR peer word %0d sent past the consumer's credits", peer_n);
                            cred_err = cred_err + 1;
                        end
                        if (peer_n % 8 == 0) begin
                            // the consumer frees 8 granules, slowly: a sender
                            // that does not wait for them runs ahead of them
                            post_due = cyc + 2000;
                            post_owed = post_owed + 1;
                        end
                    end
                end
                default: ;
            endcase
        end
    end

    // ---- the producer: CU_DATA bursts of up to 8 words on credits ----
    reg [255:0] pq [0:4095];
    integer pq_n, pq_i, b;

    // ---- run the program ----
    integer pc, errs, want_done, k;
    initial begin
        sq_w = 0; sq_r = 0; done_cnt = 0; peer_n = 0; credit_p = 0; cons_got = 0; cred_err = 0;
        errs = 0; pq_n = 0; pq_i = 0;
        repeat (10) @(posedge clk);
        resetn = 1'b1;
        repeat (60) @(posedge clk);
        // the consumer's buffer: 8 granules to start, so the stream waits on credits
        sq[sq_w % 16384] = flit(T_SIG, 8'h00, 1'b1, 2'd0, {8'h05, 16'd0, 16'd8, 216'd0});
        sq_w = sq_w + 1;
        want_done = 0;
        for (pc = 0; pc < n; pc = pc + 1) begin
            if (cop[pc] == 4'h2) begin
                pq[pq_n] = cda[pc];
                pq_n = pq_n + 1;
            end else if (cop[pc] == 4'h1) begin
                sq[sq_w % 16384] = {SX, SY, BX, BY, T_INST, 8'h00, 1'b1, 3'b000, cda[pc]};
                sq_w = sq_w + 1;
                want_done = want_done + 1;
                if (cda[pc][255:252] == 4'd1 && cda[pc][251:244] == 8'd0) tb_nw = cda[pc][11:0];
                if (cda[pc][255:252] == 4'd3) begin
                    pos_a = 0;
                    pos_b = 0;
                end
                // feed the producer's words while waiting (a RUN takes them)
                k = 0;
                while (done_cnt < want_done && k < 400000) begin
                    @(posedge clk);
                    k = k + 1;
                    if (pq_i < pq_n && credit_p >= 1) begin
                        b = pq_n - pq_i;
                        if (b > 8) b = 8;
                        if (b > credit_p) b = credit_p;
                        sq[sq_w % 16384] = flit(T_CU_DATA, 8'h00, 1'b0, 2'd0,
                                               {8'd0, 16'd0, b[7:0] - 8'd1, 8'h02, 8'd0, 208'd0});
                        sq_w = sq_w + 1;
                        for (i = 0; i < b; i = i + 1) begin
                            sq[sq_w % 16384] = flit(T_CU_DATA, 8'h00, (i == b - 1), 2'd0, pq[pq_i]);
                            sq_w = sq_w + 1;
                            pq_i = pq_i + 1;
                        end
                        credit_p = credit_p - b;
                    end
                end
                if (k >= 400000) begin
                    $display("  instruction %0d never completed (op %h)", want_done, cda[pc][255:252]);
                    errs = errs + 1;
                    pc = n;
                end
            end
        end
        repeat (50) @(posedge clk);
        // expected memory and peer words
        peer_err = 0;
        k = 0;
        for (pc = 0; pc < n; pc = pc + 1) begin
            if (cop[pc] == 4'h3 && mem[cad[pc][17:0]] !== cda[pc]) begin
                if (errs < 6) $display("  mem %h: got %h\n                want %h", cad[pc], mem[cad[pc][17:0]], cda[pc]);
                errs = errs + 1;
            end
            if (cop[pc] == 4'h4) begin
                if (k >= peer_n || peer_got[k] !== cda[pc]) begin
                    if (peer_err < 4) $display("  peer %0d: got %h\n             want %h", k, peer_got[k], cda[pc]);
                    peer_err = peer_err + 1;
                end
                k = k + 1;
            end
        end
        if (k != peer_n) begin
            $display("  peer words %0d, want %0d", peer_n, k);
            peer_err = peer_err + 1;
        end
        // the stream's credits sum to its words: none left with the producer
        if (credit_p != 0) begin
            $display("  ERROR the producer holds %0d credits past its stream", credit_p);
            errs = errs + 1;
        end
        errs = errs + peer_err + cred_err + run_err;
        if (errs == 0) begin
            $display("PASS sru_cu %0d instructions, %0d peer words", want_done, peer_n);
        end else begin
            $display("FAIL sru_cu %0d errors", errs);
        end
        $finish;
    end
endmodule

`default_nettype wire
