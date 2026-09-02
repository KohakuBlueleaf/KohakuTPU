// rv64_sys_pe -- the RV64 core as a compute unit the framework recognises.
//
// Phase 2 of the SysCore plan: the endpoint contract only. `noc_cu_base` for
// CU_INST / CU_SIGNAL / CU_CTRL, the CU_DATA loader with the inherited buf_id
// map, the kick/complete protocol, and a control region. The NoC memory
// requestor and the mover are phase 3 and are deliberately absent -- this unit
// reaches its own imem and spad and nothing else.
//
// HARVARD, UNLIKE THE STANDALONE HARNESS. `tests/rv64/link.ld` is flat because
// the harness answers any address; here .text goes to imem and everything the
// program loads or stores goes to spad, so a program built for this unit must
// use `link_pe.ld`.

`default_nettype none

module rv64_sys_pe #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer POS_X      = 2,
    parameter integer POS_Y      = 2,
    parameter integer IMEM_WORDS = 4096,      // 32-bit instruction words
    parameter integer SPAD_WORDS = 2048,      // 64-bit data words
    parameter integer INST_DEPTH = 16,
    parameter integer RECV_DEPTH = 32,
    parameter         MEM_PRIM   = "block",
    // MEASURED: `ultra` 289.9 MHz / 6,962 LUT / 1 URAM against `block` 280.9 /
    // 7,007 / 10 BRAM. URAM wins on the byte-write-enable path, which is the
    // opposite of what forcing BRAM was meant to prove.
    parameter         SPAD_STYLE = "ultra",
    // The core's own register file; `distributed` measured 329.8 MHz against
    // `block`'s 264.1 for 5 LUT.
    parameter         RF_PRIM    = "distributed",
    // Fetch stages between the PC and decode (rv64_core FETCH_LAT): 2 keeps
    // the instruction RAM's output register in front of decode.
    parameter integer FETCH_LAT  = 2,
    parameter [63:0]  SPAD_BASE  = 64'h0000_0000_0001_0000,
    parameter [63:0]  CTRL_BASE  = 64'h0000_0000_0002_0000
)(
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    input  wire                   halt_req,
    output wire                   busy,

    // Observation only -- counting, never gating. See design section 4.8.
    output wire [31:0]            dbg_cycles,
    output wire [31:0]            dbg_retired,
    output wire                   dbg_console_we,
    output wire [7:0]             dbg_console
);
    localparam [3:0] T_CU_SIGNAL = 4'h6, T_CU_DATA = 4'h8;
    localparam [7:0] BUF_SPAD = 8'd0, BUF_IMEM = 8'd1, BUF_SPAD_W = 8'd4;
    localparam [7:0] BUF_IMEM_W = 8'd5;
    localparam integer PAY = FLIT_WIDTH - 4*POS_WIDTH - 16;

    localparam integer IAW = $clog2(IMEM_WORDS);
    localparam integer SAW = $clog2(SPAD_WORDS);

    // ------------------------------------------------------------ the endpoint
    wire [FLIT_WIDTH-1:0] inst_flit, send_flit, recv_flit;
    wire                  inst_valid, send_valid, send_ready;
    wire                  recv_valid, recv_ready;
    reg                   inst_ready_r, exec_done, exec_fault;
    reg  [31:0]           exec_result;
    wire                  base_busy;

    assign send_flit  = {FLIT_WIDTH{1'b0}};
    assign send_valid = 1'b0;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y),
        .CU_TYPE(16'h5236), .CU_VERSION(8'h01), .N_BUFFERS(4),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH),
        .RECV_MEM("distributed")
    ) u_base (
        .clk           (clk),
        .resetn        (resetn),
        .noc_in_data   (noc_in_data),
        .noc_in_valid  (noc_in_valid),
        .noc_in_busy   (noc_in_busy),
        .noc_out_data  (noc_out_data),
        .noc_out_valid (noc_out_valid),
        .noc_out_busy  (noc_out_busy),
        .inst_flit     (inst_flit),
        .inst_valid    (inst_valid),
        .inst_ready    (inst_ready_r),
        .exec_done     (exec_done),
        .exec_result   (exec_result),
        .exec_fault    (exec_fault),
        .dbg_ctr       ({dbg_retired, dbg_cycles}),
        .send_flit     (send_flit),
        .send_valid    (send_valid),
        .send_ready    (send_ready),
        .recv_flit     (recv_flit),
        .recv_valid    (recv_valid),
        .recv_ready    (recv_ready),
        .inst_space    (),
        .busy          (base_busy)
    );

    // ------------------------------------------------------- the CU_DATA load
    wire [3:0] rx_ty = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    localparam integer SX_MSB = FLIT_WIDTH - 2*POS_WIDTH - 1;
    localparam integer SY_MSB = FLIT_WIDTH - 3*POS_WIDTH - 1;
    wire [POS_WIDTH-1:0] rx_sx = recv_flit[SX_MSB -: POS_WIDTH];
    wire [POS_WIDTH-1:0] rx_sy = recv_flit[SY_MSB -: POS_WIDTH];
    wire [PAY-1:0]       rx_pl = recv_flit[PAY-1:0];
    wire rx_is_cud = (rx_ty == T_CU_DATA);

    localparam [1:0] CD_IDLE = 2'd0, CD_DATA = 2'd1;
    reg  [1:0]   cst;
    reg  [7:0]   cd_buf, cd_left;
    reg  [15:0]  cd_off;
    reg  [POS_WIDTH-1:0] cd_sx, cd_sy;
    reg          cd_drop;
    reg  [255:0] gw_buf;
    reg  [2:0]   gw_cnt;
    reg          gw_busy, gw_imem;
    reg  [15:0]  gw_base;

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

    // A granule is 256 bits: 8 instruction words, or 4 spad words. `buf_id` 3 is
    // reserved and anything unknown is dropped rather than written somewhere.
    wire [16:0] nd_end = {1'b0, nd_off} + {9'd0, nd_len};
    wire nd_fits = nd_imem ? (nd_end < (IMEM_WORDS / 8))
                           : (nd_end < (SPAD_WORDS / 4));
    wire nd_ok   = (nd_gran || nd_word) && nd_fits;

    wire [2:0]  wp_sel  = rx_pl[38:36];
    wire [63:0] wp_data = rx_pl[63:0];

    wire cd_data_beat = rx_take && rx_is_cud && (cst == CD_DATA)
                     && (rx_sx == cd_sx) && (rx_sy == cd_sy);
    wire cd_word_wr = cd_data_beat && cd_is_word && !cd_drop;
    wire cd_gran_wr = cd_data_beat && cd_is_gran && !cd_drop;

    always @(posedge clk) begin
        if (!resetn) begin
            cst <= CD_IDLE; gw_busy <= 1'b0; gw_cnt <= 3'd0;
            cd_left <= 8'd0; cd_drop <= 1'b0;
        end
        else begin
            if (gw_busy) begin
                if (gw_cnt == 3'd7) begin
                    gw_busy <= 1'b0;
                end
                gw_cnt <= gw_cnt + 3'd1;
            end
            case (cst)
                // THE KICK MUST NOT OVERTAKE THE IMAGE IT IS THE DOORBELL FOR:
                // CU_INST and CU_DATA arrive on two queues, so the boot waits on
                // receive-quiet below.
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
                        gw_base <= cd_to_imem ? {cd_off[12:0], 3'd0}
                                              : {cd_off[13:0], 2'd0};
                        gw_cnt  <= 3'd0;
                        gw_busy <= 1'b1;
                        gw_imem <= cd_to_imem;
                    end
                    if (cd_left == 8'd1) begin
                        cst <= CD_IDLE;
                    end else begin
                        cd_left <= cd_left - 8'd1;
                    end
                end
                default: cst <= CD_IDLE;
            endcase
        end
    end

    // The granule is spooled one word per cycle rather than written as a wide
    // port, which is what keeps both memories at their natural width.
    wire        gw_imem_we = gw_busy && gw_imem;
    wire        gw_spad_we = gw_busy && !gw_imem && (gw_cnt < 3'd4);
    wire [31:0] gw_imem_d  = gw_buf[{gw_cnt, 5'd0} +: 32];
    wire [63:0] gw_spad_d  = gw_buf[{gw_cnt[1:0], 6'd0} +: 64];

    // ---------------------------------------------------------- the memories
    wire [63:0] imem_addr_c;
    wire [31:0] imem_data_c;

    wire [IAW-1:0] gw_imem_a = gw_base[IAW-1:0] + {{(IAW-3){1'b0}}, gw_cnt};
    wire [IAW-1:0] imem_wa   = gw_imem ? gw_imem_a : {IAW{1'b0}};
    wire           cd_imem_w = cd_word_wr && cd_to_imem;
    wire [IAW-1:0] imem_wa_w = cd_imem_w ? cd_off[IAW-1:0] : imem_wa;
    wire           imem_we   = gw_imem_we || cd_imem_w;
    wire [31:0]    imem_wd   = cd_imem_w ? wp_data[31:0] : gw_imem_d;

    // CASCADE 1: a four-deep RAMB36 chain is 1.939 ns from clock to data,
    // ahead of decode's first LUT in a 3.333 ns period; at FETCH_LAT 2 the
    // block's output register takes that, enabled by the core's advance.
    wire fetch_adv_c;
    kohaku_sdpram #(
        .WIDTH(32), .DEPTH(IMEM_WORDS), .MEM_PRIM(MEM_PRIM),
        .READ_LAT(FETCH_LAT), .CASCADE(1), .REG_CE(1)
    ) u_imem (
        .clk(clk),
        .wr_en(imem_we), .wr_addr(imem_wa_w), .wr_data(imem_wd),
        .rd_en((FETCH_LAT == 2) ? fetch_adv_c : 1'b1),
        .rd_addr(imem_addr_c[IAW+1:2]), .rd_data(imem_data_c)
    );

    wire [63:0] dmem_addr_c, dmem_wdata_c, dmem_rdata_c;
    wire [7:0]  dmem_wstrb_c;
    wire        dmem_re_c;

    wire in_spad = (dmem_addr_c >= SPAD_BASE)
                && (dmem_addr_c <  SPAD_BASE + (SPAD_WORDS * 8));
    wire in_ctrl = (dmem_addr_c >= CTRL_BASE)
                && (dmem_addr_c <  CTRL_BASE + 64'd256);

    wire [SAW-1:0] spad_ca = dmem_addr_c[SAW+2:3];
    wire           spad_core_we = in_spad && (dmem_wstrb_c != 8'd0);

    wire [SAW-1:0] spad_wa = gw_spad_we
        ? (gw_base[SAW-1:0] + {{(SAW-2){1'b0}}, gw_cnt[1:0]})
        : (cd_word_wr && !cd_to_imem) ? cd_off[SAW-1:0] : spad_ca;
    wire [63:0] spad_wd = gw_spad_we ? gw_spad_d
                        : (cd_word_wr && !cd_to_imem) ? wp_data : dmem_wdata_c;
    wire [7:0]  spad_we = gw_spad_we ? 8'hff
                        : (cd_word_wr && !cd_to_imem) ? 8'hff
                        : (spad_core_we ? dmem_wstrb_c : 8'd0);

    // Byte enables force an inferred array rather than `kohaku_sdpram`, which
    // has one enable for the whole word.
    (* ram_style = SPAD_STYLE *) reg [63:0] spad [0:SPAD_WORDS-1];
    reg [63:0] spad_q;
    integer bi;
    always @(posedge clk) begin
        for (bi = 0; bi < 8; bi = bi + 1) begin
            if (spad_we[bi]) begin
                spad[spad_wa][bi*8 +: 8] <= spad_wd[bi*8 +: 8];
            end
        end
        spad_q <= spad[spad_ca];
    end

    // ------------------------------------------------------ the control region
    // Program exit is a STORE, not ECALL -- ECALL has to stay a call, and the
    // framework's halt-and-report completion cannot move. Design section 3.3.
    localparam [7:0] R_EXIT = 8'h00, R_CONSOLE = 8'h08, R_DBELL = 8'h10;

    wire [7:0] ctrl_off  = dmem_addr_c[7:0];
    wire       ctrl_wr   = in_ctrl && (dmem_wstrb_c != 8'd0);
    reg        exit_hit;
    reg [31:0] exit_word;
    reg        dbell;
    reg        boot_v;
    wire       core_retire;

    always @(posedge clk) begin
        if (!resetn) begin
            exit_hit <= 1'b0;
            dbell    <= 1'b0;
        end
        else begin
            if (ctrl_wr && (ctrl_off == R_EXIT)) begin
                exit_hit  <= 1'b1;
                exit_word <= dmem_wdata_c[31:0];
            end
            if (ctrl_wr && (ctrl_off == R_DBELL)) begin
                dbell <= dmem_wdata_c[0];
            end
            if (boot_v) begin
                exit_hit <= 1'b0;
            end
        end
    end

    assign dbg_console_we = ctrl_wr && (ctrl_off == R_CONSOLE);
    assign dbg_console    = dmem_wdata_c[7:0];

    // REGISTERED, and the select with it: as a combinational mux this sat in the
    // core's load path (`e_s1_q_reg -> wb_val_reg`, 17 levels) and cost the PE
    // 52 MHz against the core alone. The spad is a 1-cycle read anyway, so the
    // control region matching it costs nothing.
    reg [63:0] ctrl_q;
    reg        in_ctrl_q;
    always @(posedge clk) begin
        ctrl_q    <= {63'd0, dbell};
        in_ctrl_q <= in_ctrl;
    end
    assign dmem_rdata_c = in_ctrl_q ? ctrl_q : spad_q;

    // --------------------------------------------------------------- the kick
    localparam [1:0] K_IDLE = 2'd0, K_START = 2'd1, K_RUN = 2'd2, K_DONE = 2'd3;
    reg  [1:0]  kst;
    reg  [7:0]  k_op;
    reg  [63:0] k_pc, k_arg;
    reg         halted_by_host;

    wire rx_quiet = !recv_valid && !gw_busy && (cst == CD_IDLE);
    wire [7:0]  i_op  = inst_flit[PAY-1 -: 8];
    wire [31:0] i_pc  = inst_flit[PAY-9 -: 32];
    wire [31:0] i_arg = inst_flit[PAY-41 -: 32];

    wire core_halted;
    wire [1:0] core_cause;

    always @(posedge clk) begin
        if (!resetn) begin
            kst <= K_IDLE; inst_ready_r <= 1'b0; boot_v <= 1'b0;
            exec_done <= 1'b0; exec_fault <= 1'b0; exec_result <= 32'd0;
            halted_by_host <= 1'b0;
        end
        else begin
            inst_ready_r <= 1'b0;
            boot_v       <= 1'b0;
            exec_done    <= 1'b0;
            if (halt_req) begin
                halted_by_host <= 1'b1;
            end

            case (kst)
                K_IDLE: begin
                    if (inst_valid && !inst_ready_r && rx_quiet) begin
                        k_op <= i_op;
                        k_pc <= {32'd0, i_pc};
                        k_arg <= {32'd0, i_arg};
                        inst_ready_r <= 1'b1;
                        halted_by_host <= 1'b0;
                        kst <= K_START;
                    end
                end
                K_START: begin
                    if (k_op == 8'd1) begin
                        boot_v <= 1'b1;
                        kst    <= K_RUN;
                    end else begin
                        kst <= K_DONE;
                    end
                end
                K_RUN: begin
                    if (core_halted || halted_by_host) begin
                        kst <= K_DONE;
                    end
                end
                default: begin
                    exec_done   <= 1'b1;
                    exec_result <= exit_word;
                    exec_fault  <= (core_cause == 2'd2) || (core_cause == 2'd3);
                    kst         <= K_IDLE;
                end
            endcase
        end
    end

    wire core_run = (kst == K_RUN);
    assign busy = base_busy || core_run;

    // ---------------------------------------------------------------- the core
    // Held in reset until the boot pulse, so the image is complete before a
    // single instruction is fetched.
    reg core_rstn;
    always @(posedge clk) begin
        if (!resetn) begin
            core_rstn <= 1'b0;
        end else if (boot_v) begin
            core_rstn <= 1'b1;
        end else if (kst == K_DONE) begin
            core_rstn <= 1'b0;
        end
    end

    rv64_core #(
        .RESET_PC(64'd0), .MEM_PRIM(RF_PRIM), .FETCH_LAT(FETCH_LAT)
    ) u_core (
        .clk(clk), .resetn(core_rstn),
        .imem_addr(imem_addr_c), .imem_data(imem_data_c),
        .fetch_adv(fetch_adv_c),
        .dmem_addr(dmem_addr_c), .dmem_wdata(dmem_wdata_c),
        .dmem_wstrb(dmem_wstrb_c), .dmem_re(dmem_re_c),
        .dmem_rdata(dmem_rdata_c), .dmem_stall(1'b0),
        // A physical-address machine: nothing translates, nothing faults.
        .imem_stall(1'b0), .imem_fault(1'b0),
        .dmem_fault(1'b0), .dmem_fault_cause(4'd0),
        .irq_ext(1'b0), .irq_soft(dbell),
        .ext_halt(exit_hit || halted_by_host),
        .halted(core_halted), .halt_cause(core_cause), .halt_pc(),
        .dbg_pc(), .dbg_retire(core_retire)
    );

    // ------------------------------------------------------------- observation
    // Cleared by the BOOT, not by the core's reset: the core goes back into
    // reset at K_DONE, and counters cleared there would read zero to the host.
    reg [31:0] cyc_ctr, ret_ctr;
    always @(posedge clk) begin
        if (!resetn || boot_v) begin
            cyc_ctr <= 32'd0;
            ret_ctr <= 32'd0;
        end
        else if (core_rstn) begin
            cyc_ctr <= cyc_ctr + 32'd1;
            if (core_retire) begin
                ret_ctr <= ret_ctr + 32'd1;
            end
        end
    end
    assign dbg_cycles  = cyc_ctr;
    assign dbg_retired = ret_ctr;

endmodule

`default_nettype wire
