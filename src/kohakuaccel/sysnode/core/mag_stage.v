// Memory-agent staging: the MAG L2, reached by ADDRESS through special
// aperture 0. See docs/address-map.md.

// MESH FIRST, THEN APERTURE. A packet only transiting this mesh fails the mesh
// test, so it is neither ours nor a fault and passes on untouched.

// THE LINE IS ONE FILL ENTRY, 1024 bits, not mag-staging.md's 936: 936 is the
// post-permutation L1 width, and MAG moves mag_mem_port.v's P_ENTRY_BITS.

// BANKED, because URAM columns are spread across a die whose worst SLR is at
// 95.80% CLB. BANKS=1 with PIPE=0 is the monolithic shape, for comparison.

// WIDE PATH ONE DRIVER, NARROW PATH MANY. Write data is one registered
// broadcast; only the address and control fan out per bank.

`default_nettype none

module mag_stage #(
    parameter integer DATA_W  = 256,
    parameter integer ADDR_W  = 40,
    parameter integer WORDS   = 4,         // response words in one entry
    // SHIPPED: 4 banks x 4096 rows = 64 URAM, 2 MB. A bank shallower than 4096
    // wastes URAM depth, so banking multiplies capacity rather than dividing it.
    parameter integer BANKS   = 4,         // 1 is monolithic
    parameter integer ENTRIES = 16384,     // total, across all banks
    // 1 registers the dispatch and each bank's output, so the only long wire
    // left is bank output register -> mux.
    parameter integer PIPE    = 1,
    parameter [1:0]   MESH_ID = 2'd0,
    parameter [3:0]   AP_STAGE = 4'h0,
    // What the ARCHITECTURE defines (address-space-40bit.md s3), not what any
    // port serves: no permission decision, only a_fault. mag_mem_port serves 0.
    parameter [15:0]  AP_IMPL  = 16'h0037
)(
    input  wire                    clk,
    input  wire                    rst,

    // ---- port A: the agent's operand path, one whole entry per access ----
    input  wire                    a_req,
    input  wire                    a_we,
    input  wire [ADDR_W-1:0]       a_addr,
    input  wire [WORDS*DATA_W-1:0] a_wdata,
    output wire                    a_mine,
    output wire                    a_gnt,
    output wire                    a_fault,
    output wire                    a_rvalid,
    output wire [WORDS*DATA_W-1:0] a_rdata,

    // ---- port B: the host window, one word per access --------------------
    input  wire                    b_req,
    input  wire                    b_we,
    input  wire [ADDR_W-1:0]       b_addr,
    input  wire [DATA_W-1:0]       b_wdata,
    output wire                    b_mine,
    output wire                    b_gnt,
    output wire                    b_rvalid,
    output wire [DATA_W-1:0]       b_rdata
);
    localparam integer LSB  = $clog2(DATA_W/8);              // word, in bytes
    localparam integer EW   = (WORDS <= 1) ? 1 : $clog2(WORDS);
    localparam integer EB   = LSB + EW;                      // entry, in bytes
    localparam integer BWS  = (BANKS <= 1) ? 1 : $clog2(BANKS);
    localparam integer ROWS = ENTRIES / BANKS;
    localparam integer RW   = (ROWS <= 1) ? 1 : $clog2(ROWS);
    localparam integer RLAT = 2;                             // URAM read stages
    localparam integer RTOT = (PIPE != 0) ? (RLAT + 2) : RLAT;

    // ---- the decode, in the order that makes transit safe ------------------
    wire a_mesh = (a_addr[37:36] == MESH_ID) && !a_addr[38];
    wire b_mesh = (b_addr[37:36] == MESH_ID) && !b_addr[38];
    wire [3:0] a_ap = a_addr[35:32];
    wire [3:0] b_ap = b_addr[35:32];

    assign a_mine = a_mesh && a_addr[39] && (a_ap == AP_STAGE);
    assign b_mine = b_mesh && b_addr[39] && (b_ap == AP_STAGE);

    assign a_fault = a_req && a_mesh && a_addr[39] && !AP_IMPL[a_ap];

    // BANK INTERLEAVE ON THE LOW BITS, so a sequential fill spreads across the
    // banks instead of loading one.
    wire [BWS-1:0] a_bank = (BANKS <= 1) ? {BWS{1'b0}} : a_addr[EB     +: BWS];
    wire [BWS-1:0] b_bank = (BANKS <= 1) ? {BWS{1'b0}} : b_addr[EB     +: BWS];
    wire [RW-1:0]  a_row  = (BANKS <= 1) ? a_addr[EB   +: RW]
                                         : a_addr[EB+BWS +: RW];
    wire [RW-1:0]  b_row  = (BANKS <= 1) ? b_addr[EB   +: RW]
                                         : b_addr[EB+BWS +: RW];
    wire [EW-1:0]  b_word = b_addr[LSB +: EW];

    // A wins, and B goes only when it wants the OTHER port -- not merely another
    // bank: the write data has one driver and the return select one register.
    wire a_go = a_req && a_mine;
    wire b_go = b_req && b_mine && !(a_go && (a_we == b_we));

    assign a_gnt = a_go;
    assign b_gnt = b_go;

    // ---- dispatch: narrow, per bank; wide, one driver ----------------------
    wire [BANKS-1:0] a_hit, b_hit;
    genvar g, w;
    generate
    for (g = 0; g < BANKS; g = g + 1) begin : g_sel
        assign a_hit[g] = a_go && (a_bank == g[BWS-1:0]);
        assign b_hit[g] = b_go && (b_bank == g[BWS-1:0]);
    end
    endgenerate

    wire [WORDS*DATA_W-1:0] wide_d = a_we ? a_wdata : {WORDS{b_wdata}};

    // PIPE picks which set the banks see. GENERATED, not muxed: a ternary on a
    // parameter still infers the registers, and 1,024 dead FF is not a nothing.
    wire [BANKS-1:0] d_aw, d_ar, d_bw, d_br;
    wire [RW-1:0]    d_ar_row, d_br_row;
    wire [EW-1:0]    d_bword;
    wire [BANKS*WORDS*DATA_W-1:0] d_wide;   // one slice per bank

    generate if (PIPE != 0) begin : g_disp
        // One copy per bank: as a single register this was the worst data path,
        // 4.860 ns at 98.4% route and ZERO logic levels. DONT_TOUCH or merged.
        (* DONT_TOUCH = "yes" *)
        reg  [BANKS*WORDS*DATA_W-1:0] wide_q;
        reg  [BANKS-1:0]        aw_q, ar_q, bw_q, br_q;
        reg  [RW-1:0]           arow_q, brow_q;
        reg  [EW-1:0]           bword_q;

        always @(posedge clk) begin
            if (rst) begin
                aw_q <= {BANKS{1'b0}}; ar_q <= {BANKS{1'b0}};
                bw_q <= {BANKS{1'b0}}; br_q <= {BANKS{1'b0}};
            end else begin
                aw_q <= a_hit & {BANKS{a_we}};
                ar_q <= a_hit & {BANKS{~a_we}};
                bw_q <= b_hit & {BANKS{b_we}};
                br_q <= b_hit & {BANKS{~b_we}};
                arow_q <= a_row; brow_q <= b_row; bword_q <= b_word;
                wide_q <= {BANKS{wide_d}};
            end
        end
        assign d_aw = aw_q; assign d_ar = ar_q;
        assign d_bw = bw_q; assign d_br = br_q;
        assign d_ar_row = arow_q; assign d_br_row = brow_q;
        assign d_bword = bword_q; assign d_wide = wide_q;
    end else begin : g_nodisp
        assign d_aw = a_hit & {BANKS{a_we}};
        assign d_ar = a_hit & {BANKS{~a_we}};
        assign d_bw = b_hit & {BANKS{b_we}};
        assign d_br = b_hit & {BANKS{~b_we}};
        assign d_ar_row = a_row; assign d_br_row = b_row;
        assign d_bword = b_word; assign d_wide = {BANKS{wide_d}};
    end endgenerate

    // ---- the banks ---------------------------------------------------------
    wire [BANKS*WORDS*DATA_W-1:0] bank_rd;
    wire [BANKS*WORDS*DATA_W-1:0] bank_out;

    generate
    for (g = 0; g < BANKS; g = g + 1) begin : g_bank
        wire            bk_awr = d_aw[g];
        wire [RW-1:0]   bk_wrow = bk_awr ? d_ar_row : d_br_row;
        wire [RW-1:0]   bk_rrow = d_ar[g] ? d_ar_row : d_br_row;

        for (w = 0; w < WORDS; w = w + 1) begin : g_word
            // A writes every word of its entry; the host writes the one its
            // address names.
            wire bk_we = bk_awr || (d_bw[g] && (d_bword == w[EW-1:0]));
            kohaku_sdpram #(.WIDTH(DATA_W), .DEPTH(ROWS), .MEM_PRIM("ultra"),
                            .READ_LAT(RLAT)) u_bank (
                .clk(clk),
                .wr_en(bk_we), .wr_addr(bk_wrow),
                .wr_data(d_wide[(g*WORDS + w)*DATA_W +: DATA_W]),
                .rd_en(1'b1), .rd_addr(bk_rrow),
                .rd_data(bank_rd[(g*WORDS + w)*DATA_W +: DATA_W])
            );
        end

        // The bank's own output register, so the only long wire in the design
        // is this register to the return mux.
        if (PIPE != 0) begin : g_oreg
            reg [WORDS*DATA_W-1:0] out_q;
            always @(posedge clk) begin
                out_q <= bank_rd[g*WORDS*DATA_W +: WORDS*DATA_W];
            end
            assign bank_out[g*WORDS*DATA_W +: WORDS*DATA_W] = out_q;
        end else begin : g_odir
            assign bank_out[g*WORDS*DATA_W +: WORDS*DATA_W] =
                bank_rd[g*WORDS*DATA_W +: WORDS*DATA_W];
        end
    end
    endgenerate

    // ---- return: which bank, and when --------------------------------------
    // Fixed at RTOT and cannot stall: the caller leads and takes `rvalid`.
    reg [RTOT-1:0] a_sr, b_sr;
    reg [BWS-1:0]  bk_sr [0:RTOT-1];
    reg [EW-1:0]   wd_sr [0:RTOT-1];

    integer i;
    always @(posedge clk) begin
        if (rst) begin
            a_sr <= {RTOT{1'b0}};
            b_sr <= {RTOT{1'b0}};
        end else begin
            a_sr <= {a_sr[RTOT-2:0], (a_go && !a_we)};
            b_sr <= {b_sr[RTOT-2:0], (b_go && !b_we)};
            bk_sr[0] <= (a_go && !a_we) ? a_bank : b_bank;
            wd_sr[0] <= b_word;
            for (i = 1; i < RTOT; i = i + 1) begin
                bk_sr[i] <= bk_sr[i-1];
                wd_sr[i] <= wd_sr[i-1];
            end
        end
    end

    wire [BWS-1:0] sel = bk_sr[RTOT-1];
    wire [WORDS*DATA_W-1:0] sel_entry =
        bank_out[sel*WORDS*DATA_W +: WORDS*DATA_W];

    assign a_rvalid = a_sr[RTOT-1];
    assign a_rdata  = sel_entry;
    assign b_rvalid = b_sr[RTOT-1];
    assign b_rdata  = sel_entry[wd_sr[RTOT-1]*DATA_W +: DATA_W];

`ifndef SYNTHESIS
    // An unaligned entry access reads a line the address does not name, and
    // there is no other symptom: the data is simply someone else's.
    always @(posedge clk) if (!rst) begin
        if (a_go && (a_addr[EB-1:0] != {EB{1'b0}})) begin
            $display("%0t ERROR mag_stage: port A address %h is not entry-aligned -- an entry is %0d bytes",
                     $time, a_addr, (WORDS*DATA_W)/8);
        end
        if (a_fault) begin
            $display("%0t ERROR mag_stage: address %h names aperture %0d, which is reserved -- faulting rather than aliasing onto DRAM",
                     $time, a_addr, a_ap);
        end
        if (b_req && b_mine && !b_go) begin
            $display("%0t ERROR mag_stage: host access at %h lost its cycle to the agent -- port B must retry, not assume",
                     $time, b_addr);
        end
    end
`endif

endmodule

`default_nettype wire
