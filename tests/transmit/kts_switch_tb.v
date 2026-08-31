// A three-port switch as a line: port 0 owns dst 0, port 1 owns dst 1, port 2
// everything above. Every input sends packets (a header and 0..3 payload
// flits) to random destinations on both VCs; every output checks that each
// packet arrives whole, in order per (source, VC), with the header's fields
// and the payload's {src, dst, tag, index} intact, and that every packet sent
// was received somewhere.

`timescale 1ns / 1ps
`default_nettype none

module kts_switch_tb;
    localparam integer W  = 64;
    localparam integer VC = 2;
    localparam integer K  = 3;
    localparam integer D  = 16;
    localparam integer CN_W = 4;
    localparam integer VCW = 1;
`include "kts_pkt.vh"

    reg clk = 1'b0;
    reg rst = 1'b1;
    always begin
        #1.667 clk = ~clk;
    end
    integer errors = 0;

    // ---- the switch -----------------------------------------------------------
    wire [K-1:0]        i_valid, i_last, i_crd_valid, o_valid, o_last, o_crd_valid;
    wire [K*VCW-1:0]    i_vc, i_crd_vc, o_vc, o_crd_vc;
    wire [K*W-1:0]      i_flit, o_flit;
    wire [K*CN_W-1:0]   i_crd_n, o_crd_n;

    kts_switch #(.W(W), .VC(VC), .K(K), .D(D), .CMAX(D), .CN_W(CN_W)) u_sw (
        .clk(clk), .rst(rst),
        .i_valid(i_valid), .i_vc(i_vc), .i_last(i_last), .i_flit(i_flit),
        .i_crd_valid(i_crd_valid), .i_crd_vc(i_crd_vc), .i_crd_n(i_crd_n),
        .o_valid(o_valid), .o_vc(o_vc), .o_last(o_last), .o_flit(o_flit),
        .o_crd_valid(o_crd_valid), .o_crd_vc(o_crd_vc), .o_crd_n(o_crd_n)
    );

    // sent[src][dst][vc] and got[src][dst][vc]
    integer sent [0:K-1][0:3][0:VC-1];
    integer got  [0:K-1][0:3][0:VC-1];
    reg     quiet;

    function [W-1:0] hdr;
        input [7:0] dst; input [7:0] src; input [7:0] tag; input [15:0] len; input [3:0] vc;
        begin
            hdr = {W{1'b0}};
            hdr[KTS_H_KIND_LSB +: KTS_H_KIND_W] = KTS_K_DATA;
            hdr[KTS_H_VC_LSB +: KTS_H_VC_W]     = vc;
            hdr[KTS_H_DST_LSB +: KTS_H_DST_W]   = dst;
            hdr[KTS_H_SRC_LSB +: KTS_H_SRC_W]   = src;
            hdr[KTS_H_LEN_LSB +: KTS_H_LEN_W]   = len;
            hdr[KTS_H_TAG_LSB +: KTS_H_TAG_W]   = tag;
        end
    endfunction

    genvar p, v;
    generate
    for (p = 0; p < K; p = p + 1) begin : g_src
        reg  [VC-1:0]   req_valid, req_last;
        reg  [VC*W-1:0] req_flit;
        wire [VC-1:0]   req_take;
        // a tag sequence per (vc, output port), since each output checks
        // its own order per source
        reg  [7:0]      tag  [0:VC-1][0:2];
        reg  [7:0]      dst  [0:VC-1];
        function [1:0] cls;
            input [7:0] d;
            begin
                cls = (d >= 8'd2) ? 2'd2 : d[1:0];
            end
        endfunction
        reg  [1:0]      idx  [0:VC-1];      // payload flit index, 0 = header
        reg  [1:0]      plen [0:VC-1];      // payload flits in this packet
        wire [VC*($clog2(D)+1)-1:0] credits;
        kts_tx #(.W(W), .VC(VC), .CMAX(D), .CN_W(CN_W)) u_tx (
            .clk(clk), .rst(rst),
            .req_valid(req_valid), .req_last(req_last), .req_flit(req_flit),
            .req_take(req_take),
            .tx_valid(i_valid[p]), .tx_vc(i_vc[p*VCW +: VCW]),
            .tx_last(i_last[p]), .tx_flit(i_flit[p*W +: W]),
            .crd_valid(i_crd_valid[p]), .crd_vc(i_crd_vc[p*VCW +: VCW]),
            .crd_n(i_crd_n[p*CN_W +: CN_W]),
            .credits(credits)
        );
        integer k;
        always @(posedge clk) begin
            if (rst) begin
                req_valid <= {VC{1'b0}};
                req_last  <= {VC{1'b0}};
                for (k = 0; k < VC; k = k + 1) begin
                    tag[k][0] <= 8'd0;
                    tag[k][1] <= 8'd0;
                    tag[k][2] <= 8'd0;
                    dst[k]  <= 8'd0;
                    idx[k]  <= 2'd0;
                    plen[k] <= 2'd0;
                    req_flit[k*W +: W] <= hdr(8'd0, p[7:0], 8'd0, 16'd0, k[3:0]);
                    req_last[k] <= 1'b1;
                end
            end
            else begin
                for (k = 0; k < VC; k = k + 1) begin
                    if (req_take[k]) begin
                        if (req_last[k]) begin
                            // the packet just finished: count it, start the next
                            sent[p][dst[k]][k] = sent[p][dst[k]][k] + 1;
                            tag[k][cls(dst[k])] <= tag[k][cls(dst[k])] + 8'd1;
                            dst[k]  <= $urandom % 4;
                            plen[k] <= $urandom % 4;
                            idx[k]  <= 2'd0;
                            req_valid[k] <= 1'b0;
                        end
                        else begin
                            idx[k] <= idx[k] + 2'd1;
                            req_flit[k*W +: W] <= {p[15:0], dst[k], tag[k][cls(dst[k])], 16'd0, 14'd0, idx[k] + 2'd1};
                            req_last[k] <= (idx[k] + 2'd1 == plen[k]);
                            req_valid[k] <= 1'b1;
                        end
                    end
                    else if (!req_valid[k] && !quiet && ($urandom % 3 != 0)) begin
                        // offer a header
`ifdef KTS_SW_TRACE
                        $display("%0t src %0d vc %0d: header dst %0d tag %0d plen %0d", $time, p, k, dst[k], tag[k][cls(dst[k])], plen[k]);
`endif
                        req_flit[k*W +: W] <= hdr(dst[k], p[7:0], tag[k][cls(dst[k])], {14'd0, plen[k]} * (W / 8), k[3:0]);
                        req_last[k]  <= (plen[k] == 2'd0);
                        req_valid[k] <= 1'b1;
                    end
                end
            end
        end
    end

    for (p = 0; p < K; p = p + 1) begin : g_snk
        wire [VC-1:0]   out_valid, out_last;
        wire [VC*W-1:0] out_flit;
        reg  [VC-1:0]   out_pop;
        kts_rx #(.W(W), .VC(VC), .D(D), .CN_W(CN_W)) u_rx (
            .clk(clk), .rst(rst),
            .rx_valid(o_valid[p]), .rx_vc(o_vc[p*VCW +: VCW]),
            .rx_last(o_last[p]), .rx_flit(o_flit[p*W +: W]),
            .out_valid(out_valid), .out_last(out_last), .out_flit(out_flit),
            .out_pop(out_pop),
            .crd_valid(o_crd_valid[p]), .crd_vc(o_crd_vc[p*VCW +: VCW]),
            .crd_n(o_crd_n[p*CN_W +: CN_W])
        );
        // per (src, vc): the next expected tag; per vc: the packet in flight
        reg [7:0]  exp_tag [0:K-1][0:VC-1];
        reg        inpkt   [0:VC-1];
        reg [7:0]  cur_src [0:VC-1];
        reg [7:0]  cur_dst [0:VC-1];
        reg [7:0]  cur_tag [0:VC-1];
        reg [1:0]  cur_idx [0:VC-1];
        integer k, s;
        always @(posedge clk) begin
            if (rst) begin
                out_pop <= {VC{1'b0}};
                for (k = 0; k < VC; k = k + 1) begin
                    inpkt[k] <= 1'b0;
                    for (s = 0; s < K; s = s + 1) begin
                        exp_tag[s][k] <= 8'd0;
                    end
                end
            end
            else begin
                for (k = 0; k < VC; k = k + 1) begin
                    out_pop[k] <= ($urandom % 3 != 0);
                    if (out_pop[k] && out_valid[k]) begin
                        if (!inpkt[k]) begin
                            // a header: right port, right vc, in order per source
                            cur_src[k] <= out_flit[k*W + KTS_H_SRC_LSB +: 8];
                            cur_dst[k] <= out_flit[k*W + KTS_H_DST_LSB +: 8];
                            cur_tag[k] <= out_flit[k*W + KTS_H_TAG_LSB +: 8];
                            cur_idx[k] <= 2'd0;
                            if (!(((p == 0) && (out_flit[k*W + KTS_H_DST_LSB +: 8] == 8'd0))
                                  || ((p == 1) && (out_flit[k*W + KTS_H_DST_LSB +: 8] == 8'd1))
                                  || ((p == 2) && (out_flit[k*W + KTS_H_DST_LSB +: 8] >= 8'd2)))) begin
                                $display("%0t ERROR port %0d got dst %0d", $time, p, out_flit[k*W + KTS_H_DST_LSB +: 8]);
                                errors = errors + 1;
                            end
                            if (out_flit[k*W + KTS_H_VC_LSB +: 4] != k) begin
                                $display("%0t ERROR port %0d VC %0d: header says VC %0d", $time, p, k, out_flit[k*W + KTS_H_VC_LSB +: 4]);
                                errors = errors + 1;
                            end
                            if (out_flit[k*W + KTS_H_TAG_LSB +: 8] != exp_tag[out_flit[k*W + KTS_H_SRC_LSB +: 8]][k]) begin
                                $display("%0t ERROR port %0d VC %0d from %0d: tag %0d, expected %0d", $time, p, k,
                                         out_flit[k*W + KTS_H_SRC_LSB +: 8], out_flit[k*W + KTS_H_TAG_LSB +: 8],
                                         exp_tag[out_flit[k*W + KTS_H_SRC_LSB +: 8]][k]);
                                errors = errors + 1;
                            end
                            exp_tag[out_flit[k*W + KTS_H_SRC_LSB +: 8]][k] <= exp_tag[out_flit[k*W + KTS_H_SRC_LSB +: 8]][k] + 8'd1;
                            inpkt[k] <= !out_last[k];
                            if (out_last[k]) begin
                                got[out_flit[k*W + KTS_H_SRC_LSB +: 8]][out_flit[k*W + KTS_H_DST_LSB +: 8]][k]
                                    = got[out_flit[k*W + KTS_H_SRC_LSB +: 8]][out_flit[k*W + KTS_H_DST_LSB +: 8]][k] + 1;
                            end
                        end
                        else begin
                            if (out_flit[k*W +: W] !== {8'd0, cur_src[k], cur_dst[k], cur_tag[k], 16'd0, 14'd0, cur_idx[k] + 2'd1}) begin
                                $display("%0t ERROR port %0d VC %0d: payload %h does not match packet (%0d -> %0d tag %0d idx %0d)",
                                         $time, p, k, out_flit[k*W +: W], cur_src[k], cur_dst[k], cur_tag[k], cur_idx[k] + 2'd1);
                                errors = errors + 1;
                            end
                            cur_idx[k] <= cur_idx[k] + 2'd1;
                            inpkt[k]   <= !out_last[k];
                            if (out_last[k]) begin
                                got[cur_src[k]][cur_dst[k]][k] = got[cur_src[k]][cur_dst[k]][k] + 1;
                            end
                        end
                    end
                end
            end
        end
    end
    endgenerate

    integer a, b, c;
    integer total;
    initial begin
        for (a = 0; a < K; a = a + 1) begin
            for (b = 0; b < 4; b = b + 1) begin
                for (c = 0; c < VC; c = c + 1) begin
                    sent[a][b][c] = 0;
                    got[a][b][c]  = 0;
                end
            end
        end
        quiet = 1'b0;
        repeat (5) @(posedge clk);
        rst <= 1'b0;
        repeat (6000) @(posedge clk);
        quiet = 1'b1;
        repeat (1500) @(posedge clk);
        total = 0;
        for (a = 0; a < K; a = a + 1) begin
            for (b = 0; b < 4; b = b + 1) begin
                for (c = 0; c < VC; c = c + 1) begin
                    total = total + sent[a][b][c];
                    if (sent[a][b][c] != got[a][b][c]) begin
                        $display("ERROR %0d -> dst %0d VC %0d: %0d sent, %0d received", a, b, c, sent[a][b][c], got[a][b][c]);
                        errors = errors + 1;
                    end
                end
            end
        end
        $display("kts_switch_tb: %0d packets through the 3-port line", total);
        if (errors == 0) begin
            $display("PASS");
        end
        else begin
            $display("FAIL: %0d error(s)", errors);
        end
        $finish;
    end

endmodule

`default_nettype wire
