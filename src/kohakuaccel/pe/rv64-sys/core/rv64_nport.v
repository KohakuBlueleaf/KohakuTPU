// rv64_nport -- the one AXI master, shared by everything that leaves the CPU.
//
// Four clients: the page-table walker, the L1's fill and writeback, and an
// uncached 64-bit access. AT MOST ONE IS EVER ACTIVE, which is what lets this be
// a priority mux instead of a queue:
//
//   - a walk runs to completion before the access that triggered it is retried,
//     because the MMU holds the core while it walks;
//   - the L1 sequences its own eviction ahead of its own fill;
//   - the core stalls on any node access, so it cannot issue a second one.
//
// The invariant is worth stating because it is the thing that breaks first if
// the L1 ever becomes non-blocking: then fill and uncached CAN overlap and this
// module needs real arbitration and per-client response routing.
//
// A LINE IS ONE BEAT. 32 bytes is one 256-bit AXI beat, so a fill is awlen=0 and
// a writeback is one beat -- no bursts anywhere in here.

`default_nettype none

module rv64_nport #(
    parameter integer ADDR_W = 40,
    parameter integer DATA_W = 256
)(
    input  wire                  clk,
    input  wire                  resetn,

    // ---- walker: a 64-bit read ----
    input  wire                  w_req,
    input  wire [ADDR_W-1:0]     w_addr,
    output reg                   w_ack,
    output reg  [63:0]           w_data,

    // ---- L1 fill: a 32-byte read ----
    input  wire                  fill_valid,
    output wire                  fill_ready,
    input  wire [ADDR_W-6:0]     fill_addr,
    output reg                   resp_valid,
    output reg  [255:0]          resp_data,

    // ---- L1 writeback: a 32-byte write ----
    input  wire                  wb_valid,
    output wire                  wb_ready,
    input  wire [ADDR_W-6:0]     wb_addr,
    input  wire [255:0]          wb_data,

    // ---- uncached 64-bit access ----
    input  wire                  u_req,
    input  wire                  u_we,
    input  wire [ADDR_W-1:0]     u_addr,
    input  wire [7:0]            u_be,
    input  wire [63:0]           u_wdata,
    output reg                   u_ack,
    output reg  [63:0]           u_rdata,

    output wire                  wr_idle,       // no write outstanding

    // ---- the AXI master ----
    output reg  [ADDR_W-1:0]     cp_awaddr,
    output wire [7:0]            cp_awlen,
    output reg                   cp_awvalid,
    input  wire                  cp_awready,
    output reg  [DATA_W-1:0]     cp_wdata,
    output reg  [DATA_W/8-1:0]   cp_wstrb,
    output wire                  cp_wlast,
    output reg                   cp_wvalid,
    input  wire                  cp_wready,
    input  wire                  cp_bvalid,
    output wire                  cp_bready,
    output reg  [ADDR_W-1:0]     cp_araddr,
    output wire [7:0]            cp_arlen,
    output reg                   cp_arvalid,
    input  wire                  cp_arready,
    input  wire [DATA_W-1:0]     cp_rdata,
    input  wire                  cp_rlast,
    input  wire                  cp_rvalid,
    output wire                  cp_rready
);
    localparam integer LSB = $clog2(DATA_W / 8);

    assign cp_awlen  = 8'd0;
    assign cp_arlen  = 8'd0;
    assign cp_wlast  = 1'b1;
    assign cp_bready = 1'b1;
    assign cp_rready = 1'b1;

    localparam [2:0] S_IDLE = 3'd0, S_RD = 3'd1, S_WR = 3'd2;
    localparam [1:0] C_WALK = 2'd0, C_FILL = 2'd1, C_WB = 2'd2, C_UNC = 2'd3;

    reg [2:0] st;
    reg [1:0] who;
    reg [LSB-4:0] lane;          // which 64-bit lane of the beat

    assign fill_ready = (st == S_IDLE) && !w_req;
    assign wb_ready   = (st == S_IDLE) && !w_req && !fill_valid;
    assign wr_idle    = (st != S_WR) && !cp_awvalid && !cp_wvalid;

    always @(posedge clk) begin
        if (!resetn) begin
            st         <= S_IDLE;
            cp_awvalid <= 1'b0;
            cp_wvalid  <= 1'b0;
            cp_arvalid <= 1'b0;
            w_ack      <= 1'b0;
            u_ack      <= 1'b0;
            resp_valid <= 1'b0;
        end
        else begin
            w_ack      <= 1'b0;
            u_ack      <= 1'b0;
            resp_valid <= 1'b0;

            case (st)
                S_IDLE: begin
                    // Priority, and it is the order that keeps the invariant:
                    // a walk is holding the core, a fill is holding the L1.
                    //
                    // NOT `w_req` ALONE. The acknowledge and the return to idle
                    // land on the same edge, so on the next cycle the client has
                    // not yet seen its ack and its request is still asserted --
                    // and the same PTE is fetched twice. A duplicate response
                    // then satisfies the NEXT level of the walk, which computes
                    // its address from a base one level stale.
                    if (w_req && !w_ack) begin
                        who       <= C_WALK;
                        lane      <= w_addr[LSB-1:3];
                        cp_araddr <= {w_addr[ADDR_W-1:LSB], {LSB{1'b0}}};
                        cp_arvalid<= 1'b1;
                        st        <= S_RD;
                    end
                    else if (fill_valid) begin
                        who       <= C_FILL;
                        cp_araddr <= {fill_addr, 5'd0};
                        cp_arvalid<= 1'b1;
                        st        <= S_RD;
                    end
                    else if (wb_valid) begin
                        who       <= C_WB;
                        cp_awaddr <= {wb_addr, 5'd0};
                        cp_awvalid<= 1'b1;
                        cp_wdata  <= wb_data;
                        cp_wstrb  <= {(DATA_W/8){1'b1}};
                        cp_wvalid <= 1'b1;
                        st        <= S_WR;
                    end
                    else if (u_req && !u_ack) begin
                        who  <= C_UNC;
                        lane <= u_addr[LSB-1:3];
                        if (u_we) begin
                            cp_awaddr <= {u_addr[ADDR_W-1:LSB], {LSB{1'b0}}};
                            cp_awvalid<= 1'b1;
                            cp_wdata  <= {(DATA_W/64){u_wdata}};
                            cp_wstrb  <= {{(DATA_W/8-8){1'b0}}, u_be}
                                         << {u_addr[LSB-1:3], 3'd0};
                            cp_wvalid <= 1'b1;
                            st        <= S_WR;
                        end
                        else begin
                            cp_araddr <= {u_addr[ADDR_W-1:LSB], {LSB{1'b0}}};
                            cp_arvalid<= 1'b1;
                            st        <= S_RD;
                        end
                    end
                end

                S_RD: begin
                    if (cp_arvalid && cp_arready) begin
                        cp_arvalid <= 1'b0;
                    end
                    if (cp_rvalid) begin
                        case (who)
                            C_FILL: begin
                                resp_data  <= cp_rdata;
                                resp_valid <= 1'b1;
                            end
                            C_WALK: begin
                                w_data <= cp_rdata[{lane, 6'd0} +: 64];
                                w_ack  <= 1'b1;
                            end
                            default: begin
                                u_rdata <= cp_rdata[{lane, 6'd0} +: 64];
                                u_ack   <= 1'b1;
                            end
                        endcase
                        st <= S_IDLE;
                    end
                end

                S_WR: begin
                    if (cp_awvalid && cp_awready) begin
                        cp_awvalid <= 1'b0;
                    end
                    if (cp_wvalid && cp_wready) begin
                        cp_wvalid <= 1'b0;
                    end
                    if (cp_bvalid) begin
                        if (who == C_UNC) begin
                            u_ack <= 1'b1;
                        end
                        st <= S_IDLE;
                    end
                end

                default: st <= S_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
