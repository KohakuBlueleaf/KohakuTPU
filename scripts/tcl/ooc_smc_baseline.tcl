# SmartConnect at root_smc's shape. Standalone it exposes NO address map, and
# axi_interconnect refuses to instantiate outside IPI, so this needs a BD.

#   vivado -mode batch -source scripts/tcl/ooc_smc_baseline.tcl -tclargs <outdir>

# axi_vip endpoints give the BD real address spaces while synthesising to
# nothing, so the reported cost is the interconnect alone.

set outdir [lindex $argv 0]
set part   xcvu13p-fhgb2104-2L-e

set_param general.maxThreads 4

file mkdir $outdir
create_project smcbase [file join $outdir smcbase] -part $part -force
create_bd_design bd

# (name, width, protocol) -- v5's jtag, xdma M_AXI, xdma M_AXI_LITE
set MASTERS {m0 32 AXI4 m1 512 AXI4 m2 32 AXI4LITE}
# leaf0, mesh_1 mem, leaf2, mesh ctrl, ddr ctrl, four clk_wiz
set SLAVES  {s0 512 s1 512 s2 512 s3 32 s4 32 s5 32 s6 32 s7 32 s8 32}

foreach {nm w proto} $MASTERS {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vip $nm]
    set_property -dict [list CONFIG.INTERFACE_MODE {MASTER} \
                             CONFIG.PROTOCOL $proto \
                             CONFIG.DATA_WIDTH $w \
                             CONFIG.ADDR_WIDTH {40} \
                             CONFIG.ID_WIDTH {4}] $c
}
foreach {nm w} $SLAVES {
    set c [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vip $nm]
    set_property -dict [list CONFIG.INTERFACE_MODE {SLAVE} \
                             CONFIG.PROTOCOL {AXI4} \
                             CONFIG.DATA_WIDTH $w \
                             CONFIG.ADDR_WIDTH {40} \
                             CONFIG.ID_WIDTH {8}] $c
}

set smc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect smc]
set_property -dict [list CONFIG.NUM_SI {3} CONFIG.NUM_MI {9} \
                         CONFIG.NUM_CLKS {4}] $smc

set i 0
foreach {nm w proto} $MASTERS {
    connect_bd_intf_net [get_bd_intf_pins $nm/M_AXI] \
        [get_bd_intf_pins smc/S0${i}_AXI]
    incr i
}
set i 0
foreach {nm w} $SLAVES {
    connect_bd_intf_net [get_bd_intf_pins smc/M0${i}_AXI] \
        [get_bd_intf_pins $nm/S_AXI]
    incr i
}

# FREQ_HZ IS THE WHOLE BASELINE. Without it SmartConnect reads all four domains
# as one and builds NO clock converters: 1.4k LUTRAM against v5's 11.7k.
foreach {nm hz} {clk_ctrl 100000000 clk_xdma 250000000 \
                 clk_mesh 237000000 clk_ddr 300000000} {
    create_bd_port -dir I -type clk $nm
    set_property CONFIG.FREQ_HZ $hz [get_bd_ports $nm]
}
create_bd_port -dir I -type rst aresetn
set_property CONFIG.ASSOCIATED_RESET {aresetn} [get_bd_ports clk_ctrl]

# Domains exactly as v5 has them: control is SmartConnect's primary, XDMA is
# aclk1, mesh_1 fabric aclk2, DDR ui_clk aclk3.
array set DOM {m0 clk_ctrl m1 clk_xdma m2 clk_xdma \
                s0 clk_ctrl s1 clk_mesh s2 clk_ctrl s3 clk_mesh s4 clk_ddr \
                s5 clk_ctrl s6 clk_ctrl s7 clk_ctrl s8 clk_ctrl}
foreach {cell clk} [array get DOM] {
    connect_bd_net [get_bd_ports $clk] [get_bd_pins $cell/aclk]
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins $cell/aresetn]
}
foreach {pin clk} {aclk clk_ctrl aclk1 clk_xdma aclk2 clk_mesh aclk3 clk_ddr} {
    connect_bd_net [get_bd_ports $clk] [get_bd_pins smc/$pin]
}
connect_bd_net [get_bd_ports aresetn] [get_bd_pins smc/aresetn]

# FREQ_HZ has to land on the INTERFACE, not the clock port: SmartConnect reads
# each SI/MI's own FREQ_HZ to decide whether a converter is needed.
array set HZ {clk_ctrl 100000000 clk_xdma 250000000 clk_mesh 237000000 \
              clk_ddr 300000000}
foreach {cell clk} [array get DOM] {
    catch { set_property CONFIG.FREQ_HZ $HZ($clk) [get_bd_pins $cell/aclk] }
    set ifp [get_bd_intf_pins -quiet $cell/M_AXI]
    if {[llength $ifp] == 0} { set ifp [get_bd_intf_pins $cell/S_AXI] }
    catch { set_property CONFIG.FREQ_HZ $HZ($clk) $ifp }
}

puts "@@@ address spaces: [get_bd_addr_spaces]"
puts "@@@ address segs:   [get_bd_addr_segs]"

# Same map sb_root9 decodes. The three 512-bit slaves take the mesh windows,
# with s2 covering two the way v5's leaf2 does; the rest is the control plane.
array set SEG {s0 {0x8000000000 0x1000000000} s1 {0x9000000000 0x1000000000} \
               s2 {0xA000000000 0x2000000000} s3 {0x00810000 0x10000} \
               s4 {0x00300000 0x100000}       s5 {0x00900000 0x10000} \
               s6 {0x00910000 0x10000}        s7 {0x00920000 0x10000} \
               s8 {0x00930000 0x10000}}

foreach {nm w proto} $MASTERS {
    set sp [get_bd_addr_spaces $nm/Master_AXI]
    foreach {sl pair} [array get SEG] {
        lassign $pair off rng
        set seg [get_bd_addr_segs $sl/S_AXI/Reg]
        # xdma M_AXI reaches mesh memory only; its AXI4-Lite reaches control
        # only. That exclusion is what prunes SmartConnect's connectivity.
        set mesh [expr {[lsearch {s0 s1 s2} $sl] >= 0}]
        if {($nm eq "m1" && !$mesh) || ($nm eq "m2" && $mesh)} {
            # EXCLUDE, not skip. An unassigned segment leaves SmartConnect free
            # to build the path anyway, which measures a crossbar v5 never has.
            exclude_bd_addr_seg -target_address_space $sp $seg
            continue
        }
        assign_bd_address -target_address_space $sp $seg -offset $off \
                          -range $rng -force
    }
}

puts "@@@ ---- final map"
foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces]] {
    puts [format "@@@ %-44s %s %s" $s [get_property OFFSET $s] \
              [get_property RANGE $s]]
}

validate_bd_design
save_bd_design

# AFTER validate: propagation runs there, so anything printed earlier is the
# 100 MHz default and says nothing about what SmartConnect will build.
foreach p [get_bd_intf_pins -quiet smc/*] {
    puts [format "@@@ %-22s FREQ_HZ %s" $p [get_property CONFIG.FREQ_HZ $p]]
}

# Global synthesis: one flat result instead of a per-IP OOC run for each of the
# thirteen endpoints, which is what makes this cheap to re-measure.
set bdf [get_files bd.bd]
set_property synth_checkpoint_mode None $bdf
generate_target all $bdf
make_wrapper -files $bdf -top -import

launch_runs synth_1 -jobs 4
wait_on_run synth_1
open_run synth_1 -name synth_1

source [file join [file dirname [info script]] ooc_class.tcl]

set smcc [get_cells -quiet -hier -filter {NAME =~ *smc*}]
puts "@@@ smc candidates: [llength $smcc]"
foreach c [get_cells -quiet bd_i/*] { puts "@@@ top cell: $c" }

set smcc [get_cells -quiet bd_i/smc]
if {[llength $smcc]} { ooc_count "smc" $smcc } else { puts "@@@ smc cell NOT FOUND" }

puts "@@@ ============================ whole design"
ooc_count TOTAL

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000
puts "@@@ smc_baseline done"
