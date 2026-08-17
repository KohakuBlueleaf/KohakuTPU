# The REAL root_smc, synthesised out of context from v5's own .xci. A rebuild
# with axi_vip endpoints understates it: 1,412 LUTRAM against v5's 11,748.

#   vivado -mode batch -source scripts/tcl/ooc_smc_real.tcl -tclargs <out> <xci>

# The IP directory is COPIED first. Reading it in place would regenerate output
# products inside a live project.

set out [lindex $argv 0]
set xci [lindex $argv 1]
set part xcvu13p-fhgb2104-2L-e

set_param general.maxThreads 4
source [file join [file dirname [info script]] ooc_class.tcl]

set ipdir  [file dirname $xci]
set ipname [file rootname [file tail $xci]]

file mkdir $out
file delete -force [file join $out $ipname]
file copy -force $ipdir [file join $out $ipname]
set mine [file join $out $ipname [file tail $xci]]
puts "@@@ ip $ipname"

create_project -in_memory -part $part
read_ip $mine
set ip [get_ips $ipname]

foreach p {CONFIG.NUM_SI CONFIG.NUM_MI CONFIG.NUM_CLKS} {
    puts [format "@@@ %-18s %s" $p [get_property $p $ip]]
}

generate_target synthesis $ip
synth_design -top $ipname -part $part -mode out_of_context

puts "@@@ ============================ real root_smc"
ooc_count ROOT_SMC
ooc_classify 2000
puts "@@@ smc_real done"
