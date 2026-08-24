// rv_mag_req alone: does a 32-bit core reach a 40-bit address space correctly,
// and does the write count survive flush-all's window?

`timescale 1ns / 1ps
`default_nettype none

module rv_mag_req_tb;
    localparam integer ADDR_W = 40, DATA_W = 256;

    integer errors = 0, checks = 0, spin;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg          fill_valid = 0;
    reg  [30:0]  fill_addr = 0;
    wire         fill_ready;
    wire         resp_valid;
    wire [DATA_W-1:0] resp_data;

    reg          wb_valid = 0;
    reg  [30:0]  wb_addr = 0;
    reg  [DATA_W-1:0] wb_data = 0;
    wire         wb_ready;

    reg          seg_we = 0;
    reg  [1:0]   seg_idx = 0;
    reg  [8:0]   seg_val = 0;

    wire [ADDR_W-1:0]   cp_awaddr, cp_araddr;
    wire [7:0]          cp_awlen, cp_arlen;
    wire                cp_awvalid, cp_wvalid, cp_wlast, cp_arvalid;
    wire [DATA_W-1:0]   cp_wdata;
    wire [DATA_W/8-1:0] cp_wstrb;
    wire                cp_bready, cp_rready;
    wire [15:0]         wr_out;
    wire                idle;

    reg cp_awready = 1, cp_wready = 1, cp_arready = 1;
    reg cp_bvalid = 0, cp_rvalid = 0, cp_rlast = 0;
    reg [DATA_W-1:0] cp_rdata = 0;

    rv_mag_req #(.ADDR_W(ADDR_W), .DATA_W(DATA_W)) dut (
        .clk(clk), .resetn(resetn),
        .fill_valid(fill_valid), .fill_ready(fill_ready), .fill_addr(fill_addr),
        .resp_valid(resp_valid), .resp_data(resp_data),
        .wb_valid(wb_valid), .wb_ready(wb_ready), .wb_addr(wb_addr),
        .wb_data(wb_data),
        .seg_we(seg_we), .seg_idx(seg_idx), .seg_val(seg_val),
        .cp_awaddr(cp_awaddr), .cp_awlen(cp_awlen), .cp_awvalid(cp_awvalid),
        .cp_awready(cp_awready),
        .cp_wdata(cp_wdata), .cp_wstrb(cp_wstrb), .cp_wlast(cp_wlast),
        .cp_wvalid(cp_wvalid), .cp_wready(cp_wready),
        .cp_bvalid(cp_bvalid), .cp_bready(cp_bready),
        .cp_araddr(cp_araddr), .cp_arlen(cp_arlen), .cp_arvalid(cp_arvalid),
        .cp_arready(cp_arready),
        .cp_rdata(cp_rdata), .cp_rlast(cp_rlast), .cp_rvalid(cp_rvalid),
        .cp_rready(cp_rready),
        .wr_out(wr_out), .idle(idle)
    );

    reg [ADDR_W-1:0] saw_ar, saw_aw;
    always @(posedge clk) if (resetn) begin
        if (cp_arvalid && cp_arready) begin
            saw_ar <= cp_araddr;
        end
        if (cp_awvalid && cp_awready) begin
            saw_aw <= cp_awaddr;
        end
    end

    task chk(input cond, input [8*44-1:0] what, input [63:0] got,
             input [63:0] want);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                $display("  FAIL %0s: got %h want %h", what, got, want);
            end
        end
    endtask

    task setseg(input [1:0] ix, input [8:0] v);
        begin
            @(negedge clk); seg_we = 1; seg_idx = ix; seg_val = v;
            @(negedge clk); seg_we = 0;
        end
    endtask

    task do_fill(input [30:0] a, input [DATA_W-1:0] d);
        begin
            @(negedge clk); fill_addr = a; fill_valid = 1;
            spin = 0;
            while (spin < 200) begin
                @(posedge clk);
                if (fill_ready) begin
                    spin = 900;
                end
                else begin
                    spin = spin + 1;
                end
            end
            @(negedge clk); fill_valid = 0;
            spin = 0;
            while (!cp_arvalid && spin < 200) begin @(negedge clk); spin = spin + 1; end
            @(negedge clk);
            cp_rdata = d; cp_rlast = 1; cp_rvalid = 1;
            @(negedge clk); cp_rvalid = 0; cp_rlast = 0;
            @(negedge clk);
        end
    endtask

    task do_wb(input [30:0] a, input [DATA_W-1:0] d);
        begin
            @(negedge clk); wb_addr = a; wb_data = d; wb_valid = 1;
            spin = 0;
            while (spin < 200) begin
                @(posedge clk);
                if (wb_ready) begin
                    spin = 900;
                end
                else begin
                    spin = spin + 1;
                end
            end
            // AW is raised the cycle AFTER the wb handshake, so the capture
            // needs an edge before it can be read.
            @(negedge clk); wb_valid = 0;
            repeat (3) @(negedge clk);
        end
    endtask

    initial begin
        repeat (10) @(negedge clk);
        resetn = 1;
        repeat (5) @(negedge clk);

        chk(idle === 1'b1, "idle at rest", {63'd0, idle}, 1);

        $display("--- segment 0 = 0: a plain DRAM address passes through ---");
        do_fill(31'h0010_0000, {8{32'hAAAA_0000}});
        chk(saw_ar === 40'h00_0010_0000, "ar phys, seg 0", saw_ar, 40'h00_0010_0000);
        chk(resp_data === {8{32'hAAAA_0000}}, "fill data returned",
            resp_data[63:0], {2{32'hAAAA_0000}});

        // seg[39:31]: special=1, rsvd=0, mesh=0, aperture=0 -> MAG L2 staging,
        // which mag_stage_port.v:87 claims by exactly these bits.
        $display("--- segment 1 = staging aperture ---");
        setseg(2'd1, 9'b1_0_00_0000_0);
        do_fill(31'h2000_0040, {8{32'hBBBB_0000}});
        chk(saw_ar[39] === 1'b1, "staging: a[39] set", {63'd0, saw_ar[39]}, 1);
        chk(saw_ar[38] === 1'b0, "staging: a[38] clear", {63'd0, saw_ar[38]}, 0);
        chk(saw_ar[37:36] === 2'd0, "staging: mesh 0", {62'd0, saw_ar[37:36]}, 0);
        chk(saw_ar[35:32] === 4'd0, "staging: aperture 0",
            {60'd0, saw_ar[35:32]}, 0);
        chk(saw_ar[30:0] === 31'h2000_0040, "staging: offset kept",
            {33'd0, saw_ar[30:0]}, {33'd0, 31'h2000_0040});

        $display("--- segment 2 = another mesh ---");
        setseg(2'd2, 9'b0_0_10_0000_0);
        do_fill(31'h4000_0020, {8{32'hCCCC_0000}});
        chk(saw_ar[37:36] === 2'd2, "mesh bits", {62'd0, saw_ar[37:36]}, 2);

        $display("--- writeback: one burst, all strobes, counted ---");
        setseg(2'd0, 9'd0);
        chk(wr_out == 16'd0, "wr_out clear", {48'd0, wr_out}, 0);
        do_wb(31'h0020_0000, {8{32'hDDDD_0000}});
        chk(saw_aw === 40'h00_0020_0000, "aw phys", saw_aw, 40'h00_0020_0000);
        chk(cp_awlen === 8'd0, "one beat", {56'd0, cp_awlen}, 0);
        // Counted at ACCEPT: flush-all reads this to know a line is still owed,
        // and a count that waits for B leaves a window where it reads zero.
        chk(wr_out == 16'd1, "wr_out counted at accept", {48'd0, wr_out}, 1);
        chk(idle === 1'b0, "not idle with a write owed", {63'd0, idle}, 0);

        @(negedge clk); cp_bvalid = 1;
        @(negedge clk); cp_bvalid = 0;
        repeat (3) @(negedge clk);
        chk(wr_out == 16'd0, "wr_out retired on B", {48'd0, wr_out}, 0);
        chk(idle === 1'b1, "idle again", {63'd0, idle}, 1);

        if (errors == 0) begin
            $display("PASS rv_mag_req_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL rv_mag_req_tb: %0d errors, %0d checks", errors, checks);
        end
        $finish;
    end

    initial begin
        #100000;
        $display("FAIL rv_mag_req_tb: watchdog");
        $finish;
    end
endmodule

`default_nettype wire
