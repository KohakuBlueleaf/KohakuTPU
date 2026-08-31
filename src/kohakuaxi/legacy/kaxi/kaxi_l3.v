// KohakuAXI per-home L3 cache -- direct-mapped, 1 line = 1 beat, write-through +
// write/read-allocate. Same-width AXI4 slave (crossbar home) -> AXI4 master (MIG).
// The TAG lives BESIDE the data in the SAME array row (no separate tag table):
// row = {tag, data}, one 8-wide URAM column holds 512b data + 19b tag in its 576b.
// Array is URAM by default (RAM_STYLE), so the read is SYNCHRONOUS+pipelined
// (issue -> check) -- URAM cannot be read combinationally. VALID is in flops
// (URAM has no reset). One transaction in flight per direction.

`default_nettype none

module kaxi_l3 #(
    parameter integer ADDR_W = 40,
    parameter integer DATA_W = 512,
    parameter integer ID_W   = 6,
    parameter integer SETS   = 32768,               // 512b x 32768 = 2 MB = 64 URAM
    parameter integer LINE_LSB = 6,                 // log2(DATA_W/8); 512b -> 6
    parameter integer SET_W  = 15,                  // log2(SETS)
    parameter integer TAG_W  = ADDR_W - LINE_LSB - SET_W,
    parameter         RAM_STYLE = "ultra"           // "ultra" | "block" | "distributed"
)(
    input  wire                    clk,
    input  wire                    resetn,

    input  wire [ID_W-1:0]         s_awid,
    input  wire [ADDR_W-1:0]       s_awaddr,
    input  wire [7:0]              s_awlen,
    input  wire [2:0]              s_awsize,
    input  wire [1:0]              s_awburst,
    input  wire                    s_awvalid,
    output wire                    s_awready,
    input  wire [DATA_W-1:0]       s_wdata,
    input  wire [DATA_W/8-1:0]     s_wstrb,
    input  wire                    s_wlast,
    input  wire                    s_wvalid,
    output wire                    s_wready,
    output reg  [ID_W-1:0]         s_bid,
    output reg  [1:0]              s_bresp,
    output reg                     s_bvalid,
    input  wire                    s_bready,
    input  wire [ID_W-1:0]         s_arid,
    input  wire [ADDR_W-1:0]       s_araddr,
    input  wire [7:0]              s_arlen,
    input  wire [2:0]              s_arsize,
    input  wire [1:0]              s_arburst,
    input  wire                    s_arvalid,
    output wire                    s_arready,
    output reg  [ID_W-1:0]         s_rid,
    output reg  [DATA_W-1:0]       s_rdata,
    output reg  [1:0]              s_rresp,
    output reg                     s_rlast,
    output reg                     s_rvalid,
    input  wire                    s_rready,

    output reg  [ID_W-1:0]         m_awid,
    output reg  [ADDR_W-1:0]       m_awaddr,
    output reg  [7:0]              m_awlen,
    output reg  [2:0]              m_awsize,
    output reg  [1:0]              m_awburst,
    output reg                     m_awvalid,
    input  wire                    m_awready,
    output wire [DATA_W-1:0]       m_wdata,
    output wire [DATA_W/8-1:0]     m_wstrb,
    output wire                    m_wlast,
    output wire                    m_wvalid,
    input  wire                    m_wready,
    input  wire [ID_W-1:0]         m_bid,
    input  wire [1:0]              m_bresp,
    input  wire                    m_bvalid,
    output reg                     m_bready,
    output reg  [ID_W-1:0]         m_arid,
    output reg  [ADDR_W-1:0]       m_araddr,
    output reg  [7:0]              m_arlen,
    output reg  [2:0]              m_arsize,
    output reg  [1:0]              m_arburst,
    output reg                     m_arvalid,
    input  wire                    m_arready,
    input  wire [ID_W-1:0]         m_rid,
    input  wire [DATA_W-1:0]       m_rdata,
    input  wire [1:0]              m_rresp,
    input  wire                    m_rlast,
    input  wire                    m_rvalid,
    output wire                    m_rready
);
    localparam integer BYTE_STEP = (1 << LINE_LSB);
    // Row = {valid, tag, data} in one RAM word. VALID lives in the row, NOT a flop
    // array: arr_vld[rd_idx] would be a SETS:1 mux (~11k LUT/cache at 4096). URAM
    // cannot init, so a reset FLUSH walks the sets clearing valid before serving.
    localparam integer ROW_W   = 1 + TAG_W + DATA_W;
    // ultra: 2 MB = 8-deep URAM cascade; READ_LAT 2 closed only 308 MHz, so
    // pipeline deeper. The read FSM over-waits (RD_WAIT), so any RD_LAT is safe.
    localparam integer RD_LAT  = (RAM_STYLE == "ultra") ? 4 : 1;
    localparam integer RD_WAIT = RD_LAT + 1;

    reg              flushing;
    reg [SET_W-1:0]  flush_idx;

    function [SET_W-1:0] idx_of; input [ADDR_W-1:0] a;
        begin idx_of = a[LINE_LSB +: SET_W]; end
    endfunction
    function [TAG_W-1:0] tag_of; input [ADDR_W-1:0] a;
        begin tag_of = a[LINE_LSB+SET_W +: TAG_W]; end
    endfunction

    // ============================ READ path (FSM) ============================
    localparam [2:0] R_IDLE=0, R_ISSUE=1, R_WAIT=2, R_CHK=3, R_FETCH=4, R_DRAIN=5;
    reg [2:0]        rst_st;
    reg [ADDR_W-1:0] r_addr;
    reg [7:0]        r_left;
    reg [ID_W-1:0]   r_id;
    reg [2:0]        r_size;
    reg [1:0]        r_burst;
    reg [2:0]        rwait;
    wire             r_incr = (r_burst == 2'b01);     // non-INCR -> SLVERR

    reg [TAG_W-1:0]  exptag_q;
    reg [SET_W-1:0]  ridx_q;
    wire             rd_en  = (rst_st == R_ISSUE);
    wire [SET_W-1:0] rd_idx = idx_of(r_addr);

    wire [ROW_W-1:0] rd_row;                          // {valid, tag, data}
    wire             r_hit  = rd_row[ROW_W-1]
                              && (rd_row[DATA_W +: TAG_W] == exptag_q);

    reg arrdy;
    assign s_arready = arrdy && !flushing;
    assign m_rready  = (rst_st == R_FETCH);

    always @(posedge clk) begin
        if (!resetn) begin
            rst_st <= R_IDLE; s_rvalid <= 1'b0; m_arvalid <= 1'b0; arrdy <= 1'b1;
        end else begin
            case (rst_st)
                R_IDLE: begin
                    s_rvalid <= 1'b0;
                    if (s_arvalid && arrdy && !flushing) begin
                        r_addr <= s_araddr; r_left <= s_arlen; r_id <= s_arid;
                        r_size <= s_arsize; r_burst <= s_arburst;
                        arrdy <= 1'b0; rst_st <= R_ISSUE;
                    end
                end
                R_ISSUE: begin
                    exptag_q <= tag_of(r_addr); ridx_q <= rd_idx;
                    rwait    <= RD_WAIT[2:0]; rst_st <= R_WAIT;
                end
                R_WAIT: begin
                    if (rwait <= 3'd1) begin
                        rst_st <= R_CHK;
                    end else begin
                        rwait <= rwait - 3'd1;
                    end
                end
                R_CHK: begin
                    if (!r_incr) begin                    // non-INCR: SLVERR, no walk
                        s_rdata <= {DATA_W{1'b0}}; s_rid <= r_id; s_rresp <= 2'b10;
                        s_rlast <= (r_left == 8'd0); s_rvalid <= 1'b1;
                        rst_st  <= R_DRAIN;
                    end else if (r_hit) begin
                        s_rdata <= rd_row[DATA_W-1:0]; s_rid <= r_id; s_rresp <= 2'b00;
                        s_rlast <= (r_left == 8'd0); s_rvalid <= 1'b1;
                        rst_st  <= R_DRAIN;
                    end else begin
                        m_arid <= r_id;
                        m_araddr <= {r_addr[ADDR_W-1:LINE_LSB], {LINE_LSB{1'b0}}};
                        m_arlen <= 8'd0; m_arsize <= r_size; m_arburst <= 2'b01;
                        m_arvalid <= 1'b1; rst_st <= R_FETCH;
                    end
                end
                R_FETCH: begin
                    if (m_arvalid && m_arready) begin
                        m_arvalid <= 1'b0;
                    end
                    if (m_rvalid) begin
                        s_rdata <= m_rdata; s_rid <= r_id; s_rresp <= m_rresp;
                        s_rlast <= (r_left == 8'd0); s_rvalid <= 1'b1;
                        rst_st  <= R_DRAIN;   // fill happens in the write-port block
                    end
                end
                R_DRAIN: if (s_rvalid && s_rready) begin
                    s_rvalid <= 1'b0;
                    if (s_rlast) begin arrdy <= 1'b1; rst_st <= R_IDLE; end
                    else begin
                        r_addr <= r_addr + BYTE_STEP; r_left <= r_left - 8'd1;
                        rst_st <= R_ISSUE;
                    end
                end
                default: rst_st <= R_IDLE;
            endcase
        end
    end

    // ============================ WRITE path (FSM) ===========================
    localparam [1:0] W_IDLE=0, W_AW=1, W_DATA=2, W_RESP=3;
    reg [1:0]        wst;
    reg [ADDR_W-1:0] w_addr;
    reg              w_incr;                          // AWBURST==INCR (cacheable)

    reg awrdy;
    assign s_awready = awrdy && !flushing;
    assign s_wready  = (wst == W_DATA) && m_wready;
    assign m_wdata   = s_wdata;
    assign m_wstrb   = s_wstrb;
    assign m_wlast   = s_wlast;
    assign m_wvalid  = (wst == W_DATA) && s_wvalid;

    always @(posedge clk) begin
        if (!resetn) begin
            wst <= W_IDLE; m_awvalid <= 1'b0; awrdy <= 1'b1;
            s_bvalid <= 1'b0; m_bready <= 1'b0;
        end else begin
            case (wst)
                W_IDLE: begin
                    s_bvalid <= 1'b0;
                    if (s_awvalid && awrdy && !flushing) begin
                        m_awid <= s_awid; m_awaddr <= s_awaddr; m_awlen <= s_awlen;
                        m_awsize <= s_awsize; m_awburst <= s_awburst; m_awvalid <= 1'b1;
                        w_addr <= s_awaddr; w_incr <= (s_awburst == 2'b01);
                        awrdy <= 1'b0; wst <= W_AW;
                    end
                end
                W_AW: if (m_awready) begin m_awvalid <= 1'b0; wst <= W_DATA; end
                W_DATA: if (s_wvalid && s_wready) begin
                    w_addr <= w_addr + BYTE_STEP;
                    if (s_wlast) begin m_bready <= 1'b1; wst <= W_RESP; end
                end
                W_RESP: if (m_bvalid) begin
                    s_bid <= m_bid; s_bresp <= m_bresp; s_bvalid <= 1'b1;
                    m_bready <= 1'b0; awrdy <= 1'b1; wst <= W_IDLE;
                end
                default: wst <= W_IDLE;
            endcase
            if (s_bvalid && s_bready) begin
                s_bvalid <= 1'b0;
            end
        end
    end

    // Reset flush: clear valid on every set before serving (URAM cannot init).
    always @(posedge clk) begin
        if (!resetn) begin flushing <= 1'b1; flush_idx <= {SET_W{1'b0}}; end
        else if (flushing) begin
            flush_idx <= flush_idx + 1'b1;
            if (flush_idx == (SETS-1)) begin
                flushing <= 1'b0;
            end
        end
    end

    // Single write port: flush clears valid, else alloc (write beat) or fill (read
    // miss). Row = {valid, tag, data}. A collision drops the loser -- safe: write-
    // through keeps the MIG authoritative.
    wire alloc_en = (wst == W_DATA) && s_wvalid && s_wready;
    wire fill_en  = (rst_st == R_FETCH) && m_rvalid;
    wire             ram_we    = flushing || alloc_en || fill_en;
    wire [SET_W-1:0] ram_waddr = flushing ? flush_idx
                               : alloc_en ? idx_of(w_addr) : ridx_q;
    // AXI audit fix: a PARTIAL-strobe write must not allocate a valid line -- the
    // un-strobed bytes are stale, a later hit would serve garbage. Full write
    // allocates (valid=1); partial invalidates the set (valid=0); MIG still gets
    // the strobed write, so a subsequent read misses and fetches correct data.
    wire w_full = &s_wstrb;
    wire [ROW_W-1:0] ram_wdata = flushing ? {ROW_W{1'b0}}
                               : alloc_en ? {w_full & w_incr, tag_of(w_addr), s_wdata}
                                          : {1'b1, exptag_q, m_rdata};

    kohaku_sdpram #(.WIDTH(ROW_W), .DEPTH(SETS), .MEM_PRIM(RAM_STYLE),
                    .READ_LAT(RD_LAT)) u_arr (
        .clk(clk),
        .wr_en(ram_we), .wr_addr(ram_waddr), .wr_data(ram_wdata),
        .rd_en(rd_en),  .rd_addr(rd_idx),    .rd_data(rd_row)
    );

endmodule

`default_nettype wire
