// kx_kedge -- one partition's local port on the Xache's KTS line: chain
// flits in ({dst,typ,src,pay} REQ / {dst,kind,src,pay} RSP), KTS packets
// out through a kts_tx, and the switch's local output landed by a kts_rx
// and translated back. AW -> WRREQ header, W -> data flit (kts last = AXI
// wlast), AR -> RDREQ; R/B -> one-flit RDRSP/WRRSP with the word in `user`.
// dst on the wire is the PARTITION; the home/master index rides in tag/dst
// fields of the reconstructed head.

`default_nettype none

module kx_kedge #(
    parameter integer W       = 584,
    parameter integer D       = 32,
    parameter         MEM     = "block",
    parameter integer CN_W    = 4,
    parameter integer HIDX_W  = 2,
    parameter integer MIDX_W  = 2,
    parameter integer PW      = 2,
    parameter integer QPW     = 577,
    parameter integer ARQW    = 59,
    parameter integer AWQW    = 57,
    parameter integer WPL     = 577,
    parameter integer SPW     = 521,
    parameter integer RSLOT   = HIDX_W + 2 + MIDX_W + QPW,
    parameter integer SSLOT   = MIDX_W + 1 + HIDX_W + SPW,
    parameter [255:0] HPV     = 256'h0,     // partition of home h (PW each)
    parameter [255:0] MPV     = 256'h0      // partition of master m
)(
    input  wire              clk,
    input  wire              rstn,

    // this partition's merged offers, chain-flit format
    input  wire              q_valid,
    output wire              q_ready,
    input  wire [RSLOT-1:0]  q_flit,
    input  wire              s_valid,
    output wire              s_ready,
    input  wire [SSLOT-1:0]  s_flit,

    // reconstructed heads for the consume demux, chain-flit format
    output wire              hq_valid,
    input  wire              hq_ready,
    output wire [RSLOT-1:0]  hq_flit,
    output wire              hs_valid,
    input  wire              hs_ready,
    output wire [SSLOT-1:0]  hs_flit,

    // the switch's local input / output ports
    output wire              li_valid,
    output wire              li_vc,
    output wire              li_last,
    output wire [W-1:0]      li_flit,
    input  wire              li_crd_valid,
    input  wire              li_crd_vc,
    input  wire [CN_W-1:0]   li_crd_n,
    input  wire              lo_valid,
    input  wire              lo_vc,
    input  wire              lo_last,
    input  wire [W-1:0]      lo_flit,
    output wire              lo_crd_valid,
    output wire              lo_crd_vc,
    output wire [CN_W-1:0]   lo_crd_n
);
`include "kts_pkt.vh"
    localparam integer U = KTS_H_USER_LSB;

    // ---- TX: translate the two offers, kts_tx arbitrates and credits ------
    wire [HIDX_W-1:0] q_dst = q_flit[RSLOT-1 -: HIDX_W];
    wire [1:0]        q_typ = q_flit[RSLOT-1-HIDX_W -: 2];
    wire [MIDX_W-1:0] q_src = q_flit[RSLOT-1-HIDX_W-2 -: MIDX_W];
    wire [QPW-1:0]    q_pay = q_flit[QPW-1:0];
    wire [PW-1:0]     q_dp  = HPV[q_dst*PW +: PW];
    reg  [W-1:0] q_kf;
    always @(*) begin
        q_kf = {W{1'b0}};
        q_kf[KTS_H_VC_LSB +: 4]   = 4'd0;
        q_kf[KTS_H_DST_LSB +: 8]  = {{(8-PW){1'b0}}, q_dp};
        q_kf[KTS_H_SRC_LSB +: 8]  = {{(8-MIDX_W){1'b0}}, q_src};
        q_kf[KTS_H_TAG_LSB +: 8]  = {{(8-HIDX_W){1'b0}}, q_dst};
        if (q_typ == 2'd1) begin
            // a data flit is {data, strb}; wlast rides the kts last wire
            q_kf = {{(W-WPL+1){1'b0}}, q_pay[WPL-1:1]};
        end else if (q_typ == 2'd0) begin
            q_kf[KTS_H_KIND_LSB +: 4] = KTS_K_WRREQ;
            q_kf[U +: AWQW] = q_pay[AWQW-1:0];
        end else begin
            q_kf[KTS_H_KIND_LSB +: 4] = KTS_K_RDREQ;
            q_kf[U +: ARQW] = q_pay[ARQW-1:0];
        end
    end
    wire q_hdr  = (q_typ != 2'd1);
    wire q_last = (q_typ == 2'd1) ? q_pay[0] : (q_typ == 2'd2);

    wire [MIDX_W-1:0] s_dst = s_flit[SSLOT-1 -: MIDX_W];
    wire              s_knd = s_flit[SSLOT-1-MIDX_W];
    wire [HIDX_W-1:0] s_src = s_flit[SSLOT-1-MIDX_W-1 -: HIDX_W];
    wire [SPW-1:0]    s_pay = s_flit[SPW-1:0];
    wire [PW-1:0]     s_dp  = MPV[s_dst*PW +: PW];
    reg  [W-1:0] s_kf;
    always @(*) begin
        s_kf = {W{1'b0}};
        s_kf[KTS_H_KIND_LSB +: 4] = s_knd ? KTS_K_WRRSP : KTS_K_RDRSP;
        s_kf[KTS_H_VC_LSB +: 4]   = 4'd1;
        s_kf[KTS_H_DST_LSB +: 8]  = {{(8-PW){1'b0}}, s_dp};
        s_kf[KTS_H_SRC_LSB +: 8]  = {{(8-HIDX_W){1'b0}}, s_src};
        s_kf[KTS_H_TAG_LSB +: 8]  = {{(8-MIDX_W){1'b0}}, s_dst};
        s_kf[U +: SPW] = s_pay;
    end

    wire [1:0] take;
    kts_tx #(.W(W), .VC(2), .CMAX(D), .CN_W(CN_W)) u_tx (
        .clk(clk), .rst(!rstn),
        .req_valid({s_valid, q_valid}),
        .req_last({1'b1, q_last}),
        .req_flit({s_kf, q_kf}),
        .req_take(take),
        .tx_valid(li_valid), .tx_vc(li_vc), .tx_last(li_last), .tx_flit(li_flit),
        .crd_valid(li_crd_valid), .crd_vc(li_crd_vc), .crd_n(li_crd_n),
        .credits());
    assign q_ready = take[0];
    assign s_ready = take[1];

    // ---- RX: land, then rebuild the chain-flit heads ----------------------
    wire [1:0]     ov, ol, opop;
    wire [2*W-1:0] of;
    kts_rx #(.W(W), .VC(2), .D(D), .CN_W(CN_W), .MEM(MEM)) u_rx (
        .clk(clk), .rst(!rstn),
        .rx_valid(lo_valid), .rx_vc(lo_vc), .rx_last(lo_last), .rx_flit(lo_flit),
        .out_valid(ov), .out_last(ol), .out_flit(of), .out_pop(opop),
        .crd_valid(lo_crd_valid), .crd_vc(lo_crd_vc), .crd_n(lo_crd_n));

    // VC0: a header sets (h, m); its data flits reuse them until `last`
    wire [W-1:0] r0 = of[0 +: W];
    reg          inw;
    reg [HIDX_W-1:0] cur_h;
    reg [MIDX_W-1:0] cur_m;
    wire [3:0]        h_knd = r0[KTS_H_KIND_LSB +: 4];
    wire [HIDX_W-1:0] h_tag = r0[KTS_H_TAG_LSB +: HIDX_W];
    wire [MIDX_W-1:0] h_src = r0[KTS_H_SRC_LSB +: MIDX_W];
    wire [1:0] r_typ = inw ? 2'd1 : ((h_knd == KTS_K_RDREQ) ? 2'd2 : 2'd0);
    reg [QPW-1:0] r_pay;
    always @(*) begin
        if (inw) begin
            r_pay = {{(QPW-WPL){1'b0}}, r0[WPL-2:0], ol[0]};
        end else if (h_knd == KTS_K_RDREQ) begin
            r_pay = {{(QPW-ARQW){1'b0}}, r0[U +: ARQW]};
        end else begin
            r_pay = {{(QPW-AWQW){1'b0}}, r0[U +: AWQW]};
        end
    end
    assign hq_valid = ov[0];
    assign hq_flit  = {(inw ? cur_h : h_tag), r_typ, (inw ? cur_m : h_src), r_pay};
    assign opop[0]  = hq_ready;
    always @(posedge clk) begin
        if (!rstn) begin
            inw <= 1'b0; cur_h <= {HIDX_W{1'b0}}; cur_m <= {MIDX_W{1'b0}};
        end else if (ov[0] && hq_ready) begin
            if (!inw && h_knd == KTS_K_WRREQ) begin
                inw <= 1'b1; cur_h <= h_tag; cur_m <= h_src;
            end else if (inw && ol[0]) begin
                inw <= 1'b0;
            end
        end
    end

    // VC1: single-flit responses, stateless
    wire [W-1:0] r1 = of[W +: W];
    assign hs_valid = ov[1];
    assign hs_flit  = {r1[KTS_H_TAG_LSB +: MIDX_W],
                       (r1[KTS_H_KIND_LSB +: 4] == KTS_K_WRRSP),
                       r1[KTS_H_SRC_LSB +: HIDX_W],
                       r1[U +: SPW]};
    assign opop[1]  = hs_ready;

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (rstn && ov[0] && !inw && (h_knd != KTS_K_WRREQ) && (h_knd != KTS_K_RDREQ)) begin
            $display("%0t ERROR kx_kedge: unexpected REQ kind %0d", $time, h_knd);
        end
    end
`endif
endmodule

`default_nettype wire
