// AXI4 slave RAM. Stands in for DDR4 (or an on-chip URAM scratch) so the
// machine can be simulated end to end without a memory controller.
//
// Deliberately simple: one outstanding transaction per port, INCR bursts only,
// no narrow transfers, no interleaving. That is enough to be the far side of
// MAG's master port, and being simple is the point -- a system test should fail
// because the system is wrong, not because the stub grew its own bugs.
//
// DATA_W is 256 to match the NoC flit payload exactly, so nothing in the
// simulation path has to gear 512 <-> 256. A real DDR4 attachment is 512 bits
// at the MIG user interface and needs that conversion inside MAG; see
// docs/mas/spec.md s2.
//
// PORTS INDEPENDENT CHANNELS OVER ONE ARRAY. A single 256-bit port at 300 MHz
// is 9.6 GB/s; DDR4-2400 is 19.2 GB/s per channel and a real part has several,
// so one port is a model of a narrower memory system than the one this design
// targets -- and with eight clusters behind one MAG that stub becomes the
// answer rather than the scenery. Each port has its own AW/W/B and AR/R state
// and they read and write the same `mem`, which is what a multi-channel
// controller in front of one address space actually offers.
//
// Ports are FLATTENED vectors, so at PORTS = 1 every width is exactly what it
// was and existing single-port instantiations connect unchanged.
//
// Two ports writing the same word in the same cycle is last-writer-wins here
// and unordered on real hardware. Nothing in this machine does it: each MAG
// port owns the C tiles of its own clusters, and the operands are read-only
// once uploaded.

`default_nettype none

module axi_ram #(
    parameter integer DATA_W = 256,
    parameter integer ADDR_W = 40,
    parameter integer ID_W   = 4,
    parameter integer WORDS  = 4096,         // DATA_W-bit words
    parameter integer PORTS  = 1
)(
    input  wire                     clk,
    input  wire                     resetn,

    input  wire [PORTS*ID_W-1:0]    s_awid,
    input  wire [PORTS*ADDR_W-1:0]  s_awaddr,
    input  wire [PORTS*8-1:0]       s_awlen,
    input  wire [PORTS*3-1:0]       s_awsize,
    input  wire [PORTS*2-1:0]       s_awburst,
    input  wire [PORTS-1:0]         s_awvalid,
    output wire [PORTS-1:0]         s_awready,

    input  wire [PORTS*DATA_W-1:0]  s_wdata,
    input  wire [PORTS*DATA_W/8-1:0] s_wstrb,
    input  wire [PORTS-1:0]         s_wlast,
    input  wire [PORTS-1:0]         s_wvalid,
    output wire [PORTS-1:0]         s_wready,

    output wire [PORTS*ID_W-1:0]    s_bid,
    output wire [PORTS*2-1:0]       s_bresp,
    output wire [PORTS-1:0]         s_bvalid,
    input  wire [PORTS-1:0]         s_bready,

    input  wire [PORTS*ID_W-1:0]    s_arid,
    input  wire [PORTS*ADDR_W-1:0]  s_araddr,
    input  wire [PORTS*8-1:0]       s_arlen,
    input  wire [PORTS*3-1:0]       s_arsize,
    input  wire [PORTS*2-1:0]       s_arburst,
    input  wire [PORTS-1:0]         s_arvalid,
    output wire [PORTS-1:0]         s_arready,

    output wire [PORTS*ID_W-1:0]    s_rid,
    output wire [PORTS*DATA_W-1:0]  s_rdata,
    output wire [PORTS*2-1:0]       s_rresp,
    output wire [PORTS-1:0]         s_rlast,
    output wire [PORTS-1:0]         s_rvalid,
    input  wire [PORTS-1:0]         s_rready,

    // backdoor, for the bench to preload and check without going through AXI
    input  wire                     bd_we,
    input  wire [15:0]              bd_addr,
    input  wire [DATA_W-1:0]        bd_wdata,
    output wire [DATA_W-1:0]        bd_rdata
);
    localparam integer AW  = $clog2(WORDS);
    localparam integer LSB = $clog2(DATA_W/8);      // byte offset within a word

    reg [DATA_W-1:0] mem [0:WORDS-1];

    assign bd_rdata = mem[bd_addr[AW-1:0]];

    always @(posedge clk) begin
        if (resetn && bd_we) begin
            mem[bd_addr[AW-1:0]] <= bd_wdata;
        end
    end

    genvar gp;
    generate
    for (gp = 0; gp < PORTS; gp = gp + 1) begin : g_port
        // ---------------- write ----------------
        reg [AW-1:0] wptr;
        reg [7:0]    wcnt;
        reg [1:0]    wst;
        reg          awready_r, wready_r, bvalid_r;
        reg [ID_W-1:0] bid_r;
        reg [1:0]      bresp_r;

        localparam [1:0] W_IDLE = 2'd0, W_DATA = 2'd1, W_RESP = 2'd2;

        assign s_awready[gp]              = awready_r;
        assign s_wready[gp]               = wready_r;
        assign s_bvalid[gp]               = bvalid_r;
        assign s_bid[gp*ID_W +: ID_W]     = bid_r;
        assign s_bresp[gp*2 +: 2]         = bresp_r;

        integer bi;
        always @(posedge clk) begin
            if (!resetn) begin
                wst <= W_IDLE; awready_r <= 1'b1; wready_r <= 1'b0;
                bvalid_r <= 1'b0; bid_r <= {ID_W{1'b0}}; bresp_r <= 2'b00;
                wptr <= {AW{1'b0}}; wcnt <= 8'd0;
            end else begin
                case (wst)
                    W_IDLE: if (s_awvalid[gp] && awready_r) begin
                        wptr      <= s_awaddr[gp*ADDR_W +: ADDR_W] >> LSB;
                        wcnt      <= s_awlen[gp*8 +: 8];
                        bid_r     <= s_awid[gp*ID_W +: ID_W];
                        awready_r <= 1'b0;
                        wready_r  <= 1'b1;
                        wst       <= W_DATA;
                    end
                    W_DATA: if (s_wvalid[gp] && wready_r) begin
                        for (bi = 0; bi < DATA_W/8; bi = bi + 1) begin
                            if (s_wstrb[gp*(DATA_W/8) + bi]) begin
                                mem[wptr][bi*8 +: 8] <= (
                                    s_wdata[gp*DATA_W + bi*8 +: 8]
                                );
                            end
                        end
                        wptr <= wptr + 1'b1;
                        if (s_wlast[gp] || wcnt == 8'd0) begin
                            wready_r <= 1'b0;
                            bresp_r  <= 2'b00;
                            bvalid_r <= 1'b1;
                            wst      <= W_RESP;
                        end
                        else begin
                            wcnt <= wcnt - 8'd1;
                        end
                    end
                    W_RESP: if (s_bready[gp]) begin
                        bvalid_r  <= 1'b0;
                        awready_r <= 1'b1;
                        wst       <= W_IDLE;
                    end
                    default: wst <= W_IDLE;
                endcase
            end
        end

        // ---------------- read ----------------
        reg [AW-1:0] rptr;
        reg [7:0]    rcnt;
        reg          rst_s;
        reg          arready_r, rvalid_r, rlast_r;
        reg [ID_W-1:0]   rid_r;
        reg [DATA_W-1:0] rdata_r;

        assign s_arready[gp]                = arready_r;
        assign s_rvalid[gp]                 = rvalid_r;
        assign s_rlast[gp]                  = rlast_r;
        assign s_rid[gp*ID_W +: ID_W]       = rid_r;
        assign s_rdata[gp*DATA_W +: DATA_W] = rdata_r;
        assign s_rresp[gp*2 +: 2]           = 2'b00;

        always @(posedge clk) begin
            if (!resetn) begin
                rst_s <= 1'b0; arready_r <= 1'b1;
                rvalid_r <= 1'b0; rlast_r <= 1'b0;
                rid_r <= {ID_W{1'b0}}; rdata_r <= {DATA_W{1'b0}};
                rptr <= {AW{1'b0}}; rcnt <= 8'd0;
            end else if (!rst_s) begin
                if (s_arvalid[gp] && arready_r) begin
                    rptr      <= s_araddr[gp*ADDR_W +: ADDR_W] >> LSB;
                    rcnt      <= s_arlen[gp*8 +: 8];
                    rid_r     <= s_arid[gp*ID_W +: ID_W];
                    arready_r <= 1'b0;
                    rdata_r   <= mem[(s_araddr[gp*ADDR_W +: ADDR_W] >> LSB)];
                    rlast_r   <= (s_arlen[gp*8 +: 8] == 8'd0);
                    rvalid_r  <= 1'b1;
                    rst_s     <= 1'b1;
                end
            end else if (s_rready[gp]) begin
                if (rcnt == 8'd0) begin
                    rvalid_r  <= 1'b0;
                    rlast_r   <= 1'b0;
                    arready_r <= 1'b1;
                    rst_s     <= 1'b0;
                end else begin
                    rptr    <= rptr + 1'b1;
                    rcnt    <= rcnt - 8'd1;
                    rdata_r <= mem[rptr + 1'b1];
                    rlast_r <= (rcnt == 8'd1);
                end
            end
        end
    end
    endgenerate

endmodule

`default_nettype wire
