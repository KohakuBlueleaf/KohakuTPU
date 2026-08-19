// Bench for the adapter template: STAGE=0 must be transparent, STAGE=1 must
// preserve the hold-until-taken protocol under random backpressure (checked
// by kh_port_check), and the observe taps must count exactly the transfers.
`timescale 1ns / 1ps
`default_nettype none

module kh_endpoint_adapter_template_tb;
    localparam FW = 288;
    localparam N  = 32;

    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

    // stimulus into the router face, consumption at the endpoint face
    reg  [FW-1:0] rt_data;
    reg           rt_valid = 0;
    wire          rt_busy;
    wire [FW-1:0] ep_data;
    wire          ep_valid;
    reg           ep_busy = 0;
    // upstream direction
    reg  [FW-1:0] eu_data;
    reg           eu_valid = 0;
    wire          eu_busy;
    wire [FW-1:0] ru_data;
    wire          ru_valid;
    reg           ru_busy = 0;
    wire [31:0]   n_down, n_up;

    kh_endpoint_adapter_template #(.FLIT_WIDTH(FW), .STAGE(1)) dut (
        .clk(clk), .rst(rst),
        .rt_data(rt_data), .rt_valid(rt_valid), .rt_busy(rt_busy),
        .ru_data(ru_data), .ru_valid(ru_valid), .ru_busy(ru_busy),
        .ep_data(ep_data), .ep_valid(ep_valid), .ep_busy(ep_busy),
        .eu_data(eu_data), .eu_valid(eu_valid), .eu_busy(eu_busy),
        .n_down(n_down), .n_up(n_up)
    );

    kh_port_check #(.FLIT_WIDTH(FW), .NAME("adapter-ep"), .MAX_BUSY(500)) u_chk (
        .clk(clk), .rst(rst),
        .in_data(eu_data), .in_valid(eu_valid), .in_busy(eu_busy),
        .out_data(ep_data), .out_valid(ep_valid), .out_busy(ep_busy)
    );

    // STAGE=0 beside it, same wires down: transparency is checked every cycle.
    wire [FW-1:0] p_ep_data;
    wire          p_ep_valid, p_rt_busy;
    kh_endpoint_adapter_template #(.FLIT_WIDTH(FW), .STAGE(0)) dut_pass (
        .clk(clk), .rst(rst),
        .rt_data(rt_data), .rt_valid(rt_valid), .rt_busy(p_rt_busy),
        .ru_data(), .ru_valid(), .ru_busy(1'b0),
        .ep_data(p_ep_data), .ep_valid(p_ep_valid), .ep_busy(ep_busy),
        .eu_data({FW{1'b0}}), .eu_valid(1'b0), .eu_busy(),
        .n_down(), .n_up()
    );

    integer errors = 0, checks = 0;
    task chk(input [63:0] got, input [63:0] want, input [511:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                $display("%0t ERROR %0s: got %0h want %0h", $time, what, got, want);
            end
        end
    endtask

    always @(posedge clk) begin
        if (!rst) begin
            if (p_ep_valid !== rt_valid || (rt_valid && p_ep_data !== rt_data))
                chk(0, 1, "STAGE=0 is a straight wire");
        end
    end

    // Random backpressure, updated at POSEDGE: a negedge update races the
    // driver tasks' negedge busy sample against the edge it gates.
    always @(posedge clk) begin
        ep_busy <= ($random % 4 == 0);
        ru_busy <= ($random % 3 == 0);
    end

    // downstream consumer: record what arrives, in order
    integer got_d = 0, got_u = 0;
    reg [31:0] seq_d [0:N-1];
    reg [31:0] seq_u [0:N-1];
    always @(posedge clk) begin
        if (!rst && ep_valid && !ep_busy && got_d < N) begin
            seq_d[got_d] <= ep_data[31:0];
            got_d <= got_d + 1;
        end
        if (!rst && ru_valid && !ru_busy && got_u < N) begin
            seq_u[got_u] <= ru_data[31:0];
            got_u <= got_u + 1;
        end
    end

    reg took;
    task put_rt(input [31:0] v);
        begin
            @(negedge clk);
            rt_data  = {8{v}};
            rt_valid = 1;
            took = 0;
            while (!took) begin
                took = !rt_busy;
                @(negedge clk);
            end
            rt_valid = 0;
        end
    endtask
    task put_eu(input [31:0] v);
        begin
            @(negedge clk);
            eu_data  = {8{v}};
            eu_valid = 1;
            took = 0;
            while (!took) begin
                took = !eu_busy;
                @(negedge clk);
            end
            eu_valid = 0;
        end
    endtask

    integer i, spin;
    initial begin
        repeat (4) @(negedge clk);
        rst = 0;
        repeat (2) @(negedge clk);

        for (i = 0; i < N; i = i + 1) put_rt(32'h1000 + i);
        for (i = 0; i < N; i = i + 1) put_eu(32'h8000 + i);

        spin = 0;
        while ((got_d < N || got_u < N) && spin < 2000) begin
            spin = spin + 1;
            @(negedge clk);
        end
        chk(got_d, N, "all downstream flits delivered");
        chk(got_u, N, "all upstream flits delivered");
        for (i = 0; i < N; i = i + 1) begin
            if (seq_d[i] !== 32'h1000 + i) chk(seq_d[i], 32'h1000 + i, "down order");
            if (seq_u[i] !== 32'h8000 + i) chk(seq_u[i], 32'h8000 + i, "up order");
        end
        chk(n_down, N, "down tap counts transfers");
        chk(n_up, N, "up tap counts transfers");

        u_chk.report;
        if (errors == 0 && u_chk.violations == 0)
            $display("PASS kh_endpoint_adapter_template_tb: %0d checks", checks);
        else
            $display("FAIL kh_endpoint_adapter_template_tb: %0d errors", errors);
        $finish;
    end
endmodule

`default_nettype wire
