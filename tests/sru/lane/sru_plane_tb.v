// sru_plane against both lane references: build/sru/fma_add1.hex (e8.py) and
// fn_ram.hex / fn_rom.hex / fn_nofn.hex (fn.py), the op chosen at random per
// issue, so FMA and function ops sit in the pipeline together, with random
// gaps; every result checked in issue order. At a table change only FMA ops
// issue until the function ops in flight are out. Every kind is built with the
// mantissa ROM (MROM); the RAM lane with HAS_MANT. -d SRU_TB_ROM: the exp2 ROM
// lane; -d SRU_TB_NOFN: HAS_FN = 0, the mantissa ROM alone beside the FMA.
// Run scripts/py/sru/fma_vectors.py and fn_vectors.py first.

`default_nettype none
`timescale 1ns/1ps

`ifndef SRU_DIR
  `define SRU_DIR "build/sru"
`endif

module sru_plane_tb;
`ifdef SRU_TB_ROM
    localparam integer ROM = 1;
`else
    localparam integer ROM = 0;
`endif
`ifdef SRU_TB_NOFN
    localparam integer HAS_FN = 0;
`else
    localparam integer HAS_FN = 1;
`endif
    localparam integer NMAX = 400000;
    localparam integer QMAX = 1 << 20;

    reg clk = 1'b0, rst = 1'b1;
    always #2 clk = ~clk;

    reg [23:0] ma [0:NMAX-1];
    reg [23:0] mb [0:NMAX-1];
    reg [23:0] mc [0:NMAX-1];
    reg [23:0] mw [0:NMAX-1];
    reg [23:0] fx [0:NMAX-1];
    reg [23:0] fk [0:NMAX-1];
    reg [1:0]  ff [0:NMAX-1];
    reg [5:0]  fp [0:NMAX-1];
    reg [1:0]  fc [0:NMAX-1];
    reg [2:0]  ft [0:NMAX-1];
    reg        fn_ [0:NMAX-1];
    reg        fr [0:NMAX-1];
    reg [23:0] fw [0:NMAX-1];
    reg [35:0] tabs [0:8191];
    reg [23:0] qw [0:QMAX-1];      // expected result per issue
    reg        qo [0:QMAX-1];
    reg [31:0] qi [0:QMAX-1];      // vector index per issue
    integer nm, nf, fd, got, im, jf, n_in, n_out, errs, fly;
    reg [23:0] ta, tb, tc, tw;
    reg [3:0]  tf, tcs, tt, tn, tr;
    reg [7:0]  tp;
    string     fname;

    initial begin
        $readmemh($sformatf("%s/tab_exp2.hex", `SRU_DIR), tabs, 0, 1023);
        $readmemh($sformatf("%s/tab_sigmoid.hex", `SRU_DIR), tabs, 1024, 2047);
        $readmemh($sformatf("%s/tab_phi.hex", `SRU_DIR), tabs, 2048, 3071);
        $readmemh($sformatf("%s/tab_recip.hex", `SRU_DIR), tabs, 3072, 4095);
        $readmemh($sformatf("%s/tab_rsqrt.hex", `SRU_DIR), tabs, 4096, 5119);
        $readmemh($sformatf("%s/tab_sqrt.hex", `SRU_DIR), tabs, 5120, 6143);
        fd = $fopen($sformatf("%s/fma_add1.hex", `SRU_DIR), "r");
        if (fd == 0) begin
            $display("FAIL sru_plane: no FMA vectors -- run scripts/py/sru/fma_vectors.py");
            $finish;
        end
        nm = 0;
        got = 4;
        while (got == 4 && nm < NMAX) begin
            got = $fscanf(fd, "%h %h %h %h\n", ta, tb, tc, tw);
            if (got == 4) begin
                ma[nm] = ta; mb[nm] = tb; mc[nm] = tc; mw[nm] = tw;
                nm = nm + 1;
            end
        end
        $fclose(fd);
        nf = 0;
        // one string per kind: a ternary of unequal literals pads with NULs
        if (HAS_FN == 0)   fname = $sformatf("%s/fn_nofn.hex", `SRU_DIR);
        else if (ROM != 0) fname = $sformatf("%s/fn_rom.hex", `SRU_DIR);
        else               fname = $sformatf("%s/fn_ram.hex", `SRU_DIR);
        fd = $fopen(fname, "r");
        if (fd == 0) begin
            $display("FAIL sru_plane: no function vectors -- run scripts/py/sru/fn_vectors.py");
            $finish;
        end
        got = 9;
        while (got == 9 && nf < NMAX) begin
            got = $fscanf(fd, "%h %h %h %h %h %h %h %h %h\n", ta, tb, tf, tp, tcs, tt, tn, tr, tw);
            if (got == 9) begin
                fx[nf] = ta; fk[nf] = tb; ff[nf] = tf[1:0]; fp[nf] = tp[5:0];
                fc[nf] = tcs[1:0]; ft[nf] = tt[2:0]; fn_[nf] = tn[0]; fr[nf] = tr[0];
                fw[nf] = tw;
                nf = nf + 1;
            end
        end
        $fclose(fd);
    end

    reg               in_valid, op, neg, mrom;
    reg  [1:0]        fm;
    reg  [23:0]       a, b, c;
    reg signed [5:0]  pre;
    reg  [1:0]        cs;
    wire [9:0]        tab_addr;
    reg  [35:0]       tq1, tq2;
    reg  [2:0]        cur;
    wire              out_valid;
    wire [23:0]       out;

    sru_plane #(.MODEL(`MX_MODEL), .HAS_FN(HAS_FN), .TAB((ROM != 0) ? "rom" : "ram"),
                .HAS_MANT((HAS_FN != 0) && (ROM == 0)), .MROM(1)) dut (
        .clk(clk), .rst(rst), .in_valid(in_valid), .op(op), .a(a), .b(b), .c(c),
        .fm(fm), .neg(neg), .mrom(mrom), .pre(pre), .cs(cs),
        .tab_addr(tab_addr), .tab_q(tq2),
        .out_valid(out_valid), .out(out));

    always @(posedge clk) begin
        tq1 <= tabs[{cur, tab_addr}];
        tq2 <= tq1;
    end

    // Per cycle: a gap with probability 1/4, else an FMA or a function op at
    // random while both have vectors left; idle inputs carry garbage.
    reg [2:0] r;
    reg       want_fn, fn_ok;
    always @(posedge clk) begin
        if (rst) begin
            n_in     <= 0;
            im       <= 0;
            jf       <= 0;
            in_valid <= 1'b0;
            cur      <= 3'd0;
        end else begin
            r = $random;
            in_valid <= 1'b0;
            op <= $random; a <= $random; b <= $random; c <= $random;
            fm <= $random; neg <= $random; mrom <= $random; pre <= $random; cs <= $random;
            fn_ok   = (jf < nf) && (ft[jf] == cur);
            want_fn = (jf < nf) && (r[2] || im >= nm);
            if (jf < nf && ft[jf] != cur && fly == 0 && !(in_valid && op)) begin
                cur <= ft[jf];
            end
            if (r[1:0] != 2'd0) begin
                if (want_fn && fn_ok) begin
                    in_valid <= 1'b1;
                    op  <= 1'b1;
                    a   <= fx[jf]; b <= fk[jf];
                    fm  <= ff[jf]; neg <= fn_[jf]; mrom <= fr[jf];
                    pre <= fp[jf]; cs <= fc[jf];
                    qw[n_in] <= fw[jf]; qo[n_in] <= 1'b1; qi[n_in] <= jf;
                    jf  <= jf + 1;
                    n_in <= n_in + 1;
                end else if (im < nm && !(want_fn && fn_ok)) begin
                    in_valid <= 1'b1;
                    op  <= 1'b0;
                    a   <= ma[im]; b <= mb[im]; c <= mc[im];
                    qw[n_in] <= mw[im]; qo[n_in] <= 1'b0; qi[n_in] <= im;
                    im  <= im + 1;
                    n_in <= n_in + 1;
                end
            end
        end
    end

    // function ops in flight: issued (registered) and not yet out
    wire fn_in  = in_valid && op;
    reg  fn_out;
    always @(*) fn_out = out_valid && qo[n_out];
    always @(posedge clk) begin
        if (rst) fly <= 0;
        else     fly <= fly + (fn_in ? 1 : 0) - (fn_out ? 1 : 0);
    end

    always @(posedge clk) begin
        if (rst) begin
            n_out <= 0;
            errs  <= 0;
        end else if (out_valid) begin
            if (out !== qw[n_out]) begin
                if (errs < 10) begin
                    if (qo[n_out]) begin
                        $display("  MISMATCH %0d fn %0d: x %06h k %06h mode %0d neg %0d mrom %0d pre %0d cs %0d tab %0d  got %06h want %06h",
                                 n_out, qi[n_out], fx[qi[n_out]], fk[qi[n_out]], ff[qi[n_out]],
                                 fn_[qi[n_out]], fr[qi[n_out]], $signed(fp[qi[n_out]]),
                                 fc[qi[n_out]], ft[qi[n_out]], out, qw[n_out]);
                    end else begin
                        $display("  MISMATCH %0d fma %0d: a %06h b %06h c %06h  got %06h want %06h",
                                 n_out, qi[n_out], ma[qi[n_out]], mb[qi[n_out]], mc[qi[n_out]],
                                 out, qw[n_out]);
                    end
                end
                errs <= errs + 1;
            end
            n_out <= n_out + 1;
        end
    end

    initial begin
        repeat (8) @(posedge clk);
        rst = 1'b0;
        wait (nm > 0 && im == nm && jf == nf && n_out == n_in);
        repeat (24) @(posedge clk);
        if (errs == 0 && n_out == nm + nf && !out_valid) begin
            $display("PASS sru_plane %s HAS_FN=%0d %0d FMA + %0d function vectors",
                     (ROM != 0) ? "rom" : "ram", HAS_FN, nm, nf);
        end else begin
            $display("FAIL sru_plane %s HAS_FN=%0d out %0d of %0d errors %0d",
                     (ROM != 0) ? "rom" : "ram", HAS_FN, n_out, nm + nf, errs);
        end
        $finish;
    end

    // A dead DUT stops n_out, which the wait above would hold forever.
    initial begin
        #(4 * 4 * 2 * NMAX);
        $display("FAIL sru_plane timeout: out %0d of %0d", n_out, nm + nf);
        $finish;
    end
endmodule

`default_nettype wire
