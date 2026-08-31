// Two system nodes with the RV64 control complex, joined by the interlink:
// mesh 0 reaches mesh 1 on its UP link, mesh 1 answers on its DOWN link.
//
// Nothing else holds two PROCESSORS on one link. A doorbell rung from the
// control window went nowhere for as long as the window existed -- its
// address never matched the interlink's decode -- and no host-driven bench
// could see it. The bench is the NoC: a node's memory port is fed back to it.

`default_nettype none

module rv64_node_pair #(
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

    input  wire [31:0]           a_hs_addr,
    input  wire                  a_hs_wr,
    input  wire [63:0]           a_hs_wdata,
    input  wire [7:0]            a_hs_wstrb,
    input  wire                  a_hs_rd,
    output wire [63:0]           a_hs_rdata,
    output wire                  a_console_we,
    output wire [7:0]            a_console,
    output wire [63:0]           a_status,
    output wire                  a_pe_busy,

    input  wire [31:0]           b_hs_addr,
    input  wire                  b_hs_wr,
    input  wire [63:0]           b_hs_wdata,
    input  wire [7:0]            b_hs_wstrb,
    input  wire                  b_hs_rd,
    output wire [63:0]           b_hs_rdata,
    output wire                  b_console_we,
    output wire [7:0]            b_console,
    output wire [63:0]           b_status,
    output wire                  b_pe_busy,

    // Beats seen on each link, so a copy that never crossed is told apart
    // from one that crossed and landed wrong.
    output reg  [31:0]           up_beats,     // mesh 0 -> mesh 1
    output reg  [31:0]           dn_beats      // mesh 1 -> mesh 0
);
    // ---- the links, one direction each way --------------------------------
    // Forward wire per link per node, and the credit wire that node issues for
    // what it receives there.
    wire [LINK_W-1:0]  l0_f [0:1];
    wire [1:0]         l0_v, l0_vc, l0_l;
    wire [3:0]         l0_cn [0:1];
    wire [1:0]         l0_cv, l0_cvc;
    wire [LINK_W-1:0]  l1_f [0:1];
    wire [1:0]         l1_v, l1_vc, l1_l;
    wire [3:0]         l1_cn [0:1];
    wire [1:0]         l1_cv, l1_cvc;

    // ---- DRAM behind each node ----------------------------------------------
    wire [ID_W-1:0]   m_awid [0:1], m_arid [0:1], m_bid [0:1], m_rid [0:1];
    wire [ADDR_W-1:0] m_awaddr [0:1], m_araddr [0:1];
    wire [7:0]        m_awlen [0:1], m_arlen [0:1];
    wire [2:0]        m_awsize [0:1], m_arsize [0:1];
    wire [1:0]        m_awburst [0:1], m_arburst [0:1];
    wire [1:0]        m_bresp [0:1], m_rresp [0:1];
    wire [1:0]        m_awvalid, m_awready, m_arvalid, m_arready;
    wire [MW-1:0]     m_wdata [0:1], m_rdata [0:1];
    wire [MW/8-1:0]   m_wstrb [0:1];
    wire [1:0]        m_wlast, m_wvalid, m_wready;
    wire [1:0]        m_bvalid, m_bready, m_rlast, m_rvalid, m_rready;

    // ---- the memory port, looped back --------------------------------------
    wire [FLIT_WIDTH-1:0] mi_data [0:1], mo_data [0:1];
    wire [1:0]            mi_valid, mi_busy, mo_valid, mo_busy;

    // ---- the two host windows, indexed ------------------------------------
    wire [31:0] hs_addr  [0:1];
    wire [1:0]  hs_wr, hs_rd;
    wire [63:0] hs_wdata [0:1], hs_rdata [0:1];
    wire [7:0]  hs_wstrb [0:1];
    wire [1:0]  con_we;
    wire [7:0]  con [0:1];
    wire [63:0] pe_status [0:1];
    wire [1:0]  pe_busy;

    assign hs_addr[0]  = a_hs_addr;
    assign hs_wr[0]    = a_hs_wr;
    assign hs_wdata[0] = a_hs_wdata;
    assign hs_wstrb[0] = a_hs_wstrb;
    assign hs_rd[0]    = a_hs_rd;
    assign a_hs_rdata  = hs_rdata[0];
    assign a_console_we = con_we[0];
    assign a_console   = con[0];
    assign a_status    = pe_status[0];
    assign a_pe_busy   = pe_busy[0];

    assign hs_addr[1]  = b_hs_addr;
    assign hs_wr[1]    = b_hs_wr;
    assign hs_wdata[1] = b_hs_wdata;
    assign hs_wstrb[1] = b_hs_wstrb;
    assign hs_rd[1]    = b_hs_rd;
    assign b_hs_rdata  = hs_rdata[1];
    assign b_console_we = con_we[1];
    assign b_console   = con[1];
    assign b_status    = pe_status[1];
    assign b_pe_busy   = pe_busy[1];

    genvar g;
    generate
    for (g = 0; g < 2; g = g + 1) begin : node
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
            // Shortened so the simulator is not asked to model 2 MB.
            .STAGE         (1),
            .STAGE_BANKS   (4),
            .STAGE_ENTRIES (1024),
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

            .dram_awid       (m_awid[g]),
            .dram_awaddr     (m_awaddr[g]),
            .dram_awlen      (m_awlen[g]),
            .dram_awsize     (m_awsize[g]),
            .dram_awburst    (m_awburst[g]),
            .dram_awvalid    (m_awvalid[g]),
            .dram_awready    (m_awready[g]),
            .dram_wdata      (m_wdata[g]),
            .dram_wstrb      (m_wstrb[g]),
            .dram_wlast      (m_wlast[g]),
            .dram_wvalid     (m_wvalid[g]),
            .dram_wready     (m_wready[g]),
            .dram_bid        (m_bid[g]),
            .dram_bresp      (m_bresp[g]),
            .dram_bvalid     (m_bvalid[g]),
            .dram_bready     (m_bready[g]),
            .dram_arid       (m_arid[g]),
            .dram_araddr     (m_araddr[g]),
            .dram_arlen      (m_arlen[g]),
            .dram_arsize     (m_arsize[g]),
            .dram_arburst    (m_arburst[g]),
            .dram_arvalid    (m_arvalid[g]),
            .dram_arready    (m_arready[g]),
            .dram_rid        (m_rid[g]),
            .dram_rdata      (m_rdata[g]),
            .dram_rresp      (m_rresp[g]),
            .dram_rlast      (m_rlast[g]),
            .dram_rvalid     (m_rvalid[g]),
            .dram_rready     (m_rready[g]),

            .mem_in_data     (mi_data[g]),
            .mem_in_valid    (mi_valid[g]),
            .mem_in_busy     (mi_busy[g]),
            .mem_out_data    (mo_data[g]),
            .mem_out_valid   (mo_valid[g]),
            .mem_out_busy    (mo_busy[g]),
            .mem_rd_count    (),
            .mem_wr_count    (),
            .mv_busy         (),
            .mv_fault        (),
            .mv_done         (),
            .pe_halt_req     (1'b0),
            .pe_status       (pe_status[g]),
            .pe_busy         (pe_busy[g]),

            .hs_addr         (hs_addr[g]),
            .hs_wr           (hs_wr[g]),
            .hs_wdata        (hs_wdata[g]),
            .hs_wstrb        (hs_wstrb[g]),
            .hs_rd           (hs_rd[g]),
            .hs_rdata        (hs_rdata[g]),
            .hs_console_we   (con_we[g]),
            .hs_console      (con[g]),

            .link0_out_valid    (l0_v[g]),
            .link0_out_vc       (l0_vc[g]),
            .link0_out_last     (l0_l[g]),
            .link0_out_flit     (l0_f[g]),
            .link0_out_crd_valid((g == 1) ? l1_cv[0]  : 1'b0),
            .link0_out_crd_vc   ((g == 1) ? l1_cvc[0] : 1'b0),
            .link0_out_crd_n    ((g == 1) ? l1_cn[0]  : 4'd0),
            .link0_in_valid     ((g == 1) ? l1_v[0]  : 1'b0),
            .link0_in_vc        ((g == 1) ? l1_vc[0] : 1'b0),
            .link0_in_last      ((g == 1) ? l1_l[0]  : 1'b0),
            .link0_in_flit      ((g == 1) ? l1_f[0]  : {LINK_W{1'b0}}),
            .link0_in_crd_valid (l0_cv[g]),
            .link0_in_crd_vc    (l0_cvc[g]),
            .link0_in_crd_n     (l0_cn[g]),
            .link1_out_valid    (l1_v[g]),
            .link1_out_vc       (l1_vc[g]),
            .link1_out_last     (l1_l[g]),
            .link1_out_flit     (l1_f[g]),
            .link1_out_crd_valid((g == 0) ? l0_cv[1]  : 1'b0),
            .link1_out_crd_vc   ((g == 0) ? l0_cvc[1] : 1'b0),
            .link1_out_crd_n    ((g == 0) ? l0_cn[1]  : 4'd0),
            .link1_in_valid     ((g == 0) ? l0_v[1]  : 1'b0),
            .link1_in_vc        ((g == 0) ? l0_vc[1] : 1'b0),
            .link1_in_last      ((g == 0) ? l0_l[1]  : 1'b0),
            .link1_in_flit      ((g == 0) ? l0_f[1]  : {LINK_W{1'b0}}),
            .link1_in_crd_valid (l1_cv[g]),
            .link1_in_crd_vc    (l1_cvc[g]),
            .link1_in_crd_n     (l1_cn[g])
        );

        axi_ram #(
            .DATA_W (MW),
            .ADDR_W (ADDR_W),
            .ID_W   (ID_W),
            .WORDS  (2048),
            .PORTS  (1)
        ) ram (
            .clk       (clk),
            .resetn    (resetn),
            .s_awid    (m_awid[g]),
            .s_awaddr  (m_awaddr[g]),
            .s_awlen   (m_awlen[g]),
            .s_awsize  (m_awsize[g]),
            .s_awburst (m_awburst[g]),
            .s_awvalid (m_awvalid[g]),
            .s_awready (m_awready[g]),
            .s_wdata   (m_wdata[g]),
            .s_wstrb   (m_wstrb[g]),
            .s_wlast   (m_wlast[g]),
            .s_wvalid  (m_wvalid[g]),
            .s_wready  (m_wready[g]),
            .s_bid     (m_bid[g]),
            .s_bresp   (m_bresp[g]),
            .s_bvalid  (m_bvalid[g]),
            .s_bready  (m_bready[g]),
            .s_arid    (m_arid[g]),
            .s_araddr  (m_araddr[g]),
            .s_arlen   (m_arlen[g]),
            .s_arsize  (m_arsize[g]),
            .s_arburst (m_arburst[g]),
            .s_arvalid (m_arvalid[g]),
            .s_arready (m_arready[g]),
            .s_rid     (m_rid[g]),
            .s_rdata   (m_rdata[g]),
            .s_rresp   (m_rresp[g]),
            .s_rlast   (m_rlast[g]),
            .s_rvalid  (m_rvalid[g]),
            .s_rready  (m_rready[g]),
            .bd_we     (1'b0),
            .bd_addr   (16'd0),
            .bd_wdata  ({MW{1'b0}}),
            .bd_rdata  ()
        );

        // The memory port fed back to itself. The link holds a flit until a
        // cycle with `busy` low, so the queue's full flag is the busy.
        wire fb_full, fb_empty;
        assign mo_busy[g]  = fb_full;
        assign mi_valid[g] = !fb_empty;

        sync_fifo #(
            .DATA_WIDTH  (FLIT_WIDTH),
            .FIFO_DEPTH  (32),
            .MEMORY_TYPE ("distributed")
        ) fb (
            .clk       (clk),
            .rst       (!resetn),
            .wr_en     (mo_valid[g] && !fb_full),
            .wr_data   (mo_data[g]),
            .wr_busy   (fb_full),
            .wr_almost (),
            .rd_en     (mi_valid[g] && !mi_busy[g]),
            .rd_data   (mi_data[g]),
            .rd_busy   (fb_empty)
        );
    end
    endgenerate

    always @(posedge clk) begin
        if (!resetn) begin
            up_beats <= 32'd0;
            dn_beats <= 32'd0;
        end else begin
            if (l0_v[0] || l1_v[0]) begin
                up_beats <= up_beats + 32'd1;
            end
            if (l0_v[1] || l1_v[1]) begin
                dn_beats <= dn_beats + 32'd1;
            end
        end
    end

endmodule

`default_nettype wire
