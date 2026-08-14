// What the mover moves, on the shapes the machine actually asks for, against a
// memory model whose latency is set in MESH CYCLES and whose clock is stamped.

// Both movers run: mm_mover_v1 is the pre-burst engine, so before and after are
// one measurement against one memory, not a rate law compared to a rate law.

// The latencies are the ones MEASURED through the stock mag_dram_port:
// 22 mesh cycles at 100 MHz, 49 at 300.

// Every move is CHECKED, not just timed. A bandwidth figure for a copy that
// moved the wrong bytes is the failure this bench exists to make impossible.

`default_nettype none
`timescale 1ns/1ps

module mm_mover_bw_tb;
    localparam DW = 256, AW = 40, IDW = 4;
    localparam integer WORDS = 65536;

    // Byte bases. DST is 1 MB up, which is word 32768 of the model.
    localparam [AW-1:0] SRC = 40'h00_0000;
    localparam [AW-1:0] DST = 40'h10_0000;

    // Head-to-head sizes. The old mover costs ~106 cycles a word at 300 MHz, so
    // 1024 words is what keeps the whole bench inside a check.py budget.
    localparam integer NC    = 1024;      // contiguous copy, both movers
    localparam integer NBIG  = 16384;     // contiguous copy, new mover only
    localparam integer CRUNS = 51;        // conv A': runs
    localparam integer CRUN  = 20;        // conv A': words per run
    localparam integer CROW  = 24;        // conv A': source row pitch, words
    localparam integer RT    = 32;        // relayout: a 32 x 32 word transpose

    real    mesh_mhz = 300.0;
    real    half     = 1.6667;
    integer lat_rd_i = 49, lat_wr_i = 49;

    reg clk = 0, resetn = 0;
    always #(half) clk = ~clk;

    integer cyc = 0;
    always @(posedge clk) cyc <= cyc + 1;

    // ---- configuration bus, routed to whichever mover is selected ---------
    reg        sel;                       // 0 = mm_mover_v1, 1 = mm_mover
    reg        cfg_en_o, cfg_en_n;
    reg [7:0]  cfg_addr;
    reg [63:0] cfg_data;

    wire        o_busy, n_busy;
    wire [3:0]  o_fault, n_fault;
    wire [31:0] o_done, n_done;

    wire busy = sel ? n_busy : o_busy;
    wire [3:0] flt = sel ? n_fault : o_fault;

    // ---- the two masters --------------------------------------------------
    wire [IDW-1:0] o_awid, o_arid, n_awid, n_arid;
    wire [AW-1:0]  o_awaddr, o_araddr, n_awaddr, n_araddr;
    wire [7:0]     o_awlen, o_arlen, n_awlen, n_arlen;
    wire           o_awvalid, o_wvalid, o_wlast, o_arvalid, o_bready, o_rready;
    wire           n_awvalid, n_wvalid, n_wlast, n_arvalid, n_bready, n_rready;
    wire [DW-1:0]  o_wdata, n_wdata;

    // ---- the memory's side ------------------------------------------------
    wire [IDW-1:0] m_awid, m_arid, m_bid, m_rid;
    wire [AW-1:0]  m_awaddr, m_araddr;
    wire [7:0]     m_awlen, m_arlen;
    wire           m_awvalid, m_wvalid, m_wlast, m_arvalid;
    wire           m_awready, m_wready, m_arready;
    wire [DW-1:0]  m_wdata, m_rdata;
    wire [1:0]     m_bresp, m_rresp;
    wire           m_bvalid, m_rvalid, m_rlast;

    assign m_awid    = sel ? n_awid    : o_awid;
    assign m_awaddr  = sel ? n_awaddr  : o_awaddr;
    assign m_awlen   = sel ? n_awlen   : o_awlen;
    assign m_awvalid = sel ? n_awvalid : o_awvalid;
    assign m_wdata   = sel ? n_wdata   : o_wdata;
    assign m_wlast   = sel ? n_wlast   : o_wlast;
    assign m_wvalid  = sel ? n_wvalid  : o_wvalid;
    assign m_arid    = sel ? n_arid    : o_arid;
    assign m_araddr  = sel ? n_araddr  : o_araddr;
    assign m_arlen   = sel ? n_arlen   : o_arlen;
    assign m_arvalid = sel ? n_arvalid : o_arvalid;

    // The idle mover must see no response at all: the new one would push a
    // stray R beat into its FIFO and decrement a write counter below zero.
    wire o_awready = !sel && m_awready;
    wire o_wready  = !sel && m_wready;
    wire o_bvalid  = !sel && m_bvalid;
    wire o_arready = !sel && m_arready;
    wire o_rvalid  = !sel && m_rvalid;
    wire n_awready =  sel && m_awready;
    wire n_wready  =  sel && m_wready;
    wire n_bvalid  =  sel && m_bvalid;
    wire n_arready =  sel && m_arready;
    wire n_rvalid  =  sel && m_rvalid;

    reg [15:0] lat_rd, lat_wr;
    reg [7:0]  maxout;

    mm_mover_v1 #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .IDX_WORDS(128))
    u_old (
        .clk(clk), .resetn(resetn),
        .cfg_en(cfg_en_o), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
        .stat_busy(o_busy), .stat_fault(o_fault), .stat_done(o_done),
        .m_awid(o_awid), .m_awaddr(o_awaddr), .m_awlen(o_awlen), .m_awsize(),
        .m_awburst(), .m_awvalid(o_awvalid), .m_awready(o_awready),
        .m_wdata(o_wdata), .m_wstrb(), .m_wlast(o_wlast), .m_wvalid(o_wvalid),
        .m_wready(o_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(o_bvalid),
        .m_bready(o_bready),
        .m_arid(o_arid), .m_araddr(o_araddr), .m_arlen(o_arlen), .m_arsize(),
        .m_arburst(), .m_arvalid(o_arvalid), .m_arready(o_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_rvalid(o_rvalid), .m_rready(o_rready)
    );

    mm_mover #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .IDX_WORDS(128))
    u_new (
        .clk(clk), .resetn(resetn),
        .cfg_en(cfg_en_n), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
        .stat_busy(n_busy), .stat_fault(n_fault), .stat_done(n_done),
        .m_awid(n_awid), .m_awaddr(n_awaddr), .m_awlen(n_awlen), .m_awsize(),
        .m_awburst(), .m_awvalid(n_awvalid), .m_awready(n_awready),
        .m_wdata(n_wdata), .m_wstrb(), .m_wlast(n_wlast), .m_wvalid(n_wvalid),
        .m_wready(n_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(n_bvalid),
        .m_bready(n_bready),
        .m_arid(n_arid), .m_araddr(n_araddr), .m_arlen(n_arlen), .m_arsize(),
        .m_arburst(), .m_arvalid(n_arvalid), .m_arready(n_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp), .m_rlast(m_rlast),
        .m_rvalid(n_rvalid), .m_rready(n_rready)
    );

    mm_bw_mem #(.DW(DW), .AW(AW), .IDW(IDW), .WORDS(WORDS)) u_mem (
        .clk(clk), .resetn(resetn),
        .lat_rd(lat_rd), .lat_wr(lat_wr), .maxout(maxout),
        .s_awid(m_awid), .s_awaddr(m_awaddr), .s_awlen(m_awlen),
        .s_awvalid(m_awvalid), .s_awready(m_awready),
        .s_wdata(m_wdata), .s_wlast(m_wlast), .s_wvalid(m_wvalid),
        .s_wready(m_wready),
        .s_bid(m_bid), .s_bresp(m_bresp), .s_bvalid(m_bvalid),
        .s_bready(sel ? n_bready : o_bready),
        .s_arid(m_arid), .s_araddr(m_araddr), .s_arlen(m_arlen),
        .s_arvalid(m_arvalid), .s_arready(m_arready),
        .s_rid(m_rid), .s_rdata(m_rdata), .s_rresp(m_rresp),
        .s_rlast(m_rlast), .s_rvalid(m_rvalid),
        .s_rready(sel ? n_rready : o_rready)
    );

    // ---- AXI burst monitor ------------------------------------------------

    // Where the 4 KB rule actually bites: these bursts are up to 128 words and
    // one page wide, so one page also means one mesh and one aperture.
    integer merr = 0, mchk = 0, n_ar = 0, n_aw = 0, max_ar = 0, max_aw = 0;

    always @(posedge clk) if (resetn) begin
        if (m_arvalid && m_arready) begin
            n_ar = n_ar + 1; mchk = mchk + 2;
            if (m_arlen + 1 > max_ar) max_ar = m_arlen + 1;
            if (|m_araddr[4:0] ||
                (({1'b0, m_araddr[11:5]} + {1'b0, m_arlen} + 9'd1) > 9'd128))
                begin
                    merr = merr + 1;
                    $display("  ERROR AR %h len %0d", m_araddr, m_arlen);
                end
        end
        if (m_awvalid && m_awready) begin
            n_aw = n_aw + 1; mchk = mchk + 2;
            if (m_awlen + 1 > max_aw) max_aw = m_awlen + 1;
            if (|m_awaddr[4:0] ||
                (({1'b0, m_awaddr[11:5]} + {1'b0, m_awlen} + 9'd1) > 9'd128))
                begin
                    merr = merr + 1;
                    $display("  ERROR AW %h len %0d", m_awaddr, m_awlen);
                end
        end
    end

    // ---- bookkeeping ------------------------------------------------------
    integer errors = 0, checks = 0, spin, c0, cyc_res;
    integer i, j, k;
    real    wpc, mbs, cpw;

    task chk(input [255:0] got, input [255:0] want, input [255:0] what,
             input integer where);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                if (errors < 20)
                    $display("  ERROR %0s [%0d]: got %h want %h",
                             what, where, got[63:0], want[63:0]);
            end
        end
    endtask

    task setclk(input real mhz, input integer lr, input integer lw);
        begin
            half     = 500.0 / mhz;
            mesh_mhz = mhz;
            lat_rd   = lr[15:0];
            lat_wr   = lw[15:0];
            lat_rd_i = lr;
            lat_wr_i = lw;
            repeat (8) @(negedge clk);
        end
    endtask

    task report(input [255:0] tag, input integer words);
        begin
            wpc = words * 1.0 / cyc_res;
            mbs = wpc * 32.0 * mesh_mhz;
            cpw = cyc_res * 1.0 / words;
            $display("  %0s | %0d w %0d cyc | %0d.%02d cyc/w | 0.%03d w/cyc | %0d MB/s @ %0d MHz",
                     tag, words, cyc_res,
                     $rtoi(cpw), $rtoi((cpw - $rtoi(cpw)) * 100.0),
                     $rtoi(wpc * 1000.0), $rtoi(mbs), $rtoi(mesh_mhz));
        end
    endtask

    // ---- configuration helpers -------------------------------------------
    task wr(input [7:0] a, input [63:0] d);
        begin
            @(negedge clk);
            cfg_addr = a; cfg_data = d;
            cfg_en_n = sel; cfg_en_o = !sel;
            @(negedge clk);
            cfg_en_n = 1'b0; cfg_en_o = 1'b0;
        end
    endtask

    task hdr(input s, input [AW-1:0] base, input [2:0] ndim);
        begin wr(8'h10, {17'd0, ndim, base, 3'd0, s}); end
    endtask

    task dim(input s, input [2:0] d, input [15:0] cnt,
             input signed [31:0] strd);
        begin
            wr(8'h18, {12'd0, strd, cnt, d, s});
            wr(8'h20, 64'd0);
        end
    endtask

    task launch(input [2:0] m, input [7:0] fl);
        begin
            wr(8'h00, {47'd0, 1'b1, fl, 3'd0, 2'd1, m});
            spin = 0;
            while (!busy && (spin < 200)) begin spin = spin + 1; @(negedge clk); end
            c0 = cyc;
            spin = 0;
            while (busy && (spin < 4000000)) begin
                spin = spin + 1; @(negedge clk);
            end
            cyc_res = cyc - c0;
            if (spin >= 4000000) begin
                errors = errors + 1;
                $display("  ERROR mover never went idle (mode %0d, sel %0d)", m, sel);
            end
            if (flt != 4'd0) begin
                errors = errors + 1;
                $display("  ERROR fault %0d (mode %0d, sel %0d)", flt, m, sel);
            end
        end
    endtask

    task clear_dst(input integer nw);
        begin
            for (k = 0; k < nw; k = k + 1)
                u_mem.mem[(DST >> 5) + k] = 256'd0;
        end
    endtask

    // ---- the workloads ----------------------------------------------------
    // A plain contiguous copy: the easy case, and the one that shows the
    // ceiling is reachable at all.
    task setup_copy(input integer nw);
        begin
            hdr(1'b0, SRC, 3'd1);
            dim(1'b0, 3'd0, nw[15:0], 32'sd32);
            hdr(1'b1, DST, 3'd1);
            dim(1'b1, 3'd0, nw[15:0], 32'sd32);
        end
    endtask

    task check_copy(input integer nw);
        begin
            for (k = 0; k < nw; k = k + 1)
                chk(u_mem.mem[(DST >> 5) + k], {8{k[31:0]}}, "copy", k);
        end
    endtask

    // conv A': CRUNS runs of CRUN words lifted out of rows of CROW and laid
    // down back to back -- the case that motivated all of this.
    task setup_conv;
        begin
            hdr(1'b0, SRC, 3'd2);
            dim(1'b0, 3'd0, CRUNS[15:0], CROW * 32);
            dim(1'b0, 3'd1, CRUN[15:0],  32'sd32);
            hdr(1'b1, DST, 3'd1);
            dim(1'b1, 3'd0, (CRUNS * CRUN), 32'sd32);
        end
    endtask

    task check_conv;
        begin
            for (i = 0; i < CRUNS; i = i + 1)
                for (j = 0; j < CRUN; j = j + 1)
                    chk(u_mem.mem[(DST >> 5) + i*CRUN + j],
                        {8{(i*CROW + j)}}, "conv", i*CRUN + j);
        end
    endtask

    // A relayout with runs of one word: an RT x RT word transpose. Bursting
    // cannot help the read side here and the bench says so.
    task setup_relayout;
        begin
            hdr(1'b0, SRC, 3'd2);
            dim(1'b0, 3'd0, RT[15:0], 32'sd32);
            dim(1'b0, 3'd1, RT[15:0], RT * 32);
            hdr(1'b1, DST, 3'd2);
            dim(1'b1, 3'd0, RT[15:0], RT * 32);
            dim(1'b1, 3'd1, RT[15:0], 32'sd32);
        end
    endtask

    task check_relayout;
        begin
            for (i = 0; i < RT; i = i + 1)
                for (j = 0; j < RT; j = j + 1)
                    chk(u_mem.mem[(DST >> 5) + i*RT + j],
                        {8{(j*RT + i)}}, "relayout", i*RT + j);
        end
    endtask

    task setup_fill(input integer nw);
        begin
            wr(8'h40, 64'h0000_0000_0000_1234);
            hdr(1'b1, DST, 3'd1);
            dim(1'b1, 3'd0, nw[15:0], 32'sd32);
        end
    endtask

    task check_fill(input integer nw);
        begin
            for (k = 0; k < nw; k = k + 1)
                chk(u_mem.mem[(DST >> 5) + k], {16{16'h1234}}, "fill", k);
        end
    endtask

    // ---- one measured move ------------------------------------------------
    task move(input integer which, input integer kind, input [7:0] fl,
              input [255:0] tag, input integer nw);
        begin
            sel = which[0];
            clear_dst(nw);
            case (kind)
            0: setup_copy(nw);
            1: setup_conv;
            2: setup_relayout;
            default: setup_fill(nw);
            endcase
            launch((kind == 3) ? 3'd4 : 3'd0, fl);
            case (kind)
            0: check_copy(nw);
            1: check_conv;
            2: check_relayout;
            default: check_fill(nw);
            endcase
            report(tag, nw);
        end
    endtask

    initial begin
        cfg_en_o = 0; cfg_en_n = 0; cfg_addr = 0; cfg_data = 0; sel = 1;
        lat_rd = 49; lat_wr = 49; maxout = 8'd4;

        for (i = 0; i < WORDS; i = i + 1) u_mem.mem[i] = 256'd0;
        for (i = 0; i < 32768; i = i + 1) u_mem.mem[i] = {8{i[31:0]}};

        repeat (12) @(negedge clk);
        resetn = 1;
        repeat (12) @(negedge clk);

        // =================================================================
        setclk(300.0, 49, 49);
        $display("--- mesh 300 MHz, AR->R 49 cyc, W->B 49 cyc, slave holds 4 reads ---");
        $display("--- before: mm_mover_v1, one 32-byte transaction at a time ---");
        move(0, 0, 8'h00, "copy      before   ", NC);
        move(0, 1, 8'h00, "conv A'   before   ", CRUNS * CRUN);
        move(0, 2, 8'h00, "relayout  before   ", RT * RT);
        move(0, 3, 8'h00, "fill      before   ", NC);

        $display("--- after: read bursts only (flags[3] = 0, the safe default) ---");
        move(1, 0, 8'h00, "copy      rdburst  ", NC);
        move(1, 1, 8'h00, "conv A'   rdburst  ", CRUNS * CRUN);
        move(1, 2, 8'h00, "relayout  rdburst  ", RT * RT);
        move(1, 3, 8'h00, "fill      rdburst  ", NC);

        $display("--- after: read and write bursts (flags[3] = 1) ---");
        move(1, 0, 8'h08, "copy      rd+wr    ", NC);
        move(1, 1, 8'h08, "conv A'   rd+wr    ", CRUNS * CRUN);
        move(1, 2, 8'h08, "relayout  rd+wr    ", RT * RT);
        move(1, 3, 8'h08, "fill      rd+wr    ", NC);

        $display("--- the same, with the burst cap forced to 1 word (flags[7:5]=1) ---");
        move(1, 0, 8'h20, "copy      cap1     ", NC);

        // What the slave's outstanding-read depth is worth. P3 in the analysis
        // is exactly this number, and it lives in mag_dram_port, not here.
        $display("--- outstanding reads the slave will hold: P3's whole payoff ---");
        maxout = 8'd1;
        move(1, 0, 8'h08, "copy      k=1      ", NC);
        move(1, 1, 8'h08, "conv A'   k=1      ", CRUNS * CRUN);
        move(1, 2, 8'h08, "relayout  k=1      ", RT * RT);
        maxout = 8'd4;
        move(1, 1, 8'h08, "conv A'   k=4      ", CRUNS * CRUN);
        move(1, 2, 8'h08, "relayout  k=4      ", RT * RT);
        maxout = 8'd16;
        move(1, 1, 8'h08, "conv A'   k=16     ", CRUNS * CRUN);
        move(1, 2, 8'h08, "relayout  k=16     ", RT * RT);
        maxout = 8'd4;

        // A posted 4-cycle write acknowledgement is the interlink's, and it is
        // the configuration the card's 98 MB/s was taken in.
        $display("--- before, with a posted write ack (the remote case) ---");
        setclk(300.0, 49, 2);
        move(0, 0, 8'h00, "copy      before/rem", NC);
        setclk(300.0, 49, 49);

        // A large FILL has no read side at all, so it isolates the write path.
        $display("--- the ceiling: one large copy and one large fill ---");
        move(1, 0, 8'h08, "copy 512KB rd+wr   ", NBIG);
        move(1, 3, 8'h08, "fill 512KB rd+wr   ", NBIG);

        // =================================================================
        setclk(100.09, 22, 22);
        $display("--- mesh 100.09 MHz, AR->R 22 cyc, W->B 22 cyc: where the card was ---");
        move(0, 0, 8'h00, "copy      before   ", NC);
        move(0, 1, 8'h00, "conv A'   before   ", CRUNS * CRUN);
        move(1, 0, 8'h08, "copy      rd+wr    ", NC);
        move(1, 1, 8'h08, "conv A'   rd+wr    ", CRUNS * CRUN);
        move(1, 2, 8'h08, "relayout  rd+wr    ", RT * RT);

        checks = checks + mchk;
        errors = errors + merr;
        $display("  %0d AR (longest %0d w) and %0d AW (longest %0d w) bursts, all aligned and inside 4 KB",
                 n_ar, max_ar, n_aw, max_aw);

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #400000000;
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

// An AXI4 slave whose read round trip, write-to-B and outstanding-read depth
// are inputs, so one bench covers 100 MHz, 300 MHz and P3's depth in one run.

// Beats return back to back at one per cycle once a burst is due, which is what
// memory-mover.md s1.3 measured mag_dram_port doing.

module mm_bw_mem #(
    parameter integer DW    = 256,
    parameter integer AW    = 40,
    parameter integer IDW   = 4,
    parameter integer WORDS = 65536,
    parameter integer QD    = 64
)(
    input  wire clk,
    input  wire resetn,
    input  wire [15:0] lat_rd,
    input  wire [15:0] lat_wr,
    input  wire [7:0]  maxout,

    input  wire [IDW-1:0] s_awid,
    input  wire [AW-1:0]  s_awaddr,
    input  wire [7:0]     s_awlen,
    input  wire           s_awvalid,
    output wire           s_awready,
    input  wire [DW-1:0]  s_wdata,
    input  wire           s_wlast,
    input  wire           s_wvalid,
    output wire           s_wready,
    output reg  [IDW-1:0] s_bid,
    output wire [1:0]     s_bresp,
    output reg            s_bvalid,
    input  wire           s_bready,

    input  wire [IDW-1:0] s_arid,
    input  wire [AW-1:0]  s_araddr,
    input  wire [7:0]     s_arlen,
    input  wire           s_arvalid,
    output wire           s_arready,
    output reg  [IDW-1:0] s_rid,
    output reg  [DW-1:0]  s_rdata,
    output wire [1:0]     s_rresp,
    output reg            s_rlast,
    output reg            s_rvalid,
    input  wire           s_rready
);
    localparam integer LSB = 5;
    localparam integer MW  = 16;          // $clog2(65536)
    localparam integer QW  = 6;           // $clog2(QD)

    reg [DW-1:0] mem [0:WORDS-1];
    integer mcyc;

    assign s_bresp = 2'b00;
    assign s_rresp = 2'b00;

    // ---- reads ------------------------------------------------------------
    reg [AW-1:0]  q_addr [0:QD-1];
    reg [7:0]     q_len  [0:QD-1];
    reg [IDW-1:0] q_id   [0:QD-1];
    integer       q_due  [0:QD-1];
    reg [QW-1:0]  qh, qt;
    reg [7:0]     qn;
    reg [AW-1:0]  r_addr;
    reg [8:0]     r_left;
    reg           serving;

    wire ar_acc = s_arvalid && s_arready;
    wire r_acc  = s_rvalid  && s_rready;
    wire r_end  = r_acc && (r_left == 9'd1);
    wire [AW-1:0] r_next = r_addr + {{(AW-6){1'b0}}, 6'd32};

    assign s_arready = (qn < maxout) && (qn < QD[7:0]);

    always @(posedge clk) begin
        if (!resetn) begin
            mcyc <= 0; qh <= 0; qt <= 0; qn <= 8'd0; serving <= 1'b0;
            s_rvalid <= 1'b0; s_rlast <= 1'b0; r_left <= 9'd0;
        end else begin
            mcyc <= mcyc + 1;
            if (ar_acc) begin
                q_addr[qt] <= s_araddr;
                q_len[qt]  <= s_arlen;
                q_id[qt]   <= s_arid;
                q_due[qt]  <= mcyc + lat_rd;
                qt <= qt + 1'b1;
            end
            qn <= qn + (ar_acc ? 8'd1 : 8'd0) - (r_end ? 8'd1 : 8'd0);

            if (!serving) begin
                if ((qn != 8'd0) && (mcyc >= q_due[qh])) begin
                    serving  <= 1'b1;
                    r_addr   <= q_addr[qh];
                    r_left   <= {1'b0, q_len[qh]} + 9'd1;
                    s_rid    <= q_id[qh];
                    s_rdata  <= mem[q_addr[qh][MW+LSB-1:LSB]];
                    s_rlast  <= (q_len[qh] == 8'd0);
                    s_rvalid <= 1'b1;
                end
            end else if (r_acc) begin
                if (r_left == 9'd1) begin
                    serving  <= 1'b0;
                    s_rvalid <= 1'b0;
                    s_rlast  <= 1'b0;
                    qh <= qh + 1'b1;
                end else begin
                    r_addr  <= r_next;
                    r_left  <= r_left - 9'd1;
                    s_rdata <= mem[r_next[MW+LSB-1:LSB]];
                    s_rlast <= (r_left == 9'd2);
                end
            end
        end
    end

    // ---- writes -----------------------------------------------------------
    reg [AW-1:0]  wq_addr [0:QD-1];
    reg [7:0]     wq_len  [0:QD-1];
    reg [IDW-1:0] wq_id   [0:QD-1];
    reg [QW-1:0]  wh, wt;
    reg [7:0]     wn;
    reg [AW-1:0]  cw_addr;
    reg [8:0]     cw_left;
    reg [IDW-1:0] cw_id;
    reg           cw_act;

    integer       bq_due [0:QD-1];
    reg [IDW-1:0] bq_id  [0:QD-1];
    reg [QW-1:0]  bh, bt;
    reg [7:0]     bn;

    wire aw_acc = s_awvalid && s_awready;
    wire w_acc  = s_wvalid  && s_wready;
    wire w_end  = w_acc && (cw_left == 9'd1);
    wire w_open = !cw_act && (wn != 8'd0);
    wire b_go   = (!s_bvalid || s_bready) && (bn != 8'd0)
                  && (mcyc >= bq_due[bh]);

    assign s_awready = (wn < QD[7:0]);
    assign s_wready  = cw_act;

    always @(posedge clk) begin
        if (!resetn) begin
            wh <= 0; wt <= 0; wn <= 8'd0; cw_act <= 1'b0; cw_left <= 9'd0;
            bh <= 0; bt <= 0; bn <= 8'd0; s_bvalid <= 1'b0;
        end else begin
            if (aw_acc) begin
                wq_addr[wt] <= s_awaddr;
                wq_len[wt]  <= s_awlen;
                wq_id[wt]   <= s_awid;
                wt <= wt + 1'b1;
            end
            wn <= wn + (aw_acc ? 8'd1 : 8'd0) - (w_open ? 8'd1 : 8'd0);

            if (w_open) begin
                cw_act  <= 1'b1;
                cw_addr <= wq_addr[wh];
                cw_left <= {1'b0, wq_len[wh]} + 9'd1;
                cw_id   <= wq_id[wh];
                wh <= wh + 1'b1;
            end else if (w_acc) begin
                mem[cw_addr[MW+LSB-1:LSB]] <= s_wdata;
                cw_addr <= cw_addr + {{(AW-6){1'b0}}, 6'd32};
                cw_left <= cw_left - 9'd1;
                if (cw_left == 9'd1) cw_act <= 1'b0;
            end

            if (w_end) begin
                bq_due[bt] <= mcyc + lat_wr;
                bq_id[bt]  <= cw_id;
                bt <= bt + 1'b1;
            end
            bn <= bn + (w_end ? 8'd1 : 8'd0) - (b_go ? 8'd1 : 8'd0);

            if (s_bvalid && s_bready) s_bvalid <= 1'b0;
            if (b_go) begin
                s_bvalid <= 1'b1;
                s_bid    <= bq_id[bh];
                bh <= bh + 1'b1;
            end
        end
    end
endmodule

`default_nettype wire
