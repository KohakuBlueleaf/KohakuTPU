// v2_gt4 against the transpose written out directly: out word i granule w =
// in word w granule i. A random mix of plain words and GT4 groups, with gaps
// between items and stalls inside groups, so every shift rule is exercised.

`default_nettype none
`timescale 1ns/1ps

module v2_gt4_tb;
    localparam integer N = 6000;            // input words
    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

    reg  [255:0] wd   [0:N-1];
    reg          wg   [0:N-1];              // word belongs to a GT4 group
    reg  [255:0] want [0:N-1];
    reg  [15:0]  wtag [0:N-1];
    integer n_in, n_out, errs, i, g, w, gr, k;

    reg          in_valid, in_gt4;
    reg  [255:0] in_data;
    reg  [15:0]  in_tag;
    wire         out_valid, busy;
    wire [255:0] out_data;
    wire [15:0]  out_tag;

    v2_gt4 #(.G(64), .TW(16)) dut (
        .clk(clk), .rst(rst), .in_valid(in_valid), .in_gt4(in_gt4), .in_data(in_data),
        .in_tag(in_tag), .out_valid(out_valid), .out_data(out_data), .out_tag(out_tag),
        .busy(busy));

    initial begin
        // Build the stream and its expected output.
        i = 0;
        while (i < N) begin
            if ((($random & 3) != 0) && (i + 4 <= N)) begin
                for (w = 0; w < 4; w = w + 1) begin
                    for (k = 0; k < 8; k = k + 1) wd[i+w][k*32 +: 32] = $random;
                    wg[i+w] = 1'b1;
                end
                for (gr = 0; gr < 4; gr = gr + 1) begin
                    for (w = 0; w < 4; w = w + 1) begin
                        want[i+gr][w*64 +: 64] = wd[i+w][gr*64 +: 64];
                    end
                    wtag[i+gr] = i + gr;
                end
                i = i + 4;
            end else begin
                for (k = 0; k < 8; k = k + 1) wd[i][k*32 +: 32] = $random;
                wg[i]   = 1'b0;
                want[i] = wd[i];
                wtag[i] = i;
                i = i + 1;
            end
        end
    end

    // Driver: a gap before any item with probability 1/3, a stall inside a
    // group with probability 1/4.
    reg [1:0] r;
    always @(posedge clk) begin
        if (rst) begin
            n_in <= 0; in_valid <= 1'b0;
        end else begin
            in_valid <= 1'b0;
            if (n_in < N) begin
                r = $random;
                if (r != 0) begin
                    in_valid <= 1'b1;
                    in_gt4   <= wg[n_in];
                    in_data  <= wd[n_in];
                    in_tag   <= n_in;
                    n_in     <= n_in + 1;
                end
            end
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            n_out <= 0; errs <= 0;
        end else if (out_valid) begin
            if (out_data !== want[n_out] || out_tag !== wtag[n_out]) begin
                if (errs < 8) $display("MISMATCH out %0d tag %0d want tag %0d", n_out, out_tag, wtag[n_out]);
                errs <= errs + 1;
            end
            n_out <= n_out + 1;
        end
    end

    initial begin
        repeat (4) @(posedge clk);
        rst = 0;
        wait (n_out == N || $time > 200000);
        repeat (4) @(posedge clk);
        if (n_out == N && errs == 0 && !busy)
            $display("@@@ PASS v2_gt4 %0d words", N);
        else
            $display("@@@ FAIL v2_gt4 out %0d of %0d errors %0d busy %0d", n_out, N, errs, busy);
        $finish;
    end
endmodule

`default_nettype wire
