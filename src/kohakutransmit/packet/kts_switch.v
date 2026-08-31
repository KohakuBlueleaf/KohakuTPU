// A K-port packet switch: a receiving end per input, a sending end per output,
// and per virtual channel a K:1 arbiter on every output. Routing is by the
// header's `dst` against a range per output port (LO[o] <= dst <= HI[o]), so
// the same module is a line, a ring or a crossbar depending on the ranges.
// A packet is switched whole (cut-through after its header), a VC never
// changes inside, and nothing here waits on a wire.

`default_nettype none

module kts_switch #(
    parameter integer W     = 288,
    parameter integer VC    = 2,
    parameter integer K     = 3,
    parameter integer D     = 32,
    parameter integer CMAX  = 64,
    parameter integer CN_W  = 4,
    parameter         MEM   = "distributed",
    // Output port o takes dst in [LO[o], HI[o]]; the first match wins.
    parameter [K*8-1:0] LO  = {8'd2, 8'd1, 8'd0},
    parameter [K*8-1:0] HI  = {8'd255, 8'd1, 8'd0},
    parameter integer VCW   = (VC <= 1) ? 1 : $clog2(VC),
    parameter integer KW    = (K <= 1) ? 1 : $clog2(K)
)(
    input  wire                clk,
    input  wire                rst,

    input  wire [K-1:0]        i_valid,
    input  wire [K*VCW-1:0]    i_vc,
    input  wire [K-1:0]        i_last,
    input  wire [K*W-1:0]      i_flit,
    output wire [K-1:0]        i_crd_valid,
    output wire [K*VCW-1:0]    i_crd_vc,
    output wire [K*CN_W-1:0]   i_crd_n,

    output wire [K-1:0]        o_valid,
    output wire [K*VCW-1:0]    o_vc,
    output wire [K-1:0]        o_last,
    output wire [K*W-1:0]      o_flit,
    input  wire [K-1:0]        o_crd_valid,
    input  wire [K*VCW-1:0]    o_crd_vc,
    input  wire [K*CN_W-1:0]   o_crd_n
);
`include "kts_pkt.vh"

    // ---- inputs: one receiving end each, then a TWO-ENTRY HEAD QUEUE per
    // (input, vc) holding flits with the output each goes to, so the arbiters
    // read registers and the FIFO's read enable depends on a count, not on
    // this cycle's arbitration. Read straight off the FIFO the path was FIFO
    // -> dst compare -> arbitration -> credit -> pop -> owed counter, 16
    // levels, -0.596 ns at W=1024; with one head register the FIFO's flag
    // logic sat behind the arbitration instead, 11 levels, +0.306 at W=288.
    wire [K*VC-1:0]   fv, fl, fpop;          // the FIFO heads
    wire [K*VC*W-1:0] ff;
    wire [K*VC-1:0]   hv, hl;                // the queue heads
    wire [K*VC*W-1:0] hf;
    wire [K*VC-1:0]   hpop;                  // an arbiter took the head
    wire [KW-1:0]     route [0:K*VC-1];      // output of the head's packet
    reg  [K*VC-1:0]   inpkt;                 // a packet is in flight (route locked)
    reg  [KW-1:0]     lock  [0:K*VC-1];      // its output

    function [KW-1:0] pick_out;
        input [7:0] dst;
        integer o;
        reg found;
        begin
            pick_out = {KW{1'b0}};
            found = 1'b0;
            for (o = 0; o < K; o = o + 1) begin
                if (!found && (dst >= LO[o*8 +: 8]) && (dst <= HI[o*8 +: 8])) begin
                    pick_out = o[KW-1:0];
                    found = 1'b1;
                end
            end
        end
    endfunction

    genvar i, v, o;
    generate
    for (i = 0; i < K; i = i + 1) begin : g_in
        kts_rx #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .MEM(MEM)) u_rx (
            .clk(clk), .rst(rst),
            .rx_valid(i_valid[i]), .rx_vc(i_vc[i*VCW +: VCW]),
            .rx_last(i_last[i]), .rx_flit(i_flit[i*W +: W]),
            .out_valid(fv[i*VC +: VC]), .out_last(fl[i*VC +: VC]),
            .out_flit(ff[i*VC*W +: VC*W]), .out_pop(fpop[i*VC +: VC]),
            .crd_valid(i_crd_valid[i]), .crd_vc(i_crd_vc[i*VCW +: VCW]),
            .crd_n(i_crd_n[i*CN_W +: CN_W])
        );
        for (v = 0; v < VC; v = v + 1) begin : g_iv
            localparam integer X = i * VC + v;
            reg  [1:0]    cnt;
            reg           wp, rp;
            reg  [W-1:0]  qf [0:1];
            reg           ql [0:1];
            reg  [KW-1:0] qr [0:1];
            wire          load  = fv[X] && (cnt != 2'd2);
            wire [W-1:0]  fhead = ff[X*W +: W];
            wire [KW-1:0] want  = inpkt[X] ? lock[X]
                                           : pick_out(fhead[KTS_H_DST_LSB +: 8]);
            assign fpop[X]        = load;
            assign hv[X]          = (cnt != 2'd0);
            assign hl[X]          = ql[rp];
            assign hf[X*W +: W]   = qf[rp];
            assign route[X]       = qr[rp];
            always @(posedge clk) begin
                if (rst) begin
                    cnt <= 2'd0; wp <= 1'b0; rp <= 1'b0;
                    inpkt[X] <= 1'b0;
                end
                else begin
                    cnt <= cnt + {1'b0, load} - {1'b0, hpop[X]};
                    if (load) begin
                        wp       <= !wp;
                        inpkt[X] <= !fl[X];
                        lock[X]  <= want;
                    end
                    if (hpop[X]) begin
                        rp <= !rp;
                    end
                end
                if (load) begin
                    qf[wp] <= fhead;
                    ql[wp] <= fl[X];
                    qr[wp] <= want;
                end
            end
        end
    end
    endgenerate

    // ---- outputs: per (output, vc) a locked round-robin over the inputs -----
    // Every output drives its own pop vector; an input head is wanted by one
    // output at a time, so the OR of them is the pop.
    wire [K*K*VC-1:0] pop_all;
    reg  [K*VC-1:0]   hpop_r;
    integer oo, xx;
    always @(*) begin
        hpop_r = {(K*VC){1'b0}};
        for (oo = 0; oo < K; oo = oo + 1) begin
            for (xx = 0; xx < K * VC; xx = xx + 1) begin
                hpop_r[xx] = hpop_r[xx] | pop_all[oo*K*VC + xx];
            end
        end
    end
    assign hpop = hpop_r;

    generate
    for (o = 0; o < K; o = o + 1) begin : g_out
        wire [VC-1:0]   tv, tl, take;
        wire [VC*W-1:0] tf;
        for (v = 0; v < VC; v = v + 1) begin : g_ov
            // requests: input i's VC v head wants this output
            wire [K-1:0] req;
            for (i = 0; i < K; i = i + 1) begin : g_r
                localparam integer X = i * VC + v;
                assign req[i] = hv[X] && (route[X] == o[KW-1:0]);
            end
            reg          locked;
            reg [KW-1:0] cur;
            reg [KW-1:0] rr;
            // round-robin pick among req starting at rr
            wire [2*K-1:0] dbl = {req, req} >> rr;
            wire [K-1:0]   rot = dbl[K-1:0];
            wire [K-1:0]   low = rot & (~rot + {{(K-1){1'b0}}, 1'b1});
            reg  [KW-1:0]  low_ix;
            integer q;
            always @(*) begin
                low_ix = {KW{1'b0}};
                for (q = 0; q < K; q = q + 1) begin
                    if (low[q]) begin
                        low_ix = low_ix | q[KW-1:0];
                    end
                end
            end
            wire [KW:0]   sum  = {1'b0, low_ix} + {1'b0, rr};
            wire [KW-1:0] nxt  = (sum >= K) ? (sum[KW-1:0] - K[KW-1:0]) : sum[KW-1:0];
            wire [KW-1:0] sel  = locked ? cur : nxt;
            wire          have = locked ? req[cur] : (|req);
            // A COMPARE PER INPUT. `hf[(sel*VC + v)*W +: W]` is a dynamic
            // part-select over the whole K*VC*W vector, so the tool builds a
            // barrel select across every input AND every VC instead of the
            // K:1 over one VC that this is.
            reg [W-1:0] sf;
            reg         sl;
            integer c;
            always @(*) begin
                sf = hf[(0*VC + v)*W +: W];
                sl = hl[0*VC + v];
                for (c = 1; c < K; c = c + 1) begin
                    if (sel == c[KW-1:0]) begin
                        sf = hf[(c*VC + v)*W +: W];
                        sl = hl[c*VC + v];
                    end
                end
            end
            assign tv[v]          = have;
            assign tl[v]          = sl;
            assign tf[v*W +: W]   = sf;
            // the pop of the chosen input's head
            for (i = 0; i < K; i = i + 1) begin : g_p
                assign pop_all[o*K*VC + i*VC + v] = have && take[v] && (sel == i[KW-1:0]);
            end
            always @(posedge clk) begin
                if (rst) begin
                    locked <= 1'b0;
                    cur    <= {KW{1'b0}};
                    rr     <= {KW{1'b0}};
                end
                else if (have && take[v]) begin
                    locked <= !sl;
                    cur    <= sel;
                    if (sl) begin
                        rr <= (sel == K - 1) ? {KW{1'b0}} : sel + 1'b1;
                    end
                end
            end
        end
        wire [VC*($clog2(CMAX)+1)-1:0] credits_unused;
        kts_tx #(.W(W), .VC(VC), .CMAX(CMAX), .CN_W(CN_W)) u_tx (
            .clk(clk), .rst(rst),
            .req_valid(tv), .req_last(tl), .req_flit(tf), .req_take(take),
            .tx_valid(o_valid[o]), .tx_vc(o_vc[o*VCW +: VCW]),
            .tx_last(o_last[o]), .tx_flit(o_flit[o*W +: W]),
            .crd_valid(o_crd_valid[o]), .crd_vc(o_crd_vc[o*VCW +: VCW]),
            .crd_n(o_crd_n[o*CN_W +: CN_W]),
            .credits(credits_unused)
        );
    end
    endgenerate

endmodule

`default_nettype wire
