// mag_ilink's encapsulation and outbound, alone: silicon flaw F-002
// (docs/spec/stack/flaws.md) asserted as recorded. A packet of two flits goes
// whole; a one-flit packet is read from u_enc before XPM fwft shows its flit,
// so it carries the flit before and the real one leads the next packet.
//
// Beats on the link, flits tagged 1..4 (A = 1,2; B = 3; C = 4): the real cell
// and the fwft shim give 1, 2, 2, 3; a FIFO without fwft latency gives 1, 2, 3, 4
// and fails here.

`default_nettype none
`timescale 1ns/1ps

module mag_ilink_tail_tb;
    localparam integer FW = 288;
    localparam integer NF_LA = FW - 16 - 13;
    localparam integer NF_RS = FW - 16 - 16;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg  [FW-1:0] enc_data = 0;
    reg           enc_valid = 0;
    wire          enc_busy;
    wire [95:0]   ltx_hdr;
    wire          ltx_hvalid, ltx_dlast, ltx_dvalid;
    wire [287:0]  ltx_dat;

    mag_ilink #(.MESH_ID(0)) dut (
        .clk(clk), .resetn(resetn),
        .cfg_en(1'b0), .cfg_addr(8'd0), .cfg_data(64'd0), .stat_sel(4'd0),
        .stat_q(), .dbell_counts(), .sig_word(), .my_mesh(), .fence_hold(1'b0),
        .c_awaddr(40'd0), .c_awvalid(1'b0), .c_awready(), .c_wdata(256'd0),
        .c_wstrb(32'd0), .c_wvalid(1'b0), .c_wready(), .c_bvalid(),
        .s_awaddr(40'd0), .s_awlen(8'd0), .s_awvalid(1'b0), .s_awready(),
        .s_wdata(256'd0), .s_wstrb(32'd0), .s_wvalid(1'b0), .s_wready(),
        .s_bvalid(), .s_bresp(), .s_bready(1'b1),
        .m_awaddr(), .m_awvalid(), .m_awready(1'b1), .m_wdata(), .m_wstrb(),
        .m_wlast(), .m_wvalid(), .m_wready(1'b1), .m_bvalid(1'b0),
        .m_bresp(2'd0), .m_bready(),
        .lk_awaddr(), .lk_awvalid(), .lk_awready(1'b1), .lk_wdata(), .lk_wstrb(),
        .lk_wlast(), .lk_wvalid(), .lk_wready(1'b1), .lk_bvalid(1'b0),
        .lk_bresp(2'd0), .lk_bready(),
        .enc_data(enc_data), .enc_valid(enc_valid), .enc_busy(enc_busy),
        .inj_data(), .inj_valid(), .inj_busy(1'b0),
        .ltx_hdr(ltx_hdr), .ltx_hvalid(ltx_hvalid), .ltx_hready(1'b1),
        .ltx_dat(ltx_dat), .ltx_dlast(ltx_dlast), .ltx_dvalid(ltx_dvalid),
        .ltx_dready(1'b1),
        .lrx_hdr(96'd0), .lrx_hvalid(1'b0), .lrx_hready(), .lrx_dat(288'd0),
        .lrx_dlast(1'b0), .lrx_dvalid(1'b0), .lrx_dready(),
        .sw_tx0(64'd0), .sw_rx0(64'd0), .sw_stall0(64'd0), .sw_tx1(64'd0),
        .sw_rx1(64'd0), .sw_stall1(64'd0), .sw_fwd(64'd0), .sw_lblock(64'd0),
        .sw_cred0(32'd0), .sw_cred1(32'd0), .sw_fault(4'd0),
        .bad_remote_req(1'b0)
    );

    // Every beat the link takes, its tag.
    integer beats = 0;
    reg [15:0] got [0:15];
    always @(posedge clk) if (resetn && ltx_dvalid) begin
        got[beats] <= ltx_dat[15:0];
        beats <= beats + 1;
    end

    // One flit to mesh 1, remote, tagged; `last` closes its packet.
    task flit(input [15:0] tag, input last);
        begin
            enc_data = 0;
            enc_data[15:0] = tag;
            enc_data[NF_RS +: 2] = 2'd1;
            enc_data[NF_RS + 2] = 1'b1;
            enc_data[NF_LA] = last;
            enc_valid = 1'b1;
            @(negedge clk);
            while (enc_busy) @(negedge clk);
            enc_valid = 1'b0;
        end
    endtask

    integer errors = 0, i;
    reg [15:0] want [0:3];

    initial begin
        want[0] = 16'd1; want[1] = 16'd2; want[2] = 16'd2; want[3] = 16'd3;
        repeat (8) @(negedge clk);
        resetn = 1;
        repeat (8) @(negedge clk);
        flit(16'd1, 1'b0);
        flit(16'd2, 1'b1);
        repeat (40) @(negedge clk);
        flit(16'd3, 1'b1);
        repeat (40) @(negedge clk);
        flit(16'd4, 1'b1);
        repeat (40) @(negedge clk);
        if (beats != 4) begin
            errors = errors + 1;
            $display("  FAIL %0d beats on the link, want 4", beats);
        end
        for (i = 0; i < 4 && i < beats; i = i + 1) begin
            $display("--- beat %0d: flit %0d", i, got[i]);
            if (got[i] !== want[i]) begin
                errors = errors + 1;
                $display("  FAIL beat %0d carries flit %0d, the record says %0d",
                         i, got[i], want[i]);
            end
        end
        if (errors == 0) $display("  PASS -- F-002 as recorded");
        else $display("  FAIL -- %0d errors", errors);
        $finish;
    end

    initial begin
        #100000;
        $display("  FAIL -- watchdog");
        $finish;
    end
endmodule

`default_nettype wire
