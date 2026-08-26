// kht_pe -- the assembled SIMT PE: a SIMT core, its windows, its internal L1,
// its NoC requestor, and noc_cu_base.
//
// It attaches exactly like every other compute unit -- one local port, one
// instruction FIFO, the four CU_CTRL registers -- so a driver enumerates it
// without knowing it runs shaders. What a CU_INST means here is the same thing
// it means to the controller PE: a KICK, and the unit retires when the program
// halts.
//
// THE WHOLE UNIT IS ON noc_clk AND THERE IS NO CDC IN IT, for the same reason
// the controller PE has none: the requestor speaks the port contract in that
// domain, so the core joins it rather than being bridged into it.
//
// BOOT IS NOT A MECHANISM. A shader image arrives as a CU_DATA burst into the
// instruction window, its constants as another into the scratchpad, and then
// the standard kick -- the same write path every unit has, which is why there
// is no loader here to go wrong.
//
// THE KICK'S `op` IS THE WAVE COUNT, clamped to what the build carries. op 1 is
// the single-wave case it always was, so this generalises the field rather than
// redefining it, and a launch needs no new protocol word.
//
// buf_id allocation, as flit-format requires a unit to publish:
//
//   0  scratchpad window, raw 32-byte granules      (constants, arguments)
//   1  instruction window, raw 32-byte granules     (the shader image)
//   3  RESERVED to the framework -- rejected here
//   4  scratchpad window, one 32-bit word, byte enabled
//   5  instruction window, one 32-bit word
//   everything else rejected, counted out, and reported.

`default_nettype none

module kht_pe #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer POS_X      = 1,
    parameter integer POS_Y      = 1,
    parameter integer MEM_X      = 0,
    parameter integer MEM_Y      = 1,
    parameter [39:0]  DRAM_BASE  = 40'h00_0000_0000,
    parameter integer IMEM_WORDS = 2048,
    parameter integer SPAD_WORDS = 2048,
    parameter integer L1_LINES   = 128,
    parameter integer LANES      = 8,
    parameter integer WAVES      = 16,
    parameter integer HAS_MASK   = 1,
    parameter integer HAS_IPDOM  = 1,
    // ONE COUNT EACH: 0 is not built, -1 is full rate (one bank or unit per
    // lane). The booleans that used to sit beside these are derived below.
    parameter integer LDS_BANKS  = -1,
    parameter integer SHFL_UNITS = -1,
    // G9. Eight float lanes is the rendering target's configuration, not a
    // stepping stone: a mesh is 8 DSP + 4 SIMT PEs and 8*4 + 4*8 = 64 FP FMA per
    // clock, which is one Mali-G610 shader core.
    // Three independent unit counts, 0 = not built. Carried here because
    // kht_core has them and this is the only top a build instantiates: without
    // them a count could not be set at all, and `unbuilt` would fault every
    // seed in every real machine.
    parameter integer FLANES     = 0,
    parameter integer FSFU_UNITS = 0,
    parameter integer IPDOM_D    = 8,
    parameter         MEM_PRIM   = "block",
    parameter         VREG_PRIM  = "block",
    parameter integer INST_DEPTH = 16,
    // A 288-bit FIFO is 4 RAMB36 at any depth to 512, so the deep receive queue
    // is +34 LUT and the shallow one buys nothing.
    parameter integer RECV_DEPTH = 512
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

    output wire                   dbg_run,
    output wire                   dbg_halted,
    output wire [31:0]            dbg_retire_pc,
    output wire                   dbg_retire_valid,
    output wire [LANES-1:0]       dbg_mask,
    output wire [31:0]            dbg_reqs,
    output wire [31:0]            dbg_gathers
);
    // Derived, not passed: a count already says whether the unit exists.
    localparam integer HAS_SHFL    = (SHFL_UNITS != 0) ? 1 : 0;
    localparam integer HAS_LDSBANK = (LDS_BANKS != 0) ? 1 : 0;

    localparam integer IAW = $clog2(IMEM_WORDS);
    localparam integer SAW = $clog2(SPAD_WORDS);

    localparam [3:0] T_MEM_RD_RESP = 4'h2, T_MEM_WR_ACK = 4'h3;
    localparam [3:0] T_CU_DATA = 4'h8;

    localparam [7:0] BUF_SPAD = 8'd0, BUF_IMEM = 8'd1, BUF_SPAD_W = 8'd4;
    localparam [7:0] BUF_IMEM_W = 8'd5;

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
        .CU_TYPE(16'h4750), .CU_VERSION(8'h01), .N_BUFFERS(4),
        // RECV_MEM WAS NEVER PASSED, so the 512-deep 288-bit queue below built
        // in DISTRIBUTED RAM: 2,560 LUTRAM and 3,362 LUT, 15% of the whole PE,
        // for a buffer that holds no logic. The header above assumed the block
        // form and the parameter defaults to the other one.
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH), .RECV_MEM("block")
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
    reg  [15:0]  gw_base;

    // Declared here because `recv_ready` below needs it; driven further down
    // with the rest of the registered word-write path.
    reg            wq_v, wq_imem;
    reg [3:0]      wq_be;
    reg [15:0]     wq_off;
    reg [2:0]      wq_sel;
    reg [31:0]     wq_data;

    // Low only while a granule is walked into an array -- cleared by this
    // unit's own progress, never by another flit arriving.
    // ALSO low for the one cycle a WORD write is registered, so the registered
    // word and a granule walk can never want the same port on the same cycle.
    assign recv_ready = !gw_busy && !wq_v;
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
    wire [16:0] nd_end = {1'b0, nd_off} + {9'd0, nd_len};
    wire nd_fits = nd_imem ? (nd_end < (IMEM_WORDS / 8))
                           : (nd_end < (SPAD_WORDS / 8));
    wire nd_ok   = (nd_gran || nd_word) && nd_fits;

    wire [2:0]  wp_sel  = rx_pl[38:36];
    wire [3:0]  wp_be   = rx_pl[35:32];
    wire [31:0] wp_data = rx_pl[31:0];

    wire cd_data_beat = (
        rx_take
        && rx_is_cud
        && (cst == CD_DATA)
        && (rx_sx == cd_sx)
        && (rx_sy == cd_sy)
    );
    wire cd_word_wr   = cd_data_beat && cd_is_word && !cd_drop;
    wire cd_gran_wr   = cd_data_beat && cd_is_gran && !cd_drop;

    // THE WORD WRITE IS REGISTERED, because the receive queue is block RAM and
    // its output was reaching the LDS banks' write enables through seven levels
    // -- the same "a cone that starts at a block RAM" shape that cost this PE
    // 22 MHz the moment the queue stopped being LUTRAM. The granule path was
    // already registered (`gw_buf`); this gives the word path the same footing.
    // Image load is a burst, so a cycle here buys the clock back for free.
    always @(posedge clk) begin
        if (!resetn) begin
            wq_v <= 1'b0;
        end
        else begin
            wq_v <= cd_word_wr;
        end
        wq_imem <= cd_to_imem;
        wq_be   <= wp_be;
        wq_off  <= cd_off;
        wq_sel  <= wp_sel;
        wq_data <= wp_data;
    end

    wire        spad_a_en   = (wq_v && !wq_imem) || (gw_busy && !gw_imem);
    wire [3:0]  spad_a_we   = gw_busy ? 4'hF : wq_be;
    wire [SAW-1:0] spad_a_addr = gw_busy ? {gw_base[SAW-1:3], gw_cnt}
                                         : {wq_off[SAW-4:0], wq_sel};
    // ONE GRANULE WORD FOR BOTH WINDOWS. The scratchpad and the instruction
    // window take the SAME 32 bits and differ only in their enables, so writing
    // the select out twice builds the 8:1 over `gw_buf` twice.
    wire [31:0] cd_wdata = gw_busy ? gw_buf[{gw_cnt, 5'd0} +: 32] : wq_data;

    wire [31:0] spad_a_wdata= cd_wdata;

    wire        imem_wr_en   = (wq_v && wq_imem) || (gw_busy && gw_imem);
    wire [IAW-1:0] imem_wr_a = gw_busy ? {gw_base[IAW-1:3], gw_cnt}
                                       : {wq_off[IAW-4:0], wq_sel};
    wire [31:0] imem_wr_d    = cd_wdata;

    always @(posedge clk) begin
        if (!resetn) begin
            cst     <= CD_IDLE;
            gw_busy <= 1'b0;
            gw_cnt  <= 3'd0;
            cd_left <= 8'd0;
            cd_drop <= 1'b0;
        end else begin
            if (gw_busy) begin
                if (gw_cnt == 3'd7) begin
                    gw_busy <= 1'b0;
                end
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
                    if (cd_left == 8'd1) begin
                        cst <= CD_IDLE;
                    end
                    else begin
                        cd_left <= cd_left - 8'd1;
                    end
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
                // noc_cu_base sorts CU_INST and CU_DATA into different queues, so
                // the framework preserves order on the wire but not across them:
                // the kick can reach the head while the last granule of the shader
                // is still being walked in. `rx_quiet` is cleared by this unit's
                // own progress, so waiting on it cannot deadlock.
                K_IDLE: if (inst_valid && !inst_ready_r && rx_quiet) begin
                    k_op         <= i_op;
                    k_pc         <= i_pc;
                    k_arg        <= i_arg;
                    inst_ready_r <= 1'b1;
                    kst          <= K_START;
                end
                // THE OP IS THE WAVE COUNT. op 1 launches one wave, which is
                // exactly what it meant before, so no existing caller changes.
                K_START: if (k_op != 8'd0) begin
                    boot_v <= 1'b1;
                    kst    <= K_RUN;
                end
                else begin
                    kst <= K_DONE;
                end

                // "Halted" is not enough: the completion is the host's only
                // sequencing point, so it must also mean every write this shader
                // issued has been ACKNOWLEDGED, not merely sent.
                K_RUN: begin
                    if (
                        core_halted
                        && pipe_empty
                        && req_idle
                        && (wr_out == 16'd0)
                    ) begin
                        kst <= K_DONE;
                    end
                end

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

    wire                    lds_req, lds_we, lds_done;
    wire [LANES-1:0]        lds_mask;
    wire [LANES*SAW-1:0]    lds_addr;
    wire [LANES*32-1:0]     lds_wdata, lds_rdata;
    wire [31:0]             lds_passes;

    kht_core #(
        .LANES(LANES), .WAVES(WAVES), .HAS_MASK(HAS_MASK),
        .HAS_IPDOM(HAS_IPDOM), .LDS_BANKS(LDS_BANKS),
        .SHFL_UNITS(SHFL_UNITS),
        .FLANES(FLANES), .FSFU_UNITS(FSFU_UNITS),
        .IPDOM_D(IPDOM_D),
        .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS),
        .VREG_PRIM(VREG_PRIM)
    ) u_core (
        .clk(clk), .resetn(resetn),
        .boot_v(boot_v), .boot_pc(k_pc), .boot_n(k_op),
        .run(core_run), .halted(core_halted), .cause(core_cause),
        .halt_word(core_halt_word), .pipe_empty(pipe_empty),
        .arg(k_arg), .coreid({POS_Y[3:0], POS_X[3:0]}), .wr_out(wr_out),
        .imem_addr(imem_addr), .imem_data(imem_data), .imem_ctrl(imem_ctrl),
        .spad_addr(spad_addr), .spad_we(spad_we), .spad_wdata(spad_wdata),
        .spad_rdata(spad_rdata),
        .lds_req(lds_req), .lds_we(lds_we), .lds_mask(lds_mask),
        .lds_addr(lds_addr), .lds_wdata(lds_wdata), .lds_rdata(lds_rdata),
        .lds_done(lds_done), .lds_passes(lds_passes),
        .l1_probe(l1_probe), .l1_req(l1_req), .l1_we(l1_we), .l1_be(l1_be),
        .l1_addr(l1_addr), .l1_wdata(l1_wdata), .l1_rdata(l1_rdata),
        .l1_stall(l1_stall), .l1_flush(l1_flush), .l1_inval(l1_inval),
        .l1_flush_busy(l1_flush_busy),
        .dbg_retire_pc(dbg_retire_pc), .dbg_retire_valid(dbg_retire_valid),
        .dbg_mask(dbg_mask),
        .cycle_ctr(cyc_ctr), .instret_ctr(ret_ctr),
        .req_ctr(dbg_reqs), .gather_ctr(dbg_gathers)
    );

    assign dbg_run    = core_run;
    assign dbg_halted = core_halted;

    rv_imem #(.WORDS(IMEM_WORDS), .MEM_PRIM(MEM_PRIM)) u_imem (
        .clk(clk),
        .wr_en(imem_wr_en), .wr_addr(imem_wr_a), .wr_data(imem_wr_d),
        .rd_addr(imem_addr), .rd_data(imem_data)
    );

    // DECODE ON THE WRITE SIDE. A shader word is decoded once as it lands and
    // the result is stored beside it, so the core reads control out of memory
    // instead of computing it between the window and its own registers. The
    // base core gets the same effect from rv_id's registered outputs; this
    // costs no cycle, which a decode stage would.
    wire [59:0] imem_ctrl, imem_wr_c;
    kht_predec u_predec (.instr(imem_wr_d), .ctrl(imem_wr_c));

    kohaku_sdpram #(.WIDTH(60), .DEPTH(IMEM_WORDS), .MEM_PRIM(MEM_PRIM),
                    .READ_LAT(1)) u_ictl (
        .clk(clk),
        .wr_en(imem_wr_en), .wr_addr(imem_wr_a), .wr_data(imem_wr_c),
        .rd_en(1'b1), .rd_addr(imem_addr), .rd_data(imem_ctrl)
    );

    // The window side is identical either way -- kht_lds decodes the interleave
    // itself, so a CU_DATA burst does not know which one is behind it.
    generate
    if (HAS_LDSBANK == 0) begin : g_flatlds
        assign lds_rdata  = {(LANES*32){1'b0}};
        assign lds_done   = 1'b0;
        assign lds_passes = 32'd0;
        rv_spad #(.WORDS(SPAD_WORDS), .MEM_PRIM(MEM_PRIM)) u_spad (
            .clk(clk),
            .a_en(spad_a_en), .a_we(spad_a_we), .a_addr(spad_a_addr),
            .a_wdata(spad_a_wdata), .a_rdata(),
            .b_addr(spad_addr), .b_we(spad_we), .b_wdata(spad_wdata),
            .b_rdata(spad_rdata)
        );
    end else begin : g_banklds
        // The serial walk's own LDS port is gone: with the banked path every
        // LDS access goes through the resolver, so a second face on the same
        // memory would be a second write port and a coherence question.
        assign spad_rdata = 32'd0;
        kht_lds #(.LANES(LANES), .WORDS(SPAD_WORDS), .MEM_PRIM(MEM_PRIM),
                  .BANKS(LDS_BANKS)) u_lds (
            .clk(clk), .resetn(resetn),
            .a_en(spad_a_en), .a_we(spad_a_we), .a_addr(spad_a_addr),
            .a_wdata(spad_a_wdata),
            .req(lds_req), .we(lds_we), .lmask(lds_mask), .laddr(lds_addr),
            .lwdata(lds_wdata), .lrdata(lds_rdata),
            .busy(), .done(lds_done), .pass_ctr(lds_passes)
        );
    end
    endgenerate

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

    // The peer-push path is tied off: a GPU wave reaches another unit through
    // memory, not through a per-lane push, and an unused port is better tied
    // than half-built.
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
        .push_valid(1'b0), .push_ready(),
        .push_dx({POS_WIDTH{1'b0}}), .push_dy({POS_WIDTH{1'b0}}),
        .push_win(1'b0), .push_gran(14'd0), .push_sel(3'd0), .push_be(4'd0),
        .push_data(32'd0),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .rx_rd_resp(rx_take && rx_is_resp), .rx_txn(rx_id),
        .rx_data(rx_pl[255:0]), .rx_wr_ack(rx_take && rx_is_ack),
        .wr_out(wr_out), .idle(req_idle)
    );

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        // Held, an unknown type sits at the head of the receive FIFO and raises
        // noc_in_busy for good. Dropped silently, it is invisible. So: dropped,
        // and named.
        if (rx_take && !rx_is_resp && !rx_is_ack && !rx_is_cud) begin
            $display("%0t kht_pe(%0d,%0d): flit type %0h dropped -- this unit does not consume it",
                     $time, POS_X, POS_Y, rx_ty);
        end
        if (rx_take && rx_is_cud && (cst == CD_IDLE) && !nd_ok) begin
            $display("%0t ERROR kht_pe(%0d,%0d): CU_DATA buf_id %0d offset %0d len %0d does not fit a window -- burst counted out and discarded",
                     $time, POS_X, POS_Y, nd_buf, nd_off, nd_len);
        end
        if (
            rx_take
            && rx_is_cud
            && (cst == CD_DATA)
            && ((rx_sx != cd_sx) || (rx_sy != cd_sy))
        ) begin
            $display("%0t ERROR kht_pe(%0d,%0d): a second sender interleaved into an open CU_DATA burst",
                     $time, POS_X, POS_Y);
        end
        if (cd_data_beat && (cd_left == 8'd1) && !rx_ls) begin
            $display("%0t ERROR kht_pe(%0d,%0d): CU_DATA burst ended by count with `last` clear -- two senders have interleaved",
                     $time, POS_X, POS_Y);
        end
    end
`endif

endmodule

`default_nettype wire
