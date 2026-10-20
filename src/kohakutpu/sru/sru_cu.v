// The Streaming Row Unit as a mesh endpoint: noc_cu_base framing around
// sru_core. docs/projects/kohakutpu/sru.md is the design.
//
// CU instruction, in the CU_INST payload ([255:252] op):
//
//   1 CFG    [251:244] word, [31:0] value               sru_cfg.vh's map
//   2 LOAD   [251:250] what (0 function table, 1 column constants, 2 row state),
//            [249:240] first entry, [239:200] address, [199:188] words
//   3 RUN    one job (fields below); retires when the job is done and its
//            last write has its ack
//   4 STORE  row state to memory: [248:240] first row, [239:200] address,
//            [199:188] rows (one 32-byte word a row: f32 R0, R1, R2 in
//            [31:0], [63:32], [95:64])
//   5 POST   a CREDIT of [15:0] to ([251:248] x, [247:244] y)
//
// RUN: [251] A from memory (1) or a CU_DATA COUNT stream (0); [250] out to a
//   peer (1) or memory (0); [249:210] A address; [209:194] A sub-row stride
//   (words); [193:154] out address; [153:138] out sub-row stride (words);
//   [137:98] B address; [97:82] B stride; peer out [81:78] x, [77:74] y,
//   [73:70] buf, [69:54] offset (granules), [53] COUNT; A's producer for
//   its credits [52:49] x, [48:45] y, and the stream's words [44:21] (NW x NSR).
//
// LOAD words: table 4 entries a word (entry k in [64k +: 36]); column
// constants 2 entries a word ([128k +: 128]); row state one row a word.
//
// FLOW CONTROL. Memory reads are issued only for space reserved in the
// target FIFO, in runs of at most 32 words; the txn class (0x00 A, 0x40 B,
// 0x80 load) names the target. A CU_DATA producer is posted min(A's FIFO
// depth, the stream's words) at RUN and min(8, the rest) each time 8 words
// leave the FIFO: the posts sum to the stream, so the producer's credit
// counter (one for all its sinks) ends where it began. A peer output burst
// (at most 8 words, never across a sub-row) waits for that many credits.
//
// RESET: resetn is the raw active-low reset; it meets one kh_rst_sync here
// and control state resets synchronously from that local release.

`default_nettype none

module sru_cu #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer POS_X      = 3,
    parameter integer POS_Y      = 3,
    parameter integer MEM_X      = 1,
    parameter integer MEM_Y      = 1,
    parameter integer INST_DEPTH = 32,
    parameter integer RECV_DEPTH = 16,
    parameter integer MODEL      = 0,
    // the profile: sru_core's parameters
    parameter integer F_LANES    = 16,
    parameter integer HAS_FN     = 1,
    parameter         TAB        = "ram",
    parameter integer H_TAPS     = 2,
    parameter integer HAS_R1     = 1,
    parameter integer HAS_R2     = 1,
    parameter integer HAS_MX7    = 1,
    parameter integer MX_DSP     = 1,
    parameter integer HAS_MASK   = 1,
    parameter integer HAS_B      = 1,
    parameter integer BUF_WORDS  = 512,
    parameter integer FIFO_DEPTH = 64,      // A and B input FIFOs, words
    parameter integer HAS_MANT   = 0,
    parameter integer FIN_RCP    = 0,
    parameter integer F16_IEEE   = 1
)(
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy
);
    `include "sru_cfg.vh"
    `include "sru_f16e8.vh"

    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1, T_MEM_RD_RESP = 4'h2;
    localparam [3:0] T_MEM_WR_DATA = 4'h4, T_CU_DATA = 4'h8;
    localparam [3:0] C_CFG = 4'd1, C_LOAD = 4'd2, C_RUN = 4'd3, C_STORE = 4'd4, C_POST = 4'd5;
    localparam integer FAW = $clog2(FIFO_DEPTH);

    // ================================================ reset
    wire rstn;
    kh_rst_sync u_rs (.clk(clk), .arstn(resetn), .rstn(rstn));
    wire rst = !rstn;

    // ================================================ framework
    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit;
    wire                  inst_valid, recv_valid, send_ready;
    reg                   inst_ready, exec_done;
    reg  [31:0]           exec_result;
    reg  [FLIT_WIDTH-1:0] send_flit;
    reg                   send_valid;
    wire                  cr_valid, post_ready;
    wire [15:0]           cr_amt;
    reg                   post_valid;
    reg  [3:0]            post_x, post_y;
    reg  [15:0]           post_amt;
    reg  [31:0]           cycles;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH), .POS_X(POS_X), .POS_Y(POS_Y),
        .CU_TYPE(16'h5352), .CU_VERSION(8'h06), .N_BUFFERS(1),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH),
        .MEM_TYPE("distributed"), .RECV_MEM("distributed"),
        .ACK_FENCE(1), .CREDITS(1)
    ) u_base (
        .clk(clk), .resetn(rstn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid), .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid), .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result), .exec_fault(1'b0),
        .dbg_ctr({32'd0, cycles}),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(1'b1),
        .cr_valid(cr_valid), .cr_amt(cr_amt),
        .post_valid(post_valid), .post_x(post_x[POS_WIDTH-1:0]), .post_y(post_y[POS_WIDTH-1:0]),
        .post_amt(post_amt), .post_ready(post_ready),
        .inst_space(), .busy()
    );

    wire [3:0]   i_op  = inst_flit[255:252];
    wire [3:0]   rtype = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire [7:0]   rtag  = recv_flit[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];
    wire [255:0] rpay  = recv_flit[255:0];
    wire         r_rd  = recv_valid && (rtype == T_MEM_RD_RESP);
    wire         r_cd  = recv_valid && (rtype == T_CU_DATA);

    // the configuration words this shell walks by (a copy of words 0 and 1)
    reg  [31:0]  cfg [0:1];
    wire [11:0]  c_nw   = `SRU_G_NW;
    wire [15:0]  c_nsr  = `SRU_G_NSR;
    wire         c_useb = `SRU_G_USE_B;
    wire [1:0]   c_out  = `SRU_G_OUT;

    // ================================================ the core
    reg          cfg_we, tab_we, cc_we, rs_we, start;
    reg  [7:0]   cfg_addr;
    reg  [31:0]  cfg_wdata;
    reg  [9:0]   tab_waddr;
    reg  [35:0]  tab_wdata;
    reg  [8:0]   cc_waddr, rs_waddr, rs_raddr;
    reg  [191:0] cc_wdata;
    reg  [71:0]  rs_wdata;
    wire [71:0]  rs_rdata;
    wire         busy, done;
    wire         a_valid, b_valid, a_ready, b_ready, o_valid;
    wire [255:0] a_data, b_data, o_data;
    wire [7:0]   o_count;
    wire         o_pop;

    sru_core #(
        .MODEL(MODEL), .F_LANES(F_LANES), .HAS_FN(HAS_FN), .TAB(TAB), .H_TAPS(H_TAPS),
        .HAS_R1(HAS_R1), .HAS_R2(HAS_R2), .HAS_MX7(HAS_MX7), .MX_DSP(MX_DSP),
        .HAS_MASK(HAS_MASK), .HAS_B(HAS_B), .BUF_WORDS(BUF_WORDS),
        .HAS_MANT(HAS_MANT), .FIN_RCP(FIN_RCP), .F16_IEEE(F16_IEEE)
    ) u_core (
        .clk(clk), .rst(rst),
        .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_wdata(cfg_wdata),
        .tab_we(tab_we), .tab_waddr(tab_waddr), .tab_wdata(tab_wdata),
        .cc_we(cc_we), .cc_waddr(cc_waddr), .cc_wdata(cc_wdata),
        .rs_we(rs_we), .rs_waddr(rs_waddr), .rs_wdata(rs_wdata),
        .rs_raddr(rs_raddr), .rs_rdata(rs_rdata),
        .start(start), .busy(busy), .done(done),
        .a_valid(a_valid), .a_data(a_data), .a_ready(a_ready),
        .b_valid(b_valid), .b_data(b_data), .b_ready(b_ready),
        .o_valid(o_valid), .o_data(o_data), .o_count(o_count), .o_pop(o_pop)
    );

    // ================================================ input FIFOs
    reg          af_we, bf_we;
    reg  [255:0] af_wd, bf_wd;
    wire         af_empty, bf_empty;
    sync_fifo #(.DATA_WIDTH(256), .FIFO_DEPTH(FIFO_DEPTH), .MEMORY_TYPE("lean")) u_af (
        .clk(clk), .rst(rst), .wr_en(af_we), .wr_data(af_wd), .wr_busy(), .wr_almost(),
        .rd_en(a_ready && a_valid), .rd_data(a_data), .rd_busy(af_empty)
    );
    sync_fifo #(.DATA_WIDTH(256), .FIFO_DEPTH(FIFO_DEPTH), .MEMORY_TYPE("lean")) u_bf (
        .clk(clk), .rst(rst), .wr_en(bf_we), .wr_data(bf_wd), .wr_busy(), .wr_almost(),
        .rd_en(b_ready && b_valid), .rd_data(b_data), .rd_busy(bf_empty)
    );
    assign a_valid = !af_empty;
    assign b_valid = !bf_empty;
    wire a_pop = a_ready && a_valid;
    wire b_pop = b_ready && b_valid;

    // ================================================ job registers
    reg          j_amem, j_peer, j_count;
    reg  [39:0]  j_abase, j_obase, j_bbase;
    reg  [15:0]  j_astr, j_ostr, j_bstr;
    reg  [3:0]   j_px, j_py, j_pbuf, j_ax, j_ay;

    // ================================================ request walkers
    reg  [15:0]  wa_g, wb_g;           // sub-rows left after the current one
    reg  [11:0]  wa_left, wb_left;     // words of the sub-row not yet asked for
    reg          wa_on, wb_on, wl_on;
    reg  [FAW:0] a_room, b_room;       // FIFO words neither asked for nor held
    reg  [39:0]  wl_addr;
    reg  [11:0]  wl_left;
    reg  [1:0]   wl_what;
    reg  [9:0]   wl_idx;
    reg  [7:0]   wl_inq;               // load words asked for, not landed

    function [11:0] min3;
        input [11:0] a, b, c;
        reg   [11:0] m;
        begin
            m = (a < b) ? a : b;
            min3 = (m < c) ? m : c;
        end
    endfunction
    wire [11:0] a_rm   = {{(11-FAW){1'b0}}, a_room};
    wire [11:0] b_rm   = {{(11-FAW){1'b0}}, b_room};
    wire [11:0] wa_run = min3(wa_left, a_rm, 12'd32);
    wire [11:0] wb_run = min3(wb_left, b_rm, 12'd32);
    // A run waits for RUN_MIN free words (or the sub-row's rest): one-word
    // runs hold the send path (MEASURED card_sru_1n affine: 31 cycles a word).
    localparam integer RUN_MIN = (FIFO_DEPTH / 2 < 16) ? FIFO_DEPTH / 2 : 16;
    function [11:0] need;
        input [11:0] left;
        begin
            need = (left < RUN_MIN) ? left : RUN_MIN[11:0];
        end
    endfunction
    // min(left, RUN_MIN) registered with `left`: off the request -> credit path
    reg  [11:0] a_need, b_need;
    // the run closes the sub-row: compared beside the min, not after it
    wire        wa_end = (wa_left <= a_rm) && (wa_left <= 12'd32);
    wire        wb_end = (wb_left <= b_rm) && (wb_left <= 12'd32);
    // Table and column words unpack over several cycles: one word a run.
    wire [11:0] wl_cap = (wl_what == 2'd2) ? 12'd32 : 12'd1;
    wire [11:0] wl_run = (wl_left < wl_cap) ? wl_left : wl_cap;
    reg  [4:0]  lw_n;                  // steps of a load word still to take
    wire        wa_go  = wa_on && (wa_left != 12'd0) && (a_rm >= a_need);
    wire        wb_go  = wb_on && (wb_left != 12'd0) && (b_rm >= b_need);
    wire        wl_go  = wl_on && (wl_left != 12'd0) && (wl_inq == 8'd0) && (lw_n == 5'd0);
    wire        rq_go  = wa_go || wb_go || wl_go;
    // addresses kept as running pointers: no multiply on the request path
    reg  [39:0] wa_addr, wa_row, wb_addr, wb_row;

    function [255:0] rd_req;
        input [39:0] addr;
        input [7:0]  cnt;
        begin
            rd_req = {addr, 8'd0, 8'h40, cnt, 24'd0, 2'd0, 8'd1, 158'd0};
        end
    endfunction
    wire [4*POS_WIDTH-1:0] to_mem = {MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                     POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0]};

    // ================================================ output bursts
    localparam [1:0] X_IDLE = 2'd0, X_DATA = 2'd1;
    reg  [1:0]   xs;
    reg          running;
    reg          x_peer;
    reg  [11:0]  oe_nw;
    reg  [11:0]  oe_room;              // out words left in the sub-row
    reg  [3:0]   oe_left;
    reg  [15:0]  oe_off;
    reg  [15:0]  credits;
    reg  [39:0]  oe_ptr, oe_row;       // next out word, and its sub-row's first
    wire         tx_ok    = !send_valid || send_ready;
    // the burst length min(oe_room, 8) is a register set with oe_room: the
    // header and credit test start from it
    reg  [3:0]   oe_blen;
    function [3:0] blen;
        input [11:0] room;
        begin
            blen = (room < 12'd8) ? room[3:0] : 4'd8;
        end
    endfunction
    wire [39:0]  oe_addr  = oe_ptr;
    // A header leaves only with its whole burst in the out FIFO: a burst
    // waiting on the core would hold the send path the core's input needs.
    wire         oe_hdr   = (xs == X_IDLE) && tx_ok && !rq_go && running
                         && (o_count >= {4'd0, oe_blen}) && (oe_blen != 4'd0)
                         && (!j_peer || (credits >= {12'd0, oe_blen}));
    wire         oe_data  = (xs == X_DATA) && tx_ok && o_valid;
    assign o_pop = oe_data;

    // ================================================ STORE walker
    reg          ws_on;
    reg  [8:0]   ws_row;
    reg  [11:0]  ws_left;
    reg  [39:0]  ws_addr;
    reg  [1:0]   ws_ph;

    // ================================================ CU_DATA into A
    reg          cd_st;
    reg  [7:0]   cd_left;

    // ================================================ the controller
    localparam [2:0] I_IDLE = 3'd0, I_RET = 3'd1, I_LOAD = 3'd2, I_RUN = 3'd3,
                     I_STORE = 3'd4, I_POST = 3'd5;
    reg  [2:0]   ist;
    reg  [2:0]   a_taken;
    reg          post_a;
    reg  [23:0]  cr_left;              // stream words the producer is still to be posted
    reg  [255:0] lw_d;
    reg  [3:0]   lw_k;
    reg  [191:0] cc_acc;

    always @(posedge clk) begin
        if (rst) begin
            ist <= I_IDLE;
            inst_ready <= 1'b0; exec_done <= 1'b0; exec_result <= 32'd0;
            cfg_we <= 1'b0; tab_we <= 1'b0; cc_we <= 1'b0; rs_we <= 1'b0; start <= 1'b0;
            af_we <= 1'b0; bf_we <= 1'b0;
            wa_on <= 1'b0; wb_on <= 1'b0; wl_on <= 1'b0; ws_on <= 1'b0;
            a_room <= FIFO_DEPTH[FAW:0]; b_room <= FIFO_DEPTH[FAW:0];
            wl_inq <= 8'd0; lw_n <= 5'd0;
            send_valid <= 1'b0; xs <= X_IDLE; running <= 1'b0;
            credits <= 16'd0;
            post_valid <= 1'b0; a_taken <= 3'd0; post_a <= 1'b0; cr_left <= 24'd0;
            cd_st <= 1'b0;
            cycles <= 32'd0;
        end else begin
            inst_ready <= 1'b0;
            exec_done  <= 1'b0;
            cfg_we <= 1'b0; tab_we <= 1'b0; cc_we <= 1'b0; rs_we <= 1'b0; start <= 1'b0;
            af_we <= 1'b0; bf_we <= 1'b0;
            if (busy) cycles <= cycles + 32'd1;
            if (send_valid && send_ready) send_valid <= 1'b0;
            if (post_valid && post_ready) post_valid <= 1'b0;

            // ---- inbound ----
            if (r_rd) begin
                case (rtag[7:6])
                    2'd0: begin af_we <= 1'b1; af_wd <= rpay; end
                    2'd1: begin bf_we <= 1'b1; bf_wd <= rpay; end
                    default: begin
                        wl_inq <= wl_inq - 8'd1;
                        if (wl_what == 2'd2) begin
                            rs_we    <= 1'b1;
                            rs_waddr <= wl_idx[8:0];
                            rs_wdata <= {rpay[95:72], rpay[63:40], rpay[31:8]};
                            wl_idx   <= wl_idx + 10'd1;
                        end else begin
                            lw_d <= rpay;
                            lw_n <= (wl_what == 2'd0) ? 5'd4 : 5'd16;
                            lw_k <= 4'd0;
                        end
                    end
                endcase
            end else if (lw_n != 5'd0) begin
                // a load word that unpacks over cycles (the walker waits):
                // table, an entry a step; columns, one FP16 -> E8M15 a step,
                // an entry every 8
                if (wl_what == 2'd0) begin
                    tab_we    <= 1'b1;
                    tab_waddr <= wl_idx;
                    tab_wdata <= lw_d[lw_k[1:0]*64 +: 36];
                    wl_idx    <= wl_idx + 10'd1;
                end else begin
                    cc_acc[lw_k[2:0]*24 +: 24] <= f16e8k(lw_d[lw_k*16 +: 16], F16_IEEE);
                    if (lw_k[2:0] == 3'd7) begin
                        cc_we    <= 1'b1;
                        cc_waddr <= wl_idx[8:0];
                        cc_wdata <= {f16e8k(lw_d[lw_k*16 +: 16], F16_IEEE), cc_acc[167:0]};
                        wl_idx   <= wl_idx + 10'd1;
                    end
                end
                lw_k <= lw_k + 4'd1;
                lw_n <= lw_n - 5'd1;
            end
            if (r_cd) begin
                if (!cd_st) begin
                    cd_st   <= 1'b1;
                    cd_left <= rpay[231:224];
                end else begin
                    af_we <= 1'b1;
                    af_wd <= rpay;
                    if (cd_left == 8'd0) cd_st <= 1'b0;
                    else cd_left <= cd_left - 8'd1;
                end
            end

            // ---- reservations, credits ----
            a_room <= a_room - ((wa_go && xs == X_IDLE && tx_ok) ? wa_run[FAW:0] : {(FAW+1){1'b0}})
                             + ((a_pop && j_amem) ? 1'b1 : 1'b0);
            b_room <= b_room - ((!wa_go && wb_go && xs == X_IDLE && tx_ok) ? wb_run[FAW:0] : {(FAW+1){1'b0}})
                             + (b_pop ? 1'b1 : 1'b0);
            credits <= credits + (cr_valid ? cr_amt : 16'd0)
                     - ((oe_hdr && j_peer) ? {12'd0, oe_blen} : 16'd0);
            if (a_pop && !j_amem) begin
                a_taken <= a_taken + 3'd1;
                if (a_taken == 3'd7) post_a <= 1'b1;
            end
            if (post_a && !post_valid && (ist != I_POST)) begin
                post_valid <= (cr_left != 24'd0);
                post_x <= j_ax; post_y <= j_ay;
                post_amt <= (cr_left < 24'd8) ? {8'd0, cr_left[7:0]} : 16'd8;
                cr_left <= cr_left - ((cr_left < 24'd8) ? cr_left : 24'd8);
                post_a <= 1'b0;
            end

            // ---- the send path: requests first, then output, then STORE ----
            if (xs == X_IDLE && tx_ok && wa_go) begin
                send_flit  <= {to_mem, T_MEM_RD_REQ, 8'h00, 1'b1, 3'b000, rd_req(wa_addr, wa_run[7:0])};
                send_valid <= 1'b1;
                if (wa_end) begin
                    wa_left <= c_nw;
                    a_need  <= need(c_nw);
                    wa_g    <= wa_g - 16'd1;
                    wa_row  <= wa_row + {19'd0, j_astr, 5'd0};
                    wa_addr <= wa_row + {19'd0, j_astr, 5'd0};
                    if (wa_g == 16'd0) wa_on <= 1'b0;
                end else begin
                    wa_left <= wa_left - wa_run;
                    a_need  <= need(wa_left - wa_run);
                    wa_addr <= wa_addr + {23'd0, wa_run, 5'd0};
                end
            end else if (xs == X_IDLE && tx_ok && wb_go) begin
                send_flit  <= {to_mem, T_MEM_RD_REQ, 8'h40, 1'b1, 3'b000, rd_req(wb_addr, wb_run[7:0])};
                send_valid <= 1'b1;
                if (wb_end) begin
                    wb_left <= c_nw;
                    b_need  <= need(c_nw);
                    wb_g    <= wb_g - 16'd1;
                    wb_row  <= wb_row + {19'd0, j_bstr, 5'd0};
                    wb_addr <= wb_row + {19'd0, j_bstr, 5'd0};
                    if (wb_g == 16'd0) wb_on <= 1'b0;
                end else begin
                    wb_left <= wb_left - wb_run;
                    b_need  <= need(wb_left - wb_run);
                    wb_addr <= wb_addr + {23'd0, wb_run, 5'd0};
                end
            end else if (xs == X_IDLE && tx_ok && wl_go) begin
                send_flit  <= {to_mem, T_MEM_RD_REQ, 8'h80, 1'b1, 3'b000, rd_req(wl_addr, wl_run[7:0])};
                send_valid <= 1'b1;
                wl_inq  <= wl_run[7:0];
                wl_addr <= wl_addr + {28'd0, wl_run} * 40'd32;
                wl_left <= wl_left - wl_run;
            end else if (oe_hdr) begin
                oe_left <= oe_blen;
                x_peer  <= j_peer;
                xs      <= X_DATA;
                send_valid <= 1'b1;
                send_flit <= j_peer
                    ? {j_px[POS_WIDTH-1:0], j_py[POS_WIDTH-1:0], POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                       T_CU_DATA, 8'h00, 1'b0, 3'b000,
                       {4'd0, j_pbuf}, oe_off, {4'd0, oe_blen} - 8'd1,
                       6'd0, j_count, 1'b0, 8'd0, 208'd0}
                    : {to_mem, T_MEM_WR_REQ, 8'h01, 1'b0, 3'b000,
                       oe_addr, {4'd0, oe_blen} - 8'd1, 8'd0, 200'd0};
            end else if (oe_data) begin
                send_valid <= 1'b1;
                send_flit <= x_peer
                    ? {j_px[POS_WIDTH-1:0], j_py[POS_WIDTH-1:0], POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                       T_CU_DATA, 8'h00, (oe_left == 4'd1), 3'b000, o_data}
                    : {to_mem, T_MEM_WR_DATA, 8'h01, (oe_left == 4'd1), 3'b000, o_data};
                oe_left <= oe_left - 4'd1;
                oe_off  <= oe_off + 16'd1;
                if (oe_room == 12'd1) begin
                    oe_room <= oe_nw;
                    oe_blen <= blen(oe_nw);
                    oe_row  <= oe_row + {19'd0, j_ostr, 5'd0};
                    oe_ptr  <= oe_row + {19'd0, j_ostr, 5'd0};
                end else begin
                    oe_room <= oe_room - 12'd1;
                    oe_blen <= blen(oe_room - 12'd1);
                    oe_ptr  <= oe_ptr + 40'd32;
                end
                if (oe_left == 4'd1) xs <= X_IDLE;
            end else if (ws_on && xs == X_IDLE && tx_ok) begin
                case (ws_ph)
                    2'd0: begin rs_raddr <= ws_row; ws_ph <= 2'd1; end
                    2'd1: ws_ph <= 2'd2;
                    2'd2: begin
                        send_flit  <= {to_mem, T_MEM_WR_REQ, 8'h01, 1'b0, 3'b000, ws_addr, 8'd0, 8'd0, 200'd0};
                        send_valid <= 1'b1;
                        ws_ph <= 2'd3;
                    end
                    default: begin
                        send_flit  <= {to_mem, T_MEM_WR_DATA, 8'h01, 1'b1, 3'b000,
                                       160'd0, rs_rdata[71:48], 8'd0, rs_rdata[47:24], 8'd0,
                                       rs_rdata[23:0], 8'd0};
                        send_valid <= 1'b1;
                        ws_row  <= ws_row + 9'd1;
                        ws_addr <= ws_addr + 40'd32;
                        ws_left <= ws_left - 12'd1;
                        ws_ph   <= 2'd0;
                        if (ws_left == 12'd1) ws_on <= 1'b0;
                    end
                endcase
            end

            // ---- the instruction FSM ----
            case (ist)
                I_IDLE: if (inst_valid && !inst_ready) begin
                    inst_ready <= 1'b1;
                    case (i_op)
                        C_CFG: begin
                            cfg_we    <= 1'b1;
                            cfg_addr  <= inst_flit[251:244];
                            cfg_wdata <= inst_flit[31:0];
                            if (inst_flit[251:244] == 8'd0) cfg[0] <= inst_flit[31:0];
                            if (inst_flit[251:244] == 8'd1) cfg[1] <= inst_flit[31:0];
                            ist <= I_RET;
                        end
                        C_LOAD: begin
                            wl_on   <= 1'b1;
                            wl_what <= inst_flit[251:250];
                            wl_idx  <= inst_flit[249:240];
                            wl_addr <= inst_flit[239:200];
                            wl_left <= inst_flit[199:188];
                            ist <= I_LOAD;
                        end
                        C_RUN: begin
                            j_amem  <= inst_flit[251];
                            j_peer  <= inst_flit[250];
                            j_abase <= inst_flit[249:210];
                            j_astr  <= inst_flit[209:194];
                            j_obase <= inst_flit[193:154];
                            j_ostr  <= inst_flit[153:138];
                            j_bbase <= inst_flit[137:98];
                            j_bstr  <= inst_flit[97:82];
                            j_px    <= inst_flit[81:78];
                            j_py    <= inst_flit[77:74];
                            j_pbuf  <= inst_flit[73:70];
                            oe_off  <= inst_flit[69:54];
                            j_count <= inst_flit[53];
                            j_ax    <= inst_flit[52:49];
                            j_ay    <= inst_flit[48:45];
                            wa_on <= inst_flit[251]; wa_g <= c_nsr - 16'd1; wa_left <= c_nw;
                            wb_on <= c_useb;         wb_g <= c_nsr - 16'd1; wb_left <= c_nw;
                            a_need <= need(c_nw);    b_need <= need(c_nw);
                            wa_addr <= inst_flit[249:210]; wa_row <= inst_flit[249:210];
                            wb_addr <= inst_flit[137:98];  wb_row <= inst_flit[137:98];
                            oe_ptr  <= inst_flit[193:154]; oe_row <= inst_flit[193:154];
                            oe_nw   <= c_out[1] ? {1'b0, c_nw[11:1]} : c_nw;
                            oe_room <= c_out[1] ? {1'b0, c_nw[11:1]} : c_nw;
                            oe_blen <= blen(c_out[1] ? {1'b0, c_nw[11:1]} : c_nw);
                            a_taken <= 3'd0;
                            running <= 1'b1;
                            start <= 1'b1;
                            if (!inst_flit[251]) begin
                                post_valid <= (inst_flit[44:21] != 24'd0);
                                post_x <= inst_flit[52:49]; post_y <= inst_flit[48:45];
                                post_amt <= (inst_flit[44:21] < FIFO_DEPTH) ? inst_flit[36:21]
                                                                           : FIFO_DEPTH[15:0];
                                cr_left  <= (inst_flit[44:21] < FIFO_DEPTH)
                                    ? 24'd0 : inst_flit[44:21] - FIFO_DEPTH[23:0];
                            end
                            ist <= I_RUN;
                        end
                        C_STORE: begin
                            ws_on   <= 1'b1;
                            ws_row  <= inst_flit[248:240];
                            ws_addr <= inst_flit[239:200];
                            ws_left <= inst_flit[199:188];
                            ws_ph   <= 2'd0;
                            ist <= I_STORE;
                        end
                        C_POST: begin
                            post_valid <= 1'b1;
                            post_x <= inst_flit[251:248]; post_y <= inst_flit[247:244];
                            post_amt <= inst_flit[15:0];
                            ist <= I_POST;
                        end
                        default: ist <= I_RET;
                    endcase
                end
                I_RET: begin
                    exec_done   <= 1'b1;
                    exec_result <= 32'd0;
                    ist <= I_IDLE;
                end
                I_LOAD: if (wl_left == 12'd0 && wl_inq == 8'd0 && lw_n == 5'd0 && !r_rd) begin
                    wl_on <= 1'b0;
                    ist <= I_RET;
                end
                I_RUN: if (!busy && !start && (xs == X_IDLE) && !o_valid && !send_valid && !post_valid) begin
                    running     <= 1'b0;
                    exec_done   <= 1'b1;
                    exec_result <= cycles;
                    ist <= I_IDLE;
                end
                I_STORE: if (!ws_on && !send_valid) ist <= I_RET;
                I_POST: if (!post_valid) ist <= I_RET;
                default: ist <= I_IDLE;
            endcase
        end
    end

`ifdef SRU_TRACE
    // Simulation only: every flit in, every instruction taken and retired.
    reg [31:0] tr_cyc = 32'd0;
    always @(posedge clk) begin
        tr_cyc <= tr_cyc + 32'd1;
        if (noc_in_valid && !noc_in_busy)
            $display("  SRU(%0d,%0d) %0d in type %h", POS_X, POS_Y, tr_cyc,
                     noc_in_data[FLIT_WIDTH-4*POS_WIDTH-1 -: 4]);
        if (inst_valid && inst_ready)
            $display("  SRU(%0d,%0d) %0d take op %0d", POS_X, POS_Y, tr_cyc, i_op);
        if (exec_done)
            $display("  SRU(%0d,%0d) %0d retire", POS_X, POS_Y, tr_cyc);
        if (xs == X_IDLE && tx_ok && wa_go)
            $display("  SRU(%0d,%0d) %0d rdA %0d room %0d", POS_X, POS_Y, tr_cyc, wa_run, a_room);
        if (r_rd)
            $display("  SRU(%0d,%0d) %0d resp", POS_X, POS_Y, tr_cyc);
        if (oe_hdr)
            $display("  SRU(%0d,%0d) %0d wr %0d", POS_X, POS_Y, tr_cyc, oe_blen);
    end
`endif

endmodule

`default_nettype wire
