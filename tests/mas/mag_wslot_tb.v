// MAG write-slot bench: two sources writing at once, checked at the memory.
//
// This exists because the write-slot table has broken twice, and both times it
// was found from a whole-machine run where the only symptom was a GEMM that
// never finished. A write slot is a four-state object -- free, waiting for
// data, ready, on the bus -- and the interesting failures are all mis-bindings
// between a WR_DATA flit and a slot. None of them need a cluster, a NoC or an
// accumulator to reproduce; they need two sources and a bus that does not
// answer instantly.
//
// The design under test is mag alone. Flits are driven straight into its NoC
// memory port, so a failure here is MAG's, and cannot be anything else's.
//
// What it checks:
//   1. every write lands at its own address with its own data
//   2. every write is acknowledged, to the source that asked, with its txn id
//   3. no WR_DATA is ever orphaned (mag prints an ERROR if one is)
//
// The RAM is given a deliberately slow write path. With a bus that acks in one
// cycle the window between "issued" and "acknowledged" barely exists, and the
// bug that motivated this bench cannot be hit at all.

`default_nettype none
`timescale 1ns/1ps

module mag_wslot_tb;

    localparam integer FW  = 288;
    localparam integer PW  = 4;
    localparam integer DW  = 256;
    localparam integer AW  = 34;

    localparam [3:0] T_MEM_RD_REQ  = 4'h0,
                     T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2,
                     T_MEM_WR_ACK  = 4'h3,
                     T_MEM_WR_DATA = 4'h4;

    // The two sources. These are the coordinates of the two cluster managers
    // in the driver bench, so the traffic pattern matches the real one.
    localparam integer AX = 1, AY = 1;
    localparam integer BX = 2, BY = 1;

    reg clk = 0, rstn = 0;
    always #2 clk = ~clk;

    // ------------------------------------------------------------ the DUT
    reg  [FW-1:0] in_data;
    reg           in_valid;
    wire          in_busy;
    wire [FW-1:0] out_data;
    wire          out_valid;

    // One AXI channel per MAG memory port, plus one for the host upload.
    localparam integer MEMP = 1;
    localparam integer NCH  = MEMP + 2;   // ports, upload, mover

    wire [NCH*4-1:0]    m_awid, m_arid, m_bid, m_rid;
    wire [NCH*AW-1:0]   m_awaddr, m_araddr;
    wire [NCH*8-1:0]    m_awlen, m_arlen;
    wire [NCH-1:0]      m_awvalid, m_awready, m_arvalid, m_arready;
    wire [NCH*DW-1:0]   m_wdata, m_rdata;
    wire [NCH*DW/8-1:0] m_wstrb;
    wire [NCH-1:0]      m_wlast, m_wvalid, m_wready;
    wire [NCH*2-1:0]    m_bresp, m_rresp;
    wire [NCH-1:0]      m_bvalid, m_bready, m_rlast, m_rvalid, m_rready;

    mag #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .DATA_W(DW), .ADDR_W(AW), .ID_W(4),
          .MEM_X(0), .MEM_Y(1),
          .GRID_LO(1), .GRID_HI(2), .STAGE_FLITS(128)) dut (
        .clk(clk), .resetn(rstn),
        // memory-window AXI slave: unused here, the host uploads nothing
        .sm_awid(4'd0), .sm_awaddr({AW{1'b0}}), .sm_awlen(8'd0),
        .sm_awvalid(1'b0), .sm_awready(), .sm_wdata({DW{1'b0}}),
        .sm_wstrb({(DW/8){1'b0}}), .sm_wlast(1'b0), .sm_wvalid(1'b0),
        .sm_wready(), .sm_bid(), .sm_bresp(), .sm_bvalid(), .sm_bready(1'b1),
        .sm_arid(4'd0), .sm_araddr({AW{1'b0}}), .sm_arlen(8'd0),
        .sm_arvalid(1'b0), .sm_arready(), .sm_rid(), .sm_rdata(), .sm_rresp(),
        .sm_rlast(), .sm_rvalid(), .sm_rready(1'b1),
        // control AXI slave: unused, no dispatch in this bench
        .sc_awaddr(32'd0), .sc_awvalid(1'b0), .sc_awready(),
        .sc_wdata(64'd0), .sc_wstrb(8'hFF), .sc_wvalid(1'b0), .sc_wready(),
        .sc_bresp(), .sc_bvalid(), .sc_bready(1'b1),
        .sc_araddr(32'd0), .sc_arvalid(1'b0), .sc_arready(),
        .sc_rdata(), .sc_rresp(), .sc_rvalid(), .sc_rready(1'b1),
        // AXI master to the RAM
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen),
        .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(m_bid), .m_bresp(m_bresp), .m_bvalid(m_bvalid),
        .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen),
        .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(m_rid), .m_rdata(m_rdata), .m_rresp(m_rresp),
        .m_rlast(m_rlast), .m_rvalid(m_rvalid), .m_rready(m_rready),
        // NoC memory port -- the whole point of this bench
        .mem_in_data(in_data), .mem_in_valid(in_valid), .mem_in_busy(in_busy),
        .mem_out_data(out_data), .mem_out_valid(out_valid),
        .mem_out_busy(1'b0),
        // NoC control port: tie off, nothing dispatches here
        // No agent port to tie off: it shares the memory ports now.
        .mem_rd_count(), .mem_wr_count(),
        .mv_busy(), .mv_fault(), .mv_done()
    );

    // ---- write-response delay -------------------------------------------
    // The slot bug lives entirely in the window between "issued to AXI" and
    // "acknowledged". A RAM that answers in a cycle leaves almost no window,
    // so the bench passes on buggy RTL -- which is exactly what it did before
    // this shim existed. Real memory is not that prompt: an ack crosses an
    // interconnect and a controller. B_DELAY models that, and it is what makes
    // the window wide enough for another source's traffic to land inside it.
    localparam integer B_DELAY = 12;

    wire [NCH-1:0]   ram_bvalid;
    wire [NCH*4-1:0] ram_bid;
    wire [NCH*2-1:0] ram_bresp;

    // One delay line per channel: the ports have independent B streams now, so
    // a single shared pipe would hand one port's response to another.
    genvar gb;
    generate
    for (gb = 0; gb < NCH; gb = gb + 1) begin : g_bdly
        reg [B_DELAY:0] bpipe;
        reg [3:0]       bid_pipe [0:B_DELAY];
        integer bp;
        always @(posedge clk) begin
            if (!rstn) begin
                bpipe <= 0;
            end else begin
                bpipe <= {bpipe[B_DELAY-1:0], ram_bvalid[gb]};
                bid_pipe[0] <= ram_bid[gb*4 +: 4];
                for (bp = 1; bp <= B_DELAY; bp = bp + 1)
                    bid_pipe[bp] <= bid_pipe[bp-1];
            end
        end
        assign m_bvalid[gb]       = bpipe[B_DELAY];
        assign m_bid[gb*4 +: 4]   = bid_pipe[B_DELAY];
        assign m_bresp[gb*2 +: 2] = 2'b00;
    end
    endgenerate

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(4), .WORDS(4096),
              .PORTS(NCH)) u_ram (
        .clk(clk), .resetn(rstn),
        .s_awid(m_awid), .s_awaddr(m_awaddr), .s_awlen(m_awlen),
        .s_awvalid(m_awvalid), .s_awready(m_awready),
        .s_wdata(m_wdata), .s_wstrb(m_wstrb), .s_wlast(m_wlast),
        .s_wvalid(m_wvalid), .s_wready(m_wready),
        .s_bid(ram_bid), .s_bresp(ram_bresp), .s_bvalid(ram_bvalid),
        .s_bready({NCH{1'b1}}),
        .s_arid(m_arid), .s_araddr(m_araddr), .s_arlen(m_arlen),
        .s_arvalid(m_arvalid), .s_arready(m_arready),
        .s_rid(m_rid), .s_rdata(m_rdata), .s_rresp(m_rresp),
        .s_rlast(m_rlast), .s_rvalid(m_rvalid), .s_rready(m_rready),
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DW{1'b0}}), .bd_rdata()
    );

    // ------------------------------------------------------- flit builders
    function [FW-1:0] hdr;
        input [3:0] ty;
        input integer sx, sy;
        input [7:0]   txn;
        begin
            hdr = {FW{1'b0}};
            // dst is MAG's memory endpoint (0,1); src is the cluster
            hdr[FW-1               -: PW] = 4'd0;
            hdr[FW-PW-1            -: PW] = 4'd1;
            hdr[FW-2*PW-1          -: PW] = sx[PW-1:0];
            hdr[FW-3*PW-1          -: PW] = sy[PW-1:0];
            hdr[FW-4*PW-1          -: 4]  = ty;
            hdr[FW-4*PW-5          -: 8]  = txn;
        end
    endfunction

    function [FW-1:0] wr_req;
        input integer sx, sy;
        input [7:0]   txn;
        input [33:0]  addr;
        begin
            wr_req = hdr(T_MEM_WR_REQ, sx, sy, txn);
            wr_req[255 -: 34] = addr;
            // `len` is BEATS MINUS ONE, so one beat is 0. This said 1 and
            // meant "one beat", which was harmless only while MAG ignored the
            // field: once burst writes honoured it, every slot sat waiting for
            // a second data flit that never came, the slots filled, and intake
            // wedged. docs/isa/memory.md s2.
            wr_req[215 -: 8]  = 8'd0;
            wr_req[207 -: 8]  = 8'd0;    // no QUANT: store the payload as given
        end
    endfunction

    function [FW-1:0] wr_data;
        input integer sx, sy;
        input [7:0]   txn;
        input [DW-1:0] payload;
        begin
            wr_data = hdr(T_MEM_WR_DATA, sx, sy, txn);
            wr_data[DW-1:0] = payload;
        end
    endfunction

    task send;
        input [FW-1:0] f;
        begin
            @(posedge clk);
            while (in_busy) @(posedge clk);
            in_data  <= f;
            in_valid <= 1'b1;
            @(posedge clk);
            in_valid <= 1'b0;
        end
    endtask

    // ------------------------------------------------------- ack collection
    integer acks;
    reg [255:0] ack_seen;          // bit per txn id, both sources folded in
    integer ack_bad;

    wire [3:0] o_ty  = out_data[FW-4*PW-1 -: 4];
    wire [7:0] o_txn = out_data[FW-4*PW-5 -: 8];

    always @(posedge clk) if (rstn && out_valid && o_ty == T_MEM_WR_ACK) begin
        acks <= acks + 1;
        if (ack_seen[o_txn]) begin
            $display("  FAIL duplicate ack for txn %0d", o_txn);
            ack_bad = ack_bad + 1;
        end
        ack_seen[o_txn] <= 1'b1;
    end

    // ----------------------------------------------------------- the run
    localparam integer N = 24;     // writes per source

    integer i, errs, w;
    reg [DW-1:0] want;

    // Address plan: source A writes words 0..N-1, source B words 64..64+N-1.
    // Disjoint, so a mis-bound WR_DATA shows up as a wrong VALUE at a known
    // word rather than as two writers racing for one location.
    function [33:0] word_addr;
        input integer sx;
        input integer n;
        word_addr = ((sx == AX ? n : 64 + n) * (DW/8));
    endfunction

    function [DW-1:0] payload_of;
        input integer sx;
        input integer n;
        payload_of = {8{32'h1000_0000 + (sx << 16) + n}};
    endfunction

    initial begin
        in_valid = 0; in_data = 0; acks = 0; ack_seen = 0; ack_bad = 0; errs = 0;
        #40; repeat (5) @(posedge clk); rstn = 1; repeat (5) @(posedge clk);

        // Exactly what the hardware emits: REQ then DATA, one write open per
        // source at a time (mx_cluster_cu w_st is a three-state loop), with
        // the two sources interleaved by the mesh.
        //
        // The next write is NOT gated on the previous one retiring -- the CU
        // discards WR_ACKs (mx_cluster_cu: a_in_busy = 1'b0) -- so write i+1's
        // descriptor arrives while write i is still on the AXI bus. That
        // overlap is the entire failure window, and it is why B_DELAY matters:
        // with an instant memory the next request lands after the slot is
        // already free and nothing goes wrong.
        for (i = 0; i < N; i = i + 1) begin
            send(wr_req (AX, AY, i[7:0],          word_addr(AX, i)));
            send(wr_data(AX, AY, i[7:0],          payload_of(AX, i)));
            send(wr_req (BX, BY, 8'd128 + i[7:0], word_addr(BX, i)));
            send(wr_data(BX, BY, 8'd128 + i[7:0], payload_of(BX, i)));
        end

        // drain
        repeat (4000) @(posedge clk);

        // ---- 1. every write landed, at its own address, with its own data
        for (i = 0; i < N; i = i + 1) begin
            want = payload_of(AX, i);
            w    = word_addr(AX, i) / (DW/8);
            if (u_ram.mem[w] !== want) begin
                $display("  FAIL A[%0d] word %0d: got %h want %h",
                         i, w, u_ram.mem[w][63:0], want[63:0]);
                errs = errs + 1;
            end
            want = payload_of(BX, i);
            w    = word_addr(BX, i) / (DW/8);
            if (u_ram.mem[w] !== want) begin
                $display("  FAIL B[%0d] word %0d: got %h want %h",
                         i, w, u_ram.mem[w][63:0], want[63:0]);
                errs = errs + 1;
            end
        end

        // ---- 2. every write was acknowledged exactly once
        if (acks !== 2*N) begin
            $display("  FAIL %0d acks, expected %0d", acks, 2*N);
            errs = errs + 1;
        end
        errs = errs + ack_bad;

        if (errs == 0)
            $display("  PASS  mag write slots: %0d writes, 2 sources", 2*N);
        else
            $display("  FAIL  mag write slots: %0d errors", errs);
        $finish;
    end

    initial begin
        repeat (200000) @(posedge clk);
        $display("  FAIL  mag write slots: timed out, acks=%0d of %0d, intake wedged",
                 acks, 2*N);
        $finish;
    end

endmodule

`default_nettype wire
