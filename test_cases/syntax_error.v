// syntax_error.v - the assign statement on line 4 is missing its ';'
module adder (input [3:0] a, input [3:0] b, output [4:0] sum);
    // BUG: missing semicolon at the end of the next line
    assign sum = a + b
endmodule
