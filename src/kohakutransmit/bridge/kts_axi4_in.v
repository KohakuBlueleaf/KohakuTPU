// AXI4 slave to a surface: a write (AW + W beats) leaves as one WRREQ packet,
// a read (AR) as one RDREQ header, both on VC_REQ; RDRSP and WRRSP packets on
// VC_RSP come back as R beats and B. The packet's `tag` is the slot the
// transaction holds here, and it is what the far end puts on its AXI ID, so
// responses need no ordering assumption beyond AXI's own.
//
// Widths: a beat is one flit, {strb, data} on the way out and {resp, data} on
// the way back, so W >= DATA_W * 9 / 8; the header's user field holds
// {addr, size, burst, len, id}, so W >= 48 + ADDR_W + 13 + ID_W.

`default_nettype none

module kts_axi4_in #(
    parameter integer W       = 288,
    parameter integer VC      = 2,
    parameter integer VC_REQ  = 0,
    parameter integer VC_RSP  = 1,
    parameter integer D       = 32,
    parameter integer CMAX    = 64,
    parameter integer CN_W    = 4,
    parameter         MEM     = "distributed",
    parameter integer ID_W    = 4,
    parameter integer ADDR_W  = 40,
    parameter integer DATA_W  = 256,
    parameter integer NSLOT   = 16,             // outstanding transactions
    parameter [7:0]   DST     = 8'd0,           // where every request goes
    parameter [7:0]   SRC     = 8'd0,
    parameter integer VCW     = (VC <= 1) ? 1 : $clog2(VC),
    parameter integer SW      = $clog2(NSLOT)
)(
    input  wire                 clk,
    input  wire                 rst,

    input  wire [ID_W-1:0]      s_awid,
    input  wire [ADDR_W-1:0]    s_awaddr,
    input  wire [7:0]           s_awlen,
    input  wire [2:0]           s_awsize,
    input  wire [1:0]           s_awburst,
    input  wire                 s_awvalid,
    output wire                 s_awready,
    input  wire [DATA_W-1:0]    s_wdata,
    input  wire [DATA_W/8-1:0]  s_wstrb,
    input  wire                 s_wlast,
    input  wire                 s_wvalid,
    output wire                 s_wready,
    output wire [ID_W-1:0]      s_bid,
    output wire [1:0]           s_bresp,
    output wire                 s_bvalid,
    input  wire                 s_bready,
    input  wire [ID_W-1:0]      s_arid,
    input  wire [ADDR_W-1:0]    s_araddr,
    input  wire [7:0]           s_arlen,
    input  wire [2:0]           s_arsize,
    input  wire [1:0]           s_arburst,
    input  wire                 s_arvalid,
    output wire                 s_arready,
    output wire [ID_W-1:0]      s_rid,
    output wire [DATA_W-1:0]    s_rdata,
    output wire [1:0]           s_rresp,
    output wire                 s_rlast,
    output wire                 s_rvalid,
    input  wire                 s_rready,

    output wire                 tx_valid,
    output wire [VCW-1:0]       tx_vc,
    output wire                 tx_last,
    output wire [W-1:0]         tx_flit,
    input  wire                 tx_crd_valid,
    input  wire [VCW-1:0]       tx_crd_vc,
    input  wire [CN_W-1:0]      tx_crd_n,

    input  wire                 rx_valid,
    input  wire [VCW-1:0]       rx_vc,
    input  wire                 rx_last,
    input  wire [W-1:0]         rx_flit,
    output wire                 rx_crd_valid,
    output wire [VCW-1:0]       rx_crd_vc,
    output wire [CN_W-1:0]      rx_crd_n
);
`include "kts_pkt.vh"
    localparam integer BW  = DATA_W / 8;                 // bytes per beat
    localparam integer U_ADDR = KTS_H_USER_LSB;
    localparam integer U_SIZE = U_ADDR + ADDR_W;
    localparam integer U_BRST = U_SIZE + 3;
    localparam integer U_LEN  = U_BRST + 2;
    localparam integer U_ID   = U_LEN + 8;

    // ---- slots -------------------------------------------------------------
    reg  [NSLOT-1:0] slot_busy;
    reg  [ID_W-1:0]  slot_id [0:NSLOT-1];
    wire [NSLOT-1:0] slot_free = ~slot_busy;
    wire [NSLOT-1:0] free_low  = slot_free & (~slot_free + {{(NSLOT-1){1'b0}}, 1'b1});
    reg  [SW-1:0]    free_ix;
    integer q;
    always @(*) begin
        free_ix = {SW{1'b0}};
        for (q = 0; q < NSLOT; q = q + 1) begin
            if (free_low[q]) begin
                free_ix = free_ix | q[SW-1:0];
            end
        end
    end
    wire have_slot = |slot_free;

    // ---- request side: one packet at a time on VC_REQ ------------------------
    localparam [1:0] S_IDLE = 2'd0, S_WHDR = 2'd1, S_WDAT = 2'd2, S_RHDR = 2'd3;
    reg [1:0]       st;
    reg [SW-1:0]    cur;
    reg [W-1:0]     hdr;
    reg [VC-1:0]    req_valid;
    reg [VC-1:0]    req_last;
    reg [VC*W-1:0]  req_flit;
    wire [VC-1:0]   req_take;
    wire            take = req_take[VC_REQ];

    function [W-1:0] mk_hdr;
        input [3:0]        kind;
        input [ADDR_W-1:0] addr;
        input [2:0]        size;
        input [1:0]        burst;
        input [7:0]        len;
        input [ID_W-1:0]   id;
        input [SW-1:0]     slot;
        begin
            mk_hdr = {W{1'b0}};
            mk_hdr[KTS_H_KIND_LSB +: KTS_H_KIND_W] = kind;
            mk_hdr[KTS_H_VC_LSB +: KTS_H_VC_W]     = VC_REQ[3:0];
            mk_hdr[KTS_H_DST_LSB +: KTS_H_DST_W]   = DST;
            mk_hdr[KTS_H_SRC_LSB +: KTS_H_SRC_W]   = SRC;
            mk_hdr[KTS_H_LEN_LSB +: KTS_H_LEN_W]   = ({8'd0, len} + 16'd1) * BW[15:0];
            mk_hdr[KTS_H_TAG_LSB +: KTS_H_TAG_W]   = {{(8-SW){1'b0}}, slot};
            mk_hdr[U_ADDR +: ADDR_W] = addr;
            mk_hdr[U_SIZE +: 3]      = size;
            mk_hdr[U_BRST +: 2]      = burst;
            mk_hdr[U_LEN +: 8]       = len;
            mk_hdr[U_ID +: ID_W]     = id;
        end
    endfunction

    // A write is accepted before a read when both wait (AW first keeps W
    // beats flowing); a request needs a free slot.
    wire aw_go = (st == S_IDLE) && s_awvalid && have_slot;
    wire ar_go = (st == S_IDLE) && !s_awvalid && s_arvalid && have_slot;
    assign s_awready = aw_go;
    assign s_arready = ar_go;
    assign s_wready  = (st == S_WDAT) && take;

    always @(*) begin
        req_valid = {VC{1'b0}};
        req_last  = {VC{1'b0}};
        req_flit  = {VC{{W{1'b0}}}};
        case (st)
            S_WHDR: begin
                req_valid[VC_REQ] = 1'b1;
                req_last[VC_REQ]  = 1'b0;
                req_flit[VC_REQ*W +: W] = hdr;
            end
            S_WDAT: begin
                req_valid[VC_REQ] = s_wvalid;
                req_last[VC_REQ]  = s_wlast;
                req_flit[VC_REQ*W +: W] = {{(W-DATA_W-BW){1'b0}}, s_wstrb, s_wdata};
            end
            S_RHDR: begin
                req_valid[VC_REQ] = 1'b1;
                req_last[VC_REQ]  = 1'b1;
                req_flit[VC_REQ*W +: W] = hdr;
            end
            default: begin
            end
        endcase
    end

    always @(posedge clk) begin
        if (rst) begin
            st <= S_IDLE;
        end
        else begin
            case (st)
                S_IDLE: begin
                    if (aw_go) begin
                        st  <= S_WHDR;
                        cur <= free_ix;
                        hdr <= mk_hdr(KTS_K_WRREQ, s_awaddr, s_awsize, s_awburst, s_awlen, s_awid, free_ix);
                    end
                    else if (ar_go) begin
                        st  <= S_RHDR;
                        cur <= free_ix;
                        hdr <= mk_hdr(KTS_K_RDREQ, s_araddr, s_arsize, s_arburst, s_arlen, s_arid, free_ix);
                    end
                end
                S_WHDR: if (take) begin
                    st <= S_WDAT;
                end
                S_WDAT: if (take && s_wlast) begin
                    st <= S_IDLE;
                end
                S_RHDR: if (take) begin
                    st <= S_IDLE;
                end
                default: st <= S_IDLE;
            endcase
        end
    end

    // ---- response side: RDRSP -> R, WRRSP -> B ----------------------------------
    wire [VC-1:0]   hv, hl;
    wire [VC*W-1:0] hf;
    reg  [VC-1:0]   pop;
    wire [W-1:0]    head = hf[VC_RSP*W +: W];
    wire            hvalid = hv[VC_RSP];
    wire            hlast  = hl[VC_RSP];
    wire [3:0]      hkind  = head[KTS_H_KIND_LSB +: 4];
    wire [SW-1:0]   htag   = head[KTS_H_TAG_LSB +: SW];

    reg          rd_body;          // inside an RDRSP payload
    reg [SW-1:0] rd_slot;

    wire is_rdrsp = hvalid && !rd_body && (hkind == KTS_K_RDRSP);
    wire is_wrrsp = hvalid && !rd_body && (hkind == KTS_K_WRRSP);

    assign s_bvalid = is_wrrsp;
    assign s_bid    = slot_id[htag];
    assign s_bresp  = head[U_ADDR +: 2];
    assign s_rvalid = hvalid && rd_body;
    assign s_rid    = slot_id[rd_slot];
    assign s_rdata  = head[DATA_W-1:0];
    assign s_rresp  = head[DATA_W +: 2];
    assign s_rlast  = hlast;

    wire b_pop = is_wrrsp && s_bready;
    wire h_pop = is_rdrsp;                         // the RDRSP header costs no beat
    wire r_pop = s_rvalid && s_rready;
    always @(*) begin
        pop = {VC{1'b0}};
        pop[VC_RSP] = b_pop || h_pop || r_pop;
    end

    always @(posedge clk) begin
        if (rst) begin
            rd_body <= 1'b0;
        end
        else if (h_pop) begin
            rd_body <= 1'b1;
            rd_slot <= htag;
        end
        else if (r_pop && hlast) begin
            rd_body <= 1'b0;
        end
    end

    // slot allocate / free
    integer s;
    always @(posedge clk) begin
        if (rst) begin
            slot_busy <= {NSLOT{1'b0}};
        end
        else begin
            for (s = 0; s < NSLOT; s = s + 1) begin
                if ((aw_go || ar_go) && (free_ix == s[SW-1:0])) begin
                    slot_busy[s] <= 1'b1;
                    slot_id[s]   <= aw_go ? s_awid : s_arid;
                end
                else if ((b_pop && (htag == s[SW-1:0]))
                         || (r_pop && hlast && (rd_slot == s[SW-1:0]))) begin
                    slot_busy[s] <= 1'b0;
                end
            end
        end
    end

    // ---- the two ends -----------------------------------------------------------
    wire [VC*($clog2(CMAX)+1)-1:0] credits_unused;
    kts_tx #(.W(W), .VC(VC), .CMAX(CMAX), .CN_W(CN_W)) u_tx (
        .clk(clk), .rst(rst),
        .req_valid(req_valid), .req_last(req_last), .req_flit(req_flit),
        .req_take(req_take),
        .tx_valid(tx_valid), .tx_vc(tx_vc), .tx_last(tx_last), .tx_flit(tx_flit),
        .crd_valid(tx_crd_valid), .crd_vc(tx_crd_vc), .crd_n(tx_crd_n),
        .credits(credits_unused)
    );
    kts_rx #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .MEM(MEM)) u_rx (
        .clk(clk), .rst(rst),
        .rx_valid(rx_valid), .rx_vc(rx_vc), .rx_last(rx_last), .rx_flit(rx_flit),
        .out_valid(hv), .out_last(hl), .out_flit(hf), .out_pop(pop),
        .crd_valid(rx_crd_valid), .crd_vc(rx_crd_vc), .crd_n(rx_crd_n)
    );

`ifndef SYNTHESIS
    always @(posedge clk) if (!rst && hv[VC_REQ]) begin
        $display("%0t ERROR kts_axi4_in: a flit arrived on the request VC %0d; only responses come here", $time, VC_REQ);
    end
`endif

endmodule

`default_nettype wire
