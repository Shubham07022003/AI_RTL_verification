`timescale 1ns/1ps

module tb_fifo;

    reg clk;
    reg rst;

    reg wr_en;
    reg rd_en;

    reg [7:0] data_in;

    wire [7:0] data_out;
    wire full;
    wire empty;

    fifo dut (
        .clk(clk),
        .rst(rst),

        .wr_en(wr_en),
        .rd_en(rd_en),

        .data_in(data_in),

        .data_out(data_out),
        .full(full),
        .empty(empty)
    );

    //------------------------------------------------
    // Clock
    //------------------------------------------------

    initial
    begin
        clk = 0;

        forever #5 clk = ~clk;
    end

    //------------------------------------------------
    // Stimulus
    //------------------------------------------------

    initial
    begin

        $display("FIFO Test Started");

        rst     = 1;
        wr_en   = 0;
        rd_en   = 0;
        data_in = 0;

        #20;

        rst = 0;

        //------------------------------------------------
        // Write Data
        //------------------------------------------------

        @(posedge clk);
        wr_en   = 1;
        data_in = 8'h11;

        @(posedge clk);
        data_in = 8'h22;

        @(posedge clk);
        data_in = 8'h33;

        @(posedge clk);
        wr_en = 0;

        //------------------------------------------------
        // Read Data
        //------------------------------------------------

        @(posedge clk);
        rd_en = 1;

        @(posedge clk);

        @(posedge clk);

        @(posedge clk);
        rd_en = 0;

        //------------------------------------------------
        // Finish
        //------------------------------------------------

        #50;

        $display("FIFO Test Completed");

        $finish;

    end

    //------------------------------------------------
    // Monitor
    //------------------------------------------------

    initial
    begin
        $monitor(
            "Time=%0t rst=%b wr=%b rd=%b din=%h dout=%h full=%b empty=%b",
            $time,
            rst,
            wr_en,
            rd_en,
            data_in,
            data_out,
            full,
            empty
        );
    end

endmodule