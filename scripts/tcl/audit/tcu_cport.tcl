# Structural check of mx_tcu's cross-TCU partial: OOC synthesis of a chained
# TCU (FIRST = 0, the DSP48E2 primitive), then every DSP's C pins by the net
# that drives them. Only the last stage of each of the 8 chains reads C (W),
# so 8 x 48 C pins may carry a signal and every other one ties off.
#   vivado -mode batch -source scripts/tcl/audit/tcu_cport.tcl [-tclargs <tcu.v>]
set root [file normalize [file join [file dirname [info script]] .. .. ..]]
set tcu  [expr {[llength $argv] ? [lindex $argv 0] : "$root/src/kohakutpu/matmul/mx_tcu.v"}]
read_verilog $root/src/kohakutpu/matmul/mx_mac.v
read_verilog $tcu
synth_design -top mx_tcu -part xcvu13p-fhgb2104-2L-e -mode out_of_context \
    -generic FIRST=0 -generic MODEL=0
set dsps [get_cells -hier -filter {REF_NAME == DSP48E2}]
if {[llength $dsps] != 64} { puts "TCU CPORT FAIL: [llength $dsps] DSP48E2, want 64"; exit 1 }
set live 0
set tied 0
foreach c $dsps {
    foreach p [get_pins -of $c -filter {REF_PIN_NAME =~ C[*]}] {
        set n [get_nets -quiet -of $p]
        if {$n eq "" || [get_property TYPE $n] in {GROUND POWER}} { incr tied } else { incr live }
    }
}
set want [expr {8 * 48}]
if {$live == $want && $tied == 64 * 48 - $want} {
    puts "TCU CPORT PASS: $live C pins driven (8 chains x 48), $tied tied off"
} else {
    puts "TCU CPORT FAIL: $live C pins driven, want $want; $tied tied off"
    exit 1
}
