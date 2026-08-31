// AXI4 across a surface: a master model issues random INCR bursts (writes then
// reads back) into kts_axi4_in, the packets cross eight register stages each
// way, kts_axi4_out drives an AXI4 memory model, and every read is checked
// against what was written. Several transactions stay outstanding.

`timescale 1ns / 1ps
`default_nettype none

module kts_axi4_tb;
    localparam integer W      = 160;
    localparam integer VC     = 2;
    localparam integer D      = 16;
    localparam integer CN_W   = 4;
    localparam integer VCW    = 1;
    localparam integer ID_W   = 4;
    localparam integer ADDR_W = 32;
    localparam integer DATA_W = 64;
    localparam integer NPIPE  = 8;
    localparam integer MEMW   = 4096;          // 64-bit words in the model

    reg clk = 1'b0;
    reg rst = 1'b1;
    always begin
        #1.667 clk = ~clk;
    end
    integer errors = 0;

    // ---- master side --------------------------------------------------------------
    reg  [ID_W-1:0]     awid;   reg [ADDR_W-1:0] awaddr; reg [7:0] awlen;  reg awvalid;
    wire                awready;
    reg  [DATA_W-1:0]   wdata;  reg [DATA_W/8-1:0] wstrb; reg wlast; reg wvalid;
    wire                wready;
    wire [ID_W-1:0]     bid;    wire [1:0] bresp; wire bvalid; reg bready;
    reg  [ID_W-1:0]     arid;   reg [ADDR_W-1:0] araddr; reg [7:0] arlen;  reg arvalid;
    wire                arready;
    wire [ID_W-1:0]     rid;    wire [DATA_W-1:0] rdata; wire [1:0] rresp; wire rlast; wire rvalid;
    reg                 rready;

    // in -> pipe -> out (requests), out -> pipe -> in (responses)
    wire a_v, a_l, p_v, p_l, a_cv, p_cv;
    wire [VCW-1:0] a_vc, p_vc, a_cvc, p_cvc;
    wire [W-1:0] a_f, p_f;
    wire [CN_W-1:0] a_cn, p_cn;
    wire b_v, b_l, q_v, q_l, b_cv, q_cv;
    wire [VCW-1:0] b_vc, q_vc, b_cvc, q_cvc;
    wire [W-1:0] b_f, q_f;
    wire [CN_W-1:0] b_cn, q_cn;

    kts_axi4_in #(.W(W), .VC(VC), .D(D), .CMAX(D), .CN_W(CN_W), .ID_W(ID_W),
                  .ADDR_W(ADDR_W), .DATA_W(DATA_W), .NSLOT(8)) u_in (
        .clk(clk), .rst(rst),
        .s_awid(awid), .s_awaddr(awaddr), .s_awlen(awlen), .s_awsize(3'd3), .s_awburst(2'b01),
        .s_awvalid(awvalid), .s_awready(awready),
        .s_wdata(wdata), .s_wstrb(wstrb), .s_wlast(wlast), .s_wvalid(wvalid), .s_wready(wready),
        .s_bid(bid), .s_bresp(bresp), .s_bvalid(bvalid), .s_bready(bready),
        .s_arid(arid), .s_araddr(araddr), .s_arlen(arlen), .s_arsize(3'd3), .s_arburst(2'b01),
        .s_arvalid(arvalid), .s_arready(arready),
        .s_rid(rid), .s_rdata(rdata), .s_rresp(rresp), .s_rlast(rlast), .s_rvalid(rvalid), .s_rready(rready),
        .tx_valid(a_v), .tx_vc(a_vc), .tx_last(a_l), .tx_flit(a_f),
        .tx_crd_valid(p_cv), .tx_crd_vc(p_cvc), .tx_crd_n(p_cn),
        .rx_valid(q_v), .rx_vc(q_vc), .rx_last(q_l), .rx_flit(q_f),
        .rx_crd_valid(b_cv), .rx_crd_vc(b_cvc), .rx_crd_n(b_cn)
    );
    kts_pipe #(.W(W), .VCW(VCW), .CN_W(CN_W), .N(NPIPE)) u_p1 (
        .clk(clk), .rst(rst),
        .i_valid(a_v), .i_vc(a_vc), .i_last(a_l), .i_flit(a_f),
        .o_valid(p_v), .o_vc(p_vc), .o_last(p_l), .o_flit(p_f),
        .i_crd_valid(a_cv), .i_crd_vc(a_cvc), .i_crd_n(a_cn),
        .o_crd_valid(p_cv), .o_crd_vc(p_cvc), .o_crd_n(p_cn)
    );
    kts_pipe #(.W(W), .VCW(VCW), .CN_W(CN_W), .N(NPIPE)) u_p2 (
        .clk(clk), .rst(rst),
        .i_valid(b_v), .i_vc(b_vc), .i_last(b_l), .i_flit(b_f),
        .o_valid(q_v), .o_vc(q_vc), .o_last(q_l), .o_flit(q_f),
        .i_crd_valid(b_cv), .i_crd_vc(b_cvc), .i_crd_n(b_cn),
        .o_crd_valid(q_cv), .o_crd_vc(q_cvc), .o_crd_n(q_cn)
    );

    // ---- memory side ----------------------------------------------------------------
    wire [ID_W-1:0]     m_awid;  wire [ADDR_W-1:0] m_awaddr; wire [7:0] m_awlen; wire [2:0] m_awsize;
    wire [1:0]          m_awburst; wire m_awvalid; reg m_awready;
    wire [DATA_W-1:0]   m_wdata; wire [DATA_W/8-1:0] m_wstrb; wire m_wlast; wire m_wvalid; reg m_wready;
    reg  [ID_W-1:0]     m_bid;   reg [1:0] m_bresp; reg m_bvalid; wire m_bready;
    wire [ID_W-1:0]     m_arid;  wire [ADDR_W-1:0] m_araddr; wire [7:0] m_arlen; wire [2:0] m_arsize;
    wire [1:0]          m_arburst; wire m_arvalid; reg m_arready;
    reg  [ID_W-1:0]     m_rid;   reg [DATA_W-1:0] m_rdata; reg [1:0] m_rresp; reg m_rlast; reg m_rvalid;
    wire                m_rready;

    kts_axi4_out #(.W(W), .VC(VC), .D(D), .CMAX(D), .CN_W(CN_W), .ID_W(ID_W),
                   .ADDR_W(ADDR_W), .DATA_W(DATA_W)) u_out (
        .clk(clk), .rst(rst),
        .rx_valid(p_v), .rx_vc(p_vc), .rx_last(p_l), .rx_flit(p_f),
        .rx_crd_valid(a_cv), .rx_crd_vc(a_cvc), .rx_crd_n(a_cn),
        .tx_valid(b_v), .tx_vc(b_vc), .tx_last(b_l), .tx_flit(b_f),
        .tx_crd_valid(q_cv), .tx_crd_vc(q_cvc), .tx_crd_n(q_cn),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen), .m_awsize(m_awsize),
        .m_awburst(m_awburst), .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast), .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid), .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen), .m_arsize(m_arsize),
        .m_arburst(m_arburst), .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp), .m_rlast(m_rlast), .m_rvalid(m_rvalid), .m_rready(m_rready)
    );

    // A small AXI4 slave: one write and one read in flight, INCR only, random
    // ready, B after the last beat, R beats with random gaps.
    reg [DATA_W-1:0] mem [0:MEMW-1];
    reg [ADDR_W-1:0] wa; reg [ID_W-1:0] wid_q; reg w_open;
    reg [ADDR_W-1:0] ra; reg [ID_W-1:0] rid_q; reg [7:0] rleft; reg r_open;
    integer m;
    initial begin
        for (m = 0; m < MEMW; m = m + 1) begin
            mem[m] = 64'd0;
        end
    end
    always @(posedge clk) begin
        if (rst) begin
            w_open <= 1'b0; r_open <= 1'b0; m_bvalid <= 1'b0; m_rvalid <= 1'b0;
            m_awready <= 1'b0; m_wready <= 1'b0; m_arready <= 1'b0;
        end
        else begin
            m_awready <= !w_open && ($urandom % 2 == 0);
            m_arready <= !r_open && ($urandom % 2 == 0);
            m_wready  <= w_open && ($urandom % 3 != 0);
            if (m_awvalid && m_awready) begin
                w_open <= 1'b1; wa <= m_awaddr; wid_q <= m_awid;
                m_awready <= 1'b0;
            end
            if (m_wvalid && m_wready) begin
                for (m = 0; m < DATA_W / 8; m = m + 1) begin
                    if (m_wstrb[m]) begin
                        mem[wa[ADDR_W-1:3] % MEMW][m*8 +: 8] <= m_wdata[m*8 +: 8];
                    end
                end
                wa <= wa + 8;
                if (m_wlast) begin
                    w_open <= 1'b0; m_wready <= 1'b0;
                    m_bvalid <= 1'b1; m_bid <= wid_q; m_bresp <= 2'b00;
                end
            end
            if (m_bvalid && m_bready) begin
                m_bvalid <= 1'b0;
            end
            if (m_arvalid && m_arready) begin
                r_open <= 1'b1; ra <= m_araddr; rid_q <= m_arid; rleft <= m_arlen;
                m_arready <= 1'b0;
            end
            if (r_open && !m_rvalid && ($urandom % 3 != 0)) begin
                m_rvalid <= 1'b1; m_rdata <= mem[ra[ADDR_W-1:3] % MEMW]; m_rid <= rid_q;
                m_rresp <= 2'b00; m_rlast <= (rleft == 8'd0);
            end
            if (m_rvalid && m_rready) begin
                m_rvalid <= 1'b0; ra <= ra + 8;
                if (rleft == 8'd0) begin
                    r_open <= 1'b0;
                end
                else begin
                    rleft <= rleft - 8'd1;
                end
            end
        end
    end

    // ---- the master model: NB bursts written, then read back and compared ---------
    localparam integer NB = 40;
    reg [ADDR_W-1:0] base [0:NB-1];
    reg [7:0]        blen [0:NB-1];
    function [DATA_W-1:0] word;
        input integer b; input integer i;
        begin
            word = {b[15:0], i[15:0], ~b[15:0], i[15:0] ^ 16'h5a5a};
        end
    endfunction

    integer wb, wi, rb, ri, nb_done, nr_done, ii;
    reg [ADDR_W-1:0] rdaddr;
    initial begin
        awvalid = 0; wvalid = 0; arvalid = 0; bready = 1; rready = 1;
        for (ii = 0; ii < NB; ii = ii + 1) begin
            blen[ii] = $urandom % 8;                          // 1..8 beats
            base[ii] = (ii * 64) & 32'hffff_fff8;             // 8 words apart
        end
        repeat (5) @(posedge clk);
        rst <= 1'b0;
        repeat (30) @(posedge clk);
        // writes: AW then W beats, back to back, B collected as they come
        for (wb = 0; wb < NB; wb = wb + 1) begin
            awid <= wb[ID_W-1:0]; awaddr <= base[wb]; awlen <= blen[wb]; awvalid <= 1'b1;
            @(posedge clk); while (!awready) @(posedge clk);
            awvalid <= 1'b0;
            for (wi = 0; wi <= blen[wb]; wi = wi + 1) begin
                wdata <= word(wb, wi); wstrb <= 8'hff; wlast <= (wi == blen[wb]); wvalid <= 1'b1;
                @(posedge clk); while (!wready) @(posedge clk);
                wvalid <= 1'b0;
            end
        end
        // wait for every B
        while (nb_done < NB) begin
            @(posedge clk);
        end
        // reads, checked
        for (rb = 0; rb < NB; rb = rb + 1) begin
            arid <= rb[ID_W-1:0]; araddr <= base[rb]; arlen <= blen[rb]; arvalid <= 1'b1;
            @(posedge clk); while (!arready) @(posedge clk);
            arvalid <= 1'b0;
        end
        while (nr_done < NB) begin
            @(posedge clk);
        end
        repeat (50) @(posedge clk);
        $display("kts_axi4_tb: %0d bursts written and read back across 2 x %0d stages", NB, NPIPE);
        if (errors == 0) begin
            $display("PASS");
        end
        else begin
            $display("FAIL: %0d error(s)", errors);
        end
        $finish;
    end

    // B and R checking
    integer rb_cur, ri_cur;
    initial begin nb_done = 0; nr_done = 0; rb_cur = 0; ri_cur = 0; end
    always @(posedge clk) if (!rst) begin
        if (bvalid && bready) begin
            if (bid != nb_done[ID_W-1:0]) begin
                $display("%0t ERROR B id %0d, expected %0d", $time, bid, nb_done[ID_W-1:0]);
                errors = errors + 1;
            end
            nb_done = nb_done + 1;
        end
        if (rvalid && rready) begin
            if (rdata !== word(rb_cur, ri_cur)) begin
                $display("%0t ERROR burst %0d beat %0d: read %h, expected %h", $time, rb_cur, ri_cur, rdata, word(rb_cur, ri_cur));
                errors = errors + 1;
            end
            if (rid != rb_cur[ID_W-1:0]) begin
                $display("%0t ERROR R id %0d, expected %0d", $time, rid, rb_cur[ID_W-1:0]);
                errors = errors + 1;
            end
            if (rlast != (ri_cur == blen[rb_cur])) begin
                $display("%0t ERROR burst %0d beat %0d: rlast %0d", $time, rb_cur, ri_cur, rlast);
                errors = errors + 1;
            end
            if (ri_cur == blen[rb_cur]) begin
                ri_cur = 0; rb_cur = rb_cur + 1; nr_done = nr_done + 1;
            end
            else begin
                ri_cur = ri_cur + 1;
            end
        end
    end

endmodule

`default_nettype wire
