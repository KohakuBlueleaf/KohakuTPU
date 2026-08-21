// rv_pe -- the assembled processing element: an RV32I core, its two external
// windows, its internal L1, its NoC requestor, and noc_cu_base.
//
// This is the controller PE of the KohakuAccel framework. It attaches exactly
// like every other compute unit -- one local NoC port, one instruction FIFO,
// the four CU_CTRL registers -- so a driver enumerates it without knowing it is
// a processor. What is different is only what it does with an instruction: a
// CU_INST is a KICK, and the unit retires when the program halts.
//
// THE WHOLE UNIT IS ON noc_clk AND THERE IS NO CDC ANYWHERE IN IT. The
// requestor has to speak the port contract in that domain, so the core joins it
// rather than being bridged into it (design note s16.2).
//
// BOOT IS NOT A MECHANISM. A program image arrives as a CU_DATA burst into the
// instruction window, arguments as another into the scratchpad, and then the
// standard kick. That is the same write path every unit in the framework has,
// which is why there is no loader here to go wrong.
//
// buf_id ALLOCATION, published as flit-format.md s4.7.1 requires:
//
//   0  scratchpad window, raw 32-byte granules      (bulk push, arguments)
//   1  instruction window, raw 32-byte granules     (program image)
//   3  RESERVED to the framework -- rejected here
//   4  scratchpad window, one 32-bit word, byte enabled   (a peer's sw/sh/sb)
//   5  instruction window, one 32-bit word                (patching)
//   everything else rejected, counted out to `last`, and reported.
//
// A granule is written as EIGHT 32-BIT WORDS, so `recv_ready` is low for eight
// cycles per data flit. That is bounded by this unit's own progress, never by
// another inbound flit, which is the rule that matters (compute-unit-port.md
// s5.1).

`default_nettype none

module rv_pe #(
    parameter integer FLIT_WIDTH   = 288,
    parameter integer POS_WIDTH    = 4,
    parameter integer POS_X        = 2,
    parameter integer POS_Y        = 2,
    parameter integer MEM_X        = 0,
    parameter integer MEM_Y        = 1,
    parameter [39:0]  DRAM_BASE    = 40'h00_0000_0000,
    parameter integer IMEM_WORDS   = 2048,
    parameter integer SPAD_WORDS   = 2048,
    // 128 lines x 32 B = 4 KB = 1024 words of 32 bits, which is exactly one
    // RAMB36E2 at its 1K x 36 aspect. 64 lines would be half a tile: the same
    // number of tiles for half the cache.
    parameter integer L1_LINES     = 128,
    parameter integer BTB_ENTRIES  = 32,
    parameter integer BTB_TAG_W    = 8,
    parameter         REGFILE_PRIM = "distributed",
    parameter integer FWD_X        = 1,
    parameter         MEM_PRIM     = "block",
    // 16 IS THE FLOOR, not a choice: xpm_fifo_sync refuses a depth below it and
    // its behavioural model calls $finish at 2 ps, which presents as a bench
    // that produces no output at all rather than as an error.
    parameter integer INST_DEPTH   = 16,
    parameter integer RECV_DEPTH   = 32
)(
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    output wire                   busy,

    // Visible to a bench, and to nothing in the machine.
    output wire                   dbg_run,
    output wire                   dbg_halted,
    output wire [31:0]            dbg_retire_pc,
    output wire                   dbg_retire_valid,
    output wire [4:0]             dbg_retire_rd,
    output wire [31:0]            dbg_retire_val
);
    localparam integer IAW = $clog2(IMEM_WORDS);
    localparam integer SAW = $clog2(SPAD_WORDS);

    localparam [3:0] T_MEM_RD_RESP = 4'h2,
                     T_MEM_WR_ACK  = 4'h3,
                     T_CU_DATA     = 4'h8;

    localparam [7:0] BUF_SPAD = 8'd0, BUF_IMEM = 8'd1,
                     BUF_SPAD_W = 8'd4, BUF_IMEM_W = 8'd5;

    localparam integer PAY = FLIT_WIDTH - 4*POS_WIDTH - 16;

    // ---- the base -----------------------------------------------------------
    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit, send_flit;
    wire                  inst_valid, recv_valid, send_valid, send_ready;
    wire                  recv_ready;
    reg                   inst_ready_r;
    reg                   exec_done;
    reg  [31:0]           exec_result;
    reg                   exec_fault;

    wire [31:0] cyc_ctr, ret_ctr;
    wire        base_busy;
    wire        core_run, core_halted, pipe_empty;
    wire [1:0]  core_cause;
    wire [31:0] core_halt_word;
    wire        req_idle;
    wire [15:0] wr_out;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y),
        .CU_TYPE(16'h5256), .CU_VERSION(8'h01), .N_BUFFERS(4),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH)
    ) u_base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready_r),
        .exec_done(exec_done), .exec_result(exec_result), .exec_fault(exec_fault),
        .dbg_ctr({ret_ctr, cyc_ctr}),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(recv_ready),
        .inst_space(), .busy(base_busy)
    );

    // A running core is busy with nothing queued, which noc_cu_base cannot know:
    // one kick is one instruction for however long the program runs.
    assign busy = base_busy || core_run;

    // ---- receive classification --------------------------------------------
    wire [3:0] rx_ty = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire [7:0] rx_id = recv_flit[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];
    wire       rx_ls = recv_flit[FLIT_WIDTH-4*POS_WIDTH-13];
    wire [POS_WIDTH-1:0] rx_sx = recv_flit[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] rx_sy = recv_flit[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
    wire [PAY-1:0]       rx_pl = recv_flit[PAY-1:0];

    wire rx_is_resp = (rx_ty == T_MEM_RD_RESP);
    wire rx_is_ack  = (rx_ty == T_MEM_WR_ACK);
    wire rx_is_cud  = (rx_ty == T_CU_DATA);

    // ---- CU_DATA window writer ---------------------------------------------
    localparam [1:0] CD_IDLE = 2'd0, CD_DATA = 2'd1;
    reg  [1:0]  cst;
    reg  [7:0]  cd_buf;
    reg  [15:0] cd_off;
    reg  [7:0]  cd_left;
    reg  [POS_WIDTH-1:0] cd_sx, cd_sy;
    reg         cd_drop;

    reg  [255:0] gw_buf;
    reg  [2:0]   gw_cnt;
    reg          gw_busy, gw_imem;
    // Word address of the granule being walked, wide enough for either window
    // and sliced per array rather than sized to one of them.
    reg  [15:0]  gw_base;

    // Low only while a granule is being walked into an array -- cleared by this
    // unit's own progress, never by another flit arriving.
    assign recv_ready = !gw_busy;
    wire   rx_take    = recv_valid && recv_ready;

    wire cd_is_gran = (cd_buf == BUF_SPAD) || (cd_buf == BUF_IMEM);
    wire cd_is_word = (cd_buf == BUF_SPAD_W) || (cd_buf == BUF_IMEM_W);
    wire cd_to_imem = (cd_buf == BUF_IMEM) || (cd_buf == BUF_IMEM_W);

    wire [7:0]  nd_buf = rx_pl[PAY-1 -: 8];
    wire [15:0] nd_off = rx_pl[PAY-9 -: 16];
    wire [7:0]  nd_len = rx_pl[PAY-25 -: 8];

    wire nd_gran = (nd_buf == BUF_SPAD) || (nd_buf == BUF_IMEM);
    wire nd_word = (nd_buf == BUF_SPAD_W) || (nd_buf == BUF_IMEM_W);
    wire nd_imem = (nd_buf == BUF_IMEM) || (nd_buf == BUF_IMEM_W);
    // offset + len must fit the named window, and the check MUST reject rather
    // than wrap (flit-format.md s4.7.1).
    wire [16:0] nd_end = {1'b0, nd_off} + {9'd0, nd_len};
    wire nd_fits = nd_imem ? (nd_end < (IMEM_WORDS / 8))
                           : (nd_end < (SPAD_WORDS / 8));
    wire nd_ok   = (nd_gran || nd_word) && nd_fits;

    // ---- word-write plumbing into the two windows ---------------------------
    wire [2:0]  wp_sel  = rx_pl[38:36];
    wire [3:0]  wp_be   = rx_pl[35:32];
    wire [31:0] wp_data = rx_pl[31:0];

    wire cd_data_beat = rx_take && rx_is_cud && (cst == CD_DATA) &&
                        (rx_sx == cd_sx) && (rx_sy == cd_sy);
    wire cd_word_wr   = cd_data_beat && cd_is_word && !cd_drop;
    wire cd_gran_wr   = cd_data_beat && cd_is_gran && !cd_drop;

    wire        spad_a_en;
    wire [3:0]  spad_a_we;
    wire [SAW-1:0] spad_a_addr;
    wire [31:0] spad_a_wdata;

    // INDEXED, NOT ROTATED, and measured at +178 LUT the other way: `gw_buf` is
    // loaded whole, so a rotate adds a 2:1 mux on 256 bits to save an 8:1 on 32.
    assign spad_a_en   = (cd_word_wr && !cd_to_imem) || (gw_busy && !gw_imem);
    assign spad_a_we   = gw_busy ? 4'hF : wp_be;
    assign spad_a_addr = gw_busy ? {gw_base[SAW-1:3], gw_cnt}
                                 : {cd_off[SAW-4:0], wp_sel};
    assign spad_a_wdata= gw_busy ? gw_buf[{gw_cnt, 5'd0} +: 32] : wp_data;

    wire        imem_wr_en   = (cd_word_wr && cd_to_imem) || (gw_busy && gw_imem);
    wire [IAW-1:0] imem_wr_a = gw_busy ? {gw_base[IAW-1:3], gw_cnt}
                                       : {cd_off[IAW-4:0], wp_sel};
    wire [31:0] imem_wr_d    = gw_busy ? gw_buf[{gw_cnt, 5'd0} +: 32] : wp_data;

    always @(posedge clk) begin
        if (!resetn) begin
            cst     <= CD_IDLE;
            gw_busy <= 1'b0;
            gw_cnt  <= 3'd0;
            cd_left <= 8'd0;
            cd_drop <= 1'b0;
        end else begin
            if (gw_busy) begin
                if (gw_cnt == 3'd7) gw_busy <= 1'b0;
                gw_cnt <= gw_cnt + 3'd1;
            end

            case (cst)
            CD_IDLE: if (rx_take && rx_is_cud) begin
                cd_buf  <= nd_buf;
                cd_off  <= nd_off;
                cd_left <= nd_len + 8'd1;
                cd_sx   <= rx_sx;
                cd_sy   <= rx_sy;
                cd_drop <= !nd_ok;
                cst     <= CD_DATA;
            end
            CD_DATA: if (cd_data_beat) begin
                cd_off <= cd_off + 16'd1;
                if (cd_gran_wr) begin
                    gw_buf  <= rx_pl[255:0];
                    gw_base <= {cd_off[12:0], 3'd0};
                    gw_cnt  <= 3'd0;
                    gw_busy <= 1'b1;
                    gw_imem <= cd_to_imem;
                end
                if (cd_left == 8'd1) cst <= CD_IDLE;
                else cd_left <= cd_left - 8'd1;
            end
            default: cst <= CD_IDLE;
            endcase
        end
    end

    // ---- the kick -----------------------------------------------------------
    localparam [1:0] K_IDLE = 2'd0, K_START = 2'd1, K_RUN = 2'd2, K_DONE = 2'd3;
    reg  [1:0]  kst;
    reg  [7:0]  k_op;
    reg  [31:0] k_pc, k_arg;
    reg         boot_v;

    wire rx_quiet = !recv_valid && !gw_busy && (cst == CD_IDLE);

    wire [7:0]  i_op  = inst_flit[PAY-1 -: 8];
    wire [31:0] i_pc  = inst_flit[PAY-9 -: 32];
    wire [31:0] i_arg = inst_flit[PAY-41 -: 32];

    always @(posedge clk) begin
        if (!resetn) begin
            kst          <= K_IDLE;
            inst_ready_r <= 1'b0;
            boot_v       <= 1'b0;
            exec_done    <= 1'b0;
            exec_fault   <= 1'b0;
            exec_result  <= 32'd0;
        end else begin
            inst_ready_r <= 1'b0;
            boot_v       <= 1'b0;
            exec_done    <= 1'b0;

            case (kst)
            // A KICK MUST NOT OVERTAKE THE DATA IT IS THE DOORBELL FOR.
            // noc_cu_base sorts CU_INST into the instruction FIFO and CU_DATA
            // into the receive FIFO, so the framework preserves order between
            // one sender and this unit ON THE WIRE but not across those two
            // queues: the kick reaches the head while the last granule of the
            // program image is still being walked into the window, and the core
            // starts on memory that is not there yet. The unit has to enforce
            // it, and this is where. `rx_quiet` is cleared by this unit's own
            // progress, so waiting on it cannot deadlock.
            K_IDLE: if (inst_valid && !inst_ready_r && rx_quiet) begin
                k_op         <= i_op;
                k_pc         <= i_pc;
                k_arg        <= i_arg;
                inst_ready_r <= 1'b1;
                kst          <= K_START;
            end
            // One cycle between accept and anything else: noc_cu_base clears
            // in_flight on exec_done, so a completion in the accept cycle is
            // never queued and the dispatch credit is lost for good.
            K_START: if (k_op == 8'd1) begin
                boot_v <= 1'b1;
                kst    <= K_RUN;
            end else kst <= K_DONE;

            // "Halted" is not enough. The completion is the host's only
            // sequencing point, so it must also mean every write this program
            // issued has reached memory -- which is the write acknowledgement,
            // not the last beat leaving.
            K_RUN: if (core_halted && pipe_empty && req_idle &&
                       (wr_out == 16'd0)) kst <= K_DONE;

            default: begin
                exec_done   <= 1'b1;
                exec_result <= core_halt_word;
                exec_fault  <= (core_cause == 2'd2) || (core_cause == 2'd3);
                kst         <= K_IDLE;
            end
            endcase
        end
    end

    // ---- the core -----------------------------------------------------------
    wire [IAW-1:0] imem_addr;
    wire [31:0]    imem_data;
    wire [SAW-1:0] spad_addr;
    wire [3:0]     spad_we;
    wire [31:0]    spad_wdata, spad_rdata;

    wire [31:0] l1_probe, l1_addr, l1_wdata, l1_rdata;
    wire        l1_req, l1_we, l1_stall, l1_flush, l1_inval, l1_flush_busy;
    wire [3:0]  l1_be;

    wire                  push_valid, push_ready, push_win;
    wire [POS_WIDTH-1:0]  push_dx, push_dy;
    wire [13:0]           push_gran;
    wire [2:0]            push_sel;
    wire [3:0]            push_be;
    wire [31:0]           push_data;

    rv_core #(
        .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS),
        .BTB_ENTRIES(BTB_ENTRIES), .BTB_TAG_W(BTB_TAG_W),
        .REGFILE_PRIM(REGFILE_PRIM), .FWD_X(FWD_X), .POS_WIDTH(POS_WIDTH)
    ) u_core (
        .clk(clk), .resetn(resetn),
        .boot_v(boot_v), .boot_pc(k_pc),
        .run(core_run), .halted(core_halted), .cause(core_cause),
        .halt_word(core_halt_word), .pipe_empty(pipe_empty),
        .coreid({POS_Y[3:0], POS_X[3:0]}), .arg(k_arg), .wr_out(wr_out),
        .imem_addr(imem_addr), .imem_data(imem_data),
        .spad_addr(spad_addr), .spad_we(spad_we), .spad_wdata(spad_wdata),
        .spad_rdata(spad_rdata),
        .l1_probe(l1_probe), .l1_req(l1_req), .l1_we(l1_we), .l1_be(l1_be),
        .l1_addr(l1_addr), .l1_wdata(l1_wdata), .l1_rdata(l1_rdata),
        .l1_stall(l1_stall), .l1_flush(l1_flush), .l1_inval(l1_inval),
        .l1_flush_busy(l1_flush_busy),
        .push_valid(push_valid), .push_ready(push_ready),
        .push_dx(push_dx), .push_dy(push_dy), .push_win(push_win),
        .push_gran(push_gran), .push_sel(push_sel), .push_be(push_be),
        .push_data(push_data),
        .retire_valid(dbg_retire_valid), .retire_pc(dbg_retire_pc),
        .retire_rd(dbg_retire_rd), .retire_val(dbg_retire_val),
        .cycle_ctr(cyc_ctr), .instret_ctr(ret_ctr)
    );

    assign dbg_run    = core_run;
    assign dbg_halted = core_halted;

    rv_imem #(.WORDS(IMEM_WORDS), .MEM_PRIM(MEM_PRIM)) u_imem (
        .clk(clk),
        .wr_en(imem_wr_en), .wr_addr(imem_wr_a), .wr_data(imem_wr_d),
        .rd_addr(imem_addr), .rd_data(imem_data)
    );

    rv_spad #(.WORDS(SPAD_WORDS), .MEM_PRIM(MEM_PRIM)) u_spad (
        .clk(clk),
        .a_en(spad_a_en), .a_we(spad_a_we), .a_addr(spad_a_addr),
        .a_wdata(spad_a_wdata), .a_rdata(),
        .b_addr(spad_addr), .b_we(spad_we), .b_wdata(spad_wdata),
        .b_rdata(spad_rdata)
    );

    wire         fill_valid, fill_ready, resp_valid;
    wire [30:0]  fill_addr;
    wire [255:0] resp_data;
    wire         wb_valid, wb_ready;
    wire [30:0]  wb_addr;
    wire [255:0] wb_data;

    rv_l1 #(.LINES(L1_LINES), .MEM_PRIM(MEM_PRIM)) u_l1 (
        .clk(clk), .resetn(resetn),
        .probe_addr(l1_probe), .req(l1_req), .we(l1_we), .be(l1_be),
        .addr(l1_addr), .wdata(l1_wdata), .rdata(l1_rdata), .stall(l1_stall),
        .flush(l1_flush), .inval(l1_inval), .flush_busy(l1_flush_busy),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data),
        .wb_valid(wb_valid), .wb_ready(wb_ready), .wb_addr(wb_addr),
        .wb_data(wb_data),
        .wr_idle(wr_out == 16'd0)
    );

    rv_noc_req #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y), .MEM_X(MEM_X), .MEM_Y(MEM_Y),
        .DRAM_BASE(DRAM_BASE),
        .BUF_SPAD(BUF_SPAD), .BUF_IMEM(BUF_IMEM),
        .BUF_SPAD_W(BUF_SPAD_W), .BUF_IMEM_W(BUF_IMEM_W)
    ) u_req (
        .clk(clk), .resetn(resetn),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data),
        .wb_valid(wb_valid), .wb_ready(wb_ready), .wb_addr(wb_addr),
        .wb_data(wb_data),
        .push_valid(push_valid), .push_ready(push_ready),
        .push_dx(push_dx), .push_dy(push_dy), .push_win(push_win),
        .push_gran(push_gran), .push_sel(push_sel), .push_be(push_be),
        .push_data(push_data),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .rx_rd_resp(rx_take && rx_is_resp), .rx_txn(rx_id),
        .rx_data(rx_pl[255:0]), .rx_wr_ack(rx_take && rx_is_ack),
        .wr_out(wr_out), .idle(req_idle)
    );

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        // Held, an unknown type sits at the head of the receive FIFO, raises
        // noc_in_busy for good, and wedges the instruction stream behind it.
        // Dropped silently, it is invisible. So: dropped, and named.
        if (rx_take && !rx_is_resp && !rx_is_ack && !rx_is_cud)
            $display("%0t rv_pe(%0d,%0d): flit type %0h dropped -- this unit does not consume it",
                     $time, POS_X, POS_Y, rx_ty);
        if (rx_take && rx_is_cud && (cst == CD_IDLE) && !nd_ok)
            $display("%0t ERROR rv_pe(%0d,%0d): CU_DATA buf_id %0d offset %0d len %0d does not fit a window -- burst counted out and discarded",
                     $time, POS_X, POS_Y, nd_buf, nd_off, nd_len);
        if (cd_data_beat && cd_is_word && cd_to_imem && (wp_be != 4'hF))
            $display("%0t ERROR rv_pe(%0d,%0d): a byte-enabled word push into the instruction window; that array has no byte enables and the whole word was written",
                     $time, POS_X, POS_Y);
        if (rx_take && rx_is_cud && (cst == CD_DATA) &&
            ((rx_sx != cd_sx) || (rx_sy != cd_sy)))
            $display("%0t ERROR rv_pe(%0d,%0d): a second sender interleaved into an open CU_DATA burst -- this unit reassembles one at a time",
                     $time, POS_X, POS_Y);
        if (cd_data_beat && (cd_left == 8'd1) && !rx_ls)
            $display("%0t ERROR rv_pe(%0d,%0d): CU_DATA burst ended by count with `last` clear -- two senders have interleaved",
                     $time, POS_X, POS_Y);
    end
`endif

endmodule

`default_nettype wire
