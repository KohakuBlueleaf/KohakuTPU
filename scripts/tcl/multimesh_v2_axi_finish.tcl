# Finish the AXI rework: wire what multimesh_v2_axi_opt.tcl left, save, verify.

# connect_bd_net -net <name> <pin> failed with "Cannot find parent cell", so
# every connection here is pin-to-pin, which is unambiguous.

open_project C:/Users/apoll/Desktop/vivado/JTAG-DMA-test/JTAG-DMA-test.xpr
open_bd_design [get_files multimesh_v2.bd]

proc live {pin} {
    # A net OUTLIVES the cell at its far end, so count endpoints, not nets.
    foreach n [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins $pin]] {
        if {[llength [get_bd_intf_pins -quiet -of_objects $n]] >= 2} { return 1 }
        delete_bd_objs $n
    }
    return 0
}

if {[live root_smc/M01_AXI]} {
    puts "@@@ M01 already wired"
} else {
    connect_bd_intf_net [get_bd_intf_pins root_smc/M01_AXI] \
                        [get_bd_intf_pins mesh_1/S_AXI_MEM]
    puts "@@@ M01 -> mesh_1/S_AXI_MEM"
}

foreach {pin src} {
    aclk2 clk_wiz_mesh/clk_out1
    aclk3 ddr4_3/c0_ddr4_ui_clk
} {
    set p [get_bd_pins root_smc/$pin]
    if {[llength [get_bd_nets -quiet -of_objects $p]]} {
        puts "@@@ $pin already wired"
    } else {
        connect_bd_net [get_bd_pins $src] $p
        puts "@@@ $pin <- $src"
    }
}

foreach space {jtag_axi_0/Data xdma_0/M_AXI} {
    assign_bd_address -offset 0x100000000 -range 0x100000000 \
        -target_address_space [get_bd_addr_spaces $space] \
        [get_bd_addr_segs mesh_1/S_AXI_MEM/reg0] -force
}

save_bd_design
if {[catch {validate_bd_design -force} e]} {
    puts "=== VALIDATE-FAIL ==="
    puts $e
} else {
    puts "=== VALIDATE-OK ==="
}

# validate_bd_design does NOT check that a slave is reachable: it passed a
# design whose mesh_1 4 GB window was in the map with no path to it.
puts "@@@ ---- connectivity, the check that actually means something ----"
foreach m {M00 M01 M02 M03 M04 M05 M06 M07} {
    set n [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins root_smc/${m}_AXI]]
    set ends {}
    foreach e [get_bd_intf_pins -quiet -of_objects $n] { lappend ends [get_property PATH $e] }
    puts "@@@ $m -> [expr {[llength $ends] >= 2 ? $ends : {UNCONNECTED}}]"
}
foreach ip {mesh_1/S_AXI_MEM mesh_1/S_AXI_CTRL} {
    set n [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins $ip]]
    puts "@@@ $ip -> [expr {[llength $n] ? $n : {UNCONNECTED}}]"
}
foreach c {aclk aclk1 aclk2 aclk3} {
    set n [get_bd_nets -quiet -of_objects [get_bd_pins root_smc/$c]]
    puts "@@@ root_smc/$c <- [expr {[llength $n] ? $n : {UNCONNECTED}}]"
}
close_project
puts "@@@ DONE"
