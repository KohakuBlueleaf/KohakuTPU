// kos_cluster -- NODES whole system nodes (RV64 complex, memory agent, mover,
// staging, interlink) on one chain, each with its own program, for running
// KohakuAccel OS on the processors alone: no mesh, no compute units.
//
// The chain is the card's: node g's UP link (link1) meets node g+1's DOWN link
// (link0), mesh ids 0..NODES-1 in chain order (mag_switch CH_SEQ).
//
// SHARED selects the memory: 0 gives every node its own DRAM (pure AMP: a
// node reads only its own memory and writes others' through the interlink);
// 1 gives all nodes ONE multi-port DRAM, the card's flat shared memory, where
// every spelling of an address is the same word.
//
// Each node's memory port is fed back to itself, as in rv64_node_pair: there
// are no units on the mesh. The DRAM backdoor (bd_*) lets the harness place
// and read DRAM bytes without the fabric.

`default_nettype none

module kos_cluster #(
    parameter integer NODES      = 2,
    parameter integer SHARED     = 0,
    parameter integer DRAM_WORDS = 65536,   // 64-byte words per DRAM (4 MB)
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer DATA_W     = 256,
    parameter integer ADDR_W     = 40,
    parameter integer ID_W       = 4,
    parameter integer MW         = 512,
    parameter integer LINK_W     = 288,
    parameter integer TUSER_W    = 96
)(
    input  wire                  clk,
    input  wire                  resetn,

    // one host window per node, node n in slice n
    input  wire [NODES*32-1:0]   hs_addr,
    input  wire [NODES-1:0]      hs_wr,
    input  wire [NODES*64-1:0]   hs_wdata,
    input  wire [NODES*8-1:0]    hs_wstrb,
    input  wire [NODES-1:0]      hs_rd,
    output wire [NODES*64-1:0]   hs_rdata,
    output wire [NODES-1:0]      console_we,
    output wire [NODES*8-1:0]    console,
    output wire [NODES-1:0]      pe_busy,

    // DRAM backdoor: RAM `bd_ram` (0 when SHARED), word `bd_addr`
    input  wire                  bd_we,
    input  wire [3:0]            bd_ram,
    input  wire [15:0]           bd_addr,
    input  wire [MW-1:0]         bd_wdata,
    output wire [MW-1:0]         bd_rdata,

    // beats each node sent on its links, up and down
    output wire [NODES*32-1:0]   link_beats
);
    localparam integer RAMS = SHARED ? 1 : NODES;

    // ---- links: per node, its link0 (down) and link1 (up) outputs ----------
    wire [LINK_W-1:0] l0_f [0:NODES-1];
    wire [LINK_W-1:0] l1_f [0:NODES-1];
    wire [NODES-1:0]  l0_v, l0_vc, l0_l, l1_v, l1_vc, l1_l;
    wire [3:0]        l0_cn [0:NODES-1];
    wire [3:0]        l1_cn [0:NODES-1];
    wire [NODES-1:0]  l0_cv, l0_cvc, l1_cv, l1_cvc;

    // ---- each node's DRAM master, flattened for the RAM's ports -----------
    wire [NODES*ID_W-1:0]   m_awid, m_arid, m_bid, m_rid;
    wire [NODES*ADDR_W-1:0] m_awaddr, m_araddr;
    wire [NODES*8-1:0]      m_awlen, m_arlen;
    wire [NODES*3-1:0]      m_awsize, m_arsize;
    wire [NODES*2-1:0]      m_awburst, m_arburst, m_bresp, m_rresp;
    wire [NODES-1:0]        m_awvalid, m_awready, m_arvalid, m_arready;
    wire [NODES*MW-1:0]     m_wdata, m_rdata;
    wire [NODES*MW/8-1:0]   m_wstrb;
    wire [NODES-1:0]        m_wlast, m_wvalid, m_wready;
    wire [NODES-1:0]        m_bvalid, m_bready, m_rlast, m_rvalid, m_rready;

    wire [MW-1:0] bd_q [0:RAMS-1];
    assign bd_rdata = bd_q[bd_ram < RAMS ? bd_ram : 0];

    genvar g;
    generate
    for (g = 0; g < NODES; g = g + 1) begin : node
        wire [FLIT_WIDTH-1:0] mi_data, mo_data;
        wire                  mi_valid, mi_busy, mo_valid, mo_busy;
        reg  [31:0]           beats;
        assign link_beats[g*32 +: 32] = beats;

        always @(posedge clk) begin
            if (!resetn) begin
                beats <= 32'd0;
            end else if (l0_v[g] || l1_v[g]) begin
                beats <= beats + 32'd1;
            end
        end

        sysnode #(
            .FLIT_WIDTH    (FLIT_WIDTH),
            .POS_WIDTH     (POS_WIDTH),
            .DATA_W        (DATA_W),
            .ADDR_W        (ADDR_W),
            .ID_W          (ID_W),
            .PORTS         (1),
            .MEM_X         (0),
            .MEM_Y         (1),
            .GRID_LO       (1),
            .GRID_HI       (1),
            .STAGE_FLITS   (128),
            .ILINK         (1),
            .MESH_ID       (g),
            .LINK_W        (LINK_W),
            .TUSER_W       (TUSER_W),
            .MW            (MW),
            .STAGE         (1),
            .STAGE_BANKS   (4),
            .STAGE_ENTRIES (4096),          // 512 KB of staging
            .STAGE_AT_PORT (1),
            .PE_IMEM       (8192),
            .PE_SPAD       (4096),
            .PE_L1_LINES   (64)
        ) u (
            .clk             (clk),
            .resetn          (resetn),
            .dram_aclk       (clk),
            .dram_aresetn    (resetn),

            .sm_awid         ({ID_W{1'b0}}),
            .sm_awaddr       ({ADDR_W{1'b0}}),
            .sm_awlen        (8'd0),
            .sm_awvalid      (1'b0),
            .sm_awready      (),
            .sm_wdata        ({DATA_W{1'b0}}),
            .sm_wstrb        ({(DATA_W/8){1'b0}}),
            .sm_wlast        (1'b0),
            .sm_wvalid       (1'b0),
            .sm_wready       (),
            .sm_bid          (),
            .sm_bresp        (),
            .sm_bvalid       (),
            .sm_bready       (1'b1),
            .sm_arid         ({ID_W{1'b0}}),
            .sm_araddr       ({ADDR_W{1'b0}}),
            .sm_arlen        (8'd0),
            .sm_arvalid      (1'b0),
            .sm_arready      (),
            .sm_rid          (),
            .sm_rdata        (),
            .sm_rresp        (),
            .sm_rlast        (),
            .sm_rvalid       (),
            .sm_rready       (1'b1),

            .sc_awid         ({ID_W{1'b0}}),
            .sc_awaddr       (32'd0),
            .sc_awlen        (8'd0),
            .sc_awvalid      (1'b0),
            .sc_awready      (),
            .sc_wdata        (64'd0),
            .sc_wstrb        (8'hFF),
            .sc_wlast        (1'b1),
            .sc_wvalid       (1'b0),
            .sc_wready       (),
            .sc_bid          (),
            .sc_bresp        (),
            .sc_bvalid       (),
            .sc_bready       (1'b1),
            .sc_arid         ({ID_W{1'b0}}),
            .sc_araddr       (32'd0),
            .sc_arlen        (8'd0),
            .sc_arvalid      (1'b0),
            .sc_arready      (),
            .sc_rid          (),
            .sc_rdata        (),
            .sc_rresp        (),
            .sc_rlast        (),
            .sc_rvalid       (),
            .sc_rready       (1'b1),

            .dram_awid       (m_awid[g*ID_W +: ID_W]),
            .dram_awaddr     (m_awaddr[g*ADDR_W +: ADDR_W]),
            .dram_awlen      (m_awlen[g*8 +: 8]),
            .dram_awsize     (m_awsize[g*3 +: 3]),
            .dram_awburst    (m_awburst[g*2 +: 2]),
            .dram_awvalid    (m_awvalid[g]),
            .dram_awready    (m_awready[g]),
            .dram_wdata      (m_wdata[g*MW +: MW]),
            .dram_wstrb      (m_wstrb[g*MW/8 +: MW/8]),
            .dram_wlast      (m_wlast[g]),
            .dram_wvalid     (m_wvalid[g]),
            .dram_wready     (m_wready[g]),
            .dram_bid        (m_bid[g*ID_W +: ID_W]),
            .dram_bresp      (m_bresp[g*2 +: 2]),
            .dram_bvalid     (m_bvalid[g]),
            .dram_bready     (m_bready[g]),
            .dram_arid       (m_arid[g*ID_W +: ID_W]),
            .dram_araddr     (m_araddr[g*ADDR_W +: ADDR_W]),
            .dram_arlen      (m_arlen[g*8 +: 8]),
            .dram_arsize     (m_arsize[g*3 +: 3]),
            .dram_arburst    (m_arburst[g*2 +: 2]),
            .dram_arvalid    (m_arvalid[g]),
            .dram_arready    (m_arready[g]),
            .dram_rid        (m_rid[g*ID_W +: ID_W]),
            .dram_rdata      (m_rdata[g*MW +: MW]),
            .dram_rresp      (m_rresp[g*2 +: 2]),
            .dram_rlast      (m_rlast[g]),
            .dram_rvalid     (m_rvalid[g]),
            .dram_rready     (m_rready[g]),

            .mem_in_data     (mi_data),
            .mem_in_valid    (mi_valid),
            .mem_in_busy     (mi_busy),
            .mem_out_data    (mo_data),
            .mem_out_valid   (mo_valid),
            .mem_out_busy    (mo_busy),
            .mem_rd_count    (),
            .mem_wr_count    (),
            .mv_busy         (),
            .mv_fault        (),
            .mv_done         (),
            .pe_halt_req     (1'b0),
            .pe_status       (),
            .pe_busy         (pe_busy[g]),

            .hs_addr         (hs_addr[g*32 +: 32]),
            .hs_wr           (hs_wr[g]),
            .hs_wdata        (hs_wdata[g*64 +: 64]),
            .hs_wstrb        (hs_wstrb[g*8 +: 8]),
            .hs_rd           (hs_rd[g]),
            .hs_rdata        (hs_rdata[g*64 +: 64]),
            .hs_console_we   (console_we[g]),
            .hs_console      (console[g*8 +: 8]),

            .link0_out_valid    (l0_v[g]),
            .link0_out_vc       (l0_vc[g]),
            .link0_out_last     (l0_l[g]),
            .link0_out_flit     (l0_f[g]),
            .link0_out_crd_valid((g > 0) ? l1_cv[g-1]  : 1'b0),
            .link0_out_crd_vc   ((g > 0) ? l1_cvc[g-1] : 1'b0),
            .link0_out_crd_n    ((g > 0) ? l1_cn[g-1]  : 4'd0),
            .link0_in_valid     ((g > 0) ? l1_v[g-1]   : 1'b0),
            .link0_in_vc        ((g > 0) ? l1_vc[g-1]  : 1'b0),
            .link0_in_last      ((g > 0) ? l1_l[g-1]   : 1'b0),
            .link0_in_flit      ((g > 0) ? l1_f[g-1]   : {LINK_W{1'b0}}),
            .link0_in_crd_valid (l0_cv[g]),
            .link0_in_crd_vc    (l0_cvc[g]),
            .link0_in_crd_n     (l0_cn[g]),
            .link1_out_valid    (l1_v[g]),
            .link1_out_vc       (l1_vc[g]),
            .link1_out_last     (l1_l[g]),
            .link1_out_flit     (l1_f[g]),
            .link1_out_crd_valid((g < NODES-1) ? l0_cv[g+1]  : 1'b0),
            .link1_out_crd_vc   ((g < NODES-1) ? l0_cvc[g+1] : 1'b0),
            .link1_out_crd_n    ((g < NODES-1) ? l0_cn[g+1]  : 4'd0),
            .link1_in_valid     ((g < NODES-1) ? l0_v[g+1]   : 1'b0),
            .link1_in_vc        ((g < NODES-1) ? l0_vc[g+1]  : 1'b0),
            .link1_in_last      ((g < NODES-1) ? l0_l[g+1]   : 1'b0),
            .link1_in_flit      ((g < NODES-1) ? l0_f[g+1]   : {LINK_W{1'b0}}),
            .link1_in_crd_valid (l1_cv[g]),
            .link1_in_crd_vc    (l1_cvc[g]),
            .link1_in_crd_n     (l1_cn[g])
        );

        // The memory port fed back to itself: no units on this mesh.
        wire fb_full, fb_empty;
        assign mo_busy  = fb_full;
        assign mi_valid = !fb_empty;

        sync_fifo #(
            .DATA_WIDTH  (FLIT_WIDTH),
            .FIFO_DEPTH  (32),
            .MEMORY_TYPE ("distributed")
        ) fb (
            .clk       (clk),
            .rst       (!resetn),
            .wr_en     (mo_valid && !fb_full),
            .wr_data   (mo_data),
            .wr_busy   (fb_full),
            .wr_almost (),
            .rd_en     (mi_valid && !mi_busy),
            .rd_data   (mi_data),
            .rd_busy   (fb_empty)
        );
    end

    // ---- DRAM: one RAM per node, or one RAM with a port per node -------------
    for (g = 0; g < RAMS; g = g + 1) begin : ram
        localparam integer P  = SHARED ? NODES : 1;
        localparam integer LO = SHARED ? 0 : g;

        axi_ram #(
            .DATA_W (MW),
            .ADDR_W (ADDR_W),
            .ID_W   (ID_W),
            .WORDS  (DRAM_WORDS),
            .PORTS  (P)
        ) u (
            .clk       (clk),
            .resetn    (resetn),
            .s_awid    (m_awid[LO*ID_W +: P*ID_W]),
            .s_awaddr  (m_awaddr[LO*ADDR_W +: P*ADDR_W]),
            .s_awlen   (m_awlen[LO*8 +: P*8]),
            .s_awsize  (m_awsize[LO*3 +: P*3]),
            .s_awburst (m_awburst[LO*2 +: P*2]),
            .s_awvalid (m_awvalid[LO +: P]),
            .s_awready (m_awready[LO +: P]),
            .s_wdata   (m_wdata[LO*MW +: P*MW]),
            .s_wstrb   (m_wstrb[LO*MW/8 +: P*MW/8]),
            .s_wlast   (m_wlast[LO +: P]),
            .s_wvalid  (m_wvalid[LO +: P]),
            .s_wready  (m_wready[LO +: P]),
            .s_bid     (m_bid[LO*ID_W +: P*ID_W]),
            .s_bresp   (m_bresp[LO*2 +: P*2]),
            .s_bvalid  (m_bvalid[LO +: P]),
            .s_bready  (m_bready[LO +: P]),
            .s_arid    (m_arid[LO*ID_W +: P*ID_W]),
            .s_araddr  (m_araddr[LO*ADDR_W +: P*ADDR_W]),
            .s_arlen   (m_arlen[LO*8 +: P*8]),
            .s_arsize  (m_arsize[LO*3 +: P*3]),
            .s_arburst (m_arburst[LO*2 +: P*2]),
            .s_arvalid (m_arvalid[LO +: P]),
            .s_arready (m_arready[LO +: P]),
            .s_rid     (m_rid[LO*ID_W +: P*ID_W]),
            .s_rdata   (m_rdata[LO*MW +: P*MW]),
            .s_rresp   (m_rresp[LO*2 +: P*2]),
            .s_rlast   (m_rlast[LO +: P]),
            .s_rvalid  (m_rvalid[LO +: P]),
            .s_rready  (m_rready[LO +: P]),
            .bd_we     (bd_we && (bd_ram == g)),
            .bd_addr   (bd_addr),
            .bd_wdata  (bd_wdata),
            .bd_rdata  (bd_q[g])
        );
    end
    endgenerate

endmodule

`default_nettype wire
