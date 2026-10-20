// sru_unpack over every FP16 bit pattern (scripts/py/sru/unpack_vectors.py);
// -d SRU_TB_ACC: the accumulator form (IEEE = 0, unpack_acc.hex).

`default_nettype none
`timescale 1ns/1ps

`ifndef SRU_DIR
  `define SRU_DIR "build/sru"
`endif

module sru_unpack_tb;
`ifdef SRU_TB_ACC
    localparam integer IEEE = 0;
`else
    localparam integer IEEE = 1;
`endif
    reg clk = 1'b0;
    always #2 clk = ~clk;

    reg [255:0] vin  [0:4095];
    reg [383:0] want [0:4095];
    reg [255:0] ti;
    reg [383:0] tw;
    integer n, fd, got, i, errs;

    reg  [255:0] din;
    wire [383:0] dout;
    sru_unpack #(.IEEE(IEEE)) dut (.clk(clk), .in_data(din), .out_data(dout));

    initial begin
        if (IEEE != 0) fd = $fopen($sformatf("%s/unpack.hex", `SRU_DIR), "r");
        else           fd = $fopen($sformatf("%s/unpack_acc.hex", `SRU_DIR), "r");
        if (fd == 0) begin
            $display("FAIL sru_unpack: no vector file -- run scripts/py/sru/unpack_vectors.py");
            $finish;
        end
        n = 0;
        got = 2;
        while (got == 2 && n < 4096) begin
            got = $fscanf(fd, "%h %h\n", ti, tw);
            if (got == 2) begin
                vin[n] = ti;
                want[n] = tw;
                n = n + 1;
            end
        end
        $fclose(fd);

        errs = 0;
        for (i = 0; i < n; i = i + 1) begin
            din = vin[i];
            @(posedge clk);
            #1;
            if (dout !== want[i]) begin
                if (errs < 6) $display("  MISMATCH word %0d\n    got  %h\n    want %h", i, dout, want[i]);
                errs = errs + 1;
            end
        end
        if (n == 4096 && errs == 0) begin
            $display("PASS sru_unpack IEEE=%0d 65536 values", IEEE);
        end else begin
            $display("FAIL sru_unpack IEEE=%0d %0d words, %0d wrong", IEEE, n, errs);
        end
        $finish;
    end
endmodule

`default_nettype wire
