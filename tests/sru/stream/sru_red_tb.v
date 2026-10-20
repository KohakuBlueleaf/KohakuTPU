// sru_red against scripts/py/sru/red.py: every sub-row of build/sru/red_in.hex
// with random gaps between words, each row result checked. -d SRU_TB_SUM
// benches the sum reducer, else max. Run scripts/py/sru/red_vectors.py first.

`default_nettype none
`timescale 1ns/1ps

`ifndef SRU_DIR
  `define SRU_DIR "build/sru"
`endif

module sru_red_tb;
`ifdef SRU_TB_SUM
    localparam KIND = "sum";
`else
    localparam KIND = "max";
`endif
    localparam integer WMAX = 200000;
    localparam integer RMAX = 20000;

    reg clk = 1'b0, rst = 1'b1;
    always #2 clk = ~clk;

    reg [383:0]  wd   [0:WMAX-1];
    reg          wf   [0:WMAX-1];
    reg          wl   [0:WMAX-1];
    reg [231:0]  want [0:RMAX-1];
    integer nw, nr, fd, got, i, n_in, n_out, errs;
    reg [23:0]   t [0:15];
    reg [3:0]    tf, tl;
    reg [57:0]   q [0:3];

    initial begin
        fd = $fopen($sformatf("%s/red_in.hex", `SRU_DIR), "r");
        if (fd == 0) begin
            $display("FAIL sru_red: no vector file -- run scripts/py/sru/red_vectors.py");
            $finish;
        end
        nw = 0;
        got = 18;
        while (got == 18 && nw < WMAX) begin
            got = $fscanf(fd, "%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h\n",
                          tf, tl, t[15], t[14], t[13], t[12], t[11], t[10], t[9], t[8],
                          t[7], t[6], t[5], t[4], t[3], t[2], t[1], t[0]);
            if (got == 18) begin
                wf[nw] = tf[0];
                wl[nw] = tl[0];
                for (i = 0; i < 16; i = i + 1) wd[nw][i*24 +: 24] = t[i];
                nw = nw + 1;
            end
        end
        $fclose(fd);

        fd = $fopen($sformatf("%s/red_%s.hex", `SRU_DIR, KIND), "r");
        nr = 0;
        got = 4;
        while (got == 4 && nr < RMAX) begin
            got = $fscanf(fd, "%h %h %h %h\n", q[3], q[2], q[1], q[0]);
            if (got == 4) begin
                want[nr] = {q[3], q[2], q[1], q[0]};
                nr = nr + 1;
            end
        end
        $fclose(fd);
    end

    reg          in_valid, in_first, in_last;
    reg  [383:0] in_data;
    wire         out_valid;
    wire [231:0] out;

    sru_red #(.KIND(KIND)) dut (
        .clk(clk), .rst(rst), .in_valid(in_valid), .in_first(in_first),
        .in_last(in_last), .in_data(in_data), .out_valid(out_valid), .out(out));

    reg [1:0] r;
    always @(posedge clk) begin
        if (rst) begin
            n_in     <= 0;
            in_valid <= 1'b0;
            in_first <= 1'b0;
            in_last  <= 1'b0;
        end else begin
            r = $random;
            in_valid <= 1'b0;
            in_first <= 1'b0;
            in_last  <= 1'b0;
            in_data  <= {12{$random}};
            if (n_in < nw && r != 2'd0) begin
                in_valid <= 1'b1;
                in_first <= wf[n_in];
                in_last  <= wl[n_in];
                in_data  <= wd[n_in];
                n_in     <= n_in + 1;
            end
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            n_out <= 0;
            errs  <= 0;
        end else if (out_valid) begin
            if (out !== want[n_out]) begin
                if (errs < 8) begin
                    $display("  MISMATCH sub-row %0d\n    got  %h\n    want %h", n_out, out, want[n_out]);
                end
                errs <= errs + 1;
            end
            n_out <= n_out + 1;
        end
    end

    initial begin
        repeat (8) @(posedge clk);
        rst = 1'b0;
        wait (nr > 0 && n_out == nr);
        repeat (20) @(posedge clk);
        if (errs == 0 && n_out == nr) begin
            $display("PASS sru_red %s %0d sub-rows, %0d words", KIND, nr, nw);
        end else begin
            $display("FAIL sru_red %s out %0d of %0d errors %0d", KIND, n_out, nr, errs);
        end
        $finish;
    end

`ifdef SRU_TB_DBG
    always @(posedge clk) begin
        if (dut.v[3] && n_out == `SRU_TB_DBG) begin
            $display("  DBG row2 t4 %h gr4 %0d EM4 %0d A %h f %b l %b", dut.g_sum.g_row[2].t4,
                     dut.g_sum.g_row[2].gr4, dut.g_sum.g_row[2].EM4, dut.g_sum.g_row[2].A,
                     dut.f[3], dut.l[3]);
        end
    end
`endif

    initial begin
        #(4 * 4 * WMAX);
        $display("FAIL sru_red timeout: out %0d of %0d", n_out, nr);
        $finish;
    end
endmodule

`default_nettype wire
