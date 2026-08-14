// The mover as it stood before bursts: one AXI transaction per 32-byte word,
// one in flight, and each word's write finished before the next word's read.

// Kept ONLY so mm_mover_bw_tb can measure before and after in one run, against
// one memory model. Nothing instantiates it and nothing should.

`default_nettype none

module mm_mover_v1 #(
    parameter integer DATA_W    = 256,
    parameter integer ADDR_W    = 40,
    parameter integer ID_W      = 4,
    parameter integer IDX_WORDS = 256
)(
    input  wire                clk,
    input  wire                resetn,

    input  wire                cfg_en,
    input  wire [7:0]          cfg_addr,
    input  wire [63:0]         cfg_data,

    output wire                stat_busy,
    output reg  [3:0]          stat_fault,
    output reg  [31:0]         stat_done,

    output reg  [ID_W-1:0]     m_awid,
    output reg  [ADDR_W-1:0]   m_awaddr,
    output wire [7:0]          m_awlen,
    output wire [2:0]          m_awsize,
    output wire [1:0]          m_awburst,
    output reg                 m_awvalid,
    input  wire                m_awready,
    output reg  [DATA_W-1:0]   m_wdata,
    output wire [DATA_W/8-1:0] m_wstrb,
    output wire                m_wlast,
    output reg                 m_wvalid,
    input  wire                m_wready,
    input  wire [ID_W-1:0]     m_bid,
    input  wire [1:0]          m_bresp,
    input  wire                m_bvalid,
    output wire                m_bready,
    output reg  [ID_W-1:0]     m_arid,
    output reg  [ADDR_W-1:0]   m_araddr,
    output wire [7:0]          m_arlen,
    output wire [2:0]          m_arsize,
    output wire [1:0]          m_arburst,
    output reg                 m_arvalid,
    input  wire                m_arready,
    input  wire [ID_W-1:0]     m_rid,
    input  wire [DATA_W-1:0]   m_rdata,
    input  wire [1:0]          m_rresp,
    input  wire                m_rlast,
    input  wire                m_rvalid,
    output wire                m_rready
);
    localparam [2:0] MODE_COPY = 3'd0, MODE_TRANSPOSE = 3'd1,
                     MODE_GATHER = 3'd2, MODE_GENERATE = 3'd3, MODE_FILL = 3'd4;

    localparam [3:0] F_NONE = 4'd0, F_IDXLEN = 4'd1, F_RANGE = 4'd2,
                     F_AXI = 4'd3, F_MODE = 4'd4, F_EWIDTH = 4'd5,
                     F_ALIGN = 4'd6;

    localparam [4:0] S_IDLE = 5'd0,  S_IXA = 5'd1,  S_IXD = 5'd2,
                     S_GO   = 5'd3,  S_RD  = 5'd4,  S_RDD = 5'd5,
                     S_GEN  = 5'd6,  S_WR  = 5'd7,  S_WRB = 5'd8,
                     S_STEP = 5'd9,  S_DONE = 5'd10, S_FAULT = 5'd11,
                     S_IXW  = 5'd12, S_GADR = 5'd13, S_GADR2 = 5'd14,
                     S_GADR3 = 5'd15, S_LAT = 5'd16;

    reg [2:0]  mode;
    reg [1:0]  ewidth;
    reg [7:0]  flags;
    reg [63:0] seed;
    reg [31:0] imm;
    reg [ADDR_W-1:0] idx_base, dst_base, d_src_base;
    reg [15:0] idx_count;

    reg [ADDR_W-1:0] cur_rd, cur_wr;
    reg              cur_rv, cur_wv;
    reg [31:0] gath_pitch;
    reg [15:0] gath_words;

    reg        ld_sel;
    reg [2:0]  ld_dim;
    reg [15:0] ld_count;
    reg signed [31:0] ld_stride;

    reg        d_hdr_en, d_dim_en, d_ax_en;
    reg [1:0]  d_axis;
    reg signed [15:0] d_astep;
    reg [ADDR_W-1:0]  d_base;
    reg [2:0]  d_ndim;
    reg        d_ax_sel;
    reg signed [15:0] d_abase;
    reg [15:0] d_aext;

    reg [4:0]  st;
    reg        go;

    wire [7:0] reg_sel = {cfg_addr[7:3], 3'b000};

    wire [ADDR_W-1:0] src_addr, dst_addr;
    wire src_last, dst_last, src_valid, dst_valid, src_active, dst_active;

    wire desc_start = (st == S_GO);
    wire desc_next  = (st == S_STEP) && !dst_last;

    mx_tdesc #(.NDIM(6), .AW(ADDR_W), .CW(16), .SW(32), .XW(16)) u_src (
        .clk(clk), .rst(!resetn),
        .ld_dim_en(d_dim_en && (ld_sel == 1'b0)), .ld_dim(ld_dim),
        .ld_count(ld_count), .ld_stride(ld_stride),
        .ld_axis(d_axis), .ld_astep(d_astep),
        .ld_hdr_en(d_hdr_en && (ld_sel == 1'b0)), .ld_base(d_base),
        .ld_ndim(d_ndim),
        .ld_ax_en(d_ax_en && (ld_sel == 1'b0)), .ld_ax_sel(d_ax_sel),
        .ld_abase(d_abase), .ld_aext(d_aext),
        .start(desc_start), .next(desc_next),
        .active(src_active), .last(src_last), .valid(src_valid), .addr(src_addr)
    );

    mx_tdesc #(.NDIM(6), .AW(ADDR_W), .CW(16), .SW(32), .XW(16)) u_dst (
        .clk(clk), .rst(!resetn),
        .ld_dim_en(d_dim_en && (ld_sel == 1'b1)), .ld_dim(ld_dim),
        .ld_count(ld_count), .ld_stride(ld_stride),
        .ld_axis(d_axis), .ld_astep(d_astep),
        .ld_hdr_en(d_hdr_en && (ld_sel == 1'b1)), .ld_base(d_base),
        .ld_ndim(d_ndim),
        .ld_ax_en(d_ax_en && (ld_sel == 1'b1)), .ld_ax_sel(d_ax_sel),
        .ld_abase(d_abase), .ld_aext(d_aext),
        .start(desc_start), .next(desc_next),
        .active(dst_active), .last(dst_last), .valid(dst_valid), .addr(dst_addr)
    );

    reg  [7:0]   ix_waddr, ix_raddr, ix_wr_a;
    reg  [255:0] ix_data;
    reg          ix_we;
    wire [255:0] ix_q;
    kohaku_sdpram #(.WIDTH(256), .DEPTH(IDX_WORDS), .MEM_PRIM("block"),
                    .READ_LAT(1)) u_ixbuf (
        .clk(clk), .wr_en(ix_we), .wr_addr(ix_wr_a), .wr_data(ix_data),
        .rd_en(1'b1), .rd_addr(ix_raddr), .rd_data(ix_q)
    );

    reg [15:0] ix_got;
    reg [15:0] g_row, g_word;
    wire [2:0] g_lane = g_row[2:0];
    wire [31:0] g_index = ix_q[g_lane*32 +: 32];

    reg          pr_start;
    reg  [127:0] pr_ctr;
    reg          pr_half;
    reg  [127:0] pr_lo;
    wire         pr_busy, pr_valid;
    wire [127:0] pr_out;

    mm_prng #(.ROUNDS(10)) u_prng (
        .clk(clk), .rst(!resetn),
        .start(pr_start), .key_in(seed), .ctr_in(pr_ctr),
        .busy(pr_busy), .out_valid(pr_valid), .out(pr_out)
    );

    // Same 35-bit word address and counter layout as the current mover, so a
    // GENERATE comparison between the two is a comparison and not a diff.
    wire [ADDR_W-6:0] wpos = cur_wr[ADDR_W-1:5];
    wire [127:0] gctr_lo = {{(132-ADDR_W){1'b0}}, wpos[ADDR_W-6:32],
                            1'b0, wpos[31:0]};
    wire [127:0] gctr_hi = {{(132-ADDR_W){1'b0}}, wpos[ADDR_W-6:32],
                            1'b1, wpos[31:0]};

    reg [DATA_W-1:0] fill_word;
    integer fi;
    always @(*) begin
        fill_word = {DATA_W{1'b0}};
        for (fi = 0; fi < 32; fi = fi + 1) begin
            case (ewidth)
                2'd0: fill_word[fi*8 +: 8]   = imm[7:0];
                2'd1: if (fi < 16) fill_word[fi*16 +: 16] = imm[15:0];
                default: if (fi < 8) fill_word[fi*32 +: 32] = imm[31:0];
            endcase
        end
    end

    reg [DATA_W-1:0] data;

    assign m_awlen   = 8'd0;
    assign m_arlen   = 8'd0;
    assign m_awsize  = 3'd5;
    assign m_arsize  = 3'd5;
    assign m_awburst = 2'b01;
    assign m_arburst = 2'b01;
    assign m_wstrb   = {(DATA_W/8){1'b1}};
    assign m_wlast   = 1'b1;
    assign m_bready  = 1'b1;
    assign m_rready  = 1'b1;
    assign stat_busy = (st != S_IDLE);

    wire needs_read = (mode == MODE_COPY) || (mode == MODE_TRANSPOSE)
                   || (mode == MODE_GATHER);
    reg [31:0]       idx_r, prod_r;
    reg [ADDR_W-1:0] row_base;

    wire [ADDR_W-1:0] gath_addr =
        row_base + {{(ADDR_W-21){1'b0}}, g_word[15:0], 5'd0};

    always @(posedge clk) begin
        if (!resetn) begin
            st <= S_IDLE; go <= 1'b0;
            mode <= 3'd0; ewidth <= 2'd1; flags <= 8'd0;
            seed <= 64'd0; imm <= 32'd0;
            idx_base <= {ADDR_W{1'b0}}; idx_count <= 16'd0;
            dst_base <= {ADDR_W{1'b0}}; d_src_base <= {ADDR_W{1'b0}};
            gath_pitch <= 32'd0; gath_words <= 16'd1;
            d_hdr_en <= 1'b0; d_dim_en <= 1'b0; d_ax_en <= 1'b0;
            ld_sel <= 1'b0; ld_dim <= 3'd0; ld_count <= 16'd1; ld_stride <= 32'd0;
            d_axis <= 2'd0; d_astep <= 16'd0; d_base <= {ADDR_W{1'b0}};
            d_ndim <= 3'd1; d_ax_sel <= 1'b0; d_abase <= 16'd0; d_aext <= 16'd0;
            m_awvalid <= 1'b0; m_wvalid <= 1'b0; m_arvalid <= 1'b0;
            m_awaddr <= {ADDR_W{1'b0}}; m_araddr <= {ADDR_W{1'b0}};
            m_awid <= {ID_W{1'b0}}; m_arid <= {ID_W{1'b0}};
            m_wdata <= {DATA_W{1'b0}};
            stat_fault <= F_NONE; stat_done <= 32'd0;
            ix_we <= 1'b0; ix_waddr <= 8'd0; ix_raddr <= 8'd0; ix_got <= 16'd0;
            ix_wr_a <= 8'd0; ix_data <= 256'd0;
            row_base <= {ADDR_W{1'b0}}; idx_r <= 32'd0; prod_r <= 32'd0;
            cur_rd <= {ADDR_W{1'b0}}; cur_wr <= {ADDR_W{1'b0}};
            cur_rv <= 1'b0; cur_wv <= 1'b0;
            g_row <= 16'd0; g_word <= 16'd0;
            pr_start <= 1'b0; pr_ctr <= 128'd0; pr_half <= 1'b0;
            pr_lo <= 128'd0; data <= {DATA_W{1'b0}};
        end else begin
            d_hdr_en <= 1'b0; d_dim_en <= 1'b0; d_ax_en <= 1'b0;
            ix_we <= 1'b0; pr_start <= 1'b0; go <= 1'b0;
            idx_r    <= g_index;
            prod_r   <= idx_r * gath_pitch;
            row_base <= d_src_base + {{(ADDR_W-32){1'b0}}, prod_r};

            if (cfg_en) begin
                case (reg_sel)
                8'h00: begin
                    mode   <= cfg_data[2:0];
                    ewidth <= cfg_data[4:3];
                    flags  <= cfg_data[15:8];
                    go     <= cfg_data[16];
                end
                8'h10: begin
                    ld_sel   <= cfg_data[0];
                    d_base   <= cfg_data[4 +: ADDR_W];
                    d_ndim   <= cfg_data[46:44];
                    d_hdr_en <= 1'b1;
                    if (cfg_data[0]) dst_base   <= cfg_data[4 +: ADDR_W];
                    else             d_src_base <= cfg_data[4 +: ADDR_W];
                end
                8'h18: begin
                    ld_sel    <= cfg_data[0];
                    ld_dim    <= cfg_data[3:1];
                    ld_count  <= cfg_data[19:4];
                    ld_stride <= cfg_data[51:20];
                end
                8'h20: begin
                    d_axis   <= cfg_data[1:0];
                    d_astep  <= cfg_data[17:2];
                    d_dim_en <= 1'b1;
                end
                8'h28: begin
                    ld_sel   <= cfg_data[0];
                    d_ax_sel <= cfg_data[1];
                    d_abase  <= cfg_data[17:2];
                    d_aext   <= cfg_data[33:18];
                    d_ax_en  <= 1'b1;
                end
                8'h30: begin
                    idx_base  <= cfg_data[ADDR_W-1:0];
                    idx_count <= cfg_data[55:40];
                end
                8'h38: seed <= cfg_data;
                8'h40: imm  <= cfg_data[31:0];
                8'h50: begin
                    gath_pitch <= cfg_data[31:0];
                    gath_words <= cfg_data[47:32];
                end
                default: ;
                endcase
            end

            case (st)
            S_IDLE: if (go) begin
                stat_fault <= F_NONE;
                g_row <= 16'd0; g_word <= 16'd0;
                ix_got <= 16'd0; ix_waddr <= 8'd0; ix_raddr <= 8'd0;
                pr_half <= 1'b0;
                if (ewidth == 2'd3) begin
                    stat_fault <= F_EWIDTH; st <= S_FAULT;
                end else if (mode == MODE_TRANSPOSE) begin
                    stat_fault <= F_MODE; st <= S_FAULT;
                end else if (mode == MODE_GATHER) begin
                    if (idx_count > {IDX_WORDS[12:0], 3'd0}) begin
                        stat_fault <= F_IDXLEN; st <= S_FAULT;
                    end else begin
                        m_araddr  <= idx_base;
                        m_arvalid <= 1'b1;
                        st <= S_IXA;
                    end
                end else begin
                    st <= S_GO;
                end
            end

            S_IXA: if (m_arvalid && m_arready) begin
                m_arvalid <= 1'b0;
                st <= S_IXD;
            end
            S_IXD: if (m_rvalid) begin
                if (m_rresp != 2'b00) begin
                    stat_fault <= F_AXI; st <= S_FAULT;
                end else begin
                    ix_we    <= 1'b1;
                    ix_wr_a  <= ix_waddr;
                    ix_data  <= m_rdata;
                    ix_got   <= ix_got + 16'd8;
                    ix_waddr <= ix_waddr + 8'd1;
                    if (ix_got + 16'd8 >= idx_count) begin
                        st <= S_IXW;
                    end else begin
                        m_araddr  <= m_araddr + {{(ADDR_W-6){1'b0}}, 6'd32};
                        m_arvalid <= 1'b1;
                        st <= S_IXA;
                    end
                end
            end

            S_IXW: st <= S_GO;

            S_GO: begin
                ix_raddr <= 8'd0;
                st <= (mode == MODE_GATHER) ? S_GADR : S_LAT;
            end

            S_GADR:  st <= S_GADR2;
            S_GADR2: st <= S_GADR3;
            S_GADR3: st <= S_LAT;

            S_LAT: begin
                cur_rd <= (mode == MODE_GATHER) ? gath_addr : src_addr;
                cur_rv <= (mode == MODE_GATHER) ? 1'b1      : src_valid;
                cur_wr <= dst_addr;
                cur_wv <= dst_valid;
                st <= S_RD;
            end

            S_RD: begin
                if (mode == MODE_FILL) begin
                    data <= fill_word;
                    st <= S_WR;
                end else if (mode == MODE_GENERATE) begin
                    pr_ctr   <= pr_half ? gctr_hi : gctr_lo;
                    pr_start <= 1'b1;
                    st <= S_GEN;
                end else if (needs_read && !cur_rv) begin
                    data <= fill_word;
                    st <= S_WR;
                end else begin
                    m_araddr  <= cur_rd;
                    m_arvalid <= 1'b1;
                    st <= S_RDD;
                end
            end

            S_RDD: begin
                if (m_arvalid && m_arready) m_arvalid <= 1'b0;
                if (m_rvalid) begin
                    if (m_rresp != 2'b00) begin
                        stat_fault <= F_AXI; st <= S_FAULT;
                    end else begin
                        data <= m_rdata;
                        st <= S_WR;
                    end
                end
            end

            S_GEN: if (pr_valid) begin
                if (!pr_half) begin
                    pr_lo   <= pr_out;
                    pr_half <= 1'b1;
                    pr_ctr   <= gctr_hi;
                    pr_start <= 1'b1;
                end else begin
                    data    <= {pr_out, pr_lo};
                    pr_half <= 1'b0;
                    st <= S_WR;
                end
            end

            S_WR: begin
                if (!cur_wv) begin
                    st <= S_STEP;
                end else if (|cur_wr[4:0]) begin
                    stat_fault <= F_ALIGN; st <= S_FAULT;
                end else begin
                    m_awaddr  <= cur_wr;
                    m_awvalid <= 1'b1;
                    m_wdata   <= data;
                    m_wvalid  <= 1'b1;
                    st <= S_WRB;
                end
            end

            S_WRB: begin
                if (m_awvalid && m_awready) m_awvalid <= 1'b0;
                if (m_wvalid  && m_wready)  m_wvalid  <= 1'b0;
                if (m_bvalid) begin
                    if (m_bresp != 2'b00) begin
                        stat_fault <= F_AXI; st <= S_FAULT;
                    end else st <= S_STEP;
                end
            end

            S_STEP: begin
                if (dst_last) begin
                    st <= S_DONE;
                end else begin
                    if (mode == MODE_GATHER) begin
                        if (g_word + 16'd1 == gath_words) begin
                            g_word   <= 16'd0;
                            g_row    <= g_row + 16'd1;
                            ix_raddr <= (g_row + 16'd1) >> 3;
                        end else g_word <= g_word + 16'd1;
                        st <= S_GADR;
                    end else st <= S_LAT;
                end
            end

            S_DONE: begin
                stat_done <= stat_done + 32'd1;
                st <= S_IDLE;
            end

            S_FAULT: st <= S_IDLE;
            default: st <= S_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
