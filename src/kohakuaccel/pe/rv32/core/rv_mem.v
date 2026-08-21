// rv_mem -- the memory stage, and the ONE address decoder the PE has.
//
// Four regions, decided by the top four bits of the software address and
// nothing else (docs/arch/pe/programming.md):
//
//   0x1xxx_xxxx  scratchpad    external L1 data. Real SRAM, home of its own
//                              addresses, always hits, no tags.
//   0x2xxx_xxxx  local control status, internal-L1 flush-all and
//                              invalidate-all, halt cause, kick argument.
//   0x3xxx_xxxx  peer window   UNCACHED store straight out of the NoC
//                              requestor. Push only: a load here faults.
//   0x8xxx_xxxx+ global DRAM   through the internal L1.
//   anything else               faults. 0x0xxx_xxxx is the instruction window,
//                              which the data side deliberately cannot reach.
//
// A PUSH MUST NEVER SIT IN THE WRITE-BACK CACHE. That is why the peer region is
// decoded separately rather than being "DRAM that happens to live elsewhere":
// the doorbell protocol needs stores on the wire in program order, and a dirty
// line goes out whenever the cache feels like it. This stage holds until the
// requestor has taken the push, which is what makes program order equal
// arrival order.
//
// ADDRESS HERE, DATA IN WRITEBACK. The arrays register their address input, so
// this stage presents `m_addr` and the word is out in the stage after it. One
// port serves reads and writes because the write address is the same register.
// Trying to read the NEXT access's address on the same port is what forces a
// second port or a lost store.

`default_nettype none

module rv_mem #(
    parameter integer SPAD_WORDS = 2048,
    parameter integer POS_WIDTH  = 4,
    parameter integer DSP_EN     = 0
)(
    input  wire        clk,
    input  wire        resetn,

    // ---- from EX, combinational ----
    input  wire [31:0] ex_addr,
    input  wire        x_load,
    input  wire        x_store,
    output wire        ex_bad_region,

    // ---- the MEM-stage register, from EX ----
    input  wire        m_valid,
    input  wire [4:0]  m_rd,
    input  wire        m_wen,
    input  wire [31:0] m_pc,
    input  wire [31:0] m_val,
    input  wire [31:0] m_addr,
    input  wire        m_load,
    input  wire        m_store,
    input  wire [2:0]  m_f3,
    input  wire [3:0]  m_be,
    input  wire [31:0] m_sdata,

    // ---- scratchpad, port B ----
    output wire [$clog2(SPAD_WORDS)-1:0] spad_addr,
    output wire [3:0]  spad_we,
    output wire [31:0] spad_wdata,
    input  wire [31:0] spad_rdata,

    // ---- internal L1 ----
    output wire [31:0] l1_probe,
    output wire        l1_req,
    output wire        l1_we,
    output wire [3:0]  l1_be,
    output wire [31:0] l1_addr,
    output wire [31:0] l1_wdata,
    input  wire [31:0] l1_rdata,
    input  wire        l1_stall,
    output reg         l1_flush,
    output reg         l1_inval,
    input  wire        l1_flush_busy,

    // ---- peer push, into the NoC requestor ----
    output wire                  push_valid,
    input  wire                  push_ready,
    output wire [POS_WIDTH-1:0]  push_dx,
    output wire [POS_WIDTH-1:0]  push_dy,
    output wire                  push_win,
    output wire [13:0]           push_gran,
    output wire [2:0]            push_sel,
    output wire [3:0]            push_be,
    output wire [31:0]           push_data,

    // ---- control-region sources ----
    input  wire [7:0]  ctl_coreid,
    input  wire [31:0] ctl_arg,
    input  wire [31:0] ctl_cycle,
    input  wire [31:0] ctl_instret,
    input  wire [1:0]  ctl_cause,
    input  wire [15:0] ctl_wr_out,

    // ---- the vector unit, at DSP_EN only ----
    // `base_stall` is this stage's OWN stall. The vector unit needs it
    // separately from `stall_m`: fed the combined signal it would see its own
    // stall as a reason to hold, which is a loop.
    input  wire        vec_stall,
    input  wire        vec_fault,
    input  wire        vec_w_valid,
    input  wire [31:0] vec_w_val,
    output wire        base_stall,

    // A scalar store into the vector scratchpad. It never stalls here: the
    // vector unit takes it on the port it owns, so nothing about the NoC's
    // window writer can reach this stage.
    output wire        vsp_st_valid,
    output wire [31:0] vsp_st_addr,
    output wire [3:0]  vsp_st_be,
    output wire [31:0] vsp_st_data,

    // ---- out ----
    output wire        stall_m,
    output wire [31:0] wstage_val,   // the WB value, and the distance-3 forward

    // ---- to WB ----
    output reg         w_valid,
    output reg  [4:0]  w_rd,
    output reg         w_wen,
    output reg  [31:0] w_val,
    output reg  [31:0] w_pc
);
    localparam integer SAW = $clog2(SPAD_WORDS);

    localparam [2:0] R_SPAD = 3'd1, R_CTL = 3'd2, R_PEER = 3'd3,
                     R_DRAM = 3'd4, R_VSPAD = 3'd5, R_BAD = 3'd7;

    function [2:0] region_of;
        input [31:0] a;
        begin
            if (a[31])                    region_of = R_DRAM;
            else case (a[30:28])
                3'd1:    region_of = R_SPAD;
                3'd2:    region_of = R_CTL;
                3'd3:    region_of = R_PEER;
                3'd4:    region_of = R_VSPAD;
                default: region_of = R_BAD;
            endcase
        end
    endfunction

    // Decoded in EX as well, so the halt joins the illegal and misaligned
    // checks there: a fault found in MEM would have to kill a stage the
    // redirect path does not reach.
    wire [2:0] x_region = region_of(ex_addr);
    // The vector scratchpad is STORE ONLY from the scalar side, exactly like a
    // peer window: a store stages data for the vector unit to read with `vld`,
    // and a load faults rather than paying for a read port on the scalar load
    // path -- which is the base core's critical path and the one place it can
    // least afford another mux. Without the extension the region is unmapped.
    wire x_bad_vspad = (x_region == R_VSPAD) && ((DSP_EN == 0) || x_load);
    // The vector unit's own refusals -- an encoding this build does not carry,
    // or a misaligned vector address -- join here, so every fault the core has
    // is still raised in one stage and takes one path.
    assign ex_bad_region = ((x_load || x_store) &&
                            ((x_region == R_BAD) || x_bad_vspad
                             || (x_load && (x_region == R_PEER))))
                         || vec_fault;

    wire [2:0] region = region_of(m_addr);
    wire acc   = m_valid && (m_load || m_store);
    wire is_sp = acc && (region == R_SPAD);
    wire is_ct = acc && (region == R_CTL);
    wire is_pe = acc && (region == R_PEER);
    wire is_dr = acc && (region == R_DRAM);
    wire is_vs = acc && (region == R_VSPAD) && (DSP_EN != 0);

    // ---- scratchpad -------------------------------------------------------
    assign spad_addr  = m_addr[SAW+1:2];
    assign spad_we    = (is_sp && m_store && !stall_m) ? m_be : 4'd0;
    assign spad_wdata = m_sdata;

    // ---- internal L1 ------------------------------------------------------
    assign l1_probe = ex_addr;
    assign l1_req   = is_dr;
    assign l1_we    = m_store;
    assign l1_be    = m_be;
    assign l1_addr  = m_addr;
    assign l1_wdata = m_sdata;

    // ---- peer push --------------------------------------------------------
    assign push_valid = is_pe && m_store;
    assign push_dx    = m_addr[24 +: POS_WIDTH];
    assign push_dy    = m_addr[20 +: POS_WIDTH];
    assign push_win   = m_addr[19];
    assign push_gran  = m_addr[18:5];
    assign push_sel   = m_addr[4:2];
    assign push_be    = m_be;
    assign push_data  = m_sdata;

    // ---- local control ----------------------------------------------------
    localparam [5:0] C_STATUS = 6'd0, C_FLUSH = 6'd1, C_INVAL = 6'd2,
                     C_CAUSE  = 6'd3, C_COREID= 6'd4, C_ARG   = 6'd5,
                     C_CYCLE  = 6'd6, C_INSTR = 6'd7, C_WROUT = 6'd8;

    wire [5:0] c_idx = m_addr[7:2];

    // A blocking store is what makes flush-then-doorbell a two-instruction
    // idiom: it does not complete until every dirty line has been written back
    // AND acknowledged, so the doorbell behind it cannot overtake the data.
    // CT_DONE exists so the store that started the flush is not re-detected on
    // the cycle the flush ends.
    //
    // INVALIDATE-ALL BLOCKS TOO, and must: it is a sweep of one line per cycle
    // now, and a load let past it would hit a line the sweep has not reached.
    localparam [1:0] CT_IDLE = 2'd0, CT_WAIT = 2'd1, CT_RUN = 2'd2, CT_DONE = 2'd3;
    reg [1:0] cst;

    wire flush_store = is_ct && m_store && (c_idx == C_FLUSH);
    wire inval_store = is_ct && m_store && (c_idx == C_INVAL);

    wire ct_stall = (cst == CT_WAIT) || (cst == CT_RUN) ||
                    ((cst == CT_IDLE) && (flush_store || inval_store));

    always @(posedge clk) begin
        if (!resetn) begin
            cst      <= CT_IDLE;
            l1_flush <= 1'b0;
            l1_inval <= 1'b0;
        end else begin
            l1_flush <= 1'b0;
            l1_inval <= 1'b0;
            case (cst)
            CT_IDLE: if (flush_store) begin
                         l1_flush <= 1'b1; cst <= CT_WAIT;
                     end else if (inval_store) begin
                         l1_inval <= 1'b1; cst <= CT_WAIT;
                     end
            CT_WAIT: if (l1_flush_busy) cst <= CT_RUN;
            CT_RUN:  if (!l1_flush_busy) cst <= CT_DONE;
            default: cst <= CT_IDLE;
            endcase
        end
    end

    reg [31:0] ctl_rdata;
    always @(*) begin
        case (c_idx)
        C_STATUS: ctl_rdata = {30'd0, (ctl_wr_out != 16'd0), l1_flush_busy};
        C_CAUSE:  ctl_rdata = {30'd0, ctl_cause};
        C_COREID: ctl_rdata = {24'd0, ctl_coreid};
        C_ARG:    ctl_rdata = ctl_arg;
        C_CYCLE:  ctl_rdata = ctl_cycle;
        C_INSTR:  ctl_rdata = ctl_instret;
        C_WROUT:  ctl_rdata = {16'd0, ctl_wr_out};
        default:  ctl_rdata = 32'd0;
        endcase
    end

    assign vsp_st_valid = is_vs && m_store;
    assign vsp_st_addr  = m_addr;
    assign vsp_st_be    = m_be;
    assign vsp_st_data  = m_sdata;

    assign base_stall = (is_dr && l1_stall) ||
                        (is_pe && m_store && !push_ready) ||
                        ct_stall;
    assign stall_m    = base_stall || vec_stall;

    // ---- the writeback register -------------------------------------------
    // Only what the extract needs travels: the arrays hand their word straight
    // to the next stage, so nothing here is 256 bits wide.
    reg [2:0] w_region;
    reg [2:0] w_f3;
    reg [1:0] w_off;
    reg       w_load;
    reg [31:0] w_ctl;

    always @(posedge clk) begin
        if (!resetn) begin
            w_valid <= 1'b0;
        end else begin
            w_valid  <= m_valid && !stall_m;
            w_rd     <= m_rd;
            w_wen    <= m_wen;
            w_val    <= m_val;
            w_pc     <= m_pc;
            w_load   <= m_load;
            w_f3     <= m_f3;
            w_off    <= m_addr[1:0];
            w_region <= region;
            w_ctl    <= ctl_rdata;
        end
    end

    reg [31:0] rword;
    always @(*) begin
        case (w_region)
        R_SPAD:  rword = spad_rdata;
        R_DRAM:  rword = l1_rdata;
        default: rword = w_ctl;
        endcase
    end

    wire [15:0] half = w_off[1] ? rword[31:16] : rword[15:0];
    reg  [7:0]  byt;
    always @(*) begin
        case (w_off)
        2'd0:    byt = rword[7:0];
        2'd1:    byt = rword[15:8];
        2'd2:    byt = rword[23:16];
        default: byt = rword[31:24];
        endcase
    end

    reg [31:0] load_val;
    always @(*) begin
        case (w_f3)
        3'b000:  load_val = {{24{byt[7]}},   byt};
        3'b001:  load_val = {{16{half[15]}}, half};
        3'b100:  load_val = {24'd0, byt};
        3'b101:  load_val = {16'd0, half};
        default: load_val = rword;
        endcase
    end

    // A reduction's word arrives from the vector unit already registered at the
    // same edge as everything else here, so it joins at the same place a load's
    // extracted word does.
    assign wstage_val = vec_w_valid ? vec_w_val : (w_load ? load_val : w_val);

endmodule

`default_nettype wire
