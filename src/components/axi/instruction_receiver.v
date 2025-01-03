module InstReceiver #(
    parameter INSTRUCTION_DEPTH = 16,
    parameter DATA_WIDTH = 64,
    parameter ADDR_WIDTH = 64,
    parameter STRB_WIDTH = DATA_WIDTH/8,
    parameter ID_WIDTH = 4
) (
    input clk,
    input rst,

    // AXI4-full AW channel
    input [ADDR_WIDTH-1:0] S_AXI_AWADDR,
    input [ID_WIDTH-1:0] S_AXI_AWID,
    input [7:0] S_AXI_AWLEN,
    input [2:0] S_AXI_AWSIZE,
    input [1:0] S_AXI_AWBURST,
    input S_AXI_AWVALID,
    output S_AXI_AWREADY,

    // AXI4-full W channel
    input [DATA_WIDTH-1:0] S_AXI_WDATA,
    input [DATA_WIDTH/8-1:0] S_AXI_WSTRB,
    input S_AXI_WLAST,
    input S_AXI_WVALID,
    output S_AXI_WREADY,

    // AXI4-full B channel
    output [ID_WIDTH-1:0] S_AXI_BID,
    output [1:0] S_AXI_BRESP,
    output S_AXI_BVALID,
    input S_AXI_BREADY,

    // AXI4-full AR channel
    input [ADDR_WIDTH-1:0] S_AXI_ARADDR,
    input [ID_WIDTH-1:0] S_AXI_ARID,
    input [7:0] S_AXI_ARLEN,
    input [2:0] S_AXI_ARSIZE,
    input [1:0] S_AXI_ARBURST,
    input S_AXI_ARVALID,
    output S_AXI_ARREADY,

    // AXI4-full R channel
    output [ID_WIDTH-1:0] S_AXI_RID,
    output [DATA_WIDTH-1:0] S_AXI_RDATA,
    output [1:0] S_AXI_RRESP,
    output S_AXI_RLAST,
    output S_AXI_RVALID,
    input S_AXI_RREADY,

    //Instruction Output Channel
    output reg [DATA_WIDTH-1:0] instruction,
    output reg [$clog2(INSTRUCTION_DEPTH)-1:0] instruction_id,
    output reg instruction_valid,
    input instruction_next,

    //Processing State
    input [DATA_WIDTH-1:0] data,
    input [$clog2(INSTRUCTION_DEPTH)-1:0] data_id,
    input data_valid
);
    // RAM part for instruction read/write
    reg [$clog2(INSTRUCTION_DEPTH)-1:0] inst_out_addr_reg;
    reg [INSTRUCTION_DEPTH-1:0] instruction_valid_reg;
    reg [DATA_WIDTH-1:0] instruction_reg [INSTRUCTION_DEPTH-1:0];
    reg [DATA_WIDTH-1:0] data_reg [INSTRUCTION_DEPTH-1:0];
    reg [INSTRUCTION_DEPTH-1:0] data_valid_reg;
    reg [DATA_WIDTH-1:0] data_read;
    reg data_next, data_read_valid;

    always @(posedge clk) begin
        if (rst) begin
            inst_out_addr_reg <= 0;
            instruction_valid_reg <= 0;
            data_valid_reg <= 0;
            data_read <= 0;
            data_read_valid <= 0;
        end else begin
            if (data_valid) begin
                data_reg[data_id] <= data;
                data_valid_reg[data_id] <= 1;
            end
            
            if (data_next) begin
                if(data_valid_reg[read_inst_addr]) begin
                    data_read <= data_reg[read_inst_addr];
                    data_read_valid <= 1;
                end else begin
                    data_read <= 0;
                    data_read_valid <= 0;
                end
            end else begin
                data_read <= data_read;
                data_read_valid <= 0;
            end
            
            if (instruction_next) begin
                if(instruction_valid_reg[inst_out_addr_reg]) begin
                    instruction <= instruction_reg[inst_out_addr_reg];
                    instruction_valid_reg[inst_out_addr_reg] <= 0;
                    instruction_id <= inst_out_addr_reg;
                    instruction_valid <= 1'b1;
                    inst_out_addr_reg <= inst_out_addr_reg + 1;
                end else begin
                    instruction <= 0;
                    instruction_id <= 0;
                    instruction_valid <= 1'b0;
                end
            end else if (~instruction_valid & instruction_valid_reg[inst_out_addr_reg]) begin
                instruction <= instruction_reg[inst_out_addr_reg];
                instruction_valid_reg[inst_out_addr_reg] <= 0;
                instruction_id <= inst_out_addr_reg;
                instruction_valid <= 1'b1;
                inst_out_addr_reg <= inst_out_addr_reg + 1;
            end else begin
                instruction <= instruction;
                instruction_id <= instruction_id;
                instruction_valid <= instruction_valid;
                inst_out_addr_reg <= inst_out_addr_reg;
            end
        end
    end

    parameter VALID_ADDR_OFFSET = $clog2(STRB_WIDTH);
    parameter WORD_WIDTH = STRB_WIDTH;
    parameter WORD_SIZE = DATA_WIDTH/WORD_WIDTH;

    localparam [1:0]
        WRITE_WAIT = 2'b00,
        WRITE_BURSTING = 2'b01,
        WRITE_LAST = 2'b10,
        WRITE_DONE = 2'b11;
    reg write_state_reg=WRITE_WAIT, write_state_next;

    reg [ID_WIDTH-1:0] write_id_reg={ID_WIDTH{1'b0}}, write_id_next;
    reg [ADDR_WIDTH-1:0] write_addr_reg={ADDR_WIDTH{1'b0}}, write_addr_next;
    reg write_addr_valid_reg=1'b0, write_addr_valid_next;
    reg write_last_reg=1'b0, write_last_next;
    reg [7:0] write_count_reg=8'h0, write_count_next;
    reg [2:0] write_size_reg=3'b0, write_size_next;
    reg [1:0] write_burst_reg=2'b0, write_burst_next;
    wire [$clog2(INSTRUCTION_DEPTH)-1:0] write_inst_addr;
    assign write_inst_addr = write_addr_reg[$clog2(INSTRUCTION_DEPTH)-1+VALID_ADDR_OFFSET:VALID_ADDR_OFFSET];

    reg write_valid;

    reg s_axi_awready_reg=1'b0, s_axi_awready_next;
    reg [ID_WIDTH-1:0] s_axi_bid_reg={ID_WIDTH{1'b0}}, s_axi_bid_next;
    reg s_axi_bvalid_reg=1'b0, s_axi_bvalid_next;

    assign S_AXI_AWREADY = s_axi_awready_reg;
    assign S_AXI_WREADY = write_addr_valid_reg;
    assign S_AXI_BID = s_axi_bid_reg;
    assign S_AXI_BRESP = 2'b00;
    assign S_AXI_BVALID = s_axi_bvalid_reg;

    always @* begin
        write_state_next = WRITE_WAIT;

        write_id_next = write_id_reg;
        write_addr_next = write_addr_reg;
        write_addr_valid_next = write_addr_valid_reg;
        write_last_next = write_last_reg;
        write_count_next = write_count_reg;
        write_size_next = write_size_reg;
        write_burst_next = write_burst_reg;

        s_axi_awready_next = 1'b0;
        s_axi_bid_next = s_axi_bid_reg;
        s_axi_bvalid_next = s_axi_bvalid_reg && !S_AXI_BREADY;

        case (write_state_reg)
            WRITE_WAIT: begin
                s_axi_awready_next = 1'b1;
                write_valid = 1'b0;

                if (S_AXI_AWREADY && S_AXI_AWVALID) begin
                    write_id_next = S_AXI_AWID;
                    write_addr_next = S_AXI_AWADDR;
                    write_count_next = S_AXI_AWLEN;
                    write_size_next = S_AXI_AWSIZE < $clog2(STRB_WIDTH) ? S_AXI_AWSIZE : $clog2(STRB_WIDTH);
                    write_burst_next = S_AXI_AWBURST;

                    write_addr_valid_next = 1'b1;
                    s_axi_awready_next = 1'b0;
                    if (write_count_reg > 0) begin
                        write_last_next = 1'b0;
                    end else begin
                        write_last_next = 1'b1;
                    end
                    write_state_next = WRITE_BURSTING;
                end else begin
                    write_state_next = WRITE_WAIT;
                end
            end
            WRITE_BURSTING: begin
                if (S_AXI_WREADY && S_AXI_WVALID) begin
                    write_valid = 1'b1;
                    if (write_burst_reg != 2'b00) begin
                        write_addr_next = write_addr_reg + (1 << write_size_reg);
                    end
                    write_count_next = write_count_reg - 1;
                    write_last_next = write_count_next == 0;
                    if (write_count_reg > 0) begin
                        write_addr_valid_next = 1'b1;
                        write_state_next = WRITE_BURSTING;
                    end else begin
                        write_addr_valid_next = 1'b0;
                        if (S_AXI_BREADY || !S_AXI_BVALID) begin
                            s_axi_bid_next = write_id_reg;
                            s_axi_bvalid_next = 1'b1;
                            s_axi_awready_next = 1'b1;
                            write_state_next = WRITE_WAIT;
                        end else begin
                            write_state_next = WRITE_LAST;
                        end
                    end
                end else begin
                    write_valid = 1'b0;
                    write_state_next = WRITE_BURSTING;
                end
            end
            WRITE_LAST: begin
                write_valid = 1'b0;
                if (S_AXI_BREADY || !S_AXI_BVALID) begin
                    s_axi_bid_next = write_id_reg;
                    s_axi_bvalid_next = 1'b1;
                    s_axi_awready_next = 1'b1;
                    write_state_next = WRITE_WAIT;
                end else begin
                    write_state_next = WRITE_LAST;
                end
            end
        endcase
    end

    always @(posedge clk) begin
        write_state_reg <= write_state_next;
        write_id_reg <= write_id_next;
        write_addr_reg <= write_addr_next;
        write_addr_valid_reg <= write_addr_valid_next;
        write_last_reg <= write_last_next;
        write_count_reg <= write_count_next;
        write_size_reg <= write_size_next;
        write_burst_reg <= write_burst_next;

        s_axi_awready_reg <= s_axi_awready_next;
        s_axi_bid_reg <= s_axi_bid_next;
        s_axi_bvalid_reg <= s_axi_bvalid_next;

        if(write_valid) begin
            instruction_reg[write_inst_addr] <= S_AXI_WDATA;
            instruction_valid_reg[write_inst_addr] <= 1;
            data_valid_reg[write_inst_addr] <= 0;
        end

        if (rst) begin
            write_state_reg <= WRITE_WAIT;
            write_addr_valid_reg <= 1'b0;
            s_axi_awready_reg <= 1'b0;
            s_axi_bvalid_reg <= 1'b0;
        end
    end

    localparam [1:0]
        READ_WAIT = 2'b00,
        READ_BURSTING = 2'b01,
        READ_LAST = 2'b10,
        READ_DONE = 2'b11;
    reg [1:0] read_state_reg=READ_WAIT, read_state_next;

    reg [ID_WIDTH-1:0] read_id_reg={ID_WIDTH{1'b0}}, read_id_next;
    reg [ADDR_WIDTH-1:0] read_addr_reg={ADDR_WIDTH{1'b0}}, read_addr_next;
    reg read_addr_valid_reg=1'b0, read_addr_valid_next;
    reg read_last_reg=1'b0, read_last_next;
    reg [7:0] read_count_reg=8'h0, read_count_next;
    reg [2:0] read_size_reg=3'b0, read_size_next;
    reg [1:0] read_burst_reg=2'b0, read_burst_next;
    wire [$clog2(INSTRUCTION_DEPTH)-1:0] read_inst_addr;
    assign read_inst_addr = read_addr_reg[$clog2(INSTRUCTION_DEPTH)-1+VALID_ADDR_OFFSET:VALID_ADDR_OFFSET];

    reg data_next_next;
    reg s_axi_arready_reg=1'b0, s_axi_arready_next;
    assign S_AXI_RVALID = S_AXI_RREADY && data_read_valid;
    assign S_AXI_ARREADY = s_axi_arready_reg;
    assign S_AXI_RDATA = data_read;
    assign S_AXI_RLAST = S_AXI_RVALID && read_count_next <= 0;
    assign S_AXI_RID = read_id_reg;
    assign S_AXI_RRESP = 2'b00;

    always @* begin
        read_state_next = READ_WAIT;
        read_id_next = read_id_reg;
        read_addr_next = read_addr_reg;
        read_addr_valid_next = read_addr_valid_reg;
        read_last_next = read_last_reg;
        read_count_next = read_count_reg;
        read_size_next = read_size_reg;
        read_burst_next = read_burst_reg;
        data_next_next = 1'b0;
        s_axi_arready_next = 1'b0;

        case (read_state_reg)
            READ_WAIT: begin
                s_axi_arready_next = 1'b1;
                if (S_AXI_ARREADY && S_AXI_ARVALID) begin
                    read_id_next = S_AXI_ARID;
                    read_addr_next = S_AXI_ARADDR;
                    read_count_next = S_AXI_ARLEN;
                    read_size_next = S_AXI_ARSIZE;
                    read_burst_next = S_AXI_ARBURST;
                    read_addr_valid_next = 1'b1;
                    if (read_count_next-1 > 0) begin
                        read_last_next = 1'b0;
                    end else begin
                        read_last_next = 1'b1;
                    end
                    read_state_next = READ_BURSTING;
                    data_next_next = 1'b1;
                end else begin
                    read_state_next = READ_WAIT;
                end
            end
            READ_BURSTING: begin
                if (S_AXI_RREADY && S_AXI_RVALID) begin
                    read_addr_next = read_addr_reg + (1 << read_size_reg);
                    read_count_next = read_count_reg - 1;
                    read_last_next = read_count_next <=0;
                    if (read_count_next > 0) begin
                        read_addr_valid_next = 1'b1;
                        read_state_next = READ_BURSTING;
                        data_next_next = 1'b1;
                    end else begin
                        read_addr_valid_next = 1'b0;
                        data_next_next = 1'b0;
                        read_state_next = READ_LAST;
                    end
                end else begin
                    read_state_next = READ_BURSTING;
                end
            end
        endcase
    end

    always @(posedge clk) begin
        if (rst) begin
            read_state_reg <= READ_WAIT;
            read_addr_valid_reg <= 1'b0;
            s_axi_arready_reg <= 1'b0;
        end else begin
            read_state_reg <= read_state_next;
            read_id_reg <= read_id_next;
            read_addr_reg <= read_addr_next;
            read_addr_valid_reg <= read_addr_valid_next;
            read_last_reg <= read_last_next;
            read_count_reg <= read_count_next;
            read_size_reg <= read_size_next;
            read_burst_reg <= read_burst_next;
            data_next <= data_next_next;
            s_axi_arready_reg <= s_axi_arready_next;
        end
    end
endmodule