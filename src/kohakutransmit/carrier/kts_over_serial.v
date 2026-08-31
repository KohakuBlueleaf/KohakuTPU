// A surface carried by a word stream -- a transceiver's user interface, a
// cable, a lossy channel model. Every flit and every credit count leaves as a
// frame: a header word, the payload words, a checksum word. With RELIABLE
// the frames carry a sequence number, the sender keeps them until they are
// acknowledged and replays from the oldest unacknowledged when the far end
// goes quiet (go-back-N); the receiver takes only the frame it expects and
// acknowledges every frame it takes. Without it the frames are fire-and-forget
// on a lossless carrier.
//
//   header word (CW >= 64):  [7:0] 8'hA5  [9:8] type (0 flit, 1 credit, 2 ack)
//     [10] last  [14:11] vc  [22:15] seq  [26:23] credit count  [34:27] words
//   payload: ceil(W / CW) words for a flit, none otherwise
//   trailer: the INVERTED XOR of every word before it, carried with the
//     carrier's `last`; the word after a `last` is a header. A frame that
//     loses a word ends at the next `last` with a bad sum and is dropped
//     whole, and the frame after it parses cleanly.

`default_nettype none

module kts_over_serial #(
    parameter integer W        = 288,
    parameter integer VC       = 2,
    parameter integer D        = 32,
    parameter integer CN_W     = 4,
    parameter integer CW       = 64,               // carrier word
    parameter integer RELIABLE = 1,
    parameter integer WIN      = 32,               // frames kept for replay (power of 2)
    parameter integer TIMEOUT  = 512,              // cycles without an ack before replay
    parameter integer DEPTH    = (VC * D < 16) ? 16 : VC * D,
    parameter         MEM      = "distributed",
    parameter integer VCW      = (VC <= 1) ? 1 : $clog2(VC),
    parameter integer CRW      = $clog2(D) + 1
)(
    input  wire              clk,
    input  wire              rst,

    input  wire              i_valid,
    input  wire [VCW-1:0]    i_vc,
    input  wire              i_last,
    input  wire [W-1:0]      i_flit,
    output reg               o_valid,
    output reg  [VCW-1:0]    o_vc,
    output reg               o_last,
    output reg  [W-1:0]      o_flit,
    input  wire              i_crd_valid,
    input  wire [VCW-1:0]    i_crd_vc,
    input  wire [CN_W-1:0]   i_crd_n,
    output reg               o_crd_valid,
    output reg  [VCW-1:0]    o_crd_vc,
    output reg  [CN_W-1:0]   o_crd_n,

    output reg               c_tx_valid,
    input  wire              c_tx_ready,
    output reg  [CW-1:0]     c_tx_data,
    output reg               c_tx_last,
    input  wire              c_rx_valid,
    input  wire [CW-1:0]     c_rx_data,
    input  wire              c_rx_last
);
    localparam integer PW   = (W + CW - 1) / CW;           // payload words per flit
    localparam integer PWW  = (PW <= 1) ? 1 : $clog2(PW + 1);
    localparam integer SW   = 8;
    localparam integer WW   = $clog2(WIN);
    localparam integer EW   = 2 + 1 + VCW + CN_W + W;       // a retained frame
    localparam integer TW   = $clog2(TIMEOUT + 1);
    localparam [CN_W-1:0] CN_MAX = {CN_W{1'b1}};
    localparam [VCW-1:0]  VC_C   = VC;
    localparam [1:0] T_FLIT = 2'd0, T_CRD = 2'd1, T_ACK = 2'd2;

    // ---- what waits to go: flits, credit counts, an ack -----------------------
    wire             f_full, f_empty, f_pop;
    wire [VCW+W:0]   f_rd;
    kts_fifo #(.W(VCW + 1 + W), .DEPTH(DEPTH), .MEM(MEM)) u_f (
        .clk(clk), .rst(rst),
        .wr_en(i_valid), .wr_data({i_vc, i_last, i_flit}), .full(f_full),
        .rd_en(f_pop), .rd_data(f_rd), .empty(f_empty)
    );

    reg  [CRW-1:0] acc [0:VC-1];
    reg  [VCW-1:0] rr;
    wire [VC-1:0]  due;
    genvar g;
    generate
    for (g = 0; g < VC; g = g + 1) begin : g_due
        assign due[g] = (acc[g] != {CRW{1'b0}});
    end
    endgenerate
    wire [2*VC-1:0] dbl = {due, due} >> rr;
    wire [VC-1:0]   rot = dbl[VC-1:0];
    wire [VC-1:0]   low = rot & (~rot + {{(VC-1){1'b0}}, 1'b1});
    reg  [VCW-1:0]  low_ix;
    integer i;
    always @(*) begin
        low_ix = {VCW{1'b0}};
        for (i = 0; i < VC; i = i + 1) begin
            if (low[i]) begin
                low_ix = low_ix | i[VCW-1:0];
            end
        end
    end
    wire [VCW:0]    sum   = {1'b0, low_ix} + {1'b0, rr};
    wire [VCW-1:0]  pick  = (sum >= VC) ? (sum[VCW-1:0] - VC_C) : sum[VCW-1:0];
    wire [CRW-1:0]  have  = acc[pick];
    wire [CN_W-1:0] cn    = (have > CN_MAX) ? CN_MAX : have[CN_W-1:0];

    reg           ack_req;                  // an ack is owed to the far end
    reg [SW-1:0]  ack_seq;                  // the last sequence taken
    // from the receiver below: an ack arrived / a frame was taken
    reg           rx_ack, rx_took;
    reg [SW-1:0]  rx_ack_seq, rx_seq;

    // ---- the sender: one frame at a time, retained when RELIABLE -----------------
    reg [SW-1:0]  seq_next;                 // next new frame's sequence
    reg [SW-1:0]  seq_base;                 // oldest unacknowledged
    reg [SW-1:0]  seq_play;                 // replay cursor
    reg           replaying;
    reg [TW-1:0]  quiet;                    // cycles since the far end acknowledged
    reg [1:0]     dups;                     // acks that brought no progress
    wire [SW-1:0] outstanding = seq_next - seq_base;
    wire          room        = (RELIABLE == 0) || (outstanding < WIN[SW-1:0]);

    // retention ring: written as a new frame starts, read as a replay starts
    wire [EW-1:0] ret_rd;
    reg           ret_we;
    reg  [WW-1:0] ret_wa, ret_ra;
    reg  [EW-1:0] ret_wd;
    kts_ram #(.W(EW), .DEPTH(WIN), .MEM("distributed"), .READ_LAT(1)) u_ret (
        .clk(clk),
        .wr_en(ret_we), .wr_addr(ret_wa), .wr_data(ret_wd),
        .rd_en(1'b1), .rd_addr(ret_ra), .rd_data(ret_rd)
    );

    localparam [2:0] S_IDLE = 3'd0, S_FETCH = 3'd1, S_HDR = 3'd2, S_PAY = 3'd3;
    localparam [2:0] S_SUM = 3'd4, S_LAND = 3'd5;
    reg [2:0]      st;
    reg [1:0]      cur_t;
    reg            cur_last;
    reg [VCW-1:0]  cur_vc;
    reg [CN_W-1:0] cur_n;
    reg [W-1:0]    cur_flit;
    reg [SW-1:0]   cur_seq;
    reg            cur_new;                 // a new frame (advances seq_next)
    reg            cur_play;                // a replayed frame (advances seq_play)
    reg [PWW-1:0]  wi;
    reg [CW-1:0]   csum;
    wire           tx_go = c_tx_valid && c_tx_ready;

    wire           start_ack  = ack_req;
    wire           start_crd  = (|due) && room;
    wire           start_flit = !f_empty && room;
    // replay after silence, or on the second ack that brought no progress
    wire           start_play = (RELIABLE != 0) && !replaying && (outstanding != {SW{1'b0}})
                                && ((quiet >= TIMEOUT[TW-1:0]) || (dups >= 2'd2));
    assign f_pop = (st == S_IDLE) && !start_ack && !start_play && !replaying && !start_crd && start_flit;
    wire   crd_pop = (st == S_IDLE) && !start_ack && !start_play && !replaying && start_crd;

    wire [PW*CW-1:0] padded = {{(PW*CW-W){1'b0}}, cur_flit};

    // A SHIFT, NOT `padded[wi*CW +: CW]`: the dynamic part-select builds a
    // barrel select across the whole flit, where a fixed shift is wiring and
    // the registers it costs are free.
    reg [PW*CW-1:0] pay_sr;

    function [CW-1:0] mk_hdr;
        input [1:0] t; input l; input [VCW-1:0] v; input [SW-1:0] s; input [CN_W-1:0] n; input [7:0] words;
        begin
            mk_hdr = {CW{1'b0}};
            mk_hdr[7:0]   = 8'hA5;
            mk_hdr[9:8]   = t;
            mk_hdr[10]    = l;
            mk_hdr[14:11] = v;
            mk_hdr[22:15] = s;
            mk_hdr[26:23] = n;
            mk_hdr[34:27] = words;
        end
    endfunction

    integer v;
    always @(posedge clk) begin
        if (rst) begin
            st <= S_IDLE; c_tx_valid <= 1'b0; c_tx_last <= 1'b0; cur_play <= 1'b0;
            seq_next <= {SW{1'b0}}; seq_base <= {SW{1'b0}}; seq_play <= {SW{1'b0}};
            replaying <= 1'b0; quiet <= {TW{1'b0}}; ret_we <= 1'b0; ack_req <= 1'b0;
            rr <= {VCW{1'b0}}; dups <= 2'd0;
        end
        else begin
            ret_we <= 1'b0;
            if (crd_pop) begin
                rr <= (pick == VC - 1) ? {VCW{1'b0}} : pick + 1'b1;
            end
            case (st)
                S_IDLE: begin
                    if (start_ack) begin
                        cur_t <= T_ACK; cur_last <= 1'b0; cur_vc <= {VCW{1'b0}};
                        cur_n <= {CN_W{1'b0}}; cur_seq <= ack_seq; cur_new <= 1'b0;
                        cur_play <= 1'b0;
                        ack_req <= 1'b0;
                        st <= S_HDR;
                    end
                    else if (start_play || replaying) begin
                        // replay the frame at seq_play from the ring
                        if (seq_play == seq_next) begin
                            replaying <= 1'b0;
                        end
                        else begin
                            replaying <= 1'b1;
                            ret_ra    <= seq_play[WW-1:0];
                            cur_seq   <= seq_play;
                            cur_new   <= 1'b0;
                            cur_play  <= 1'b1;
                            st        <= S_FETCH;
                        end
                        if (start_play) begin
                            seq_play <= seq_base;
                            quiet    <= {TW{1'b0}};
                            dups     <= 2'd0;
                            replaying <= 1'b1;
                            st <= S_IDLE;
                        end
                    end
                    else if (start_crd) begin
                        cur_t <= T_CRD; cur_last <= 1'b0; cur_vc <= pick; cur_n <= cn;
                        cur_seq <= seq_next; cur_new <= 1'b1; cur_play <= 1'b0;
                        st <= S_HDR;
                    end
                    else if (start_flit) begin
                        cur_t <= T_FLIT; cur_vc <= f_rd[W + 1 +: VCW]; cur_last <= f_rd[W];
                        cur_flit <= f_rd[W-1:0]; cur_n <= {CN_W{1'b0}};
                        cur_seq <= seq_next; cur_new <= 1'b1; cur_play <= 1'b0;
                        st <= S_HDR;
                    end
                end
                S_FETCH: begin
                    // the ring's address was registered last cycle; its data
                    // lands at the end of this one
                    st <= S_LAND;
                end
                S_LAND: begin
                    {cur_t, cur_last, cur_vc, cur_n, cur_flit} <= ret_rd;
                    st <= S_HDR;
                end
                S_HDR: begin
                    if (!c_tx_valid || tx_go) begin
                        c_tx_valid <= 1'b1;
                        c_tx_data  <= mk_hdr(cur_t, cur_last, cur_vc, cur_seq, cur_n,
                                             (cur_t == T_FLIT) ? PW[7:0] : 8'd0);
                        c_tx_last  <= 1'b0;
                        csum       <= mk_hdr(cur_t, cur_last, cur_vc, cur_seq, cur_n,
                                             (cur_t == T_FLIT) ? PW[7:0] : 8'd0);
                        wi         <= {PWW{1'b0}};
                        pay_sr     <= padded;
                        st         <= (cur_t == T_FLIT) ? S_PAY : S_SUM;
                        if (cur_new && (RELIABLE != 0)) begin
                            ret_we <= 1'b1;
                            ret_wa <= cur_seq[WW-1:0];
                            ret_wd <= {cur_t, cur_last, cur_vc, cur_n, cur_flit};
                        end
                    end
                end
                S_PAY: begin
                    if (tx_go) begin
                        c_tx_data <= pay_sr[CW-1:0];
                        csum      <= csum ^ pay_sr[CW-1:0];
                        pay_sr    <= pay_sr >> CW;
                        wi        <= wi + 1'b1;
                        if (wi == PW - 1) begin
                            st <= S_SUM;
                        end
                    end
                end
                S_SUM: begin
                    if (tx_go) begin
                        c_tx_data <= ~csum;
                        c_tx_last <= 1'b1;
                        st        <= S_IDLE;
                        if (cur_new) begin
                            seq_next <= seq_next + 1'b1;
                        end
                        if (cur_play) begin
                            seq_play <= cur_seq + 1'b1;
                        end
                    end
                end
                default: st <= S_IDLE;
            endcase
            // the trailer word leaves: drop valid after it is taken
            if ((st == S_IDLE) && tx_go) begin
                c_tx_valid <= 1'b0;
                c_tx_last  <= 1'b0;
            end
            // acknowledgement bookkeeping
            if (rx_ack) begin
                seq_base <= rx_ack_seq + 1'b1;
                quiet    <= {TW{1'b0}};
                if ((rx_ack_seq + 1'b1) == seq_base) begin
                    if (dups != 2'd3) begin
                        dups <= dups + 1'b1;
                    end
                end
                else begin
                    dups <= 2'd0;
                end
            end
            else if ((RELIABLE != 0) && (outstanding != {SW{1'b0}}) && (quiet != {TW{1'b1}})) begin
                quiet <= quiet + 1'b1;
            end
            if (rx_took) begin
                ack_req <= (RELIABLE != 0);
                ack_seq <= rx_seq;
            end
        end
        for (v = 0; v < VC; v = v + 1) begin
            if (rst) begin
                acc[v] <= {CRW{1'b0}};
            end
            else begin
                acc[v] <= acc[v]
                        + ((i_crd_valid && (i_crd_vc == v[VCW-1:0])) ? i_crd_n : {CN_W{1'b0}})
                        - ((crd_pop && (pick == v[VCW-1:0])) ? cn : {CN_W{1'b0}});
            end
        end
    end

    // ---- the receiver: parse, check, take in order --------------------------------
    reg            r_in;                     // inside a frame
    reg [1:0]      r_t;
    reg            r_last;
    reg [VCW-1:0]  r_vc;
    reg [SW-1:0]   r_seq;
    reg [CN_W-1:0] r_n;
    reg [7:0]      r_words, r_left;
    reg [CW-1:0]   r_sum;
    reg [PW*CW-1:0] r_pay;
    reg [SW-1:0]   exp_seq;

    // A header is the word after a `last` (or the first after reset) with
    // the sync byte; a header without it is skipped until the next `last`.
    reg  r_skip;                               // discarding until the next `last`
    wire hdr_ok  = c_rx_valid && !r_in && !r_skip && (c_rx_data[7:0] == 8'hA5) && !c_rx_last;
    wire trailer = c_rx_valid && r_in && (r_left == 8'd0) && c_rx_last;
    wire sum_ok  = (c_rx_data == ~r_sum);
    wire seq_ok  = (RELIABLE == 0) || (r_seq == exp_seq) || (r_t == T_ACK);

    always @(posedge clk) begin
        if (rst) begin
            r_in <= 1'b0; r_skip <= 1'b0; exp_seq <= {SW{1'b0}}; o_valid <= 1'b0; o_crd_valid <= 1'b0;
            rx_ack <= 1'b0; rx_took <= 1'b0;
        end
        else begin
            o_valid     <= 1'b0;
            o_crd_valid <= 1'b0;
            rx_ack      <= 1'b0;
            rx_took     <= 1'b0;
            if (r_skip) begin
                if (c_rx_valid && c_rx_last) begin
                    r_skip <= 1'b0;
                end
            end
            else if (hdr_ok) begin
                r_in    <= 1'b1;
                r_t     <= c_rx_data[9:8];
                r_last  <= c_rx_data[10];
                r_vc    <= c_rx_data[11 +: VCW];
                r_seq   <= c_rx_data[22:15];
                r_n     <= c_rx_data[26:23];
                r_words <= c_rx_data[34:27];
                r_left  <= c_rx_data[34:27];
                r_sum   <= c_rx_data;
            end
            else if (c_rx_valid && !r_in) begin
                // not a header where one was due: discard to the next `last`
                r_skip <= !c_rx_last;
            end
            else if (c_rx_valid && r_in) begin
                if (c_rx_last && (r_left != 8'd0)) begin
                    // the frame ended early: a word was lost
                    r_in <= 1'b0;
                end
                else if (r_left != 8'd0) begin
                    r_pay[(r_words - r_left)*CW +: CW] <= c_rx_data;
                    r_sum  <= r_sum ^ c_rx_data;
                    r_left <= r_left - 8'd1;
                end
                else if (!c_rx_last) begin
                    // the trailer's `last` was lost: this word begins the next
                    // frame's tail; discard to its `last`
                    r_in   <= 1'b0;
                    r_skip <= 1'b1;
                end
                else begin
                    r_in <= 1'b0;
                    if (sum_ok && seq_ok) begin
                        case (r_t)
                            T_FLIT: begin
                                o_valid <= 1'b1; o_vc <= r_vc; o_last <= r_last;
                                o_flit  <= r_pay[W-1:0];
                            end
                            T_CRD: begin
                                o_crd_valid <= 1'b1; o_crd_vc <= r_vc; o_crd_n <= r_n;
                            end
                            default: begin
                                rx_ack <= 1'b1; rx_ack_seq <= r_seq;
                            end
                        endcase
                        if (r_t != T_ACK) begin
                            exp_seq <= r_seq + 1'b1;
                            rx_took <= 1'b1;
                            rx_seq  <= r_seq;
                        end
                    end
                    else if (sum_ok && (RELIABLE != 0) && (r_t != T_ACK)) begin
                        // out of sequence: repeat the ack of the last frame taken
                        rx_took <= 1'b1;
                        rx_seq  <= exp_seq - 1'b1;
                    end
                end
            end
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (!rst && i_valid && f_full) begin
        $display("%0t ERROR kts_over_serial: flit FIFO full -- more flits in flight than credits", $time);
    end
`ifdef KTS_SER_TRACE
    always @(posedge clk) if (!rst) begin
        if ((st == S_SUM) && tx_go) begin
            $display("%0t %m TX type %0d seq %0d vc %0d n %0d%s base %0d next %0d", $time, cur_t, cur_seq, cur_vc, cur_n,
                     replaying ? " (replay)" : "", seq_base, seq_next);
        end
        if (trailer) begin
            $display("%0t %m RX type %0d seq %0d exp %0d sum %s %s", $time, r_t, r_seq, exp_seq,
                     sum_ok ? "ok" : "BAD", (sum_ok && seq_ok) ? "taken" : "dropped");
        end
        if (start_play) begin
            $display("%0t %m REPLAY from %0d to %0d (quiet %0d dups %0d)", $time, seq_base, seq_next, quiet, dups);
        end
    end
`endif
`endif

endmodule

`default_nettype wire
