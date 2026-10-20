// sru_core running whole jobs (scripts/py/sru/core_vectors.py, expectations
// from scripts/py/sru/core.py): configuration, tables and row state loaded,
// A and B streamed with random gaps, the out stream popped with random gaps
// and checked word by word, then the job's row state read back.
// The profile's knobs come as defines from scripts/py/sru/profiles.py (through
// xsim.py); SRU_TB_PROF numbers it in profiles.PROFILES order, which names the
// command file core_<profile>.cmd.

`default_nettype none
`timescale 1ns/1ps

`ifndef SRU_DIR
  `define SRU_DIR "build/sru"
`endif
`ifndef SRU_TB_PROF
  `define SRU_TB_PROF 0
  `define SRU_F_LANES 16
  `define SRU_HAS_FN 1
  `define SRU_H_TAPS 2
  `define SRU_HAS_R1 1
  `define SRU_HAS_R2 1
  `define SRU_HAS_MX7 1
  `define SRU_HAS_MASK 1
  `define SRU_HAS_B 1
  `define SRU_BUF_WORDS 512
`endif

module sru_core_tb;
`ifdef SRU_TAB_ROM
    localparam TAB = "rom";
`else
    localparam TAB = "ram";
`endif
    localparam integer PROF = `SRU_TB_PROF;
    localparam integer NMAX = 20000;
    reg [8*8-1:0] pname;
    initial begin
        case (PROF)
            0: pname = "strong";
            1: pname = "rom";
            2: pname = "mid";
            3: pname = "spu";
            default: pname = "pack";
        endcase
    end

    reg clk = 1'b0, rst = 1'b1;
    always #2 clk = ~clk;

    reg [3:0]   cop [0:NMAX-1];
    reg [15:0]  cad [0:NMAX-1];
    reg [255:0] cda [0:NMAX-1];
    integer n, fd, got, pc, errs, jobs;
    reg [3:0]   t_op;
    reg [15:0]  t_ad;
    reg [255:0] t_da;

    initial begin
        #1;
        fd = $fopen($sformatf("%s/core_%0s.cmd", `SRU_DIR, pname), "r");
        if (fd == 0) begin
            $display("FAIL sru_core: no command file -- run scripts/py/sru/core_vectors.py");
            $finish;
        end
        n = 0;
        got = 3;
        while (got == 3 && n < NMAX) begin
            got = $fscanf(fd, "%h %h %h\n", t_op, t_ad, t_da);
            if (got == 3) begin
                cop[n] = t_op; cad[n] = t_ad; cda[n] = t_da;
                n = n + 1;
            end
        end
        $fclose(fd);
    end

    // ---- DUT ----
    reg          cfg_we, tab_we, cc_we, rs_we, start;
    reg  [7:0]   cfg_addr;
    reg  [31:0]  cfg_wdata;
    reg  [9:0]   tab_waddr;
    reg  [35:0]  tab_wdata;
    reg  [8:0]   cc_waddr, rs_waddr, rs_raddr;
    reg  [191:0] cc_wdata;
    reg  [71:0]  rs_wdata;
    wire [71:0]  rs_rdata;
    wire         busy, done;
    reg          a_valid, b_valid, o_pop;
    reg  [255:0] a_data, b_data;
    wire         a_ready, b_ready, o_valid;
    wire [255:0] o_data;

    sru_core #(.MODEL(`MX_MODEL), .F_LANES(`SRU_F_LANES), .HAS_FN(`SRU_HAS_FN), .TAB(TAB),
               .H_TAPS(`SRU_H_TAPS),
               .HAS_R1(`SRU_HAS_R1), .HAS_R2(`SRU_HAS_R2), .HAS_MX7(`SRU_HAS_MX7),
               .HAS_MASK(`SRU_HAS_MASK), .HAS_B(`SRU_HAS_B), .BUF_WORDS(`SRU_BUF_WORDS),
               .HAS_MANT(`SRU_HAS_MANT), .FIN_RCP(`SRU_FIN_RCP), .F16_IEEE(`SRU_F16_IEEE)) dut (
        .clk(clk), .rst(rst),
        .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_wdata(cfg_wdata),
        .tab_we(tab_we), .tab_waddr(tab_waddr), .tab_wdata(tab_wdata),
        .cc_we(cc_we), .cc_waddr(cc_waddr), .cc_wdata(cc_wdata),
        .rs_we(rs_we), .rs_waddr(rs_waddr), .rs_wdata(rs_wdata),
        .rs_raddr(rs_raddr), .rs_rdata(rs_rdata),
        .start(start), .busy(busy), .done(done),
        .a_valid(a_valid), .a_data(a_data), .a_ready(a_ready),
        .b_valid(b_valid), .b_data(b_data), .b_ready(b_ready),
        .o_valid(o_valid), .o_data(o_data), .o_count(), .o_pop(o_pop)
    );

    // ---- one job's streams ----
    integer ai, bi, oi, na, nb, no, ja, jb, jo, k, jerr;
    reg [255:0] qa [0:4095];
    reg [255:0] qb [0:4095];
    reg [255:0] qo [0:4095];
    reg         running;

`ifdef SRU_TB_TRACE
    integer tc = 0;
    always @(posedge clk) begin
        if (!rst && tc < `SRU_TB_TRACE && (^{dut.infl, dut.m7[0], dut.issue} === 1'bx)) begin
            tc = tc + 1;
            $display("  T %0t st %0d cur %0d nops %0d slot %b credit %b a_v %b issue %b infl %0d m7 %b m4 %b plov %b",
                     $time, dut.st, dut.cur, dut.nops_c, dut.slot, dut.credit, a_valid,
                     dut.issue, dut.infl, dut.m7, dut.m4, dut.pl_ov);
        end
    end
`endif

    // A and B: hold a word until taken; a gap before a new one 1 time in 4
    always @(posedge clk) begin
        if (rst || !running) begin
            a_valid <= 1'b0;
            b_valid <= 1'b0;
        end else begin
            if (a_valid && a_ready) begin
                ai = ai + 1;
                a_valid <= 1'b0;
            end
            if (b_valid && b_ready) begin
                bi = bi + 1;
                b_valid <= 1'b0;
            end
            if ((!a_valid || a_ready) && ai < na && ($random & 3) != 0) begin
                a_valid <= 1'b1;
                a_data  <= qa[ai];
            end
            if ((!b_valid || b_ready) && bi < nb && ($random & 3) != 0) begin
                b_valid <= 1'b1;
                b_data  <= qb[bi];
            end
        end
    end

    // out: popped 3 times in 4
    always @(posedge clk) begin
        o_pop <= ($random & 3) != 0;
        if (running && o_valid && o_pop) begin
            if (oi >= no || o_data !== qo[oi]) begin
                if (jerr < 4) begin
                    $display("  MISMATCH job %0d out %0d of %0d\n    got  %h\n    want %h",
                             jobs, oi, no, o_data, (oi < no) ? qo[oi] : 256'd0);
                end
                jerr = jerr + 1;
            end
            oi = oi + 1;
        end
    end

    initial begin
        cfg_we = 0; tab_we = 0; cc_we = 0; rs_we = 0; start = 0; running = 0;
        rs_raddr = 0; o_pop = 0;
        errs = 0; jobs = 0;
        repeat (8) @(posedge clk);
        rst = 0;
        repeat (4) @(posedge clk);
        pc = 0;
        while (pc < n) begin
            @(posedge clk);
            cfg_we <= 0; tab_we <= 0; cc_we <= 0; rs_we <= 0;
            case (cop[pc])
                4'h0: begin cfg_we <= 1; cfg_addr <= cad[pc]; cfg_wdata <= cda[pc]; pc = pc + 1; end
                4'h1: begin tab_we <= 1; tab_waddr <= cad[pc]; tab_wdata <= cda[pc]; pc = pc + 1; end
                4'h2: begin cc_we <= 1; cc_waddr <= cad[pc]; cc_wdata <= cda[pc]; pc = pc + 1; end
                4'h3: begin rs_we <= 1; rs_waddr <= cad[pc]; rs_wdata <= cda[pc]; pc = pc + 1; end
                4'h4: begin
                    // gather this job's streams, then run it
                    pc = pc + 1;
                    na = 0; nb = 0; no = 0;
                    while (cop[pc] == 4'h5) begin qa[na] = cda[pc]; na = na + 1; pc = pc + 1; end
                    while (cop[pc] == 4'h6) begin qb[nb] = cda[pc]; nb = nb + 1; pc = pc + 1; end
                    while (cop[pc] == 4'h7) begin qo[no] = cda[pc]; no = no + 1; pc = pc + 1; end
                    ai = 0; bi = 0; oi = 0; jerr = 0;
                    start <= 1;
                    @(posedge clk);
                    start <= 0;
                    running <= 1;
                    @(posedge clk);
                    k = 0;
                    while (!done && k < 200000) begin
                        @(posedge clk);
                        k = k + 1;
                    end
                    running <= 0;
                    if (k >= 200000) begin
                        $display("  job %0d: no done (A %0d/%0d, B %0d/%0d, out %0d/%0d)",
                                 jobs, ai, na, bi, nb, oi, no);
                        jerr = jerr + 1;
                    end
                    if (oi != no) begin
                        $display("  job %0d: %0d out words, want %0d", jobs, oi, no);
                        jerr = jerr + 1;
                    end
                    // row state
                    while (cop[pc] == 4'h8) begin
                        rs_raddr <= cad[pc];
                        repeat (3) @(posedge clk);
                        if (rs_rdata !== cda[pc][71:0]) begin
                            if (jerr < 8) $display("  job %0d row %0d: got %h want %h",
                                                   jobs, cad[pc], rs_rdata, cda[pc][71:0]);
                            jerr = jerr + 1;
                        end
                        pc = pc + 1;
                    end
                    if (cop[pc] == 4'h9) pc = pc + 1;
                    $display("  job %0d: %0d in, %0d out, %0d errors", jobs, na, no, jerr);
                    errs = errs + jerr;
                    jobs = jobs + 1;
                end
                default: pc = pc + 1;
            endcase
        end
        repeat (4) @(posedge clk);
        if (errs == 0 && jobs > 0) begin
            $display("PASS sru_core %0s %0d jobs", pname, jobs);
        end else begin
            $display("FAIL sru_core %0s %0d errors over %0d jobs", pname, errs, jobs);
        end
        $finish;
    end
endmodule

`default_nettype wire
