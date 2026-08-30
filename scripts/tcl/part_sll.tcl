# The part's die-crossing budget: Laguna sites and TX/RX register BELs per
# SLR, read from the device model. Device queries need an open design, so a
# one-flop module is synthesised out of context first.
#   vivado -mode batch -source scripts/tcl/part_sll.tcl -tclargs <outdir>
set outdir [lindex $argv 0]
set part xcvu13p-fhgb2104-2L-e
file mkdir $outdir
set fh [open $outdir/one_flop.v w]
puts $fh "module one_flop(input wire clk, output reg q); always @(posedge clk) q <= ~q; endmodule"
close $fh
read_verilog $outdir/one_flop.v
synth_design -top one_flop -part $part -mode out_of_context
set fo [open $outdir/part_sll.txt w]
foreach slr [lsort [get_slrs]] {
    set lag [get_sites -quiet -of_objects $slr -filter {SITE_TYPE =~ LAGUNA*}]
    set tx  [get_bels -quiet -of_objects $lag -filter {TYPE =~ *TX_REG*}]
    set rx  [get_bels -quiet -of_objects $lag -filter {TYPE =~ *RX_REG*}]
    set line "$slr laguna_sites [llength $lag] tx_regs [llength $tx] rx_regs [llength $rx] clock_regions [llength [get_clock_regions -of_objects $slr]]"
    puts "@@@ $line" ; puts $fo $line
}
close $fo
