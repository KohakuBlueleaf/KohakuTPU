// The mover commanded THE WAY SILICON COMMANDS IT: through the station bus's
// 32-bit control port, at the real window base.

// scripts/tcl/v6/40_bus.tcl:141 puts an axi_dwidth_converter SI=32 MI=64 in
// front of every mesh's S_AXI_CTRL, because the station emits only 32 or FW.
// So a host `write64` reaches noc_orchestrator as one of three shapes, and
// which one depends on whether the upsizer packed. Every bench before this
// drove shape A only.

//   A w64      one 64-bit beat, wstrb FF          -- the packed case
//   B split32  two single-beat writes, +0 then +4, wstrb 0F then F0
//   C burst2   one awlen=1 burst, the same two halves
//   D flit32   THE SHIP'S SHAPE. sb_nmu.v:355 sets hdr_size = FSZ and :381
//              aligns the address down to the flit, so a 64-bit write leaves
//              the manager as a 32-BYTE transfer. sb_nsu.v:305 re-expresses
//              that as 8 beats of 32 bits, and the upsizer makes 4 beats of
//              64 -- one strobed, three not.

// scripts/tcl/v6/50_addr_lit.tcl gives segment XLT = base, so the mesh sees
// 0x800000 + 0x800 + offset. Driven in full here: `is_aux` compares
// waddr[15:0], and the window is 64 KiB aligned, so the decode is part of
// what this proves.

`timescale 1ns / 1ps
`default_nettype none

module mover_cfg32_core #(
    // The SHIP builds MAG_CDC=1: MAG (and so S_AXI_CTRL, the orchestrator and
    // the mover) on clk_out4, the fabric on clk_out1. At 0 they are one clock
    // and `mag_clk_i = noc_clk` -- ktpu_min_1m.v:230.
    parameter integer MAGCDC  = 0,
    parameter integer UNITCDC = 0
)();
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4, MW = 512;
    localparam LW = 288, UW = 96;
    // Must COVER 0x200000 + 2 KB: axi_ram indexes mem[addr >> LSB] unmasked, so
    // a short RAM reads X and drops writes rather than aliasing.
    localparam integer RAMW = 33792;
    localparam integer NW   = 64;          // 32-byte words to move
    localparam integer SPIN_MAX = 40000;

    // boards/multimesh_v65.json: ctrl_base, and mover.py's AUX_CFG.
    localparam [31:0] CTRL_BASE = 32'h0080_0000;
    localparam [31:0] A_MV_CFG  = CTRL_BASE + 32'h0800;

    // `clk` is whatever S_AXI_CTRL runs on: axi_aclk at MAGCDC=1, noc_clk at 0.
    reg clk = 0, resetn = 0, dclk = 0, nclk = 0, tclk = 0, vclk = 0;
    always begin
        #2     clk  = ~clk;
    end
    always begin
        #1.7   dclk = ~dclk;
    end
    always begin
        #1.451 nclk = ~nclk;
    end
    always begin
        #1.889 tclk = ~tclk;
    end
    always begin
        #2.237 vclk = ~vclk;
    end

    wire noc_i = MAGCDC  ? nclk : clk;
    wire mat_i = UNITCDC ? tclk : noc_i;
    wire vec_i = UNITCDC ? vclk : noc_i;

    integer cyc = 0;
    always @(posedge clk) begin
        cyc <= cyc + 1;
    end

    reg  [31:0] sc_awaddr;
    reg  [7:0]  sc_awlen;
    reg         sc_awvalid, sc_wvalid, sc_wlast;
    reg  [63:0] sc_wdata;
    reg  [7:0]  sc_wstrb;
    wire        sc_awready, sc_wready, sc_bvalid;

    wire [IDW-1:0]  m_awid, m_arid, m_bid, m_rid;
    wire [AW-1:0]   m_awaddr, m_araddr;
    wire [7:0]      m_awlen, m_arlen;
    wire [2:0]      m_awsize, m_arsize;
    wire [1:0]      m_awburst, m_arburst, m_bresp, m_rresp;
    wire            m_awvalid, m_awready, m_arvalid, m_arready;
    wire [MW-1:0]   m_wdata, m_rdata;
    wire [MW/8-1:0] m_wstrb;
    wire            m_wlast, m_wvalid, m_wready;
    wire            m_bvalid, m_bready, m_rlast, m_rvalid, m_rready;

    ktpu_min_1m #(.MESH_ID(0), .MODEL(1), .MW(MW),
                  .MAG_CDC(MAGCDC), .UNIT_CDC(UNITCDC)) u (
        .axi_aclk(clk), .axi_aresetn(resetn),
        .noc_clk(noc_i), .mat_clk(mat_i), .vec_clk(vec_i),
        .dram_aclk(dclk), .dram_aresetn(resetn),
        .S_AXI_MEM_awid({IDW{1'b0}}), .S_AXI_MEM_awaddr({AW{1'b0}}),
        .S_AXI_MEM_awlen(8'd0), .S_AXI_MEM_awvalid(1'b0), .S_AXI_MEM_awready(),
        .S_AXI_MEM_wdata({DW{1'b0}}), .S_AXI_MEM_wstrb({(DW/8){1'b0}}),
        .S_AXI_MEM_wlast(1'b0), .S_AXI_MEM_wvalid(1'b0), .S_AXI_MEM_wready(),
        .S_AXI_MEM_bid(), .S_AXI_MEM_bresp(), .S_AXI_MEM_bvalid(),
        .S_AXI_MEM_bready(1'b1),
        .S_AXI_MEM_arid({IDW{1'b0}}), .S_AXI_MEM_araddr({AW{1'b0}}),
        .S_AXI_MEM_arlen(8'd0), .S_AXI_MEM_arvalid(1'b0), .S_AXI_MEM_arready(),
        .S_AXI_MEM_rid(), .S_AXI_MEM_rdata(), .S_AXI_MEM_rresp(),
        .S_AXI_MEM_rlast(), .S_AXI_MEM_rvalid(), .S_AXI_MEM_rready(1'b1),

        .S_AXI_CTRL_awid({IDW{1'b0}}), .S_AXI_CTRL_awaddr(sc_awaddr),
        .S_AXI_CTRL_awlen(sc_awlen), .S_AXI_CTRL_awvalid(sc_awvalid),
        .S_AXI_CTRL_awready(sc_awready),
        .S_AXI_CTRL_wdata(sc_wdata), .S_AXI_CTRL_wstrb(sc_wstrb),
        .S_AXI_CTRL_wlast(sc_wlast), .S_AXI_CTRL_wvalid(sc_wvalid),
        .S_AXI_CTRL_wready(sc_wready),
        .S_AXI_CTRL_bid(), .S_AXI_CTRL_bresp(), .S_AXI_CTRL_bvalid(sc_bvalid),
        .S_AXI_CTRL_bready(1'b1),
        .S_AXI_CTRL_arid({IDW{1'b0}}), .S_AXI_CTRL_araddr(32'd0),
        .S_AXI_CTRL_arlen(8'd0), .S_AXI_CTRL_arvalid(1'b0), .S_AXI_CTRL_arready(),
        .S_AXI_CTRL_rid(), .S_AXI_CTRL_rdata(), .S_AXI_CTRL_rresp(),
        .S_AXI_CTRL_rlast(), .S_AXI_CTRL_rvalid(), .S_AXI_CTRL_rready(1'b1),

        .M_AXI_DRAM_awid(m_awid), .M_AXI_DRAM_awaddr(m_awaddr),
        .M_AXI_DRAM_awlen(m_awlen), .M_AXI_DRAM_awsize(m_awsize),
        .M_AXI_DRAM_awburst(m_awburst),
        .M_AXI_DRAM_awvalid(m_awvalid), .M_AXI_DRAM_awready(m_awready),
        .M_AXI_DRAM_wdata(m_wdata), .M_AXI_DRAM_wstrb(m_wstrb),
        .M_AXI_DRAM_wlast(m_wlast), .M_AXI_DRAM_wvalid(m_wvalid),
        .M_AXI_DRAM_wready(m_wready),
        .M_AXI_DRAM_bid(m_bid), .M_AXI_DRAM_bresp(m_bresp),
        .M_AXI_DRAM_bvalid(m_bvalid), .M_AXI_DRAM_bready(m_bready),
        .M_AXI_DRAM_arid(m_arid), .M_AXI_DRAM_araddr(m_araddr),
        .M_AXI_DRAM_arlen(m_arlen), .M_AXI_DRAM_arsize(m_arsize),
        .M_AXI_DRAM_arburst(m_arburst),
        .M_AXI_DRAM_arvalid(m_arvalid), .M_AXI_DRAM_arready(m_arready),
        .M_AXI_DRAM_rid(m_rid), .M_AXI_DRAM_rdata(m_rdata),
        .M_AXI_DRAM_rresp(m_rresp), .M_AXI_DRAM_rlast(m_rlast),
        .M_AXI_DRAM_rvalid(m_rvalid), .M_AXI_DRAM_rready(m_rready),

        .M_AXIS_LINK0_tdata(), .M_AXIS_LINK0_tuser(), .M_AXIS_LINK0_tlast(),
        .M_AXIS_LINK0_tvalid(), .M_AXIS_LINK0_tready(1'b1),
        .S_AXIS_LINK0_tdata({LW{1'b0}}), .S_AXIS_LINK0_tuser({UW{1'b0}}),
        .S_AXIS_LINK0_tlast(1'b0), .S_AXIS_LINK0_tvalid(1'b0),
        .S_AXIS_LINK0_tready(),
        .M_AXIS_LINK1_tdata(), .M_AXIS_LINK1_tuser(), .M_AXIS_LINK1_tlast(),
        .M_AXIS_LINK1_tvalid(), .M_AXIS_LINK1_tready(1'b1),
        .S_AXIS_LINK1_tdata({LW{1'b0}}), .S_AXIS_LINK1_tuser({UW{1'b0}}),
        .S_AXIS_LINK1_tlast(1'b0), .S_AXIS_LINK1_tvalid(1'b0),
        .S_AXIS_LINK1_tready()
    );

    axi_ram #(.DATA_W(MW), .ADDR_W(AW), .ID_W(IDW), .WORDS(RAMW), .PORTS(1)) ram (
        .clk(dclk), .resetn(resetn),
        .s_awid(m_awid), .s_awaddr(m_awaddr), .s_awlen(m_awlen),
        .s_awsize(m_awsize), .s_awburst(m_awburst),
        .s_awvalid(m_awvalid), .s_awready(m_awready),
        .s_wdata(m_wdata), .s_wstrb(m_wstrb), .s_wlast(m_wlast),
        .s_wvalid(m_wvalid), .s_wready(m_wready),
        .s_bid(m_bid), .s_bresp(m_bresp), .s_bvalid(m_bvalid), .s_bready(m_bready),
        .s_arid(m_arid), .s_araddr(m_araddr), .s_arlen(m_arlen),
        .s_arsize(m_arsize), .s_arburst(m_arburst),
        .s_arvalid(m_arvalid), .s_arready(m_arready),
        .s_rid(m_rid), .s_rdata(m_rdata), .s_rresp(m_rresp), .s_rlast(m_rlast),
        .s_rvalid(m_rvalid), .s_rready(m_rready),
        .bd_we(1'b0), .bd_addr(16'd0), .bd_wdata({MW{1'b0}}), .bd_rdata()
    );

    reg  [31:0]  bd_a;
    wire [255:0] bd_rd = ram.mem[bd_a >> 1][(bd_a & 1) * 256 +: 256];

    // ---- what the mover's own cfg port actually received -------------------
    // The question the silicon symptom could not answer: how many pulses, at
    // which register, carrying what.
    wire        mv_cfg_en   = u.u_mag.u_pe.cfg_en_i;
    wire [7:0]  mv_cfg_addr = u.u_mag.u_pe.cfg_addr_i;
    wire [63:0] mv_cfg_data = u.u_mag.u_pe.cfg_data_i;
    wire        mv_busy     = u.u_mag.u_pe.u_mover.stat_busy;
    wire [3:0]  mv_fault    = u.u_mag.u_pe.u_mover.stat_fault;

    integer n_pulse = 0, trace = 0;
    always @(posedge clk) if (resetn && mv_cfg_en) begin
        n_pulse = n_pulse + 1;
        if (trace) begin
            $display("    cfg pulse %0d: addr %02h data %h",
                     n_pulse, mv_cfg_addr, mv_cfg_data);
        end
    end

    // ---- the NORMAL register path, which reads the same strided `waddr` ----
    // `wsel` is noc_orchestrator.v:366 off the register :448 increments, so the
    // aux window is not special. PROG_KICK is the one that acts on ARRIVAL
    // rather than on data: `A_PROG_KICK: kick_req <= 1'b1` regardless of wdata.
    wire [7:0]  ag_dst  = u.u_mag.u_mag.u_agent.prog_dst;
    wire [15:0] ag_len  = u.u_mag.u_mag.u_agent.prog_len;
    wire [15:0] ag_base = u.u_mag.u_mag.u_agent.prog_base;
    wire [15:0] ag_cred = u.u_mag.u_mag.u_agent.prog_credit;
    wire [15:0] ag_sig  = u.u_mag.u_mag.u_agent.sig_done_count;
    wire        ag_kick = u.u_mag.u_mag.u_agent.kick_req;
    wire        ag_run  = u.u_mag.u_mag.u_agent.prog_run;

    // WHERE the first flit was addressed, which is the difference between "the
    // dispatch ran" and "the dispatch ran to node {0,0} and was dropped".
    reg       arm = 1'b0;              // tasks drive this
    reg       seen;                    // the block below drives this
    reg [7:0] dst_at_disp;
    always @(posedge clk) if (resetn) begin
        if (!arm) begin
            seen <= 1'b0;
        end
        else if (u.u_mag.u_mag.u_agent.disp_push && !seen) begin
            dst_at_disp <= ag_dst;
            seen        <= 1'b1;
        end
    end

    integer n_kick = 0, n_disp = 0;
    always @(posedge clk) begin
        if (resetn && ag_kick) begin
            n_kick = n_kick + 1;
        end
    end
    always @(posedge clk) begin
        if (resetn && u.u_mag.u_mag.u_agent.disp_push) begin
            n_disp = n_disp + 1;
        end
    end

    // ---- the descriptor the mover ended up holding -------------------------
    wire [AW-1:0] src_base = u.u_mag.u_pe.u_mover.u_src.d_base;
    wire [2:0]    src_ndim = u.u_mag.u_pe.u_mover.u_src.d_ndim;
    wire [15:0]   src_cnt0 = u.u_mag.u_pe.u_mover.u_src.d_count[0];
    wire [AW-1:0] dst_base = u.u_mag.u_pe.u_mover.u_dst.d_base;
    wire [2:0]    dst_ndim = u.u_mag.u_pe.u_mover.u_dst.d_ndim;
    wire [15:0]   dst_cnt0 = u.u_mag.u_pe.u_mover.u_dst.d_count[0];

    integer errors = 0, checks = 0, i, spin;
    reg [255:0] rd;

    task chk(input cond, input [255:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 14) begin
                    $display("  FAIL %0s [%0d]", what, where);
                end
            end
        end
    endtask

    // ================================================== the three write shapes
    localparam integer SHAPE_W64 = 0, SHAPE_SPLIT32 = 1, SHAPE_BURST2 = 2;
    localparam integer SHAPE_FLIT32 = 3, SHAPE_FLIT0 = 4;
    integer shape;
    // ZERO, not stale: sb_nmu.v:578 clears the whole packer register at each
    // flit's first beat and merges in only strobed lanes, so a byte the host
    // did not write leaves as 00. That is what makes the neighbour a ZEROING.
    reg [63:0] stale = 64'd0;
    // Which window `cfgwr` aims at: the mover's aux slice, or the orchestrator's
    // own registers.
    reg [31:0] wbase = A_MV_CFG;

    task aw(input [31:0] a, input [7:0] len);
        begin
            @(negedge clk);
            sc_awaddr = a; sc_awlen = len; sc_awvalid = 1'b1;
            spin = 0;
            while (spin < 3000) begin
                @(posedge clk);
                if (sc_awready) begin
                    spin = 9000;
                end
                else begin
                    spin = spin + 1;
                end
            end
            @(negedge clk); sc_awvalid = 1'b0;
        end
    endtask

    task wbeat(input [63:0] d, input [7:0] strb, input last);
        begin
            @(negedge clk);
            sc_wdata = d; sc_wstrb = strb; sc_wlast = last; sc_wvalid = 1'b1;
            spin = 0;
            while (spin < 3000) begin
                @(posedge clk);
                if (sc_wready) begin
                    spin = 9000;
                end
                else begin
                    spin = spin + 1;
                end
            end
            @(negedge clk); sc_wvalid = 1'b0; sc_wlast = 1'b0;
        end
    endtask

    task bwait;
        begin
            spin = 0;
            while (!sc_bvalid && spin < 3000) begin spin = spin + 1; @(negedge clk); end
            @(negedge clk);
        end
    endtask

    // An upsizer pads the lane the beat did not carry by REPLICATION, so the
    // half that must be ignored is a plausible value rather than zero -- which
    // is the whole reason a strobe-blind register cannot notice.
    task cfgwr(input [7:0] off, input [63:0] d);
        reg [31:0] lo, hi;
        reg [7:0]  fbase;
        reg [2:0]  live;
        integer k;
        begin
            lo = d[31:0]; hi = d[63:32];
            fbase = off & 8'hE0;              // the 32-byte flit `off` falls in
            live  = off[4:3];                 // which 64-bit beat carries it
            case (shape)
                SHAPE_W64: begin
                    aw(wbase + {24'd0, off}, 8'd0);
                    wbeat(d, 8'hFF, 1'b1);
                    bwait;
                end
                SHAPE_SPLIT32: begin
                    aw(wbase + {24'd0, off}, 8'd0);
                    wbeat({lo, lo}, 8'h0F, 1'b1);
                    bwait;
                    aw(wbase + {24'd0, off} + 32'd4, 8'd0);
                    wbeat({hi, hi}, 8'hF0, 1'b1);
                    bwait;
                end
                SHAPE_BURST2: begin
                    aw(wbase + {24'd0, off}, 8'd1);
                    wbeat({lo, lo}, 8'h0F, 1'b0);
                    wbeat({hi, hi}, 8'hF0, 1'b1);
                    bwait;
                end
                SHAPE_FLIT32: begin
                    aw(wbase + {24'd0, fbase}, 8'd3);
                    for (k = 0; k < 4; k = k + 1) begin
                        wbeat((k == {29'd0, live}) ? d : stale,
                              (k == {29'd0, live}) ? 8'hFF : 8'h00, k == 3);
                    end
                    bwait;
                end
                // The SAME four beats with the padding FULL-STROBED, which is what
                // the pre-fix orchestrator did with them: it wrote every beat
                // wholesale. Reproduces the old behaviour on the fixed RTL.
                default: begin
                    aw(wbase + {24'd0, fbase}, 8'd3);
                    for (k = 0; k < 4; k = k + 1) begin
                        wbeat((k == {29'd0, live}) ? d : stale, 8'hFF, k == 3);
                    end
                    bwait;
                end
            endcase
        end
    endtask

    // ============================================ the driver's seven words
    // driver/kohakuaccel/device/mover.py, for
    //   copy(Walker(0x100000,[(64,32)]), Walker(0x200000,[(64,32)]))
    // with the defaults ewidth=W16, flags=FLAG_WCOAL. Written as literals so
    // this bench states what software emits rather than re-deriving it.
    localparam [AW-1:0] SRC_OFF = 40'h10_0000;
    localparam [AW-1:0] DST_OFF = 40'h20_0000;

    task issue_program;
        begin
            cfgwr(8'h10, 64'h0000_1000_0100_0000);   // hdr src, 0x100000, ndim 1
            cfgwr(8'h18, 64'h0000_0000_0200_0400);   // dim src, d0, 64 x 32
            cfgwr(8'h20, 64'h0000_0000_0000_0000);   // latch
            cfgwr(8'h10, 64'h0000_1000_0200_0001);   // hdr dst, 0x200000, ndim 1
            cfgwr(8'h18, 64'h0000_0000_0200_0401);   // dim dst, d0, 64 x 32
            cfgwr(8'h20, 64'h0000_0000_0000_0000);   // latch
            cfgwr(8'h00, 64'h0000_0000_0001_0808);   // COPY W16 WCOAL, go
        end
    endtask

    // A plain 64-bit write to ONE ordinary orchestrator register, in whichever
    // shape. PROG_DST (0x40) shares its 32-byte flit with PROG_LEN (0x48),
    // PROG_KICK (0x50) and PROG_STAT (0x58).
    task reg_probe(input integer sh, input [255:0] tag);
        begin
            shape = sh;
            n_kick = 0;
            wbase  = CTRL_BASE;
            stale  = 64'hBAD0_BAD0_BAD0_BAD0;
            // A_PROG_LEN first, so a spurious kick has a non-zero length to run
            // with -- `kick_req && !prog_run && prog_len != 0` ignores len 0.
            cfgwr(8'h48, 64'd4);
            cfgwr(8'h40, 64'h0000_0000_0000_0021);   // PROG_DST = {y,x} = 2,1
            repeat (40) @(negedge clk);
            $display("    %0s regs: dst %02h len %0d base %0d cred %0d sig %0d | %0d kick_req",
                     tag, ag_dst, ag_len, ag_base, ag_cred, ag_sig, n_kick);
            chk(n_kick == 0, "no spurious PROG_KICK", n_kick);
            chk(ag_dst === 8'h21, "PROG_DST took its value", {248'd0, ag_dst});
            chk(ag_len === 16'd4, "PROG_LEN survived the neighbour", {240'd0, ag_len});
            wbase = A_MV_CFG;
        end
    endtask

    // THE DOCUMENTED KICK ORDER, replayed. BASE, LEN, DST, CRED, KICK is
    // recorded as load-bearing with "any other order launches nothing".
    task step(input [255:0] what);
        begin
            repeat (30) @(negedge clk);
            $display("      %0s: run %0d base %0d len %0d dst %02h cred %0d sig %0d | %0d kick %0d flit",
                     what, ag_run, ag_base, ag_len, ag_dst, ag_cred, ag_sig,
                     n_kick, n_disp);
        end
    endtask

    task kick_lore(input integer sh, input want_lore, input [255:0] tag);
        begin
            shape = sh; wbase = CTRL_BASE; stale = 64'd0;
            n_kick = 0; n_disp = 0;
            $display("    %0s: BASE, LEN, DST, CRED, KICK", tag);
            cfgwr(8'h60, 64'd9);  // a credit to watch BASE destroy
            cfgwr(8'h68, 64'd0);  step("after BASE");
            chk((ag_cred == 16'd0) == want_lore, "BASE zeroed the credit", ag_cred);
            cfgwr(8'h48, 64'd4);  step("after LEN ");
            // The claim: LEN is the REAL kick, and a zero credit holds it.
            chk(ag_run == want_lore, "LEN fired the dispatch", ag_run);
            chk(n_disp == 0, "no flit moved on a zero credit", n_disp);
            cfgwr(8'h40, 64'h21); step("after DST ");
            chk(ag_dst === 8'h21, "DST repaired what LEN zeroed", {248'd0, ag_dst});
            cfgwr(8'h60, 64'd16); step("after CRED");
            chk((n_disp != 0) == want_lore, "credit released the dispatch", n_disp);
            cfgwr(8'h50, 64'd1);  step("after KICK");
            wbase = A_MV_CFG;
        end
    endtask

    // IS THE ORDER STILL REQUIRED? The documented one against the one a driver
    // would write knowing nothing about the bug, in both shapes.
    task kick_drain;
        begin
            shape = SHAPE_W64; wbase = CTRL_BASE;
            cfgwr(8'h60, 64'd64);
            repeat (150) @(negedge clk);
        end
    endtask

    task kick_seq(input integer sh, input doc, input [255:0] tag);
        begin
            kick_drain;
            arm = 1'b0; @(negedge clk); arm = 1'b1;
            shape = sh; wbase = CTRL_BASE; stale = 64'd0;
            n_kick = 0; n_disp = 0; dst_at_disp = 8'hFF;
            if (doc) begin
                cfgwr(8'h68, 64'd0);  cfgwr(8'h48, 64'd4); cfgwr(8'h40, 64'h21);
                cfgwr(8'h60, 64'd16); cfgwr(8'h50, 64'd1);
            end else begin
                cfgwr(8'h40, 64'h21); cfgwr(8'h48, 64'd4); cfgwr(8'h68, 64'd0);
                cfgwr(8'h60, 64'd16); cfgwr(8'h50, 64'd1);
            end
            repeat (200) @(negedge clk);
            $display("    %0s %0s: %0d flits, first went to %02h (want 21)",
                     tag, doc ? "documented" : "natural   ", n_disp, dst_at_disp);
            wbase = A_MV_CFG;
        end
    endtask

    // ---- one whole checked move, in whichever shape ------------------------
    localparam [255:0] POISON = {8{32'hA5A5_A5A5}};

    // `want_ok` is the CURRENT behaviour, asserted in both directions: A must be
    // exact, B and C must still corrupt. If an orchestrator fix makes B or C
    // exact this fails and says so, rather than passing quietly on a stale claim.
    integer pj, n_bad, exact;
    task do_move(input integer sh, input want_ok, input integer exp_pulse,
                 input [255:0] tag);
        begin
            shape = sh;
            n_pulse = 0;
            for (pj = 0; pj < NW; pj = pj + 1) begin
                ram.mem[((DST_OFF >> 5) + pj) >> 1]
                       [(((DST_OFF >> 5) + pj) & 1) * 256 +: 256] = POISON;
            end

            issue_program;

            spin = 0;
            while (!mv_busy && spin < 4000) begin spin = spin + 1; @(negedge clk); end
            chk(spin < 4000, "the mover started", 0);
            spin = 0;
            while (mv_busy && spin < SPIN_MAX) begin spin = spin + 1; @(negedge clk); end
            chk(spin < SPIN_MAX, "the mover went idle", 0);
            // NO FAULT IN EVERY SHAPE, which is the trap: a corrupted descriptor
            // completes and reports success exactly as a good one does.
            chk(mv_fault === 4'd0, "no fault", {28'd0, mv_fault});

            $display("    %0s: %0d cfg pulses | src base %h ndim %0d count %0d | dst base %h ndim %0d count %0d",
                     tag, n_pulse, src_base, src_ndim, src_cnt0,
                     dst_base, dst_ndim, dst_cnt0);
            exact = (src_base === SRC_OFF) && (dst_base === DST_OFF)
                 && (src_ndim === 3'd1)   && (dst_ndim === 3'd1)
                 && (src_cnt0 === NW[15:0]) && (dst_cnt0 === NW[15:0]);
            chk(n_pulse == exp_pulse, "cfg pulses", n_pulse);
            if (want_ok) begin
                chk(src_base === SRC_OFF, "src base survived", {216'd0, src_base});
                chk(dst_base === DST_OFF, "dst base survived", {216'd0, dst_base});
                chk(src_ndim === 3'd1, "source ndim survived", {61'd0, src_ndim});
                chk(dst_ndim === 3'd1, "destination ndim survived", {61'd0, dst_ndim});
                chk(src_cnt0 === NW[15:0], "source count survived", {240'd0, src_cnt0});
                chk(dst_cnt0 === NW[15:0], "destination count survived", {240'd0, dst_cnt0});
            end

            spin = 0;
            bd_a = (DST_OFF >> 5) + NW - 1; #1;
            while ((bd_rd !== {8{32'hAC00_0000 | (NW - 1)}}) && spin < 6000) begin
                spin = spin + 1; @(negedge clk);
                bd_a = (DST_OFF >> 5) + NW - 1; #1;
            end

            // Every word CHECKED. A fast copy of wrong bytes must not pass, and
            // a copy that never happened leaves POISON rather than a mismatch.
            n_bad = 0;
            for (i = 0; i < NW; i = i + 1) begin
                bd_a = (DST_OFF >> 5) + i; #1;
                rd = bd_rd;
                if (want_ok) begin
                    chk(rd === {8{32'hAC00_0000 | i[31:0]}},
                        "destination word", i);
                end
                if (rd !== {8{32'hAC00_0000 | i[31:0]}}) begin
                    n_bad = n_bad + 1;
                end
            end
            // The source is checked in EVERY shape: a descriptor mangled by the
            // write path must not scribble over what it was told to read.
            for (i = 0; i < NW; i = i + 1) begin
                bd_a = (SRC_OFF >> 5) + i; #1;
                chk(bd_rd === {8{32'hAC00_0000 | i[31:0]}}, "source unmodified", i);
            end
            $display("    %0s: %0d of %0d destination words wrong", tag, n_bad, NW);
            if (want_ok) begin
                chk(exact && (n_bad == 0), "the move was exact", n_bad);
            end
            else begin
                chk(!exact || (n_bad != 0),
                    "split writes still corrupt -- update this bench", n_bad);
            end
        end
    endtask

    initial begin
        sc_awvalid = 0; sc_wvalid = 0; sc_wlast = 0; sc_awaddr = 0;
        sc_awlen = 0; sc_wdata = 0; sc_wstrb = 8'hFF; bd_a = 0; shape = 0;
        for (i = 0; i < NW; i = i + 1) begin
            ram.mem[((SRC_OFF >> 5) + i) >> 1]
                   [(((SRC_OFF >> 5) + i) & 1) * 256 +: 256]
                = {8{32'hAC00_0000 | i[31:0]}};
        end

        repeat (20) @(negedge clk);
        resetn = 1'b1;
        repeat (40) @(negedge clk);

        $display("--- mover config through S_AXI_CTRL at %h, %0d words, MAG_CDC=%0d UNIT_CDC=%0d ---",
                 A_MV_CFG, NW, MAGCDC, UNITCDC);
        trace = 1;
        do_move(SHAPE_W64, 1'b1, 7, "A w64    ");
        trace = 0;
        // Both halves are live, so B still pulses twice per word -- but the
        // untouched bytes now persist, so the register is whole by the second.
        do_move(SHAPE_SPLIT32, 1'b1, 14, "B split32");
        // C addresses TWO registers by construction (a 2-beat 64-bit burst is
        // +0 and +8), so no strobe rule can make it mean one. Stays corrupt.
        do_move(SHAPE_BURST2, 1'b0, 14, "C burst2 ");
        trace = 1;
        do_move(SHAPE_FLIT32, 1'b1, 7, "D flit32 ");
        trace = 0;

        // The blast radius outside the mover: `wsel` reads the same strided
        // address, so a plain register write is four register writes too.
        reg_probe(SHAPE_W64, "A w64    ");
        reg_probe(SHAPE_FLIT32, "D flit32 ");

        // E reproduces the pre-fix path, F is the same writes with the fix
        // doing its job: only PROG_KICK may kick.
        kick_lore(SHAPE_FLIT0, 1'b1, "E pre-fix");
        kick_lore(SHAPE_FLIT32, 1'b0, "F fixed  ");

        // Is the ORDER load-bearing, and is it still?
        kick_seq(SHAPE_FLIT0,  1'b1, "G pre-fix");
        kick_seq(SHAPE_FLIT0,  1'b0, "H pre-fix");
        kick_seq(SHAPE_FLIT32, 1'b1, "I fixed  ");
        kick_seq(SHAPE_FLIT32, 1'b0, "J fixed  ");
        chk(dst_at_disp === 8'h21, "fixed: any order reaches the node",
            {248'd0, dst_at_disp});

        if (errors == 0) begin
            $display("  PASS mover_cfg32: %0d checks", checks);
        end
        else begin
            $display("  FAIL mover_cfg32: %0d errors, %0d checks", errors, checks);
        end
        $finish;
    end

    initial begin
        #4000000;
        $display("  FAIL mover_cfg32: watchdog");
        $finish;
    end
endmodule

// One clock, the shape every earlier mover bench ran in.
module mover_cfg32_tb;
    mover_cfg32_core #(.MAGCDC(0), .UNITCDC(0)) c ();
endmodule

// The SHIP's clocking: MAG on its own clock, the fabric and the units on theirs.
module mover_cfg32_cdc_tb;
    mover_cfg32_core #(.MAGCDC(1), .UNITCDC(1)) c ();
endmodule

`default_nettype wire
