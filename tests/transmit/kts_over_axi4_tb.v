// A surface tunnelled through AXI4 writes: sender -> kts_over_axi4 (A) ->
// an interconnect model (AW and W each through NST register stages with
// random ready, B answered by the slave) -> kts_over_axi4 (B) -> receiver,
// and B's credit writes back through the same model to A. Order, content,
// drain, and that the write-only windows never see a stray address.

`timescale 1ns / 1ps
`default_nettype none

module kts_over_axi4_tb;
    localparam integer W      = 64;
    localparam integer VC     = 2;
    localparam integer D      = 16;
    localparam integer CN_W   = 4;
    localparam integer VCW    = 1;
    localparam integer ADDR_W = 32;
    localparam integer DATA_W = 128;
    localparam integer ID_W   = 4;
    localparam integer NST    = 3;
    localparam [31:0]  A_FLIT = 32'h0000_0000, A_CRD = 32'h0000_1000;   // A's windows
    localparam [31:0]  B_FLIT = 32'h0001_0000, B_CRD = 32'h0001_1000;   // B's windows

    reg clk = 1'b0;
    reg rst = 1'b1;
    always begin
        #1.667 clk = ~clk;
    end
    integer errors = 0;

    // ---- the surface's ends ----------------------------------------------------
    reg  [VC-1:0]   req_valid;
    reg  [VC*W-1:0] req_flit;
    wire [VC-1:0]   req_take;
    reg  [31:0]     seq_tx [0:VC-1];
    reg  [31:0]     seq_rx [0:VC-1];
    reg             quiet;
    wire            t_v, t_l, r_v, r_l, tc_v, rc_v;
    wire [VCW-1:0]  t_vc, r_vc, tc_vc, rc_vc;
    wire [W-1:0]    t_f, r_f;
    wire [CN_W-1:0] tc_n, rc_n;
    wire [VC-1:0]   out_valid, out_last;
    wire [VC*W-1:0] out_flit;
    reg  [VC-1:0]   out_pop;
    wire [VC*($clog2(D)+1)-1:0] credits;

    kts_tx #(.W(W), .VC(VC), .CMAX(D), .CN_W(CN_W)) u_tx (
        .clk(clk), .rst(rst),
        .req_valid(req_valid), .req_last({VC{1'b1}}), .req_flit(req_flit), .req_take(req_take),
        .tx_valid(t_v), .tx_vc(t_vc), .tx_last(t_l), .tx_flit(t_f),
        .crd_valid(tc_v), .crd_vc(tc_vc), .crd_n(tc_n), .credits(credits)
    );
    kts_rx #(.W(W), .VC(VC), .D(D), .CN_W(CN_W)) u_rx (
        .clk(clk), .rst(rst),
        .rx_valid(r_v), .rx_vc(r_vc), .rx_last(r_l), .rx_flit(r_f),
        .out_valid(out_valid), .out_last(out_last), .out_flit(out_flit), .out_pop(out_pop),
        .crd_valid(rc_v), .crd_vc(rc_vc), .crd_n(rc_n)
    );

    // ---- AXI wires: A's master -> B's slave, B's master -> A's slave ---------------
    // master side of A (ma_*), delayed into slave side of B (sb_*); and mb_* -> sa_*
    wire [ID_W-1:0]   ma_awid, mb_awid;  wire [ADDR_W-1:0] ma_awaddr, mb_awaddr;
    wire [7:0] ma_awlen, mb_awlen; wire [2:0] ma_awsize, mb_awsize; wire [1:0] ma_awburst, mb_awburst;
    wire ma_awvalid, mb_awvalid, ma_awready, mb_awready;
    wire [DATA_W-1:0] ma_wdata, mb_wdata; wire [DATA_W/8-1:0] ma_wstrb, mb_wstrb;
    wire ma_wlast, mb_wlast, ma_wvalid, mb_wvalid, ma_wready, mb_wready;
    wire [ID_W-1:0] ma_bid, mb_bid; wire [1:0] ma_bresp, mb_bresp; wire ma_bvalid, mb_bvalid, ma_bready, mb_bready;
    wire [ID_W-1:0]   sa_awid, sb_awid;  wire [ADDR_W-1:0] sa_awaddr, sb_awaddr;
    wire [7:0] sa_awlen, sb_awlen; wire sa_awvalid, sb_awvalid, sa_awready, sb_awready;
    wire [DATA_W-1:0] sa_wdata, sb_wdata; wire sa_wlast, sb_wlast, sa_wvalid, sb_wvalid, sa_wready, sb_wready;
    wire [ID_W-1:0] sa_bid, sb_bid; wire [1:0] sa_bresp, sb_bresp; wire sa_bvalid, sb_bvalid, sa_bready, sb_bready;

    wire            a_ov, a_ol, a_ocv, b_ocv;
    wire [VCW-1:0]  a_ovc, a_ocvc, b_ocvc;
    wire [W-1:0]    a_of;
    wire [CN_W-1:0] a_ocn, b_ocn;

    kts_over_axi4 #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W),
                    .FLIT_AT(B_FLIT), .CRD_AT(B_CRD), .MY_FLIT(A_FLIT), .MY_CRD(A_CRD)) u_a (
        .clk(clk), .rst(rst),
        .i_valid(t_v), .i_vc(t_vc), .i_last(t_l), .i_flit(t_f),
        .o_valid(a_ov), .o_vc(a_ovc), .o_last(a_ol), .o_flit(a_of),
        .i_crd_valid(1'b0), .i_crd_vc({VCW{1'b0}}), .i_crd_n({CN_W{1'b0}}),
        .o_crd_valid(tc_v), .o_crd_vc(tc_vc), .o_crd_n(tc_n),
        .m_awid(ma_awid), .m_awaddr(ma_awaddr), .m_awlen(ma_awlen), .m_awsize(ma_awsize), .m_awburst(ma_awburst),
        .m_awvalid(ma_awvalid), .m_awready(ma_awready),
        .m_wdata(ma_wdata), .m_wstrb(ma_wstrb), .m_wlast(ma_wlast), .m_wvalid(ma_wvalid), .m_wready(ma_wready),
        .m_bid(ma_bid), .m_bresp(ma_bresp), .m_bvalid(ma_bvalid), .m_bready(ma_bready),
        .s_awid(sa_awid), .s_awaddr(sa_awaddr), .s_awlen(sa_awlen), .s_awvalid(sa_awvalid), .s_awready(sa_awready),
        .s_wdata(sa_wdata), .s_wlast(sa_wlast), .s_wvalid(sa_wvalid), .s_wready(sa_wready),
        .s_bid(sa_bid), .s_bresp(sa_bresp), .s_bvalid(sa_bvalid), .s_bready(sa_bready)
    );
    kts_over_axi4 #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W),
                    .FLIT_AT(A_FLIT), .CRD_AT(A_CRD), .MY_FLIT(B_FLIT), .MY_CRD(B_CRD)) u_b (
        .clk(clk), .rst(rst),
        .i_valid(1'b0), .i_vc({VCW{1'b0}}), .i_last(1'b0), .i_flit({W{1'b0}}),
        .o_valid(r_v), .o_vc(r_vc), .o_last(r_l), .o_flit(r_f),
        .i_crd_valid(rc_v), .i_crd_vc(rc_vc), .i_crd_n(rc_n),
        .o_crd_valid(b_ocv), .o_crd_vc(b_ocvc), .o_crd_n(b_ocn),
        .m_awid(mb_awid), .m_awaddr(mb_awaddr), .m_awlen(mb_awlen), .m_awsize(mb_awsize), .m_awburst(mb_awburst),
        .m_awvalid(mb_awvalid), .m_awready(mb_awready),
        .m_wdata(mb_wdata), .m_wstrb(mb_wstrb), .m_wlast(mb_wlast), .m_wvalid(mb_wvalid), .m_wready(mb_wready),
        .m_bid(mb_bid), .m_bresp(mb_bresp), .m_bvalid(mb_bvalid), .m_bready(mb_bready),
        .s_awid(sb_awid), .s_awaddr(sb_awaddr), .s_awlen(sb_awlen), .s_awvalid(sb_awvalid), .s_awready(sb_awready),
        .s_wdata(sb_wdata), .s_wlast(sb_wlast), .s_wvalid(sb_wvalid), .s_wready(sb_wready),
        .s_bid(sb_bid), .s_bresp(sb_bresp), .s_bvalid(sb_bvalid), .s_bready(sb_bready)
    );

    // ---- the interconnect model: a valid/ready pipe per channel ---------------------
    // Generic NST-stage skid pipe with random ready at the tail.
    `define PIPE(NAME, WD, IV, IR, ID, OV, OR, OD) \
        reg  [WD-1:0] NAME``_d [0:NST-1]; \
        reg           NAME``_v [0:NST-1]; \
        wire [NST:0]  NAME``_t; \
        assign NAME``_t[NST] = NAME``_v[NST-1] && OR; \
        assign IR = !NAME``_v[0] || NAME``_t[1]; \
        assign OV = NAME``_v[NST-1]; \
        assign OD = NAME``_d[NST-1]; \
        genvar NAME``_g; \
        generate for (NAME``_g = 0; NAME``_g < NST; NAME``_g = NAME``_g + 1) begin : NAME``_gs \
            wire inv = (NAME``_g == 0) ? IV : NAME``_v[NAME``_g-1]; \
            assign NAME``_t[NAME``_g] = inv && (!NAME``_v[NAME``_g] || NAME``_t[NAME``_g+1]); \
        end endgenerate \
        integer NAME``_i; \
        always @(posedge clk) begin \
            for (NAME``_i = 0; NAME``_i < NST; NAME``_i = NAME``_i + 1) begin \
                if (rst) begin \
                    NAME``_v[NAME``_i] <= 1'b0; \
                end \
                else if (NAME``_t[NAME``_i]) begin \
                    NAME``_v[NAME``_i] <= 1'b1; \
                    NAME``_d[NAME``_i] <= (NAME``_i == 0) ? ID : NAME``_d[NAME``_i-1]; \
                end \
                else if (NAME``_t[NAME``_i+1]) begin \
                    NAME``_v[NAME``_i] <= 1'b0; \
                end \
            end \
        end

    localparam integer AWW = ID_W + ADDR_W + 8;
    localparam integer WWW = DATA_W + 1;
    wire [AWW-1:0] ab_aw_in = {ma_awid, ma_awaddr, ma_awlen};
    wire [AWW-1:0] ab_aw_out;
    wire [WWW-1:0] ab_w_in  = {ma_wdata, ma_wlast};
    wire [WWW-1:0] ab_w_out;
    wire [AWW-1:0] ba_aw_in = {mb_awid, mb_awaddr, mb_awlen};
    wire [AWW-1:0] ba_aw_out;
    wire [WWW-1:0] ba_w_in  = {mb_wdata, mb_wlast};
    wire [WWW-1:0] ba_w_out;
    `PIPE(p_ab_aw, AWW, ma_awvalid, ma_awready, ab_aw_in, sb_awvalid, sb_awready, ab_aw_out)
    `PIPE(p_ab_w,  WWW, ma_wvalid,  ma_wready,  ab_w_in,  sb_wvalid,  sb_wready,  ab_w_out)
    `PIPE(p_ba_aw, AWW, mb_awvalid, mb_awready, ba_aw_in, sa_awvalid, sa_awready, ba_aw_out)
    `PIPE(p_ba_w,  WWW, mb_wvalid,  mb_wready,  ba_w_in,  sa_wvalid,  sa_wready,  ba_w_out)
    assign {sb_awid, sb_awaddr, sb_awlen} = ab_aw_out;
    assign {sb_wdata, sb_wlast} = ab_w_out;
    assign {sa_awid, sa_awaddr, sa_awlen} = ba_aw_out;
    assign {sa_wdata, sa_wlast} = ba_w_out;
    // B channel straight back (posted writes; the masters discard it)
    assign ma_bid = sb_bid; assign ma_bresp = sb_bresp; assign ma_bvalid = sb_bvalid; assign sb_bready = ma_bready;
    assign mb_bid = sa_bid; assign mb_bresp = sa_bresp; assign mb_bvalid = sa_bvalid; assign sa_bready = mb_bready;
    // the slaves' ready is by contract never low while offered
    always @(posedge clk) if (!rst) begin
        if ((sb_wvalid && !sb_wready && sb_awvalid) || (sa_wvalid && !sa_wready && sa_awvalid)) begin
            $display("%0t ERROR a window held W with AW offered", $time);
            errors = errors + 1;
        end
    end

    // ---- traffic ------------------------------------------------------------------
    integer k;
    always @(posedge clk) begin
        if (rst) begin
            req_valid <= {VC{1'b0}};
            out_pop   <= {VC{1'b0}};
            quiet     <= 1'b0;
            for (k = 0; k < VC; k = k + 1) begin
                seq_tx[k] <= 0;
                seq_rx[k] <= 0;
                req_flit[k*W +: W] <= {k[31:0], 32'd0};
            end
        end
        else begin
            for (k = 0; k < VC; k = k + 1) begin
                if (req_take[k]) begin
                    seq_tx[k] <= seq_tx[k] + 1;
                    req_flit[k*W +: W] <= {k[31:0], seq_tx[k] + 32'd1};
                    req_valid[k] <= !quiet && ($urandom % 3 != 0);
                end
                else if (!req_valid[k]) begin
                    req_valid[k] <= !quiet && ($urandom % 3 != 0);
                end
                out_pop[k] <= ($urandom % 3 != 0);
                if (out_pop[k] && out_valid[k]) begin
                    if (out_flit[k*W +: W] !== {k[31:0], seq_rx[k]}) begin
                        $display("%0t ERROR VC %0d: got %h, expected seq %0d", $time, k, out_flit[k*W +: W], seq_rx[k]);
                        errors = errors + 1;
                    end
                    seq_rx[k] <= seq_rx[k] + 1;
                end
            end
        end
    end

    integer j;
    initial begin
        repeat (5) @(posedge clk);
        rst <= 1'b0;
        repeat (8000) @(posedge clk);
        quiet <= 1'b1;
        repeat (800) @(posedge clk);
        for (j = 0; j < VC; j = j + 1) begin
            if (seq_rx[j] != seq_tx[j]) begin
                $display("ERROR VC %0d did not drain: tx %0d rx %0d", j, seq_tx[j], seq_rx[j]);
                errors = errors + 1;
            end
        end
        $display("kts_over_axi4_tb: %0d/%0d flits (VC0/VC1) as posted writes through %0d stages each way", seq_tx[0], seq_tx[1], NST);
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
