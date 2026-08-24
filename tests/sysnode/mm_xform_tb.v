// The mover with the transform slot ON ITS READ-RETURN PATH: mem -> occupant
// -> mem in ONE pass, the walker feeding the occupant directly.
//
// The check is against a REFERENCE occupant fed the same beats in walker order.
// mx_quant's arithmetic is its own bench's business; what is under test here is
// that the right bytes reach it, in the right order, and that its four words
// land at the right destination.
//
// CASE 2 IS THE POINT. A source strided WITHIN an entry is what the separate
// engine could not do: it had no walker, so a strided source cost a gather pass
// into staging first. Here the walker issues the entry's eight reads wherever
// they live and the returns stream into the occupant in order.

`default_nettype none
`timescale 1ns/1ps

module mm_xform_tb;
    localparam integer DW = 256, AW = 40, IDW = 4;
    localparam integer NENT = 3;
    // Sized: a count field is 16 bits and an integer part-select is not portable.
    localparam [15:0] NENT16 = 16'd3, NSRCW16 = 16'd24;

    // Case 1: entries back to back. Case 2: 64 B between words, 512 B between
    // entries, so no two words of an entry are adjacent.
    localparam [AW-1:0] SRC1 = 40'h10_0000;
    localparam [AW-1:0] DST1 = 40'h20_0000;
    localparam [AW-1:0] SRC2 = 40'h30_0000;
    localparam [AW-1:0] DST2 = 40'h40_0000;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg         cfg_en = 0;
    reg  [7:0]  cfg_addr = 0;
    reg  [63:0] cfg_data = 0;
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

    wire        x_req, x_gnt, x_start, x_bv, x_done;
    wire [3:0]  x_id, x_mode;
    wire [DW-1:0] x_beat, x_w0, x_w1, x_w2, x_w3;

    mm_mover #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .IDX_WORDS(128),
               .XID_W(4), .XMODE_W(4),
               .XF_IN_BITS(2048), .XF_OUT_WORDS(4)) dut (
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
        .m_rvalid(rvalid), .m_rready(rready),
        .x_req(x_req), .x_gnt(x_gnt), .x_start(x_start),
        .x_id(x_id), .x_mode(x_mode),
        .x_beat(x_beat), .x_beat_valid(x_bv),
        .x_done(x_done), .x_w0(x_w0), .x_w1(x_w1), .x_w2(x_w2), .x_w3(x_w3)
    );

    mag_xform #(.DATA_W(DW), .NREQ(1), .SLOTS(1), .ID_W(4), .MODE_W(4),
                .IN_BITS(2048), .OUT_WORDS(4)) u_slot (
        .clk(clk), .rst(!resetn),
        .req(x_req), .gnt(x_gnt),
        .start(x_start), .id(x_id), .mode(x_mode),
        .beat(x_beat), .beat_valid(x_bv),
        .done(x_done), .word0(x_w0), .word1(x_w1), .word2(x_w2), .word3(x_w3)
    );

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .WORDS(200000), .PORTS(1))
    u_ram (
        .clk(clk), .resetn(resetn),
        .s_awid(awid), .s_awaddr(awaddr), .s_awlen(awlen), .s_awsize(awsize),
        .s_awburst(awburst), .s_awvalid(awvalid), .s_awready(awready),
        .s_wdata(wdata), .s_wstrb(wstrb), .s_wlast(wlast), .s_wvalid(wvalid),
        .s_wready(wready),
        .s_bid(bid), .s_bresp(bresp), .s_bvalid(bvalid), .s_bready(bready),
        .s_arid(arid), .s_araddr(araddr), .s_arlen(arlen), .s_arsize(arsize),
        .s_arburst(arburst), .s_arvalid(arvalid), .s_arready(arready),
        .s_rid(rid), .s_rdata(rdata), .s_rresp(rresp), .s_rlast(rlast),
        .s_rvalid(rvalid), .s_rready(rready),
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({DW{1'b0}}), .bd_rdata()
    );

    // ---- the reference: the same occupant, fed the same beats --------------
    reg           g_start = 0, g_bv = 0;
    reg  [DW-1:0] g_beat = 0;
    wire          g_done;
    wire [DW-1:0] g_w0, g_w1, g_w2, g_w3;
    mx_quant u_ref (
        .clk(clk), .rst(!resetn),
        .start(g_start), .b_layout(1'b0),
        .beat(g_beat), .beat_valid(g_bv),
        .need_beat(), .done(g_done),
        .word0(g_w0), .word1(g_w1), .word2(g_w2), .word3(g_w3)
    );

    reg [DW-1:0] ref_w [0:NENT*4-1];

    integer errors = 0, checks = 0, spin, i, e, b;

    // ---- AXI monitor: alignment and the 4 KB rule, live throughout ---------
    integer merr = 0, mchk = 0, n_ar = 0, n_aw = 0;
    always @(posedge clk) if (resetn) begin
        if (arvalid && arready) begin
            n_ar = n_ar + 1; mchk = mchk + 1;
            if (|araddr[4:0]) begin
                merr = merr + 1;
                $display("  ERROR AR unaligned %h", araddr);
            end
        end
        if (awvalid && awready) begin
            n_aw = n_aw + 1; mchk = mchk + 2;
            if (|awaddr[4:0]) begin
                merr = merr + 1;
                $display("  ERROR AW unaligned %h", awaddr);
            end
            if (awlen !== 8'd3) begin
                merr = merr + 1;
                $display("  ERROR a transform write must be OUT_WORDS beats, got %0d",
                         awlen + 8'd1);
            end
        end
    end

    task chk(input [255:0] got, input [255:0] want, input [8*40-1:0] what,
             input integer where);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                if (errors < 20) begin
                    $display("  FAIL %0s [%0d]: got %h want %h",
                             what, where, got[63:0], want[63:0]);
                end
            end
        end
    endtask

    task wr(input [7:0] a, input [63:0] d);
        begin
            @(negedge clk);
            cfg_en = 1'b1; cfg_addr = a; cfg_data = d;
            @(negedge clk);
            cfg_en = 1'b0;
        end
    endtask

    // The transform id and mode ride the SOURCE header's free upper bits.
    task hdrx(input sel, input [AW-1:0] base, input [2:0] ndim,
              input [3:0] xid, input [3:0] xmode);
        begin
            wr(8'h10, {5'd0, xmode, 4'd0, xid, ndim, base, 3'd0, sel});
        end
    endtask

    task dim(input sel, input [2:0] d, input [15:0] cnt,
             input signed [31:0] strd);
        begin
            wr(8'h18, {12'd0, strd, cnt, d, sel});
            wr(8'h20, 64'd0);
        end
    endtask

    task go(input [2:0] mode);
        begin
            wr(8'h00, {47'd0, 1'b1, 8'd0, 3'd0, 2'd1, mode});
            @(negedge clk);
            spin = 0;
            while (stat_busy && spin < 200000) begin
                spin = spin + 1;
                @(negedge clk);
            end
            if (spin >= 200000) begin
                errors = errors + 1;
                $display("  FAIL the mover never went idle (mode %0d)", mode);
            end
        end
    endtask

    // Run the reference over one entry's eight words, in walker order.
    task reference(input integer ent, input integer base_word,
                   input integer word_stride);
        begin
            @(negedge clk); g_start = 1'b1;
            @(negedge clk); g_start = 1'b0;
            for (b = 0; b < 8; b = b + 1) begin
                g_beat = u_ram.mem[base_word + b*word_stride];
                g_bv   = 1'b1;
                @(negedge clk);
                g_bv   = 1'b0;
            end
            spin = 0;
            while (!g_done && spin < 200) begin
                @(negedge clk); spin = spin + 1;
            end
            chk({255'd0, (spin < 200)}, 256'd1,
                "the reference occupant finished", ent);
            ref_w[ent*4+0] = g_w0; ref_w[ent*4+1] = g_w1;
            ref_w[ent*4+2] = g_w2; ref_w[ent*4+3] = g_w3;
        end
    endtask

    // SIZED: an integer inside a replication contributes 32 bits, so {16{...}}
    // of an unsized expression builds 8 copies of a 32-bit word, not 16 of a
    // 16-bit one -- and the source then differs from what the check expects.
    reg [15:0] sv;
    initial begin
        // A recognisable FP16 source for both layouts.
        for (e = 0; e < NENT; e = e + 1) begin
            for (b = 0; b < 8; b = b + 1) begin
                sv = 16'h3C00 + e[15:0] * 16'd8 + b[15:0];
                u_ram.mem[(SRC1 >> 5) + e*8 + b]      = {16{sv}};
                u_ram.mem[(SRC2 >> 5) + e*16 + b*2]   = {16{sv}};
            end
        end
        for (i = 0; i < NENT*4; i = i + 1) begin
            u_ram.mem[(DST1 >> 5) + i] = {8{32'hA5A5_A5A5}};
            u_ram.mem[(DST2 >> 5) + i] = {8{32'hA5A5_A5A5}};
        end

        repeat (20) @(negedge clk);
        resetn = 1'b1;
        repeat (10) @(negedge clk);

        // ============ 1. a CONTIGUOUS source ============
        $display("--- 1. contiguous source, one pass ---");
        for (e = 0; e < NENT; e = e + 1) begin
            reference(e, (SRC1 >> 5) + e*8, 1);
        end

        hdrx(1'b0, SRC1, 3'd1, 4'd1, 4'd0);
        dim(1'b0, 3'd0, NSRCW16, 32'sd32);
        hdrx(1'b1, DST1, 3'd1, 4'd0, 4'd0);
        dim(1'b1, 3'd0, NENT16, 32'sd128);
        go(3'd5);

        chk({252'd0, stat_fault}, 256'd0, "no fault", 1);
        for (i = 0; i < NENT*4; i = i + 1) begin
            chk(u_ram.mem[(DST1 >> 5) + i], ref_w[i],
                "contiguous destination word", i);
        end
        chk(u_ram.mem[SRC1 >> 5], {16{16'h3C00}}, "the source is untouched", 0);

        // ============ 2. a STRIDED source, no gather pass ============
        $display("--- 2. strided source: 64 B between words ---");
        for (e = 0; e < NENT; e = e + 1) begin
            reference(e, (SRC2 >> 5) + e*16, 2);
        end

        hdrx(1'b0, SRC2, 3'd2, 4'd1, 4'd0);
        dim(1'b0, 3'd0, NENT16, 32'sd512);          // outer: the entry
        dim(1'b0, 3'd1, 16'd8,  32'sd64);           // inner: the word
        hdrx(1'b1, DST2, 3'd1, 4'd0, 4'd0);
        dim(1'b1, 3'd0, NENT16, 32'sd128);
        go(3'd5);

        chk({252'd0, stat_fault}, 256'd0, "no fault on a strided source", 2);
        for (i = 0; i < NENT*4; i = i + 1) begin
            chk(u_ram.mem[(DST2 >> 5) + i], ref_w[i],
                "strided destination word", i);
        end

        // Every entry of both moves reported.
        chk({224'd0, stat_done}, {224'd0, 32'd2}, "both moves completed", 0);

        checks = checks + mchk;
        errors = errors + merr;
        $display("  %0d AR and %0d AW bursts checked", n_ar, n_aw);

        if (errors == 0) begin
            $display("PASS mm_xform_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL mm_xform_tb: %0d errors, %0d checks", errors, checks);
        end
        $finish;
    end

    initial begin
        #4000000;
        $display("FAIL mm_xform_tb: watchdog  busy=%b fault=%0d done=%0d",
                 stat_busy, stat_fault, stat_done);
        $finish;
    end
endmodule

`default_nettype wire
