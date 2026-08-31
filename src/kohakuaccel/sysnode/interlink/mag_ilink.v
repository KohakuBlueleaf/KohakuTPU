// mag_ilink -- everything MAG needs to speak to another mesh, in one place.
//
// docs/interlink/transfers.md. Four jobs, and they are together because they
// share the switch's one local port and the register window that reports them:
//
//   1. the mover's writes, split by address. `awaddr[37:36] != my_mesh` becomes
//      a MEM_WR packet and the write is answered LOCALLY, at once. A posted
//      write is the whole point -- waiting for a far DRAM would put an SLR
//      round trip in the mover's per-word loop, and the mover already runs one
//      transaction at a time.
//   2. inbound MEM_WR, written into this mesh's DRAM through its own AXI master.
//   3. NoC flits marked for another mesh, encapsulated; and inbound ones
//      injected back into this mesh's NoC.
//   4. DOORBELL, in both directions, and the registers in boundary.md s3.
//
// COMPLETION MEANS LANDED. An inbound DOORBELL waits for every write ahead of it
// to have its BRESP before it counts, so a consumer released by a doorbell is
// released by data that is in DRAM rather than in a queue.
//
// A BEAT IS ONE FLIT, and the link is 288 bits because that is what one NoC
// port produces. 256 payload bits per beat is 9.6 GB/s at 300 MHz, and a single
// port cannot exceed that -- so at this width the link is matched to its source
// rather than waiting on it, and a flit crosses verbatim with nothing packed,
// padded or reconstructed.
//
// A wider link only pays once TWO ports can feed one MAG at the same time: the
// encapsulator takes one flit per cycle across all of them today. 576 bits
// (two 288-bit slots, two flits per beat) is the shape that becomes worth
// building then -- measured at 19.2 GB/s and 328 MHz, and reverted here because
// nothing can currently drive it.
//
// Narrower also buys the thing nobody has measured: 386 nets per direction
// instead of 674, against a Laguna budget that topology.md s4 flags as the real
// unknown behind SLL count.
//
// FLITS ARE STILL PACKED INTO MULTI-BEAT PACKETS -- one packet carries a whole
// burst, up to MAX_BEATS flits, so the header cost is amortised even though
// each beat holds one flit.
//
// THE SOURCE COORDINATE IS PRESERVED across the crossing. Rewriting it to this
// MAG's own would make two remote bursts arriving at one node indistinguishable
// to vec_cu's `cd_alien` check, which is the mechanism that stops two senders'
// data being merged into one L1 region. The cost is that `ack == 0` -- "answer
// the sender" -- would answer a node in the WRONG mesh, so a remote burst must
// name its ack destination explicitly and IL_F_ACK0 reports one that does not.

`default_nettype none

module mag_ilink #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer DATA_W     = 256,
    parameter integer ADDR_W     = 40,
    parameter integer LINK_W     = 288,
    parameter integer TUSER_W    = 96,
    parameter integer MESH_ID    = 0,
    parameter integer MAX_BEATS  = 32,
    parameter integer MEM_X      = 0,
    parameter integer MEM_Y      = 1
)(
    input  wire                  clk,
    input  wire                  resetn,

    // ---- aux config window (writes) and status window (reads) -------------
    input  wire                  cfg_en,
    input  wire [7:0]            cfg_addr,
    input  wire [63:0]           cfg_data,
    input  wire [3:0]            stat_sel,
    output reg  [63:0]           stat_q,
    // The four inbound doorbell counts in one word, for a consumer that polls
    // them every wakeup. A second `stat_sel` port would duplicate a 16-way
    // 64-bit mux to deliver four registers this reads directly.
    output wire [63:0]           dbell_counts,
    output wire [1:0]            my_mesh,

    // ---- the mover's write channel: slave in, master out ------------------
    input  wire [ADDR_W-1:0]     s_awaddr,
    // The splitter needs the burst length; m_awlen reaches the local master
    // straight from the mover (mag.v), so it is not re-driven here.
    input  wire [7:0]            s_awlen,
    input  wire                  s_awvalid,
    output wire                  s_awready,
    input  wire [DATA_W-1:0]     s_wdata,
    input  wire [DATA_W/8-1:0]   s_wstrb,
    input  wire                  s_wvalid,
    output wire                  s_wready,
    output wire                  s_bvalid,
    output wire [1:0]            s_bresp,
    input  wire                  s_bready,

    output wire [ADDR_W-1:0]     m_awaddr,
    output wire                  m_awvalid,
    input  wire                  m_awready,
    output wire [DATA_W-1:0]     m_wdata,
    output wire [DATA_W/8-1:0]   m_wstrb,
    output wire                  m_wlast,
    output wire                  m_wvalid,
    input  wire                  m_wready,
    input  wire                  m_bvalid,
    input  wire [1:0]            m_bresp,
    output wire                  m_bready,

    // ---- inbound writes into this mesh's DRAM -----------------------------
    output reg  [ADDR_W-1:0]     lk_awaddr,
    output reg                   lk_awvalid,
    input  wire                  lk_awready,
    output reg  [DATA_W-1:0]     lk_wdata,
    output wire [DATA_W/8-1:0]   lk_wstrb,
    output wire                  lk_wlast,
    output reg                   lk_wvalid,
    input  wire                  lk_wready,
    input  wire                  lk_bvalid,
    input  wire [1:0]            lk_bresp,
    output wire                  lk_bready,

    // ---- NoC: flits to encapsulate, and flits to inject -------------------
    input  wire [FLIT_WIDTH-1:0] enc_data,
    input  wire                  enc_valid,
    output wire                  enc_busy,
    output reg  [FLIT_WIDTH-1:0] inj_data,
    output reg                   inj_valid,
    input  wire                  inj_busy,

    // ---- the switch's local port ------------------------------------------
    output reg  [TUSER_W-1:0]    ltx_hdr,
    output reg                   ltx_hvalid,
    input  wire                  ltx_hready,
    output reg  [LINK_W-1:0]     ltx_dat,
    output reg                   ltx_dlast,
    output reg                   ltx_dvalid,
    input  wire                  ltx_dready,

    input  wire [TUSER_W-1:0]    lrx_hdr,
    input  wire                  lrx_hvalid,
    output wire                  lrx_hready,
    input  wire [LINK_W-1:0]     lrx_dat,
    input  wire                  lrx_dlast,
    input  wire                  lrx_dvalid,
    output wire                  lrx_dready,

    // ---- reported by the switch, mirrored into the status window ----------
    input  wire [63:0]           sw_tx0, sw_rx0, sw_stall0,
    input  wire [63:0]           sw_tx1, sw_rx1, sw_stall1,
    input  wire [63:0]           sw_fwd, sw_lblock,
    input  wire [31:0]           sw_cred0, sw_cred1,
    input  wire [3:0]            sw_fault,

    // ---- a NoC memory request whose address is not in this mesh -----------
    input  wire                  bad_remote_req
);
    localparam [3:0] K_MEM_WR = 4'h1, K_NOC_FLIT = 4'h2, K_DOORBELL = 4'h3;

    localparam integer U_KIND = 0, U_DMESH = 4, U_SMESH = 6, U_TXN = 8;
    localparam integer U_LEN = 16, U_ADDR = 32;

    localparam integer F_RD_REMOTE = 0, F_ACK0 = 1, F_SWITCH = 2, F_AXI = 3;
    localparam integer F_INJ = 4;

    localparam integer LSB = $clog2(DATA_W/8);

    // Flit header positions, restated from noc_pkt.vh -- nothing includes it.
    localparam integer NF_DX = FLIT_WIDTH - POS_WIDTH,            // 284
                       NF_DY = FLIT_WIDTH - 2*POS_WIDTH,          // 280
                       NF_SX = FLIT_WIDTH - 3*POS_WIDTH,          // 276
                       NF_SY = FLIT_WIDTH - 4*POS_WIDTH,          // 272
                       NF_TY = FLIT_WIDTH - 4*POS_WIDTH - 4,      // 268
                       NF_TX = FLIT_WIDTH - 4*POS_WIDTH - 12,     // 260
                       NF_LA = FLIT_WIDTH - 4*POS_WIDTH - 13,     // 259
                       NF_RS = FLIT_WIDTH - 4*POS_WIDTH - 16;     // 256

    localparam [7:0] CUD_ACK_LSB = 208;

    // A beat is one slot, and a slot is one flit or one memory word in its low
    // bits. LINK_W == SLOT_W here; the field exists because the 576-bit variant
    // makes it two, and every use below is written against SLOT_W so that
    // change stays local.
    localparam integer SLOT_W = FLIT_WIDTH;
    // 72, not 66: the address field below it is ADDR_W wide, and this header is
    // its OWN encoding -- widening noc_pkt.vh's spare did not move this one.
    localparam integer U_ODD  = U_ADDR + ADDR_W;  // last beat carries one slot

    // =====================================================================
    // Registers
    // =====================================================================
    reg [1:0]  mesh_r;
    reg        enable_r;
    reg [7:0]  fault_r;
    reg [31:0] dbell_n  [0:3];
    reg [15:0] dbell_tx [0:3];
    reg [31:0] door_sent;
    reg        door_req;
    reg [1:0]  door_dst;
    reg [7:0]  door_txn;
    reg        dbell_clr;

    // Declared here because the encapsulator reads them and the outbound
    // arbiter drives them; xvlog rejects the other order.
    wire       ob_fl_ack, ob_db_ack;
    // Every fault is raised in one place, so the register has one driver. The
    // conditions are wires from wherever they are observed.
    wire       flt_axi_wr, flt_axi_lk, flt_drop, flt_ack0;

    assign my_mesh = mesh_r;

    assign dbell_counts = {dbell_n[3][15:0], dbell_n[2][15:0],
                           dbell_n[1][15:0], dbell_n[0][15:0]};

    wire [63:0] caps = {32'd0, 4'd1, 4'd4, {2'd0, mesh_r}, 4'd2, 16'h494C};

    always @(*) begin
        case (stat_sel)
            4'd0:    stat_q = enable_r ? caps : 64'd0;
            4'd1:    stat_q = {56'd0, fault_r};
            4'd2:    stat_q = {16'd0, dbell_tx[0], dbell_n[0]};
            4'd3:    stat_q = {16'd0, dbell_tx[1], dbell_n[1]};
            4'd4:    stat_q = {16'd0, dbell_tx[2], dbell_n[2]};
            4'd5:    stat_q = {16'd0, dbell_tx[3], dbell_n[3]};
            4'd6:    stat_q = sw_tx0;
            4'd7:    stat_q = sw_rx0;
            4'd8:    stat_q = sw_tx1;
            4'd9:    stat_q = sw_rx1;
            4'd10:   stat_q = sw_stall0;
            4'd11:   stat_q = sw_stall1;
            4'd12:   stat_q = sw_fwd;
            4'd13:   stat_q = {sw_cred1, sw_cred0};
            4'd14:   stat_q = {32'd0, door_sent};
            default: stat_q = sw_lblock;
        endcase
    end

    wire [7:0] cfg_sel = {cfg_addr[7:3], 3'b000};
    wire       cfg_mine = cfg_en && cfg_addr[7];

    // =====================================================================
    // The mover's write channel, split by address.
    // =====================================================================
    // A LOCAL BURST STREAMS. It used to take one AW and one single W beat and
    // hold until B, which taxed every mover write in an ILINK=1 mesh at one word

    // per DRAM round trip -- measured 26.90 cycles a word through a real mesh,
    // and it applied to local traffic that never touches the link.
    localparam [1:0] WS_IDLE = 2'd0, WS_LOC = 2'd1, WS_REM = 2'd2;

    reg [1:0]         ws;
    reg [8:0]         w_left;         // beats still owed by this burst
    reg [ADDR_W-1:0]  a_r;
    reg [DATA_W-1:0]  d_r;
    reg [DATA_W/8-1:0] st_r;
    reg               loc_aw;
    reg [5:0]         b_owed;         // B responses owed to the mover
    reg               ob_wr_req;      // a remote write wants the link
    wire              ob_wr_ack;

    // ONE B PER BURST, not per beat: the mover counts B against AW.
    assign s_awready = (ws == WS_IDLE) && (b_owed < 6'd32);
    assign s_wready  = (ws == WS_LOC) ? m_wready
                     : (ws == WS_REM) ? !ob_wr_req : 1'b0;
    assign s_bvalid  = (b_owed != 6'd0);
    assign s_bresp   = 2'b00;

    assign m_awaddr  = a_r;
    assign m_awvalid = loc_aw;
    assign m_wdata   = s_wdata;
    assign m_wstrb   = s_wstrb;
    assign m_wlast   = (w_left == 9'd1);
    assign m_wvalid  = (ws == WS_LOC) && s_wvalid;
    assign m_bready  = 1'b1;

    // NOT the top two bits -- those are the special flag and a reserved bit. A
    // DRAM and an aperture address carry the mesh alike, so this never decodes one.
    localparam integer A_MESH_LO = ADDR_W - 4;
    wire remote_now = (s_awaddr[A_MESH_LO +: 2] != mesh_r);

    wire w_beat  = s_wvalid && s_wready;
    wire w_last  = (w_left == 9'd1);
    wire b_take  = s_bvalid && s_bready;

    always @(posedge clk) begin
        // `a_r`/`d_r` are not reset: `ws` and `loc_aw` qualify them. `st_r` keeps
        // its all-ones default, which is a meaningful value rather than a clear.
        if (!resetn) begin
            ws <= WS_IDLE; w_left <= 9'd0; b_owed <= 6'd0;
            st_r <= {(DATA_W/8){1'b1}};
            loc_aw <= 1'b0; ob_wr_req <= 1'b0;
        end else begin
            if (loc_aw && m_awready) begin
                loc_aw <= 1'b0;
            end

            case (ws)
                WS_IDLE: if (s_awvalid && s_awready) begin
                    a_r    <= s_awaddr;
                    w_left <= {1'b0, s_awlen} + 9'd1;
                    if (remote_now && enable_r) begin
                        ws <= WS_REM;
                    end
                    else begin
                        loc_aw <= 1'b1;
                        ws     <= WS_LOC;
                    end
                end

                WS_LOC: if (w_beat) begin
                    if (w_last) begin
                        ws <= WS_IDLE;
                    end
                    else begin
                        w_left <= w_left - 9'd1;
                    end
                end

                // One packet per word still. The beats are decomposed rather than
                // gathered, so a remote burst is correct here and not yet fast.
                WS_REM: begin
                    if (w_beat) begin
                        d_r  <= s_wdata;
                        st_r <= s_wstrb;
                        ob_wr_req <= 1'b1;
                    end
                    if (ob_wr_ack) begin
                        ob_wr_req <= 1'b0;
                        a_r <= a_r + {{(ADDR_W-6){1'b0}}, 6'd32};
                        if (w_last) begin
                            ws <= WS_IDLE;
                        end
                        else begin
                            w_left <= w_left - 9'd1;
                        end
                    end
                end

                default: ws <= WS_IDLE;
            endcase

            // m_bvalid only ever answers a LOCAL burst, so it is not gated on
            // the current burst: a local B landing after a remote burst began

            // would be dropped, and the mover's outstanding count never retires.
            b_owed <= b_owed
                    + (m_bvalid ? 6'd1 : 6'd0)
                    + (((ws == WS_REM) && ob_wr_ack && w_last) ? 6'd1 : 6'd0)
                    - (b_take ? 6'd1 : 6'd0);
        end
    end

    // =====================================================================
    // Encapsulation: accumulate a burst's flits into one packet.
    // =====================================================================
    wire [1:0]  e_mesh = enc_data[NF_RS +: 2];
    wire        e_rem  = enc_data[NF_RS + 2];
    wire [7:0]  e_fin  = enc_data[NF_TX +: 8];
    wire [2*POS_WIDTH-1:0] e_src = enc_data[NF_SY +: 2*POS_WIDTH];
    wire        e_last = enc_data[NF_LA];

    reg                    acc_open;
    reg [1:0]              acc_mesh;
    reg [7:0]              acc_fin;
    reg [2*POS_WIDTH-1:0]  acc_src;
    reg [15:0]             acc_beats;          // complete beats already pushed
    reg                    acc_half;           // slot 0 is filled, slot 1 is not
    reg [SLOT_W-1:0]       acc_lo;
    reg                    acc_ready;          // closed and waiting for the link
    reg [15:0]             acc_nb;             // the closed packet's beats
    reg                    acc_nodd;

    wire acc_match = (
        acc_open
        && (acc_mesh == e_mesh)
        && (acc_fin == e_fin)
        && (acc_src == e_src)
    );
    wire ef_full, ef_empty;
    wire [SLOT_W-1:0] ef_q;

    // Accept while the packet stays open; a flit that would start a different
    // one waits rather than being merged into this. A remote flit arriving at a
    // disabled interlink is consumed and dropped -- holding the port would stop
    // this mesh's own memory traffic behind a link nobody turned on.
    wire enc_take = (
        enc_valid
        && enable_r
        && e_rem
        && !acc_ready
        && !ef_full
        && (!acc_open || acc_match)
    );
    wire enc_drop = enc_valid && (!enable_r || !e_rem);
    assign enc_busy = !(enc_take || enc_drop);
    assign flt_drop = enc_drop;

    // COMBINATIONAL, and it has to be. Registered, the pop reaches the FIFO a
    // cycle after the beat that consumed the head was loaded, so a first-word
    // fall-through head is still the OLD flit when the next beat loads it --
    // the first flit of every burst goes twice and the last one never goes.
    wire ef_pop;

    // One flit fills a beat, so every accepted flit completes one. The packet
    // closes on NOC_LAST or when it is full; `acc_nodd` is therefore always 0
    // and the far side reads U_ODD without caring which width built the packet.
    wire e_completes = enc_take;
    wire acc_full    = e_completes && (acc_beats + 16'd1 == MAX_BEATS[15:0]);
    wire acc_close   = enc_take && (e_last || acc_full);

    sync_fifo #(.DATA_WIDTH(SLOT_W), .FIFO_DEPTH(MAX_BEATS),
                .MEMORY_TYPE("distributed")) u_enc (
        .clk(clk), .rst(!resetn),
        .wr_en(e_completes), .wr_data(enc_data), .wr_busy(ef_full),
        .rd_en(ef_pop), .rd_data(ef_q), .rd_busy(ef_empty)
    );

    // A remote burst whose ack is 0 would be answered in the wrong mesh, so it
    // is reported at the descriptor flit -- the only flit that carries one.
    wire e_isdesc = enc_take && !acc_open;
    wire [7:0] e_ack = enc_data[CUD_ACK_LSB +: 8];
    // CU_DATA ONLY. A memory descriptor carries its burst length in those bits,
    // and a one-beat remote drain would raise IL_F_ACK0 on every crossing.
    wire e_iscud = (enc_data[NF_TY +: 4] == 4'h8);
    assign flt_ack0 = e_isdesc && e_iscud && (e_ack == 8'd0);

    always @(posedge clk) begin
        if (!resetn) begin
            acc_open <= 1'b0; acc_mesh <= 2'd0; acc_fin <= 8'd0;
            acc_src <= {(2*POS_WIDTH){1'b0}};
            acc_beats <= 16'd0; acc_half <= 1'b0; acc_lo <= {SLOT_W{1'b0}};
            acc_ready <= 1'b0; acc_nb <= 16'd0; acc_nodd <= 1'b0;
        end else begin
            if (enc_take) begin
                if (!acc_open) begin
                    acc_open <= 1'b1;
                    acc_mesh <= e_mesh;
                    acc_fin  <= e_fin;
                    acc_src  <= e_src;
                end
                if (!e_completes) begin
                    acc_lo <= enc_data;
                end
                if (acc_close) begin
                    acc_ready <= 1'b1;
                    acc_nb    <= acc_beats + 16'd1;
                    acc_nodd  <= !e_completes;
                    acc_open  <= 1'b0;
                    acc_half  <= 1'b0;
                    acc_beats <= 16'd0;
                end else begin
                    acc_half  <= !e_completes;
                    if (e_completes) begin
                        acc_beats <= acc_beats + 16'd1;
                    end
                end
            end
            if (ob_fl_ack) begin
                acc_ready <= 1'b0;
            end
        end
    end

    // =====================================================================
    // Outbound arbitration. Three sources, rotating so none can be starved.
    // =====================================================================
    localparam [1:0] OB_IDLE = 2'd0, OB_HDR = 2'd1, OB_DAT = 2'd2;
    reg [1:0]  obst;
    reg [1:0]  ob_who, ob_rr;
    reg [15:0] ob_left;

    wire req_wr = ob_wr_req;
    wire req_fl = acc_ready;
    wire req_db = door_req;

    // Rotating priority: the last winner drops to the back.
    wire [2:0] req = {req_db, req_fl, req_wr};
    reg  [1:0] pick;
    always @(*) begin
        case (ob_rr)
            2'd0:    pick = req_wr ? 2'd0 : req_fl ? 2'd1 : 2'd2;
            2'd1:    pick = req_fl ? 2'd1 : req_db ? 2'd2 : 2'd0;
            default: pick = req_db ? 2'd2 : req_wr ? 2'd0 : 2'd1;
        endcase
    end

    assign ob_wr_ack = (obst == OB_HDR) && ltx_hready && (ob_who == 2'd0);
    assign ob_fl_ack = (
        (obst == OB_DAT)
        && ltx_dready
        && ltx_dlast
        && (ob_who == 2'd1)
    );
    assign ob_db_ack = (
        (obst == OB_DAT)
        && ltx_dready
        && ltx_dlast
        && (ob_who == 2'd2)
    );
    assign ef_pop = (
        (ob_who == 2'd1)
        && (
            ((obst == OB_HDR) && ltx_hready)
            || ((obst == OB_DAT) && ltx_dready && (ob_left != 16'd1))
        )
    );

    // A memory packet is still one word today, so its beat is odd by
    // construction; the mover hands over one 32-byte write at a time and there
    // is nothing contiguous to gather. The far side reads U_ODD either way, so
    // widening this to a run of words is a change here and nowhere else.
    // Destination mesh at [ADDR_W-1 -: 2] read {special, reserved} -- 00 for any
    // DRAM address -- so a remote write looped back into the sender's own DRAM.
    wire [TUSER_W-1:0] hdr_wr = {{(TUSER_W-U_ODD-1){1'b0}}, 1'b1,
                                 a_r,
                                 16'd0, 8'd0, mesh_r,
                                 a_r[ADDR_W-4 +: 2], K_MEM_WR};
    wire [TUSER_W-1:0] hdr_fl = {{(TUSER_W-U_ODD-1){1'b0}}, acc_nodd,
                                 {(ADDR_W-8){1'b0}}, acc_fin,
                                 acc_nb - 16'd1, 8'd0, mesh_r,
                                 acc_mesh, K_NOC_FLIT};
    wire [TUSER_W-1:0] hdr_db = {{(TUSER_W-U_ODD-1){1'b0}}, 1'b1,
                                 {ADDR_W{1'b0}},
                                 16'd0, door_txn, mesh_r,
                                 door_dst, K_DOORBELL};

    always @(posedge clk) begin
        // `ltx_hdr`/`ltx_dat` are not reset: their valids qualify them.
        if (!resetn) begin
            obst <= OB_IDLE; ob_who <= 2'd0; ob_rr <= 2'd0; ob_left <= 16'd0;
            ltx_hvalid <= 1'b0;
            ltx_dvalid <= 1'b0; ltx_dlast <= 1'b0;
            door_sent <= 32'd0;
        end else begin
            case (obst)
                OB_IDLE: if (|req && enable_r) begin
                    ob_who <= pick;
                    case (pick)
                        2'd0: begin ltx_hdr <= hdr_wr; ob_left <= 16'd1; end
                        2'd1: begin ltx_hdr <= hdr_fl; ob_left <= acc_nb; end
                        default: begin ltx_hdr <= hdr_db; ob_left <= 16'd1; end
                    endcase
                    ltx_hvalid <= 1'b1;
                    obst <= OB_HDR;
                end

                OB_HDR: if (ltx_hready) begin
                    ltx_hvalid <= 1'b0;
                    case (ob_who)
                        2'd0:    ltx_dat <= {{(LINK_W-DATA_W){1'b0}}, d_r};
                        2'd1:    ltx_dat <= ef_q;
                        default: ltx_dat <= {LINK_W{1'b0}};
                    endcase
                    ltx_dlast  <= (ob_left == 16'd1);
                    ltx_dvalid <= 1'b1;
                    obst <= OB_DAT;
                end

                OB_DAT: if (ltx_dready) begin
                    if (ob_left == 16'd1) begin
                        ltx_dvalid <= 1'b0;
                        ltx_dlast  <= 1'b0;
                        ob_rr      <= ob_who;
                        if (ob_who == 2'd2) begin
                            door_sent <= door_sent + 32'd1;
                        end
                        obst       <= OB_IDLE;
                    end else begin
                        ob_left <= ob_left - 16'd1;
                        ltx_dat <= ef_q;
                        ltx_dlast <= (ob_left == 16'd2);
                    end
                end

                default: obst <= OB_IDLE;
            endcase
        end
    end

    // =====================================================================
    // Inbound. One stream, handled in order, because the order is what makes a
    // DOORBELL mean "the data ahead of me has landed".
    // =====================================================================
    localparam [2:0] IN_HDR = 3'd0, IN_WR = 3'd1, IN_FLIT = 3'd2;
    localparam [2:0] IN_DOOR = 3'd3, IN_SKIP = 3'd4;
    reg [2:0]  inst;
    reg [3:0]  in_kind;
    reg [1:0]  in_src;
    reg [7:0]  in_txn;
    reg [ADDR_W-1:0] in_addr;
    reg [15:0] in_left;
    reg [3:0]  wr_out;          // AXI writes issued, BRESP not yet back
    reg        in_slot;         // which half of the beat is next
    reg        in_odd;          // the last beat of this packet carries one slot
    integer    dj;

    assign lk_wstrb  = {(DATA_W/8){1'b1}};
    assign lk_wlast  = 1'b1;
    assign lk_bready = 1'b1;

    wire lk_free  = (
        (wr_out != 4'd8)
        && (!lk_awvalid || lk_awready)
        && (!lk_wvalid || lk_wready)
    );
    wire inj_free = !inj_valid || !inj_busy;

    // The beat is popped on the cycle its LAST slot is consumed, so slot 0 is
    // read from the FIFO head while it is still there. `lrx_dready` therefore
    // has to be combinational: a registered ready pops a cycle late, and the
    // next beat's slot 0 would be read from the beat that had already gone.
    wire in_last_beat = (in_left == 16'd1);
    wire in_one_slot  = in_last_beat && in_odd;
    wire in_beat_done = 1'b1;         // one slot per beat at this width

    wire [SLOT_W-1:0] in_slotd = lrx_dat[SLOT_W-1:0];
    wire [FLIT_WIDTH-1:0] in_flit = in_slotd;

    wire in_act_wr = (inst == IN_WR)   && lrx_dvalid && lk_free;
    wire in_act_fl = (inst == IN_FLIT) && lrx_dvalid && inj_free;

    assign lrx_hready = (inst == IN_HDR);
    assign lrx_dready = ((in_act_wr || in_act_fl) && in_beat_done)
                     || ((inst == IN_DOOR) && (wr_out == 4'd0))
                     || (inst == IN_SKIP);

    assign flt_axi_wr = m_bvalid && (m_bresp != 2'b00);
    assign flt_axi_lk = lk_bvalid && (lk_bresp != 2'b00);

    always @(posedge clk) begin
        if (!resetn) begin
            inst <= IN_HDR; in_kind <= 4'd0; in_src <= 2'd0; in_txn <= 8'd0;
            // in_addr is ADDR_W wide and the header loads it; the 34'd0 here
            // was a leftover that zero-extended silently.
            in_left <= 16'd0; wr_out <= 4'd0;
            in_slot <= 1'b0; in_odd <= 1'b0;
            lk_awvalid <= 1'b0; lk_wvalid <= 1'b0;
            lk_awaddr <= {ADDR_W{1'b0}};
            // lk_wdata and inj_data are payload; lk_wvalid and inj_valid above
            // are the qualifiers, so resetting the data as well buys nothing.
            inj_valid <= 1'b0;
            for (dj = 0; dj < 4; dj = dj + 1) begin
                dbell_n[dj]  <= 32'd0;
                dbell_tx[dj] <= 16'd0;
            end
        end else begin
            // A clear racing a doorbell loses to it, below: losing one count is
            // better than a clear that silently does not clear.
            if (dbell_clr) begin
                for (dj = 0; dj < 4; dj = dj + 1) begin
                    dbell_n[dj] <= 32'd0;
                end
            end
            if (lk_awvalid && lk_awready) begin
                lk_awvalid <= 1'b0;
            end
            if (lk_wvalid  && lk_wready) begin
                lk_wvalid  <= 1'b0;
            end
            if (inj_valid && !inj_busy) begin
                inj_valid  <= 1'b0;
            end

            case ({lk_awvalid && lk_awready, lk_bvalid})
                2'b10:   wr_out <= wr_out + 4'd1;
                2'b01:   wr_out <= wr_out - 4'd1;
                default: ;
            endcase

            case (inst)
                IN_HDR: begin
                    if (lrx_hvalid && lrx_hready) begin
                        in_kind <= lrx_hdr[U_KIND  +: 4];
                        in_src  <= lrx_hdr[U_SMESH +: 2];
                        in_txn  <= lrx_hdr[U_TXN   +: 8];
                        in_addr <= lrx_hdr[U_ADDR  +: ADDR_W];
                        in_left <= lrx_hdr[U_LEN   +: 16] + 16'd1;
                        in_odd  <= lrx_hdr[U_ODD];
                        in_slot <= 1'b0;
                        case (lrx_hdr[U_KIND +: 4])
                            K_MEM_WR:   inst <= IN_WR;
                            K_NOC_FLIT: inst <= IN_FLIT;
                            K_DOORBELL: inst <= IN_DOOR;
                            default:    inst <= IN_SKIP;
                        endcase
                    end
                end

                // A DRAM address lands by its LOW 32 bits (local DRAM starts
                // at zero; the mesh field would put it 4 GB out). A SPECIAL
                // address keeps all 40: mag_dram_port claims by bit 39 and the
                // mesh field. Truncated, a mover's copy into the far mesh's
                // staging landed in its DRAM at the aperture offset, silently.
                IN_WR: if (in_act_wr) begin
                    lk_awaddr  <= (
                        in_addr[ADDR_W-1] ? in_addr
                        : {{(ADDR_W-32){1'b0}}, in_addr[31:0]}
                    );
                    lk_awvalid <= 1'b1;
                    lk_wdata   <= in_slotd[DATA_W-1:0];
                    lk_wvalid  <= 1'b1;
                    in_addr    <= in_addr + (1 << LSB);
                    if (in_beat_done) begin
                        in_slot <= 1'b0;
                        if (in_last_beat) begin
                            inst <= IN_HDR;
                        end
                        else begin
                            in_left <= in_left - 16'd1;
                        end
                    end
                    else begin
                        in_slot <= 1'b1;
                    end
                end

                // The flit's own txn carries {fin_y, fin_x}; the header carries the
                // same, and using the flit's needs no lookahead. `src` is kept.
                IN_FLIT: if (in_act_fl) begin
                    inj_data <= in_flit;
                    inj_data[NF_DX +: POS_WIDTH] <= in_flit[NF_TX +: POS_WIDTH];
                    inj_data[NF_DY +: POS_WIDTH]
                        <= in_flit[NF_TX + POS_WIDTH +: POS_WIDTH];
                    inj_data[NF_TX +: 8] <= 8'd0;
                    inj_data[NF_RS +: 3] <= 3'd0;
                    inj_valid <= 1'b1;
                    if (in_beat_done) begin
                        in_slot <= 1'b0;
                        if (in_last_beat) begin
                            inst <= IN_HDR;
                        end
                        else begin
                            in_left <= in_left - 16'd1;
                        end
                    end
                    else begin
                        in_slot <= 1'b1;
                    end
                end

                // Held until every write ahead of it has its BRESP. Without this a
                // doorbell overtakes data that is still in the AXI pipeline, and
                // the consumer it releases reads the previous contents.
                IN_DOOR: begin
                    if (lrx_dvalid && lrx_dready) begin
                        dbell_n[in_src]  <= dbell_n[in_src] + 32'd1;
                        dbell_tx[in_src] <= {8'd0, in_txn};
                        inst <= IN_HDR;
                    end
                end

                IN_SKIP: begin
                    if (lrx_dvalid && lrx_dready && lrx_dlast) begin
                        inst <= IN_HDR;
                    end
                end

                default: inst <= IN_HDR;
            endcase
        end
    end

    // =====================================================================
    // Config writes and fault collection.
    // =====================================================================
    always @(posedge clk) begin
        if (!resetn) begin
            mesh_r <= MESH_ID[1:0];
            enable_r <= 1'b1;
            fault_r <= 8'd0;
            door_req <= 1'b0; door_dst <= 2'd0; door_txn <= 8'd0;
            dbell_clr <= 1'b0;
        end else begin
            dbell_clr <= 1'b0;

            if (bad_remote_req) begin
                fault_r[F_RD_REMOTE] <= 1'b1;
            end
            if (|sw_fault) begin
                fault_r[F_SWITCH]    <= 1'b1;
            end
            if (flt_ack0) begin
                fault_r[F_ACK0]      <= 1'b1;
            end
            if (flt_axi_wr || flt_axi_lk) begin
                fault_r[F_AXI]       <= 1'b1;
            end
            if (flt_drop) begin
                fault_r[F_INJ]       <= 1'b1;
            end

            if (ob_db_ack) begin
                door_req <= 1'b0;
            end

            if (cfg_mine) begin
                case (cfg_sel)
                    8'h80: begin
                        enable_r  <= cfg_data[0];
                        dbell_clr <= cfg_data[1];
                        if (cfg_data[2]) begin
                            fault_r <= 8'd0;
                        end
                    end
                    8'h88: mesh_r <= cfg_data[1:0];
                    8'h90: begin
                        door_dst <= cfg_data[1:0];
                        door_txn <= cfg_data[15:8];
                        door_req <= 1'b1;
                    end
                    default: ;
                endcase
            end
        end
    end

endmodule

`default_nettype wire
