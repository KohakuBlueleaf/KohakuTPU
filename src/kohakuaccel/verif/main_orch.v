// Main orchestrator: an AXI4 SLAVE so the host can load a control program, and
// an AXI4 MASTER so it can execute one.
//
// NOT a NoC node: its reach into the mesh is an AXI write into a MAG control
// window, which MAG turns into a flit, so dispatch, TLB programming and debug
// injection share one mechanism. See docs/arch-design.md s6.4.
//
// THE DRIVER-SIDE INSTRUCTION SET. The host writes a list of commands, then
// writes GO:
//
//   WR    addr, data          issue an AXI write
//   POLL  addr, mask, want    read addr until (data & mask) == want
//   DONE  code                stop, latch code, raise the done flag
//
// Three opcodes are enough because the machine's whole control surface is
// memory-mapped; branches or arithmetic here would duplicate the host. The
// value is that a run becomes ONE host transaction, so the host is not in the
// loop per poll and the same program works over JTAG and over PCIe.

`default_nettype none

module main_orch #(
    parameter integer ADDR_W  = 32,
    parameter integer ID_W    = 4,
    parameter integer NCMD    = 128,         // command slots; NCMD*32 <= 0x1000
    parameter integer POLL_IVL = 31          // cycles between POLL retries
)(
    input  wire               clk,
    input  wire               resetn,

    // ---- AXI4-Lite-ish slave: the host loads commands and polls status ----
    input  wire [ID_W-1:0]    s_awid,
    input  wire [ADDR_W-1:0]  s_awaddr,
    input  wire [7:0]         s_awlen,
    input  wire               s_awvalid,
    output reg                s_awready,
    input  wire [63:0]        s_wdata,
    input  wire [7:0]         s_wstrb,
    input  wire               s_wlast,
    input  wire               s_wvalid,
    output reg                s_wready,
    output reg  [ID_W-1:0]    s_bid,
    output reg  [1:0]         s_bresp,
    output reg                s_bvalid,
    input  wire               s_bready,
    input  wire [ID_W-1:0]    s_arid,
    input  wire [ADDR_W-1:0]  s_araddr,
    input  wire [7:0]         s_arlen,
    input  wire               s_arvalid,
    output reg                s_arready,
    output reg  [ID_W-1:0]    s_rid,
    output reg  [63:0]        s_rdata,
    output reg  [1:0]         s_rresp,
    output reg                s_rlast,
    output reg                s_rvalid,
    input  wire               s_rready,

    // ---- AXI4 master: executes the program ------------------------------
    output reg  [ID_W-1:0]    m_awid,
    output reg  [ADDR_W-1:0]  m_awaddr,
    output wire [7:0]         m_awlen,
    output reg                m_awvalid,
    input  wire               m_awready,
    output reg  [63:0]        m_wdata,
    output wire [7:0]         m_wstrb,
    output wire               m_wlast,
    output reg                m_wvalid,
    input  wire               m_wready,
    input  wire [ID_W-1:0]    m_bid,
    input  wire [1:0]         m_bresp,
    input  wire               m_bvalid,
    output wire               m_bready,
    output reg  [ID_W-1:0]    m_arid,
    output reg  [ADDR_W-1:0]  m_araddr,
    output wire [7:0]         m_arlen,
    output reg                m_arvalid,
    input  wire               m_arready,
    input  wire [ID_W-1:0]    m_rid,
    input  wire [63:0]        m_rdata,
    input  wire [1:0]         m_rresp,
    input  wire               m_rlast,
    input  wire               m_rvalid,
    output wire               m_rready,

    output wire               irq
);
    // ---- register map (slave side) ---------------------------------------
    // A_CTRL write: [0] GO, a one-cycle pulse. There is NO abort -- the write
    // decode below acts on bit 0 and nothing else, so a running program can
    // only be stopped by reset. See docs/isa/orchestrator.md.
    localparam [15:0] A_CTRL   = 16'h0000,   // read: [0] busy [1] done [2] err
                      A_PC     = 16'h0008,   // read: current command index
                      A_CODE   = 16'h0010,   // read: DONE code
                      A_POLLS  = 16'h0018,   // read: polls executed (debug)
                      A_CMD    = 16'h1000;   // command RAM, 4 words per command

    localparam [3:0] OP_WR = 4'd1, OP_POLL = 4'd2, OP_DONE = 4'd3;

    // one command = { op, addr, data, mask }
    reg [3:0]        c_op   [0:NCMD-1];
    reg [ADDR_W-1:0] c_addr [0:NCMD-1];
    reg [63:0]       c_data [0:NCMD-1];
    reg [63:0]       c_mask [0:NCMD-1];

    localparam integer PCW = $clog2(NCMD);

    reg [PCW-1:0] pc;
    reg           busy, done, err;
    reg [63:0]    code;
    reg [31:0]    npolls;
    reg           go;

    assign m_awlen  = 8'd0;
    assign m_arlen  = 8'd0;
    assign m_wstrb  = 8'hFF;
    assign m_wlast  = 1'b1;
    assign m_bready = 1'b1;
    assign m_rready = 1'b1;
    assign irq      = done;

    // =====================================================================
    // Slave: command load and status
    // =====================================================================
    reg [ADDR_W-1:0] waddr, raddr;
    reg [1:0] wst;
    localparam [1:0] W_IDLE = 2'd0, W_DATA = 2'd1, W_RESP = 2'd2;

    // A command is 4 x 64-bit words = 32 bytes, so the field selector is
    // waddr[4:3] and the index starts at bit 5. NCMD*32 must fit inside the
    // 0x1000..0x1FFF window that w_is_cmd decodes -- hence NCMD <= 128.
    wire        w_is_cmd  = (waddr[15:12] == 4'h1);
    wire [PCW-1:0] w_cmd_idx = waddr[5 +: PCW];
    wire [1:0]  w_cmd_fld  = waddr[4:3];

    integer ci;
    always @(posedge clk) begin
        if (!resetn) begin
            wst <= W_IDLE; s_awready <= 1'b1; s_wready <= 1'b0;
            s_bvalid <= 1'b0; s_bid <= {ID_W{1'b0}}; s_bresp <= 2'b00;
            waddr <= {ADDR_W{1'b0}}; go <= 1'b0;
            for (ci = 0; ci < NCMD; ci = ci + 1) begin
                c_op[ci] <= 4'd0;
            end
        end else begin
            go <= 1'b0;
            case (wst)
                W_IDLE: if (s_awvalid && s_awready) begin
                    waddr <= s_awaddr; s_bid <= s_awid;
                    s_awready <= 1'b0; s_wready <= 1'b1; wst <= W_DATA;
                end
                W_DATA: if (s_wvalid) begin
                    if (w_is_cmd) begin
                        case (w_cmd_fld)
                            2'd0: c_op[w_cmd_idx]   <= s_wdata[3:0];
                            2'd1: c_addr[w_cmd_idx] <= s_wdata[ADDR_W-1:0];
                            2'd2: c_data[w_cmd_idx] <= s_wdata;
                            2'd3: c_mask[w_cmd_idx] <= s_wdata;
                        endcase
                    end else begin
                        case (waddr[15:0])
                            A_CTRL: if (s_wdata[0]) begin
                                go <= 1'b1;
                            end
                            default: ;
                        endcase
                    end
                    s_wready <= 1'b0; s_bresp <= 2'b00; s_bvalid <= 1'b1;
                    wst <= W_RESP;
                end
                W_RESP: if (s_bready) begin
                    s_bvalid <= 1'b0; s_awready <= 1'b1; wst <= W_IDLE;
                end
                default: wst <= W_IDLE;
            endcase
        end
    end

    always @(posedge clk) begin
        if (!resetn) begin
            // s_rdata/s_rid/s_rresp are the read response `s_rvalid` qualifies;
            // raddr is loaded from s_araddr on the accepted AR.
            s_arready <= 1'b1; s_rvalid <= 1'b0; s_rlast <= 1'b0;
        end else begin
            if (s_arvalid && s_arready) begin
                raddr <= s_araddr; s_rid <= s_arid;
                s_arready <= 1'b0; s_rlast <= 1'b1; s_rvalid <= 1'b1;
                case (s_araddr[15:0])
                    A_CTRL:  s_rdata <= {61'd0, err, done, busy};
                    A_PC:    s_rdata <= {{(64-PCW){1'b0}}, pc};
                    A_CODE:  s_rdata <= code;
                    A_POLLS: s_rdata <= {32'd0, npolls};
                    default: s_rdata <= 64'd0;
                endcase
            end else if (s_rvalid && s_rready) begin
                s_rvalid <= 1'b0; s_rlast <= 1'b0; s_arready <= 1'b1;
            end
        end
    end

    // =====================================================================
    // Master: execute the program
    // =====================================================================
    reg [2:0] est;
    localparam [2:0] E_IDLE = 3'd0, E_FETCH = 3'd1, E_WR_AW = 3'd2;
    localparam [2:0] E_WR_B = 3'd3, E_POLL_AR = 3'd4, E_POLL_R = 3'd5;
    localparam [2:0] E_POLL_W = 3'd6;

    // Cycles to wait before re-reading a POLL address that did not match.
    // A poll is a spin on a memory-mapped register, and the register does not
    // change any sooner for being read more often -- but every read is a real
    // AXI transaction crossing the same fabric the clusters use to fetch
    // operands, so retrying flat out steals bandwidth from the work being
    // waited on. A GEMM waits thousands of cycles, so an interval costs
    // nothing to notice completion and removes ~97% of the traffic.
    reg [15:0] poll_wait;

    reg [3:0]        cur_op;
    reg [ADDR_W-1:0] cur_addr;
    reg [63:0]       cur_data, cur_mask;

    always @(posedge clk) begin
        if (!resetn) begin
            est <= E_IDLE; pc <= {PCW{1'b0}};
            busy <= 1'b0; done <= 1'b0; err <= 1'b0;
            code <= 64'd0; npolls <= 32'd0; poll_wait <= 16'd0;
            // The AXI payloads are qualified by the three valids above, and
            // cur_* is the decoded command `est` only reads once it has one.
            m_awvalid <= 1'b0; m_wvalid <= 1'b0; m_arvalid <= 1'b0;
        end else begin
            if (m_awvalid && m_awready) begin
                m_awvalid <= 1'b0;
            end
            if (m_wvalid  && m_wready) begin
                m_wvalid  <= 1'b0;
            end
            if (m_arvalid && m_arready) begin
                m_arvalid <= 1'b0;
            end

            case (est)
                E_IDLE: if (go) begin
                    pc <= {PCW{1'b0}}; busy <= 1'b1; done <= 1'b0; err <= 1'b0;
                    npolls <= 32'd0;
                    est <= E_FETCH;
                end

                E_FETCH: begin
                    cur_op   <= c_op[pc];
                    cur_addr <= c_addr[pc];
                    cur_data <= c_data[pc];
                    cur_mask <= c_mask[pc];
                    case (c_op[pc])
                        OP_WR:   est <= E_WR_AW;
                        OP_POLL: est <= E_POLL_AR;
                        OP_DONE: begin
                            code <= c_data[pc];
                            busy <= 1'b0; done <= 1'b1;
                            est  <= E_IDLE;
                        end
                        default: begin          // unknown opcode: stop, flag it
                            err  <= 1'b1; busy <= 1'b0; done <= 1'b1;
                            est  <= E_IDLE;
                        end
                    endcase
                end

                E_WR_AW: begin
                    m_awaddr  <= cur_addr;
                    m_awid    <= {ID_W{1'b0}};
                    m_awvalid <= 1'b1;
                    m_wdata   <= cur_data;
                    m_wvalid  <= 1'b1;
                    est <= E_WR_B;
                end
                E_WR_B: if (m_bvalid) begin
                    pc  <= pc + 1'b1;
                    est <= E_FETCH;
                end

                E_POLL_AR: begin
                    m_araddr  <= cur_addr;
                    m_arid    <= {ID_W{1'b0}};
                    m_arvalid <= 1'b1;
                    est <= E_POLL_R;
                end
                E_POLL_R: if (m_rvalid) begin
                    npolls <= npolls + 32'd1;
                    if ((m_rdata & cur_mask) == cur_data) begin
                        pc  <= pc + 1'b1;
                        est <= E_FETCH;
                    end else begin
                        poll_wait <= POLL_IVL;  // back off, then retry
                        est <= E_POLL_W;
                    end
                end

                E_POLL_W: if (poll_wait == 16'd0) begin
                    est <= E_POLL_AR;
                end
                          else begin
                              poll_wait <= poll_wait - 16'd1;
                          end

                default: est <= E_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
