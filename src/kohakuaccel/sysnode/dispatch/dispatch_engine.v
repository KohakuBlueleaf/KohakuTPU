// dispatch_engine -- replays a controller's queued register writes at one a
// cycle, each WAIT entry held until a hardware completion counter reaches its
// value. It never decodes what it writes: docs/spec/dispatch-engine.md.
//
// Entry codes: 0..31 write target register `code`; 32 WAIT, data =
// {src[60:56], want[CTR_W-1:0]}, src < NCTR a unit counter, src = NCTR the
// mover's done count since the last clear. A CU_SIGNAL from a mapped source
// with a retirement code (0x00, 0x01) is counted and claimed from the
// controller's queue; every other signal still reaches the controller.

`default_nettype none

module dispatch_engine #(
    parameter integer DEPTH      = 64,   // queued entries, a power of two
    parameter integer NCTR       = 16,   // unit counters, a power of two <= 32
    parameter integer CTR_W      = 12,   // waits stay within 2^(CTR_W-1) of the count
    parameter integer POS_WIDTH  = 4,
    parameter integer GO_CODE    = 5,    // the mailbox commit
    parameter integer GO_GAP     = 3,    // cycles until the mailbox shows the commit
    // Codes MV_LO..IL_LO-1 are the mover's registers: its config queue takes
    // them during a move, so they wait only for `mv_room`. Codes >= IL_LO wait
    // for every move to finish.
    parameter integer MV_LO      = 8,
    parameter integer IL_LO      = 24,
    parameter integer MV_GO_CODE = 8,
    parameter integer MV_GO_BIT  = 16
)(
    input  wire                  clk,
    input  wire                  resetn,

    input  wire                  enq_en,
    input  wire [5:0]            enq_code,
    input  wire [63:0]           enq_data,
    input  wire                  cfg_en,
    input  wire [1:0]            cfg_addr,   // 1 CTL {clear[1], enable[0]}, 2 MAP
    input  wire [63:0]           cfg_data,
    input  wire [4:0]            rd_addr,    // 0 STAT, 1 MOVES, 16+k counter k
    output reg  [63:0]           rd_data,

    output wire                  t_en,
    output wire [4:0]            t_code,
    output wire [63:0]           t_data,
    input  wire                  t_hold,

    input  wire                  tx_offered,
    input  wire                  mv_busy,
    input  wire [15:0]           mv_done,
    input  wire                  mv_room,

    input  wire                  sig_v,
    input  wire [POS_WIDTH-1:0]  sig_x,
    input  wire [POS_WIDTH-1:0]  sig_y,
    input  wire [7:0]            sig_code,
    output wire                  sig_claim
);
    localparam integer AW = $clog2(DEPTH);
    localparam integer CW = $clog2(NCTR);
    localparam integer MW = 2 * POS_WIDTH;
    localparam [5:0]   C_WAIT = 6'd32;

    reg              enable, clearing, ovf;
    reg  [CW:0]      clr_i;
    reg  [CTR_W-1:0] gos, mv_base;
    reg  [15:0]      issued;
    reg  [1:0]       cool;

    wire clr = cfg_en && cfg_addr == 2'd1 && cfg_data[1];

    // ------------------------------------------------------------ the queue
    (* ram_style = "distributed" *) reg [69:0] q [0:DEPTH-1];
    reg  [AW-1:0] wp, rp;
    reg  [AW:0]   cnt;
    reg           head_v;
    reg  [5:0]    head_c;
    reg  [63:0]   head_d;

    wire full = (cnt == DEPTH[AW:0]);
    wire push = enq_en && !full;
    wire pop;
    wire load = (!head_v || pop) && (cnt != 0);

    always @(posedge clk) begin
        if (push) begin
            q[wp] <= {enq_code, enq_data};
        end
    end

    always @(posedge clk) begin
        if (!resetn || clr) begin
            wp     <= {AW{1'b0}};
            rp     <= {AW{1'b0}};
            cnt    <= {(AW+1){1'b0}};
            head_v <= 1'b0;
        end
        else begin
            if (push) begin
                wp <= wp + 1'b1;
            end
            if (load) begin
                rp <= rp + 1'b1;
            end
            cnt <= cnt + {{AW{1'b0}}, push} - {{AW{1'b0}}, load};
            if (load) begin
                head_v <= 1'b1;
            end
            else if (pop) begin
                head_v <= 1'b0;
            end
        end
    end

    always @(posedge clk) begin
        if (load) begin
            {head_c, head_d} <= q[rp];
        end
    end

    // ------------------------------------------------------------ counters
    // One write, three reads (the increment's, the head WAIT's, the
    // controller's): a RAM32M per two bits.
    (* ram_style = "distributed" *) reg [CTR_W-1:0] ctr [0:NCTR-1];
    (* ram_style = "distributed" *) reg [CW:0]      smap [0:(1<<MW)-1];

    integer ii;
    initial begin
        for (ii = 0; ii < (1 << MW); ii = ii + 1) begin
            smap[ii] = {(CW+1){1'b0}};
        end
        for (ii = 0; ii < NCTR; ii = ii + 1) begin
            ctr[ii] = {CTR_W{1'b0}};
        end
    end

    wire [CW:0] hit = smap[{sig_y, sig_x}];
    assign sig_claim = enable && !clearing && hit[CW]
                    && (sig_code == 8'h00 || sig_code == 8'h01);

    reg          inc_v;
    reg [CW-1:0] inc_i;
    always @(posedge clk) begin
        inc_v <= resetn && sig_v && sig_claim;
        inc_i <= hit[CW-1:0];
    end

    wire [CTR_W-1:0] inc_old = ctr[inc_i];
    wire             ctr_we  = clearing || inc_v;
    wire [CW-1:0]    ctr_wa  = clearing ? clr_i[CW-1:0] : inc_i;
    wire [CTR_W-1:0] ctr_wd  = clearing ? {CTR_W{1'b0}} : inc_old + 1'b1;
    always @(posedge clk) begin
        if (ctr_we) begin
            ctr[ctr_wa] <= ctr_wd;
        end
    end

    always @(posedge clk) begin
        if (cfg_en && cfg_addr == 2'd2) begin
            smap[{cfg_data[8 +: POS_WIDTH], cfg_data[0 +: POS_WIDTH]}]
                <= {cfg_data[31], cfg_data[16 +: CW]};
        end
    end

    // ---------------------------------------------------------------- issue
    wire [CTR_W-1:0] mv_rel  = mv_done[CTR_W-1:0] - mv_base;
    wire [CTR_W-1:0] owed    = gos - mv_rel;
    wire [4:0]       src     = head_d[60:56];
    wire [CTR_W-1:0] src_v   = (src < NCTR) ? ctr[src[CW-1:0]] : mv_rel;
    wire [CTR_W-1:0] behind  = src_v - head_d[CTR_W-1:0];
    wire             wait_ok = !behind[CTR_W-1];

    wire is_wait  = (head_c == C_WAIT);
    wire is_go    = (head_c[4:0] == GO_CODE[4:0]);
    wire mv_class = (head_c[4:0] >= MV_LO[4:0]) && (head_c[4:0] < IL_LO[4:0]);
    wire il_class = (head_c[4:0] >= IL_LO[4:0]);
    wire moving   = mv_busy || (owed != {CTR_W{1'b0}} && !owed[CTR_W-1]);
    wire mv_start = (head_c[4:0] == MV_GO_CODE[4:0]) && head_d[MV_GO_BIT];
    wire write_ok = !t_hold
                 && !(is_go && (tx_offered || cool != 2'd0))
                 && !(mv_class && !mv_room)
                 && !(il_class && moving);
    wire run      = enable && !clearing && head_v;

    assign pop    = run && (is_wait ? wait_ok : write_ok);
    assign t_en   = run && !is_wait && write_ok;
    assign t_code = head_c[4:0];
    assign t_data = head_d;

    always @(posedge clk) begin
        if (!resetn) begin
            enable   <= 1'b0;
            clearing <= 1'b0;
            ovf      <= 1'b0;
            clr_i    <= {(CW+1){1'b0}};
            gos      <= {CTR_W{1'b0}};
            mv_base  <= {CTR_W{1'b0}};
            issued   <= 16'd0;
            cool     <= 2'd0;
        end
        else begin
            if (cool != 2'd0) begin
                cool <= cool - 2'd1;
            end
            if (t_en && is_go) begin
                cool <= GO_GAP[1:0];
            end
            if (pop) begin
                issued <= issued + 16'd1;
            end
            if (t_en && mv_start) begin
                gos <= gos + 1'b1;
            end
            if (enq_en && full) begin
                ovf <= 1'b1;
            end
            if (clearing) begin
                clr_i <= clr_i + 1'b1;
                if (clr_i == NCTR[CW:0] - 1'b1) begin
                    clearing <= 1'b0;
                end
            end
            if (cfg_en && cfg_addr == 2'd1) begin
                enable <= cfg_data[0];
            end
            if (clr) begin
                clearing <= 1'b1;
                clr_i    <= {(CW+1){1'b0}};
                ovf      <= 1'b0;
                gos      <= {CTR_W{1'b0}};
                mv_base  <= mv_done[CTR_W-1:0];
                issued   <= 16'd0;
                cool     <= 2'd0;
            end
        end
    end

    // ---------------------------------------------------------------- reads
    wire [CTR_W-1:0] rd_ctr = ctr[rd_addr[CW-1:0]];
    wire [AW+1:0]    used   = cnt + {{AW{1'b0}}, head_v};
    always @(*) begin
        case (rd_addr)
            5'd0: rd_data = {16'hDE01, AW[7:0], CW[3:0], ovf, clearing,
                             head_v && !pop && run, enable, issued,
                             {(16-AW-2){1'b0}}, used};
            5'd1: rd_data = {{(32-CTR_W){1'b0}}, gos, {(32-CTR_W){1'b0}}, mv_rel};
            default: rd_data = (rd_addr[4] && rd_addr[3:0] < NCTR)
                             ? {{(64-CTR_W){1'b0}}, rd_ctr} : 64'd0;
        endcase
    end

endmodule

`default_nettype wire
