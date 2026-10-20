// sru_e8f16_acc (both halves) against scripts/py/sru/unpack_vectors.py's
// e8f16_acc.hex: every mantissa of exponents 100..150, a stride of the rest.

`default_nettype none
`timescale 1ns/1ps

`ifndef SRU_DIR
  `define SRU_DIR "build/sru"
`endif

module sru_e8f16_tb;
    localparam integer NMAX = 131072;

    reg [383:0] vin  [0:NMAX-1];
    reg [255:0] want [0:NMAX-1];
    reg [383:0] ti;
    reg [255:0] tw;
    integer n, fd, got, i, errs;

    reg  [383:0] din;
    wire [319:0] mid;
    wire [255:0] dout;
    genvar j;
    generate
    for (j = 0; j < 16; j = j + 1) begin : g_l
        sru_e8f16_acc_s1 u_1 (.e8(din[j*24 +: 24]), .mid(mid[j*20 +: 20]));
        sru_e8f16_acc_s2 u_2 (.mid(mid[j*20 +: 20]), .f16(dout[j*16 +: 16]));
    end
    endgenerate

    initial begin
        fd = $fopen($sformatf("%s/e8f16_acc.hex", `SRU_DIR), "r");
        if (fd == 0) begin
            $display("FAIL sru_e8f16: no vector file -- run scripts/py/sru/unpack_vectors.py");
            $finish;
        end
        n = 0;
        got = 2;
        while (got == 2 && n < NMAX) begin
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
            #1;
            if (dout !== want[i]) begin
                if (errs < 6) $display("  MISMATCH word %0d\n    in   %h\n    got  %h\n    want %h", i, vin[i], dout, want[i]);
                errs = errs + 1;
            end
        end
        if (n > 100000 && errs == 0) begin
            $display("PASS sru_e8f16 %0d values", n * 16);
        end else begin
            $display("FAIL sru_e8f16 %0d words, %0d wrong", n, errs);
        end
        $finish;
    end
endmodule

`default_nettype wire
