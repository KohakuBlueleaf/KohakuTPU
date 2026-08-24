# Prices moving vdot's sum and accumulate into the DSP48 column.
#   vivado -mode batch -source ooc_dot_probe.tcl -tclargs <FORM> <period>

set root [file normalize [file join [file dirname [info script]] .. ..]]

set form [lindex $argv 0]
set per  [lindex $argv 1]
if {$form eq ""} { set form 0 }
if {$per  eq ""} { set per 3.333 }

set_param general.maxThreads 4

read_verilog [list [file join $root tests pe probe khs_dot_probe.v]]

synth_design -top khs_dot_probe -part xcvu13p-fhgb2104-2L-e \
             -mode out_of_context -flatten_hierarchy none \
             -generic FORM=$form

create_clock -period $per -name clk [get_ports clk]

set lut [llength [get_cells -quiet -hier -filter {REF_NAME =~ LUT?}]]
set ff  [llength [get_cells -quiet -hier -filter {REF_NAME =~ FD*}]]
set dsp [llength [get_cells -quiet -hier -filter {REF_NAME =~ DSP*}]]
set crr [llength [get_cells -quiet -hier -filter {REF_NAME =~ CARRY*}]]
puts "@@@DOT form=$form lut=$lut ff=$ff dsp=$dsp carry=$crr"
