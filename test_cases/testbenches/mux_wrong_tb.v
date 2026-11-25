// Testbench for incorrect_combinational.v (module mux_wrong).
// It prints "FAIL ..." when the output differs from the expected 2-to-1 mux behaviour.
`timescale 1ns/1ps
module mux_wrong_tb;
    reg a, b, sel;
    wire y;
    integer errors;

    mux_wrong dut (.a(a), .b(b), .sel(sel), .y(y));

    initial begin
        errors = 0;
        a = 0; b = 0; sel = 0; #1;
        // sel = 1 selects b, so y must follow b
        sel = 1; b = 1; #1;
        if (y !== 1'b1) begin
            $display("FAIL: sel=1 b=1 expected y=1 but got y=%b", y);
            errors = errors + 1;
        end
        // sel = 0 selects a, so y must follow a
        sel = 0; a = 1; #1;
        if (y !== 1'b1) begin
            $display("FAIL: sel=0 a=1 expected y=1 but got y=%b", y);
            errors = errors + 1;
        end
        if (errors == 0) $display("PASS: all checks passed");
        $finish;
    end
endmodule
