// mx_acu_fp_pump with TILE_OREG 1 against TILE_OREG 0, in lockstep, for the
// block-RAM and the URAM tile: the same random command stream into both, every
// output compared every cycle. The stream keeps the REUSE_MIN contract and
// reuses an address at exactly REUSE_MIN as often as it can, so a write landing
// in the cycle of the earlier read is exercised.

`default_nettype none
`timescale 1ns/1ps

module mx_acu_pump_oreg_pair #(
    parameter PRIM = "block",
    parameter integer DEPTH = 64,
    parameter integer SEED = 1,
    parameter integer N = 20000
)(
    input  wire clk,
    input  wire rst,
    input  wire go,
    output reg  [31:0] errs,
    output reg  [31:0] cmds,
    output reg  [31:0] tight,
    output reg  [31:0] emits,
    output reg         done
);
    localparam integer MW = 14;
    localparam integer TAW = $clog2(DEPTH);
    localparam integer TW = 16 * (MW + 8);
    localparam integer REUSE = 5;
    localparam integer NA = 8;               // addresses in play

    reg  [383:0]  part_in, part_in2;
    reg  [31:0]   sa, sb, sa2, sb2;
    reg  [7:0]    anchor;
    reg           single, le, cmd_valid;
    reg  [2:0]    op;
    reg  [TAW-1:0] addr;
    reg  [TW-1:0] peer_in;

    wire [TW-1:0]  po0, po1;
    wire [255:0]   eo0, eo1;
    wire           pv0, pv1, ev0, ev1, b0, b1;

    mx_acu_fp_pump #(.DEPTH(DEPTH), .ACC_MW(MW), .TILE_PRIM(PRIM), .TILE_OREG(0)) u0 (
        .clk(clk), .rst(rst), .en(1'b1),
        .part_in(part_in), .sa(sa), .sb(sb), .anchor(anchor),
        .part_in2(part_in2), .sa2(sa2), .sb2(sb2), .single(single),
        .op(op), .le(le), .tile_addr(addr), .cmd_valid(cmd_valid), .peer_in(peer_in),
        .peer_out(po0), .peer_valid(pv0), .emit_out(eo0), .emit_valid(ev0), .busy(b0));
    mx_acu_fp_pump #(.DEPTH(DEPTH), .ACC_MW(MW), .TILE_PRIM(PRIM), .TILE_OREG(1)) u1 (
        .clk(clk), .rst(rst), .en(1'b1),
        .part_in(part_in), .sa(sa), .sb(sb), .anchor(anchor),
        .part_in2(part_in2), .sa2(sa2), .sb2(sb2), .single(single),
        .op(op), .le(le), .tile_addr(addr), .cmd_valid(cmd_valid), .peer_in(peer_in),
        .peer_out(po1), .peer_valid(pv1), .emit_out(eo1), .emit_valid(ev1), .busy(b1));

    integer seed, cyc, k, pick;
    integer last  [0:NA-1];                  // cycle of the last command to the address
    reg     opened [0:NA-1];

    // E5M3 block scales near the bias, so values neither vanish nor saturate
    task rand_payload;
        integer j;
        begin
            for (j = 0; j < 12; j = j + 1) begin
                part_in[j*32 +: 32]  = $random(seed);
                part_in2[j*32 +: 32] = $random(seed);
            end
            for (j = 0; j < 4; j = j + 1) begin
                sa[j*8 +: 8]  = 8'h38 + ($random(seed) & 8'h0F);
                sb[j*8 +: 8]  = 8'h38 + ($random(seed) & 8'h0F);
                sa2[j*8 +: 8] = 8'h38 + ($random(seed) & 8'h0F);
                sb2[j*8 +: 8] = 8'h38 + ($random(seed) & 8'h0F);
            end
            anchor = 8'd112 + ($random(seed) & 8'h0F);
            single = ($random(seed) & 7) == 0;
            for (j = 0; j < TW / 32; j = j + 1) peer_in[j*32 +: 32] = $random(seed);
        end
    endtask

    initial begin
        seed = SEED; errs = 0; cmds = 0; tight = 0; emits = 0; done = 0;
        cmd_valid = 0; op = 0; le = 0; addr = 0;
        part_in = 0; part_in2 = 0; sa = 0; sb = 0; sa2 = 0; sb2 = 0; anchor = 0; single = 0; peer_in = 0;
        for (k = 0; k < NA; k = k + 1) begin last[k] = -100; opened[k] = 0; end
    end

    always @(negedge clk) begin
        if (go && !rst && !done) begin
            cyc = cyc + 1;
            cmd_valid = 0; op = 3'd0; le = 0;
            // prefer an address whose contract window ends exactly now
            pick = -1;
            for (k = 0; k < NA; k = k + 1) begin
                if (pick < 0 && opened[k] && cyc - last[k] == REUSE) pick = k;
            end
            if (pick < 0 && ($random(seed) & 1)) begin
                k = {$random(seed)} % NA;
                if (cyc - last[k] >= REUSE) pick = k;
            end
            if (pick >= 0 && cmds < N) begin
                cmd_valid = 1;
                addr = pick;
                rand_payload;
                if (!opened[pick]) begin
                    op = 3'd1;                       // OP_LOAD opens a tile
                    opened[pick] = 1;
                end else begin
                    case ({$random(seed)} % 8)
                        0: op = 3'd1;                // LOAD
                        1, 2: op = 3'd2;             // ADD
                        3: op = 3'd3;                // ADD_PEER
                        4: op = 3'd4;                // SEND
                        5: op = 3'd5;                // EMIT
                        6: op = 3'd7;                // ADD_EMIT
                        default: op = 3'd6;          // FWD
                    endcase
                end
                le = (op == 3'd1) && ($random(seed) & 1);
                if (cyc - last[pick] == REUSE) tight = tight + 1;
                last[pick] = cyc;
                cmds = cmds + 1;
            end
            if (cmds >= N && !b0 && !b1) done = 1;
        end
    end

    initial cyc = 0;

    always @(posedge clk) begin
        if (go && !rst) begin
            if (ev0) emits = emits + 1;
            if ((pv0 !== pv1) || (ev0 !== ev1) || (b0 !== b1)
                || (pv0 && (po0 !== po1)) || (ev0 && (eo0 !== eo1))) begin
                errs = errs + 1;
                if (errs <= 6) $display("  FAIL %0s cyc %0d: peer %b/%b emit %b/%b busy %b/%b data peer %0s emit %0s",
                                        PRIM, cyc, pv0, pv1, ev0, ev1, b0, b1,
                                        (po0 === po1) ? "same" : "DIFF", (eo0 === eo1) ? "same" : "DIFF");
            end
        end
    end
endmodule

module mx_acu_pump_oreg_tb;
    reg clk = 0, rst = 1, go = 0;
    always #2 clk = ~clk;

    wire [31:0] eb, cb, tb, mb, eu, cu, tu, mu;
    wire        db, du;
    mx_acu_pump_oreg_pair #(.PRIM("block"), .DEPTH(64),   .SEED(5))  pb (.clk(clk), .rst(rst), .go(go),
        .errs(eb), .cmds(cb), .tight(tb), .emits(mb), .done(db));
    mx_acu_pump_oreg_pair #(.PRIM("ultra"), .DEPTH(4096), .SEED(17)) pu (.clk(clk), .rst(rst), .go(go),
        .errs(eu), .cmds(cu), .tight(tu), .emits(mu), .done(du));

    initial begin
        repeat (10) @(posedge clk);
        rst = 0;
        repeat (10) @(posedge clk);
        go = 1;
        wait (db && du);
        repeat (40) @(posedge clk);
        $display("  block: %0d commands, %0d at exactly REUSE_MIN, %0d emits, %0d mismatches", cb, tb, mb, eb);
        $display("  ultra: %0d commands, %0d at exactly REUSE_MIN, %0d emits, %0d mismatches", cu, tu, mu, eu);
        if (eb == 0 && eu == 0 && tb > 1000 && tu > 1000 && mb > 1000 && mu > 1000)
            $display("  PASS -- mx_acu_fp_pump TILE_OREG 1 == 0 in lockstep");
        else
            $display("  FAIL -- mx_acu_fp_pump TILE_OREG differential");
        $finish;
    end
    initial begin
        #5000000;
        $display("  FAIL -- mx_acu_pump_oreg_tb watchdog");
        $finish;
    end
endmodule

`default_nettype wire
