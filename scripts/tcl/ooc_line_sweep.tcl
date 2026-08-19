# The v6 line, swept, one config per invocation.
#   -tclargs <fw> <nq> <cdc> <period_ns> <tag> <lut_per_bram> <ost> <sfwd> <half>

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set fw   [lindex $argv 0]
set nq   [lindex $argv 1]
set cdc  [lindex $argv 2]
set per  [lindex $argv 3]
set tag  [lindex $argv 4]
set lpb  [lindex $argv 5]
set ost  [lindex $argv 6]
set sfwd [lindex $argv 7]
set half [lindex $argv 8]
set aw   [lindex $argv 9]
set nm   [lindex $argv 10]
if {$aw eq ""} { set aw 43 }
if {$nm eq ""} { set nm 3 }
if {$fw   eq ""} { set fw   512 }
if {$nq   eq ""} { set nq   4 }
if {$cdc  eq ""} { set cdc  1 }
if {$per  eq ""} { set per  5.000 }
if {$tag  eq ""} { set tag  default }
if {$lpb  eq ""} { set lpb  0 }
if {$ost  eq ""} { set ost  4 }
if {$sfwd eq ""} { set sfwd 1 }
if {$half eq ""} { set half 0 }

# PORTW must cover NQ and the flit format is uniform along the line.
set portw [expr {$nq <= 1 ? 1 : int(ceil(log($nq)/log(2)))}]
if {[expr {1 << $portw}] < $nq} { incr portw }
# SRCW likewise covers NM; at NM=6 the default 2 truncates the source field.
set srcw [expr {$nm <= 1 ? 1 : int(ceil(log($nm)/log(2)))}]
if {[expr {1 << $srcw}] < $nm} { incr srcw }
if {$srcw < 2} { set srcw 2 }

set_param general.maxThreads 4

read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common async_fifo.v] \
    [file join $root src kohakuaccel common sb_skid.v] \
    [file join $root src kohakuaccel axi station sb_hub.v] \
    [file join $root src kohakuaccel axi station sb_nmu.v] \
    [file join $root src kohakuaccel axi station sb_nsu.v] \
    [file join $root src kohakuaccel axi link sb_link.v] \
    [file join $root src kohakuaccel axi link sb_link_cdc.v] \
    [file join $root src kohakuaccel axi topo sb_stn_line.v] \
    [file join $root src kohakuaccel axi topo sb_line4.v]]

# The bus period is the knob under test, so write the XDC per run.
set xdc $root/build/ooc_line_${tag}.xdc
file mkdir $root/build
set fh [open $xdc w]
foreach c {bus_clk0 bus_clk1 bus_clk2 bus_clk3} {
    puts $fh "create_clock -name $c -period $per \[get_ports $c\]"
}
puts $fh "create_clock -name clk_ctrl -period 10.000 \[get_ports clk_ctrl\]"
puts $fh "create_clock -name clk_xdma -period 4.000  \[get_ports clk_xdma\]"
puts $fh "create_clock -name clk_s0   -period 4.219  \[get_ports clk_s0\]"
puts $fh "create_clock -name clk_s1   -period 3.333  \[get_ports clk_s1\]"
puts $fh "create_clock -name clk_s2   -period 5.545  \[get_ports clk_s2\]"
puts $fh "create_clock -name clk_s3   -period 4.746  \[get_ports clk_s3\]"
# clk_ddr0..3, NOT clk_ddr: the scalar port went when port 2 became per-station,
# so this matched nothing and left all four DDR domains untimed and ungrouped.
foreach c {clk_ddr0 clk_ddr1 clk_ddr2 clk_ddr3} {
    puts $fh "create_clock -name $c -period 3.333 \[get_ports $c\]"
}
set grp {}
foreach c {bus_clk0 bus_clk1 bus_clk2 bus_clk3 clk_ctrl clk_xdma \
           clk_s0 clk_s1 clk_s2 clk_s3 \
           clk_ddr0 clk_ddr1 clk_ddr2 clk_ddr3} {
    append grp " -group \[get_clocks $c\]"
}
puts $fh "set_clock_groups -asynchronous$grp"
close $fh
read_xdc $xdc

puts "@@@ line sweep fw $fw nq $nq cdc $cdc period $per tag $tag"

synth_design -top sb_line4 -part $part -mode out_of_context \
             -flatten_hierarchy none -directive default \
             -generic FW=$fw -generic NQ=$nq -generic LINK_CDC=$cdc \
             -generic PORTW=$portw -generic LUT_PER_BRAM=$lpb \
             -generic OST=$ost -generic STORE_FWD=$sfwd \
             -generic AW=$aw -generic LINK_FULL=$half \
             -generic NM=$nm -generic SRCW=$srcw

source [file join $root scripts tcl ooc_class.tcl]

ooc_record $tag "top=sb_line4 fw=$fw nq=$nq nm=$nm aw=$aw cdc=$cdc half=$half\
 lpb=$lpb ost=$ost sfwd=$sfwd period=$per" 2000 2

puts "@@@ ============================ per instance $tag"
foreach inst {g_stn[0].u_stn g_stn[1].u_stn g_stn[2].u_stn g_stn[3].u_stn \
              g_stn[1].g_mgr.g_nmu[0].u_nmu g_stn[1].g_mgr.g_nmu[1].u_nmu \
              g_stn[0].g_nsu[0].u_nsu g_stn[0].g_nsu[1].u_nsu \
              g_link[0].g_cdc.u_rq_fwd g_link[0].g_cdc.u_rs_fwd \
              g_link[0].g_sync.u_rq_fwd g_link[0].g_sync.u_rs_fwd} {
    if {[llength [get_cells -quiet $inst]] == 0} { continue }
    ooc_count $inst $inst
}

puts "@@@ ============================ Fmax $tag"
ooc_classify 2000
puts "@@@ line sweep done $tag"
