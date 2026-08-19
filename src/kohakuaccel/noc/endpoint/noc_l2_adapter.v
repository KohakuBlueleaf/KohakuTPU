// L2 staging in a NoC local link: NoCRouter <-> noc_l2_adapter <-> endpoint.
// Same six-signal interface on both faces, so PASS=1 is a straight wire.

// FRAMEWORK, NOT PROJECT. Its control plane is CU_CTRL, the framework's own
// class, so it sits behind any endpoint and speaks no unit's vocabulary.

// A CU_CTRL flit in this adapter's index window is TERMINATED here and answered
// on the router face. The noc_cu_base behind it never sees one.

// THE ENDPOINT FACE IS REGISTERED and the window is decoded AT CAPTURE, so no
// path runs input to output through it: 284 MHz -> see the landing doc.

// 256-bit line: a MEM_RD_RESP payload IS 256 bits and a local port sends one
// flit per cycle, so a wider line adds a mux and buys nothing. MAG differs.

// THE MESH IS NOT DECODED HERE. A local link carries no transit traffic, and
// the programmed base carries the mesh id, so the compare is plain.

// EXPLICIT STAGING, NOT A CACHE. No tags: an address is in the window or it is
// not, and a run that leaves it is forwarded whole.

`default_nettype none

module noc_l2_adapter #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer ADDR_W     = 40,
    parameter integer DEPTH      = 8192,   // lines; 4 URAM per 4096 at 256b
    // Reset value of the window base. Software moves it with a CU_CTRL write,
    // so the mesh id in addr[37:36] is programmed rather than compiled in.
    parameter [39:0]  L2_BASE    = 40'h00_F000_0000,
    // MUST be 5 + $clog2(DEPTH), so the window and the store are the same size.
    parameter integer L2_BITS    = 18,
    parameter integer QD         = 16,     // response queue; XPM's floor is 16
    // The framework's addon-slot control window, 8 indices, 8-aligned.
    parameter [7:0]   CTRL_BASE  = 8'hE0,
    // LEGACY PRODUCERS ONLY, and none remain: mx_cluster_cu writes the full
    // 40-bit NOC_MEM_ADDR. Setting this reads that field times 64.
    parameter integer ADDR34     = 0,
    parameter integer PASS       = 0
)(
    input  wire                   clk,
    input  wire                   rst,

    // ---- router side ----
    input  wire [FLIT_WIDTH-1:0]  rt_data,     // router -> here
    input  wire                   rt_valid,
    output wire                   rt_busy,
    output wire [FLIT_WIDTH-1:0]  ru_data,     // here -> router
    output wire                   ru_valid,
    input  wire                   ru_busy,

    // ---- endpoint side ----
    output wire [FLIT_WIDTH-1:0]  ep_data,     // here -> endpoint
    output wire                   ep_valid,
    input  wire                   ep_busy,
    input  wire [FLIT_WIDTH-1:0]  eu_data,     // endpoint -> here
    input  wire                   eu_valid,
    output wire                   eu_busy
);
    localparam [3:0] T_MEM_RD_REQ  = 4'h0,
                     T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2,
                     T_MEM_WR_ACK  = 4'h3,
                     T_MEM_WR_DATA = 4'h4,
                     T_CU_CTRL     = 4'h7;

    // DEPTH must be a power of two and at least 512: the window decode is a
    // carry out of AW, and the run-length adders are AW+1 wide.
    localparam integer AW = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    generate if (PASS != 0) begin : g_bypass

        assign ep_data = rt_data;  assign ep_valid = rt_valid;
        assign rt_busy = ep_busy;
        assign ru_data = eu_data;  assign ru_valid = eu_valid;
        assign eu_busy = ru_busy;

    end else begin : g_l2

    // ---- the programmed window, written over CU_CTRL -----------------------
    // DISABLED AT RESET: an adapter nobody configured must claim no address.
    reg  [39:0] l2_base;
    reg         l2_en;
    reg  [31:0] n_serv, n_fwd;

    // ---- capture: decode the offered flit, register the answer -------------
    // Everything below the skid is register logic, which is the whole point.
    wire [3:0]  i_ty  = eu_data[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire [39:0] i_adr = (ADDR34 != 0) ? {6'd0, eu_data[255 -: 34]}
                                      : eu_data[255 -: 40];
    wire [7:0]  i_len = eu_data[215 -: 8];    // beats minus one
    wire [7:0]  i_flg = eu_data[207 -: 8];
    wire [7:0]  i_cnt = eu_data[199 -: 8];    // entries, when STREAM
    wire [1:0]  i_nd  = eu_data[167 -: 2];    // extra multicast destinations
    wire [7:0]  i_ewr = eu_data[165 -: 8];    // words per entry, 0 means 4

    wire i_quant  = i_flg[4];
    wire i_stream = i_flg[6];
    wire [2:0] i_ew = (i_ewr == 8'd0 || i_ewr > 8'd4) ? 3'd4 : i_ewr[2:0];
    wire [7:0] i_cnt0 = (i_cnt == 8'd0) ? 8'd1 : i_cnt;

    wire i_isrd = (i_ty == T_MEM_RD_REQ);

    // Lines the run covers. A streaming fetch is `cnt` entries of `ew` words
    // and both are consecutive, so the whole run is one contiguous line walk.
    wire [15:0] i_lines = (i_isrd && i_stream)
        ? ((i_ew == 3'd1) ? {8'd0, i_cnt0}
        :  (i_ew == 3'd2) ? {7'd0, i_cnt0, 1'b0}
        :  (i_ew == 3'd3) ? ({8'd0, i_cnt0} + {7'd0, i_cnt0, 1'b0})
        :                   {6'd0, i_cnt0, 2'b00})
        : ({8'd0, i_len} + 16'd1);

    // ---- the address decode: window, plus "does the run stay inside" -------
    // The second half is what makes a straddle a forward, not a wrong answer.
    wire i_inrange = l2_en && (i_adr[39:L2_BITS] == l2_base[39:L2_BITS]);
    wire [AW-1:0] i_line = i_adr[5 +: AW];

    // THREE CARRIES IN PARALLEL, muxed at the end. Forming cnt*ew first and
    // adding after put a mux in front of the adder: 11 levels, -1.627 ns.
    wire [AW:0] c_base = {1'b0, i_line};
    wire [AW:0] c_x1 = c_base + {{(AW-7){1'b0}}, i_cnt};
    wire [AW:0] c_x2 = c_base + {{(AW-8){1'b0}}, i_cnt, 1'b0};
    wire [AW:0] c_x4 = c_base + {{(AW-9){1'b0}}, i_cnt, 2'b00};
    wire [AW:0] c_pl = c_base + {{(AW-7){1'b0}}, i_len} + {{AW{1'b0}}, 1'b1};

    // DEPTH is a power of two, so leaving the window IS the carry out of the
    // line index. ew=3 is priced at 4 and so is conservative by one entry.
    wire ov = (i_isrd && i_stream)
            ? ((i_ewr == 8'd1) ? c_x1[AW] : (i_ewr == 8'd2) ? c_x2[AW]
                                                            : c_x4[AW])
            : c_pl[AW];

    // THE TOP LINE IS A GUARD, never served: `end < DEPTH` is one carry bit
    // where `end <= DEPTH` needs an OR-reduce after it. `cnt == 0` likewise.
    wire i_fits = i_inrange && !ov && !(i_isrd && i_stream && (i_cnt == 8'd0));

    // QUANT and multicast stay with MAG: staging holds bytes verbatim and
    // converts nothing, and the adapter can only answer the node behind it.
    wire i_mine = i_fits && !i_quant && (i_nd == 2'd0);

    // ---- the skid: one flit, plus what the decode concluded about it -------
    reg  [FLIT_WIDTH-1:0] s_flit;
    reg                   s_val, s_mine, s_inrange, s_fits;
    reg  [15:0]           s_lines;

    // Free of the register, so none of these cost a capture bit.
    wire [3:0]  s_ty  = s_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire [7:0]  s_tag = s_flit[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];
    wire [POS_WIDTH-1:0] s_dx = s_flit[FLIT_WIDTH-1           -: POS_WIDTH];
    wire [POS_WIDTH-1:0] s_dy = s_flit[FLIT_WIDTH-POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] s_sx = s_flit[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] s_sy = s_flit[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
    wire [39:0] s_adr = s_flit[255 -: 40];
    wire [7:0]  s_len = s_flit[215 -: 8];
    wire [7:0]  s_ewr = s_flit[165 -: 8];
    wire [7:0]  s_flg = s_flit[207 -: 8];
    wire        s_stream = s_flg[6];
    wire [2:0]  s_ew = (s_ewr == 8'd0 || s_ewr > 8'd4) ? 3'd4 : s_ewr[2:0];
    wire [AW-1:0] s_line = s_adr[5 +: AW];

    // ---- store: 256b line, one flit payload per read, no mux ---------------
    reg  [AW-1:0]  wr_addr, rd_addr;
    reg            wr_en;
    reg  [255:0]   wr_data;
    wire [255:0]   rd_data;

    kohaku_sdpram #(.WIDTH(256), .DEPTH(DEPTH), .MEM_PRIM("ultra"),
                    .READ_LAT(2)) u_ram (
        .clk(clk),
        .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data),
        .rd_en(1'b1),  .rd_addr(rd_addr), .rd_data(rd_data)
    );

    // ---- read engine -------------------------------------------------------
    // No reorder buffer: the CU tags responses {entry, word} (mx_cluster_cu.v).
    reg          run, str_r;
    reg  [15:0]  left;
    reg  [AW-1:0] cur;
    reg  [7:0]   tag_r, ent_r;
    reg  [2:0]   ew_r, word_r;
    reg  [POS_WIDTH-1:0] dx_r, dy_r, sx_r, sy_r;

    // ---- write engine ------------------------------------------------------
    // ONE open write, exact not optimistic: a local link has a single producer.
    reg          w_run, w_l2;
    reg  [AW-1:0] w_line;
    reg  [8:0]   w_left;
    reg  [7:0]   w_txn;
    reg  [POS_WIDTH-1:0] w_dx, w_dy, w_sx, w_sy;

    // ---- control plane: CU_CTRL in the addon window, terminated here -------
    wire [3:0]  r_ty  = rt_data[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire [7:0]  r_txn = rt_data[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];
    wire [POS_WIDTH-1:0] r_dx = rt_data[FLIT_WIDTH-1           -: POS_WIDTH];
    wire [POS_WIDTH-1:0] r_dy = rt_data[FLIT_WIDTH-POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] r_sx = rt_data[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] r_sy = rt_data[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
    wire [7:0]  r_op  = rt_data[255 -: 8];    // 0 read, 1 write
    wire [7:0]  r_idx = rt_data[247 -: 8];
    wire [63:0] r_val = rt_data[239 -: 64];

    reg         c_pend;
    reg  [POS_WIDTH-1:0] c_dx, c_dy, c_sx, c_sy;
    reg  [7:0]  c_txn, c_idx;
    reg  [63:0] c_val;

    wire ctrl_claim = rt_valid && (r_ty == T_CU_CTRL) &&
                      (r_idx[7:3] == CTRL_BASE[7:3]);
    wire ctrl_take  = ctrl_claim && !c_pend;

    wire [63:0] caps_w = {16'h0002, 8'd1, L2_BITS[7:0], DEPTH[31:0]};

    // ---- what the skid's flit does, entirely from registers ----------------
    wire s_isrd = (s_ty == T_MEM_RD_REQ);
    wire s_iswr = (s_ty == T_MEM_WR_REQ);
    wire s_iswd = (s_ty == T_MEM_WR_DATA) && w_run;

    // Held, not forwarded: the address is ours, so passing it to MAG would read
    // DRAM. Bounded by the run in flight, which is why busy cannot stick.
    wire hold_rd = s_val && s_isrd && s_mine && run;
    wire hold_wr = s_val && s_iswr && w_run;
    wire hold    = hold_rd || hold_wr;

    wire take_rd = s_val && s_isrd && s_mine && !run;
    wire take_wr = s_val && s_iswr && !w_run && s_mine;
    wire take_wd = s_val && s_iswd && w_l2;
    wire swallow = take_rd || take_wr || take_wd;

    // The control reply owns the router face while it is pending, so a flit to
    // forward waits behind it. One flit, so nothing sticks.
    wire fwd_ok = !ru_busy && !c_pend;
    wire s_go   = s_val && !hold && (swallow || fwd_ok);
    wire s_take = eu_valid && !eu_busy;

    // ---- response queue ----------------------------------------------------
    // Credit is counted, not read off `wr_almost` -- XPM ties that to full.
    wire [FLIT_WIDTH-1:0] q_flit;
    wire                  q_empty, q_full;
    reg  [7:0]            q_used;         // queued PLUS in flight

    wire q_valid = !q_empty;
    wire q_pop   = q_valid && !ep_busy;

    // READ_LAT=2 plus the launch register: three stages between deciding to
    // read a line and having its data beside its header.
    reg  [2:0] vld_sr, ack_sr, lst_sr;
    reg  [1:0] wsr [0:2];
    reg  [7:0] tsr [0:2];

    wire push    = vld_sr[2];
    wire ack_go  = take_wd && (w_left == 9'd1);
    // Three stages in flight plus the ack's slot, so QD-3 outstanding can
    // never overrun a QD-deep queue.
    wire credit  = (q_used < (QD - 3));
    wire launch  = run && !ack_go && credit;

    integer i;
    always @(posedge clk) begin
        if (rst) begin
            s_val <= 1'b0; s_mine <= 1'b0; s_inrange <= 1'b0; s_fits <= 1'b0;
            run <= 1'b0; left <= 16'd0; cur <= {AW{1'b0}};
            vld_sr <= 3'd0; ack_sr <= 3'd0; lst_sr <= 3'd0;
            word_r <= 3'd0; ent_r <= 8'd0; ew_r <= 3'd4; str_r <= 1'b0;
            wr_en <= 1'b0; q_used <= 8'd0;
            w_run <= 1'b0; w_l2 <= 1'b0; w_left <= 9'd0;
            c_pend <= 1'b0;   // c_val/c_idx/c_txn are the reply c_pend qualifies
            l2_base <= L2_BASE; l2_en <= 1'b0;
            n_serv <= 32'd0; n_fwd <= 32'd0;
        end else begin
            wr_en  <= 1'b0;
            vld_sr <= {vld_sr[1:0], 1'b0};
            ack_sr <= {ack_sr[1:0], 1'b0};
            lst_sr <= {lst_sr[1:0], 1'b0};
            for (i = 2; i > 0; i = i - 1) begin
                wsr[i] <= wsr[i-1]; tsr[i] <= tsr[i-1];
            end
            q_used <= q_used + ((launch || ack_go) ? 8'd1 : 8'd0)
                             - (q_pop ? 8'd1 : 8'd0);
            if (push) n_serv <= n_serv + 32'd1;
            if (s_go && (s_isrd || s_iswr) && s_inrange && !s_mine)
                n_fwd <= n_fwd + 32'd1;

            // ------------------------------------------------ the skid
            if (s_take) begin
                s_val     <= 1'b1;
                s_flit    <= eu_data;
                s_mine    <= i_mine;
                s_inrange <= i_inrange;
                s_fits    <= i_fits;
                s_lines   <= i_lines;
            end else if (s_go) s_val <= 1'b0;

            // ------------------------------------------------ control plane
            if (ctrl_take) begin
                c_pend <= 1'b1;
                c_dx <= r_sx; c_dy <= r_sy; c_sx <= r_dx; c_sy <= r_dy;
                c_txn <= r_txn; c_idx <= r_idx;
                case (r_idx[2:0])
                    3'd0:    c_val <= caps_w;
                    3'd1:    c_val <= {24'd0, l2_base};
                    3'd2:    c_val <= {63'd0, l2_en};
                    3'd3:    c_val <= {n_serv, n_fwd};
                    default: c_val <= 64'd0;
                endcase
                if (r_op == 8'd1) begin
                    if (r_idx[2:0] == 3'd1) l2_base <= r_val[39:0];
                    if (r_idx[2:0] == 3'd2) l2_en   <= r_val[0];
                end
            end else if (c_pend && !ru_busy) c_pend <= 1'b0;

            // ------------------------------------------------ write intake
            if (s_go && take_wr) begin
                w_run  <= 1'b1;
                w_l2   <= 1'b1;
                w_line <= s_line;
                w_left <= {1'b0, s_len} + 9'd1;
                w_txn  <= s_tag;
                w_dx   <= s_sx; w_dy <= s_sy;
                w_sx   <= s_dx; w_sy <= s_dy;
            end else if (s_go && s_iswr && !w_run) begin
                // Not ours. Track it anyway, so its data flits are forwarded
                // rather than mistaken for a staged write's.
                w_run  <= 1'b1;
                w_l2   <= 1'b0;
                w_left <= {1'b0, s_len} + 9'd1;
            end else if (s_go && s_iswd) begin
                w_left <= w_left - 9'd1;
                if (w_l2) begin
                    wr_en   <= 1'b1;
                    wr_addr <= w_line;
                    wr_data <= s_flit[255:0];
                    w_line  <= w_line + {{(AW-1){1'b0}}, 1'b1};
                end
                if (w_left == 9'd1) begin w_run <= 1'b0; w_l2 <= 1'b0; end
            end

            // ------------------------------------------------ read intake
            if (s_go && take_rd) begin
                run    <= 1'b1;
                left   <= s_lines;
                cur    <= s_line;
                tag_r  <= s_tag;
                str_r  <= s_stream;
                ew_r   <= s_ew;
                word_r <= 3'd0;
                ent_r  <= 8'd0;
                // Reply to the requester: swap source and destination.
                dx_r <= s_sx; dy_r <= s_sy;
                sx_r <= s_dx; sy_r <= s_dy;
            end else if (launch) begin
                rd_addr   <= cur;
                vld_sr[0] <= 1'b1;
                wsr[0]    <= str_r ? word_r[1:0] : 2'd0;
                tsr[0]    <= str_r ? (tag_r + ent_r) : tag_r;
                // LAST closes an ENTRY under STREAM and the whole run without
                // it, which is what mag_mem_port's two read paths do.
                lst_sr[0] <= str_r ? (word_r == (ew_r - 3'd1)) : (left == 16'd1);
                cur       <= cur + {{(AW-1){1'b0}}, 1'b1};
                left      <= left - 16'd1;
                if (str_r && (word_r == (ew_r - 3'd1))) begin
                    word_r <= 3'd0;
                    ent_r  <= ent_r + 8'd1;
                end else word_r <= word_r + 3'd1;
                if (left == 16'd1) run <= 1'b0;
            end

            // The ack rides the same pipeline, so the two can never both write
            // the queue: `launch` stands down for the one cycle it needs.
            if (ack_go) begin
                vld_sr[0] <= 1'b1;
                ack_sr[0] <= 1'b1;
                lst_sr[0] <= 1'b1;
                tsr[0]    <= w_txn;
                wsr[0]    <= 2'd0;
            end
        end
    end

    wire o_ack = ack_sr[2];
    wire [POS_WIDTH-1:0] o_dx = o_ack ? w_dx : dx_r;
    wire [POS_WIDTH-1:0] o_dy = o_ack ? w_dy : dy_r;
    wire [POS_WIDTH-1:0] o_sx = o_ack ? w_sx : sx_r;
    wire [POS_WIDTH-1:0] o_sy = o_ack ? w_sy : sy_r;

    // 4*POS_WIDTH + 4 + 8 + 1 + 1 + 2 + 256 = FLIT_WIDTH at POS_WIDTH 4.
    wire [FLIT_WIDTH-1:0] q_in =
        { o_dx, o_dy, o_sx, o_sy,
          o_ack ? T_MEM_WR_ACK : T_MEM_RD_RESP, tsr[2],
          lst_sr[2], 1'b0, wsr[2],
          o_ack ? {(FLIT_WIDTH-4*POS_WIDTH-16){1'b0}} : rd_data };

    // The reply noc_cu_base would have sent, field for field, so a controller
    // cannot tell an addon's register from a unit's.
    wire [FLIT_WIDTH-1:0] c_flit =
        { c_dx, c_dy, c_sx, c_sy, T_CU_CTRL, c_txn, 1'b1, 3'b000,
          8'd2, c_idx, c_val, {(FLIT_WIDTH-4*POS_WIDTH-16-16-64){1'b0}} };

    sync_fifo #(.DATA_WIDTH(FLIT_WIDTH), .FIFO_DEPTH(QD),
                .MEMORY_TYPE("distributed")) u_q (
        .clk(clk), .rst(rst),
        .wr_en(push), .wr_data(q_in), .wr_busy(q_full), .wr_almost(),
        .rd_en(q_pop), .rd_data(q_flit), .rd_busy(q_empty)
    );

    // ---- downstream: router flit, or a local response ----------------------
    // Dropping a claimed CU_CTRL IS the termination mechanism; no new type.
    assign ep_data  = q_valid ? q_flit : rt_data;
    assign ep_valid = q_valid | (rt_valid && !ctrl_claim);
    assign rt_busy  = ctrl_claim ? c_pend : (ep_busy | q_valid);

    // ---- upstream: the skid's flit, or a control reply ---------------------
    assign ru_data  = c_pend ? c_flit : s_flit;
    assign ru_valid = c_pend | (s_val && !hold && !swallow);
    assign eu_busy  = s_val && !s_go;

`ifndef SYNTHESIS
    // Fired on `s_go`, which is one cycle per flit, so each is said once.
    wire [39:0] s_last = s_adr + {19'd0, s_lines, 5'd0} - 40'd32;

    always @(posedge clk) if (!rst && s_go && l2_en) begin
        if ((s_isrd || s_iswr) && !s_inrange &&
            (s_last[39:L2_BITS] == l2_base[39:L2_BITS]))
            $display("%0t ERROR noc_l2_adapter: request at %h covers %0d lines and runs UP into the L2 window -- forwarded whole, so it will read DRAM where the staged copy lives",
                     $time, s_adr, s_lines);
        if ((s_isrd || s_iswr) && s_inrange && !s_fits)
            $display("%0t ERROR noc_l2_adapter: request at %h covers %0d lines and straddles the top of the L2 window -- forwarded whole, so it will read DRAM",
                     $time, s_adr, s_lines);
        if (s_isrd && s_fits && !s_mine)
            $display("%0t ERROR noc_l2_adapter: read at %h is in the L2 window but sets QUANT/nd -- forwarded, because staging neither converts nor multicasts",
                     $time, s_adr);
    end
    always @(posedge clk) if (!rst) begin
        if (s_val && s_iswr && w_run)
            $display("%0t ERROR noc_l2_adapter: a second write descriptor arrived with %0d beats of the first still owing -- held, and only a single-producer link makes that safe",
                     $time, w_left);
        if (push && q_full)
            $display("%0t ERROR noc_l2_adapter: response queue overrun -- the launch credit is wrong",
                     $time);
    end
`endif

    end endgenerate
endmodule

`default_nettype wire
