// THE MAG L2: a staging store reached through special aperture 0.

// The two faces disagree about granularity by construction -- port A moves a
// 1024-bit entry, port B one word -- so whether they agree about BYTES is it.

// TWO DUTS, ONE STIMULUS: banked with a registered dispatch, and the monolithic
// shape. Both are graded, because the banking argument has to be measured.

// Read latency differs between them (4 and 2) and cannot stall, so each is
// collected on its OWN rvalid rather than at a fixed offset.

`default_nettype none
`timescale 1ns/1ps

// -d TB_ENT total entries (16384 is the ship: 4 URAM deep at one bank),
// -d TB_BANKS the banked DUT's banks, -d TB_RLAT its RLAT (0 = depth + 1).
`ifndef TB_ENT
  `define TB_ENT 1024
`endif
`ifndef TB_BANKS
  `define TB_BANKS 2
`endif
`ifndef TB_RLAT
  `define TB_RLAT 0
`endif

module mag_stage_tb;
    localparam integer DW    = 256;
    localparam integer AW    = 40;
    localparam integer WORDS = 4;
    localparam integer ENT   = `TB_ENT;
    localparam integer BANKS = `TB_BANKS;
    localparam integer RLAT  = `TB_RLAT;
    localparam integer LINE  = WORDS*DW;
    localparam integer EBYTES = (WORDS*DW)/8;      // 128
    localparam [1:0]   MESH  = 2'd0;

    // {special, rsvd, mesh, aperture} in the top two nibbles. Mesh 0 staging
    // is 0x80_..., mesh 1's is 0x90_..., a reserved aperture is 0x83_...
    localparam [39:0] BASE   = 40'h80_0000_0000;
    localparam [39:0] REMOTE = 40'h90_0000_0000;
    localparam [39:0] RSVDAP = 40'h83_0000_0000;
    localparam [39:0] DRAM   = 40'h00_1000_0000;

    reg clk = 0, rst = 1;
    always begin
        #2 clk = ~clk;
    end

    reg             a_req = 0, a_we = 0;
    reg  [AW-1:0]   a_addr = 0;
    reg  [LINE-1:0] a_wdata = 0;
    reg             b_req = 0, b_we = 0;
    reg  [AW-1:0]   b_addr = 0;
    reg  [DW-1:0]   b_wdata = 0;

    wire mine_b, gnt_b, flt_b, arv_b, bmine_b, bgnt_b, brv_b;
    wire mine_m, gnt_m, flt_m, arv_m, bmine_m, bgnt_m, brv_m;
    wire [LINE-1:0] ard_b, ard_m;
    wire [DW-1:0]   brd_b, brd_m;

    mag_stage #(.DATA_W(DW), .ADDR_W(AW), .WORDS(WORDS), .BANKS(BANKS),
                .ENTRIES(ENT), .PIPE(1), .RLAT(RLAT), .MESH_ID(MESH)) bnk (
        .clk(clk), .rst(rst),
        .a_req(a_req), .a_we(a_we), .a_addr(a_addr), .a_wdata(a_wdata),
        .a_mine(mine_b), .a_gnt(gnt_b), .a_fault(flt_b),
        .a_rvalid(arv_b), .a_rdata(ard_b),
        .b_req(b_req), .b_we(b_we), .b_addr(b_addr), .b_wdata(b_wdata),
        .b_wstrb({(DW/8){1'b1}}),
        .b_mine(bmine_b), .b_gnt(bgnt_b), .b_rvalid(brv_b), .b_rdata(brd_b)
    );

    mag_stage #(.DATA_W(DW), .ADDR_W(AW), .WORDS(WORDS), .BANKS(1),
                .ENTRIES(ENT), .PIPE(0), .MESH_ID(MESH)) mon (
        .clk(clk), .rst(rst),
        .a_req(a_req), .a_we(a_we), .a_addr(a_addr), .a_wdata(a_wdata),
        .a_mine(mine_m), .a_gnt(gnt_m), .a_fault(flt_m),
        .a_rvalid(arv_m), .a_rdata(ard_m),
        .b_req(b_req), .b_we(b_we), .b_addr(b_addr), .b_wdata(b_wdata),
        .b_wstrb({(DW/8){1'b1}}),
        .b_mine(bmine_m), .b_gnt(bgnt_m), .b_rvalid(brv_m), .b_rdata(brd_m)
    );

    integer errors = 0, checks = 0;

    task chk(input cond, input [255:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 25) begin
                    $display("  FAIL %0s [%0d]", what, where);
                end
            end
        end
    endtask

    // ------------------------------------------------- per-DUT collection
    localparam integer CAP = 64;
    reg [LINE-1:0] ac_b [0:CAP-1], ac_m [0:CAP-1];
    reg [DW-1:0]   bc_b [0:CAP-1], bc_m [0:CAP-1];
    integer na_b = 0, na_m = 0, nb_b = 0, nb_m = 0;

    always @(posedge clk) if (!rst) begin
        if (arv_b) begin
            if (na_b < CAP) begin
                ac_b[na_b] = ard_b;
            end
            na_b = na_b + 1;
        end
        if (arv_m) begin
            if (na_m < CAP) begin
                ac_m[na_m] = ard_m;
            end
            na_m = na_m + 1;
        end
        if (brv_b) begin
            if (nb_b < CAP) begin
                bc_b[nb_b] = brd_b;
            end
            nb_b = nb_b + 1;
        end
        if (brv_m) begin
            if (nb_m < CAP) begin
                bc_m[nb_m] = brd_m;
            end
            nb_m = nb_m + 1;
        end
    end

    task clear_caps;
        begin na_b = 0; na_m = 0; nb_b = 0; nb_m = 0; end
    endtask

    // Word w of entry e, distinct in every field so a mis-banked write shows
    // up as the wrong WORD rather than as plausible data.
    function [DW-1:0] word_of(input integer e, input integer w);
        begin word_of = {8{16'(e), 8'(w), 8'hC5}}; end
    endfunction

    function [LINE-1:0] entry_of(input integer e);
        integer w;
        begin
            entry_of = {LINE{1'b0}};
            for (w = 0; w < WORDS; w = w + 1) begin
                entry_of[w*DW +: DW] = word_of(e, w);
            end
        end
    endfunction

    function [AW-1:0] eaddr(input integer e);
        begin eaddr = BASE + (e * EBYTES); end
    endfunction

    // ---------------------------------------------------------- port drivers
    task a_write(input integer e);
        begin
            @(negedge clk);
            a_req = 1; a_we = 1; a_addr = eaddr(e); a_wdata = entry_of(e);
            @(negedge clk);
            a_req = 0; a_we = 0;
        end
    endtask

    task a_read(input integer e);
        begin
            @(negedge clk);
            a_req = 1; a_we = 0; a_addr = eaddr(e);
            @(negedge clk);
            a_req = 0;
        end
    endtask

    task b_write(input integer e, input integer w, input [DW-1:0] d);
        begin
            @(negedge clk);
            b_req = 1; b_we = 1; b_addr = eaddr(e) + (w * (DW/8));
            b_wdata = d;
            @(negedge clk);
            b_req = 0; b_we = 0;
        end
    endtask

    task b_read(input integer e, input integer w);
        begin
            @(negedge clk);
            b_req = 1; b_we = 0; b_addr = eaddr(e) + (w * (DW/8));
            @(negedge clk);
            b_req = 0;
        end
    endtask

    task settle(input integer n);
        begin repeat (n) @(negedge clk); end
    endtask

    // Both DUTs returned exactly one entry, and it is the one asked for.
    task one_entry(input integer e, input [255:0] what);
        begin
            chk((na_b == 1) && (ac_b[0] === entry_of(e)), what, e);
            chk((na_m == 1) && (ac_m[0] === entry_of(e)), what, e + 10000);
        end
    endtask

    // ---------------------------------------------------------------- the run
    integer i, w, bad_b, bad_m;

    initial begin
        repeat (8) @(negedge clk);
        rst = 0;
        repeat (4) @(negedge clk);

        // MESH FIRST, THEN APERTURE: that ordering is what stops a transit MAG
        // claiming staging that was only passing through it.
        $display("--- 1. mesh, then aperture, then range: 1 ERROR expected ---");
        @(negedge clk);
        a_addr = BASE;                          #1;
        chk(mine_b === 1'b1, "the staging aperture is ours", 0);
        chk(mine_m === 1'b1, "on both shapes", 0);
        a_addr = BASE + (ENT*EBYTES) - 40'd1;   #1;
        chk(mine_b === 1'b1, "the last byte of the store is ours", 0);
        a_addr = DRAM;                          #1;
        chk(mine_b === 1'b0, "a DRAM address is not", 0);
        chk(flt_b  === 1'b0, "and does not fault", 0);

        // THE TRANSIT CASE. Another mesh's staging address is neither ours nor
        // a fault: it belongs to a MAG further down the chain.
        a_addr = REMOTE;                        #1;
        chk(mine_b === 1'b0, "another mesh's staging is not ours", 0);
        chk(flt_b  === 1'b0, "and must not fault -- it is in transit", 0);
        a_addr = REMOTE + 40'h20_0000;          #1;
        chk(mine_b === 1'b0, "at any offset", 0);
        chk(flt_b  === 1'b0, "at any offset", 0);

        // A reserved aperture in THIS mesh faults rather than aliasing.
        a_addr = RSVDAP;                        #1;
        chk(mine_b === 1'b0, "a reserved aperture is not ours", 0);
        chk(flt_b  === 1'b0, "and is quiet until someone requests it", 0);
        @(negedge clk);
        a_req = 1; a_we = 0; a_addr = RSVDAP;   #1;
        chk(flt_b === 1'b1, "a request to a reserved aperture faults", 0);
        chk(gnt_b === 1'b0, "and is not granted", 0);
        @(negedge clk);
        a_req = 0; a_addr = 0;
        // A reserved aperture in ANOTHER mesh is transit, so it is silent here.
        @(negedge clk);
        a_req = 1; a_addr = REMOTE | 40'h03_0000_0000; #1;
        chk(flt_b === 1'b0, "another mesh's reserved aperture is not our fault",
            0);
        @(negedge clk);
        a_req = 0; a_addr = 0;
        b_addr = BASE + 40'd64;                 #1;
        chk(bmine_b === 1'b1, "the host sees the same aperture", 0);
        @(negedge clk);
        b_addr = 0;

        // ============================================ 2. entry in, entry out
        $display("--- 2. write entries on port A and read them back ---");
        for (i = 0; i < 8; i = i + 1) begin
            a_write(i);
        end
        for (i = ENT-4; i < ENT; i = i + 1) begin
            a_write(i);
        end

        bad_b = 0; bad_m = 0;
        for (i = 0; i < 8; i = i + 1) begin
            clear_caps;
            a_read(i);
            settle(8);
            if ((na_b != 1) || (ac_b[0] !== entry_of(i))) begin
                bad_b = bad_b + 1;
            end
            if ((na_m != 1) || (ac_m[0] !== entry_of(i))) begin
                bad_m = bad_m + 1;
            end
        end
        chk(bad_b == 0, "banked: every entry comes back whole", bad_b);
        chk(bad_m == 0, "monolithic: every entry comes back whole", bad_m);

        bad_b = 0; bad_m = 0;
        for (i = ENT-4; i < ENT; i = i + 1) begin
            clear_caps;
            a_read(i);
            settle(8);
            if ((na_b != 1) || (ac_b[0] !== entry_of(i))) begin
                bad_b = bad_b + 1;
            end
            if ((na_m != 1) || (ac_m[0] !== entry_of(i))) begin
                bad_m = bad_m + 1;
            end
        end
        chk(bad_b == 0, "including the last entries in the store", bad_b);
        chk(bad_m == 0, "on both shapes", bad_m);

        // ============================================ 3. the two granularities
        $display("--- 3. the host reads the agent's entry, word by word ---");
        bad_b = 0; bad_m = 0;
        for (i = 0; i < 4; i = i + 1) begin
            for (w = 0; w < WORDS; w = w + 1) begin
                clear_caps;
                b_read(i, w);
                settle(8);
                if ((nb_b != 1) || (bc_b[0] !== word_of(i, w))) begin
                    bad_b = bad_b + 1;
                end
                if ((nb_m != 1) || (bc_m[0] !== word_of(i, w))) begin
                    bad_m = bad_m + 1;
                end
            end
        end
        chk(bad_b == 0, "word w of entry e is at byte offset w*32", bad_b);
        chk(bad_m == 0, "on both shapes", bad_m);

        $display("--- 4. the host writes words, the agent reads the entry ---");
        for (w = 0; w < WORDS; w = w + 1) begin
            b_write(100, w, word_of(100, w));
        end
        clear_caps;
        a_read(100);
        settle(8);
        one_entry(100, "four host words assemble into one fill entry");

        // A single word, so a write that lands in the wrong bank is visible.
        b_write(100, 2, word_of(777, 2));
        clear_caps;
        a_read(100);
        settle(8);
        bad_b = 0;
        for (w = 0; w < WORDS; w = w + 1) begin
            if (ac_b[0][w*DW +: DW] !== ((w == 2) ? word_of(777, 2)
                                                 : word_of(100, w)))
                bad_b = bad_b + 1;
        end
        chk(bad_b == 0, "a host word write touches its bank and no other", bad_b);

        // A read and a host write want different ports, so both go on either
        // shape. Two reads of DIFFERENT BANKS is what banking alone buys.
        $display("--- 5. arbitration, and what banking adds: 1 ERROR expected ---");
        @(negedge clk);
        a_req = 1; a_we = 0; a_addr = eaddr(0);
        b_req = 1; b_we = 1; b_addr = eaddr(200); b_wdata = word_of(200, 0);
        #1;
        chk(gnt_b  === 1'b1, "the agent read is granted", 0);
        chk(bgnt_b === 1'b1, "and the host write goes beside it", 0);
        chk(bgnt_m === 1'b1, "on the monolithic shape too", 0);
        @(negedge clk);
        a_req = 0; b_req = 0; b_we = 0;
        settle(8);

        // Entry 0 is bank 0 and entry 1 is bank 1, interleaved on the low bit.
        @(negedge clk);
        a_req = 1; a_we = 0; a_addr = eaddr(0);
        b_req = 1; b_we = 0; b_addr = eaddr(1);
        #1;
        // BANKING BUYS PLACEMENT AND Fmax, NOT CONCURRENCY. Two reads share the
        // return select whatever bank they name, so B retries on both shapes.
        chk(gnt_b  === 1'b1, "banked: the agent read is granted", 0);
        chk(bgnt_b === 1'b0, "banked: two reads share the return select", 0);
        chk(bgnt_m === 1'b0, "monolithic: one array, so the host must retry", 0);
        @(negedge clk);
        a_req = 0; b_req = 0;
        settle(8);

        // Same bank, same port: A wins on both shapes.
        @(negedge clk);
        a_req = 1; a_we = 0; a_addr = eaddr(0);
        b_req = 1; b_we = 0; b_addr = eaddr(2);
        #1;
        chk(gnt_b  === 1'b1, "same bank: the agent still wins", 0);
        chk(bgnt_b === 1'b0, "and the host is told to retry", 0);
        @(negedge clk);
        a_req = 0; b_req = 0;
        settle(8);

        // ============================================ 6. a miss writes nothing
        $display("--- 6. an out-of-aperture write changes nothing ---");
        @(negedge clk);
        a_req = 1; a_we = 1; a_addr = DRAM; a_wdata = {LINE{1'b1}};
        #1;
        chk(mine_b === 1'b0, "out of aperture is not ours", 0);
        chk(gnt_b  === 1'b0, "and is not granted", 0);
        @(negedge clk);
        a_req = 0; a_we = 0;
        clear_caps;
        a_read(0);
        settle(8);
        one_entry(0, "entry 0 is untouched");

        // A REMOTE write must not land either: that is the transit failure.
        @(negedge clk);
        a_req = 1; a_we = 1; a_addr = REMOTE; a_wdata = {LINE{1'b1}};
        @(negedge clk);
        a_req = 0; a_we = 0;
        clear_caps;
        a_read(0);
        settle(8);
        one_entry(0, "a remote staging write did not land here");

        // Back to back with no gap: both pipelines are fixed, so the answers
        // come out in order at one per cycle or not at all.
        $display("--- 7. eight reads back to back ---");
        for (i = 0; i < 8; i = i + 1) begin
            a_write(i);
        end
        clear_caps;
        for (i = 0; i < 8; i = i + 1) begin
            @(negedge clk);
            a_req = 1; a_we = 0; a_addr = eaddr(i);
        end
        @(negedge clk);
        a_req = 0;
        settle(12);

        chk(na_b == 8, "banked: eight requests, eight answers", na_b);
        chk(na_m == 8, "monolithic: eight requests, eight answers", na_m);
        bad_b = 0; bad_m = 0;
        for (i = 0; i < 8; i = i + 1) begin
            if ((i < na_b) && (ac_b[i] !== entry_of(i))) begin
                bad_b = bad_b + 1;
            end
            if ((i < na_m) && (ac_m[i] !== entry_of(i))) begin
                bad_m = bad_m + 1;
            end
        end
        chk(bad_b == 0, "banked: in order, across both banks", bad_b);
        chk(bad_m == 0, "monolithic: in order", bad_m);

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
        #2000000;
        $display("  FAIL -- watchdog (checks=%0d errors=%0d)", checks, errors);
        $finish;
    end

endmodule

`default_nettype wire
