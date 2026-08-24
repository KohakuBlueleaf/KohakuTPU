// THE FRAMEWORK WITH NO PROJECT IN IT.
//
// Every source this bench compiles is kohakuaccel, templates or verif: the
// mover, the slot, the identity bank. `mag_xform` names `xform_bank` and that is
// the one module name the framework fixes, so if the only `xform_bank` in the
// tree belonged to a project, this bench could not be built at all -- which is
// the dependency rule it exists to hold.
//
// The identity bank is 1:1, so a transform move through it is a copy, and the
// destination must equal the source byte for byte. That also checks the slot's
// fixed-output-shape rule from the other side: four beats in, four words out,
// `done` on the last, for an occupant that computes nothing.

`default_nettype none
`timescale 1ns/1ps

module xform_identity_tb;
    localparam integer DW = 256, AW = 40, IDW = 4;
    localparam integer NENT = 4;
    localparam [15:0] NENT16 = 16'd4, NSRCW16 = 16'd16;

    localparam [AW-1:0] SRC = 40'h10_0000;
    localparam [AW-1:0] DST = 40'h20_0000;

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
    wire [31:0] x_rdata;
    wire [3:0]  x_fault;

    mm_mover #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .IDX_WORDS(128),
               .XID_W(4), .XMODE_W(4),
               .XF_IN_BITS(1024), .XF_OUT_WORDS(4)) dut (
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
                .IN_BITS(1024), .OUT_WORDS(4)) u_slot (
        .clk(clk), .rst(!resetn),
        .req(x_req), .gnt(x_gnt),
        .start(x_start), .id(x_id), .mode(x_mode),
        .beat(x_beat), .beat_valid(x_bv),
        .done(x_done), .word0(x_w0), .word1(x_w1), .word2(x_w2), .word3(x_w3),
        .cfg_en(1'b0), .cfg_id(4'd0), .cfg_addr(8'd4), .cfg_data(32'd0),
        .cfg_rdata(x_rdata), .fault(x_fault)
    );

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .WORDS(70000), .PORTS(1))
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

    integer errors = 0, checks = 0, spin, i;

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

    initial begin
        for (i = 0; i < NENT*4; i = i + 1) begin
            u_ram.mem[(SRC >> 5) + i] = {8{32'h5EED_0000}} | i;
            u_ram.mem[(DST >> 5) + i] = {8{32'hA5A5_A5A5}};
        end

        repeat (20) @(negedge clk);
        resetn = 1'b1;
        repeat (10) @(negedge clk);

        $display("--- an identity transform is a copy ---");
        hdrx(1'b0, SRC, 3'd1, 4'd0, 4'd0);
        dim(1'b0, 3'd0, NSRCW16, 32'sd32);
        hdrx(1'b1, DST, 3'd1, 4'd0, 4'd0);
        dim(1'b1, 3'd0, NENT16, 32'sd128);
        wr(8'h00, {47'd0, 1'b1, 8'd0, 3'd0, 2'd1, 3'd5});

        @(negedge clk);
        spin = 0;
        while (stat_busy && spin < 200000) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk({255'd0, (spin < 200000)}, 256'd1, "the mover went idle", spin);
        chk({252'd0, stat_fault}, 256'd0, "no fault", 0);

        for (i = 0; i < NENT*4; i = i + 1) begin
            chk(u_ram.mem[(DST >> 5) + i], u_ram.mem[(SRC >> 5) + i],
                "the identity bank copied the word", i);
        end

        // The geometry register: {8'd0, OUT_WORDS, IN_BITS} = 4 words, 1024 bits.
        chk({224'd0, x_rdata}, {224'd0, 32'h0004_0400},
            "the bank reports its geometry", 0);
        chk({252'd0, x_fault}, 256'd0, "an empty bank never faults", 0);

        if (errors == 0) begin
            $display("PASS xform_identity_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL xform_identity_tb: %0d errors, %0d checks",
                     errors, checks);
        end
        $finish;
    end

    initial begin
        #2000000;
        $display("FAIL xform_identity_tb: watchdog  busy=%b fault=%0d",
                 stat_busy, stat_fault);
        $finish;
    end
endmodule

`default_nettype wire
