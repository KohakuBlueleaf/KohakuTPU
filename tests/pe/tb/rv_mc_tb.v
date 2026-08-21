// rv_mc_body -- level 4: several RV32 PEs on ONE NoC and ONE MAG.
//
// Level 3 proved one PE against the real memory path. The question here is the
// one that only appears at two or more: does a PE still get its memory when
// another is using it, and do the push-and-doorbell rules the memory map
// promises actually hold between two running cores rather than between a core
// and a bench pretending to be one?
//
// Four cases, all four run at every core count:
//
//   iso   unrelated programs on disjoint 8 KB slices. Halt word, RETIRED COUNT
//         and the whole final DRAM are checked against the golden model, so a
//         core that lost traffic to a neighbour fails on its answer.
//   pp    A pushes, B replies, 16 rounds. The initiator reads CTL_CYCLE either
//         side of a round, so ROUND-TRIP LATENCY is measured in cycles by the
//         software that pays it. At four PEs both pairs run at once.
//   agg   every worker sums its slice and pushes VALUE THEN FLAG to core 0.
//         The leader polls flags only: one sender's pushes arrive in program
//         order, so a flag it can see means the value beside it has landed.
//   ho    the DRAM hand-off of docs/arch/pe/programming.md in its four steps.
//         The reader CACHES the region first, so without the invalidate its
//         halt word is the stale sum -- and the generator refuses values whose
//         sums are equal, so that failure cannot pass.
//
// Programs and expected answers come from tests/pe/tools/rv_mc_gen.py; run it
// first or the bench reports no cases. Every poll loop in every program has a
// spin cap and halts with 0xDEAD00nn, so a deadlock is a named halt word in
// seconds rather than this bench's watchdog minutes later.
//
//     python tests/pe/tools/rv_run.py --gate 4

`default_nettype none
`timescale 1ns/1ps

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif

// THE VARIANT KNOBS. One frontier point has to drive the simulation and the
// synthesis from the same numbers: a knob that reaches only one of the two
// reports cycle counts for a design nobody built.
`ifndef RV_L1_LINES
 `define RV_L1_LINES 128
`endif
`ifndef RV_BTB
 `define RV_BTB 32
`endif
`ifndef RV_FWD_X
 `define RV_FWD_X 1
`endif

module rv_mc_body #(
    parameter integer NPE = 2
);
    localparam integer FW  = 288;
    localparam integer PW  = 4;
    localparam integer DW  = 256;
    localparam integer AW  = 40;
    localparam integer IW  = 2048;
    localparam integer SW  = 2048;
    // 32 KB, one 8 KB slice per core. RAM_DEPTH counts 256-bit entries.
    localparam integer DWORDS    = 8192;
    localparam integer RAM_DEPTH = 1024;
    localparam integer META_N    = 24;
    // Every program's own spin cap is ~100k cycles, so a program that gives up
    // still reports before this does.
    localparam integer LIMIT     = 400000;
    localparam integer SPADZ     = 32;      // scratchpad words zeroed before a kick

    localparam [7:0] BUF_SPAD = 8'd0, BUF_IMEM = 8'd1;

    reg clk = 1'b0, rstn = 1'b0;
    always #2 clk = ~clk;

    integer errors = 0, checks = 0;
    task chk(input [63:0] got, input [63:0] want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                $display("  FAIL %0s: got %h want %h", what, got, want);
                errors = errors + 1;
            end
        end
    endtask

    wire [FW-1:0] ag_out, ag_in;
    wire          ag_out_valid, ag_out_busy, ag_in_valid, ag_in_busy;
    wire [3:0]    pe_run, pe_halted, pe_busy;

`ifdef RV_RF_BRAM
    localparam RF_PRIM = "block";
`else
    localparam RF_PRIM = "distributed";
`endif

    rv_mesh #(.FW(FW), .PW(PW), .DW(DW), .AW(AW), .NPE(NPE),
              .IMEM_WORDS(IW), .SPAD_WORDS(SW),
              .L1_LINES(`RV_L1_LINES), .BTB_ENTRIES(`RV_BTB),
              .FWD_X(`RV_FWD_X), .REGFILE_PRIM(RF_PRIM),
              .RAM_DEPTH(RAM_DEPTH)) dut (
        .clk(clk), .rstn(rstn),
        .ext_in_data(ag_out), .ext_in_valid(ag_out_valid),
        .ext_in_busy(ag_out_busy),
        .ext_out_data(ag_in), .ext_out_valid(ag_in_valid),
        .ext_out_busy(ag_in_busy),
        .pe_run(pe_run), .pe_halted(pe_halted), .pe_busy(pe_busy)
    );

    rv_agent #(.FW(FW), .PW(PW), .AX(1), .AY(0), .IMG_WORDS(IW)) u_ag (
        .clk(clk), .resetn(rstn),
        .out_data(ag_out), .out_valid(ag_out_valid), .out_busy(ag_out_busy),
        .in_data(ag_in), .in_valid(ag_in_valid), .in_busy(ag_in_busy)
    );

    // PE i sits on router i's local port and carries that router's coordinate.
    function integer px; input integer i; px = ((i == 0) || (i == 2)) ? 1 : 2;
    endfunction
    function integer py; input integer i; py = (i < 2) ? 1 : 2; endfunction
    // rv_agent files a completion at {y[1:0], x[1:0]}, which for coordinates
    // below four is this.
    function integer sidx; input integer i; sidx = py(i) * 4 + px(i); endfunction

    // cyc_q is cleared by the kick and frozen by the halt: kick-to-halt for
    // that core alone. ret_q is what the model's retired count is checked on.
    wire [31:0] pcyc [0:3];
    wire [31:0] pret [0:3];
    genvar g;
    generate
    for (g = 0; g < 4; g = g + 1) begin : g_probe
        if (g < NPE) begin : g_on
            assign pcyc[g] = dut.g_pe[g].g_have.u_pe.u_core.cyc_q;
            assign pret[g] = dut.g_pe[g].g_have.u_pe.u_core.ret_q;
        end else begin : g_off
            assign pcyc[g] = 32'd0;
            assign pret[g] = 32'd0;
        end
    end
    endgenerate

`ifdef RV_MC_TRACE_MEM
    // Every core's line traffic, so a line that arrives at the wrong core or the
    // wrong address can be attributed. One outstanding fill per core, so a REQ
    // and the next RESP on the same core are the same transaction.
    generate
    for (g = 0; g < 4; g = g + 1) begin : g_trmem
        if (g < NPE) begin : g_on
            wire [30:0] t_fa = dut.g_pe[g].g_have.u_pe.u_l1.fill_addr;
            wire [30:0] t_wa = dut.g_pe[g].g_have.u_pe.u_l1.wb_addr;
            always @(posedge clk) if (rstn) begin
                if (dut.g_pe[g].g_have.u_pe.u_l1.fill_valid &&
                    dut.g_pe[g].g_have.u_pe.u_l1.fill_ready)
                    $display("  TRMEM %0t pe%0d FILLREQ line %0d", $time, g,
                             t_fa[30:5]);
                if (dut.g_pe[g].g_have.u_pe.u_req.send_valid &&
                    dut.g_pe[g].g_have.u_pe.u_req.send_ready)
                    $display("  TRMEM %0t pe%0d SEND ty %h addr %h", $time, g,
                             dut.g_pe[g].g_have.u_pe.u_req.send_flit[271:268],
                             dut.g_pe[g].g_have.u_pe.u_req.send_flit[255:216]);
                if (dut.g_pe[g].g_have.u_pe.u_l1.resp_valid)
                    $display("  TRMEM %0t pe%0d FILLRSP w0 %08x w1 %08x", $time,
                             g, dut.g_pe[g].g_have.u_pe.u_l1.resp_data[31:0],
                             dut.g_pe[g].g_have.u_pe.u_l1.resp_data[63:32]);
                if (dut.g_pe[g].g_have.u_pe.u_l1.wb_valid &&
                    dut.g_pe[g].g_have.u_pe.u_l1.wb_ready)
                    $display("  TRMEM %0t pe%0d WB line %0d w0 %08x w1 %08x",
                             $time, g, t_wa[30:5],
                             dut.g_pe[g].g_have.u_pe.u_l1.wb_data[31:0],
                             dut.g_pe[g].g_have.u_pe.u_l1.wb_data[63:32]);
            end
        end
    end
    endgenerate

    // The write slot's address and its data arrive as separate flits and are
    // paired by slot number: if the pairing slips, one core's line is written
    // to another core's address, which reads back as a stale line at one end
    // and a corrupted line at the other.
    always @(posedge clk) if (rstn) begin
        if (dut.u_mag.g_port[0].u_eng.take_wr_req)
            $display("  TRMAG %0t open  slot %0d addr %h", $time,
                     dut.u_mag.g_port[0].u_eng.ws_free,
                     dut.u_mag.g_port[0].u_eng.wi_addr);
        if (dut.u_mag.g_port[0].u_eng.take_wr_data)
            $display("  TRMAG %0t data  slot %0d w0 %08x w1 %08x", $time,
                     dut.u_mag.g_port[0].u_eng.ws_match,
                     dut.u_mag.g_port[0].u_eng.wq_flit[31:0],
                     dut.u_mag.g_port[0].u_eng.wq_flit[63:32]);
        if (dut.u_mag.g_port[0].u_eng.ws_issue)
            $display("  TRMAG %0t issue slot %0d addr %h", $time,
                     dut.u_mag.g_port[0].u_eng.ws_pick,
                     dut.u_mag.g_port[0].u_eng.ws_addr[
                         dut.u_mag.g_port[0].u_eng.ws_pick]);
        if (dut.u_mag.g_port[0].u_eng.m_arvalid &&
            dut.u_mag.g_port[0].u_eng.m_arready)
            $display("  TRAXI %0t AR addr %h len %0d", $time,
                     dut.u_mag.g_port[0].u_eng.m_araddr,
                     dut.u_mag.g_port[0].u_eng.m_arlen);
        if (dut.u_mag.g_port[0].u_eng.m_awvalid &&
            dut.u_mag.g_port[0].u_eng.m_awready)
            $display("  TRAXI %0t AW addr %h", $time,
                     dut.u_mag.g_port[0].u_eng.m_awaddr);
        if (dut.u_mag.u_dram.rd_take)
            $display("  TRARB %0t RDtake sel %0d cap %h  (qv %b qw %b)", $time,
                     dut.u_mag.u_dram.rd_sel, dut.u_mag.u_dram.sel_rad,
                     dut.u_mag.u_dram.q_valid, dut.u_mag.u_dram.q_write);
        if (dut.u_mag.u_dram.wr_take)
            $display("  TRARB %0t WRtake sel %0d cap %h  (qv %b qw %b)", $time,
                     dut.u_mag.u_dram.wr_sel, dut.u_mag.u_dram.sel_wad,
                     dut.u_mag.u_dram.q_valid, dut.u_mag.u_dram.q_write);
        if (dut.u_ram.s_axi_arvalid && dut.u_ram.s_axi_arready)
            $display("  TRRAM %0t ARcap addr %h", $time, dut.u_ram.s_axi_araddr);
        if (dut.u_ram.s_axi_awvalid && dut.u_ram.s_axi_awready)
            $display("  TRRAM %0t AWcap addr %h", $time, dut.u_ram.s_axi_awaddr);
        if (dut.u_ram.s_axi_wvalid && dut.u_ram.s_axi_wready)
            $display("  TRRAM %0t WRITE idx %0d w0 %08x strb %h", $time,
                     dut.u_ram.widx, dut.u_ram.s_axi_wdata[31:0],
                     dut.u_ram.s_axi_wstrb);
        if ((dut.u_ram.rstate == 1'b1) &&
            (!dut.u_ram.rvalid_r || dut.u_ram.s_axi_rready) &&
            (dut.u_ram.rbeats != 9'd0))
            $display("  TRRAM %0t READ  idx %0d w0 %08x", $time,
                     dut.u_ram.ridx, dut.u_ram.mem[dut.u_ram.ridx][31:0]);
        if (dut.u_mag.g_port[0].u_eng.r_valid &&
            dut.u_mag.g_port[0].u_eng.r_ready)
            $display("  TRAXI %0t R  w0 %08x last %b -> pe at %0d,%0d txn %h",
                     $time, dut.u_mag.g_port[0].u_eng.r_data[31:0],
                     dut.u_mag.g_port[0].u_eng.r_last,
                     dut.u_mag.g_port[0].u_eng.rq_x,
                     dut.u_mag.g_port[0].u_eng.rq_y,
                     dut.u_mag.g_port[0].u_eng.rq_txn);
    end
`endif

    // ---- golden data --------------------------------------------------------
    reg [31:0] dinit [0:DWORDS-1];
    reg [31:0] dfin  [0:DWORDS-1];
    reg [31:0] meta  [0:META_N-1];

    integer i, k, c, ok, sig_before, lat;
    reg [31:0] dsum;
    string fn;

    // The RAM is 256 bits wide and the model thinks in 32-bit words; one place
    // converts, so the two can never disagree about which word is which.
    task dram_write_init;
        begin
            for (i = 0; i < RAM_DEPTH; i = i + 1)
                for (k = 0; k < 8; k = k + 1)
                    dut.u_ram.mem[i][k*32 +: 32] = dinit[i * 8 + k];
        end
    endtask

    task dram_checksum(output reg [31:0] s);
        begin
            s = 32'd0;
            for (i = 0; i < RAM_DEPTH; i = i + 1)
                for (k = 0; k < 8; k = k + 1)
                    s = s + dut.u_ram.mem[i][k*32 +: 32] * (i * 8 + k + 1);
        end
    endtask

    function [31:0] dram_word;
        input integer wi;
        dram_word = dut.u_ram.mem[wi / 8][(wi % 8) * 32 +: 32];
    endfunction

    task reset_mesh;
        begin
            rstn = 1'b0;
            repeat (12) @(posedge clk);
            rstn = 1'b1;
            repeat (8) @(posedge clk);
        end
    endtask

    // ---- one case -----------------------------------------------------------
    // mode 1 also reports the ping-pong latency; every other case is checked
    // entirely by halt words, retired counts and DRAM.
    task run_case(input string dir, input string title, input integer mode);
        begin
            $display("--- %0s: %0d PEs, one NoC, one MAG ---", title, NPE);
            fn = $sformatf("%s/mc/%s/meta.hex", `PE_DIR, dir);
            $readmemh(fn, meta);
            fn = $sformatf("%s/mc/%s/dfin.hex", `PE_DIR, dir);
            $readmemh(fn, dfin);
            $readmemh({`PE_DIR, "/mc/dram.hex"}, dinit);

            reset_mesh;
            dram_write_init;

            for (c = 0; c < NPE; c = c + 1) begin
                // Zeroed BEFORE any kick: a program clearing its own mailbox
                // could erase a peer's push, which no doorbell survives.
                for (i = 0; i < IW; i = i + 1) u_ag.img[i] = 32'd0;
                u_ag.load_image(px(c), py(c), BUF_SPAD, SPADZ, 0);
                fn = $sformatf("%s/mc/%s/core%0d.hex", `PE_DIR, dir, c);
                $readmemh(fn, u_ag.img);
                u_ag.load_image(px(c), py(c), BUF_IMEM, meta[16 + c], 0);
                u_ag.sig_n_at[sidx(c)] = 0;
            end

            sig_before = u_ag.sig_n;
            for (c = 0; c < NPE; c = c + 1)
                u_ag.kick(px(c), py(c), 8'h50 + c[7:0], 1'b0, 8'd1, 32'd0, 32'd0);
            u_ag.wait_signal(sig_before + NPE, LIMIT, ok);
            chk(ok, 1, "every core retired");

            for (c = 0; c < NPE; c = c + 1) begin
                $display("    core%0d (%0d,%0d)  halt %08x  code %02h  cycles %8d  retired %7d",
                         c, px(c), py(c), u_ag.sig_arg_at[sidx(c)],
                         u_ag.sig_code_at[sidx(c)], pcyc[c], pret[c]);
                if (u_ag.sig_arg_at[sidx(c)][31:16] == 16'hDEAD)
                    $display("      ^ a poll loop hit its spin cap: the peer never answered");
                if (ok) begin
                    chk(u_ag.sig_n_at[sidx(c)], 1, "one completion from this core");
                    chk(u_ag.sig_code_at[sidx(c)],
                        (meta[8 + c] == 32'd1) ? 8'h00 : 8'h04, "completion code");
                    if (meta[3][c])
                        chk(u_ag.sig_arg_at[sidx(c)], meta[4 + c], "halt word");
                    if (meta[12 + c] != 32'd0)
                        chk(pret[c], meta[12 + c], "instructions retired");
                end
            end

            if (ok && (mode == 1)) begin
                for (c = 0; c < NPE; c = c + 2) begin
                    lat = u_ag.sig_arg_at[sidx(c)] / meta[20];
                    $display("    core%0d -> core%0d round trip  %0d cycles, mean of %0d rounds",
                             c, c + 1, lat, meta[20]);
                    // Two pushes and two polls across two routers: tens of
                    // cycles is a measurement, thousands is a stall averaged.
                    chk((lat > 4) && (lat < 2000), 1'b1,
                        "the measured round trip is a round trip");
                end
            end

            if (ok && meta[1][0]) begin
                dram_checksum(dsum);
                chk(dsum, meta[2], "DRAM after every core retired");
                if (dsum !== meta[2]) begin
                    k = 0;
                    for (i = 0; i < DWORDS; i = i + 1)
                        if ((dram_word(i) !== dfin[i]) && (k < 8)) begin
                            $display("      dram word %0d (byte %0d): rtl %08x model %08x",
                                     i, i * 4, dram_word(i), dfin[i]);
                            k = k + 1;
                        end
                end
            end
        end
    endtask

    initial begin
        meta[0] = 32'hxxxx_xxxx;
        fn = $sformatf("%s/mc/n%0d/iso/meta.hex", `PE_DIR, NPE);
        $readmemh(fn, meta);
        if ((^meta[0] === 1'bx) || (meta[0] !== NPE)) begin
            $display("  FAIL no %0d-core cases at %s/mc -- run python tests/pe/tools/rv_mc_gen.py",
                     NPE, `PE_DIR);
            $display("========================================");
            $display("  FAIL -- 0 checks, 1 errors");
            $display("========================================");
            $finish;
        end

        // At one core `iso` is the uncontended floor and the other three have
        // no peer to talk to, so only it is generated and only it runs.
        run_case($sformatf("n%0d/iso", NPE), "independent programs", 0);
        if (NPE > 1) begin
            run_case($sformatf("n%0d/pp",  NPE), "push and doorbell ping-pong", 1);
            run_case($sformatf("n%0d/agg", NPE), "all-to-one aggregation", 0);
            run_case($sformatf("n%0d/ho",  NPE), "DRAM hand-off", 0);
        end

        $display("========================================");
        if (checks == 0)      $display("  FAIL -- the bench made no checks");
        else if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else                  $display("  FAIL -- %0d checks, %0d errors",
                                       checks, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #20000000;
        $display("  FAIL WATCHDOG -- the multi-core bench never finished");
        $display("========================================");
        $finish;
    end

`ifdef RV_MC_STUCK
    // WHERE IS EVERYONE. A core that stops retiring is stalled, and every stall
    // in this PE is one of four things; print all four rather than guess which.
    // -d RV_MC_STUCK, and set RV_MC_STUCK_AT to a time past a healthy case.
 `ifndef RV_MC_STUCK_AT
  `define RV_MC_STUCK_AT 300000
 `endif
    task dump(input integer i, input [31:0] pc, input v, input sm,
              input l1s, input [3:0] l1st, input [1:0] rst,
              input sv, input pqv, input gw, input pv);
        $display("    STUCK core%0d pc %08x f2v %0d | stall_m %0d | l1 stall %0d st %0d | req st %0d send_v %0d pq_v %0d | gw_busy %0d push_v %0d",
                 i, pc, v, sm, l1s, l1st, rst, sv, pqv, gw, pv);
    endtask

    // An X in the fetch address is the failure that looks like a stall but is
    // not: nothing is held, the core simply stops meaning anything. Name the
    // first cycle it happens and what the predictor said that cycle.
    reg saw_x = 1'b0;
    always @(posedge clk) if (rstn && !saw_x &&
        (^dut.g_pe[0].g_have.u_pe.u_core.u_if.pc_fetch === 1'bx)) begin
        saw_x <= 1'b1;
        $display("    STUCK core0 first X in pc_fetch at %0t: f2_pc %08x f2_v %0d redir_q %0d pred_taken %0d pred_target %08x | bp init_q %0d q_v %0d q_c %0d q_t %02x",
                 $time,
                 dut.g_pe[0].g_have.u_pe.u_core.u_if.f2_pc,
                 dut.g_pe[0].g_have.u_pe.u_core.u_if.f2_valid,
                 dut.g_pe[0].g_have.u_pe.u_core.u_if.redir_q,
                 dut.g_pe[0].g_have.u_pe.u_core.u_if.pred_taken,
                 dut.g_pe[0].g_have.u_pe.u_core.u_if.pred_target,
                 dut.g_pe[0].g_have.u_pe.u_core.u_if.u_bp.init_q,
                 dut.g_pe[0].g_have.u_pe.u_core.u_if.u_bp.q_v,
                 dut.g_pe[0].g_have.u_pe.u_core.u_if.u_bp.q_c,
                 dut.g_pe[0].g_have.u_pe.u_core.u_if.u_bp.q_t);
    end

    initial begin
        #(`RV_MC_STUCK_AT);
        $display("--- state dump at %0t ---", $time);
        dump(0, dut.g_pe[0].g_have.u_pe.u_core.u_if.f2_pc,
                dut.g_pe[0].g_have.u_pe.u_core.u_if.f2_valid,
                dut.g_pe[0].g_have.u_pe.u_core.u_mem.stall_m,
                dut.g_pe[0].g_have.u_pe.u_l1.stall,
                dut.g_pe[0].g_have.u_pe.u_l1.st,
                dut.g_pe[0].g_have.u_pe.u_req.st,
                dut.g_pe[0].g_have.u_pe.u_req.send_valid,
                dut.g_pe[0].g_have.u_pe.u_req.pq_valid,
                dut.g_pe[0].g_have.u_pe.gw_busy,
                dut.g_pe[0].g_have.u_pe.push_valid);
        dump(1, dut.g_pe[1].g_have.u_pe.u_core.u_if.f2_pc,
                dut.g_pe[1].g_have.u_pe.u_core.u_if.f2_valid,
                dut.g_pe[1].g_have.u_pe.u_core.u_mem.stall_m,
                dut.g_pe[1].g_have.u_pe.u_l1.stall,
                dut.g_pe[1].g_have.u_pe.u_l1.st,
                dut.g_pe[1].g_have.u_pe.u_req.st,
                dut.g_pe[1].g_have.u_pe.u_req.send_valid,
                dut.g_pe[1].g_have.u_pe.u_req.pq_valid,
                dut.g_pe[1].g_have.u_pe.gw_busy,
                dut.g_pe[1].g_have.u_pe.push_valid);
    end
`endif

endmodule

`default_nettype wire
