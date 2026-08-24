// rv_mag_pe -- the system node's control processor, assembled. Scalar core,
// the memory mover as its SIMD memory unit, and the transform slot as that
// unit's extension. All three are ONE thing: the mover has no command window
// of its own and the slot is reached only through the mover's read return.
//
//   memory   rv_l1 -> rv_mag_req -> MAG's converged path      (not flits)
//   flits    rv_noc_req at MEM_PATH=0 -- dispatch and completions only
//   mover    mv_exec on the cfg port; `mv.go` is a STORE, not an opcode
//
// Decoding mv.go from an address rather than an opcode keeps the ISA unchanged
// and matches the framework rule that control is a range, not a side channel.
//
// (0,0) IS NOT A CHOICE -- a corner touches no router, so no mesh map can put
// anything there and the coordinate is free by construction. sn_hub.v has why.

`default_nettype none

module rv_mag_pe #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer ADDR_W     = 40,
    parameter integer DATA_W     = 256,
    parameter integer ID_W       = 4,
    parameter integer IMEM_WORDS = 2048,
    parameter integer SPAD_WORDS = 2048,
    parameter integer L1_LINES   = 128,
    parameter integer INST_DEPTH = 16,
    parameter integer RECV_DEPTH = 32,
    // The transform slot. Selection is an ID, not a bit per transform, so the
    // field is sized for several occupants even though the reference project
    // ships one -- widening it later is a protocol change.
    parameter integer XFORM_SLOTS     = 1,
    parameter integer XID_W           = 4,
    parameter integer XMODE_W         = 4,
    // Declared by the occupant, needed by the engine before it has run.
    parameter integer XFORM_IN_BITS   = 2048,
    parameter integer XFORM_OUT_WORDS = 4,
    parameter         MEM_PRIM   = "block"
)(
    input  wire                   clk,
    input  wire                   resetn,

    // ---- flits: a client of sn_hub, with no port of its own ----
    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    // ---- memory: MAG's converged path ----
    output wire [ADDR_W-1:0]      cp_awaddr,
    output wire [7:0]             cp_awlen,
    output wire                   cp_awvalid,
    input  wire                   cp_awready,
    output wire [DATA_W-1:0]      cp_wdata,
    output wire [DATA_W/8-1:0]    cp_wstrb,
    output wire                   cp_wlast,
    output wire                   cp_wvalid,
    input  wire                   cp_wready,
    input  wire                   cp_bvalid,
    output wire                   cp_bready,
    output wire [ADDR_W-1:0]      cp_araddr,
    output wire [7:0]             cp_arlen,
    output wire                   cp_arvalid,
    input  wire                   cp_arready,
    input  wire [DATA_W-1:0]      cp_rdata,
    input  wire                   cp_rlast,
    input  wire                   cp_rvalid,
    output wire                   cp_rready,

    // ---- the mover's AXI master: channel MV on MAG's converged path ----
    // The DATAPATH stays separate so a 32 B/cycle walk never contends with L1
    // fills in the same channel. Only the CONTROL collapsed into the pipeline.
    output wire [ID_W-1:0]        mv_awid,
    output wire [ADDR_W-1:0]      mv_awaddr,
    output wire [7:0]             mv_awlen,
    output wire [2:0]             mv_awsize,
    output wire [1:0]             mv_awburst,
    output wire                   mv_awvalid,
    input  wire                   mv_awready,
    output wire [DATA_W-1:0]      mv_wdata,
    output wire [DATA_W/8-1:0]    mv_wstrb,
    output wire                   mv_wlast,
    output wire                   mv_wvalid,
    input  wire                   mv_wready,
    input  wire [ID_W-1:0]        mv_bid,
    input  wire [1:0]             mv_bresp,
    input  wire                   mv_bvalid,
    output wire                   mv_bready,
    output wire [ID_W-1:0]        mv_arid,
    output wire [ADDR_W-1:0]      mv_araddr,
    output wire [7:0]             mv_arlen,
    output wire [2:0]             mv_arsize,
    output wire [1:0]             mv_arburst,
    output wire                   mv_arvalid,
    input  wire                   mv_arready,
    input  wire [ID_W-1:0]        mv_rid,
    input  wire [DATA_W-1:0]      mv_rdata,
    input  wire [1:0]             mv_rresp,
    input  wire                   mv_rlast,
    input  wire                   mv_rvalid,
    output wire                   mv_rready,

    // The host's AUX_CFG window, forwarded by MAG. Below offset 0x80 is the
    // mover's; the processor's own store WINS when both pulse.
    input  wire                   aux_cfg_en,
    input  wire [7:0]             aux_cfg_addr,
    input  wire [63:0]            aux_cfg_data,
    input  wire                   ilink_on,
    output wire                   mv_busy,
    output wire [3:0]             mv_fault,
    output wire [31:0]            mv_done,

    // ---- host-visible, mirrored into AUX_STAT ----
    input  wire                   halt_req,
    output wire [63:0]            pe_status,
    output wire                   busy
);
    localparam integer IAW = $clog2(IMEM_WORDS);
    localparam integer SAW = $clog2(SPAD_WORDS);

    localparam integer POS_X = 0;
    localparam integer POS_Y = 0;

    localparam [3:0] T_MEM_RD_RESP = 4'h2, T_MEM_WR_ACK = 4'h3;
    localparam [3:0] T_CU_SIGNAL = 4'h6, T_CU_DATA = 4'h8;
    localparam [7:0] BUF_SPAD = 8'd0, BUF_IMEM = 8'd1, BUF_SPAD_W = 8'd4;
    localparam [7:0] BUF_IMEM_W = 8'd5;

    localparam integer PAY = FLIT_WIDTH - 4*POS_WIDTH - 16;

    // The node's own control range. A store here is a command, never a line.
    localparam [31:0] NODE_MVGO = 32'hF000_0000;

    // ------------------------------------------------------------- the base
    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit, send_flit;
    wire                  inst_valid, recv_valid, send_valid, send_ready;
    wire                  recv_ready;
    reg                   inst_ready_r, exec_done, exec_fault;
    reg  [31:0]           exec_result;

    wire [31:0] cyc_ctr, ret_ctr;
    wire        base_busy, core_run, core_halted, pipe_empty;
    wire [1:0]  core_cause;
    wire [31:0] core_halt_word;
    wire        req_idle, mag_idle;
    wire [15:0] wr_out_noc, wr_out_mag;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y),
        .CU_TYPE(16'h5243), .CU_VERSION(8'h01), .N_BUFFERS(4),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH),
        .RECV_MEM("distributed")
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

    // --------------------------------------------------- receive and windows
    wire [3:0] rx_ty = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire [7:0] rx_id = recv_flit[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];
    wire [POS_WIDTH-1:0] rx_sx = recv_flit[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] rx_sy = recv_flit[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
    wire [PAY-1:0]       rx_pl = recv_flit[PAY-1:0];

    wire rx_is_resp = (rx_ty == T_MEM_RD_RESP);
    wire rx_is_ack  = (rx_ty == T_MEM_WR_ACK);
    wire rx_is_sig  = (rx_ty == T_CU_SIGNAL);
    wire rx_is_cud  = (rx_ty == T_CU_DATA);

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
    wire [16:0] nd_end = {1'b0, nd_off} + {9'd0, nd_len};
    wire nd_fits = nd_imem ? (nd_end < (IMEM_WORDS / 8))
                           : (nd_end < (SPAD_WORDS / 8));
    wire nd_ok = (nd_gran || nd_word) && nd_fits;

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

    always @(posedge clk) begin
        if (!resetn) begin
            cst <= CD_IDLE; gw_busy <= 1'b0; gw_cnt <= 3'd0;
            cd_left <= 8'd0; cd_drop <= 1'b0;
        end else begin
            if (gw_busy) begin
                if (gw_cnt == 3'd7) begin
                    gw_busy <= 1'b0;
                end
                gw_cnt <= gw_cnt + 3'd1;
            end
            case (cst)
                CD_IDLE: if (rx_take && rx_is_cud) begin
                    cd_buf <= nd_buf; cd_off <= nd_off;
                    cd_left <= nd_len + 8'd1;
                    cd_sx <= rx_sx; cd_sy <= rx_sy;
                    cd_drop <= !nd_ok;
                    cst <= CD_DATA;
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

    // ------------------------------------------------------------- the kick
    localparam [1:0] K_IDLE = 2'd0, K_START = 2'd1, K_RUN = 2'd2, K_DONE = 2'd3;
    reg  [1:0]  kst;
    reg  [7:0]  k_op;
    reg  [31:0] k_pc, k_arg;
    reg         boot_v, halted_by_host;

    wire rx_quiet = !recv_valid && !gw_busy && (cst == CD_IDLE);
    wire [7:0]  i_op  = inst_flit[PAY-1 -: 8];
    wire [31:0] i_pc  = inst_flit[PAY-9 -: 32];
    wire [31:0] i_arg = inst_flit[PAY-41 -: 32];

    always @(posedge clk) begin
        if (!resetn) begin
            kst <= K_IDLE; inst_ready_r <= 1'b0; boot_v <= 1'b0;
            exec_done <= 1'b0; exec_fault <= 1'b0; exec_result <= 32'd0;
            halted_by_host <= 1'b0;
        end else begin
            inst_ready_r <= 1'b0;
            boot_v       <= 1'b0;
            exec_done    <= 1'b0;
            if (halt_req) begin
                halted_by_host <= 1'b1;
            end

            case (kst)
                // The kick must not overtake the image it is the doorbell for.
                K_IDLE: if (inst_valid && !inst_ready_r && rx_quiet) begin
                    k_op <= i_op; k_pc <= i_pc; k_arg <= i_arg;
                    inst_ready_r <= 1'b1;
                    halted_by_host <= 1'b0;
                    kst <= K_START;
                end
                K_START: if (k_op == 8'd1) begin
                    boot_v <= 1'b1;
                    kst    <= K_RUN;
                end
                else begin
                    kst <= K_DONE;
                end
                // Retire only once every write this program issued has LANDED --
                // both requesters, because it has two.
                K_RUN: begin
                    if (
                        (core_halted || halted_by_host)
                        && pipe_empty
                        && req_idle
                        && mag_idle
                        && (wr_out_noc == 16'd0)
                        && (wr_out_mag == 16'd0)
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

    // -------------------------------------------------------------- the core
    wire [IAW-1:0] imem_addr;
    wire [31:0]    imem_data;
    wire [SAW-1:0] spad_addr;
    wire [3:0]     spad_we;
    wire [31:0]    spad_wdata, spad_rdata;

    wire [31:0] l1_probe, l1_addr, l1_wdata, l1_rdata_raw;
    wire        l1_req_core, l1_we, l1_stall_raw, l1_flush, l1_inval,
                l1_flush_busy;
    wire [3:0]  l1_be;

    wire                 push_valid, push_ready, push_win;
    wire [POS_WIDTH-1:0] push_dx, push_dy;
    wire [13:0]          push_gran;
    wire [2:0]           push_sel;
    wire [3:0]           push_be;
    wire [31:0]          push_data;

    // ARG and PC land in registers; the OP store fires the dispatch.
    wire        disp_wr, disp_fire, disp_ready;
    wire [2:0]  disp_sel, disp_rsvd;
    wire [POS_WIDTH-1:0] disp_dx, disp_dy;
    wire [7:0]  disp_txn;
    wire        disp_last;
    wire [31:0] disp_data;
    wire        sig_pop;
    wire [7:0]  sig_cnt, sig_code, sig_id;
    wire        sig_ovf;
    wire [31:0] sig_arg;

    reg  [31:0] ds_pc, ds_arg;
    always @(posedge clk) if (disp_wr) begin
        if (disp_sel == 3'd0) begin
            ds_arg <= disp_data;
        end
        if (disp_sel == 3'd1) begin
            ds_pc  <= disp_data;
        end
    end
    wire        disp_valid = disp_fire;
    wire [7:0]  disp_op    = disp_data[7:0];
    wire [31:0] disp_pc    = ds_pc;
    wire [31:0] disp_arg   = ds_arg;

    wire [7:0]  rx_sig_code = rx_pl[PAY-1 -: 8];
    wire [31:0] rx_sig_arg  = rx_pl[PAY-9 -: 32];

    // Declared ahead of use: xvlog rejects a forward reference, and letting it
    // slide is how a 288-bit net became 1 bit elsewhere in this project.
    wire        mv_exec_busy;
    wire        mv_sp_req;
    wire [SAW-1:0] mv_sp_addr;
    wire [31:0] mv_sp_rdata;

    // ---- the node control range: a command, never a cache line ------------
    wire is_node = l1_req_core && (l1_addr[31:28] == 4'hF);
    // Bit 16 splits the range: below it the node's own registers, at or above
    // it the transform slot's occupants, indexed by id. A register the
    // processor can read and one it can write are the same mechanism.
    wire is_slot = is_node && l1_addr[16];
    wire mv_go   = is_node && !is_slot && l1_we && (l1_addr[15:0] == 16'd0);

    wire                xcfg_en   = is_slot && l1_we;
    wire [XID_W-1:0]    xcfg_id   = l1_addr[8 +: XID_W];
    wire [7:0]          xcfg_addr = l1_addr[7:0];
    wire [31:0]         xcfg_data = l1_wdata;
    wire [31:0]         xcfg_rdata;
    wire [3:0]          xf_fault;

    // DISJOINT FIELDS. These were OR-ed into one word, so bit 0 read
    // `fault[0] | busy` -- a poll loop on bit 0 spins forever on fault code 1
    // (index length), and no code could tell a fault from a move in flight.
    //   [0]     busy, and mv_exec's own busy already spans the whole move
    //   [7:4]   mover fault, 0 = none
    //   [11:8]  the occupant's own fault, beside the mover's rather than
    //           merged into it
    wire [31:0] node_word = is_slot
        ? xcfg_rdata
        : {20'd0, xf_fault, mv_fault[3:0], 3'd0, mv_exec_busy};

    // HELD ONE CYCLE, because rv_l1 answers in WB (rv_l1.v:51) and a node read
    // has to arrive with it. Combinational, it is sampled with `l1_req` already
    // low, `is_node` false, and the L1 array's word returned in its place --
    // which is why a status load read zero however the mover was doing.
    reg        node_r;
    reg [31:0] node_data_r;
    always @(posedge clk) begin
        if (!resetn) begin
            node_r <= 1'b0;
        end else begin
            node_r      <= is_node && !l1_we;
            node_data_r <= node_word;
        end
    end

    wire l1_req   = l1_req_core && !is_node;
    wire l1_stall = is_node ? 1'b0 : l1_stall_raw;
    wire [31:0] l1_rdata = node_r ? node_data_r : l1_rdata_raw;

    rv_core #(
        .IMEM_WORDS(IMEM_WORDS), .SPAD_WORDS(SPAD_WORDS),
        .POS_WIDTH(POS_WIDTH), .SIMD_EN(0), .MEM_PRIM(MEM_PRIM)
    ) u_core (
        .clk(clk), .resetn(resetn),
        .boot_v(boot_v), .boot_pc(k_pc),
        .run(core_run), .halted(core_halted), .cause(core_cause),
        .halt_word(core_halt_word), .pipe_empty(pipe_empty),
        // The core's own writes go through rv_mag_req, so that is the count it
        // must see -- flushing against the flit requestor would never settle.
        .coreid({POS_Y[3:0], POS_X[3:0]}), .arg(k_arg), .wr_out(wr_out_mag),
        .imem_addr(imem_addr), .imem_data(imem_data),
        .spad_addr(spad_addr), .spad_we(spad_we), .spad_wdata(spad_wdata),
        .spad_rdata(spad_rdata),
        .l1_probe(l1_probe), .l1_req(l1_req_core), .l1_we(l1_we), .l1_be(l1_be),
        .l1_addr(l1_addr), .l1_wdata(l1_wdata), .l1_rdata(l1_rdata),
        .l1_stall(l1_stall), .l1_flush(l1_flush), .l1_inval(l1_inval),
        .l1_flush_busy(l1_flush_busy),
        .push_valid(push_valid), .push_ready(push_ready),
        .push_dx(push_dx), .push_dy(push_dy), .push_win(push_win),
        .push_gran(push_gran), .push_sel(push_sel), .push_be(push_be),
        .push_data(push_data),
        .disp_wr(disp_wr), .disp_fire(disp_fire), .disp_ready(disp_ready),
        .disp_sel(disp_sel), .disp_dx(disp_dx), .disp_dy(disp_dy),
        .disp_txn(disp_txn), .disp_last(disp_last), .disp_rsvd(disp_rsvd),
        .disp_data(disp_data),
        .sig_pop(sig_pop), .ctl_sig_cnt(sig_cnt), .ctl_sig_ovf(sig_ovf),
        .ctl_sig_code(sig_code), .ctl_sig_id(sig_id), .ctl_sig_arg(sig_arg),
        .retire_valid(), .retire_pc(), .retire_rd(), .retire_val(),
        .cycle_ctr(cyc_ctr), .instret_ctr(ret_ctr),
        .vspad_en(1'b0), .vspad_we(4'd0), .vspad_word(13'd0),
        .vspad_wdata(32'd0)
    );

    rv_imem #(.WORDS(IMEM_WORDS), .MEM_PRIM(MEM_PRIM)) u_imem (
        .clk(clk),
        .wr_en((cd_word_wr && cd_to_imem) || (gw_busy && gw_imem)),
        .wr_addr(gw_busy ? {gw_base[IAW-1:3], gw_cnt} : {cd_off[IAW-4:0], wp_sel}),
        .wr_data(gw_busy ? gw_buf[{gw_cnt, 5'd0} +: 32] : wp_data),
        .rd_addr(imem_addr), .rd_data(imem_data)
    );

    // Port A serves the CU_DATA loader AND mv_exec's fetch: load time versus
    // run time, so they cannot overlap. The assertion below says so if they do.
    wire        cd_spad_wr = cd_word_wr && !cd_to_imem;
    wire        gw_spad_wr = gw_busy && !gw_imem;

    rv_spad #(.WORDS(SPAD_WORDS), .MEM_PRIM(MEM_PRIM)) u_spad (
        .clk(clk),
        .a_en(cd_spad_wr || gw_spad_wr || mv_sp_req),
        .a_we(gw_spad_wr ? 4'hF : (cd_spad_wr ? wp_be : 4'h0)),
        .a_addr(mv_sp_req ? mv_sp_addr
                          : (gw_spad_wr ? {gw_base[SAW-1:3], gw_cnt}
                                        : {cd_off[SAW-4:0], wp_sel})),
        .a_wdata(gw_spad_wr ? gw_buf[{gw_cnt, 5'd0} +: 32] : wp_data),
        .a_rdata(mv_sp_rdata),
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
        .addr(l1_addr), .wdata(l1_wdata), .rdata(l1_rdata_raw),
        .stall(l1_stall_raw),
        .flush(l1_flush), .inval(l1_inval), .flush_busy(l1_flush_busy),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data),
        .wb_valid(wb_valid), .wb_ready(wb_ready), .wb_addr(wb_addr),
        .wb_data(wb_data),
        .wr_idle(wr_out_mag == 16'd0)
    );

    // MEMORY goes to MAG's converged path, never to a flit.
    rv_mag_req #(.ADDR_W(ADDR_W), .DATA_W(DATA_W)) u_mem (
        .clk(clk), .resetn(resetn),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data),
        .wb_valid(wb_valid), .wb_ready(wb_ready), .wb_addr(wb_addr),
        .wb_data(wb_data),
        .seg_we(1'b0), .seg_idx(2'd0), .seg_val(9'd0),
        .cp_awaddr(cp_awaddr), .cp_awlen(cp_awlen), .cp_awvalid(cp_awvalid),
        .cp_awready(cp_awready),
        .cp_wdata(cp_wdata), .cp_wstrb(cp_wstrb), .cp_wlast(cp_wlast),
        .cp_wvalid(cp_wvalid), .cp_wready(cp_wready),
        .cp_bvalid(cp_bvalid), .cp_bready(cp_bready),
        .cp_araddr(cp_araddr), .cp_arlen(cp_arlen), .cp_arvalid(cp_arvalid),
        .cp_arready(cp_arready),
        .cp_rdata(cp_rdata), .cp_rlast(cp_rlast), .cp_rvalid(cp_rvalid),
        .cp_rready(cp_rready),
        .wr_out(wr_out_mag), .idle(mag_idle)
    );

    // FLITS only: dispatch out, completions in. Its memory arms are gated off.
    rv_noc_req #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y), .MEM_PATH(0)
    ) u_flit (
        .clk(clk), .resetn(resetn),
        .fill_valid(1'b0), .fill_ready(), .fill_addr(31'd0),
        .resp_valid(), .resp_data(),
        .wb_valid(1'b0), .wb_ready(), .wb_addr(31'd0), .wb_data(256'd0),
        .push_valid(push_valid), .push_ready(push_ready),
        .push_dx(push_dx), .push_dy(push_dy), .push_win(push_win),
        .push_gran(push_gran), .push_sel(push_sel), .push_be(push_be),
        .push_data(push_data),
        .disp_valid(disp_valid), .disp_ready(disp_ready),
        .disp_dx(disp_dx), .disp_dy(disp_dy), .disp_txn(disp_txn),
        .disp_last(disp_last), .disp_rsvd(disp_rsvd),
        .disp_op(disp_op), .disp_pc(disp_pc), .disp_arg(disp_arg),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .rx_rd_resp(rx_take && rx_is_resp), .rx_txn(rx_id),
        .rx_data(rx_pl[255:0]), .rx_wr_ack(rx_take && rx_is_ack),
        .rx_sig(rx_take && rx_is_sig), .rx_sig_id(rx_id),
        .rx_sig_code(rx_sig_code), .rx_sig_arg(rx_sig_arg),
        .sig_pop(sig_pop), .sig_cnt(sig_cnt), .sig_ovf(sig_ovf),
        .sig_code(sig_code), .sig_id(sig_id), .sig_arg(sig_arg),
        .wr_out(wr_out_noc), .idle(req_idle)
    );

    // ------------------------------------------- the mover, as an executor
    wire        pe_cfg_en;
    wire [7:0]  pe_cfg_addr;
    wire [63:0] pe_cfg_data;

    mv_exec #(.SAW(SAW)) u_mv (
        .clk(clk), .resetn(resetn),
        .go(mv_go), .ptr(l1_wdata[SAW-1:0]), .busy(mv_exec_busy),
        .sp_req(mv_sp_req), .sp_addr(mv_sp_addr), .sp_data(mv_sp_rdata),
        .cfg_en(pe_cfg_en), .cfg_addr(pe_cfg_addr), .cfg_data(pe_cfg_data),
        .mv_busy(mv_busy)
    );

    // The aux window splits at offset 0x80: below is the mover's, at or above
    // the interlink's. Without the interlink the gate is a constant and the
    // mover sees every write, as it always has.
    wire        host_cfg_mine = aux_cfg_en && (!ilink_on || !aux_cfg_addr[7]);
    wire        cfg_en_i      = pe_cfg_en || host_cfg_mine;
    wire [7:0]  cfg_addr_i    = pe_cfg_en ? pe_cfg_addr : aux_cfg_addr;
    wire [63:0] cfg_data_i    = pe_cfg_en ? pe_cfg_data : aux_cfg_data;

    // ---- the transform slot, on the mover's read-return path -------------
    wire                x_req, x_gnt, x_start, x_bv, x_done;
    wire [XID_W-1:0]    x_id;
    wire [XMODE_W-1:0]  x_mode;
    wire [DATA_W-1:0]   x_beat, x_w0, x_w1, x_w2, x_w3;

    mm_mover #(.DATA_W(DATA_W), .ADDR_W(ADDR_W), .ID_W(ID_W),
               .XID_W(XID_W), .XMODE_W(XMODE_W),
               .XF_IN_BITS(XFORM_IN_BITS),
               .XF_OUT_WORDS(XFORM_OUT_WORDS)) u_mover (
        .clk(clk), .resetn(resetn),
        .cfg_en(cfg_en_i), .cfg_addr(cfg_addr_i), .cfg_data(cfg_data_i),
        .stat_busy(mv_busy), .stat_fault(mv_fault), .stat_done(mv_done),
        .m_awid(mv_awid), .m_awaddr(mv_awaddr), .m_awlen(mv_awlen),
        .m_awsize(mv_awsize), .m_awburst(mv_awburst),
        .m_awvalid(mv_awvalid), .m_awready(mv_awready),
        .m_wdata(mv_wdata), .m_wstrb(mv_wstrb), .m_wlast(mv_wlast),
        .m_wvalid(mv_wvalid), .m_wready(mv_wready),
        .m_bid(mv_bid), .m_bresp(mv_bresp),
        .m_bvalid(mv_bvalid), .m_bready(mv_bready),
        .m_arid(mv_arid), .m_araddr(mv_araddr), .m_arlen(mv_arlen),
        .m_arsize(mv_arsize), .m_arburst(mv_arburst),
        .m_arvalid(mv_arvalid), .m_arready(mv_arready),
        .m_rid(mv_rid), .m_rdata(mv_rdata), .m_rresp(mv_rresp),
        .m_rlast(mv_rlast), .m_rvalid(mv_rvalid), .m_rready(mv_rready),
        .x_req(x_req), .x_gnt(x_gnt), .x_start(x_start),
        .x_id(x_id), .x_mode(x_mode),
        .x_beat(x_beat), .x_beat_valid(x_bv),
        .x_done(x_done), .x_w0(x_w0), .x_w1(x_w1), .x_w2(x_w2), .x_w3(x_w3)
    );

    mag_xform #(.DATA_W(DATA_W), .NREQ(1), .SLOTS(XFORM_SLOTS),
                .ID_W(XID_W), .MODE_W(XMODE_W),
                .IN_BITS(XFORM_IN_BITS), .OUT_WORDS(XFORM_OUT_WORDS))
    u_xform (
        .clk(clk), .rst(!resetn),
        .req(x_req), .gnt(x_gnt),
        .start(x_start), .id(x_id), .mode(x_mode),
        .beat(x_beat), .beat_valid(x_bv),
        .done(x_done), .word0(x_w0), .word1(x_w1), .word2(x_w2), .word3(x_w3),
        .cfg_en(xcfg_en), .cfg_id(xcfg_id),
        .cfg_addr(xcfg_addr), .cfg_data(xcfg_data),
        .cfg_rdata(xcfg_rdata), .fault(xf_fault)
    );

    // One 64-bit read tells the host everything, so polling costs no flit.
    assign pe_status = {32'd0,
                        ret_ctr[7:0],
                        3'd0, mv_fault,
                        core_cause, halted_by_host, mv_exec_busy,
                        mv_busy, core_halted, core_run, busy,
                        8'd0};

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        if (mv_sp_req && (cd_spad_wr || gw_spad_wr)) begin
            $display("%0t ERROR rv_mag_pe: mv_exec and the CU_DATA loader want scratchpad port A in the same cycle",
                     $time);
        end
        if (mv_go && mv_exec_busy) begin
            $display("%0t ERROR rv_mag_pe: mv.go while the mover is busy", $time);
        end
    end
`endif
endmodule

`default_nettype wire
