// axi_up32to64 alone: what shape does a 32-bit burst arrive in at 64 bits?
//
// The cases are the ones the ship actually produces. Case D is sb_nsu's
// re-expression of one host write64 that a station NMU flit-aligned to 32 bytes:
// eight 32-bit beats, of which only two carry strobes.

`timescale 1ns / 1ps
`default_nettype none

module axi_up32to64_tb;
    localparam integer AW = 32, IDW = 4;

    integer errors = 0, checks = 0;

    reg clk = 0, resetn = 0;
    always begin
        #2 clk = ~clk;
    end

    reg  [IDW-1:0] s_awid = 0;
    reg  [AW-1:0]  s_awaddr = 0;
    reg  [7:0]     s_awlen = 0;
    reg            s_awvalid = 0;
    reg  [31:0]    s_wdata = 0;
    reg  [3:0]     s_wstrb = 0;
    reg            s_wlast = 0, s_wvalid = 0;
    wire           s_awready, s_wready, s_bvalid;
    reg  [AW-1:0]  s_araddr = 0;
    reg  [7:0]     s_arlen = 0;
    reg            s_arvalid = 0, s_rready = 1;
    wire           s_arready, s_rvalid, s_rlast;
    wire [31:0]    s_rdata;

    wire [IDW-1:0] m_awid, m_arid;
    wire [AW-1:0]  m_awaddr, m_araddr;
    wire [7:0]     m_awlen, m_arlen;
    wire [2:0]     m_awsize, m_arsize;
    wire [1:0]     m_awburst, m_arburst;
    wire           m_awvalid, m_wvalid, m_wlast, m_arvalid;
    wire [63:0]    m_wdata;
    wire [7:0]     m_wstrb;
    wire           m_bready, m_rready;

    reg            m_awready = 1, m_wready = 1, m_arready = 1;
    reg            m_bvalid = 0, m_rvalid = 0, m_rlast = 0;
    reg  [63:0]    m_rdata = 0;

    axi_up32to64 #(.AW(AW), .IDW(IDW)) dut (
        .clk(clk), .resetn(resetn),
        .s_awid(s_awid), .s_awaddr(s_awaddr), .s_awlen(s_awlen),
        .s_awsize(3'd2), .s_awburst(2'b01),
        .s_awvalid(s_awvalid), .s_awready(s_awready),
        .s_wdata(s_wdata), .s_wstrb(s_wstrb), .s_wlast(s_wlast),
        .s_wvalid(s_wvalid), .s_wready(s_wready),
        .s_bid(), .s_bresp(), .s_bvalid(s_bvalid), .s_bready(1'b1),
        .s_arid(4'd0), .s_araddr(s_araddr), .s_arlen(s_arlen),
        .s_arsize(3'd2), .s_arburst(2'b01),
        .s_arvalid(s_arvalid), .s_arready(s_arready),
        .s_rid(), .s_rdata(s_rdata), .s_rresp(), .s_rlast(s_rlast),
        .s_rvalid(s_rvalid), .s_rready(s_rready),
        .m_awid(m_awid), .m_awaddr(m_awaddr), .m_awlen(m_awlen),
        .m_awsize(m_awsize), .m_awburst(m_awburst),
        .m_awvalid(m_awvalid), .m_awready(m_awready),
        .m_wdata(m_wdata), .m_wstrb(m_wstrb), .m_wlast(m_wlast),
        .m_wvalid(m_wvalid), .m_wready(m_wready),
        .m_bid(4'd0), .m_bresp(2'b00), .m_bvalid(m_bvalid), .m_bready(m_bready),
        .m_arid(m_arid), .m_araddr(m_araddr), .m_arlen(m_arlen),
        .m_arsize(m_arsize), .m_arburst(m_arburst),
        .m_arvalid(m_arvalid), .m_arready(m_arready),
        .m_rid(4'd0), .m_rdata(m_rdata), .m_rresp(2'b00), .m_rlast(m_rlast),
        .m_rvalid(m_rvalid), .m_rready(m_rready)
    );

    // ---- capture every master write beat -----------------------------------
    reg [63:0] cap_d [0:31];
    reg [7:0]  cap_s [0:31];
    reg [AW-1:0] cap_aw;
    reg [7:0]  cap_awlen;
    integer    ncap = 0, naw = 0;
    always @(posedge clk) if (resetn) begin
        if (m_awvalid && m_awready) begin
            cap_aw <= m_awaddr; cap_awlen <= m_awlen; naw = naw + 1;
        end
        if (m_wvalid && m_wready) begin
            cap_d[ncap] = m_wdata;
            cap_s[ncap] = m_wstrb;
            ncap = ncap + 1;
        end
    end

    // B one cycle after the last master beat, as a slave would.
    always @(posedge clk) begin
        if (!resetn) begin
            m_bvalid <= 1'b0;
        end
        else begin
            if (m_bvalid && m_bready) begin
                m_bvalid <= 1'b0;
            end
            if (m_wvalid && m_wready && m_wlast) begin
                m_bvalid <= 1'b1;
            end
        end
    end

    task chk(input cond, input [8*40-1:0] what, input integer got,
             input integer want);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                $display("  FAIL %0s: got %0d (%h) want %0d (%h)",
                         what, got, got, want, want);
            end
        end
    endtask

    integer i, spin;

    // One 32-bit burst of `len+1` beats from `addr`, data d0, d0+1, ...
    task wburst(input [AW-1:0] addr, input [7:0] len, input [31:0] d0);
        integer b;
        begin
            ncap = 0; naw = 0;
            @(negedge clk);
            s_awaddr = addr; s_awlen = len; s_awvalid = 1'b1;
            spin = 0;
            while (spin < 500) begin
                @(posedge clk);
                if (s_awready) begin
                    spin = 900;
                end
                else begin
                    spin = spin + 1;
                end
            end
            @(negedge clk); s_awvalid = 1'b0;
            for (b = 0; b <= len; b = b + 1) begin
                @(negedge clk);
                s_wdata = d0 + b; s_wstrb = 4'hF;
                s_wlast = (b == len); s_wvalid = 1'b1;
                spin = 0;
                while (spin < 500) begin
                    @(posedge clk);
                    if (s_wready) begin
                        spin = 900;
                    end
                    else begin
                        spin = spin + 1;
                    end
                end
                @(negedge clk); s_wvalid = 1'b0; s_wlast = 1'b0;
            end
            spin = 0;
            while (!s_bvalid && spin < 500) begin @(negedge clk); spin = spin + 1; end
            @(negedge clk);
        end
    endtask

    // The NSU's shape: eight 32-bit beats covering a 32-byte flit, of which only
    // the pair at `live` carries strobes.
    task wflit(input [AW-1:0] base, input integer live, input [31:0] d0);
        integer b;
        begin
            ncap = 0; naw = 0;
            @(negedge clk);
            s_awaddr = base; s_awlen = 8'd7; s_awvalid = 1'b1;
            spin = 0;
            while (spin < 500) begin
                @(posedge clk);
                if (s_awready) begin
                    spin = 900;
                end
                else begin
                    spin = spin + 1;
                end
            end
            @(negedge clk); s_awvalid = 1'b0;
            for (b = 0; b < 8; b = b + 1) begin
                @(negedge clk);
                s_wdata = (b == live || b == live + 1) ? (d0 + b) : 32'd0;
                s_wstrb = (b == live || b == live + 1) ? 4'hF : 4'h0;
                s_wlast = (b == 7); s_wvalid = 1'b1;
                spin = 0;
                while (spin < 500) begin
                    @(posedge clk);
                    if (s_wready) begin
                        spin = 900;
                    end
                    else begin
                        spin = spin + 1;
                    end
                end
                @(negedge clk); s_wvalid = 1'b0; s_wlast = 1'b0;
            end
            spin = 0;
            while (!s_bvalid && spin < 500) begin @(negedge clk); spin = spin + 1; end
            @(negedge clk);
        end
    endtask

    initial begin
        repeat (10) @(negedge clk);
        resetn = 1'b1;
        repeat (10) @(negedge clk);

        $display("--- A: one 32-bit beat at +0 ---");
        wburst(32'h1000, 8'd0, 32'hAAAA_0001);
        chk(ncap == 1, "A master beats", ncap, 1);
        chk(cap_aw === 32'h1000, "A awaddr", cap_aw, 32'h1000);
        chk(cap_awlen === 8'd0, "A awlen", cap_awlen, 0);
        chk(cap_s[0] === 8'h0F, "A strb low half only", cap_s[0], 8'h0F);
        chk(cap_d[0][31:0] === 32'hAAAA_0001, "A data", cap_d[0][31:0],
            32'hAAAA_0001);

        $display("--- B: one 32-bit beat at +4 (high half) ---");
        wburst(32'h1004, 8'd0, 32'hBBBB_0001);
        chk(ncap == 1, "B master beats", ncap, 1);
        chk(cap_aw === 32'h1000, "B awaddr aligned down", cap_aw, 32'h1000);
        chk(cap_s[0] === 8'hF0, "B strb high half only", cap_s[0], 8'hF0);
        chk(cap_d[0][63:32] === 32'hBBBB_0001, "B data", cap_d[0][63:32],
            32'hBBBB_0001);

        $display("--- C: two beats at +0 PACK into one ---");
        wburst(32'h1000, 8'd1, 32'hCCCC_0000);
        chk(ncap == 1, "C packed to one beat", ncap, 1);
        chk(cap_s[0] === 8'hFF, "C strb both halves", cap_s[0], 8'hFF);
        chk(cap_d[0] === {32'hCCCC_0001, 32'hCCCC_0000}, "C packed data", 0, 0);

        $display("--- D: the ship's shape, 8 beats, one strobed pair ---");
        // A 64-bit register write at flit offset 0x10 is the pair at index 4.
        wflit(32'h2000, 4, 32'hDDDD_0000);
        chk(ncap == 4, "D master beats", ncap, 4);
        chk(cap_awlen === 8'd3, "D awlen", cap_awlen, 3);
        chk(cap_s[0] === 8'h00, "D beat0 unstrobed", cap_s[0], 8'h00);
        chk(cap_s[1] === 8'h00, "D beat1 unstrobed", cap_s[1], 8'h00);
        chk(cap_s[2] === 8'hFF, "D beat2 STROBED", cap_s[2], 8'hFF);
        chk(cap_s[3] === 8'h00, "D beat3 unstrobed", cap_s[3], 8'h00);
        chk(cap_d[2] === {32'hDDDD_0005, 32'hDDDD_0004}, "D live data", 0, 0);

        $display("--- E: odd length, 3 beats from +0 ---");
        wburst(32'h3000, 8'd2, 32'hEEEE_0000);
        chk(ncap == 2, "E master beats", ncap, 2);
        chk(cap_awlen === 8'd1, "E awlen", cap_awlen, 1);
        chk(cap_s[0] === 8'hFF, "E beat0 full", cap_s[0], 8'hFF);
        // The tail beat carries one 32-bit word, so half the strobes are clear:
        // a WSTRB-blind subordinate would zero the co-resident word here.
        chk(cap_s[1] === 8'h0F, "E tail half-strobed", cap_s[1], 8'h0F);

        $display("--- F: read splits back to 32 bits ---");
        fork
            begin
                @(negedge clk);
                s_araddr = 32'h4000; s_arlen = 8'd3; s_arvalid = 1'b1;
                spin = 0;
                while (spin < 500) begin
                    @(posedge clk);
                    if (s_arready) begin
                        spin = 900;
                    end
                    else begin
                        spin = spin + 1;
                    end
                end
                @(negedge clk); s_arvalid = 1'b0;
            end
            begin
                // Answer two 64-bit beats once the master asks.
                spin = 0;
                while (!m_arvalid && spin < 500) begin @(negedge clk); spin = spin + 1; end
                chk(m_arlen === 8'd1, "F arlen halved", m_arlen, 1);
                for (i = 0; i < 2; i = i + 1) begin
                    @(negedge clk);
                    m_rdata = {32'hF00D_0001 + i*2, 32'hF00D_0000 + i*2};
                    m_rlast = (i == 1); m_rvalid = 1'b1;
                    spin = 0;
                    while (spin < 500) begin
                        @(posedge clk);
                        if (m_rready) begin
                            spin = 900;
                        end
                        else begin
                            spin = spin + 1;
                        end
                    end
                    @(negedge clk); m_rvalid = 1'b0; m_rlast = 1'b0;
                end
            end
        join

        if (errors == 0) begin
            $display("PASS axi_up32to64_tb: %0d checks", checks);
        end
        else begin
            $display("FAIL axi_up32to64_tb: %0d errors, %0d checks", errors, checks);
        end
        $finish;
    end

    // The read beats the slave side actually saw.
    integer nrd = 0;
    always @(posedge clk) begin
        if (resetn && s_rvalid && s_rready) begin
            nrd = nrd + 1;
        end
    end

    initial begin
        #200000;
        $display("FAIL axi_up32to64_tb: watchdog");
        $finish;
    end
endmodule

`default_nettype wire
