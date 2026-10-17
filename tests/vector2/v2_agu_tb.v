// v2_agu against a reference walk: random descriptors (dimension 0 contiguous
// or strided, zero bounds, negative strides, carries through all four
// dimensions), offsets and caps, both walkers at once, colliding starts, table
// rewrites while a walker runs. Every step checks addr, n, left0, stride0 and
// contig; `total` is checked from 4 cycles after the start. Steps begin 4
// cycles after the start, when D2/D3 have settled (the core's first is at 5).

`default_nettype none
`timescale 1ns/1ps

module v2_agu_tb;
    localparam integer AW = 40;
    localparam integer NWALK = 3000;       // walks per walker

    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

    reg              wr_en = 0;
    reg  [2:0]       wr_ad = 0, wr_fld = 0;
    reg  [AW-1:0]    wr_val = 0;
    reg  [1:0]       start = 0, step = 0;
    reg  [5:0]       sel = 0;
    reg  [2*AW-1:0]  off = 0;
    reg  [31:0]      cap = 0;
    wire [1:0]       start_ok, contig;
    wire [31:0]      n, left0;
    wire [2*AW-1:0]  addr;
    wire [63:0]      total;
    wire [35:0]      stride0;

    v2_agu #(.AW(AW)) dut (
        .clk(clk), .rst(rst), .wr_en(wr_en), .wr_ad(wr_ad), .wr_fld(wr_fld), .wr_val(wr_val),
        .start(start), .start_ok(start_ok), .sel(sel), .off(off), .step(step), .cap(cap),
        .n(n), .contig(contig), .addr(addr), .total(total), .stride0(stride0), .left0(left0)
    );

    // the table as the hardware holds it (from the writes it took)
    reg  [AW-1:0]       h_base [0:7];
    reg  signed [17:0]  h_str  [0:7][0:3];
    reg  [15:0]         h_bnd  [0:7][0:3];

    // each walker's reference, latched at its start
    reg  [AW-1:0]       r_org  [0:1];
    reg  signed [17:0]  r_str  [0:1][0:3];
    reg  [15:0]         r_bnd  [0:1][0:3];
    integer             r_idx  [0:1][0:3];
    reg                 r_run  [0:1];
    integer             r_age  [0:1];
    integer             r_done [0:1];

    integer errs, checks, cyc, w, k, d, nst, fl, lft, f;
    reg  [AW-1:0]  want_a;
    reg  [31:0]    s01, s23;
    reg            c32;
    reg  [1:0]     want_start;
    reg  [2:0]     want_sel [0:1];
    reg  [AW-1:0]  want_off [0:1];
    reg  signed [17:0] ns;
    reg  [15:0]    nb;

    function [15:0] bnd1(input [15:0] x);
        bnd1 = (x == 16'd0) ? 16'd1 : x;
    endfunction

    task chk(input [63:0] got, input [63:0] want, input [127:0] what, input integer ww);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errs = errs + 1;
                if (errs <= 12) $display("  FAIL cyc %0d walker %0d %0s got %h want %h", cyc, ww, what, got, want);
            end
        end
    endtask

    // a random table word for entry e, field f (0 base, 1..4 dimension f-1)
    task drive_wr(input integer e, input integer fd);
        begin
            wr_en = 1'b1; wr_ad = e; wr_fld = fd;
            case ($random & 7)
                0: nb = 16'd0;
                1, 2: nb = 16'd1;
                default: nb = 1 + ($random & 7);
            endcase
            ns = (fd == 1 && ($random & 1)) ? 18'sd32 : $signed({$random} % 4096) - 18'sd2048;
            wr_val = (fd == 0) ? ({$random, $random} & {AW{1'b1}})
                               : {{(AW-34){1'b0}}, ns, nb};
        end
    endtask

    initial begin
        for (k = 0; k < 8; k = k + 1) begin
            h_base[k] = 0;
            for (d = 0; d < 4; d = d + 1) begin h_str[k][d] = 0; h_bnd[k][d] = 16'd1; end
        end
    end
    always @(posedge clk) begin
        if (!rst && wr_en) begin
            if (wr_fld == 3'd0) h_base[wr_ad] <= wr_val;
            else if (wr_fld <= 3'd4) begin
                h_str[wr_ad][wr_fld - 3'd1] <= wr_val[33:16];
                h_bnd[wr_ad][wr_fld - 3'd1] <= wr_val[15:0];
            end
        end
    end

    initial begin
        errs = 0; checks = 0;
        repeat (4) @(posedge clk);
        rst = 0;
        for (k = 0; k < 8; k = k + 1) begin
            for (f = 0; f < 5; f = f + 1) begin
                @(negedge clk); drive_wr(k, f);
            end
        end
        @(negedge clk); wr_en = 0;
        for (w = 0; w < 2; w = w + 1) begin r_run[w] = 0; r_done[w] = 0; r_age[w] = 0; end

        cyc = 0;
        while ((r_done[0] < NWALK || r_done[1] < NWALK || r_run[0] || r_run[1]) && cyc < 4000000) begin
            @(negedge clk);
            cyc = cyc + 1;
            // ---- this cycle's outputs, and the steps taken in it
            step = 2'b00;
            for (w = 0; w < 2; w = w + 1) begin
                if (r_run[w] && r_age[w] >= 3) begin
                    s01 = bnd1(r_bnd[w][0]) * bnd1(r_bnd[w][1]);
                    s23 = bnd1(r_bnd[w][2]) * bnd1(r_bnd[w][3]);
                    s01 = (s01 > 32'hFFFF) ? 32'hFFFF : s01;
                    s23 = (s23 > 32'hFFFF) ? 32'hFFFF : s23;
                    chk(total[w*32 +: 32], s01 * s23, "total", w);
                end
                if (r_run[w] && r_age[w] >= 3 && ($random & 3) != 0) begin
                    step[w] = 1'b1;
                    cap[w*16 +: 16] = 1 + ($random & 15);
                end
            end
            #1;     // `n` is combinational in `cap`
            for (w = 0; w < 2; w = w + 1) begin
                if (step[w]) begin
                    want_a = r_org[w];
                    for (d = 0; d < 4; d = d + 1) begin
                        want_a = want_a + {{(AW-18){r_str[w][d][17]}}, r_str[w][d]} * r_idx[w][d];
                    end
                    c32 = (r_str[w][0] == 18'sd32);
                    lft = bnd1(r_bnd[w][0]) - r_idx[w][0];
                    chk(addr[w*AW +: AW], want_a, "addr", w);
                    chk(n[w*16 +: 16], !c32 ? 1 : (cap[w*16 +: 16] < lft) ? cap[w*16 +: 16] : lft, "n", w);
                    chk(left0[w*16 +: 16], lft, "left0", w);
                    chk(stride0[w*18 +: 18], {46'd0, r_str[w][0][17:0]}, "stride0", w);
                    chk(contig[w], c32, "contig", w);
                end
            end
            // ---- an idle walker starts at random; both may ask in one cycle
            want_start = 2'b00;
            for (w = 0; w < 2; w = w + 1) begin
                if (!r_run[w] && r_done[w] < NWALK && ($random & 3) == 0) begin
                    want_start[w] = 1'b1;
                    want_sel[w] = $random;
                    want_off[w] = {$random, $random} & {AW{1'b1}};
                end
            end
            start = want_start;
            sel = {want_sel[1], want_sel[0]};
            off = {want_off[1], want_off[0]};
            // ---- a table word rewritten at random, running walkers or not
            wr_en = 0;
            if (($random & 7) == 0) drive_wr($random & 7, {$random} % 5);
            // a start reads the table as it stands before this edge's write
            for (w = 1; w >= 0; w = w - 1) begin
                if (want_start[w] && !(w == 0 && want_start[1])) begin
                    r_org[w] = h_base[want_sel[w]] + want_off[w];
                    for (d = 0; d < 4; d = d + 1) begin
                        r_str[w][d] = h_str[want_sel[w]][d];
                        r_bnd[w][d] = h_bnd[want_sel[w]][d];
                    end
                end
            end
            @(posedge clk);
            #1;
            // ---- the reference moves on this edge
            for (w = 0; w < 2; w = w + 1) begin
                r_age[w] = r_age[w] + 1;
                if (step[w]) begin
                    c32 = (r_str[w][0] == 18'sd32);
                    lft = bnd1(r_bnd[w][0]) - r_idx[w][0];
                    nst = !c32 ? 1 : (cap[w*16 +: 16] < lft) ? cap[w*16 +: 16] : lft;
                    r_idx[w][0] = r_idx[w][0] + nst;
                    fl = 1;
                    for (d = 0; d < 4; d = d + 1) begin
                        if (fl && r_idx[w][d] == bnd1(r_bnd[w][d])) begin
                            r_idx[w][d] = 0;
                            if (d < 3) r_idx[w][d+1] = r_idx[w][d+1] + 1;
                            else begin r_run[w] = 0; r_done[w] = r_done[w] + 1; end
                        end else begin
                            fl = 0;
                        end
                    end
                end
            end
            for (w = 1; w >= 0; w = w - 1) begin
                if (want_start[w] && !(w == 0 && want_start[1])) begin
                    r_run[w] = 1; r_age[w] = 0;
                    for (d = 0; d < 4; d = d + 1) r_idx[w][d] = 0;
                end
            end
        end
        checks = checks + 1;
        if (r_done[0] < NWALK || r_done[1] < NWALK) begin
            errs = errs + 1;
            $display("  FAIL only %0d / %0d walks finished", r_done[0], r_done[1]);
        end
        if (errs == 0) $display("  PASS -- v2_agu: %0d walks per walker, %0d checks", NWALK, checks);
        else           $display("  FAIL -- v2_agu: %0d errors of %0d checks", errs, checks);
        $finish;
    end
endmodule

`default_nettype wire
