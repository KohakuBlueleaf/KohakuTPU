// MAG's L2 on the CONVERGED internal path: requesters -> here -> mag_dram_port.

// Inside mag_mem_port the store sat UPSTREAM of where the requesters meet, so
// mover and interlink traffic could never be staged; MEM_PORTS=4 cost 256 URAM.

// REACHED BY ADDRESS, never an instruction. A foreign mesh's address is not ours
// and passes through, which is what lets mesh 0 reach mesh 3's L2.

// STAGE=0 generates a straight wire.

`default_nettype none

module mag_stage_port #(
    parameter integer N        = 5,
    parameter integer ADDR_W   = 40,
    parameter integer SW       = 256,
    parameter integer STAGE    = 0,
    parameter integer BANKS    = 4,
    parameter integer ENTRIES  = 16384,
    parameter integer PIPE     = 1,
    parameter [1:0]   MESH_ID  = 2'd0,
    parameter [3:0]   AP_STAGE = 4'h0
)(
    input  wire                clk,
    input  wire                rst,

    // ---- from the requesters ----
    input  wire [N-1:0]        q_valid,
    output wire [N-1:0]        q_ready,
    input  wire [N*ADDR_W-1:0] q_addr,
    input  wire [N*16-1:0]     q_len,
    input  wire [N-1:0]        q_write,
    input  wire [N-1:0]        w_valid,
    output wire [N-1:0]        w_ready,
    input  wire [N*SW-1:0]     w_data,
    input  wire [N*SW/8-1:0]   w_strb,
    output wire [N-1:0]        r_valid,
    input  wire [N-1:0]        r_ready,
    output wire [N*SW-1:0]     r_data,
    output wire [N-1:0]        r_last,
    output wire [N-1:0]        b_valid,

    // ---- to mag_dram_port ----
    output wire [N-1:0]        dq_valid,
    input  wire [N-1:0]        dq_ready,
    output wire [N*ADDR_W-1:0] dq_addr,
    output wire [N*16-1:0]     dq_len,
    output wire [N-1:0]        dq_write,
    output wire [N-1:0]        dw_valid,
    input  wire [N-1:0]        dw_ready,
    output wire [N*SW-1:0]     dw_data,
    output wire [N*SW/8-1:0]   dw_strb,
    input  wire [N-1:0]        dr_valid,
    output wire [N-1:0]        dr_ready,
    input  wire [N*SW-1:0]     dr_data,
    input  wire [N-1:0]        dr_last,
    input  wire [N-1:0]        db_valid
);
    localparam integer IDX_W  = (N <= 1) ? 1 : $clog2(N);
    localparam integer SBYTES = SW / 8;
    localparam integer LSB    = $clog2(SBYTES);
    // SIZED. `SBYTES[ADDR_W-1:0]` selects past the end of a 32-bit integer and
    // those bits read X, which poisons the address on the first increment.
    localparam [ADDR_W-1:0] ASTEP = SW / 8;

    genvar g;
    generate
    if (STAGE == 0) begin : g_wire
        assign dq_valid = q_valid;  assign q_ready  = dq_ready;
        assign dq_addr  = q_addr;   assign dq_len   = q_len;
        assign dq_write = q_write;
        assign dw_valid = w_valid;  assign w_ready  = dw_ready;
        assign dw_data  = w_data;   assign dw_strb  = w_strb;
        assign r_valid  = dr_valid; assign dr_ready = r_ready;
        assign r_data   = dr_data;  assign r_last   = dr_last;
        assign b_valid  = db_valid;
    end else begin : g_stage

    // ---- the aperture test, per requester ------------------------------
    // Same decode mag_stage applies, needed here because the claim has to be
    // made BEFORE a request is forwarded.
    wire [N-1:0] mine;
    for (g = 0; g < N; g = g + 1) begin : g_mine
        wire [ADDR_W-1:0] a = q_addr[g*ADDR_W +: ADDR_W];
        // QUALIFIED BY q_valid: an idle requester's address is X, and an X here
        // reaches q_ready through `claimed` and stalls a handshake for good.
        assign mine[g] = q_valid[g] && a[39] && !a[38]
                         && (a[37:36] == MESH_ID) && (a[35:32] == AP_STAGE);
    end

    // ---- one claimed burst at a time -----------------------------------
    localparam [1:0] S_IDLE = 2'd0, S_WR = 2'd1, S_RD = 2'd2, S_RESP = 2'd3;
    reg [1:0]        st;
    reg [IDX_W-1:0]  id;
    // Active claimant one-hot, mirroring every `st` assignment below. Decoding
    // `(id == g)` per port sat 4 levels ahead of u_dram's arbiter, at -0.354.
    reg [N-1:0]      act_oh;
    reg [ADDR_W-1:0] addr;
    reg [15:0]       left;
    reg              is_wr;

    // W follows AW order PER REQUESTER, R follows AR: the same kind must never
    // be outstanding on both paths, or the wrong path eats the next beat.
    reg [3:0] dwr [0:N-1];      // writes forwarded to DRAM, B not yet back
    reg [3:0] drd [0:N-1];      // reads forwarded to DRAM, last beat not yet taken
    wire [N-1:0] d_wr_idle, d_rd_idle;
    for (g = 0; g < N; g = g + 1) begin : g_out
        assign d_wr_idle[g] = (dwr[g] == 4'd0);
        assign d_rd_idle[g] = (drd[g] == 4'd0);
    end

    // Round robin so one requester cannot hold the store.
    reg  [IDX_W-1:0] rr;
    wire [N-1:0]     want = q_valid & mine &
                            ((q_write & d_wr_idle) | (~q_write & d_rd_idle));
    reg  [IDX_W-1:0] pick;
    reg              pick_v;
    integer i;
    always @* begin
        pick = rr; pick_v = 1'b0;
        for (i = 0; i < N; i = i + 1) begin
            // The first claimer at or after `rr`, wrapping.
            if (!pick_v && want[(rr + i) % N]) begin
                pick   = (rr + i) % N;
                pick_v = 1'b1;
            end
        end
    end

    // THE GRANT IS REGISTERED: q_ready reading the rotating priority scan put 7
    // levels in the mover's backpressure chain, -0.498 ns against -0.239.
    reg             gnt_v;
    reg [IDX_W-1:0] gnt_id;
    always @(posedge clk) begin
        if (rst) gnt_v <= 1'b0;
        else if (gnt_v) gnt_v <= 1'b0;
        else if ((st == S_IDLE) && pick_v) begin
            gnt_v  <= 1'b1;
            gnt_id <= pick;
        end
    end

    wire              take      = gnt_v && (st == S_IDLE);
    wire [ADDR_W-1:0] pick_addr = q_addr[gnt_id*ADDR_W +: ADDR_W];
    wire [15:0]       pick_len  = q_len [gnt_id*16     +: 16];

    // ---- mag_stage, port B: one word per access, 1:1 with the beat -----
    // ONE read outstanding. The store returns in order after a fixed latency and
    // only one word is held here, so a second request would drop the first.
    reg               rd_out;
    reg [15:0]        req_left;
    reg               rd_hold;

    // req_nz, not `req_left != 0`: a 16-bit compare here reaches the requester's
    // ready through stg_gnt, and landed in the mover's AW enable at 15 levels.
    reg               req_nz;
    wire              b_go_w = (st == S_WR) && w_valid[id];
    wire              b_go_r = (st == S_RD) && !rd_out && !rd_hold && req_nz;
    wire              stg_gnt, stg_rvalid, stg_mine;
    wire [SW-1:0]     stg_rdata;

    mag_stage #(.DATA_W(SW), .ADDR_W(ADDR_W), .WORDS(4), .BANKS(BANKS),
                .ENTRIES(ENTRIES), .PIPE(PIPE), .MESH_ID(MESH_ID),
                .AP_STAGE(AP_STAGE)) u_stg (
        .clk(clk), .rst(rst),
        // Port A is mag_mem_port's entry-granular fill read and is not ours.
        .a_req(1'b0), .a_we(1'b0), .a_addr({ADDR_W{1'b0}}),
        .a_wdata({(4*SW){1'b0}}),
        .a_mine(), .a_gnt(), .a_fault(), .a_rvalid(), .a_rdata(),
        .b_req(b_go_w || b_go_r), .b_we(b_go_w), .b_addr(addr),
        .b_wdata(w_data[id*SW +: SW]),
        .b_mine(stg_mine), .b_gnt(stg_gnt),
        .b_rvalid(stg_rvalid), .b_rdata(stg_rdata)
    );

    // A read beat is held until the requester takes it.
    reg [SW-1:0]  rd_word;
    wire          rd_push = stg_rvalid && rd_out;
    wire          rd_take = rd_hold && r_ready[id];

    always @(posedge clk) begin
        if (rst) begin
            st <= S_IDLE; rr <= {IDX_W{1'b0}};
            // CONTROL only: pointer, counters, state -- each gates a handshake.
            // `addr` is a payload `st` qualifies, so it stays unreset.
            rd_hold <= 1'b0; rd_out <= 1'b0; req_left <= 16'd0; req_nz <= 1'b0;
            id <= {IDX_W{1'b0}}; left <= 16'd0; is_wr <= 1'b0;
            act_oh <= {N{1'b0}};
        end else begin
            case (st)
                S_IDLE: if (take) begin
`ifndef SYNTHESIS
                    $display("  TAKE %0t pick=%0d addr=%h len=%0d wr=%b qv=%b mine=%b",
                             $time, gnt_id, pick_addr, pick_len, q_write[gnt_id],
                             q_valid, mine);
`endif
                    id       <= gnt_id;
                    addr     <= pick_addr;
                    left     <= pick_len + 16'd1;   // q_len 0 means one beat
                    req_left <= pick_len + 16'd1;
                    req_nz   <= 1'b1;
                    is_wr    <= q_write[gnt_id];
                    st       <= q_write[gnt_id] ? S_WR : S_RD;
                    rr       <= (gnt_id + 1) % N;
                    act_oh   <= ({{(N-1){1'b0}}, 1'b1} << gnt_id);
                end
                S_WR: if (stg_gnt) begin
                    addr <= addr + ASTEP;
                    left <= left - 16'd1;
                    if (left == 16'd1) st <= S_RESP;
                end
                S_RD: begin
                    // The request side steps on the GRANT, the return side on the
                    // TAKE, so a beat is counted once on each.
                    if (b_go_r && stg_gnt) begin
                        addr     <= addr + ASTEP;
                        req_left <= req_left - 16'd1;
                        req_nz   <= (req_left != 16'd1);
                        rd_out   <= 1'b1;
                    end
                    if (rd_push) begin
                        rd_hold <= 1'b1; rd_word <= stg_rdata; rd_out <= 1'b0;
                    end
                    if (rd_take) begin
                        rd_hold <= 1'b0;
                        left    <= left - 16'd1;
                        if (left == 16'd1) begin
                            st <= S_IDLE; act_oh <= {N{1'b0}};
                        end
                    end
                end
                S_RESP: begin st <= S_IDLE; act_oh <= {N{1'b0}}; end
                default: begin st <= S_IDLE; act_oh <= {N{1'b0}}; end
            endcase
        end
    end

    integer d;
    always @(posedge clk) begin
        if (rst) begin
            for (d = 0; d < N; d = d + 1) begin
                dwr[d] <= 4'd0; drd[d] <= 4'd0;
            end
        end else begin
            for (d = 0; d < N; d = d + 1) begin
                dwr[d] <= dwr[d]
                        + ((dq_valid[d] && dq_ready[d] &&  dq_write[d]) ? 4'd1 : 4'd0)
                        - (db_valid[d] ? 4'd1 : 4'd0);
                drd[d] <= drd[d]
                        + ((dq_valid[d] && dq_ready[d] && !dq_write[d]) ? 4'd1 : 4'd0)
                        - ((dr_valid[d] && r_ready[d] && dr_last[d]) ? 4'd1 : 4'd0);
            end
        end
    end

    // ---- steer -----------------------------------------------------------
    for (g = 0; g < N; g = g + 1) begin : g_steer
        wire claimed = mine[g];
        wire active  = act_oh[g];

        // A claimed request is never offered to the DRAM port, and a forwarded
        // one waits while this requester has a staged burst of the same kind.
        wire fwd_ok = !(active && (q_write[g] == is_wr));
        assign dq_valid[g] = q_valid[g] && !claimed && fwd_ok;
        assign q_ready[g]  = claimed ? (take && (gnt_id == g[IDX_W-1:0]))
                                     : (dq_ready[g] && fwd_ok);
        assign dq_addr[g*ADDR_W +: ADDR_W] = q_addr[g*ADDR_W +: ADDR_W];
        assign dq_len[g*16 +: 16]          = q_len[g*16 +: 16];
        assign dq_write[g]                 = q_write[g];

        // W follows its own AW: only the active claimant's beats are consumed
        // here, everyone else's go downstream.
        wire wr_here = active && (st == S_WR);
        assign dw_valid[g] = w_valid[g] && !wr_here;
        assign w_ready[g]  = wr_here ? stg_gnt : dw_ready[g];
        assign dw_data[g*SW +: SW]         = w_data[g*SW +: SW];
        assign dw_strb[g*SBYTES +: SBYTES] = w_strb[g*SBYTES +: SBYTES];

        wire rd_here = active && (st == S_RD);
        assign r_valid[g] = rd_here ? rd_hold : dr_valid[g];
        assign dr_ready[g] = rd_here ? 1'b0   : r_ready[g];
        assign r_data[g*SW +: SW] = rd_here ? rd_word
                                            : dr_data[g*SW +: SW];
        assign r_last[g]  = rd_here ? (rd_hold && (left == 16'd1)) : dr_last[g];

        assign b_valid[g] = db_valid[g] |
                            ((st == S_RESP) && (id == g[IDX_W-1:0]));
    end

`ifndef SYNTHESIS
    // The store declines an address this port claimed: the two decodes have
    // drifted apart and the burst would hang with no beat ever returning.
    always @(posedge clk) if (!rst && (st != S_IDLE) && !stg_mine)
        $display("%0t ERROR mag_stage_port: claimed %h but mag_stage says not mine",
                 $time, addr);

    // A burst that stops advancing, reported ONCE with everything that decides
    // whether it can: a hang otherwise shows only as a bench timeout.
    integer stuck;
    reg     said;
    always @(posedge clk) begin
        if (rst) begin stuck <= 0; said <= 1'b0; end
        else if (st == S_IDLE) begin stuck <= 0; said <= 1'b0; end
        else begin
            stuck <= stuck + 1;
            if ((stuck > 400) && !said) begin
                said <= 1'b1;
                // Indented: xsim.py's keep() drops a line starting with a digit.
                $display("  STALL %0t mag_stage_port st=%0d id=%0d addr=%h left=%0d req_left=%0d",
                         $time, st, id, addr, left, req_left);
                $display("           stg_mine=%b stg_gnt=%b stg_rvalid=%b rd_out=%b rd_hold=%b",
                         stg_mine, stg_gnt, stg_rvalid, rd_out, rd_hold);
                $display("           w_valid=%b w_ready=%b r_ready=%b q_valid=%b mine=%b",
                         w_valid, w_ready, r_ready, q_valid, mine);
            end
        end
    end
`endif

    end
    endgenerate

endmodule

`default_nettype wire
