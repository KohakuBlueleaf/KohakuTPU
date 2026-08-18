# What actually drives noc_out_data_reg[*]/R, given the RTL never resets it.
#   vivado -mode batch -source scripts/tcl/v5_nocreset.tcl

open_project C:/Users/apoll/Desktop/vivado/JTAG-DMA-test/JTAG-DMA-test.xpr
open_run impl_1 -name impl_1
puts "@@@ opened"

set base multimesh_v5_i/mesh_0/inst/u_cu0/u_cu/u_base
set fl [get_cells -quiet "$base/noc_out_data_reg\[*\]"]
puts "@@@ noc_out_data flops [llength $fl]"
if {[llength $fl]} {
    set c0 [lindex $fl 0]
    puts "@@@ ref [get_property REF_NAME $c0] loc [get_property LOC $c0]"
    foreach pin {R S CE D} {
        set p [get_pins -quiet "$c0/$pin"]
        if {$p eq ""} { continue }
        set n [get_nets -quiet -of_objects $p]
        if {$n eq ""} { puts "@@@ pin $pin net NONE" ; continue }
        puts "@@@ pin $pin net [get_property NAME $n]\
 type [get_property TYPE $n] fanout [get_property FLAT_PIN_COUNT $n]"
    }
    # Control set: what Vivado grouped these flops with.
    puts "@@@ ctrlset clk [get_property CONTROL_SET $c0]"
}

# How many flops device-wide sit on that same reset net.
set rp [get_pins -quiet "$base/noc_out_data_reg\[0\]/R"]
if {$rp ne ""} {
    set rn [get_nets -quiet -of_objects $rp]
    if {$rn ne ""} {
        set drv [get_pins -quiet -of_objects $rn -filter {DIRECTION == OUT}]
        puts "@@@ reset driver $drv"
        puts "@@@ reset loads [llength [get_pins -quiet -of_objects $rn -filter {DIRECTION == IN}]]"
    }
}

# Same question for the whole design: how many FDRE have a live R.
set live 0
set dead 0
foreach c [get_cells -quiet -hier -filter {REF_NAME =~ FDRE}] {
    set n [get_nets -quiet -of_objects [get_pins -quiet $c/R]]
    if {$n eq "" || [get_property TYPE $n] eq "GROUND"} { incr dead } else { incr live }
}
puts "@@@ FDRE live_R $live tied_R $dead"

puts "@@@ v5_nocreset done"
