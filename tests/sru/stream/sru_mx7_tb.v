// sru_mx7 against the MXFP7 spec (scripts/py/sru/mx7_vectors.py): every
// entry of build/sru/mx7_in.hex, random gaps between words, each out word
// checked. -d SRU_TB_BLAYOUT benches the B layout, -d SRU_TB_DSP the DSP
// multiplies.

`default_nettype none
`timescale 1ns/1ps

`ifndef SRU_DIR
  `define SRU_DIR "build/sru"
`endif

module sru_mx7_tb;
`ifdef SRU_TB_BLAYOUT
    localparam integer BL = 1;
`else
    localparam integer BL = 0;
`endif
`ifdef SRU_TB_DSP
    localparam integer MUL_DSP = 1;
`else
    localparam integer MUL_DSP = 0;
`endif
    localparam integer NMAX = 40000;

    reg clk = 1'b0, rst = 1'b1;
    always #2 clk = ~clk;

    reg [255:0] win  [0:NMAX-1];
    reg         wfst [0:NMAX-1];
    reg [255:0] want [0:NMAX-1];
    integer nin, nout, fd, got, n_in, n_out, errs;
    reg [3:0]   tf;
    reg [255:0] tw;

    initial begin
        fd = $fopen($sformatf("%s/mx7_in.hex", `SRU_DIR), "r");
        if (fd == 0) begin
            $display("FAIL sru_mx7: no vector file -- run scripts/py/sru/mx7_vectors.py");
            $finish;
        end
        nin = 0;
        got = 2;
        while (got == 2 && nin < NMAX) begin
            got = $fscanf(fd, "%h %h\n", tf, tw);
            if (got == 2) begin
                wfst[nin] = tf[0];
                win[nin]  = tw;
                nin = nin + 1;
            end
        end
        $fclose(fd);
        fd = $fopen($sformatf("%s/mx7_%s.hex", `SRU_DIR, BL ? "b" : "a"), "r");
        nout = 0;
        got = 1;
        while (got == 1 && nout < NMAX) begin
            got = $fscanf(fd, "%h\n", tw);
            if (got == 1) begin
                want[nout] = tw;
                nout = nout + 1;
            end
        end
        $fclose(fd);
    end

    reg          in_valid, in_first;
    reg  [255:0] in_data;
    wire         out_valid;
    wire [255:0] out_data;

    sru_mx7 #(.MUL_DSP(MUL_DSP)) dut (
        .clk(clk), .rst(rst), .b_layout(BL[0]), .in_valid(in_valid),
        .in_first(in_first), .in_data(in_data), .out_valid(out_valid),
        .out_data(out_data));

    reg [1:0] r;
    always @(posedge clk) begin
        if (rst) begin
            n_in     <= 0;
            in_valid <= 1'b0;
            in_first <= 1'b0;
        end else begin
            r = $random;
            in_valid <= 1'b0;
            in_first <= 1'b0;
            in_data  <= {8{$random}};
            if (n_in < nin && r != 2'd0) begin
                in_valid <= 1'b1;
                in_first <= wfst[n_in];
                in_data  <= win[n_in];
                n_in     <= n_in + 1;
            end
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            n_out <= 0;
            errs  <= 0;
        end else if (out_valid) begin
            if (out_data !== want[n_out]) begin
                if (errs < 6) begin
                    $display("  MISMATCH out %0d\n    got  %h\n    want %h", n_out, out_data, want[n_out]);
                end
                errs <= errs + 1;
            end
            n_out <= n_out + 1;
        end
    end

    initial begin
        repeat (8) @(posedge clk);
        rst = 1'b0;
        wait (nout > 0 && n_out == nout);
        repeat (40) @(posedge clk);
        if (errs == 0 && n_out == nout) begin
            $display("PASS sru_mx7 %s %0d words in, %0d out", BL ? "B" : "A", nin, nout);
        end else begin
            $display("FAIL sru_mx7 %s out %0d of %0d errors %0d", BL ? "B" : "A", n_out, nout, errs);
        end
        $finish;
    end

    initial begin
        #(4 * 4 * NMAX);
        $display("FAIL sru_mx7 timeout: out %0d of %0d", n_out, nout);
        $finish;
    end
endmodule

`default_nettype wire
