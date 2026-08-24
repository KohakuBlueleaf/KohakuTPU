// Global Orchestrator -- AXI4 slave <-> NoC local port. See docs/noc/spec.md s10.
//
// The control plane and the bring-up instrument. It gives a host (XDMA, or
// jtag_axi during development) three things:
//
//   a raw flit mailbox   inject and receive ANY flit, malformed ones included.
//                        An address-mapped bridge could only emit
//                        MEM_RD_REQ/MEM_WR_REQ, never CU_INST or a bad header.
//   a status mirror      NODE_STATUS[n], updated from CU_SIGNAL, so the host
//                        polls a 2 KB register window rather than DDR4 over PCIe.
//   capability discovery so software can size itself without a hardcoded map.
//
// AXI and NoC share one clock (spec s10.6). SmartConnect does the crossing to
// XDMA/jtag, so there is no CDC in here.
//
// The AXI slave FSM is the one from src/kohakuaxi/axi4_ram.v: VALID is never a
// function of READY, the burst counter rather than WLAST ends a burst, and
// BID/RID echo AWID/ARID.

`default_nettype none

module noc_orchestrator #(
    parameter DATA_WIDTH = 64,     // AXI data width; 288-bit flit = 5 beats
    parameter ADDR_WIDTH = 32,
    parameter ID_WIDTH   = 4,
    parameter FLIT_WIDTH = 288,
    parameter POS_WIDTH  = 4,
    parameter GRID_LO    = 1,
    parameter GRID_HI    = 14,
    parameter ORC_X      = 1,      // our own coordinates, stamped into dispatched
    parameter ORC_Y      = 1,      // flits so targets can reply without config

    parameter TX_DEPTH   = 16,
    parameter RX_DEPTH   = 16,
    parameter STAGE_FLITS = 128    // instruction staging buffer, in flits
) (
    input  wire                    clk,
    input  wire                    resetn,

    // ---- AXI4 slave ----
    input  wire [ID_WIDTH-1:0]     s_axi_awid,
    input  wire [ADDR_WIDTH-1:0]   s_axi_awaddr,
    input  wire [7:0]              s_axi_awlen,
    input  wire [2:0]              s_axi_awsize,
    input  wire [1:0]              s_axi_awburst,
    input  wire                    s_axi_awvalid,
    output wire                    s_axi_awready,
    input  wire [DATA_WIDTH-1:0]   s_axi_wdata,
    input  wire [DATA_WIDTH/8-1:0] s_axi_wstrb,
    input  wire                    s_axi_wlast,
    input  wire                    s_axi_wvalid,
    output wire                    s_axi_wready,
    output wire [ID_WIDTH-1:0]     s_axi_bid,
    output wire [1:0]              s_axi_bresp,
    output wire                    s_axi_bvalid,
    input  wire                    s_axi_bready,
    input  wire [ID_WIDTH-1:0]     s_axi_arid,
    input  wire [ADDR_WIDTH-1:0]   s_axi_araddr,
    input  wire [7:0]              s_axi_arlen,
    input  wire [2:0]              s_axi_arsize,
    input  wire [1:0]              s_axi_arburst,
    input  wire                    s_axi_arvalid,
    output wire                    s_axi_arready,
    output wire [ID_WIDTH-1:0]     s_axi_rid,
    output wire [DATA_WIDTH-1:0]   s_axi_rdata,
    output wire [1:0]              s_axi_rresp,
    output wire                    s_axi_rlast,
    output wire                    s_axi_rvalid,
    input  wire                    s_axi_rready,

    // ---- auxiliary command window, forwarded verbatim ----
    // A reserved slice of the control window whose writes leave this module
    // unexamined; MAG binds it to the memory mover. A block design carries
    // clock, reset and AXI only, so a submodule's command path arrives here.
    output reg                     aux_cfg_en,
    output reg  [7:0]              aux_cfg_addr,
    output reg  [DATA_WIDTH-1:0]   aux_cfg_data,
    input  wire [DATA_WIDTH-1:0]   aux_stat,

    // A READ window to match the write one: 16 words a client can present
    // without this module knowing what they mean. `aux_stat` alone is one
    // address, which was enough for the mover and is not enough for anything
    // that reports per-link state.
    //
    // Reads here returned zero before this window existed, and return zero
    // still when `aux_stat_q` is tied off -- so a bitstream built without a
    // client behaves exactly as every bitstream before it did.
    output wire [3:0]              aux_stat_sel,
    input  wire [DATA_WIDTH-1:0]   aux_stat_q,

    // ---- NoC local port ----
    output reg  [FLIT_WIDTH-1:0]   noc_out_data,
    output reg                     noc_out_valid,
    input  wire                    noc_out_busy,
    input  wire [FLIT_WIDTH-1:0]   noc_in_data,
    input  wire                    noc_in_valid,
    output wire                    noc_in_busy
);

    localparam FLIT_WORDS = (FLIT_WIDTH + DATA_WIDTH - 1) / DATA_WIDTH;  // 5 at 64b
    localparam PAD_WIDTH  = FLIT_WORDS * DATA_WIDTH;                     // 320
    localparam [1:0] RESP_OKAY = 2'b00;

    // register map, byte offsets (spec s10.2)
    localparam [15:0] A_CTRL = 16'h0000, A_STATUS = 16'h0008, A_CAPS = 16'h0010;
    localparam [15:0] A_IRQ_STAT = 16'h0018, A_IRQ_EN = 16'h0020;
    localparam [15:0] A_PROG_DST = 16'h0040, A_PROG_LEN = 16'h0048;
    localparam [15:0] A_PROG_KICK = 16'h0050, A_PROG_STAT = 16'h0058;
    localparam [15:0] A_PROG_CRED = 16'h0060, A_PROG_BASE = 16'h0068;
    localparam [15:0] A_SIG_DONE = 16'h0070, A_AUX_STAT = 16'h0078;
    localparam [15:0] A_AUX_STATW = 16'h0080, A_TX_FLIT0 = 16'h0100;
    localparam [15:0] A_TX_KICK = 16'h0140, A_TX_STATUS = 16'h0148;
    localparam [15:0] A_RX_FLIT0 = 16'h0180, A_RX_POP = 16'h01C0;
    localparam [15:0] A_RX_STATUS = 16'h01C8;

    // ------------------------------------------------------------ registers
    reg [DATA_WIDTH-1:0] ctrl_reg, irq_en_reg, irq_stat_reg;
    reg [PAD_WIDTH-1:0]  tx_stage;          // TX_FLIT[0..4]

    // Instruction dispatch. The host stages CU_INST flits in a local BRAM through
    // this same AXI slave, then names a destination and kicks. No AXI master is
    // needed: the orchestrator never fetches from DRAM, it only forwards what the
    // host already placed here.
    localparam STAGE_WORDS = STAGE_FLITS * FLIT_WORDS;
    localparam SW_BITS     = $clog2(STAGE_WORDS);
    localparam [15:0] A_STAGE = 16'h2000;

    // 256 BYTES, not 256 entries: the offset within the window IS `aux_cfg_addr`,
    // so whatever sits behind it keeps its own register offsets verbatim. An
    // index here would alias a client's 0x38 onto its 0x00.
    localparam [15:0] A_AUX_CFG = 16'h0800;
    localparam [15:0] A_AUX_END = A_AUX_CFG + 16'h0100;

    // Infers LUTRAM (RAM64M8 x100 at STAGE_FLITS=128), not BRAM. NO ram_style
    // attribute: "block" is rejected as infeasible and silently downgraded, and
    // an ignored attribute reads like a guarantee. The likely blocker is the
    // read destination being a variable part-select -- BRAM read data has to
    // land in a plain register. ~1000 LUTs, one per system; revisit if
    // STAGE_FLITS grows by an order of magnitude.
    reg [DATA_WIDTH-1:0] stage_ram [0:STAGE_WORDS-1];

    reg [2*POS_WIDTH-1:0] prog_dst;
    reg [15:0]            prog_len;      // flits to send
    // First staging slot of this program. Without it every kick restarts at slot
    // 0, so a second cluster's flits cannot be staged until the first has
    // consumed its own, serialising clusters with no data dependency.
    reg [15:0]            prog_base;
    reg [15:0]            prog_left;
    reg [SW_BITS-1:0]     prog_rd;
    reg [15:0]            prog_credit;   // seeded by the host, refilled on INST_COMPLETE
    reg                   prog_run;
    reg                   kick_req;    // one-cycle requests from the AXI write FSM
    reg                   cred_wr;
    reg                   sig_done_clr;
    reg [15:0]            cred_val;
    reg [2:0]             prog_word;
    reg [PAD_WIDTH-1:0]   prog_flit;

    wire [DATA_WIDTH-1:0] caps_word =
        { {(DATA_WIDTH-48){1'b0}},
          GRID_HI[7:0], GRID_LO[7:0], POS_WIDTH[7:0], FLIT_WIDTH[15:0] };

    // ------------------------------------------------------------- TX FIFO
    // Two writers: the mailbox (TX_KICK, for bring-up) and the dispatcher. The
    // dispatcher only runs between kicks, so they never contend -- TX_KICK is
    // ignored while prog_run is set.
    reg                   tx_push;
    reg                   disp_push;
    reg  [FLIT_WIDTH-1:0] disp_flit;
    wire                  tx_full, tx_empty;
    wire [FLIT_WIDTH-1:0] tx_head;
    // Popped only when the output register can take it -- empty, or being
    // emptied this cycle. A pop decided from `!noc_out_busy` alone commits the
    // flit to a register that may not be read, and the dispatched CU_INST is
    // then simply missing from the program.
    wire                  tx_free = !noc_out_valid || !noc_out_busy;
    wire                  tx_pop = !tx_empty && tx_free;

    wire                  tx_wr_en   = tx_push | disp_push;
    wire [FLIT_WIDTH-1:0] tx_wr_data = disp_push ? disp_flit : tx_stage[FLIT_WIDTH-1:0];

    sync_fifo #(.DATA_WIDTH(FLIT_WIDTH), .FIFO_DEPTH(TX_DEPTH),
                .MEMORY_TYPE("distributed")) u_tx (
        .clk(clk), .rst(!resetn),
        .wr_en(tx_wr_en), .wr_data(tx_wr_data), .wr_busy(tx_full),
        .rd_en(tx_pop),   .rd_data(tx_head),    .rd_busy(tx_empty)
    );

    // Mirrors OutPortSwitch: hold the flit asserted until a cycle in which the
    // receiver is not busy.
    always @(posedge clk) begin
        // `noc_out_data` is not reset: `noc_out_valid` qualifies it.
        if (!resetn) begin
            noc_out_valid <= 1'b0;
        end else begin
            noc_out_valid <= tx_pop | (noc_out_valid && noc_out_busy);
            if (tx_pop) begin
                noc_out_data <= tx_head;
            end
        end
    end

    // ------------------------------------------------------------- RX FIFO
    wire [3:0] in_type = noc_in_data[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    reg                   rx_pop;
    wire                  rx_full, rx_empty;
    wire [FLIT_WIDTH-1:0] rx_head;

    // CU_SIGNAL is summarised into NODE_STATUS and NOT queued here. Queued,
    // unread signals fill the FIFO, raise noc_in_busy and stop the orchestrator
    // accepting anything -- including the signals that return dispatch credits,
    // so a host that never reads RX wedges the control plane after RX_DEPTH
    // completions. NODE_STATUS is the mechanism for completions (spec s10.4);
    // RX is for traffic with no other home, such as CU_CTRL replies.
    wire in_is_sig = (in_type == 4'h6);

    sync_fifo #(.DATA_WIDTH(FLIT_WIDTH), .FIFO_DEPTH(RX_DEPTH),
                .MEMORY_TYPE("distributed")) u_rx (
        .clk(clk), .rst(!resetn),
        .wr_en(noc_in_valid && !rx_full && !in_is_sig),
        .wr_data(noc_in_data), .wr_busy(rx_full),
        .rd_en(rx_pop), .rd_data(rx_head), .rd_busy(rx_empty)
    );
    // signals are always absorbed, so they can never be blocked by a full RX
    assign noc_in_busy = rx_full && !in_is_sig;

    reg rx_overflow;
    always @(posedge clk) begin
        if (!resetn) begin
            rx_overflow <= 1'b0;
        end
        else if (noc_in_valid && rx_full) begin
            rx_overflow <= 1'b1;
        end
    end

    // --------------------------------------------------------- status mirror
    // Indexed by {src_y, src_x} so every coordinate has a slot, border PEs
    // included. This is the ONLY record of a CU_SIGNAL -- the flit itself is
    // dropped rather than queued, for the reason given at the RX FIFO above.
    localparam N_STATUS = 1 << (2*POS_WIDTH);
    // Also LUTRAM (RAM256X1D x64): the signal_count read-modify-write below
    // touches one address in one cycle, which BRAM cannot do, so ram_style
    // "block" is rejected here too and is deliberately not written.
    reg [63:0] node_status [0:N_STATUS-1];

    wire [POS_WIDTH-1:0] in_src_x = noc_in_data[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] in_src_y = noc_in_data[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
    wire [2*POS_WIDTH-1:0] in_idx = {in_src_y, in_src_x};
    wire                 in_is_signal = noc_in_valid && in_is_sig;

    wire [7:0]  sig_code = noc_in_data[255 -: 8];
    wire [31:0] sig_arg  = noc_in_data[247 -: 32];
    reg  [15:0] status_count_next;

    integer si;
    initial begin
        for (si = 0; si < N_STATUS; si = si + 1) begin
            node_status[si] = 64'd0;
        end
    end

    always @(posedge clk) begin
        if (in_is_signal) begin
            // signal_count, not a sticky flag: a host polling slower than events
            // arrive can tell how many it missed.
            status_count_next = node_status[in_idx][23:8] + 16'd1;
            node_status[in_idx] <= {sig_code, sig_arg, status_count_next, 7'd0, 1'b1};
        end
    end

    // ------------------------------------------------------------ dispatcher
    // Reads FLIT_WORDS words per flit out of the staging RAM, rewrites the
    // destination to prog_dst, and pushes. Stalls on credit rather than on the
    // network: backpressuring CU_INST into the mesh is the protocol deadlock
    // spec s7 exists to prevent.
    // SIG_INST_COMPLETE
    wire in_inst_done = (
        noc_in_valid
        && in_is_sig
        && (noc_in_data[255 -: 8] == 8'h00)
    );

    // Completions from EVERY node, in one register: NODE_STATUS is per node, so
    // "is everyone finished" would otherwise cost N polls and grow the program
    // with the machine. Cleared by writing SIG_DONE.
    //
    // Counts EVERY signal, matching NODE_STATUS, not just SIG_INST_COMPLETE:
    // the last instruction of a batch reports SIG_BATCH_COMPLETE instead
    // (noc_cu_base.v), so counting only INST_COMPLETE sees N-1 of every N and a
    // host waiting for N waits forever.
    reg [15:0] sig_done_count;

    always @(posedge clk) begin
        if (!resetn) begin
            sig_done_count <= 16'd0;
        end
        else if (sig_done_clr) begin
            sig_done_count <= 16'd0;
        end
        else if (in_is_signal) begin
            sig_done_count <= sig_done_count + 16'd1;
        end
    end

    wire disp_can = prog_run && !tx_full && (prog_credit != 16'd0);

    // ALL dispatcher state is driven from this one block, credit counter and
    // kick included; the AXI write FSM only requests, via kick_req / cred_wr.
    // Split across both blocks, prog_run/prog_left/prog_rd/prog_credit become
    // multi-driven: simulation resolves that by scheduling order and looks
    // correct, while synthesis reports `multi-driven net on pin Q ... 2nd driver
    // GND` and builds something else -- in one case optimising the staging RAM
    // away entirely.
    always @(posedge clk) begin
        disp_push <= 1'b0;
        if (!resetn) begin
            prog_run    <= 1'b0;
            prog_word   <= 3'd0;
            prog_left   <= 16'd0;
            // prog_rd is loaded at the kick; prog_flit is 320 bits the FIFO
            // takes only once complete, word by word.
            prog_credit <= 16'd0;
        end else begin
            // credit: one per CU_INST flit sent, refilled by SIG_INST_COMPLETE.
            // A host write wins, so re-seeding between programs is predictable.
            if (cred_wr) begin
                prog_credit <= cred_val;
            end
            else if (disp_push && !in_inst_done && prog_credit != 16'd0) begin
                prog_credit <= prog_credit - 16'd1;
            end
            else if (in_inst_done && !disp_push) begin
                prog_credit <= prog_credit + 16'd1;
            end

            // kick_req only asserts while !prog_run and disp_can requires
            // prog_run, so these two arms are mutually exclusive
            if (kick_req && !prog_run && prog_len != 16'd0) begin
                prog_run  <= 1'b1;
                prog_left <= prog_len;
                prog_rd   <= prog_base[SW_BITS-1:0] * FLIT_WORDS;
                prog_word <= 3'd0;
            end else if (disp_can) begin
                prog_flit[prog_word*DATA_WIDTH +: DATA_WIDTH] <= stage_ram[prog_rd];
                prog_rd <= prog_rd + 1'b1;
                if (prog_word == FLIT_WORDS-1) begin
                    prog_word <= 3'd0;
                    disp_push <= 1'b1;
                    if (prog_left == 16'd1) begin
                        prog_run <= 1'b0;
                    end
                    else begin
                        prog_left <= prog_left - 16'd1;
                    end
                end else begin
                    prog_word <= prog_word + 3'd1;
                end
            end
        end
    end

    // disp_push is non-blocking, so it is high the cycle AFTER the final word
    // lands in prog_flit: the FIFO captures a complete flit, and the next flit's
    // first word does not land until the end of that same cycle.
    //
    // The dispatcher owns the routing header -- destination from PROG_DST, source
    // stamped with our own coordinates so the target can reply. The mailbox path
    // does neither, because injecting a hand-built (or wrong) header is its point.
    always @(*) begin
        disp_flit = prog_flit[FLIT_WIDTH-1:0];
        disp_flit[FLIT_WIDTH-1              -: POS_WIDTH] = prog_dst[POS_WIDTH-1:0];
        disp_flit[FLIT_WIDTH-POS_WIDTH-1    -: POS_WIDTH] = prog_dst[2*POS_WIDTH-1:POS_WIDTH];
        disp_flit[FLIT_WIDTH-2*POS_WIDTH-1  -: POS_WIDTH] = ORC_X[POS_WIDTH-1:0];
        disp_flit[FLIT_WIDTH-3*POS_WIDTH-1  -: POS_WIDTH] = ORC_Y[POS_WIDTH-1:0];
    end

    // ---------------------------------------------------------- AXI write FSM
    localparam [1:0] W_IDLE = 2'd0, W_DATA = 2'd1, W_RESP = 2'd2;
    reg [1:0]            wstate;
    reg [ADDR_WIDTH-1:0] waddr;
    reg [7:0]            wlen;
    reg [ID_WIDTH-1:0]   wid;

    assign s_axi_awready = (wstate == W_IDLE);
    assign s_axi_wready  = (wstate == W_DATA);
    assign s_axi_bvalid  = (wstate == W_RESP);
    assign s_axi_bid     = wid;
    assign s_axi_bresp   = RESP_OKAY;

    wire [15:0] wsel     = waddr[15:0];
    wire [2:0]  tx_word  = wsel[5:3];              // TX_FLIT index within 0x100..0x120
    wire        is_tx_fl = (wsel >= A_TX_FLIT0) && (wsel < A_TX_FLIT0 + FLIT_WORDS*8);
    // The staging window is derived from STAGE_WORDS, not fixed at one 4 KB
    // page: `waddr[15:12] == 4'h2` covers 0x2000..0x2FFF = 512 words = 102.4
    // flits, so at STAGE_FLITS=128 a full program's last 26 flits decode as
    // register writes. Silent -- the program just stops early with the staging
    // RAM holding whatever was there before.
    localparam [15:0] STAGE_END = A_STAGE + STAGE_WORDS*8;
    wire        is_stage = (waddr[15:0] >= A_STAGE) && (waddr[15:0] < STAGE_END);
    // Index relative to A_STAGE. Slicing waddr directly folds base-address bits
    // into the index, harmless only while SW_BITS is small enough that 0x2000
    // aliases to 0.
    wire [SW_BITS-1:0] stage_widx = (waddr[15:0] - A_STAGE) >> 3;
    wire        is_aux = (waddr[15:0] >= A_AUX_CFG) && (waddr[15:0] < A_AUX_END);

    // A station-bus manager flit-aligns, so one host write64 arrives as four
    // beats, one strobed: ignoring WSTRB made that eight register writes.
    wire w_live = |s_axi_wstrb;
    wire w_lo16 = |s_axi_wstrb[1:0];      // a 16-bit register lives in bytes 0-1

    // dat/stb are ARGUMENTS: in a continuous assign xsim sensitises only the
    // argument list, so reading them from module scope pulsed stale data.
    function [DATA_WIDTH-1:0] wmerge;
        input [DATA_WIDTH-1:0]   cur;
        input [DATA_WIDTH-1:0]   dat;
        input [DATA_WIDTH/8-1:0] stb;
        integer b;
        begin
            wmerge = cur;
            for (b = 0; b < DATA_WIDTH/8; b = b + 1) begin
                if (stb[b]) begin
                    wmerge[b*8 +: 8] = dat[b*8 +: 8];
                end
            end
        end
    endfunction

    // The client has no byte enables, so a half-strobed pair (+0 then +4) can
    // only be delivered whole -- accumulate, then pulse the merged value.
    reg [7:0]            aux_sh_addr;
    reg [DATA_WIDTH-1:0] aux_sh_data;
    wire [7:0]           aux_addr_a = {waddr[7:3], 3'b000};
    wire [DATA_WIDTH-1:0] aux_next =
        wmerge((aux_sh_addr == aux_addr_a) ? aux_sh_data : {DATA_WIDTH{1'b0}},
               s_axi_wdata, s_axi_wstrb);

    integer wb;
    always @(posedge clk) begin
        tx_push  <= 1'b0;
        rx_pop   <= 1'b0;
        kick_req <= 1'b0;
        cred_wr  <= 1'b0;
        sig_done_clr <= 1'b0;
        aux_cfg_en   <= 1'b0;

        if (!resetn) begin
            wstate       <= W_IDLE;
            waddr        <= {ADDR_WIDTH{1'b0}};
            wlen         <= 8'd0;
            wid          <= {ID_WIDTH{1'b0}};
            ctrl_reg     <= {DATA_WIDTH{1'b0}};
            irq_en_reg   <= {DATA_WIDTH{1'b0}};
            irq_stat_reg <= {DATA_WIDTH{1'b0}};
            prog_dst     <= {(2*POS_WIDTH){1'b0}};
            prog_len     <= 16'd0;
            prog_base    <= 16'd0;
            cred_val     <= 16'd0;
            // Never 8-aligned, so it cannot match a real aux offset and the
            // first write to any register accumulates from zero.
            aux_sh_addr  <= 8'hFF;
            // tx_stage is 320 bits pushed only once the host has written all
            // five words; aux_cfg_addr/data are qualified by aux_cfg_en above.
        end else begin
            case (wstate)
                W_IDLE: if (s_axi_awvalid) begin
                    wid    <= s_axi_awid;
                    waddr  <= s_axi_awaddr;
                    wlen   <= s_axi_awlen;
                    wstate <= W_DATA;
                end
                W_DATA: if (s_axi_wvalid) begin
                    // A beat with no strobes carries nothing: it must not write
                    // a register and must not act as an arrival.
                    if (!w_live) begin
                        ;
                    end else if (is_stage) begin
                        stage_ram[stage_widx]
                            <= wmerge(stage_ram[stage_widx], s_axi_wdata,
                                      s_axi_wstrb);
                    end else if (is_tx_fl) begin
                        for (wb = 0; wb < DATA_WIDTH/8; wb = wb + 1) begin
                            if (s_axi_wstrb[wb]) begin
                                tx_stage[tx_word*DATA_WIDTH + wb*8 +: 8]
                                    <= s_axi_wdata[wb*8 +: 8];
                            end
                        end
                    // ONE PULSE PER BEAT, because a client may use consecutive
                    // writes as a load-then-commit pair; a burst walks `waddr`
                    // and so writes its registers in order.
                    end else if (is_aux) begin
                        aux_cfg_en   <= 1'b1;
                        aux_cfg_addr <= aux_addr_a;
                        aux_cfg_data <= aux_next;
                        aux_sh_addr  <= aux_addr_a;
                        aux_sh_data  <= aux_next;
                    end else begin
                        case (wsel)
                            A_CTRL:      ctrl_reg   <= wmerge(ctrl_reg,
                                                        s_axi_wdata, s_axi_wstrb);
                            A_IRQ_EN:    irq_en_reg <= wmerge(irq_en_reg,
                                                        s_axi_wdata, s_axi_wstrb);
                            // W1C, and only where this beat carries a byte.
                            A_IRQ_STAT:  irq_stat_reg <= irq_stat_reg
                                                       & ~wmerge({DATA_WIDTH{1'b0}},
                                                           s_axi_wdata, s_axi_wstrb);
                            A_PROG_DST:  begin
                                if (w_lo16) begin
                                    prog_dst <= s_axi_wdata[2*POS_WIDTH-1:0];
                                end
                            end
                            A_PROG_LEN:  begin
                                if (w_lo16) begin
                                    prog_len <= s_axi_wdata[15:0];
                                end
                            end
                            A_PROG_BASE: begin
                                if (w_lo16) begin
                                    prog_base <= s_axi_wdata[15:0];
                                end
                            end
                            A_SIG_DONE:  sig_done_clr <= 1'b1;
                            A_PROG_CRED: begin
                                if (w_lo16) begin
                                    cred_wr  <= 1'b1;
                                    cred_val <= s_axi_wdata[15:0];
                                end
                            end
                            A_PROG_KICK: kick_req <= 1'b1;
                            // mailbox is ignored mid-dispatch; they share the TX FIFO
                            A_TX_KICK:   tx_push      <= !tx_full && !prog_run;
                            A_RX_POP:    rx_pop       <= !rx_empty;
                            default: ;
                        endcase
                    end
                    waddr <= waddr + (DATA_WIDTH/8);
                    if (wlen == 8'd0) begin
                        wstate <= W_RESP;
                    end
                    else begin
                        wlen   <= wlen - 8'd1;
                    end
                end
                W_RESP: begin
                    if (s_axi_bready) begin
                        wstate <= W_IDLE;
                    end
                end
                default: wstate <= W_IDLE;
            endcase
        end
    end

    // ----------------------------------------------------------- AXI read FSM
    localparam R_IDLE = 1'b0, R_DATA = 1'b1;
    reg                  rstate;
    reg [ADDR_WIDTH-1:0] raddr;
    reg [8:0]            rbeats;
    reg [ID_WIDTH-1:0]   rid;
    reg [DATA_WIDTH-1:0] rdata_r;
    reg                  rvalid_r, rlast_r;

    assign s_axi_arready = (rstate == R_IDLE);
    assign s_axi_rvalid  = rvalid_r;
    assign s_axi_rdata   = rdata_r;
    assign s_axi_rlast   = rlast_r;
    assign s_axi_rid     = rid;
    assign s_axi_rresp   = RESP_OKAY;

    wire r_can_advance = !rvalid_r || s_axi_rready;
    wire [15:0] rsel    = raddr[15:0];
    wire [2:0]  rx_word = rsel[5:3];
    wire        is_rx_fl = (rsel >= A_RX_FLIT0) && (rsel < A_RX_FLIT0 + FLIT_WORDS*8);
    wire        is_node  = (raddr[15:12] == 4'h1);
    wire        is_auxst = (rsel >= A_AUX_STATW) && (rsel < A_AUX_STATW + 16*8);
    assign      aux_stat_sel = rsel[6:3];

    wire [PAD_WIDTH-1:0] rx_padded = { {(PAD_WIDTH-FLIT_WIDTH){1'b0}}, rx_head };

    reg [DATA_WIDTH-1:0] reg_rd;
    always @(*) begin
        reg_rd = {DATA_WIDTH{1'b0}};
        if (is_rx_fl) begin
            reg_rd = rx_padded[rx_word*DATA_WIDTH +: DATA_WIDTH];
        end
        else if (is_auxst) begin
            reg_rd = aux_stat_q;
        end
        else begin
            case (rsel)
                A_CTRL:      reg_rd = ctrl_reg;
                A_CAPS:      reg_rd = caps_word;
                A_IRQ_EN:    reg_rd = irq_en_reg;
                A_IRQ_STAT:  reg_rd = irq_stat_reg;
                A_PROG_DST:  reg_rd = { {(DATA_WIDTH-2*POS_WIDTH){1'b0}},
                                        prog_dst };
                A_PROG_LEN:  reg_rd = { {(DATA_WIDTH-16){1'b0}}, prog_len };
                A_PROG_BASE: reg_rd = { {(DATA_WIDTH-16){1'b0}}, prog_base };
                A_SIG_DONE:  reg_rd = { {(DATA_WIDTH-16){1'b0}}, sig_done_count };
                A_AUX_STAT:  reg_rd = aux_stat;
                A_PROG_CRED: reg_rd = { {(DATA_WIDTH-16){1'b0}}, prog_credit };
                A_STATUS:    reg_rd = { {(DATA_WIDTH-3){1'b0}},
                                        1'b1 /*mesh_ready*/,
                                        1'b0 /*error*/,
                                        (!tx_empty | prog_run) };
                A_PROG_STAT: reg_rd = { {(DATA_WIDTH-33){1'b0}}, prog_credit,
                                        prog_left, prog_run };
                A_TX_STATUS: reg_rd = { {(DATA_WIDTH-17){1'b0}}, tx_full, 16'd0 };
                A_RX_STATUS: reg_rd = { {(DATA_WIDTH-18){1'b0}}, rx_overflow,
                                        rx_empty, 16'd0 };
                default:     reg_rd = {DATA_WIDTH{1'b0}};
            endcase
        end
    end

    always @(posedge clk) begin
        if (!resetn) begin
            rstate   <= R_IDLE;
            rvalid_r <= 1'b0;
            rlast_r  <= 1'b0;
            raddr    <= {ADDR_WIDTH{1'b0}};
            rbeats   <= 9'd0;
            rid      <= {ID_WIDTH{1'b0}};
        end else begin
            case (rstate)
                R_IDLE: begin
                    if (rvalid_r && s_axi_rready) begin
                        rvalid_r <= 1'b0;
                    end
                    if (s_axi_arvalid) begin
                        rid    <= s_axi_arid;
                        raddr  <= s_axi_araddr;
                        rbeats <= {1'b0, s_axi_arlen} + 9'd1;
                        rstate <= R_DATA;
                    end
                end
                R_DATA: if (r_can_advance) begin
                    if (rbeats != 9'd0) begin
                        rdata_r  <= is_node ? node_status[raddr[3 +: 2*POS_WIDTH]]
                                            : reg_rd;
                        rvalid_r <= 1'b1;
                        rlast_r  <= (rbeats == 9'd1);
                        rbeats   <= rbeats - 9'd1;
                        raddr    <= raddr + (DATA_WIDTH/8);
                    end else begin
                        rvalid_r <= 1'b0;
                        rlast_r  <= 1'b0;
                        rstate   <= R_IDLE;
                    end
                end
            endcase
        end
    end

endmodule

`default_nettype wire
