// incorrect_combinational.v - intended: 2-to-1 mux  y = sel ? b : a
// BUG: the sensitivity list only contains 'a'. In simulation y does NOT update when
// 'b' or 'sel' change, but synthesis ignores the list - so simulation and hardware disagree.
module mux_wrong (input a, input b, input sel, output reg y);
    always @(a) begin
        if (sel)
            y = b;
        else
            y = a;
    end
endmodule
