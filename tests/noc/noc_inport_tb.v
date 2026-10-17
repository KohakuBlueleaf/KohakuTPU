// InPortSwitch against a queue model, one harness per FIFO memory type. The
// sender keeps `valid` until a cycle without `busy` (docs/noc/spec.md s2.1);
// destinations are random, inside and outside the router grid; grants come at
// random, only on the requested output. Every accepted flit must leave exactly
// once, in order, requesting the output its destination routes to, and an
// empty holding queue requests nothing.

`default_nettype none
`timescale 1ns/1ps

module noc_inport_harness #(
    parameter MEM = "distributed",
    parameter integer SEED = 1
)(
    input  wire clk,
    input  wire rst,
    input  wire go,
    input  wire drain,      // stop sending, grant every request
    output reg  [31:0] errs,
    output reg  [31:0] sent,
    output reg  [31:0] taken
);
    localparam integer DW = 32, PW = 4;
    localparam integer MX = 2, MY = 2, LO = 1, HI = 3;
    localparam integer QN = 65536;

    reg  [DW-1:0] din;
    reg           dv;
    wire          busy;
    wire [DW-1:0] hd;
    wire [4:0]    hreq;
    reg  [4:0]    grant;

    InPortSwitch #(
        .DATA_WIDTH(DW), .FIFO_DEPTH(32), .MEMORY_TYPE(MEM), .POS_WIDTH(PW),
        .POS_X(MX), .POS_Y(MY), .GRID_LO(LO), .GRID_HI(HI)
    ) dut (
        .clk(clk), .rst(rst), .data_in(din), .data_valid(dv), .port_busy(busy),
        .head_data(hd), .head_req(hreq), .grant(grant)
    );

    // the routing rule, written out: XY on the clamped destination
    function [4:0] route(input [DW-1:0] f);
        reg [PW-1:0] x, y, rx, ry;
        begin
            x  = f[DW-1 -: PW];
            y  = f[DW-PW-1 -: PW];
            rx = (x < LO) ? LO : (x > HI) ? HI : x;
            ry = (y < LO) ? LO : (y > HI) ? HI : y;
            if (rx != MX)      route = (rx < MX) ? 5'b01000 : 5'b00010;   // west / east
            else if (ry != MY) route = (ry < MY) ? 5'b00001 : 5'b00100;   // north / south
            else if (x < MX)   route = 5'b01000;
            else if (x > MX)   route = 5'b00010;
            else if (y < MY)   route = 5'b00001;
            else if (y > MY)   route = 5'b00100;
            else               route = 5'b10000;                          // local
        end
    endfunction

    reg  [DW-1:0] q [0:QN-1];
    integer qh, qt, phase, seed;
    reg     have;

    initial begin
        seed = SEED; errs = 0; sent = 0; taken = 0; qh = 0; qt = 0;
        din = 0; dv = 0; grant = 0; have = 0; phase = 0;
    end

    // Sender (negedge): offer a new flit once the last was taken; hold it
    // (valid and data) while busy. Receiver: grant the requested output.
    always @(negedge clk) begin
        if (go && !rst) begin
            phase = phase + 1;
            if (!drain && !dv && ($random(seed) & 3) != 0 && sent < QN - 1) begin
                din = $random(seed);
                dv  = 1'b1;
            end
            grant = 5'b00000;
            if (hreq != 5'b00000) begin
                // bursts of back-pressure: long stalls and full-rate spells
                if (drain || ((phase / 300) % 2 == 0 ? (($random(seed) & 7) == 0) : (($random(seed) & 7) != 0))) begin
                    grant = hreq;
                end
            end
        end
    end

    always @(posedge clk) begin
        if (go && !rst) begin
            // the head this cycle, and what a grant consumes
            if (hreq != 5'b00000) begin
                if (qh == qt) begin
                    errs <= errs + 1;
                    $display("  FAIL %0s: head requests %b with nothing sent", MEM, hreq);
                end else begin
                    if (hd !== q[qh]) begin
                        errs <= errs + 1;
                        if (errs < 8) $display("  FAIL %0s: flit %0d head %h want %h", MEM, qh, hd, q[qh]);
                    end
                    if (hreq !== route(q[qh])) begin
                        errs <= errs + 1;
                        if (errs < 8) $display("  FAIL %0s: flit %0d requests %b want %b", MEM, qh, hreq, route(q[qh]));
                    end
                end
            end
            if (grant != 5'b00000) begin
                qh = qh + 1;
                taken <= taken + 1;
            end
            if (dv && !busy) begin
                q[qt] = din;
                qt = qt + 1;
                sent <= sent + 1;
                dv = 1'b0;
            end
        end
    end
endmodule

module noc_inport_tb;
    reg clk = 0, rst = 1, go = 0, drain = 0;
    always #2 clk = ~clk;

    wire [31:0] e0, s0, t0, e1, s1, t1, e2, s2, t2;
    noc_inport_harness #(.MEM("distributed"), .SEED(11)) h0 (.clk(clk), .rst(rst), .go(go), .drain(drain), .errs(e0), .sent(s0), .taken(t0));
    noc_inport_harness #(.MEM("block"),       .SEED(23)) h1 (.clk(clk), .rst(rst), .go(go), .drain(drain), .errs(e1), .sent(s1), .taken(t1));
    noc_inport_harness #(.MEM("lean"),        .SEED(37)) h2 (.clk(clk), .rst(rst), .go(go), .drain(drain), .errs(e2), .sent(s2), .taken(t2));

    integer bad;
    initial begin
        repeat (8) @(posedge clk);
        rst = 0;
        repeat (40) @(posedge clk);
        go = 1;
        repeat (30000) @(posedge clk);
        drain = 1;
        repeat (500) @(posedge clk);
        bad = 0;
        if (e0 != 0 || e1 != 0 || e2 != 0) bad = 1;
        if (s0 != t0 || s1 != t1 || s2 != t2) begin
            bad = 1;
            $display("  FAIL flits sent and taken differ after the drain");
        end
        $display("  distributed: sent %0d taken %0d errors %0d", s0, t0, e0);
        $display("  block:       sent %0d taken %0d errors %0d", s1, t1, e1);
        $display("  lean:        sent %0d taken %0d errors %0d", s2, t2, e2);
        if (s0 < 1000 || s1 < 1000 || s2 < 1000) begin
            bad = 1;
            $display("  FAIL too little traffic to mean anything");
        end
        if (bad == 0) $display("  PASS -- noc_inport: %0d flits through three FIFO types", s0 + s1 + s2);
        else          $display("  FAIL -- noc_inport");
        $finish;
    end
endmodule

`default_nettype wire
