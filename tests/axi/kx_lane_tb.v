// kx_lane bench: one source, NT taps (-d TB_NT), payload W (-d TB_W), tap t
// taking destination t. Random destinations, per-destination scoreboards
// (in order, once), random ready per tap, a long stall at one tap with the
// others live (in-order per lane: nothing overtakes, nothing is lost), a
// credit soak, and tap resets released at different times.

`timescale 1ns/1ps
`default_nettype none

`ifndef TB_NT
  `define TB_NT 3
`endif
`ifndef TB_W
  `define TB_W 590
`endif
`ifndef TB_LEAN
  `define TB_LEAN 1
`endif

module kx_lane_tb;
    localparam integer NT = `TB_NT, W = `TB_W, DW = 2, ND = 1 << DW;
    localparam         BUF = (`TB_LEAN != 0) ? "lean" : "xpm";
    localparam         MEM = (W > 128) ? "block" : "distributed";
    function [NT*ND-1:0] mk_take; input integer dummy; integer t;
        begin mk_take = 0; for (t = 0; t < NT; t = t + 1) begin mk_take[t*ND + t] = 1'b1; end end
    endfunction
    localparam [NT*ND-1:0] TAKE = mk_take(0);

    reg clk = 0;
    always #1.667 begin
        clk = ~clk;
    end
    reg          rstn_s = 0;
    reg [NT-1:0] rstn_t = 0;

    reg           s_valid = 0;
    wire          s_ready;
    reg  [DW-1:0] s_dst = 0;
    reg  [W-1:0]  s_data = 0;
    wire [NT-1:0]    t_valid;
    reg  [NT-1:0]    t_ready = 0;
    wire [NT*DW-1:0] t_dst;
    wire [NT*W-1:0]  t_data;

    kx_lane #(.W(W), .DW(DW), .NT(NT), .TAKE(TAKE), .DEPTH(16), .MEM(MEM), .BUF(BUF)) dut (
        .clk(clk), .rstn_s(rstn_s), .rstn_t(rstn_t),
        .s_valid(s_valid), .s_ready(s_ready), .s_dst(s_dst), .s_data(s_data),
        .t_valid(t_valid), .t_ready(t_ready), .t_dst(t_dst), .t_data(t_data));

    integer errors = 0, checks = 0;
    task check(input cond, input [8*64-1:0] what);
        begin
            checks = checks + 1;
            if (!cond) begin errors = errors + 1; $display("%0t ERROR %0s", $time, what); end
        end
    endtask
    // payload = destination and that destination's sequence number, stretched
    function [W-1:0] pat(input integer d, input integer n);
        integer k;
        begin
            pat = 0;
            for (k = 0; k < W; k = k + 32) begin pat[k +: 32] = (d * 32'h1000_0000) + n * 32'h9e3779b1 + k; end
        end
    endfunction

    // driver: a queue of (dst) decided up front so the scoreboard knows the order
    integer sent [0:ND-1];
    integer got  [0:ND-1];
    integer nsend = 0;
    // several taps pop in one cycle: a shared counter incremented by each
    // would lose the others' increments, so the total is summed
    function integer sum_got(input integer dummy); integer q;
        begin sum_got = 0; for (q = 0; q < NT; q = q + 1) begin sum_got = sum_got + got[q]; end end
    endfunction
    wire integer ngot = sum_got(0);
    reg drive = 0;
    reg [DW-1:0] next_dst;
    integer d0;
    initial begin for (d0 = 0; d0 < ND; d0 = d0 + 1) begin sent[d0] = 0; got[d0] = 0; end end
    always @(*) begin s_valid = drive && rstn_s; end
    always @(posedge clk) begin
        if (s_valid && s_ready) begin
            sent[s_dst] <= sent[s_dst] + 1;
            nsend <= nsend + 1;
            next_dst = $urandom % NT;
            s_dst  <= next_dst;
            s_data <= pat(next_dst, sent[next_dst] + ((next_dst == s_dst) ? 1 : 0));
        end
    end
    // tap scoreboards: in order, once, per destination
    genvar g;
    generate for (g = 0; g < NT; g = g + 1) begin : g_sb
        always @(posedge clk) begin
            if (t_valid[g] && t_ready[g]) begin
                check(t_dst[g*DW +: DW] == g, "flit at the wrong tap");
                check(t_data[g*W +: W] == pat(g, got[g]), "flit out of order or corrupted at a tap");
                got[g] <= got[g] + 1;
            end
        end
    end endgenerate

    task cycles(input integer n); begin repeat (n) @(posedge clk); #1; end endtask
    integer i, t, before2;
    initial begin
        s_dst = 0; s_data = pat(0, 0);
        // 1. taps released one by one AFTER the source starts: nothing lost
        cycles(4);
        rstn_s <= 1; drive <= 1; t_ready <= {NT{1'b1}};
        for (t = 0; t < NT; t = t + 1) begin cycles(5); rstn_t[t] <= 1'b1; end
        cycles(300);
        drive <= 0; cycles(60);
        check(ngot == nsend, "flits lost across staggered tap releases");
        check(nsend > 200, "source barely ran");

        // 2. random ready everywhere, sustained
        drive <= 1;
        for (i = 0; i < 20000; i = i + 1) begin
            @(posedge clk);
            t_ready <= $urandom;
            drive   <= ($urandom % 4) != 0;
        end
        @(posedge clk); drive <= 0; t_ready <= {NT{1'b1}};
        cycles(80);
        check(ngot == nsend, "soak lost or duplicated flits");
        check(nsend > 8000, "soak moved too little");

        // 3. head-of-line: tap 0 stalls for 300 cycles while the source keeps
        //    sending to every destination; the lane is in order, so the
        //    far taps go quiet once the buffers behind tap 0 hold a tap-0 flit,
        //    and everything resumes in order afterwards
        if (NT > 1) begin
            drive <= 1; t_ready <= {NT{1'b1}}; t_ready[0] <= 1'b0;
            cycles(300);
            before2 = got[NT-1];
            cycles(100);
            check(got[NT-1] == before2, "a far tap advanced past a stalled near tap's flit");
            t_ready[0] <= 1'b1;
            cycles(200);
            drive <= 0; cycles(80);
            check(ngot == nsend, "flits lost across the head-of-line stall");
        end

        // 4. the source's partition reset alone, longer than the drain
        drive <= 1; cycles(50);
        drive <= 0; rstn_s <= 0; cycles(80);
        check(ngot == nsend, "flits lost or invented across the source reset");
        rstn_s <= 1; cycles(10);
        check(!(|t_valid), "a tap presented a flit nobody sent");

        if (errors == 0) begin $display("PASS -- kx_lane NT=%0d W=%0d buf=%0s, %0d checks, 0 errors", NT, W, BUF, checks); end
        else             begin $display("FAIL -- kx_lane NT=%0d W=%0d buf=%0s, %0d checks, %0d errors", NT, W, BUF, checks, errors); end
        $finish;
    end
endmodule

`default_nettype wire
