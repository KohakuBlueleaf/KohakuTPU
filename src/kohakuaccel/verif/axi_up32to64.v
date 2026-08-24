// A 32 -> 64 AXI4 upsizer, behavioural: the ship's control hop.
//
// 40_bus.tcl puts an axi_dwidth_converter SI=32 MI=64 in front of every mesh's
// S_AXI_CTRL: a station port is 32 bits (sb_line4.v:413), the control slave 64.
// Two consecutive 32-bit beats sharing a word pack into ONE master beat; a lone
// beat leaves the other half's strobes CLEAR, which is why the subordinate must
// honour WSTRB. One transaction at a time, INCR only, slave AxSIZE = 2.

`default_nettype none

module axi_up32to64 #(
    parameter integer AW  = 32,
    parameter integer IDW = 4
)(
    input  wire            clk,
    input  wire            resetn,

    // ---- 32-bit slave: the station port ----
    input  wire [IDW-1:0]  s_awid,
    input  wire [AW-1:0]   s_awaddr,
    input  wire [7:0]      s_awlen,
    input  wire [2:0]      s_awsize,
    input  wire [1:0]      s_awburst,
    input  wire            s_awvalid,
    output wire            s_awready,
    input  wire [31:0]     s_wdata,
    input  wire [3:0]      s_wstrb,
    input  wire            s_wlast,
    input  wire            s_wvalid,
    output wire            s_wready,
    output wire [IDW-1:0]  s_bid,
    output wire [1:0]      s_bresp,
    output wire            s_bvalid,
    input  wire            s_bready,
    input  wire [IDW-1:0]  s_arid,
    input  wire [AW-1:0]   s_araddr,
    input  wire [7:0]      s_arlen,
    input  wire [2:0]      s_arsize,
    input  wire [1:0]      s_arburst,
    input  wire            s_arvalid,
    output wire            s_arready,
    output wire [IDW-1:0]  s_rid,
    output wire [31:0]     s_rdata,
    output wire [1:0]      s_rresp,
    output wire            s_rlast,
    output wire            s_rvalid,
    input  wire            s_rready,

    // ---- 64-bit master: MAG's S_AXI_CTRL ----
    output reg  [IDW-1:0]  m_awid,
    output reg  [AW-1:0]   m_awaddr,
    output reg  [7:0]      m_awlen,
    output wire [2:0]      m_awsize,
    output wire [1:0]      m_awburst,
    output reg             m_awvalid,
    input  wire            m_awready,
    output reg  [63:0]     m_wdata,
    output reg  [7:0]      m_wstrb,
    output reg             m_wlast,
    output reg             m_wvalid,
    input  wire            m_wready,
    input  wire [IDW-1:0]  m_bid,
    input  wire [1:0]      m_bresp,
    input  wire            m_bvalid,
    output wire            m_bready,
    output reg  [IDW-1:0]  m_arid,
    output reg  [AW-1:0]   m_araddr,
    output reg  [7:0]      m_arlen,
    output wire [2:0]      m_arsize,
    output wire [1:0]      m_arburst,
    output reg             m_arvalid,
    input  wire            m_arready,
    input  wire [IDW-1:0]  m_rid,
    input  wire [63:0]     m_rdata,
    input  wire [1:0]      m_rresp,
    input  wire            m_rlast,
    input  wire            m_rvalid,
    output wire            m_rready
);
    assign m_awsize  = 3'd3;
    assign m_arsize  = 3'd3;
    assign m_awburst = 2'b01;
    assign m_arburst = 2'b01;

    // ===================================================================== W
    localparam [1:0] WS_IDLE = 2'd0, WS_AW = 2'd1, WS_FILL = 2'd2, WS_B = 2'd3;
    reg [1:0]       ws;
    reg [IDW-1:0]   w_id;
    reg [8:0]       w_left;      // 32-bit beats still owed by the slave
    reg             w_half;      // which half of the 64-bit word comes next
    reg [63:0]      w_acc;
    reg [7:0]       w_str;
    reg             w_acc_v;     // the accumulator holds something to send

    wire [8:0] s_beats = {1'b0, s_awlen} + 9'd1;
    // Beats the master needs: the first 32-bit beat may start in the high half,
    // so the span is offset by awaddr[2] before halving.
    wire [9:0] w_span  = {1'b0, s_beats} + {9'd0, s_awaddr[2]};
    wire [8:0] w_m_len = w_span[9:1] + {8'd0, w_span[0]} - 9'd1;

    assign s_awready = (ws == WS_IDLE);
    // A beat is taken when there is room: either the accumulator is empty, or
    // it is draining this cycle.
    wire w_room  = !w_acc_v || (m_wvalid && m_wready);
    assign s_wready = (ws == WS_FILL) && w_room;
    wire s_wbeat = s_wvalid && s_wready;

    // Close the master beat on the high half, or when the slave says last.
    wire w_close = s_wbeat && (w_half || s_wlast);

    assign m_bready = (ws == WS_B);
    assign s_bvalid = (ws == WS_B) && m_bvalid;
    assign s_bid    = w_id;
    assign s_bresp  = m_bresp;

    always @(posedge clk) begin
        if (!resetn) begin
            ws <= WS_IDLE; m_awvalid <= 1'b0; m_wvalid <= 1'b0;
            w_acc_v <= 1'b0; w_half <= 1'b0; w_str <= 8'd0; m_wlast <= 1'b0;
        end else begin
            if (m_awvalid && m_awready) begin
                m_awvalid <= 1'b0;
            end
            if (m_wvalid  && m_wready)  begin
                m_wvalid <= 1'b0;
                w_acc_v  <= 1'b0;
            end

            case (ws)
                WS_IDLE: if (s_awvalid && s_awready) begin
                    w_id      <= s_awid;
                    m_awid    <= s_awid;
                    m_awaddr  <= {s_awaddr[AW-1:3], 3'b000};
                    m_awlen   <= w_m_len[7:0];
                    m_awvalid <= 1'b1;
                    w_left    <= s_beats;
                    w_half    <= s_awaddr[2];
                    w_acc     <= 64'd0;
                    w_str     <= 8'd0;
                    w_acc_v   <= 1'b0;
                    ws        <= WS_FILL;
                end

                WS_FILL: begin
                    if (s_wbeat) begin
                        // Merge into the half this beat's address selects; the other
                        // half keeps whatever an earlier beat put there, and its
                        // strobes stay clear if nothing did.
                        if (w_half) begin
                            w_acc[63:32] <= s_wdata;
                            w_str[7:4]   <= s_wstrb;
                        end else begin
                            w_acc[31:0] <= s_wdata;
                            w_str[3:0]  <= s_wstrb;
                        end
                        w_half <= !w_half;
                        w_left <= w_left - 9'd1;
                    end
                    // Built from this beat plus the register, not from w_acc: the
                    // branch above is non-blocking and has not landed yet.
                    if (w_close) begin
                        m_wdata  <= w_half ? {s_wdata, w_acc[31:0]}
                                           : {w_acc[63:32], s_wdata};
                        m_wstrb  <= w_half ? {s_wstrb, w_str[3:0]}
                                           : {w_str[7:4], s_wstrb};
                        m_wlast  <= s_wlast;
                        m_wvalid <= 1'b1;
                        w_acc_v  <= 1'b1;
                        w_acc    <= 64'd0;
                        w_str    <= 8'd0;
                        if (s_wlast) begin
                            ws <= WS_B;
                        end
                    end
                end

                WS_B: if (m_bvalid && s_bready) begin
                    ws <= WS_IDLE;
                end
                default: ws <= WS_IDLE;
            endcase
        end
    end

    // ===================================================================== R
    localparam [1:0] RS_IDLE = 2'd0, RS_AR = 2'd1, RS_DATA = 2'd2;
    reg [1:0]     rs;
    reg [IDW-1:0] r_id;
    reg [8:0]     r_left;        // 32-bit beats still owed to the slave
    reg           r_half;
    reg [63:0]    r_word;
    reg           r_word_v;

    wire [8:0] r_beats = {1'b0, s_arlen} + 9'd1;
    wire [9:0] r_span  = {1'b0, r_beats} + {9'd0, s_araddr[2]};
    wire [8:0] r_m_len = r_span[9:1] + {8'd0, r_span[0]} - 9'd1;

    assign s_arready = (rs == RS_IDLE);
    assign m_rready  = (rs == RS_DATA) && !r_word_v;

    assign s_rvalid = (rs == RS_DATA) && r_word_v;
    assign s_rid    = r_id;
    assign s_rdata  = r_half ? r_word[63:32] : r_word[31:0];
    assign s_rresp  = 2'b00;
    assign s_rlast  = (r_left == 9'd1);

    always @(posedge clk) begin
        if (!resetn) begin
            rs <= RS_IDLE; m_arvalid <= 1'b0; r_word_v <= 1'b0; r_half <= 1'b0;
        end else begin
            if (m_arvalid && m_arready) begin
                m_arvalid <= 1'b0;
            end

            case (rs)
                RS_IDLE: if (s_arvalid && s_arready) begin
                    r_id      <= s_arid;
                    m_arid    <= s_arid;
                    m_araddr  <= {s_araddr[AW-1:3], 3'b000};
                    m_arlen   <= r_m_len[7:0];
                    m_arvalid <= 1'b1;
                    r_left    <= r_beats;
                    r_half    <= s_araddr[2];
                    r_word_v  <= 1'b0;
                    rs        <= RS_DATA;
                end

                RS_DATA: begin
                    if (m_rvalid && m_rready) begin
                        r_word   <= m_rdata;
                        r_word_v <= 1'b1;
                    end
                    if (s_rvalid && s_rready) begin
                        r_left <= r_left - 9'd1;
                        r_half <= !r_half;
                        // The word is spent once its high half has gone, or when the
                        // burst ends inside it.
                        if (r_half || (r_left == 9'd1)) begin
                            r_word_v <= 1'b0;
                        end
                        if (r_left == 9'd1) begin
                            rs <= RS_IDLE;
                        end
                    end
                end
                default: rs <= RS_IDLE;
            endcase
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        if (s_awvalid && s_awready && (s_awsize != 3'd2)) begin
            $display("%0t ERROR axi_up32to64: AWSIZE %0d, this models a 32-bit port",
                     $time, s_awsize);
        end
        if (s_awvalid && s_awready && (s_awburst != 2'b01)) begin
            $display("%0t ERROR axi_up32to64: AWBURST %b, INCR only",
                     $time, s_awburst);
        end
    end
`endif
endmodule

`default_nettype wire
