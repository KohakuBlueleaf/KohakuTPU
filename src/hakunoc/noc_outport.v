module OutPortSwitch #(
    parameter DATA_WIDTH = 288
)(
    input clk,
    input rst,
    
    // In Port Signals
    input wire  [4:0][DATA_WIDTH-1:0] in_ports,
    input wire  [4:0] ports_valid,
    output reg  [4:0] ports_clear,
    
    // Output Signals
    output reg [DATA_WIDTH-1:0] port_out,
    output reg out_valid,
    input wire busy
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
    wire [4:0] current_valid = ports_valid & ~ports_clear;
    reg [4:0] port_selection;
    reg [2:0] port_rr;
    wire [2:0] pr1, pr2, pr3, pr4, pr5;
    assign pr1 = port_rr;
    assign pr2 = (port_rr+1)%5;
    assign pr3 = (port_rr+2)%5;
    assign pr4 = (port_rr+3)%5;
    assign pr5 = (port_rr+4)%5;
    
    always @(*) begin
        port_selection = 5'b0;
        if (current_valid[pr1]) begin
            port_selection[pr1] = 1'b1;
        end else if (current_valid[pr2]) begin
            port_selection[pr2] = 1'b1;
        end else if (current_valid[pr3]) begin
            port_selection[pr3] = 1'b1;
        end else if (current_valid[pr4]) begin
            port_selection[pr4] = 1'b1;
        end else if (current_valid[pr5]) begin
            port_selection[pr5] = 1'b1;
        end
    end
    
    always @(posedge clk or posedge rst) begin
        if (rst) begin
            port_rr <= 3'd0;
            port_out <= 288'd0;
            out_valid <= 1'b0;
            ports_clear <= 5'b00000;
        end else begin
            port_rr <= pr2;
            if(~busy) begin
                case (port_selection)
                    5'b00001: port_out <= in_ports[0];
                    5'b00010: port_out <= in_ports[1];
                    5'b00100: port_out <= in_ports[2];
                    5'b01000: port_out <= in_ports[3];
                    5'b10000: port_out <= in_ports[4];
                endcase
                out_valid <= port_selection != 5'b00000;
                ports_clear <= port_selection;
            end else begin
                out_valid <= 1'b0;
                ports_clear <= 5'b00000;
            end
        end
    end
endmodule