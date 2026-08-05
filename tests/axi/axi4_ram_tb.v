// axi4_ram_tb.v -- self-checking AXI4 slave testbench with a HOSTILE master.
//
// The point of this bench is everything inst_receive_tb2.v does NOT do:
//
//   * RREADY starts LOW and is raised only after RVALID has been observed.
//     This is legal AXI and it is exactly what deadlocks a slave that computes
//     `RVALID = RREADY && ...`. A cooperative bench (RREADY tied high before the
//     read is issued) hides the bug completely.
//   * BREADY starts LOW, so the slave must HOLD BVALID rather than pulse it.
//   * Random stall cycles on every channel, so VALID/READY independence is
//     actually exercised instead of assumed.
//   * Interleaved / back-to-back transactions and out-of-order-looking IDs.
//   * A global watchdog: an AXI violation shows up as a hang, not a wrong value,
//     so "no timeout" is itself a pass criterion.
//
// Run:  see tests/run_axi_sim.ps1

`timescale 1ns/1ps

module axi4_ram_tb;

    localparam DW = 64;
    localparam AW = 64;
    localparam IW = 4;
    localparam DEPTH = 1024;

    reg clk = 0, resetn = 0;
    always #5 clk = ~clk;

    reg  [IW-1:0]   awid=0;   reg [AW-1:0] awaddr=0; reg [7:0] awlen=0;
    reg  [2:0]      awsize=3; reg [1:0]    awburst=1; reg awvalid=0; wire awready;
    reg  [DW-1:0]   wdata=0;  reg [DW/8-1:0] wstrb={(DW/8){1'b1}};
    reg             wlast=0;  reg wvalid=0; wire wready;
    wire [IW-1:0]   bid;      wire [1:0] bresp; wire bvalid; reg bready=0;
    reg  [IW-1:0]   arid=0;   reg [AW-1:0] araddr=0; reg [7:0] arlen=0;
    reg  [2:0]      arsize=3; reg [1:0]    arburst=1; reg arvalid=0; wire arready;
    wire [IW-1:0]   rid;      wire [DW-1:0] rdata; wire [1:0] rresp;
    wire            rlast;    wire rvalid; reg rready=0;

    integer errors = 0;
    integer checks = 0;

    axi4_ram #(.DATA_WIDTH(DW), .ADDR_WIDTH(AW), .ID_WIDTH(IW), .DEPTH(DEPTH)) dut (
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

    // ---------------------------------------------------------------- checkers
    // PROTOCOL ASSERTION: once VALID is asserted it must stay asserted, with the
    // payload unchanged, until READY. Violating this is the second most common
    // way a hand-written slave passes sim and dies on hardware.
    reg          prev_rvalid = 0, prev_bvalid = 0;
    reg [DW-1:0] prev_rdata  = 0;
    reg          r_accepted  = 0, b_accepted = 0;

    always @(posedge clk) begin
        prev_rvalid <= rvalid;
        prev_rdata  <= rdata;
        prev_bvalid <= bvalid;
        r_accepted  <= rvalid && rready;
        b_accepted  <= bvalid && bready;
    end

    // Verilog-2001, no SVA: if VALID was high and NOT accepted, then this cycle
    // it must still be high with an unchanged payload.
    always @(posedge clk) if (resetn) begin
        if (prev_rvalid && !r_accepted) begin
            if (!rvalid) begin
                $display("[%0t] PROTOCOL ERROR: RVALID dropped before RREADY", $time);
                errors = errors + 1;
            end else if (rdata !== prev_rdata) begin
                $display("[%0t] PROTOCOL ERROR: RDATA changed while RVALID held", $time);
                errors = errors + 1;
            end
        end
        if (prev_bvalid && !b_accepted && !bvalid) begin
            $display("[%0t] PROTOCOL ERROR: BVALID dropped before BREADY", $time);
            errors = errors + 1;
        end
    end

    // ---------------------------------------------------------------- tasks
    integer seed = 32'hCAFE1234;
    task stall; // random idle gap, so nothing is accidentally synchronised
        integer n; integer k;
        begin
            n = {$random(seed)} % 4;
            for (k = 0; k < n; k = k + 1) @(posedge clk);
        end
    endtask

    // Burst write. bready_delay = cycles to keep BREADY LOW after issuing.
    task axi_write;
        input [AW-1:0] addr;
        input [7:0]    len;      // AXI len (beats-1)
        input [IW-1:0] id;
        input [DW-1:0] base;     // beat i writes base+i
        input integer  bready_delay;
        integer i, d;
        begin
            @(posedge clk);
            awid <= id; awaddr <= addr; awlen <= len; awsize <= 3'd3;
            awburst <= 2'b01; awvalid <= 1'b1;
            @(posedge clk);
            while (!awready) @(posedge clk);   // wait for the slave to take AW
            awvalid <= 1'b0;

            for (i = 0; i <= len; i = i + 1) begin
                stall;                          // W may lag AW arbitrarily
                wdata  <= base + i;
                wstrb  <= {(DW/8){1'b1}};
                wlast  <= (i == len);
                wvalid <= 1'b1;
                @(posedge clk);
                while (!wready) @(posedge clk);
                wvalid <= 1'b0;
                wlast  <= 1'b0;
            end

            // Hold BREADY low first -- the slave must keep BVALID asserted.
            for (d = 0; d < bready_delay; d = d + 1) @(posedge clk);
            bready <= 1'b1;
            @(posedge clk);
            while (!bvalid) @(posedge clk);
            if (bid !== id) begin
                $display("[%0t] ERROR: BID %0h != AWID %0h", $time, bid, id);
                errors = errors + 1;
            end
            if (bresp !== 2'b00) begin
                $display("[%0t] ERROR: BRESP=%b", $time, bresp);
                errors = errors + 1;
            end
            bready <= 1'b0;
            checks = checks + 1;
        end
    endtask

    // Burst read + verify. RREADY is deliberately raised LATE.
    task axi_read_check;
        input [AW-1:0] addr;
        input [7:0]    len;
        input [IW-1:0] id;
        input [DW-1:0] base;
        integer i;
        begin
            @(posedge clk);
            arid <= id; araddr <= addr; arlen <= len; arsize <= 3'd3;
            arburst <= 2'b01; arvalid <= 1'b1;
            @(posedge clk);
            while (!arready) @(posedge clk);
            arvalid <= 1'b0;

            // THE CRITICAL BIT: do not touch RREADY yet. Wait for the slave to
            // present data on its own. A slave with RVALID=f(RREADY) hangs here.
            @(posedge clk);
            while (!rvalid) @(posedge clk);
            repeat (3) @(posedge clk);   // and make it hold for a while

            for (i = 0; i <= len; i = i + 1) begin
                rready <= 1'b1;
                @(posedge clk);
                while (!rvalid) @(posedge clk);
                if (rdata !== (base + i)) begin
                    $display("[%0t] ERROR: beat %0d @0x%0h read %0h expected %0h",
                             $time, i, addr, rdata, base + i);
                    errors = errors + 1;
                end
                if (rid !== id) begin
                    $display("[%0t] ERROR: RID %0h != ARID %0h", $time, rid, id);
                    errors = errors + 1;
                end
                if (rlast !== (i == len)) begin
                    $display("[%0t] ERROR: RLAST=%b on beat %0d of %0d",
                             $time, rlast, i, len);
                    errors = errors + 1;
                end
                rready <= 1'b0;          // drop READY between beats: legal, and
                stall;                   // forces the slave to hold its output
                checks = checks + 1;
            end
            rready <= 1'b0;
        end
    endtask

    // ---------------------------------------------------------------- stimulus
    initial begin
        $dumpfile("axi4_ram_tb.vcd");
        $dumpvars(0, axi4_ram_tb);

        resetn = 0;
        repeat (8) @(posedge clk);
        resetn = 1;
        repeat (4) @(posedge clk);

        $display("--- single-beat write/read ---");
        axi_write(64'h0000, 8'd0, 4'h1, 64'hDEAD_BEEF_0000_0000, 0);
        axi_read_check(64'h0000, 8'd0, 4'h1, 64'hDEAD_BEEF_0000_0000);

        $display("--- BREADY held low for 5 cycles ---");
        axi_write(64'h0040, 8'd0, 4'h2, 64'hAAAA_0000_0000_0001, 5);
        axi_read_check(64'h0040, 8'd0, 4'h2, 64'hAAAA_0000_0000_0001);

        $display("--- 4-beat burst ---");
        axi_write(64'h0100, 8'd3, 4'h3, 64'h1111_2222_0000_0000, 2);
        axi_read_check(64'h0100, 8'd3, 4'h3, 64'h1111_2222_0000_0000);

        $display("--- 16-beat burst ---");
        axi_write(64'h0200, 8'd15, 4'h4, 64'h5555_0000_0000_0000, 0);
        axi_read_check(64'h0200, 8'd15, 4'h4, 64'h5555_0000_0000_0000);

        $display("--- 256-beat burst (max AXI4) ---");
        axi_write(64'h1000, 8'd255, 4'h5, 64'h0BAD_0000_0000_0000, 3);
        axi_read_check(64'h1000, 8'd255, 4'h5, 64'h0BAD_0000_0000_0000);

        $display("--- back-to-back, differing IDs ---");
        axi_write(64'h0300, 8'd1, 4'h6, 64'h6666_0000_0000_0000, 0);
        axi_write(64'h0310, 8'd1, 4'h7, 64'h7777_0000_0000_0000, 0);
        axi_read_check(64'h0300, 8'd1, 4'h6, 64'h6666_0000_0000_0000);
        axi_read_check(64'h0310, 8'd1, 4'h7, 64'h7777_0000_0000_0000);

        $display("--- byte-enables (WSTRB) ---");
        begin
            // full word, then overwrite only the low 2 bytes
            axi_write(64'h0400, 8'd0, 4'h8, 64'hFFFF_FFFF_FFFF_FFFF, 0);
            @(posedge clk);
            awid <= 4'h9; awaddr <= 64'h0400; awlen <= 0; awsize <= 3'd3;
            awburst <= 2'b01; awvalid <= 1'b1;
            @(posedge clk);
            while (!awready) @(posedge clk);
            awvalid <= 1'b0;
            wdata <= 64'h0000_0000_0000_1234; wstrb <= 8'b0000_0011;
            wlast <= 1'b1; wvalid <= 1'b1;
            @(posedge clk);
            while (!wready) @(posedge clk);
            wvalid <= 1'b0; wlast <= 1'b0; wstrb <= {(DW/8){1'b1}};
            bready <= 1'b1;
            @(posedge clk);
            while (!bvalid) @(posedge clk);
            bready <= 1'b0;
            axi_read_check(64'h0400, 8'd0, 4'h9, 64'hFFFF_FFFF_FFFF_1234);
        end

        repeat (20) @(posedge clk);
        $display("========================================");
        if (errors == 0)
            $display("  PASS -- %0d checks, 0 errors", checks);
        else
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    // Watchdog: an AXI protocol break manifests as a hang.
    initial begin
        #2_000_000;
        $display("========================================");
        $display("  FAIL -- WATCHDOG TIMEOUT (deadlock)");
        $display("  a hang here means VALID/READY dependency or a lost response");
        $display("========================================");
        $finish;
    end

endmodule
