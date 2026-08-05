module uram_fifo_tb();

    // Parameters
    localparam DATA_WIDTH = 16;  // Using smaller width for easier testing
    localparam FIFO_DEPTH = 18;  // Using smaller depth for simulation
    localparam CLK_PERIOD = 10;  // 100MHz clock
    localparam READ_LATENCY = 2;  // Read latency of 2 cycle
    
    // Signals
    reg                     clk;
    reg                     rst;
    reg                     wr_en;
    reg  [DATA_WIDTH-1:0]   wr_data;
    wire                    wr_busy;
    reg                     rd_en;
    wire [DATA_WIDTH-1:0]   rd_data;
    wire                    rd_busy;
    
    // Test variables
    integer i;
    integer write_count = 0;
    integer read_count = 0;
    integer error_count = 0;
    reg [DATA_WIDTH-1:0] expected_data;
    
    // Instantiate FIFO
    uram_fifo #(
        .DATA_WIDTH(DATA_WIDTH),
        .FIFO_DEPTH(FIFO_DEPTH),
        .READ_LATENCY(READ_LATENCY)
    ) dut (
        .clk(clk),
        .rst(rst),
        .wr_en(wr_en),
        .wr_data(wr_data),
        .wr_busy(wr_busy),
        .rd_en(rd_en),
        .rd_data(rd_data),
        .rd_busy(rd_busy)
    );
    
    // Clock generation
    initial begin
        clk = 0;
        forever #(CLK_PERIOD/2) clk = ~clk;
    end
    
    // Test stimulus
    initial begin
        // Initialize signals
        rst = 0;
        wr_en = 0;
        rd_en = 0;
        wr_data = 0;
        
        // Apply reset
        @(posedge clk);
        rst = 1;
        repeat(5) @(posedge clk);
        rst = 0;
        repeat(5) @(posedge clk);
        
        // Test 1: Write until almost full
        $display("Test 1: Writing data to FIFO");
        @(negedge clk);
        wr_en = 1;
        for(i = 0; i < FIFO_DEPTH; i = i+1) begin
            if (!wr_busy) begin
                wr_data = i+1;
                write_count = write_count + 1;
            end
            @(negedge clk);
        end
        wr_en = 0;
        
        // Wait a few cycles
        repeat(5) @(posedge clk);
        
        // Test 2: Read until empty
        $display("Test 2: Reading and verifying data");
        @(negedge clk);
        expected_data = 1;
        rd_en = 1;
        while (read_count < write_count) begin
            if(read_count) begin
                @(negedge clk);
            end
            if (!rd_busy) begin
                if (rd_data !== expected_data) begin
                    $display("Error: Expected %h but got %h at read %d",
                            expected_data, rd_data, read_count);
                    error_count = error_count + 1;
                end
                expected_data = expected_data + 1;
                read_count = read_count + 1;
            end
        end
        @(negedge clk);
        rd_en = 0;
        
        @(negedge clk);
        wr_en = 1;
        wr_data = 16'h0030;
        @(negedge clk);
        wr_data = 16'h0031;
        @(negedge clk);
        wr_data = 16'h0032;
        @(negedge clk);
        wr_data = 16'h0033;
        rd_en = 1;
        @(negedge clk);
        wr_data = 16'h0034;
        @(negedge clk);
        wr_data = 16'h0035;
        @(negedge clk);
        wr_data = 16'h0036;
        @(negedge clk);
        wr_data = 16'h0037;
        @(negedge clk);
        wr_en = 0;
        
        // // Test 3: Alternating read/write
        // $display("Test 3: Alternating read/write operations");
        // write_count = 0;
        // read_count = 0;
        // expected_data = 32'hA0;  // Different pattern
        
        // repeat(20) begin
        //     @(posedge clk);
        //     if (!wr_busy) begin
        //         wr_en = 1;
        //         wr_data = expected_data + write_count;
        //         write_count = write_count + 1;
        //     end
        //     @(posedge clk);
        //     wr_en = 0;
            
        //     if (!rd_busy) begin
        //         rd_en = 1;
        //         @(posedge clk);
        //         if (rd_valid) begin
        //             if (rd_data !== expected_data + read_count) begin
        //                 $display("Error: Expected %h but got %h in alternating test",
        //                         expected_data + read_count, rd_data);
        //                 error_count = error_count + 1;
        //             end
        //             read_count = read_count + 1;
        //         end
        //     end
        //     rd_en = 0;
        // end
        
        // Final report
        repeat(5) @(posedge clk);
        if (error_count == 0)
            $display("All tests passed successfully!");
        else
            $display("Tests completed with %d errors", error_count);
            
        $finish;
    end
    
    // Optional: Timeout
    initial begin
        #50000;  // Adjust timeout value as needed
        $display("Timeout: Test did not complete in time");
        $finish;
    end
    
    // Optional: Dump waveform
    initial begin
        $dumpfile("uram_fifo_tb.vcd");
        $dumpvars(0, uram_fifo_tb);
    end

endmodule