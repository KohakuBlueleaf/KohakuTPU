# Shrink the control fabric IN PLACE: only SmartConnect and XDMA change, so the
# meshes, MIGs and register slices keep their cached OOC results.

# Measured LUT: 1x3 8,516  1x4 10,376  3x5 25,766  2x8 30,034  3x13 ~40,000.
# Per MI ~1,800 throughout. The SI cost is a STEP, not a slope:

#     base(1 SI) 2,936    base(2 SI) 15,634    base(3 SI) 16,871

# One master needs no arbitration; the second buys the whole crossbar and the
# third is nearly free. So a 1-SI leaf is disproportionately cheap.

# SLR1 measured 36,142 -> 30,034, saving 6,108. Dropping the third SI was worth
# only ~1,240 of that; deleting leaf_smc_1 did the rest.

# Dropping AXI-Lite forecloses host MMIO without a DMA descriptor: it is useless
# only because every control window is above 4 GB and Lite is 32-bit.

set xpr C:/Users/apoll/Desktop/vivado/JTAG-DMA-test/JTAG-DMA-test.xpr
set bd  multimesh_v2

open_project $xpr
open_bd_design [get_files $bd.bd]

puts "@@@ BEFORE root SI=[get_property CONFIG.NUM_SI [get_bd_cells root_smc]] MI=[get_property CONFIG.NUM_MI [get_bd_cells root_smc]]"

# ---- 1. XDMA drops its AXI-Lite master ------------------------------------
set_property CONFIG.axilite_master_en {false} [get_bd_cells xdma_0]

# ---- 2. leaf_smc_1 goes ----------------------------------------------------
# delete_bd_objs ERRORS on an empty list: that aborted the whole second pass.
set l1 [get_bd_cells -quiet leaf_smc_1]
if {[llength $l1]} { delete_bd_objs $l1 } else { puts "@@@ leaf_smc_1 already gone" }

# ---- 3. reshape the root ---------------------------------------------------
# aclk=XDMA, aclk1=100 MHz, aclk2=mesh (mesh_1 S_AXI), aclk3=ddr4_3 ui_clk.
set_property -dict [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {8} \
                         CONFIG.NUM_CLKS {4}] [get_bd_cells root_smc]

# ---- 4. rewire ------------------------------------------------------------
# Only unconnected ports are touched, so re-running this script is safe.
proc wire {mport target} {
    set p [get_bd_intf_pins root_smc/$mport]
    # A net OUTLIVES the cell at its far end, so count endpoints, not nets:
    # believing the net left mesh_1/S_AXI_MEM unwired and validate_bd_design mute.
    foreach n [get_bd_intf_nets -quiet -of_objects $p] {
        if {[llength [get_bd_intf_pins -quiet -of_objects $n]] >= 2} {
            puts "@@@   $mport already connected, left alone"
            return
        }
        puts "@@@   $mport: deleting stub net $n"
        delete_bd_objs $n
    }
    connect_bd_intf_net $p [get_bd_intf_pins $target]
    puts "@@@   $mport -> $target"
}
wire M01_AXI mesh_1/S_AXI_MEM
wire M05_AXI mesh_1/S_AXI_CTRL
wire M06_AXI ddr4_3/C0_DDR4_S_AXI_CTRL
wire M07_AXI axi_gpio_0/S_AXI

# mesh_1's slave ports run on the MESH clock, ddr4_3's CTRL on its ui_clk.
connect_bd_net -net clk_mesh [get_bd_pins root_smc/aclk2]
connect_bd_net -net ddr4_3_ui_clk [get_bd_pins root_smc/aclk3]

# ---- 5. addresses ----------------------------------------------------------
# The segments and the two master spaces are unchanged; M_AXI_LITE's space is
# gone with the port, and its exclusions with it.
foreach space {jtag_axi_0/Data xdma_0/M_AXI} {
    foreach id {0 1 2 3} {
        assign_bd_address -offset [format 0x%llX [expr {$id * 0x100000000}]] \
            -range 0x100000000 -target_address_space [get_bd_addr_spaces $space] \
            [get_bd_addr_segs mesh_$id/S_AXI_MEM/reg0] -force
        assign_bd_address -offset [format 0x%llX [expr {0x400800000 + $id * 0x10000}]] \
            -range 0x10000 -target_address_space [get_bd_addr_spaces $space] \
            [get_bd_addr_segs mesh_$id/S_AXI_CTRL/reg0] -force
    }
    foreach i {0 1 2 3} {
        assign_bd_address -offset [format 0x%llX [expr {0x400000000 + $i * 0x100000}]] \
            -range 0x100000 -target_address_space [get_bd_addr_spaces $space] \
            [get_bd_addr_segs ddr4_$i/C0_DDR4_MEMORY_MAP_CTRL/C0_REG] -force
    }
    assign_bd_address -offset 0x400400000 -range 0x10000 \
        -target_address_space [get_bd_addr_spaces $space] \
        [get_bd_addr_segs axi_gpio_0/S_AXI/Reg] -force
    assign_bd_address -offset 0x400900000 -range 0x10000 \
        -target_address_space [get_bd_addr_spaces $space] \
        [get_bd_addr_segs clk_wiz_mesh/s_axi_lite/Reg] -force
}

regenerate_bd_layout
save_bd_design
if {[catch {validate_bd_design -force} e]} {
    puts "=== VALIDATE-FAIL ==="
    puts $e
} else {
    puts "=== VALIDATE-OK ==="
}

puts "@@@ AFTER  root SI=[get_property CONFIG.NUM_SI [get_bd_cells root_smc]] MI=[get_property CONFIG.NUM_MI [get_bd_cells root_smc]]"
puts "@@@ leaf cells now: [get_property NAME [get_bd_cells -quiet leaf_smc_*]]"
puts "@@@ xdma axilite  : [get_property CONFIG.axilite_master_en [get_bd_cells xdma_0]]"
puts "=== ADDRESS MAP ==="
foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces]] {
    puts [format "%-58s %s +%s" $s [get_property OFFSET $s] [get_property RANGE $s]]
}
close_project
puts "@@@ DONE"
