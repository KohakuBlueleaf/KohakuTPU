// The memory mover alone, against an AXI RAM. No MAG, no NoC, no mesh.
//
// The relayout test is the one that matters: a word transpose is exactly the
// shape of tile order to entry order, which is the conversion that currently
// costs a vector band and which produced a 1.26e+00 relative error with no
// exception when it was got wrong.
//
// Determinism for GENERATE is checked by SPLITTING the region, because the
// counter is the absolute word address: filling 8 words in one move and the
// same 8 words as two moves of 4 must produce identical bytes.

`default_nettype none
`timescale 1ns/1ps

module mm_mover_tb;
    localparam DW = 256, AW = 40, IDW = 4;

    localparam [AW-1:0] SRC  = 40'h00000;
    localparam [AW-1:0] DST  = 40'h01000;
    localparam [AW-1:0] PADD = 40'h02000;
    localparam [AW-1:0] GEN1 = 40'h03000;
    localparam [AW-1:0] GEN2 = 40'h04000;
    localparam [AW-1:0] TBL  = 40'h05000;
    localparam [AW-1:0] IDXB = 40'h06000;
    localparam [AW-1:0] OUT  = 40'h07000;
    // Rows of 64 words: a table of 8, a gathered block of 5, a scatter target.
    localparam [AW-1:0] BIGT = 40'h08000;
    localparam [AW-1:0] BIGO = 40'h10000;
    localparam [AW-1:0] SCD  = 40'h18000;
    localparam [AW-1:0] FIDX = 40'h06100;
    localparam [AW-1:0] SCS  = 40'h1E000;
    localparam integer  RW   = 64;
    // Twenty one-word rows: an index list three buffer words long, u32 and fp32.
    // The u32 list starts two words before a 4 KB page end: its load splits.
    localparam [AW-1:0] IDX2 = 40'h1EFC0;
    localparam [AW-1:0] FIDX2 = 40'h06300;
    localparam integer  NR   = 20;

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
        .m_rvalid(rvalid), .m_rready(rready)
    );

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .WORDS(4096), .PORTS(1))
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
        .s_rvalid(rvalid), .s_rready(rready)
    );

    integer errors = 0, checks = 0, spin;

    // ---- AXI burst monitor, live under every case below ------------------

    // A burst inside one 4 KB page is inside one mesh and one aperture, since
    // a mesh is 64 GB: the 40-bit split needs no separate check.
    integer merr = 0, mchk = 0, n_ar = 0, n_aw = 0;
    reg [3:0] ar_top, aw_top;

    always @(posedge clk) if (resetn) begin
        if (arvalid && arready) begin
            n_ar = n_ar + 1; mchk = mchk + 2; ar_top = araddr[AW-1 -: 4];
            if (|araddr[4:0]) begin
                merr = merr + 1;
                $display("  ERROR AR unaligned %h", araddr);
            end
            if (({1'b0, araddr[11:5]} + {1'b0, arlen} + 9'd1) > 9'd128) begin
                merr = merr + 1;
                $display("  ERROR AR crosses 4 KB: %h len %0d", araddr, arlen);
            end
        end
        if (awvalid && awready) begin
            n_aw = n_aw + 1; mchk = mchk + 2; aw_top = awaddr[AW-1 -: 4];
            if (|awaddr[4:0]) begin
                merr = merr + 1;
                $display("  ERROR AW unaligned %h", awaddr);
            end
            if (({1'b0, awaddr[11:5]} + {1'b0, awlen} + 9'd1) > 9'd128) begin
                merr = merr + 1;
                $display("  ERROR AW crosses 4 KB: %h len %0d", awaddr, awlen);
            end
        end
    end

    task chk(input [255:0] got, input [255:0] want, input [255:0] what,
             input integer where);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                if (errors < 40) begin
                    $display("  FAIL %0s [%0d]: got %h want %h",
                             what, where, got[63:0], want[63:0]);
                end
            end
        end
    endtask

    // ---- configuration helpers ------------------------------------------
    task wr(input [7:0] a, input [63:0] d);
        begin
            @(negedge clk);
            cfg_en = 1'b1; cfg_addr = a; cfg_data = d;
            @(negedge clk);
            cfg_en = 1'b0;
        end
    endtask

    // At 40 bits the base fills [43:4] exactly and the old six-bit filler is
    // gone: writing it now would push ndim six bits high.
    task hdr(input sel, input [AW-1:0] base, input [2:0] ndim);
        begin wr(8'h10, {17'd0, ndim, base, 3'd0, sel}); end
    endtask

    task dim(input sel, input [2:0] d, input [15:0] cnt,
             input signed [31:0] strd, input [1:0] ax, input signed [15:0] astep);
        begin
            wr(8'h18, {12'd0, strd, cnt, d, sel});
            wr(8'h20, {46'd0, astep, ax});
        end
    endtask

    task axbound(input sel, input which, input signed [15:0] ab,
                 input [15:0] ext);
        begin wr(8'h28, {30'd0, ext, ab, which, sel}); end
    endtask

    task go(input [2:0] mode, input [1:0] ew);
        begin gof(mode, ew, 1'b0); end
    endtask

    // `fp`: the index list holds fp32 values (register 0x00 bit 17). `took`
    // is the move's cycles from GO to idle.
    integer took;
    task gof(input [2:0] mode, input [1:0] ew, input fp);
        begin
            wr(8'h00, {46'd0, fp, 1'b1, 8'd0, 3'd0, ew, mode});
            @(negedge clk);
            spin = 0;
            while (stat_busy && spin < 200000) begin
                spin = spin + 1;
                @(negedge clk);
            end
            took = spin;
            if (spin >= 200000) begin
                errors = errors + 1;
                $display("  FAIL mover never went idle (mode %0d)", mode);
            end
        end
    endtask

    task want_fault(input [3:0] f, input [255:0] what);
        begin
            checks = checks + 1;
            if (stat_fault !== f) begin
                errors = errors + 1;
                $display("  FAIL %0s should fault %0d, got %0d", what, f, stat_fault);
            end
        end
    endtask

    // A non-negative integer below 2^24 as fp32.
    function [31:0] f32(input integer k);
        integer e;
        reg [31:0] m;
        begin
            f32 = 32'd0;
            if (k > 0) begin
                e = 0;
                while ((k >> (e + 1)) != 0) begin
                    e = e + 1;
                end
                m = k << (23 - e);
                f32 = {1'b0, 8'd127 + e[7:0], m[22:0]};
            end
        end
    endfunction

    function integer ix2(input integer r);
        begin ix2 = (r * 7 + 3) % NR; end
    endfunction

    integer i, j, r, w;
    reg [255:0] a, b;
    reg [255:0] gen_ref [0:7];
    integer idxv [0:4];

    initial begin
        cfg_en = 0; cfg_addr = 0; cfg_data = 0;
        for (i = 0; i < 4096; i = i + 1) begin
            u_ram.mem[i] = {8{32'hDEAD_0000}} | i;
        end

        // a recognisable source: word n holds n in every lane
        for (i = 0; i < 32; i = i + 1) begin
            u_ram.mem[(SRC >> 5) + i] = {8{i[31:0]}};
        end
        for (i = 0; i < 16; i = i + 1) begin
            u_ram.mem[(TBL >> 5) + i] = {8{32'hAA00_0000 | i}};
        end
        // five indices, packed eight to a word
        // lane 0 is the low 32 bits, so index r is word[r*32 +: 32]
        u_ram.mem[IDXB >> 5] = {32'd0, 32'd0, 32'd0,
                                32'd7, 32'd1, 32'd5, 32'd2, 32'd3};
        idxv[0] = 3; idxv[1] = 2; idxv[2] = 5; idxv[3] = 1; idxv[4] = 7;

        repeat (8) @(negedge clk);
        resetn = 1;
        repeat (4) @(negedge clk);

        // ============ 1. COPY as a word transpose: 4x8 -> 8x4 ============
        $display("--- 1. COPY, the relayout shape ---");
        hdr(1'b0, SRC, 3'd2);
        dim(1'b0, 3'd0, 16'd4, 32'sd256, 2'd0, 16'sd0);
        dim(1'b0, 3'd1, 16'd8, 32'sd32,  2'd0, 16'sd0);
        hdr(1'b1, DST, 3'd2);
        dim(1'b1, 3'd0, 16'd4, 32'sd32,  2'd0, 16'sd0);
        dim(1'b1, 3'd1, 16'd8, 32'sd128, 2'd0, 16'sd0);
        go(3'd0, 2'd1);

        for (i = 0; i < 4; i = i + 1) begin
            for (j = 0; j < 8; j = j + 1) begin
                chk(u_ram.mem[(DST >> 5) + j*4 + i],
                    u_ram.mem[(SRC >> 5) + i*8 + j], "transposed word", i*8+j);
            end
        end

        // ============ 2. padding: a bound axis clips the source ============
        $display("--- 2. padding via a bound axis ---");
        wr(8'h40, 64'h0000_0000_BEEF_BEEF);
        hdr(1'b0, SRC, 3'd1);
        dim(1'b0, 3'd0, 16'd6, 32'sd32, 2'd1, 16'sd1);
        axbound(1'b0, 1'b0, 16'sd0, 16'd4);
        hdr(1'b1, PADD, 3'd1);
        dim(1'b1, 3'd0, 16'd6, 32'sd32, 2'd0, 16'sd0);
        go(3'd0, 2'd1);

        for (i = 0; i < 6; i = i + 1) begin
            if (i < 4) begin
                b = u_ram.mem[(SRC >> 5) + i];
            end
            else begin
                b = {16{16'hBEEF}};
            end
            chk(u_ram.mem[(PADD >> 5) + i], b, "padded element", i);
        end

        // ============ 3. FILL ============
        $display("--- 3. FILL ---");
        wr(8'h40, 64'h0000_0000_0000_1234);
        hdr(1'b1, PADD, 3'd1);
        dim(1'b1, 3'd0, 16'd4, 32'sd32, 2'd0, 16'sd0);
        go(3'd4, 2'd1);
        for (i = 0; i < 4; i = i + 1) begin
            chk(u_ram.mem[(PADD >> 5) + i], {16{16'h1234}}, "filled word", i);
        end

        // ============ 4. GENERATE, and split determinism ============
        $display("--- 4. GENERATE ---");
        wr(8'h38, 64'h0123_4567_89AB_CDEF);
        hdr(1'b1, GEN1, 3'd1);
        dim(1'b1, 3'd0, 16'd8, 32'sd32, 2'd0, 16'sd0);
        go(3'd3, 2'd1);
        for (i = 0; i < 8; i = i + 1) begin
            gen_ref[i] = u_ram.mem[(GEN1 >> 5) + i];
        end

        // same eight words, as two moves of four -- the counter is absolute,
        // so the bytes must be identical
        for (i = 0; i < 8; i = i + 1) begin
            u_ram.mem[(GEN1 >> 5) + i] = 256'd0;
        end
        hdr(1'b1, GEN1, 3'd1);
        dim(1'b1, 3'd0, 16'd4, 32'sd32, 2'd0, 16'sd0);
        go(3'd3, 2'd1);
        hdr(1'b1, GEN1 + 40'd128, 3'd1);
        dim(1'b1, 3'd0, 16'd4, 32'sd32, 2'd0, 16'sd0);
        go(3'd3, 2'd1);
        for (i = 0; i < 8; i = i + 1) begin
            chk(u_ram.mem[(GEN1 >> 5) + i], gen_ref[i], "split determinism", i);
        end

        // a different address must give different bytes
        hdr(1'b1, GEN2, 3'd1);
        dim(1'b1, 3'd0, 16'd8, 32'sd32, 2'd0, 16'sd0);
        go(3'd3, 2'd1);
        checks = checks + 1;
        if (u_ram.mem[GEN2 >> 5] === gen_ref[0]) begin
            errors = errors + 1;
            $display("  FAIL a different address produced the same noise");
        end

        // ============ 5. GATHER ============
        $display("--- 5. GATHER, rows of two words ---");
        wr(8'h30, {8'd0, 16'd5, IDXB});
        wr(8'h50, {16'd0, 16'd2, 32'd64});
        hdr(1'b0, TBL, 3'd1);
        hdr(1'b1, OUT, 3'd2);
        dim(1'b1, 3'd0, 16'd5, 32'sd64, 2'd0, 16'sd0);
        dim(1'b1, 3'd1, 16'd2, 32'sd32, 2'd0, 16'sd0);
        go(3'd2, 2'd1);

        for (r = 0; r < 5; r = r + 1) begin
            for (w = 0; w < 2; w = w + 1) begin
                chk(u_ram.mem[(OUT >> 5) + r*2 + w],
                    u_ram.mem[(TBL >> 5) + idxv[r]*2 + w], "gathered word",
                    r*2 + w);
            end
        end

        // ============ 6. faults ============
        $display("--- 6. faults ---");
        hdr(1'b1, DST, 3'd1);
        dim(1'b1, 3'd0, 16'd1, 32'sd32, 2'd0, 16'sd0);
        go(3'd0, 2'd3);
        checks = checks + 1;
        if (stat_fault !== 4'd5) begin
            errors = errors + 1;
            $display("  FAIL ewidth=3 should fault 5, got %0d", stat_fault);
        end

        hdr(1'b1, DST + 40'd8, 3'd1);
        dim(1'b1, 3'd0, 16'd1, 32'sd32, 2'd0, 16'sd0);
        hdr(1'b0, SRC, 3'd1);
        dim(1'b0, 3'd0, 16'd1, 32'sd32, 2'd0, 16'sd0);
        go(3'd0, 2'd1);
        checks = checks + 1;
        if (stat_fault !== 4'd6) begin
            errors = errors + 1;
            $display("  FAIL a misaligned destination should fault 6, got %0d",
                     stat_fault);
        end

        // ============ 7. the 40-bit top four bits are carried, not masked ===

        // Aliasing a mesh or an aperture onto local DRAM is the silent failure
        // the 40-bit map exists to prevent, so check the address on the wire.
        $display("--- 7. mesh and aperture pass-through ---");
        // ar_top / aw_top have the monitor as their one driver (Verilator
        // misorders a blocking write from here against it); burst counts
        // prove the move issued.
        hdr(1'b0, SRC | (40'd1 << 39), 3'd1);           // aperture 0, mesh 0
        dim(1'b0, 3'd0, 16'd4, 32'sd32, 2'd0, 16'sd0);
        hdr(1'b1, DST | (40'd2 << 36), 3'd1);           // DRAM, mesh 2
        dim(1'b1, 3'd0, 16'd4, 32'sd32, 2'd0, 16'sd0);
        i = n_ar; j = n_aw;
        go(3'd0, 2'd1);
        checks = checks + 1;
        if ((n_ar == i) || (n_aw == j)) begin
            errors = errors + 1;
            $display("  FAIL the pass-through move issued no burst");
        end
        checks = checks + 1;
        if (ar_top !== 4'b1000) begin
            errors = errors + 1;
            $display("  FAIL AR top four should be 1000, got %b", ar_top);
        end
        checks = checks + 1;
        if (aw_top !== 4'b0010) begin
            errors = errors + 1;
            $display("  FAIL AW top four should be 0010, got %b", aw_top);
        end

        // A run spanning the top of a mesh must split, and the burst monitor
        // above is what proves it: 128 words from 64 GB minus 2 KB.
        hdr(1'b0, (40'd1 << 36) - 40'd2048, 3'd1);
        dim(1'b0, 3'd0, 16'd128, 32'sd32, 2'd0, 16'sd0);
        hdr(1'b1, (40'd1 << 36) - 40'd2048, 3'd1);
        dim(1'b1, 3'd0, 16'd128, 32'sd32, 2'd0, 16'sd0);
        go(3'd0, 2'd1);

        // ============ 8. GATHER, rows of 64 words: a row a pass ============
        // One index pipeline pass a row, then the row's words at copy rate: 5
        // rows of 64 must take well under 2 cycles a word (5 a word when every
        // word paid the pipeline).
        $display("--- 8. GATHER, rows of 64 words ---");
        for (i = 0; i < 8 * RW; i = i + 1) begin
            u_ram.mem[(BIGT >> 5) + i] = {8{32'hB000_0000 | i}};
        end
        // the same 320 words as one plain copy: the rate to match
        hdr(1'b0, BIGT, 3'd1);
        dim(1'b0, 3'd0, 5 * RW, 32'sd32, 2'd0, 16'sd0);
        hdr(1'b1, BIGO, 3'd1);
        dim(1'b1, 3'd0, 5 * RW, 32'sd32, 2'd0, 16'sd0);
        go(3'd0, 2'd1);
        j = took;
        wr(8'h30, {8'd0, 16'd5, IDXB});
        wr(8'h50, {16'd0, RW[15:0], 32'd2048});
        go(3'd2, 2'd1);
        for (r = 0; r < 5; r = r + 1) begin
            for (w = 0; w < RW; w = w + 1) begin
                chk(u_ram.mem[(BIGO >> 5) + r*RW + w],
                    u_ram.mem[(BIGT >> 5) + idxv[r]*RW + w], "long-row gather",
                    r*RW + w);
            end
        end
        // A row may cost its index pass (and a burst break) on top of the copy.
        checks = checks + 1;
        $display("  320 words: copy %0d cycles, gather %0d", j, took);
        if (took > j + 5 * 16) begin
            errors = errors + 1;
            $display("  FAIL the gather is not at copy rate: %0d vs %0d", took, j);
        end

        // ============ 9. SCATTER: the source walks, the index writes ========
        $display("--- 9. SCATTER, rows of two words, then of 64 ---");
        for (i = 0; i < 8 * 2; i = i + 1) begin
            u_ram.mem[(SCD >> 5) + i] = {8{32'h5C00_0000 | i}};
        end
        for (i = 0; i < 10; i = i + 1) begin
            u_ram.mem[(SCS >> 5) + i] = {8{32'h5500_0000 | i}};
        end
        wr(8'h30, {8'd0, 16'd5, IDXB});
        wr(8'h50, {16'd0, 16'd2, 32'd64});
        hdr(1'b0, SCS, 3'd1);
        dim(1'b0, 3'd0, 16'd10, 32'sd32, 2'd0, 16'sd0);
        hdr(1'b1, SCD, 3'd1);
        go(3'd6, 2'd1);
        want_fault(4'd0, "a scatter");
        for (r = 0; r < 5; r = r + 1) begin
            for (w = 0; w < 2; w = w + 1) begin
                chk(u_ram.mem[(SCD >> 5) + idxv[r]*2 + w],
                    u_ram.mem[(SCS >> 5) + r*2 + w], "scattered word", r*2 + w);
            end
        end
        // rows 0, 4 and 6 are named by no index: untouched
        for (w = 0; w < 2; w = w + 1) begin
            chk(u_ram.mem[(SCD >> 5) + 0*2 + w], {8{32'h5C00_0000 | (0*2 + w)}},
                "unnamed row 0", w);
            chk(u_ram.mem[(SCD >> 5) + 4*2 + w], {8{32'h5C00_0000 | (4*2 + w)}},
                "unnamed row 4", w);
            chk(u_ram.mem[(SCD >> 5) + 6*2 + w], {8{32'h5C00_0000 | (6*2 + w)}},
                "unnamed row 6", w);
        end
        // the long-row gather's output scattered back: the table rows again
        wr(8'h50, {16'd0, RW[15:0], 32'd2048});
        hdr(1'b0, BIGO, 3'd1);
        dim(1'b0, 3'd0, 5 * RW, 32'sd32, 2'd0, 16'sd0);
        hdr(1'b1, SCD, 3'd1);
        go(3'd6, 2'd1);
        for (r = 0; r < 5; r = r + 1) begin
            for (w = 0; w < RW; w = w + 1) begin
                chk(u_ram.mem[(SCD >> 5) + idxv[r]*RW + w],
                    u_ram.mem[(BIGT >> 5) + idxv[r]*RW + w], "long-row scatter",
                    r*RW + w);
            end
        end
        // a destination base off a word faults, and writes nothing
        hdr(1'b1, SCD + 40'd8, 3'd1);
        go(3'd6, 2'd1);
        want_fault(4'd6, "a misaligned scatter base");

        // ============ 10. a float index list ============
        // fp32, as a V2 core's F32 pack writes an integral lane value; -0 is 0.
        $display("--- 10. GATHER with fp32 indices ---");
        u_ram.mem[FIDX >> 5] = {32'd0, 32'd0, 32'h8000_0000, 32'h40E0_0000,
                                32'h3F80_0000, 32'h40A0_0000, 32'h4000_0000,
                                32'h4040_0000};
        wr(8'h30, {8'd0, 16'd6, FIDX});
        wr(8'h50, {16'd0, 16'd2, 32'd64});
        hdr(1'b0, TBL, 3'd1);
        hdr(1'b1, OUT, 3'd2);
        dim(1'b1, 3'd0, 16'd6, 32'sd64, 2'd0, 16'sd0);
        dim(1'b1, 3'd1, 16'd2, 32'sd32, 2'd0, 16'sd0);
        gof(3'd2, 2'd1, 1'b1);
        want_fault(4'd0, "an fp32 gather");
        for (r = 0; r < 6; r = r + 1) begin
            for (w = 0; w < 2; w = w + 1) begin
                chk(u_ram.mem[(OUT >> 5) + r*2 + w],
                    u_ram.mem[(TBL >> 5) + ((r < 5) ? idxv[r] : 0)*2 + w],
                    "fp32-indexed word", r*2 + w);
            end
        end

        // Not an index: 2.5, 0.5, -1, 2^31, a NaN; each a one-index list.
        u_ram.mem[(FIDX >> 5) + 0] = {224'd0, 32'h4020_0000};
        u_ram.mem[(FIDX >> 5) + 1] = {224'd0, 32'h3F00_0000};
        u_ram.mem[(FIDX >> 5) + 2] = {224'd0, 32'hBF80_0000};
        u_ram.mem[(FIDX >> 5) + 3] = {224'd0, 32'h4F00_0000};
        u_ram.mem[(FIDX >> 5) + 4] = {224'd0, 32'h7FC0_0000};
        for (i = 0; i < 5; i = i + 1) begin
            wr(8'h30, {8'd0, 16'd1, FIDX + i * 32});
            hdr(1'b1, OUT, 3'd1);
            dim(1'b1, 3'd0, 16'd2, 32'sd32, 2'd0, 16'sd0);
            gof(3'd2, 2'd1, 1'b1);
            want_fault(4'd10, "a non-index fp32");
        end

        // ============ 11. index lists longer than a buffer word ============
        // Rows 8 and 16 read a new buffer word: one-word rows give the row
        // pipeline no slack, so a stale word shows as a wrong row.
        $display("--- 11. %0d one-word rows: gather, fp32 gather, scatter ---", NR);
        for (r = 0; r < NR; r = r + 1) begin
            u_ram.mem[(IDX2 >> 5) + r / 8][(r % 8) * 32 +: 32] = ix2(r);
            u_ram.mem[(FIDX2 >> 5) + r / 8][(r % 8) * 32 +: 32] = f32(ix2(r));
        end
        for (j = 0; j < 2; j = j + 1) begin
            for (r = 0; r < NR; r = r + 1) begin
                u_ram.mem[(OUT >> 5) + r] = 256'd0;
            end
            wr(8'h30, {8'd0, NR[15:0], (j == 0) ? IDX2 : FIDX2});
            wr(8'h50, {16'd0, 16'd1, 32'd32});
            hdr(1'b0, BIGT, 3'd1);
            hdr(1'b1, OUT, 3'd1);
            dim(1'b1, 3'd0, NR[15:0], 32'sd32, 2'd0, 16'sd0);
            gof(3'd2, 2'd1, j[0]);
            want_fault(4'd0, "a 20-row gather");
            for (r = 0; r < NR; r = r + 1) begin
                chk(u_ram.mem[(OUT >> 5) + r], u_ram.mem[(BIGT >> 5) + ix2(r)],
                    (j == 0) ? "20-row gather" : "20-row fp32 gather", r);
            end
        end
        for (r = 0; r < NR; r = r + 1) begin
            u_ram.mem[(SCS >> 5) + r] = {8{32'h5A00_0000 | r}};
        end
        wr(8'h30, {8'd0, NR[15:0], IDX2});
        wr(8'h50, {16'd0, 16'd1, 32'd32});
        hdr(1'b0, SCS, 3'd1);
        dim(1'b0, 3'd0, NR[15:0], 32'sd32, 2'd0, 16'sd0);
        hdr(1'b1, SCD, 3'd1);
        go(3'd6, 2'd1);
        want_fault(4'd0, "a 20-row scatter");
        for (r = 0; r < NR; r = r + 1) begin
            chk(u_ram.mem[(SCD >> 5) + ix2(r)], u_ram.mem[(SCS >> 5) + r],
                "20-row scatter", r);
        end

        // A bad fp32 index on a row latched at the previous row's end (rows of
        // 8 words leave the pipeline time to have it ready): row 0 lands, then
        // fault 10.
        u_ram.mem[FIDX >> 5] = {192'd0, 32'h4020_0000, 32'h3F80_0000};
        for (r = 0; r < 16; r = r + 1) begin
            u_ram.mem[(OUT >> 5) + r] = 256'd0;
        end
        wr(8'h30, {8'd0, 16'd2, FIDX});
        wr(8'h50, {16'd0, 16'd8, 32'd256});
        hdr(1'b0, BIGT, 3'd1);
        hdr(1'b1, OUT, 3'd1);
        dim(1'b1, 3'd0, 16'd16, 32'sd32, 2'd0, 16'sd0);
        gof(3'd2, 2'd1, 1'b1);
        want_fault(4'd10, "a bad second fp32 row");
        for (w = 0; w < 8; w = w + 1) begin
            chk(u_ram.mem[(OUT >> 5) + w], u_ram.mem[(BIGT >> 5) + 8 + w],
                "row 0 before a bad row", w);
            chk(u_ram.mem[(OUT >> 5) + 8 + w], 256'd0, "nothing of the bad row", w);
        end

        checks = checks + mchk;
        errors = errors + merr;
        $display("  %0d AR and %0d AW bursts checked for alignment and 4 KB",
                 n_ar, n_aw);

        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #4000000;
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire
