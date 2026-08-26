// rv64_syscore -- the RV64 control complex that replaces the RV32 one inside a
// system node. NO NoC compute-unit shell: see .plan/syscore/decisions.md D1.
//
//   core (physical)  ->  Sv39 MMU  ->  decode  ->  local spad / control
//                                              ->  L1        -> node port
//                                              ->  uncached  -> node port
//
// SV39 SITS HERE, NOT IN THE CORE (D3). `rv64_core` is a physical-address
// machine, so the mesh compute-unit configuration carries no MMU and pays
// nothing for one.
//
// THE L1 CACHES DRAM AND NOTHING ELSE. Staging holds the page tables, the
// mailbox and the allocator bitmap (design s8) and is uncached, which is what
// stops a page-table walk from waiting on the L1 miss that triggered it -- the
// deadlock design s5 raises against a blocking L1.
//
// The host loads this unit by writing its memories over the slave window and
// then ringing a doorbell, the same way it reaches everything else on the card.

`default_nettype none

module rv64_syscore #(
    parameter integer ADDR_W     = 40,
    parameter integer DATA_W     = 256,
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer IMEM_WORDS = 8192,      // 32-bit words
    parameter integer SPAD_WORDS = 4096,      // 64-bit words
    parameter integer L1_LINES   = 64,
    parameter integer TLB_ENTRIES = 32,
    parameter         MEM_PRIM   = "block",
    parameter         SPAD_STYLE = "ultra",
    parameter         RF_PRIM    = "distributed",

    parameter [63:0]  SPAD_BASE  = 64'h0000_0000_0001_0000,
    parameter [63:0]  CTRL_BASE  = 64'h0000_0000_0002_0000,
    parameter [63:0]  NODE_BASE  = 64'h0000_0000_1000_0000,
    // Within the node range, this and above is cached. Below it -- staging and
    // the node's own registers -- is not.
    parameter [63:0]  CACHE_LO   = 64'h0000_0000_8000_0000
)(
    input  wire                   clk,
    input  wire                   resetn,

    // ---- the host's window: load imem/spad, ring the boot doorbell ---------
    input  wire [31:0]            hs_addr,
    input  wire                   hs_wr,
    input  wire [63:0]            hs_wdata,
    input  wire [7:0]             hs_wstrb,
    input  wire                   hs_rd,
    output reg  [63:0]            hs_rdata,
    output wire                   hs_ready,

    // ---- flits: a client of the node's hub, with no port of its own ---------
    // The shell is gone by decision, so dispatch and completions ride a
    // mailbox in the control region instead of a kick-and-report lifecycle.
    input  wire [POS_WIDTH-1:0]   my_x,
    input  wire [POS_WIDTH-1:0]   my_y,
    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    // ---- the node port: one AXI master onto MAG ----------------------------
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

    // ---- the mover's config port. `mv.go` is a STORE, not an opcode --------
    output reg                    mv_cfg_en,
    output reg  [7:0]             mv_cfg_addr,
    output reg  [63:0]            mv_cfg_data,
    input  wire                   mv_busy,
    input  wire [3:0]             mv_fault,
    input  wire [31:0]            mv_done,

    // ---- the interlink doorbell, both directions ---------------------------
    output reg                    db_en,
    output reg  [7:0]             db_addr,
    output reg  [63:0]            db_data,
    input  wire [63:0]            db_status,

    input  wire                   irq_summary,

    output wire                   running,
    output wire                   dbg_console_we,
    output wire [7:0]             dbg_console,
    output wire [63:0]            dbg_cycles,
    output wire [63:0]            dbg_retired
);
    localparam integer IAW = $clog2(IMEM_WORDS);
    localparam integer SAW = $clog2(SPAD_WORDS);

    assign hs_ready = 1'b1;

    // ------------------------------------------------------------ host window
    localparam [3:0] H_IMEM = 4'h0, H_SPAD = 4'h1, H_CTRL = 4'h2;
    wire [3:0] h_sel     = hs_addr[31:28];
    wire       h_imem_we = hs_wr && (h_sel == H_IMEM);
    wire       h_spad_we = hs_wr && (h_sel == H_SPAD);
    wire       h_ctrl_we = hs_wr && (h_sel == H_CTRL);

    localparam [7:0] HR_BOOT = 8'h00, HR_PC = 8'h08, HR_DBELL = 8'h10;
    localparam [7:0] HR_STATUS = 8'h18, HR_EXIT = 8'h20, HR_HALTPC = 8'h28;
    localparam [7:0] HR_CYCLES = 8'h30, HR_RETIRED = 8'h38;

    reg         nm_en;
    reg  [2:0]  nm_addr;
    reg  [63:0] nm_data;
    wire [63:0] nm_rdata;
    wire        noc_cq_nonempty;

    reg         boot_req, boot_ack, run_en;
    reg  [63:0] boot_pc;
    reg         dbell;
    reg  [63:0] exit_word;
    reg         exited;

    wire        core_halted;
    wire [1:0]  core_cause;
    wire [63:0] core_halt_pc;

    // LATCHED: `run_en` drops the core's reset the moment it halts, and that
    // clears `halted` -- the status register would report nothing forever.
    reg         halt_l;
    reg  [1:0]  cause_l;
    reg  [63:0] haltpc_l;

    always @(posedge clk) begin
        if (!resetn) begin
            boot_req <= 1'b0;
            run_en   <= 1'b0;
            dbell    <= 1'b0;
            boot_pc  <= 64'd0;
            halt_l   <= 1'b0;
        end
        else begin
            boot_req <= 1'b0;
            if (h_ctrl_we) begin
                case (hs_addr[7:0])
                    HR_BOOT: begin
                        boot_req <= hs_wdata[0];
                        run_en   <= hs_wdata[0];
                    end
                    HR_PC: begin
                        boot_pc <= hs_wdata;
                    end
                    HR_DBELL: begin
                        dbell <= hs_wdata[0];
                    end
                    default: begin
                    end
                endcase
            end
            if (core_halted) begin
                run_en <= 1'b0;
            end

            if (boot_req) begin
                halt_l <= 1'b0;
            end else if (core_halted && !halt_l) begin
                halt_l   <= 1'b1;
                cause_l  <= core_cause;
                haltpc_l <= core_halt_pc;
            end
        end
    end

    always @(posedge clk) begin
        boot_ack <= boot_req;
    end

    always @(posedge clk) begin
        case (hs_addr[7:0])
            HR_STATUS:  hs_rdata <= {60'd0, exited, halt_l, cause_l};
            HR_EXIT:    hs_rdata <= exit_word;
            HR_HALTPC:  hs_rdata <= haltpc_l;
            HR_CYCLES:  hs_rdata <= dbg_cycles;
            HR_RETIRED: hs_rdata <= dbg_retired;
            default:    hs_rdata <= 64'd0;
        endcase
    end

    // ---------------------------------------------------------- instructions
    wire [63:0] imem_addr_c;
    wire [31:0] imem_data_c;
    wire        imem_stall_c;
    wire [63:0] fetch_pa;

    // THE ARRAY IS ADDRESSED BY THE TRANSLATED PC. With translation off this is
    // the PC unchanged, so a machine-mode runtime fetches exactly as before.
    kohaku_sdpram #(
        .WIDTH(32), .DEPTH(IMEM_WORDS), .MEM_PRIM(MEM_PRIM), .READ_LAT(1)
    ) u_imem (
        .clk(clk),
        .wr_en(h_imem_we), .wr_addr(hs_addr[IAW+1:2]), .wr_data(hs_wdata[31:0]),
        .rd_en(1'b1), .rd_addr(fetch_pa[IAW+1:2]), .rd_data(imem_data_c)
    );

    // ------------------------------------------------------------ the core
    wire [63:0] dmem_addr_c, dmem_wdata_c, dmem_rdata_c;
    wire [7:0]  dmem_wstrb_c;
    wire        dmem_re_c;
    wire        dmem_stall_c;
    wire        dmem_req_c;
    wire        dmem_st_c;
    wire        core_retire;
    wire        core_settle;
    reg         core_rstn;

    wire core_mem_req = dmem_re_c || (dmem_wstrb_c != 8'd0);
    wire core_is_st   = (dmem_wstrb_c != 8'd0);

    // ---------------------------------------------------------------- Sv39
    // `satp` IS THE CSR, not the control-region word. Two writable copies of a
    // translation root is one too many: the control region keeps a read-only
    // mirror for the host, and supervisor software owns the value.
    wire [63:0] core_satp;
    wire [1:0]  core_priv;
    wire        core_sum, core_mxr, core_sfence;
    wire [ADDR_W-1:0] pa;
    wire        mmu_busy, mmu_fault, mmu_fault_fetch;
    wire [3:0]  mmu_cause;
    // The MMU is shared; a fault reaches only the requester it belongs to.
    wire        dmem_fault_w = mmu_fault && !mmu_fault_fetch;
    wire        if_fault     = mmu_fault &&  mmu_fault_fetch;
    wire        w_req, w_ack;
    wire [ADDR_W-1:0] w_addr;
    wire [63:0] w_data;

    // ---- fetch translation, one page at a time -----------------------------
    // A FULL ITLB WOULD BE PAID EVERY CYCLE FOR A LOOKUP THAT ALMOST NEVER
    // CHANGES: consecutive fetches share a page, so one registered translation
    // covers about a thousand instructions and refills on the crossing. The
    // data port wins the MMU; a fetch stall issues no data access, so the two
    // cannot wait on each other.
    wire translating = (core_satp[63:60] == 4'd8) && (core_priv != 2'd3);

    reg         if_ok;
    reg  [26:0] if_vpn;
    reg  [ADDR_W-13:0] if_ppn;
    // A PAGE THAT FAULTED IS STILL AN ENTRY -- a poisoned one. Retrying the
    // walk instead re-faults forever and the core never sees it. The fetch
    // proceeds, the word is marked, and the core traps on it in E.
    reg         if_bad;

    reg         if_req;
    reg  [1:0]  if_wait;

    wire [26:0] if_want = imem_addr_c[38:12];
    wire        if_hit  = if_ok && (if_vpn == if_want);
    wire        if_need = translating && !if_hit;
    wire        if_go   = if_req;

    // ONE CYCLE PAST THE CAPTURE. The array has READ_LAT 1, so on the cycle the
    // translation lands it is still answering the untranslated address; the
    // first word that belongs to the new page arrives the cycle after.
    reg if_settle;

    // THE FENCE LANDS A CYCLE LATE, AND FETCH WAITS FOR IT. `core_sfence` is
    // qualified by the instruction boundary, which carries the L1 tag compare;
    // as the first term of this block it was in every `if_*` enable, 13 levels.
    // Registered, the entry is retired one cycle after the fence retires, and
    // `imem_stall` covers that cycle so no fetch reads the stale page.
    reg sfence_q;
    always @(posedge clk) begin
        sfence_q <= core_sfence;
    end

    // `core_settle` is the cycle after a trap or return: the PC has moved but
    // `core_priv` has not, so `translating` is stale and no fetch may use it.
    assign imem_stall_c = if_need || if_settle || sfence_q || core_settle;

    always @(posedge clk) begin
        if_settle <= 1'b0;
        if (!core_rstn) begin
            if_ok  <= 1'b0;
            if_req <= 1'b0;
        end
        else if (sfence_q || !translating) begin
            // One entry, no tag beyond its own VPN: a new root or a fence
            // retires it outright.
            if_ok  <= 1'b0;
            if_req <= 1'b0;
        end
        else if (!if_req) begin
            // ARBITRATE ON `dmem_req`, NOT ON THE STROBES. `core_mem_req`
            // carries `dmem_wstrb`, which carries `misalign`, which is the
            // address adder -- and that put the whole operand cone into these
            // registers. `dmem_req` is decode-only and exists for this.
            // A data access is already in E and cannot be deferred; a fetch can.
            if (if_need && !dmem_req_c && !core_settle) begin
                if_req  <= 1'b1;
                if_wait <= 2'd2;
            end
        end
        else if (dmem_req_c) begin
            // Data took the port. Abandon and retry -- fetch is stalled anyway.
            if_req <= 1'b0;
        end
        // `!mmu_busy` IS TRUE IN THE CYCLE THE REQUEST RISES, before the MMU has
        // registered it, and the address latched then is the previous access's.
        // Two cycles is what `q_active` and the registered resolution take.
        else if (if_wait != 2'd0) begin
            if_wait <= if_wait - 2'd1;
        end
        else if (if_fault) begin
            if_vpn    <= if_want;
            if_ok     <= 1'b1;
            if_bad    <= 1'b1;
            if_req    <= 1'b0;
            if_settle <= 1'b1;
        end
        else if (!mmu_busy) begin
            if_vpn    <= if_want;
            if_ppn    <= pa[ADDR_W-1:12];
            if_ok     <= 1'b1;
            if_bad    <= 1'b0;
            if_req    <= 1'b0;
            if_settle <= 1'b1;
        end
    end

    // Travels with the word: the array answers a cycle after the address.
    reg  imem_fault_q;
    always @(posedge clk) begin
        imem_fault_q <= translating && if_hit && if_bad;
    end
    wire imem_fault_c = imem_fault_q;

    assign fetch_pa = translating
                    ? {{(64-ADDR_W){1'b0}}, if_ppn, imem_addr_c[11:0]}
                    : imem_addr_c;

    rv64_mmu #(
        .ENTRIES(TLB_ENTRIES), .ADDR_W(ADDR_W), .MEM_PRIM(MEM_PRIM)
    ) u_mmu (
        .clk(clk), .resetn(core_rstn),
        .satp(core_satp), .priv(core_priv),
        .sum(core_sum), .mxr(core_mxr), .sfence(core_sfence),
        // EVERY CONTROL INPUT HERE IS DECODE-DERIVED. `core_mem_req` is
        // `dmem_wstrb != 0`, which carries `misalign` and therefore the address
        // adder -- and `busy` feeds both the core's stall and the fetch
        // capture, so the adder landed in front of both. Measured 27 levels
        // into `if_ppn`'s clock enable. `dmem_req`/`dmem_st` say the same thing
        // from decode. Only `va` still carries the address, which is the one
        // consumer that must.
        .req(dmem_req_c || if_go),
        .va(dmem_req_c ? dmem_addr_c : imem_addr_c),
        .is_store(dmem_req_c && dmem_st_c), .is_fetch(!dmem_req_c),
        .busy(mmu_busy), .pa(pa), .fault(mmu_fault),
        .fault_fetch(mmu_fault_fetch), .cause(mmu_cause),
        .w_req(w_req), .w_addr(w_addr), .w_ack(w_ack), .w_data(w_data)
    );

    // ------------------------------------------------------- address decode
    // BIT TESTS, NOT MAGNITUDE COMPARES. This decode is in the stall path --
    // forward mux, address adder, decode, stall, and stall gates the whole
    // pipeline including the predictor's RAS. As `>=`/`<` on 40 bits it cost
    // 200 failing paths at -0.552. Every range is power-of-two aligned and
    // sized, so each test is one equality or one bit.
    localparam integer SPAD_LSB = $clog2(SPAD_WORDS * 8);
    wire in_spad  = (pa[ADDR_W-1:SPAD_LSB] == SPAD_BASE[ADDR_W-1:SPAD_LSB]);
    wire in_ctrl  = (pa[ADDR_W-1:8]        == CTRL_BASE[ADDR_W-1:8]);
    wire in_node  = |pa[ADDR_W-1:28];                    // NODE_BASE = 2^28
    wire in_cache = in_node && pa[31];                   // CACHE_LO = 2^31
    wire in_unc   = in_node && !in_cache;

    wire acc_ok = core_mem_req && !mmu_busy && !dmem_fault_w;

    // ------------------------------------------------------------ local spad
    // THE READ ADDRESS IS EARLY, THE WRITE IS REGISTERED. The read has to be
    // issued in the first cycle to be answered in the second; the write does
    // not, and driven combinationally it put the address adder on the array's
    // byte-write-enable (`wb_val_reg -> spad/BWE_B`, 13 levels).
    wire [SAW-1:0] spad_ca = pa[SAW+2:3];
    wire           spad_cwe = mem_started && sel_spad && m_st_q;
    wire [SAW-1:0] spad_wa = h_spad_we ? hs_addr[SAW+2:3] : m_pa_q[SAW+2:3];
    wire [63:0]    spad_wd = h_spad_we ? hs_wdata : m_wd_q;
    wire [7:0]     spad_we = h_spad_we ? hs_wstrb : (spad_cwe ? m_be_q : 8'd0);

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

    // ---------------------------------------------------------------- the L1
    // ONE EXTRA CYCLE PER CACHED ACCESS. The core issues in E and consumes in M,
    // so a tag lookup started in E answers in M -- too late to hold E on a miss.
    // Holding E for the lookup instead makes hit and miss the same shape, at one
    // cycle. Removing it means computing the address in D, which is a pipeline
    // change, not a wrapper change.
    // ONE HANDSHAKE FOR EVERY ACCESS, and the first cycle is decided from
    // `dmem_req` -- decode only. The range decode, which needs the address
    // adder, is REGISTERED on that first cycle and only steers cycles 2+. That
    // keeps the adder out of `stall`, which gates every pipeline register.
    reg  mem_started;
    reg  sel_spad, sel_ctrl, sel_cache, sel_unc;
    wire mem_first = dmem_req_c && !mem_started && !mmu_busy;

    wire l1_act  = sel_cache && mem_started;
    wire unc_act = sel_unc   && mem_started;

    wire [63:0] l1_rdata;
    wire        l1_stall, l1_flush_busy;
    wire        fill_valid, fill_ready, resp_valid;
    wire [ADDR_W-6:0] fill_addr, wb_addr;
    wire [255:0] resp_data, wb_data;
    wire        wb_valid, wb_ready, wr_idle;

    // REGISTERED INTO THE L1. Driven combinationally, the forward mux reached
    // the data array's byte-write-enable through the address adder and the
    // decode: `wb_val_reg -> u_l1/.../ENBWREN`, 14 levels, 200 failing paths.
    // The core is held across the lookup anyway, so the register is free.
    reg [ADDR_W-1:0] m_pa_q;
    reg [7:0]        m_be_q;
    reg [63:0]       m_wd_q;
    reg              m_st_q;
    always @(posedge clk) if (mem_first) begin
        m_pa_q <= pa;
        m_be_q <= dmem_wstrb_c;
        m_wd_q <= dmem_wdata_c;
        m_st_q <= core_is_st;
    end

    rv64_l1 #(.LINES(L1_LINES), .ADDR_W(ADDR_W), .MEM_PRIM(MEM_PRIM)) u_l1 (
        .clk(clk), .resetn(core_rstn),
        .probe_addr(m_pa_q), .req(l1_act), .we(m_st_q), .be(m_be_q),
        .addr(m_pa_q), .wdata(m_wd_q), .rdata(l1_rdata), .stall(l1_stall),
        .flush(1'b0), .inval(1'b0), .flush_busy(l1_flush_busy),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data),
        .wb_valid(wb_valid), .wb_ready(wb_ready), .wb_addr(wb_addr),
        .wb_data(wb_data), .wr_idle(wr_idle)
    );

    // The L1 sees the access a cycle later now, so it is done only once its own
    // request has been asserted and its stall has cleared.
    wire l1_done = l1_act && !l1_stall;

    // ------------------------------------------------------- uncached access
    wire        u_ack;
    wire [63:0] u_rdata;
    wire        unc_done = unc_act && u_ack;

    // A local access is answered by the array in the cycle after the address,
    // which is exactly when `mem_started` first holds.
    wire mem_done = mem_started
                 && ((sel_spad || sel_ctrl) ? 1'b1
                   : sel_cache             ? l1_done
                   : sel_unc               ? unc_done
                                           : 1'b1);

    always @(posedge clk) begin
        if (!core_rstn) begin
            mem_started <= 1'b0;
        end
        else if (mem_first) begin
            mem_started <= 1'b1;
            sel_spad    <= in_spad  && !in_node;
            sel_ctrl    <= in_ctrl  && !in_node;
            sel_cache   <= in_cache;
            sel_unc     <= in_unc;
        end
        else if (mem_started && mem_done) begin
            mem_started <= 1'b0;
        end
    end

    rv64_nport #(.ADDR_W(ADDR_W), .DATA_W(DATA_W)) u_np (
        .clk(clk), .resetn(resetn),
        .w_req(w_req), .w_addr(w_addr), .w_ack(w_ack), .w_data(w_data),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data),
        .wb_valid(wb_valid), .wb_ready(wb_ready), .wb_addr(wb_addr),
        .wb_data(wb_data),
        .u_req(unc_act && !unc_done), .u_we(m_st_q), .u_addr(m_pa_q),
        .u_be(m_be_q), .u_wdata(m_wd_q),
        .u_ack(u_ack), .u_rdata(u_rdata), .wr_idle(wr_idle),
        .cp_awaddr(cp_awaddr), .cp_awlen(cp_awlen), .cp_awvalid(cp_awvalid),
        .cp_awready(cp_awready), .cp_wdata(cp_wdata), .cp_wstrb(cp_wstrb),
        .cp_wlast(cp_wlast), .cp_wvalid(cp_wvalid), .cp_wready(cp_wready),
        .cp_bvalid(cp_bvalid), .cp_bready(cp_bready),
        .cp_araddr(cp_araddr), .cp_arlen(cp_arlen), .cp_arvalid(cp_arvalid),
        .cp_arready(cp_arready), .cp_rdata(cp_rdata), .cp_rlast(cp_rlast),
        .cp_rvalid(cp_rvalid), .cp_rready(cp_rready)
    );

    // ---------------------------------------------------- the control region
    // `mv.go` IS A STORE, not an opcode: decoding it from an address keeps the
    // ISA unchanged and matches the rule that control is a range.
    localparam [7:0] R_EXIT = 8'h00, R_CONSOLE = 8'h08, R_DBELL = 8'h10;
    localparam [7:0] R_SATP = 8'h18, R_NOC = 8'h40, R_MVCFG = 8'h80;
    localparam [7:0] R_DBCFG = 8'hC0;

    // READS EARLY, WRITES REGISTERED, for the same reason as the spad: the read
    // must be answered in the second cycle, the write must not carry the address
    // adder into a register's clock enable (`wb_val_reg -> db_addr_reg/CE`).
    wire [7:0] ctrl_off_rd = pa[7:0];
    wire [7:0] ctrl_off    = m_pa_q[7:0];
    wire       ctrl_wr     = mem_started && sel_ctrl && m_st_q;

    always @(posedge clk) begin
        if (!resetn) begin
            exited    <= 1'b0;
            mv_cfg_en <= 1'b0;
            db_en     <= 1'b0;
            nm_en     <= 1'b0;
        end
        else begin
            mv_cfg_en <= 1'b0;
            db_en     <= 1'b0;
            nm_en     <= 1'b0;
            if (ctrl_wr) begin
                if (ctrl_off[7:6] == 2'b01) begin
                    nm_en   <= 1'b1;
                    nm_addr <= ctrl_off[5:3];
                    nm_data <= m_wd_q;
                end
                if (ctrl_off == R_EXIT) begin
                    exited    <= 1'b1;
                    exit_word <= m_wd_q;
                end
                // The mover's window and the doorbell's are sub-ranges, so the
                // register index comes from the address rather than a decode.
                if (ctrl_off[7:6] == 2'b10) begin
                    mv_cfg_en   <= 1'b1;
                    mv_cfg_addr <= {2'd0, ctrl_off[5:0]};
                    mv_cfg_data <= m_wd_q;
                end
                if (ctrl_off[7:6] == 2'b11) begin
                    // BIT 7 SET: the interlink claims a config write only at
                    // 0x80 and above (enable 0x80, mesh 0x88, ring 0x90), so
                    // the window's offset 0x00..0x3F maps onto 0x80..0xBF.
                    // Without it every doorbell write was dropped, silently.
                    db_en   <= 1'b1;
                    db_addr <= {2'b10, ctrl_off[5:0]};
                    db_data <= m_wd_q;
                end
            end
            if (boot_req) begin
                exited <= 1'b0;
            end
        end
    end

    assign dbg_console_we = ctrl_wr && (ctrl_off == R_CONSOLE);
    assign dbg_console    = m_wd_q[7:0];

    reg [63:0] ctrl_q;
    always @(posedge clk) begin
        if (ctrl_off_rd[7:6] == 2'b01) begin
            ctrl_q <= nm_rdata;
        end else begin
            case (ctrl_off_rd)
                R_DBELL: ctrl_q <= {63'd0, dbell};
                // A read-only mirror of the CSR.
                R_SATP:  ctrl_q <= core_satp;
                8'h20:   ctrl_q <= {31'd0, mv_busy, mv_fault, mv_done[27:0]};
                8'h28:   ctrl_q <= db_status;
                default: ctrl_q <= 64'd0;
            endcase
        end
    end

    rv64_noc_mbox #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH), .CQ_DEPTH(16)
    ) u_mbox (
        .clk(clk), .resetn(core_rstn),
        .my_x(my_x), .my_y(my_y),
        .cfg_en(nm_en), .cfg_addr(nm_addr), .cfg_data(nm_data),
        .rd_addr(ctrl_off_rd[5:3]), .rd_data(nm_rdata),
        .tx_data(noc_out_data), .tx_valid(noc_out_valid),
        .tx_busy(noc_out_busy),
        .rx_data(noc_in_data), .rx_valid(noc_in_valid), .rx_busy(noc_in_busy),
        .cq_nonempty(noc_cq_nonempty)
    );

    // ------------------------------------------------------ the return path
    localparam [1:0] P_SPAD = 2'd0, P_CTRL = 2'd1, P_L1 = 2'd2, P_UNC = 2'd3;
    reg [1:0]  path_q;
    reg [63:0] l1_hold, unc_hold;

    // From the REGISTERED select, so the return path does not carry the decode.
    wire [1:0] path_sel = (
        sel_cache  ? P_L1
        : sel_unc  ? P_UNC
        : sel_ctrl ? P_CTRL
        : P_SPAD
    );
    always @(posedge clk) begin
        path_q <= path_sel;
        if (l1_done) begin
            l1_hold <= l1_rdata;
        end
        if (unc_done) begin
            unc_hold <= u_rdata;
        end
    end

    assign dmem_rdata_c = (
        (path_q == P_L1)     ? l1_hold
        : (path_q == P_UNC)  ? unc_hold
        : (path_q == P_CTRL) ? ctrl_q
        : spad_q
    );

    // SHALLOW BY CONSTRUCTION: `dmem_req_c` is decode-only and everything else
    // here is a register.
    assign dmem_stall_c = mmu_busy || mem_first || (mem_started && !mem_done);

    // ------------------------------------------------------------- the core
    always @(posedge clk) begin
        if (!resetn) begin
            core_rstn <= 1'b0;
        end else if (boot_ack) begin
            core_rstn <= 1'b1;
        end else if (!run_en) begin
            core_rstn <= 1'b0;
        end
    end
    assign running = core_rstn && !core_halted;

    rv64_core #(
        .RESET_PC(64'd0), .MEM_PRIM(RF_PRIM), .HAS_ATOMIC(1),
        .PADDR_W(ADDR_W)
    ) u_core (
        .clk(clk), .resetn(core_rstn),
        .imem_addr(imem_addr_c), .imem_data(imem_data_c),
        .imem_stall(imem_stall_c),
        .dmem_addr(dmem_addr_c), .dmem_wdata(dmem_wdata_c),
        .dmem_wstrb(dmem_wstrb_c), .dmem_re(dmem_re_c),
        .dmem_rdata(dmem_rdata_c), .dmem_stall(dmem_stall_c),
        .dmem_req(dmem_req_c), .dmem_st(dmem_st_c),
        .dmem_fault(dmem_fault_w), .dmem_fault_cause(mmu_cause),
        .imem_fault(imem_fault_c),
        .priv_o(core_priv), .satp_o(core_satp),
        .sum_o(core_sum), .mxr_o(core_mxr), .sfence_o(core_sfence),
        .priv_settle_o(core_settle),
        // A completion waiting is exactly the condition a scheduler must not
        // have to poll for, so it raises the external line beside the node's.
        .irq_ext(irq_summary || noc_cq_nonempty), .irq_soft(dbell),
        .ext_halt(exited),
        .halted(core_halted), .halt_cause(core_cause), .halt_pc(core_halt_pc),
        .dbg_pc(), .dbg_retire(core_retire)
    );

    // 64-bit, unlike the CU shell's 32: a runtime runs long enough to wrap 32.
    reg [63:0] cyc_ctr, ret_ctr;
    always @(posedge clk) begin
        if (!resetn || boot_ack) begin
            cyc_ctr <= 64'd0;
            ret_ctr <= 64'd0;
        end else if (core_rstn) begin
            cyc_ctr <= cyc_ctr + 64'd1;
            if (core_retire) begin
                ret_ctr <= ret_ctr + 64'd1;
            end
        end
    end
    assign dbg_cycles  = cyc_ctr;
    assign dbg_retired = ret_ctr;

endmodule

`default_nettype wire
