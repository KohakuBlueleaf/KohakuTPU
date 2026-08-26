// rv64_noc_mbox -- the system core's way onto the mesh: dispatch out,
// completions in. SysCore drops the compute-unit shell by decision, and that
// dropped its only path onto the fabric with it.
//
// SOFTWARE WRITES A DISPATCH, NOT A FLIT. A flit is 288 bits against a 64-bit
// store port, so composing one in software is five stores with a tearing
// window in the middle.

`default_nettype none

module rv64_noc_mbox #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer CQ_DEPTH   = 16
)(
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [POS_WIDTH-1:0]   my_x,
    input  wire [POS_WIDTH-1:0]   my_y,

    // ---- the control-region window ----
    input  wire                   cfg_en,
    input  wire [2:0]             cfg_addr,
    input  wire [63:0]            cfg_data,
    input  wire [2:0]             rd_addr,
    output reg  [63:0]            rd_data,

    // ---- flits, as a client of the node's hub ----
    output reg  [FLIT_WIDTH-1:0]  tx_data,
    output reg                    tx_valid,
    input  wire                   tx_busy,
    input  wire [FLIT_WIDTH-1:0]  rx_data,
    input  wire                   rx_valid,
    output wire                   rx_busy,

    output wire                   cq_nonempty
);
    localparam [3:0] T_CU_INST   = 4'h5;
    localparam [3:0] T_CU_SIGNAL = 4'h6;

    localparam integer PAY_W = FLIT_WIDTH - 4*POS_WIDTH - 16;

    // POPPING IS A WRITE, not a side effect of the read. The control region
    // answers reads from a register a cycle later, so a read-triggered pop
    // would have to guess which cycle the read really happened on.
    localparam [2:0] R_DST = 3'd0, R_ARG0 = 3'd1, R_ARG1 = 3'd2;
    localparam [2:0] R_GO  = 3'd3, R_STAT = 3'd4, R_HEAD = 3'd5;
    localparam [2:0] R_POP = 3'd6;

    reg [POS_WIDTH-1:0] dst_x, dst_y;
    reg [63:0]          arg0, arg1;
    reg [7:0]           txn;

    // ---- outbound ----------------------------------------------------------
    wire [PAY_W-1:0] payload = {{(PAY_W-128){1'b0}}, arg1, arg0};

    always @(posedge clk) begin
        // RESET THE CONTROL, NOT THE DATA. `dst`, `arg0` and `arg1` are written
        // before they are read and a reset value costs a control set on 136
        // bits of register for nothing.
        if (!resetn) begin
            tx_valid <= 1'b0;
            txn      <= 8'd0;
        end
        else begin
            // HOLD UNTIL TAKEN. Withdrawing an offered flit destroys it, and
            // the loss is silent at every point downstream.
            if (tx_valid && !tx_busy) begin
                tx_valid <= 1'b0;
            end

            if (cfg_en) begin
                case (cfg_addr)
                    R_DST: begin
                        dst_x <= cfg_data[POS_WIDTH-1:0];
                        dst_y <= cfg_data[8 +: POS_WIDTH];
                    end
                    R_ARG0: arg0 <= cfg_data;
                    R_ARG1: arg1 <= cfg_data;
                    R_GO: if (!tx_valid) begin
                        tx_data  <= {dst_x, dst_y, my_x, my_y,
                                     T_CU_INST, txn, 1'b1, 3'b000, payload};
                        tx_valid <= 1'b1;
                        txn      <= txn + 8'd1;
                    end
                    default: ;
                endcase
            end
        end
    end

    // ---- inbound completions ----------------------------------------------
    localparam integer CQ_AW = $clog2(CQ_DEPTH);

    reg [63:0]      cq [0:CQ_DEPTH-1];
    reg [CQ_AW:0]   cq_wr, cq_rd;
    wire [CQ_AW:0]  cq_used  = cq_wr - cq_rd;
    wire            cq_full  = (cq_used == CQ_DEPTH[CQ_AW:0]);
    assign cq_nonempty = (cq_wr != cq_rd);

    // A flit the queue cannot take must still be ACCEPTED and dropped, never
    // held: held, it sits at the head of the hub's queue and stalls the link
    // for everything behind it, including the traffic that would drain us.
    assign rx_busy = 1'b0;

    wire [3:0] rx_type = rx_data[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire [POS_WIDTH-1:0] rx_sx = rx_data[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] rx_sy = rx_data[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
    wire [7:0]  rx_code = rx_data[7:0];
    wire [31:0] rx_arg  = rx_data[39:8];

    reg cq_ovf;

    always @(posedge clk) begin
        if (!resetn) begin
            cq_wr  <= {(CQ_AW+1){1'b0}};
            cq_rd  <= {(CQ_AW+1){1'b0}};
            cq_ovf <= 1'b0;
        end
        else begin
            if (rx_valid && (rx_type == T_CU_SIGNAL)) begin
                if (!cq_full) begin
                    cq[cq_wr[CQ_AW-1:0]] <= {
                        8'd0,               // [63:56]
                        rx_sy,              // [55:52] source y
                        rx_sx,              // [51:48] source x
                        rx_code,            // [47:40] completion code
                        rx_arg,             // [39:8]  argument
                        {(64-8-2*POS_WIDTH-8-32){1'b0}}
                    };
                    cq_wr <= cq_wr + 1'b1;
                end
                else begin
                    // STICKY, because a dropped completion and a unit that
                    // never finished look identical from software.
                    cq_ovf <= 1'b1;
                end
            end
            if (cfg_en && (cfg_addr == R_POP) && cq_nonempty) begin
                cq_rd <= cq_rd + 1'b1;
            end
        end
    end

    always @(*) begin
        case (rd_addr)
            R_DST:  rd_data = {{(56-POS_WIDTH){1'b0}}, dst_y,
                               {(8-POS_WIDTH){1'b0}}, dst_x};
            R_ARG0: rd_data = arg0;
            R_ARG1: rd_data = arg1;
            R_STAT: rd_data = {32'd0, cq_ovf, 15'd0,
                               tx_valid, 7'd0, {(8-CQ_AW-1){1'b0}}, cq_used};
            R_HEAD: rd_data = cq_nonempty ? cq[cq_rd[CQ_AW-1:0]] : 64'd0;
            default: rd_data = 64'd0;
        endcase
    end

endmodule

`default_nettype wire
