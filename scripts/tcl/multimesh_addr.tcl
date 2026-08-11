# `%llX`, NOT `%X`: Tcl's %X is 32-bit, so 0x400000000 formatted as an offset
# silently became 0x0 and every window collapsed onto the bottom of the map.
if {[info exists ::env(MULTIMESH_XPR)]} {
    open_project $::env(MULTIMESH_XPR)
} else {
    open_project C:/Users/apoll/Desktop/vivado/multimesh_review/multimesh_review.xpr
}
open_bd_design [get_files multimesh.bd]

# The DDR refclks are 400 MHz on this board; design_1.bd's 100 MHz gave four
# CRITICAL WARNINGs. singlemesh.bd, which is on the card, declares this value.
foreach i {0 1 2 3} {
    set_property CONFIG.FREQ_HZ {400160000} [get_bd_intf_ports c${i}_sys]
}

# A 34-bit global address is {mesh_id[1:0], local[31:0]}, which is what
# ktpu.hw.interlink.global_addr() already emits -- so no translation anywhere.
proc place {space seg off rng} {
    if {[catch {
        assign_bd_address -offset $off -range $rng \
            -target_address_space [get_bd_addr_spaces $space] \
            [get_bd_addr_segs $seg] -force
    } e]} {
        puts "PLACE-FAIL $space <- $seg @ $off : $e"
        return 0
    }
    return 1
}

foreach space {jtag_axi_0/Data xdma_0/M_AXI} {
    foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces $space]] {
        catch { delete_bd_addr_seg $s }
    }
}

# Control windows FIRST, high; the 4 GB memory windows can only follow, since
# a 4 GB block placed at 0 would swallow anything already sitting under it.
foreach space {jtag_axi_0/Data xdma_0/M_AXI} {
    foreach i {0 1 2 3} {
        place $space ddr4_$i/C0_DDR4_MEMORY_MAP_CTRL/C0_REG \
            [format 0x%llX [expr {0x400000000 + $i * 0x100000}]] 0x100000
    }
    place $space axi_gpio_0/S_AXI/Reg 0x400400000 0x10000
    foreach id {0 1 2 3} {
        place $space mesh_$id/S_AXI_CTRL/reg0 \
            [format 0x%llX [expr {0x400800000 + $id * 0x10000}]] 0x10000
    }
    foreach id {0 1 2 3} {
        place $space mesh_$id/S_AXI_MEM/reg0 \
            [format 0x%llX [expr {$id * 0x100000000}]] 0x100000000
    }
}

save_bd_design
puts "=== VALIDATE ==="
if {[catch {validate_bd_design -force} e]} { puts "VALIDATE-FAIL"; puts $e } \
else { puts "VALIDATE-OK" }
save_bd_design

puts "=== CONTROL PLANE, as placed ==="
foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces xdma_0/M_AXI]] {
    puts [format "  %-40s %18s +%s" [get_property NAME $s] \
              [get_property OFFSET $s] [get_property RANGE $s]]
}

# THE WRAPPER IS THE TOP, and the runs must be reset to see it: a run keeps the
# top it was launched with, which is why synth kept building singlemesh.
set bd [get_files multimesh.bd]
make_wrapper -files $bd -top -force
set wrap [file dirname [file dirname $bd]]/../../JTAG-DMA-test.gen/sources_1/bd/multimesh/hdl/multimesh_wrapper.v
if {[file exists $wrap]} { add_files -quiet -norecurse -fileset sources_1 $wrap }
update_compile_order -fileset sources_1
set_property top multimesh_wrapper [get_filesets sources_1]
set_property top_file $wrap [get_filesets sources_1]
foreach r {synth_1 impl_1} {
    if {[llength [get_runs -quiet $r]]} { catch { reset_run $r } }
}
puts "=== TOP is now [get_property top [get_filesets sources_1]]; runs reset ==="
puts "=== DONE ==="
