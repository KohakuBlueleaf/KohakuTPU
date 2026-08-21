# OOC synthesis of the RV32 controller PE. SYNTH ONLY. Results are the @@@ lines.
#   vivado -mode batch -source scripts/tcl/ooc_rv_pe.tcl -tclargs \
#     <top> <regfile_prim> <l1_lines> <fwd_x> <btb> <imem> <spad> <directive> \
#     <period_ns>
#
# The period is an argument because LUT is not independent of it: a resource
# figure means nothing without the target it was asked for.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set top  [lindex $argv 0]
set rfp  [lindex $argv 1]
set lin  [lindex $argv 2]
set fwx  [lindex $argv 3]
set btb  [lindex $argv 4]
set imw  [lindex $argv 5]
set spw  [lindex $argv 6]
set dirv [lindex $argv 7]
set per  [lindex $argv 8]
if {$per  eq ""} { set per  2.500 }
set ::ooc_period $per
if {$top  eq ""} { set top  rv_pe }
if {$rfp  eq ""} { set rfp  distributed }
if {$lin  eq ""} { set lin  64 }
if {$fwx  eq ""} { set fwx  1 }
if {$btb  eq ""} { set btb  32 }
if {$imw  eq ""} { set imw  2048 }
if {$spw  eq ""} { set spw  2048 }
if {$dirv eq ""} { set dirv default }

set_param general.maxThreads 4

source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
    [file join $root src kohakuaccel noc endpoint noc_cu_base.v] \
    [file join $root src kohakuaccel pe rv32 mem rv_ram_be.v] \
    [file join $root src kohakuaccel pe rv32 mem rv_imem.v] \
    [file join $root src kohakuaccel pe rv32 mem rv_spad.v] \
    [file join $root src kohakuaccel pe rv32 mem rv_l1.v] \
    [file join $root src kohakuaccel pe rv32 core rv_regfile.v] \
    [file join $root src kohakuaccel pe rv32 core rv_bpred.v] \
    [file join $root src kohakuaccel pe rv32 core rv_if.v] \
    [file join $root src kohakuaccel pe rv32 core rv_id.v] \
    [file join $root src kohakuaccel pe rv32 core rv_ex.v] \
    [file join $root src kohakuaccel pe rv32 core rv_mem.v] \
    [file join $root src kohakuaccel pe rv32 core rv_wb.v] \
    [file join $root src kohakuaccel pe rv32 core rv_core.v] \
    [file join $root src kohakuaccel pe rv32 noc rv_noc_req.v]]

read_verilog [file join $root src kohakuaccel pe rv32 rv_pe.v]
read_xdc [file join $root scripts xdc ooc_rv_pe.xdc]

puts "@@@ top $top regfile $rfp l1_lines $lin fwd_x $fwx btb $btb imem $imw spad $spw dir $dirv period $per"

# A Verilog string parameter needs its inner quotes to survive Tcl: bare
# REGFILE_PRIM=block arrives as an identifier and is silently ignored.
synth_design -top $top -part $part -mode out_of_context \
             -flatten_hierarchy none -directive $dirv \
             -generic "REGFILE_PRIM=\"$rfp\"" \
             -generic L1_LINES=$lin -generic FWD_X=$fwx \
             -generic BTB_ENTRIES=$btb \
             -generic IMEM_WORDS=$imw -generic SPAD_WORDS=$spw

ooc_record "$top-rf$rfp-l1$lin-fx$fwx-btb$btb-im$imw-sp$spw-t$per" \
    "top=$top regfile=$rfp l1_lines=$lin fwd_x=$fwx btb=$btb imem=$imw spad=$spw dir=$dirv period=$per" \
    2000 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

puts "@@@ ============================ by primitive"
foreach p {LUT1 LUT2 LUT3 LUT4 LUT5 LUT6 FDRE FDSE FDCE FDPE RAMD32 RAMD64E \
           RAMS32 SRL16E SRLC32E RAMB36E2 RAMB18E2 MUXF7 MUXF8 CARRY8} {
    set n [llength [get_cells -hier -filter "REF_NAME == $p"]]
    if {$n} { puts [format "@@@ %-10s %8d" $p $n] }
}

puts "@@@ ============================ per unit"
set insts {u_core u_core/u_if u_core/u_if/g_bpred.u_bp u_core/u_id u_core/u_ex \
           u_core/u_mem u_core/u_wb u_core/u_rf \
           u_imem u_spad u_l1 u_req u_base}
foreach inst $insts {
    if {[llength [get_cells -quiet $inst]] == 0} {
        puts "@@@ $inst MISSING"
        continue
    }
    ooc_count $inst $inst
}

# Any array that earns a BRAM tile must fill the tile's natural depth at its
# aspect; report the depth used so that is a number, not an assertion.
puts "@@@ ============================ BRAM depth utilization"
foreach {nm words width} [list imem $imw 32 spad $spw 32 l1_data [expr {$lin * 8}] 32] {
    set bits [expr {$words * $width}]
    # RAMB36E2 at the 1K x 36 aspect a 32-bit port selects.
    set tiles [expr {int(ceil($words / 1024.0))}]
    set depth_used [expr {$tiles > 0 ? 100.0 * $words / ($tiles * 1024.0) : 0.0}]
    puts [format "@@@ bram %-8s %6d words x %2d = %7d bits, %d tile(s) at 1Kx36, depth %.1f%%, width %.1f%%" \
              $nm $words $width $bits $tiles $depth_used [expr {100.0 * $width / 36.0}]]
}

puts "@@@ ============================ control sets"
ooc_ctrlsets

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

puts "@@@ ooc_rv_pe done $top regfile $rfp l1_lines $lin"
