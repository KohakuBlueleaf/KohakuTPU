// axi4_master driving axi4_ram. Checks two separate things:
//
//   data      write a pattern, read it back, compare
//   legality  every burst the master emits is legal AXI4 -- no 4 KB crossing,
//             AxLEN <= 255, WLAST exactly on the last beat of each burst
//
// The second is the point. Data can round-trip correctly while the master emits
// illegal bursts that a simple slave happens to tolerate and a real interconnect
// does not, so the bus monitor checks the property directly rather than inferring
// it from the payload.

`timescale 1ns/1ps

module axi4_master_tb;

    localparam DW = 64;
    localparam AW = 32;
    localparam IW = 4;
    localparam BYTES = DW/8;
    localparam DEPTH = 8192;          // words in the slave

    reg clk = 0, resetn = 0;
    always #5 clk = ~clk;

    integer errors = 0, checks = 0;

    // command / data-stream side
    reg  [AW-1:0] cmd_addr = 0;
    reg  [31:0]   cmd_beats = 0;
    reg           cmd_write = 0, cmd_valid = 0;
    wire          cmd_ready, cmd_done;
    wire [1:0]    cmd_resp;

    reg  [DW-1:0] wr_data = 0;
    reg           wr_valid = 0;
    wire          wr_ready;
    wire [DW-1:0] rd_data;
    wire          rd_valid;
    reg           rd_ready = 0;

    // AXI
    wire [IW-1:0] awid, arid, bid, rid;
    wire [AW-1:0] awaddr, araddr;
    wire [7:0]    awlen, arlen;
    wire [2:0]    awsize, arsize;
    wire [1:0]    awburst, arburst, bresp, rresp;
    wire          awvalid, awready, arvalid, arready;
    wire [DW-1:0] wdata, rdata;
    wire [DW/8-1:0] wstrb;
    wire          wlast, wvalid, wready, bvalid, bready, rlast, rvalid, rready;

    axi4_master #(.DATA_WIDTH(DW), .ADDR_WIDTH(AW), .ID_WIDTH(IW)) mst (
        .clk(clk), .resetn(resetn),
        .cmd_addr(cmd_addr), .cmd_beats(cmd_beats), .cmd_write(cmd_write),
        .cmd_valid(cmd_valid), .cmd_ready(cmd_ready),
        .cmd_done(cmd_done), .cmd_resp(cmd_resp),
        .wr_data(wr_data), .wr_strb({(DW/8){1'b1}}), .wr_valid(wr_valid), .wr_ready(wr_ready),
        .rd_data(rd_data), .rd_valid(rd_valid), .rd_ready(rd_ready),
        .m_axi_awid(awid), .m_axi_awaddr(awaddr), .m_axi_awlen(awlen),
        .m_axi_awsize(awsize), .m_axi_awburst(awburst),
        .m_axi_awvalid(awvalid), .m_axi_awready(awready),
        .m_axi_wdata(wdata), .m_axi_wstrb(wstrb), .m_axi_wlast(wlast),
        .m_axi_wvalid(wvalid), .m_axi_wready(wready),
        .m_axi_bid(bid), .m_axi_bresp(bresp), .m_axi_bvalid(bvalid), .m_axi_bready(bready),
        .m_axi_arid(arid), .m_axi_araddr(araddr), .m_axi_arlen(arlen),
        .m_axi_arsize(arsize), .m_axi_arburst(arburst),
        .m_axi_arvalid(arvalid), .m_axi_arready(arready),
        .m_axi_rid(rid), .m_axi_rdata(rdata), .m_axi_rresp(rresp),
        .m_axi_rlast(rlast), .m_axi_rvalid(rvalid), .m_axi_rready(rready)
    );

    axi4_ram #(.DATA_WIDTH(DW), .ADDR_WIDTH(AW), .ID_WIDTH(IW), .DEPTH(DEPTH)) slv (
        .clk(clk), .resetn(resetn),
        .s_axi_awid(awid), .s_axi_awaddr(awaddr), .s_axi_awlen(awlen),
        .s_axi_awsize(awsize), .s_axi_awburst(awburst),
        .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(wstrb), .s_axi_wlast(wlast),
        .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bid(bid), .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_arid(arid), .s_axi_araddr(araddr), .s_axi_arlen(arlen),
        .s_axi_arsize(arsize), .s_axi_arburst(arburst),
        .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rid(rid), .s_axi_rdata(rdata), .s_axi_rresp(rresp),
        .s_axi_rlast(rlast), .s_axi_rvalid(rvalid), .s_axi_rready(rready)
    );

    // ------------------------------------------------------------ bus monitor
    integer bursts = 0;
    integer wbeat_count = 0;
    reg [8:0] expect_wbeats = 0;

    task chk(input cond, input [255:0] what);
        begin
            checks = checks + 1;
            if (!cond) begin
                $display("[%0t] LEGALITY FAIL: %0s", $time, what);
                errors = errors + 1;
            end
        end
    endtask

    always @(posedge clk) if (resetn) begin
        if (awvalid && awready) begin
            bursts = bursts + 1;
            chk(awlen <= 8'd255, "AWLEN > 255");
            // a burst of N beats starting at A must stay inside A's 4 KB page
            chk(({20'd0, awaddr[11:0]} + ((awlen + 1) << $clog2(BYTES))) <= 32'd4096,
                "write burst crosses a 4 KB boundary");
            expect_wbeats = awlen + 9'd1;
            wbeat_count   = 0;
        end
        if (wvalid && wready) begin
            wbeat_count = wbeat_count + 1;
            chk(wlast == (wbeat_count == expect_wbeats), "WLAST not on the last beat");
        end
        if (arvalid && arready) begin
            bursts = bursts + 1;
            chk(arlen <= 8'd255, "ARLEN > 255");
            chk(({20'd0, araddr[11:0]} + ((arlen + 1) << $clog2(BYTES))) <= 32'd4096,
                "read burst crosses a 4 KB boundary");
        end
    end

    // ------------------------------------------------------------------ tasks
    task do_write(input [AW-1:0] a, input [31:0] n, input [DW-1:0] base);
        integer i;
        begin
            @(posedge clk);
            cmd_addr <= a; cmd_beats <= n; cmd_write <= 1'b1; cmd_valid <= 1'b1;
            @(posedge clk); while (!cmd_ready) @(posedge clk);
            cmd_valid <= 1'b0;
            for (i = 0; i < n; i = i + 1) begin
                wr_data  <= base + i;
                wr_valid <= 1'b1;
                @(posedge clk);
                while (!wr_ready) @(posedge clk);
            end
            wr_valid <= 1'b0;
            while (!cmd_done) @(posedge clk);
            chk(cmd_resp == 2'b00, "write response not OKAY");
        end
    endtask

    task do_read_check(input [AW-1:0] a, input [31:0] n, input [DW-1:0] base);
        integer i;
        begin
            @(posedge clk);
            cmd_addr <= a; cmd_beats <= n; cmd_write <= 1'b0; cmd_valid <= 1'b1;
            @(posedge clk); while (!cmd_ready) @(posedge clk);
            cmd_valid <= 1'b0;
            for (i = 0; i < n; i = i + 1) begin
                rd_ready <= 1'b1;
                @(posedge clk);
                while (!rd_valid) @(posedge clk);
                checks = checks + 1;
                if (rd_data !== (base + i)) begin
                    $display("[%0t] DATA FAIL @0x%0h beat %0d: got %h want %h",
                             $time, a, i, rd_data, base + i);
                    errors = errors + 1;
                end
            end
            rd_ready <= 1'b0;
            while (!cmd_done) @(posedge clk);
            chk(cmd_resp == 2'b00, "read response not OKAY");
        end
    endtask

    task roundtrip(input [AW-1:0] a, input [31:0] n, input [DW-1:0] base,
                   input [255:0] name);
        integer b0;
        begin
            b0 = bursts;
            do_write(a, n, base);
            do_read_check(a, n, base);
            $display("  %-42s %4d beats, %0d bursts", name, n, bursts - b0);
        end
    endtask

    initial begin
        $dumpfile("axi4_master_tb.vcd");
        $dumpvars(0, axi4_master_tb);
        resetn = 0; repeat (10) @(posedge clk);
        resetn = 1; repeat (4)  @(posedge clk);

        $display("--- transfers ---");
        roundtrip(32'h0000,   1,   64'h1111_0000_0000_0000, "single beat");
        roundtrip(32'h0040,   4,   64'h2222_0000_0000_0000, "short burst");
        roundtrip(32'h0100,  16,   64'h3333_0000_0000_0000, "16 beats");
        roundtrip(32'h1000, 256,   64'h4444_0000_0000_0000, "256 beats, page-aligned");
        // 4 KB is 512 beats at 64-bit; starting 4 beats short forces a split
        roundtrip(32'h0FE0,  64,   64'h5555_0000_0000_0000, "crosses a 4 KB boundary");
        roundtrip(32'h2000, 700,   64'h6666_0000_0000_0000, "700 beats, multi-burst");
        roundtrip(32'h4FF8, 300,   64'h7777_0000_0000_0000, "unaligned + crossing");

        repeat (20) @(posedge clk);
        $display("");
        $display("========================================");
        $display("  %0d bursts issued, %0d checks", bursts, checks);
        if (errors == 0) $display("  PASS -- 0 errors");
        else             $display("  FAIL -- %0d errors", errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #20_000_000;
        $display("========================================");
        $display("  FAIL -- WATCHDOG TIMEOUT");
        $display("========================================");
        $finish;
    end

endmodule
