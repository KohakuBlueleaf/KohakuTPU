# OOC synthesis of ONE vendor IP as a reference point for the KTS measurement
# page: the IP is created in an in-memory project, generated, synthesised out
# of context at the given period, and reported like ooc_mod.tcl does
# (hier.rpt, util.rpt, timing.rpt, timing_summary.rpt, result.txt).
#
#   vivado -mode batch -source scripts/tcl/ooc_kts_ref.tcl \
#       -tclargs <outdir> <ip_name> <module_name> <period_ns> <clock_port_globs> <props|->
#   props: NAME:VALUE joined by +  (CONFIG.* properties, the CONFIG. prefix implied)

if {[llength $argv] < 6} { puts "usage: ooc_kts_ref.tcl <outdir> <ip> <module> <period> <clocks> <props|->"; exit 2 }
set outdir [lindex $argv 0]
set ipname [lindex $argv 1]
set modname [lindex $argv 2]
set period [lindex $argv 3]
set cspec  [lindex $argv 4]
set pspec  [lindex $argv 5]
set part   xcvu13p-fhgb2104-2L-e
file mkdir $outdir

create_project -in_memory -part $part
set_property target_language Verilog [current_project]
create_ip -name $ipname -vendor xilinx.com -library ip -module_name $modname -dir $outdir
set ip [get_ips $modname]
if {$pspec ne "-"} {
    set props {}
    foreach kv [split $pspec "+"] {
        set p [split $kv ":"]
        lappend props "CONFIG.[lindex $p 0]" [lindex $p 1]
    }
    set_property -dict $props $ip
}
generate_target all $ip
set cmd [list synth_design -top $modname -part $part -mode out_of_context]
if {[catch {eval $cmd} err]} { puts "SYNTH FAILED: $err"; exit 1 }

set clkports {}
foreach cg [split $cspec "+"] { set clkports [concat $clkports [get_ports -quiet -filter {DIRECTION == IN} $cg]] }
set names {}
foreach cp $clkports {
    set nm [get_property NAME $cp]
    create_clock -name $nm -period $period $cp
    lappend names $nm
}
if {[llength $names] > 1} {
    set grp {}
    foreach nm $names { lappend grp -group [get_clocks $nm] }
    set_clock_groups -asynchronous {*}$grp
}
if {[llength $names] == 0} { puts "NO CLOCK PORT MATCHED"; exit 1 }

report_utilization -hierarchical -hierarchical_depth 6 -file "$outdir/hier.rpt"
report_utilization -file "$outdir/util.rpt"
report_timing -delay_type max -max_paths 100 -nworst 1 -sort_by slack -file "$outdir/timing.rpt"
report_timing_summary -file "$outdir/timing_summary.rpt"

set paths [get_timing_paths -delay_type max -max_paths 1 -nworst 1]
if {[llength $paths] == 0} {
    set wns 0.0
    set fmax 0.0
} else {
    set wns [get_property SLACK [lindex $paths 0]]
    set fmax [expr {1000.0 / ($period - $wns)}]
}
set lut "?"; set ff "?"; set uram "?"; set bram "?"
foreach line [split [report_utilization -return_string] "\n"] {
    if {[regexp {CLB LUTs\*?\s+\|\s+(\d+)} $line -> v]} { set lut $v }
    if {[regexp {CLB Registers\s+\|\s+(\d+)} $line -> v]} { set ff $v }
    if {[regexp {\|\s*URAM\s+\|\s+(\d+)} $line -> v]} { set uram $v }
    if {[regexp {Block RAM Tile\s+\|\s+([\d.]+)} $line -> v]} { set bram $v }
}
set fh [open "$outdir/result.txt" w]
puts $fh "LUT=$lut FF=$ff URAM=$uram BRAM=$bram WNS=[format %.3f $wns] Fmax=[format %.1f $fmax] ip=$ipname props={$pspec}"
close $fh
puts "RESULT LUT=$lut FF=$ff URAM=$uram BRAM=$bram WNS=[format %.3f $wns] Fmax=[format %.1f $fmax]"
