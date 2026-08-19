// mag_link_cdc -- one direction of an interlink, across two mesh clocks.

// Per-mesh clocks put an asynchronous boundary on a link with no ready
// travelling back. Without this the peer samples a foreign domain, silently.

// A PLAIN FIFO IS ENOUGH, and only because flow control is credit-based: no
// handshake to preserve, and inserting latency is free (mag_link_pipe.v).

// DEPTH MUST EXCEED THE SENDER'S TOTAL CREDIT, both classes together, or a
// beat is DISCARDED -- xpm_fifo_async does not flag a write to a full FIFO.

// LOAD-BEARING: the far end MUST be a mag_link with tready tied high. This
// never backpressures, so a real slave there loses beats, not just throughput.

`default_nettype none

module mag_link_cdc #(
    parameter integer LINK_W  = 288,
    parameter integer TUSER_W = 96,
    // 2*RX_BEATS is the whole credit both classes can hold outstanding; the
    // rest is headroom for credit packets, which consume none.
    parameter integer DEPTH   = 256,
    parameter integer RX_BEATS = 64,
    parameter         MEM_TYPE = "block"
)(
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 s_axis_aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF S_AXIS, ASSOCIATED_RESET s_axis_aresetn" *)
    input  wire                 s_axis_aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 s_axis_aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire                 s_axis_aresetn,
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 m_axis_aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF M_AXIS, ASSOCIATED_RESET m_axis_aresetn" *)
    input  wire                 m_axis_aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 m_axis_aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire                 m_axis_aresetn,

    input  wire [LINK_W-1:0]    S_AXIS_tdata,
    input  wire [TUSER_W-1:0]   S_AXIS_tuser,
    input  wire                 S_AXIS_tlast,
    input  wire                 S_AXIS_tvalid,
    output wire                 S_AXIS_tready,

    output reg  [LINK_W-1:0]    M_AXIS_tdata,
    output reg  [TUSER_W-1:0]   M_AXIS_tuser,
    output reg                  M_AXIS_tlast,
    output reg                  M_AXIS_tvalid,
    input  wire                 M_AXIS_tready,

    // Sticky, in the WRITE domain: a beat arrived with the FIFO full.
    output reg                  fault,

    // XPM HOLDS ITS RESET BUSY FOR SEVERAL CYCLES and drops writes made during
    // it. Hold the meshes in reset until every crossing on the chain says this.
    output reg                  ready
);
    localparam integer W = LINK_W + TUSER_W + 1;

    // Both ends flushed together. A link whose partner reset alone has stale
    // credit on one side, which the FIFO cannot repair and must not hide.
    wire rst = !s_axis_aresetn || !m_axis_aresetn;

    wire         wr_full, rd_empty;
    wire [W-1:0] rd_data;

    // `rst` combines two domains' resets, so its DEASSERTION is asynchronous to
    // at least one clock; a metastable tvalid injects a beat that never existed.
    reg [1:0] s_rst_q, m_rst_q;
    always @(posedge s_axis_aclk or posedge rst)
        if (rst) s_rst_q <= 2'b11; else s_rst_q <= {s_rst_q[0], 1'b0};
    always @(posedge m_axis_aclk or posedge rst)
        if (rst) m_rst_q <= 2'b11; else m_rst_q <= {m_rst_q[0], 1'b0};

    wire s_rst = s_rst_q[1];
    wire m_rst = m_rst_q[1];

    // Never backpressures: the peer's mag_link ties tready high, and a ready
    // travelling back is the combinational SLR crossing mag_link exists to avoid.
    assign S_AXIS_tready = 1'b1;

    // `s_rst`, NOT `rst`. async_fifo's port is wr_clk-domain and xpm_fifo_async
    // wants it so: a raw OR of two domains can half-flush and mismatch pointers.
    async_fifo #(.DATA_WIDTH(W), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE(MEM_TYPE))
    u_fifo (
        .wr_clk(s_axis_aclk), .wr_rst(s_rst),
        .wr_en(S_AXIS_tvalid && !wr_full),
        .wr_data({S_AXIS_tuser, S_AXIS_tlast, S_AXIS_tdata}),
        .wr_full(wr_full),
        .rd_clk(m_axis_aclk), .rd_en(!rd_empty),
        .rd_data(rd_data), .rd_empty(rd_empty)
    );

    always @(posedge s_axis_aclk) begin
        if (s_rst)                           fault <= 1'b0;
        else if (S_AXIS_tvalid && wr_full)   fault <= 1'b1;
    end

    // Empty at reset exit, so `wr_full` IS wr_rst_busy until the first write.
    // Sticky, or it would read as "not ready" whenever the FIFO fills.
    always @(posedge s_axis_aclk) begin
        if (s_rst)         ready <= 1'b0;
        else if (!wr_full) ready <= 1'b1;
    end

    // Registered out, so the far end is still driven from a flop and the
    // placer can keep using a Laguna site for the crossing beyond it.
    always @(posedge m_axis_aclk) begin
        if (m_rst) begin
            M_AXIS_tdata  <= {LINK_W{1'b0}};
            M_AXIS_tuser  <= {TUSER_W{1'b0}};
            M_AXIS_tlast  <= 1'b0;
            M_AXIS_tvalid <= 1'b0;
        end else begin
            M_AXIS_tvalid <= !rd_empty;
            if (!rd_empty)
                {M_AXIS_tuser, M_AXIS_tlast, M_AXIS_tdata} <= rd_data;
        end
    end

`ifndef SYNTHESIS
    initial if (DEPTH < 2*RX_BEATS + 16)
        $display("ERROR mag_link_cdc: DEPTH=%0d cannot hold %0d credited beats plus credit packets",
                 DEPTH, 2*RX_BEATS);
    reg said;
    always @(posedge s_axis_aclk) begin
        if (rst) said <= 1'b0;
        else if (fault && !said) begin
            said <= 1'b1;
            $display("%0t ERROR mag_link_cdc: beat DISCARDED -- the FIFO is shallower than the sender's credit",
                     $time);
        end
    end
    always @(posedge m_axis_aclk)
        if (!rst && M_AXIS_tvalid && !M_AXIS_tready)
            $display("%0t ERROR mag_link_cdc: m_axis_tready low. The far end must be a mag_link with tready tied high.",
                     $time);
`endif

endmodule

`default_nettype wire
