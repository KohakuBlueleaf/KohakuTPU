// mag_link -- one full-duplex end of a MAG-to-MAG interlink.
//
// docs/interlink/protocol.md. Two of these back to back, with nothing between
// them but registers, is a complete crossing.
//
//   local ─► tx0 (stops at the peer) ─┐
//         ─► tx1 (the peer forwards) ─┼─► arbiter ─► REG ─► m_axis
//            credit returns ──────────┘
//
//   s_axis ─► REG ─► decode ─┬─► rx0 (this mesh)  ─► local
//                            ├─► rx1 (we forward) ─► the other link
//                            └─► CREDIT ─► a counter, never a FIFO
//
// Three properties are structural rather than stylistic, and each is why the
// module exists:
//
// 1. NOTHING COMBINATIONAL CROSSES. Every output is a register, every input is
//    registered before use. A Laguna site IS a flip-flop, so the tool can only
//    use one when the path is flop -> SLL -> flop; one gate in the crossing
//    forfeits it and the path becomes ordinary interconnect -- which is how an
//    implemented mesh measured 4.6 ns at 98.3% routing with zero logic levels.
//
// 2. TREADY DOES NOT CROSS, and this end never reads it. The receiver is always
//    ready because credit reserved the space before the beat was sent, so the
//    output register is simply overwritten every cycle. `m_axis_tready` reaches
//    nothing but a simulation assertion; wiring a real slave here would put a
//    combinational path back across the SLR, which the assertion catches.
//
// 3. CREDIT IS PER CLASS -- "does this packet stop at the peer, or does the peer
//    forward it". One shared pool would let a stalled forward path stop traffic
//    that was going to terminate anyway. That is protocol.md s4(b) expressed as
//    two counters rather than as an argument.
//
// Credit packets do not consume credit: they are absorbed into an adder on
// arrival and never enter a FIFO, so no credit return waits on the space it is
// about to release.
//
// Every packet has at least one beat, DOORBELL and CREDIT included -- their beat
// is zero and ignored. One wasted beat on the two rare kinds removes a special
// case from the framing, both FIFOs, the arbiter and both benches.

`default_nettype none

module mag_link #(
    // 288 = one flit, which is what one NoC port produces. The link itself is
    // width-agnostic; both ends and every pipe stage between them must agree.
    parameter integer LINK_W     = 288,
    parameter integer TUSER_W    = 96,
    // Receive buffer per class, in beats, and therefore the initial credit.
    // Both ends must agree: a receiver smaller than the sender's credit
    // overflows, and no backpressure is left to catch it.
    parameter integer RX_BEATS   = 64,
    parameter integer CRED_BATCH = 8,
    // Longest packet this end may emit. Above RX_BEATS there exists a packet
    // that can never be granted credit, which presents as a dead link.
    parameter integer MAX_BEATS  = 32
)(
    input  wire                 clk,
    input  wire                 resetn,

    // Mesh ids, not parameters: IL_MESH makes one bitstream usable at any
    // position in the grid. Both must be settled before any traffic -- changing
    // either mid-stream reclassifies packets already counted against a credit.
    input  wire [1:0]           my_mesh,
    input  wire [1:0]           peer_mesh,

    // ---- local send: class 0 stops at the peer, class 1 is forwarded -------
    input  wire [TUSER_W-1:0]   tx0_hdr,
    input  wire                 tx0_hvalid,
    output wire                 tx0_hready,
    input  wire [LINK_W-1:0]    tx0_dat,
    input  wire                 tx0_dlast,
    input  wire                 tx0_dvalid,
    output wire                 tx0_dready,

    input  wire [TUSER_W-1:0]   tx1_hdr,
    input  wire                 tx1_hvalid,
    output wire                 tx1_hready,
    input  wire [LINK_W-1:0]    tx1_dat,
    input  wire                 tx1_dlast,
    input  wire                 tx1_dvalid,
    output wire                 tx1_dready,

    // ---- local receive, same two classes ----------------------------------
    output wire [TUSER_W-1:0]   rx0_hdr,
    output wire                 rx0_hvalid,
    input  wire                 rx0_hready,
    output wire [LINK_W-1:0]    rx0_dat,
    output wire                 rx0_dlast,
    output wire                 rx0_dvalid,
    input  wire                 rx0_dready,

    output wire [TUSER_W-1:0]   rx1_hdr,
    output wire                 rx1_hvalid,
    input  wire                 rx1_hready,
    output wire [LINK_W-1:0]    rx1_dat,
    output wire                 rx1_dlast,
    output wire                 rx1_dvalid,
    input  wire                 rx1_dready,

    // ---- AXI4-Stream out: this is the SLR crossing ------------------------
    output reg  [LINK_W-1:0]    m_axis_tdata,
    output reg  [TUSER_W-1:0]   m_axis_tuser,
    output reg                  m_axis_tlast,
    output reg                  m_axis_tvalid,
    input  wire                 m_axis_tready,

    // ---- AXI4-Stream in ----------------------------------------------------
    input  wire [LINK_W-1:0]    s_axis_tdata,
    input  wire [TUSER_W-1:0]   s_axis_tuser,
    input  wire                 s_axis_tlast,
    input  wire                 s_axis_tvalid,
    output wire                 s_axis_tready,

    // ---- counters, docs/interlink/boundary.md s3 --------------------------
    output wire [63:0]          ctr_tx,        // {beats, packets} sent
    output wire [63:0]          ctr_rx,        // {beats, packets} received
    output wire [63:0]          ctr_stall,     // {idle cycles, credit-stalled}
    output wire [31:0]          cred_state,
    output wire                 fault_len
);
    // protocol.md s1. Re-declared in mag_switch.v and mag_ilink.v because this
    // project has no header that anything includes, and the last silent
    // divergence put CU_DATA into MAG's write queue -- so interlink_pkt_tb.v
    // compares all three by hierarchical reference.
    localparam [3:0] K_MEM_WR   = 4'h1,
                     K_NOC_FLIT = 4'h2,
                     K_DOORBELL = 4'h3,
                     K_CREDIT   = 4'h4;

    localparam integer U_KIND = 0,  U_DMESH = 4,  U_SMESH = 6,
                       U_TXN  = 8,  U_LEN   = 16, U_ADDR  = 32;

    localparam integer CW = 16;

    localparam [1:0] T_IDLE = 2'd0, T_DATA = 2'd1, T_CRED = 2'd2;

    // ===================================================================
    // declarations first: xvlog rejects a name used before it appears, and
    // Vivado's own synthesiser does not, so this ordering is load-bearing.
    // ===================================================================
    reg  [CW-1:0]      cred0, cred1;   // beats the peer can still absorb
    reg  [CW-1:0]      ret0, ret1;     // beats freed here, not yet returned
    reg  [1:0]         tst;
    reg                tsel, thdr_first, csel_r;
    reg  [16:0]        tleft;
    reg  [TUSER_W-1:0] thdr;
    reg  [CW-1:0]      csend;
    reg                fault_len_r, fl_said;

    reg  [LINK_W-1:0]  r_data;
    reg  [TUSER_W-1:0] r_user;
    reg                r_last, r_valid, r_first;
    reg                cur_is_cred, cur_cls;

    wire rx0_hempty, rx1_hempty, rx0_dempty, rx1_dempty;
    wire h0_full, h1_full, d0_full, d1_full;

    // ===================================================================
    // Inbound decode. Ahead of the outbound block because the credit counters
    // it feeds are updated there.
    // ===================================================================
    assign s_axis_tready = 1'b1;

    always @(posedge clk) begin
        if (!resetn) begin
            r_data <= {LINK_W{1'b0}}; r_user <= {TUSER_W{1'b0}};
            r_last <= 1'b0; r_valid <= 1'b0;
        end else begin
            r_data  <= s_axis_tdata;
            r_user  <= s_axis_tuser;
            r_last  <= s_axis_tlast;
            r_valid <= s_axis_tvalid;
        end
    end

    // "First beat of a packet" is "the beat after a last", and it resets to 1
    // or the first packet of all time reads as a continuation.
    always @(posedge clk) begin
        if (!resetn)      r_first <= 1'b1;
        else if (r_valid) r_first <= r_last;
    end

    wire [3:0] r_kind   = r_user[U_KIND  +: 4];
    wire [1:0] r_dmesh  = r_user[U_DMESH +: 2];
    wire       first_now = r_valid && r_first;
    wire       kind_cred = (r_kind == K_CREDIT);
    // Must agree with the sender's classification or credits are returned to
    // the wrong counter. Both sides compare dst_mesh against this mesh's id.
    wire       cls_now   = (r_dmesh != my_mesh);

    always @(posedge clk) begin
        if (!resetn) begin
            cur_is_cred <= 1'b0; cur_cls <= 1'b0;
        end else if (first_now) begin
            cur_is_cred <= kind_cred;
            cur_cls     <= cls_now;
        end
    end

    wire is_cred = first_now ? kind_cred : cur_is_cred;
    wire in_cls  = first_now ? cls_now   : cur_cls;
    wire enq     = r_valid && !is_cred;

    wire          rr_cred_en  = first_now && kind_cred;
    wire          rr_cred_cls = r_user[U_ADDR];
    wire [CW-1:0] rr_cred_n   = {{(CW-8){1'b0}}, r_user[U_TXN +: 8]};

    // ===================================================================
    // Outbound.
    // ===================================================================
    // A HEADER IS REGISTERED BEFORE IT IS PRICED. Measured: reading `len`
    // straight off the offered header put a distributed-RAM FIFO output through
    // the arbiter, a 17-bit add, a 17-bit compare and a 16-bit subtract in one
    // cycle -- mag_switch closed 243.9 MHz, with the worst path running from
    // link0's forward FIFO into link1's credit counter. Behind this register
    // both halves are short. It costs one cycle per packet, on a path whose
    // latency is already a crossing.
    reg [TUSER_W-1:0] q0_hdr, q1_hdr;
    reg               q0_val, q1_val;

    wire [15:0] h0_len = q0_hdr[U_LEN +: 16];
    wire [15:0] h1_len = q1_hdr[U_LEN +: 16];
    wire [16:0] h0_need = {1'b0, h0_len} + 17'd1;
    wire [16:0] h1_need = {1'b0, h1_len} + 17'd1;
    wire        h0_fits = (h0_need <= MAX_BEATS[16:0]);
    wire        h1_fits = (h1_need <= MAX_BEATS[16:0]);

    wire tx0_can = q0_val && h0_fits && (h0_need <= {1'b0, cred0});
    wire tx1_can = q1_val && h1_fits && (h1_need <= {1'b0, cred1});

    // Credits go first: they are what unblocks the far end, and a link that
    // prefers its own data converges on both ends full.
    wire cred_due = (ret0 >= CRED_BATCH[CW-1:0]) || (ret1 >= CRED_BATCH[CW-1:0]);
    // A partial batch is flushed when there is nothing else to send, so a link
    // going quiet does not leave the peer short until it wakes up.
    wire cred_idle = (|ret0 || |ret1) && !q0_val && !q1_val;
    wire cred_go   = (tst == T_IDLE) && (cred_due || cred_idle);
    wire cred_sel  = (ret0 >= CRED_BATCH[CW-1:0]) ? 1'b0 :
                     (ret1 >= CRED_BATCH[CW-1:0]) ? 1'b1 : (|ret0 ? 1'b0 : 1'b1);

    // The grant empties the holding slot; the slot's own readiness is what the
    // source sees, so a stalled credit never reaches back to the switch.
    wire grant0 = (tst == T_IDLE) && !cred_go && tx0_can;
    wire grant1 = (tst == T_IDLE) && !cred_go && !tx0_can && tx1_can;

    assign tx0_hready = !q0_val;
    assign tx1_hready = !q1_val;
    assign tx0_dready = (tst == T_DATA) && (tsel == 1'b0);
    assign tx1_dready = (tst == T_DATA) && (tsel == 1'b1);

    always @(posedge clk) begin
        if (!resetn) begin
            q0_val <= 1'b0; q1_val <= 1'b0;
            q0_hdr <= {TUSER_W{1'b0}}; q1_hdr <= {TUSER_W{1'b0}};
        end else begin
            if (grant0) q0_val <= 1'b0;
            if (grant1) q1_val <= 1'b0;
            if (tx0_hvalid && tx0_hready) begin q0_hdr <= tx0_hdr; q0_val <= 1'b1; end
            if (tx1_hvalid && tx1_hready) begin q1_hdr <= tx1_hdr; q1_val <= 1'b1; end
        end
    end

    // Framing follows TLAST, and the credit follows `len`. They are two facts
    // about one packet, so the assertion below is the only thing keeping them
    // one fact -- a source whose len undercounts its beats would otherwise send
    // uncredited beats into a full receiver.
    wire              tx_dv = (tsel == 1'b0) ? tx0_dvalid : tx1_dvalid;
    wire              tx_dl = (tsel == 1'b0) ? tx0_dlast  : tx1_dlast;
    wire [LINK_W-1:0] tx_dd = (tsel == 1'b0) ? tx0_dat    : tx1_dat;
    wire              tx_fire = (tst == T_DATA) && tx_dv;
    wire              cred_fire = (tst == T_CRED);

    // One assignment per register per cycle. A grant subtracting while a return
    // adds are both live in the same cycle and on the same class, and Verilog
    // resolves two non-blocking writes by order rather than by summing -- so the
    // update is written once, as a sum of deltas.
    wire [CW-1:0] cred0_add = (rr_cred_en && !rr_cred_cls) ? rr_cred_n : {CW{1'b0}};
    wire [CW-1:0] cred1_add = (rr_cred_en &&  rr_cred_cls) ? rr_cred_n : {CW{1'b0}};
    wire [CW-1:0] cred0_sub = grant0 ? h0_need[CW-1:0] : {CW{1'b0}};
    wire [CW-1:0] cred1_sub = grant1 ? h1_need[CW-1:0] : {CW{1'b0}};

    wire [CW-1:0] ret0_add = (rx0_dvalid && rx0_dready) ? {{(CW-1){1'b0}}, 1'b1}
                                                        : {CW{1'b0}};
    wire [CW-1:0] ret1_add = (rx1_dvalid && rx1_dready) ? {{(CW-1){1'b0}}, 1'b1}
                                                        : {CW{1'b0}};
    wire [CW-1:0] ret0_sub = (cred_fire && !csel_r) ? csend : {CW{1'b0}};
    wire [CW-1:0] ret1_sub = (cred_fire &&  csel_r) ? csend : {CW{1'b0}};

    always @(posedge clk) begin
        if (!resetn) begin
            cred0 <= RX_BEATS[CW-1:0]; cred1 <= RX_BEATS[CW-1:0];
            ret0  <= {CW{1'b0}};       ret1  <= {CW{1'b0}};
        end else begin
            cred0 <= cred0 + cred0_add - cred0_sub;
            cred1 <= cred1 + cred1_add - cred1_sub;
            ret0  <= ret0  + ret0_add  - ret0_sub;
            ret1  <= ret1  + ret1_add  - ret1_sub;
        end
    end

    // A batch is capped at 255 because the count rides in the 8-bit txn field;
    // the remainder stays owed and goes out in the next packet.
    wire [CW-1:0] ret_sel = cred_sel ? ret1 : ret0;
    wire [CW-1:0] ret_cap = (ret_sel > {{(CW-8){1'b0}}, 8'hFF})
                            ? {{(CW-8){1'b0}}, 8'hFF} : ret_sel;

    always @(posedge clk) begin
        if (!resetn) begin
            tst <= T_IDLE; tsel <= 1'b0; tleft <= 17'd0; csel_r <= 1'b0;
            thdr <= {TUSER_W{1'b0}}; thdr_first <= 1'b0; csend <= {CW{1'b0}};
            m_axis_tdata  <= {LINK_W{1'b0}};
            m_axis_tuser  <= {TUSER_W{1'b0}};
            m_axis_tlast  <= 1'b0;
            m_axis_tvalid <= 1'b0;
        end else begin
            m_axis_tvalid <= 1'b0;

            case (tst)
            T_IDLE: begin
                if (cred_go) begin
                    csend  <= ret_cap;
                    csel_r <= cred_sel;
                    tst    <= T_CRED;
                end else if (grant0) begin
                    thdr <= q0_hdr; tsel <= 1'b0; tleft <= h0_need;
                    thdr_first <= 1'b1; tst <= T_DATA;
                end else if (grant1) begin
                    thdr <= q1_hdr; tsel <= 1'b1; tleft <= h1_need;
                    thdr_first <= 1'b1; tst <= T_DATA;
                end
            end

            // A state of its own so `csend` is a register when the header is
            // built: a count read and cleared in one expression returns a stale
            // or a doubled batch.
            T_CRED: begin
                m_axis_tdata  <= {LINK_W{1'b0}};
                m_axis_tuser  <= { {(TUSER_W-U_ADDR-1){1'b0}}, csel_r,
                                   16'd0, csend[7:0], my_mesh,
                                   peer_mesh, K_CREDIT };
                m_axis_tlast  <= 1'b1;
                m_axis_tvalid <= 1'b1;
                tst <= T_IDLE;
            end

            T_DATA: if (tx_dv) begin
                m_axis_tdata  <= tx_dd;
                m_axis_tuser  <= thdr_first ? thdr : {TUSER_W{1'b0}};
                m_axis_tlast  <= tx_dl;
                m_axis_tvalid <= 1'b1;
                thdr_first    <= 1'b0;
                tleft         <= tleft - 17'd1;
                if (tx_dl) tst <= T_IDLE;
            end

            default: tst <= T_IDLE;
            endcase
        end
    end

    always @(posedge clk) begin
        if (!resetn) fault_len_r <= 1'b0;
        else if ((q0_val && !h0_fits) || (q1_val && !h1_fits))
            fault_len_r <= 1'b1;
    end
    assign fault_len = fault_len_r;

    // ===================================================================
    // Receive buffers. Header and data are separate FIFOs so a 32-beat packet
    // costs one header entry rather than 32 copies of a 96-bit header. Depths
    // match because the worst case is every packet being one beat, and the beat
    // credit is then the only bound that has to hold.
    // ===================================================================
    sync_fifo #(.DATA_WIDTH(TUSER_W), .FIFO_DEPTH(RX_BEATS),
                .MEMORY_TYPE("distributed")) u_h0 (
        .clk(clk), .rst(!resetn),
        .wr_en(enq && first_now && !in_cls), .wr_data(r_user), .wr_busy(h0_full),
        .rd_en(rx0_hvalid && rx0_hready), .rd_data(rx0_hdr), .rd_busy(rx0_hempty)
    );
    sync_fifo #(.DATA_WIDTH(TUSER_W), .FIFO_DEPTH(RX_BEATS),
                .MEMORY_TYPE("distributed")) u_h1 (
        .clk(clk), .rst(!resetn),
        .wr_en(enq && first_now && in_cls), .wr_data(r_user), .wr_busy(h1_full),
        .rd_en(rx1_hvalid && rx1_hready), .rd_data(rx1_hdr), .rd_busy(rx1_hempty)
    );
    sync_fifo #(.DATA_WIDTH(LINK_W+1), .FIFO_DEPTH(RX_BEATS),
                .MEMORY_TYPE("distributed")) u_d0 (
        .clk(clk), .rst(!resetn),
        .wr_en(enq && !in_cls), .wr_data({r_last, r_data}), .wr_busy(d0_full),
        .rd_en(rx0_dvalid && rx0_dready), .rd_data({rx0_dlast, rx0_dat}),
        .rd_busy(rx0_dempty)
    );
    sync_fifo #(.DATA_WIDTH(LINK_W+1), .FIFO_DEPTH(RX_BEATS),
                .MEMORY_TYPE("distributed")) u_d1 (
        .clk(clk), .rst(!resetn),
        .wr_en(enq && in_cls), .wr_data({r_last, r_data}), .wr_busy(d1_full),
        .rd_en(rx1_dvalid && rx1_dready), .rd_data({rx1_dlast, rx1_dat}),
        .rd_busy(rx1_dempty)
    );

    assign rx0_hvalid = !rx0_hempty;
    assign rx1_hvalid = !rx1_hempty;
    assign rx0_dvalid = !rx0_dempty;
    assign rx1_dvalid = !rx1_dempty;

    // ===================================================================
    // Counters. A link slow because it is starved and a link slow because it is
    // stalled on credit look identical from a packet count and need opposite
    // fixes, so both cycles are counted.
    // ===================================================================
    reg [31:0] n_tx_pkt, n_tx_beat, n_rx_pkt, n_rx_beat, n_stall, n_idle;

    wire want_send = q0_val || q1_val;
    wire can_send  = tx0_can || tx1_can;

    always @(posedge clk) begin
        if (!resetn) begin
            n_tx_pkt <= 32'd0; n_tx_beat <= 32'd0;
            n_rx_pkt <= 32'd0; n_rx_beat <= 32'd0;
            n_stall  <= 32'd0; n_idle    <= 32'd0;
        end else begin
            if (tx_fire) begin
                n_tx_beat <= n_tx_beat + 32'd1;
                if (tx_dl) n_tx_pkt <= n_tx_pkt + 32'd1;
            end
            if (enq) begin
                n_rx_beat <= n_rx_beat + 32'd1;
                if (r_last) n_rx_pkt <= n_rx_pkt + 32'd1;
            end
            if ((tst == T_IDLE) && want_send && !can_send) n_stall <= n_stall + 32'd1;
            if ((tst == T_IDLE) && !want_send)             n_idle  <= n_idle  + 32'd1;
        end
    end

    assign ctr_tx     = {n_tx_beat, n_tx_pkt};
    assign ctr_rx     = {n_rx_beat, n_rx_pkt};
    assign ctr_stall  = {n_idle, n_stall};
    assign cred_state = {cred1[7:0], cred0[7:0], ret1[7:0], ret0[7:0]};

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (resetn && m_axis_tvalid && !m_axis_tready)
            $display("%0t ERROR mag_link: m_axis_tready low. The far end must be a mag_link with tready tied high -- a real slave here reintroduces a combinational SLR crossing.",
                     $time);
        if (resetn && enq && ((!in_cls && d0_full) || (in_cls && d1_full)))
            $display("%0t ERROR mag_link: receive FIFO overflow on class %0d -- credit accounting is wrong, not the buffer size.",
                     $time, in_cls);
        if (resetn && tx_fire && (tx_dl != (tleft == 17'd1)))
            $display("%0t ERROR mag_link: TLAST and `len` disagree -- %0d beats still credited when TLAST arrived. The source's header undercounts its own packet, and the extra beats are uncredited.",
                     $time, tleft);
        if (resetn && fault_len_r && !fl_said) begin
            fl_said <= 1'b1;
            $display("%0t ERROR mag_link: a packet longer than MAX_BEATS=%0d was offered; it can never be granted credit.",
                     $time, MAX_BEATS);
        end
        if (!resetn) fl_said <= 1'b0;
    end
`endif

endmodule

`default_nettype wire
