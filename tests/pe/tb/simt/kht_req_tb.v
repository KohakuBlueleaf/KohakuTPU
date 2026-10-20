// kht_req_tb -- the requestor against a memory model that acknowledges writes
// and answers fills after random delays, out of order. Checked every flit: a
// write's data follows its descriptor with nothing between; no fill leaves while
// an un-acknowledged write carries its line; no two un-acknowledged writes share
// a line; never more than WR_OUT un-acknowledged; `wr_out` counts them; every
// request is taken. The bench also asserts WR_OUT writes were in flight at once.

`timescale 1ns/1ps
`default_nettype none

`ifndef KHT_WOUT
 `define KHT_WOUT 4
`endif

module kht_req_tb;
    localparam integer FW = 288;
    localparam integer WN = `KHT_WOUT;
    localparam integer NREQ = 6000;
    localparam integer LPOOL = 3 * WN + 4;
    localparam [3:0] T_RD = 4'h0, T_WR = 4'h1, T_WD = 4'h4;

    reg clk = 1'b0, resetn = 1'b0;
    always #2 clk = ~clk;

    reg          rd_v = 0, wr_v = 0;
    reg  [25:0]  rd_line = 0, wr_line = 0;
    reg  [7:0]   rd_tag = 0;
    reg  [255:0] wr_data = 0;
    wire         rd_ready, wr_ready, rsp_v;
    wire [7:0]   rsp_tag;
    wire [255:0] rsp_data;
    wire [FW-1:0] send_flit;
    wire         send_valid;
    reg          send_ready = 0;
    reg          rx_rd_resp = 0, rx_wr_ack = 0;
    reg  [7:0]   rx_txn = 0;
    reg  [255:0] rx_data = 0;
    wire [15:0]  wr_out;
    wire         idle;

    kht_req #(.WR_OUT(WN)) dut (
        .clk(clk), .resetn(resetn),
        .rd_v(rd_v), .rd_ready(rd_ready), .rd_line(rd_line), .rd_tag(rd_tag),
        .rsp_v(rsp_v), .rsp_tag(rsp_tag), .rsp_data(rsp_data),
        .wr_v(wr_v), .wr_ready(wr_ready), .wr_line(wr_line), .wr_data(wr_data),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .rx_rd_resp(rx_rd_resp), .rx_txn(rx_txn), .rx_data(rx_data),
        .rx_wr_ack(rx_wr_ack), .wr_out(wr_out), .idle(idle)
    );

    integer errors = 0, checks = 0, i;
    task fail(input [8*72-1:0] what);
        begin
            errors = errors + 1;
            if (errors < 12) $display("  FAIL %0t %0s", $time, what);
        end
    endtask

    // The memory's view: un-acknowledged writes (by txn), and when each is due.
    reg         m_open   [0:255];
    reg [25:0]  m_line   [0:255];
    integer     m_due    [0:255];
    reg         rd_pend  [0:255];
    integer     rd_due   [0:255];
    integer     n_open = 0, peak = 0, now = 0;
    reg         want_data = 0;
    reg [7:0]   data_txn;
    integer     wr_sent = 0, rd_sent = 0, wr_acked = 0, rd_answered = 0;

    wire [3:0]  f_ty  = send_flit[271:268];
    wire [7:0]  f_txn = send_flit[267:260];
    wire        f_lst = send_flit[259];
    wire [25:0] f_ln  = send_flit[255-9 -: 26];

    always @(posedge clk) now <= now + 1;

    // ---- the network side: take flits, check them, schedule answers ----
    integer t, j;
    reg found;
    always @(posedge clk) if (resetn) begin
        rx_wr_ack  <= 1'b0;
        rx_rd_resp <= 1'b0;
        send_ready <= ({$random} % 4) != 0;

        if (send_valid && send_ready) begin
            checks = checks + 1;
            if (want_data) begin
                if (f_ty != T_WD || f_txn != data_txn || !f_lst) fail("write data does not follow its descriptor");
                want_data = 0;
            end
            else if (f_ty == T_WR) begin
                if (m_open[f_txn]) fail("write txn reused while open");
                for (j = 0; j < 256; j = j + 1)
                    if (m_open[j] && m_line[j] == f_ln) fail("two open writes carry one line");
                m_open[f_txn] = 1'b1;
                m_line[f_txn] = f_ln;
                m_due[f_txn]  = now + 4 + ({$random} % 120);
                n_open = n_open + 1;
                if (n_open > peak) peak = n_open;
                if (n_open > WN) fail("more writes un-acknowledged than WR_OUT");
                want_data = 1;
                data_txn  = f_txn;
                wr_sent   = wr_sent + 1;
            end
            else if (f_ty == T_RD) begin
                for (j = 0; j < 256; j = j + 1)
                    if (m_open[j] && m_line[j] == f_ln) fail("fill sent while a write of its line is un-acknowledged");
                rd_pend[f_txn] = 1'b1;
                rd_due[f_txn]  = now + 4 + ({$random} % 40);
                rd_sent = rd_sent + 1;
            end
            else fail("unknown flit type");
        end

        // One answer a cycle, the earliest due first in index order.
        found = 0;
        for (t = 0; t < 256; t = t + 1) begin
            if (!found && m_open[t] && m_due[t] <= now) begin
                found = 1;
                m_open[t] = 1'b0;
                n_open = n_open - 1;
                wr_acked = wr_acked + 1;
                rx_wr_ack <= 1'b1;
                rx_txn    <= t;
            end
        end
        for (t = 0; t < 256; t = t + 1) begin
            if (!found && rd_pend[t] && rd_due[t] <= now) begin
                found = 1;
                rd_pend[t] = 1'b0;
                rd_answered = rd_answered + 1;
                rx_rd_resp <= 1'b1;
                rx_txn     <= t;
            end
        end
    end

    // `wr_out` counts descriptors taken and not acknowledged.
    integer cnt_out = 0;
    always @(posedge clk) if (resetn) begin
        cnt_out = cnt_out + (wr_v && wr_ready) - rx_wr_ack;
        #0.1;
        checks = checks + 1;
        if (wr_out != cnt_out) fail("wr_out does not count the open writes");
    end

    // ---- the LSU side: random reads and writes over a small pool of lines ----
    integer n_rd = 0, n_wr = 0, tg = 0;
    always @(posedge clk) if (resetn) begin
        if (rd_v && rd_ready) begin
            rd_v <= 1'b0;
            n_rd <= n_rd + 1;
        end
        if (wr_v && wr_ready) begin
            wr_v <= 1'b0;
            n_wr <= n_wr + 1;
        end
        if ((!rd_v || rd_ready) && n_rd < NREQ && ({$random} % 3 == 0)
            && !rd_pend[8'h40 + (tg % 64)]) begin
            rd_v    <= 1'b1;
            rd_line <= {$random} % LPOOL;
            rd_tag  <= 8'h40 + (tg % 64);
            tg      <= tg + 1;
        end
        if ((!wr_v || wr_ready) && n_wr < NREQ && ({$random} % 4 != 0)) begin
            wr_v    <= 1'b1;
            wr_line <= {$random} % LPOOL;
            wr_data <= {8{$random}};
        end
    end

    initial begin
        for (i = 0; i < 256; i = i + 1) begin
            m_open[i] = 0; rd_pend[i] = 0;
        end
        repeat (4) @(posedge clk);
        resetn = 1;
        wait ((n_rd >= NREQ && n_wr >= NREQ) || now > 2000000);
        repeat (200) @(posedge clk);
        checks = checks + 4;
        if (n_rd < NREQ || n_wr < NREQ) fail("requests never taken");
        if (wr_acked != wr_sent) fail("writes never acknowledged");
        if (rd_answered != rd_sent) fail("fills never answered");
        if (peak != WN) fail("WR_OUT writes were never in flight at once");
        $display("    %0d fills, %0d writes, peak %0d un-acknowledged (WR_OUT %0d)",
                 rd_sent, wr_sent, peak, WN);
        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end
endmodule

`default_nettype wire
