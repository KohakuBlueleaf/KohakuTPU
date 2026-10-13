// An array of N W-bit counters, one per vector register, for the V2 core's
// scoreboard: one +1 a cycle at `inc_a`, R asynchronous reads.
//
//   PRIM "ff"           flip-flops; `rst` clears every counter at once; each
//                       read is an N:1 mux.
//   PRIM "distributed"  LUTRAM (kohaku_sdpram, READ_LAT 0): R + 1 copies
//                       written together (the extra one reads `inc_a` for the
//                       increment); `clr` writes zero at `clr_a`, one counter a
//                       cycle, and `rst` is ignored. A read costs ~W x N/64 LUT
//                       rather than an N:1 mux a bit.
//
// A count wraps modulo 2^W; the core compares differences, never values.

`default_nettype none

module v2_cnt #(
    parameter integer N    = 32,
    parameter integer W    = 6,
    parameter integer R    = 1,
    parameter         PRIM = "ff"
)(
    input  wire                         clk,
    input  wire                         rst,
    input  wire                         clr,
    input  wire [$clog2(N)-1:0]         clr_a,
    input  wire                         inc,
    input  wire [$clog2(N)-1:0]         inc_a,
    input  wire [R*$clog2(N)-1:0]       ra,
    output wire [R*W-1:0]               rd
);
    localparam integer A = $clog2(N);
    genvar p;

    generate
    if (PRIM == "ff") begin : g_ff
        reg [W-1:0] c [0:N-1];
        integer i;
        always @(posedge clk) begin
            for (i = 0; i < N; i = i + 1) begin
                if (rst) begin
                    c[i] <= {W{1'b0}};
                end else if (inc && (inc_a == i[A-1:0])) begin
                    c[i] <= c[i] + 1'b1;
                end
            end
        end
        for (p = 0; p < R; p = p + 1) begin : g_rd
            assign rd[p*W +: W] = c[ra[p*A +: A]];
        end
    end else begin : g_lut
        wire [W-1:0] cur;
        wire         we = clr || inc;
        wire [A-1:0] wa = clr ? clr_a : inc_a;
        wire [W-1:0] wd = clr ? {W{1'b0}} : cur + 1'b1;
        for (p = 0; p <= R; p = p + 1) begin : g_cp
            wire [W-1:0] q;
            kohaku_sdpram #(.WIDTH(W), .DEPTH(N), .MEM_PRIM("distributed"), .READ_LAT(0)) u_m (
                .clk(clk), .wr_en(we), .wr_addr(wa), .wr_data(wd),
                .rd_en(1'b1), .rd_addr((p == R) ? inc_a : ra[(p == R ? 0 : p)*A +: A]),
                .rd_data(q));
            if (p == R) begin : g_inc
                assign cur = q;
            end else begin : g_out
                assign rd[p*W +: W] = q;
            end
        end
    end
    endgenerate

`ifndef SYNTHESIS
    // The counts as the core sees them, for the core's end-of-run checks.
    reg [W-1:0] sh [0:N-1];
    integer si;
    initial for (si = 0; si < N; si = si + 1) sh[si] = {W{1'b0}};
    always @(posedge clk) begin
        if (PRIM == "ff" && rst) begin
            for (si = 0; si < N; si = si + 1) sh[si] <= {W{1'b0}};
        end else if (PRIM != "ff" && clr) begin
            sh[clr_a] <= {W{1'b0}};
        end else if (inc) begin
            sh[inc_a] <= sh[inc_a] + 1'b1;
        end
    end
`endif

endmodule

`default_nettype wire
