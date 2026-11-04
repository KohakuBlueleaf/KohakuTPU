// The mover behind the skids `mag.v` puts on its AW and W at IL_MVSKID=1 (v9t5
// and later images), against an AXI RAM. A skid's input ready is a register,
// so the first beat of a write burst chained off the previous burst's last can
// leave the mover the next edge, and the data FIFO's head must already be the
// next word then: XPM fwft shows a word written into an emptied FIFO only two
// edges after the write.
//
// Copies of contiguous source words to a strided destination make every write
// a one-beat burst chained off the one before. The silicon flaw (ISSUES 48) is
// asserted as recorded: R spaced 3 cycles shifts the destination by one word,
// every other spacing is exact. A shim without fwft latency fails here.

`default_nettype none
`timescale 1ns/1ps

module mm_mover_wskid_tb;
    localparam DW = 256, AW = 40, IDW = 4;
    localparam [AW-1:0] SRC = 40'h00000;
    localparam [AW-1:0] DST = 40'h04000;
    localparam integer  N   = 96;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg         cfg_en;
    reg  [7:0]  cfg_addr;
    reg  [63:0] cfg_data;
    wire        stat_busy;
    wire [3:0]  stat_fault;
    wire [31:0] stat_done;

    wire [IDW-1:0] awid, arid, bid, rid;
    wire [AW-1:0]  awaddr, araddr;
    wire [7:0]     awlen, arlen;
    wire [2:0]     awsize, arsize;
    wire [1:0]     awburst, arburst, bresp, rresp;
    wire           awvalid, awready, arvalid, arready;
    wire [DW-1:0]  wdata, rdata;
    wire [DW/8-1:0] wstrb;
    wire           wlast, wvalid, wready, bvalid, bready, rlast, rvalid, rready;
    wire           m_rvalid, m_rready;

    mm_mover #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .IDX_WORDS(128)) dut (
        .clk(clk), .resetn(resetn),
        .cfg_en(cfg_en), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
        .stat_busy(stat_busy), .stat_fault(stat_fault), .stat_done(stat_done),
        .m_awid(awid), .m_awaddr(awaddr), .m_awlen(awlen), .m_awsize(awsize),
        .m_awburst(awburst), .m_awvalid(awvalid), .m_awready(awready),
        .m_wdata(wdata), .m_wstrb(wstrb), .m_wlast(wlast), .m_wvalid(wvalid),
        .m_wready(wready),
        .m_bid(bid), .m_bresp(bresp), .m_bvalid(bvalid), .m_bready(bready),
        .m_arid(arid), .m_araddr(araddr), .m_arlen(arlen), .m_arsize(arsize),
        .m_arburst(arburst), .m_arvalid(arvalid), .m_arready(arready),
        .m_rid(rid), .m_rdata(rdata), .m_rresp(rresp), .m_rlast(rlast),
        .m_rvalid(m_rvalid), .m_rready(m_rready)
    );

    // mag.v g_mvskid: one skid on AW (id, addr, len, size, burst), one on W.
    localparam integer MAW = IDW + AW + 8 + 3 + 2;
    localparam integer MWW = DW + DW/8 + 1;
    wire [IDW-1:0]  s_awid;
    wire [AW-1:0]   s_awaddr;
    wire [7:0]      s_awlen;
    wire [2:0]      s_awsize;
    wire [1:0]      s_awburst;
    wire            s_awvalid, s_awready;
    wire [DW-1:0]   s_wdata;
    wire [DW/8-1:0] s_wstrb;
    wire            s_wlast, s_wvalid, s_wready;

    sb_skid #(.W(MAW)) u_awskid (
        .clk(clk), .rst(!resetn),
        .i_valid(awvalid), .i_ready(awready),
        .i_data({awid, awaddr, awlen, awsize, awburst}),
        .o_valid(s_awvalid), .o_ready(s_awready),
        .o_data({s_awid, s_awaddr, s_awlen, s_awsize, s_awburst})
    );
    sb_skid #(.W(MWW)) u_wskid (
        .clk(clk), .rst(!resetn),
        .i_valid(wvalid), .i_ready(wready),
        .i_data({wlast, wstrb, wdata}),
        .o_valid(s_wvalid), .o_ready(s_wready),
        .o_data({s_wlast, s_wstrb, s_wdata})
    );

    // R passes one cycle in `period`, at `phase`: words reach the data FIFO at
    // every spacing against the write engine's bursts.
    integer period = 1, phase = 0, cyc = 0;
    always @(posedge clk) begin
        cyc <= cyc + 1;
    end
    wire r_ok = (cyc % period) == phase;
    assign m_rvalid = rvalid && r_ok;
    assign rready   = m_rready && r_ok;

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .WORDS(4096), .PORTS(1))
    u_ram (
        .clk(clk), .resetn(resetn),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen),
        .s_awsize(s_awsize), .s_awburst(s_awburst), .s_awvalid(s_awvalid),
        .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(bid), .s_bresp(bresp), .s_bvalid(bvalid), .s_bready(bready),
        .s_arid(arid), .s_araddr(araddr), .s_arlen(arlen), .s_arsize(arsize),
        .s_arburst(arburst), .s_arvalid(arvalid), .s_arready(arready),
        .s_rid(rid), .s_rdata(rdata), .s_rresp(rresp), .s_rlast(rlast),
        .s_rvalid(rvalid), .s_rready(rready)
    );

    integer errors = 0, checks = 0, spin, i, k, gap, errs0;

    task wr(input [7:0] a, input [63:0] d);
        begin
            @(negedge clk);
            cfg_en = 1'b1; cfg_addr = a; cfg_data = d;
            @(negedge clk);
            cfg_en = 1'b0;
        end
    endtask

    task hdr(input sel, input [AW-1:0] base, input [2:0] ndim);
        begin wr(8'h10, {17'd0, ndim, base, 3'd0, sel}); end
    endtask

    task dim(input sel, input [2:0] d, input [15:0] cnt, input signed [31:0] strd);
        begin
            wr(8'h18, {12'd0, strd, cnt, d, sel});
            wr(8'h20, 64'd0);
        end
    endtask

    task go;
        begin
            wr(8'h00, {46'd0, 1'b0, 1'b1, 8'd0, 3'd0, 2'd1, 3'd0});
            @(negedge clk);
            spin = 0;
            while (stat_busy && spin < 100000) begin
                spin = spin + 1;
                @(negedge clk);
            end
            if (spin >= 100000) begin
                errors = errors + 1;
                $display("  FAIL mover never went idle");
            end
        end
    endtask

    task chk(input [255:0] got, input [255:0] want, input integer where);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                if (errors < 20 && period != 3) begin
                    $display("  FAIL [%0d]: got %h want %h", where, got[31:0], want[31:0]);
                end
            end
        end
    endtask

    initial begin
        cfg_en = 0; cfg_addr = 0; cfg_data = 0;
        for (i = 0; i < 4096; i = i + 1) begin
            u_ram.mem[i] = {8{32'hDEAD_0000}} | i;
        end
        for (i = 0; i < N; i = i + 1) begin
            u_ram.mem[(SRC >> 5) + i] = {8{32'h5000_0000 | i}};
        end
        repeat (8) @(negedge clk);
        resetn = 1;
        repeat (4) @(negedge clk);

        // N contiguous words to every GAP-th destination word, each its own
        // burst, under every R spacing.
        for (period = 1; period <= 6; period = period + 1) begin
            for (phase = 0; phase < period; phase = phase + 1) begin
                for (gap = 2; gap <= 3; gap = gap + 1) begin
                    // A word left in the FIFO shifts every later move: reset per case.
                    resetn = 0;
                    repeat (4) @(negedge clk);
                    resetn = 1;
                    repeat (4) @(negedge clk);
                    errs0 = errors;
                    for (i = 0; i < N * 3; i = i + 1) begin
                        u_ram.mem[(DST >> 5) + i] = {8{32'hDEAD_0000}} | i;
                    end
                    hdr(1'b0, SRC, 3'd1);
                    dim(1'b0, 3'd0, N[15:0], 32'sd32);
                    hdr(1'b1, DST, 3'd1);
                    dim(1'b1, 3'd0, N[15:0], 32 * gap);
                    go;
                    for (i = 0; i < N; i = i + 1) begin
                        chk(u_ram.mem[(DST >> 5) + i * gap], {8{32'h5000_0000 | i}}, i);
                        for (k = 1; k < gap; k = k + 1) begin
                            chk(u_ram.mem[(DST >> 5) + i * gap + k],
                                {8{32'hDEAD_0000}} | (i * gap + k), i * gap + k);
                        end
                    end
                    $display("--- R 1 in %0d phase %0d, destination stride %0d: %0d wrong",
                             period, phase, gap, errors - errs0);
                    // ISSUES 48: at one word every 3 cycles each chained burst writes
                    // the word before its own, so word i holds word i-1 for i >= 1.
                    if (period == 3) begin
                        checks = checks + 1;
                        if (errors - errs0 == N - 1) begin
                            errors = errs0;
                        end
                        else begin
                            errors = errs0 + 1;
                            $display("  FAIL the recorded shift is not %0d words", N - 1);
                        end
                    end
                end
            end
        end

        if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $finish;
    end

    initial begin
        #2000000;
        $display("  FAIL -- watchdog");
        $finish;
    end
endmodule

`default_nettype wire
