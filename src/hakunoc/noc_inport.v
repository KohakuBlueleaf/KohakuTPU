module InPortSwitch #(
    parameter DATA_WIDTH = 288,
    parameter FIFO_DEPTH = 4096,
    parameter POS_WIDTH = 4,
    parameter POS_X = 0,
    parameter POS_Y = 0
)(
    input clk,
    input rst,
    
    // In Port Signals
    input wire [DATA_WIDTH-1:0] data_in,
    input wire data_valid,
    output wire port_busy,
    
    // NoC Router Signals, 5ports
    output reg [4:0][DATA_WIDTH-1:0] port_out,
    output reg [4:0] port_valid,
    input wire [4:0] clear
);
    reg next_data;
    wire [DATA_WIDTH-1:0] rd_data, current_data;
    wire rd_busy;
    uram_fifo #(
        .DATA_WIDTH(DATA_WIDTH),
        .FIFO_DEPTH(FIFO_DEPTH)
    ) inport_fifo (
        .clk(clk),
        .rst(rst),
        .wr_en(data_valid),
        .wr_data(data_in),
        .wr_busy(port_busy),
        .rd_en(next_data),
        .rd_data(rd_data),
        .rd_busy(rd_busy)
    );

    /*
        5 port:
        0: North  --  pos_y < POS_Y
        1: East   --  pos_x > POS_X
        2: South  --  pos_y > POS_Y
        3: West   --  pos_x < POS_X
        4: Local  --  pos_x == POS_X && pos_y == POS_Y
        if both pos_x,y are different from POS_X,Y, we choose direction based on the distance
        larger distance axis is chosen
    */
    reg state, use_cache;
    reg [DATA_WIDTH-1:0] cache;
    reg [4:0] direction_rr;

    assign current_data = use_cache ? cache : rd_data;

    wire [POS_WIDTH-1:0] pos_x = current_data[DATA_WIDTH-1:DATA_WIDTH-POS_WIDTH];
    wire [POS_WIDTH-1:0] pos_y = current_data[DATA_WIDTH-POS_WIDTH-1:DATA_WIDTH-POS_WIDTH*2];
    wire [4:0] port_choice = {
        (pos_x == POS_X && pos_y == POS_Y) ? 1'b1 : 1'b0, //local
        (pos_x < POS_X) ? 1'b1 : 1'b0, //west
        (pos_y > POS_Y) ? 1'b1 : 1'b0, //south
        (pos_x > POS_X) ? 1'b1 : 1'b0, //east
        (pos_y < POS_Y) ? 1'b1 : 1'b0  //north
    };
    wire [4:0] real_port_valid = port_valid & ~clear;
    wire [4:0] avail_port = port_choice & ~real_port_valid;
    wire [4:0] masked_avail_port = avail_port & direction_rr;

    always @(posedge clk, posedge rst) begin
        if (rst) begin
            state <= 1'b0;
            direction_rr <= 5'b10101;
            next_data <= 1'b0;
            port_valid <= 0;
            use_cache <= 1'b0;
            port_out <= 0;
            cache <= 0;
        end else begin
            direction_rr <= ~direction_rr;
            if (~rd_busy | use_cache) begin //FIFO have data
                if(masked_avail_port != 5'b00000) begin //multiple port is not used
                    if(state == 1'b1 | use_cache) begin //first data
                        case (masked_avail_port)
                            5'b00001: port_out[0] <= current_data; //north
                            5'b00010: port_out[1] <= current_data; //east
                            5'b00100: port_out[2] <= current_data; //south
                            5'b01000: port_out[3] <= current_data; //west
                            5'b10000: port_out[4] <= current_data; //local
                        endcase
                        port_valid <= real_port_valid | masked_avail_port;
                        if(use_cache) begin
                            use_cache <= 1'b0;
                            next_data <= 1'b0;
                            state <= 1'b0;
                        end else begin
                            next_data <= 1'b1;
                            state <= 1'b1;
                        end
                    end else begin
                        use_cache <= 1'b0;
                        port_valid <= port_valid ^ clear;
                        next_data <= 1'b1;
                        state <= 1'b1;
                    end 
                end else if(avail_port != 5'b00000) begin //port is not used
                    if(state == 1'b1 | use_cache) begin //first data
                        case (avail_port)
                            5'b00001: port_out[0] <= current_data; //north
                            5'b00010: port_out[1] <= current_data; //east
                            5'b00100: port_out[2] <= current_data; //south
                            5'b01000: port_out[3] <= current_data; //west
                            5'b10000: port_out[4] <= current_data; //local
                        endcase
                        port_valid <= real_port_valid | avail_port;
                        if(use_cache) begin
                            use_cache <= 1'b0;
                            next_data <= 1'b0;
                            state <= 1'b0;
                        end else begin
                            next_data <= 1'b1;
                            state <= 1'b1;
                        end
                    end else begin
                        use_cache <= 1'b0;
                        port_valid <= port_valid ^ clear;
                        next_data <= 1'b1;
                        state <= 1'b1;
                    end 
                end else begin
                    if(next_data) begin
                        cache <= rd_data;
                        use_cache <= 1'b1;
                    end else begin
                        use_cache <= use_cache;
                        cache <= cache;
                    end
                    state <= 1'b0;
                    port_valid <= port_valid ^ clear;
                    next_data <= 1'b0;
                end
            end else begin // FIFO is empty and no cache, do nothing
                use_cache <= 1'b0;
                cache <= cache;
                state <= 1'b0;
                port_valid <= port_valid ^ clear;
                next_data <= 1'b0;
            end
        end
    end
endmodule