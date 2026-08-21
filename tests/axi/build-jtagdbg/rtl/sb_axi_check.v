// AXI4 protocol monitor. Simulation only -- bind one to every shim boundary.

// Catches what a data scoreboard cannot: a withdrawn VALID, a payload moving
// under a stalled handshake, a beat count disagreeing with AxLEN.

`timescale 1ns / 1ps
`default_nettype none

module sb_axi_check #(
    parameter integer DW   = 32,
    parameter integer AWD  = 40,
    parameter integer QD   = 64,
    parameter         NAME = "port"
)(
    input  wire            clk,
    input  wire            resetn,

    input  wire [AWD-1:0]  awaddr,
    input  wire [7:0]      awlen,
    input  wire            awvalid,
    input  wire            awready,

    input  wire [DW-1:0]   wdata,
    input  wire            wlast,
    input  wire            wvalid,
    input  wire            wready,

    input  wire            bvalid,
    input  wire            bready,

    input  wire [AWD-1:0]  araddr,
    input  wire [7:0]      arlen,
    input  wire            arvalid,
    input  wire            arready,

    input  wire            rlast,
    input  wire            rvalid,
    input  wire            rready,

    output reg [31:0]      nerr,
    // A silent monitor and a correct design look identical. This says which.
    output reg [31:0]      ntxn
);
    initial begin nerr = 32'd0; ntxn = 32'd0; end

    always @(posedge clk)
        if (resetn && ((awvalid && awready) || (arvalid && arready)))
            ntxn <= ntxn + 32'd1;

    task err;
        input [1023:0] msg;
        begin
            nerr = nerr + 32'd1;
            $display("%0t FAIL %0s: %0s", $time, NAME, msg);
        end
    endtask

    // ------------------------------------------------- VALID may not withdraw
    reg            aw_v_d, w_v_d, ar_v_d, b_v_d, r_v_d;
    reg            aw_r_d, w_r_d, ar_r_d, b_r_d, r_r_d;
    reg [AWD-1:0]  aw_a_d, ar_a_d;
    reg [7:0]      aw_l_d, ar_l_d;
    reg [DW-1:0]   w_d_d;
    reg            w_l_d;

    always @(posedge clk) begin
        aw_v_d <= awvalid; aw_r_d <= awready; aw_a_d <= awaddr; aw_l_d <= awlen;
        ar_v_d <= arvalid; ar_r_d <= arready; ar_a_d <= araddr; ar_l_d <= arlen;
        w_v_d  <= wvalid;  w_r_d  <= wready;  w_d_d  <= wdata;  w_l_d <= wlast;
        b_v_d  <= bvalid;  b_r_d  <= bready;
        r_v_d  <= rvalid;  r_r_d  <= rready;

        if (resetn) begin
            if (aw_v_d && !aw_r_d) begin
                if (!awvalid)             err("AWVALID withdrawn before AWREADY");
                if (awaddr !== aw_a_d)    err("AWADDR moved while stalled");
                if (awlen  !== aw_l_d)    err("AWLEN moved while stalled");
            end
            if (ar_v_d && !ar_r_d) begin
                if (!arvalid)             err("ARVALID withdrawn before ARREADY");
                if (araddr !== ar_a_d)    err("ARADDR moved while stalled");
                if (arlen  !== ar_l_d)    err("ARLEN moved while stalled");
            end
            if (w_v_d && !w_r_d) begin
                if (!wvalid)              err("WVALID withdrawn before WREADY");
                if (wdata !== w_d_d)      err("WDATA moved while stalled");
                if (wlast !== w_l_d)      err("WLAST moved while stalled");
            end
            if (b_v_d && !b_r_d && !bvalid) err("BVALID withdrawn before BREADY");
            if (r_v_d && !r_r_d && !rvalid) err("RVALID withdrawn before RREADY");
        end
    end

    // ------------------------------------------- beat counts against AxLEN
    reg [7:0]  awq [0:QD-1];
    reg [7:0]  arq [0:QD-1];
    integer    awq_w, awq_r, arq_w, arq_r;
    integer    w_beats, r_beats;
    integer    b_out;

    always @(posedge clk) begin
        if (!resetn) begin
            awq_w = 0; awq_r = 0; arq_w = 0; arq_r = 0;
            w_beats = 0; r_beats = 0; b_out = 0;
        end else begin
            if (awvalid && awready) begin
                awq[awq_w % QD] = awlen;
                awq_w = awq_w + 1;
                b_out = b_out + 1;
            end
            if (arvalid && arready) begin
                arq[arq_w % QD] = arlen;
                arq_w = arq_w + 1;
            end

            if (wvalid && wready) begin
                if (wlast) begin
                    if (awq_w == awq_r) err("WLAST with no outstanding AW");
                    else begin
                        if (w_beats != awq[awq_r % QD])
                            err("W beat count disagrees with AWLEN");
                        awq_r = awq_r + 1;
                    end
                    w_beats = 0;
                end else w_beats = w_beats + 1;
            end

            if (rvalid && rready) begin
                if (rlast) begin
                    if (arq_w == arq_r) err("RLAST with no outstanding AR");
                    else begin
                        if (r_beats != arq[arq_r % QD])
                            err("R beat count disagrees with ARLEN");
                        arq_r = arq_r + 1;
                    end
                    r_beats = 0;
                end else r_beats = r_beats + 1;
            end

            if (bvalid && bready) begin
                if (b_out == 0) err("B response with no outstanding write");
                else b_out = b_out - 1;
            end
        end
    end
endmodule

`default_nettype wire
