# Driven vs tied DSP48E2 C pins, inside mx_tcu and elsewhere.
#   vivado -mode batch -source scripts/tcl/audit/dsp_cpins.tcl -tclargs <checkpoint.dcp>
set dcp [lindex $argv 0]
open_checkpoint $dcp
set tcu_cells [get_cells -quiet -hierarchical -filter {REF_NAME == mx_tcu || ORIG_REF_NAME == mx_tcu}]
set in_tcu {}
foreach t $tcu_cells { lappend in_tcu [get_property NAME $t]/ }
array set n {tcu_dsp 0 tcu_live 0 tcu_tied 0 other_dsp 0 other_live 0 other_tied 0}
foreach c [get_cells -hierarchical -filter {REF_NAME == DSP48E2}] {
    set name [get_property NAME $c]
    set k other
    foreach p $in_tcu { if {[string first $p $name] == 0} { set k tcu ; break } }
    incr n(${k}_dsp)
    foreach p [get_pins -of $c -filter {REF_PIN_NAME =~ C[*]}] {
        set net [get_nets -quiet -of $p]
        if {$net eq "" || [get_property TYPE $net] in {GROUND POWER}} { incr n(${k}_tied) } else { incr n(${k}_live) }
    }
}
puts "@@@ dsp_cpins $dcp"
puts "@@@ mx_tcu instances [llength $tcu_cells]"
foreach k {tcu other} {
    puts [format "@@@ %-5s DSP48E2 %6d  C pins driven %7d  tied %7d" $k $n(${k}_dsp) $n(${k}_live) $n(${k}_tied)]
}
