// rv64_mmu -- Sv39 translation: a direct-mapped TLB and a hardware walker.
//
// IT LIVES OUTSIDE THE CORE. `rv64_core` is a physical-address machine and stays
// one; translation is a property of the system, so it sits between the core's
// memory port and the node. See .plan/syscore/decisions.md D3. The mesh compute
// unit configuration therefore carries none of this and pays nothing for it.
//
// AN ENTRY IS 57 BITS BECAUSE THE CARD IS 40-BIT PHYSICAL. Sv39's PPN field is
// 44 bits, but no address on this card exceeds 40, so the stored PPN is 28 bits
// and an entry is {valid, tag[21:0], ppn[27:0], perms[5:0]} = 57 -- inside a
// block-RAM port. Storing the architectural 44 would make it 73 and the array
// would silently become LUTs, which is the failure this tree has already paid
// for once with a 74-bit ROM.
//
// DIRECT MAPPED, NOT A CAM. A 32-entry fully-associative array is thirty-two
// 22-bit comparators and a priority encoder, and it buys hit rate this machine
// does not need: its addresses are computable ahead of time and its working sets
// are descriptor-shaped rather than pointer-shaped.
//
// THE WALKER USES THE UNCACHED PORT. Page tables live in staging (design s8),
// staging is outside the cached range, so a walk never enters the L1 and cannot
// wait on the miss that triggered it -- which is the deadlock design s5 raised.

`default_nettype none

module rv64_mmu #(
    parameter integer ENTRIES  = 32,          // power of two
    parameter integer ADDR_W   = 40,
    parameter         MEM_PRIM = "block"
)(
    input  wire                clk,
    input  wire                resetn,

    // ---- control ----
    input  wire [63:0]         satp,          // MODE 63:60, ASID 59:44, PPN 43:0
    input  wire [1:0]          priv,          // 3 machine, 1 supervisor, 0 user
    input  wire                sum,           // mstatus.SUM
    input  wire                mxr,           // mstatus.MXR
    input  wire                sfence,        // pulse: drop every entry

    // ---- translation, one at a time ----
    input  wire                req,
    input  wire [63:0]         va,
    input  wire                is_store,
    input  wire                is_fetch,
    output wire                busy,          // hold the core
    output wire [ADDR_W-1:0]   pa,
    output wire                fault,
    output reg                 fault_fetch,   // ...and it belongs to a fetch
    output reg  [3:0]          cause,         // RISC-V exception code

    // ---- the walker's own memory port, uncached ----
    output reg                 w_req,
    output reg  [ADDR_W-1:0]   w_addr,
    input  wire                w_ack,
    input  wire [63:0]         w_data
);
    localparam integer IDX_W = $clog2(ENTRIES);
    localparam integer TAG_W = 27 - IDX_W;         // VPN is 27 bits
    localparam integer PPN_W = ADDR_W - 12;
    localparam integer EW    = 1 + TAG_W + PPN_W + 6;

    // The entry's field offsets, used by BOTH the read decode and the write, so
    // the two cannot drift. They did: the read took the PPN from a literal 11
    // while the write placed it at 6, and every translation came back shifted
    // five bits with no fault and no failing bench.
    localparam integer P_PERM = 0;
    localparam integer P_PPN  = P_PERM + 6;
    localparam integer P_TAG  = P_PPN  + PPN_W;
    localparam integer P_V    = P_TAG  + TAG_W;

    // Sv39 is off unless satp.MODE is 8, and machine mode never translates.
    wire enabled = (satp[63:60] == 4'd8) && (priv != 2'd3);

    wire [26:0] vpn = va[38:12];
    wire [IDX_W-1:0] t_idx = vpn[IDX_W-1:0];
    wire [TAG_W-1:0] t_tag = vpn[26:IDX_W];

    reg              t_we;
    reg  [IDX_W-1:0] t_wa;
    reg  [EW-1:0]    t_wd;
    wire [EW-1:0]    t_q;

    kohaku_sdpram #(.WIDTH(EW), .DEPTH(ENTRIES),
                    .MEM_PRIM(MEM_PRIM), .READ_LAT(1)) u_tlb (
        .clk(clk),
        .wr_en(t_we), .wr_addr(t_wa), .wr_data(t_wd),
        .rd_en(1'b1), .rd_addr(t_idx), .rd_data(t_q)
    );

    wire             e_v    = t_q[P_V];
    wire [TAG_W-1:0] e_tag  = t_q[P_TAG  +: TAG_W];
    wire [PPN_W-1:0] e_ppn  = t_q[P_PPN  +: PPN_W];
    wire [5:0]       e_perm = t_q[P_PERM +: 6];  // {D, A, U, X, W, R}

    // The tag is compared against the REGISTERED request, because the array
    // answers a cycle after the address goes in.
    reg  [26:0] q_vpn;
    reg  [11:0] q_off;
    reg         q_store, q_fetch, q_active;
    always @(posedge clk) begin
        if (req) begin
            q_vpn   <= vpn;
            q_off   <= va[11:0];
            q_store <= is_store;
            q_fetch <= is_fetch;
        end
        q_active <= req;
    end

    wire hit = e_v && (e_tag == q_vpn[26:IDX_W]);

    // ---- permission ---------------------------------------------------------
    wire p_r = e_perm[0], p_w = e_perm[1], p_x = e_perm[2];
    wire p_u = e_perm[3], p_a = e_perm[4], p_d = e_perm[5];
    wire readable = p_r || (mxr && p_x);
    wire priv_ok  = (priv == 2'd0) ? p_u : (!p_u || sum);
    wire perm_ok  = priv_ok && p_a
                 && (q_fetch ? p_x : q_store ? (p_w && p_d) : readable);

    // ---- the walk -----------------------------------------------------------
    localparam [2:0] W_IDLE = 3'd0, W_REQ = 3'd1, W_WAIT = 3'd2;
    localparam [2:0] W_DONE = 3'd3, W_FAULT = 3'd4, W_DONE2 = 3'd5;
    reg [2:0]  wst;
    reg [IDX_W:0] sweep;
    reg        sweeping;
    reg [1:0]  level;
    reg [ADDR_W-1:0] w_base;
    reg [63:0] pte;

    wire pte_v = w_data[0];
    wire pte_r = w_data[1], pte_w_ = w_data[2], pte_x = w_data[3];
    wire pte_leaf = pte_r || pte_x;
    // A PPN wider than the card is a malformed table, not a translation.
    wire [PPN_W-1:0] pte_ppn = w_data[10 +: PPN_W];

    // THE WALK OWNS ITS OWN COPY OF THE REQUEST. `q_vpn` follows whatever is
    // on the port, and the wrapper switches the port to a data access the
    // cycle one arrives -- so a fetch walk indexed by `q_vpn` finished with the
    // data address's VPN slices and installed that hybrid as a translation.
    reg [26:0] w_vpn;
    reg        w_fetch, w_store;

    wire [8:0] vpn_sel = (level == 2'd2) ? w_vpn[26:18]
                       : (level == 2'd1) ? w_vpn[17:9]
                                         : w_vpn[8:0];

    // A SUPERPAGE IS FILLED AS THE 4 KB SLICE THE ACCESS ASKED FOR. The array is
    // indexed by the whole VPN, so each 4 KB piece of a 2 MB or 1 GB mapping
    // earns its own entry on demand and no level field has to be stored --
    // taking `pte_ppn` unmodified instead translates the whole superpage to its
    // first page, silently.
    wire [PPN_W-1:0] eff_ppn = (level == 2'd2)
                                   ? {pte_ppn[PPN_W-1:18], w_vpn[17:0]}
                             : (level == 2'd1)
                                   ? {pte_ppn[PPN_W-1:9],  w_vpn[8:0]}
                                   : pte_ppn;

    // A superpage whose PPN is not aligned to its own size is a malformed
    // table, and the specification requires a fault rather than a truncation.
    wire sp_bad = (level == 2'd2) ? |pte_ppn[17:0]
                : (level == 2'd1) ? |pte_ppn[8:0]
                                  : 1'b0;

    // A FAULT IS A LEVEL, NOT A PULSE. The consumer is a stalled pipeline that
    // takes the trap at its next instruction boundary, which is not the cycle
    // the walk gave up on. Held until the request is withdrawn -- and it also
    // suppresses the retry, or a failed walk restarts the moment `busy` drops
    // and the core never sees a fault at all.
    reg  fault_p, fault_lat;
    assign fault = fault_p || fault_lat;

    wire miss = q_active && enabled && !hit && !fault;

    // TRANSLATION OFF COSTS NOTHING. Machine mode, or `satp.MODE != 8`, takes
    // the address straight through combinationally, so a machine-mode runtime
    // runs at the IPC it had before this module existed. With Sv39 on, the
    // array answers a cycle after the address goes in, so even a hit holds the
    // core for one cycle; pipelining that into E is a later optimisation.
    wire resolved = q_active && hit && !sweeping && (wst == W_IDLE);

    // `busy` MUST BE REGISTER-DERIVED. It gates the core's stall, and the stall
    // gates the trap boundary, so a combinational path from the TLB array to
    // here lands on every CSR register's write enable: measured 25 logic levels
    // and WNS -3.842 at the node, from one root, when `hit` fed it directly.
    // The cost is one more cycle per translated access, only with Sv39 on.
    // ...AND IT ANSWERS FOR THE REQUEST ON THE PORT NOW. `resolved` is about the
    // address presented a cycle ago; carried forward unqualified it released
    // a data access on the fetch's hit when the port switched between them.
    reg resolved_q;
    wire same_req = (
        (vpn == q_vpn)
        && (is_fetch == q_fetch)
        && (is_store == q_store)
    );
    always @(posedge clk) begin
        if (!resetn) begin
            resolved_q <= 1'b0;
        end else begin
            resolved_q <= resolved && same_req;
        end
    end

    // A latched fault holds only its owner; the other requester runs.
    wire fault_mine = fault && (is_fetch == fault_fetch);
    assign busy = fault_mine ? 1'b0 : (enabled && req && !resolved_q);
    assign pa   = enabled ? {e_ppn, q_off} : va[ADDR_W-1:0];

    always @(posedge clk) begin
        if (!resetn) begin
            wst    <= W_IDLE;
            w_req  <= 1'b0;
            t_we   <= 1'b0;
            fault_p   <= 1'b0;
            fault_lat <= 1'b0;
            sweep  <= {(IDX_W+1){1'b0}};
            sweeping <= 1'b1;
        end
        else begin
            t_we  <= 1'b0;
            fault_p <= 1'b0;
            if (fault_p) begin
                fault_lat <= 1'b1;
            end
            // Withdrawn by its owner, or displaced by the other requester.
            if (!req || (is_fetch != fault_fetch)) begin
                fault_lat <= 1'b0;
            end

            // The array has no reset, and `sfence` is the same sweep.
            if (sweeping) begin
                t_we <= 1'b1;
                t_wa <= sweep[IDX_W-1:0];
                t_wd <= {EW{1'b0}};
                sweep <= sweep + 1'b1;
                if (sweep[IDX_W-1:0] == {IDX_W{1'b1}}) begin
                    sweeping <= 1'b0;
                end
            end
            else if (sfence) begin
                sweep    <= {(IDX_W+1){1'b0}};
                sweeping <= 1'b1;
            end

            case (wst)
                W_IDLE: begin
                    if (miss && !sweeping) begin
                        level   <= 2'd2;
                        w_base  <= {satp[PPN_W-1:0], 12'd0};
                        w_vpn   <= q_vpn;
                        w_fetch <= q_fetch;
                        w_store <= q_store;
                        wst     <= W_REQ;
                    end
                    else if (q_active && enabled && hit && !perm_ok
                             && !fault) begin
                        fault_p     <= 1'b1;
                        fault_fetch <= q_fetch;
                        cause <= q_fetch ? 4'd12 : q_store ? 4'd15 : 4'd13;
                    end
                end
                W_REQ: begin
                    w_addr <= w_base + {{(ADDR_W-12){1'b0}}, vpn_sel, 3'd0};
                    w_req  <= 1'b1;
                    wst    <= W_WAIT;
                end
                W_WAIT: if (w_ack) begin
                    w_req <= 1'b0;
                    if (!pte_v || (!pte_r && pte_w_)) begin
                        wst <= W_FAULT;
                    end
                    else if (pte_leaf && sp_bad) begin
                        wst <= W_FAULT;
                    end
                    else if (pte_leaf) begin
                        t_we <= 1'b1;
                        t_wa <= w_vpn[IDX_W-1:0];
                        t_wd <= {1'b1, w_vpn[26:IDX_W], eff_ppn,
                                 w_data[7], w_data[6], w_data[4],
                                 pte_x, pte_w_, pte_r};
                        wst  <= W_DONE;
                    end
                    else if (level == 2'd0) begin
                        wst <= W_FAULT;              // no leaf at the last level
                    end
                    else begin
                        level  <= level - 2'd1;
                        w_base <= {pte_ppn, 12'd0};
                        wst    <= W_REQ;
                    end
                end
                // TWO cycles, not one. `t_we` is non-blocking, so it is high
                // during W_DONE and the array writes at the edge ENDING it; a
                // read-first array issued that same cycle still returns the old
                // entry. One cycle here re-probed stale, missed, and walked the
                // whole table a second time -- 6 PTE reads per translation
                // instead of 3, with a correct result and no failing check.
                W_DONE:  wst <= W_DONE2;
                W_DONE2: wst <= W_IDLE;
                default: begin
                    fault_p     <= 1'b1;
                    fault_fetch <= w_fetch;
                    cause <= w_fetch ? 4'd12 : w_store ? 4'd15 : 4'd13;
                    wst   <= W_IDLE;
                end
            endcase
        end
    end

endmodule

`default_nettype wire
