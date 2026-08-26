# OOC synthesis of the ASSEMBLED SIMD PE -- the whole rv_pe with the extension
# enabled. SYNTH ONLY. Results are the @@@ lines.
#   vivado -mode batch -source scripts/tcl/ooc_simd_pe.tcl -tclargs \
#     <dsp_en> <simd> <ilanes> <red> <reserved> <wb> <period_ns> ...
#     ... <permu at 19> ... <shiftu at 24> <generics at 25>
#
# POSITIONS 2 AND 3 CHANGED MEANING when every compute feature became a width:
# they were <muls> and <has_shift>, and are now the integer-lane COUNT and the
# reduce COUNT. 0 means NOT BUILT. Positions 4 and 11 are reserved -- they held
# <has_perm> and <dot_dsp>, both of which are gone.
#
# The unit measured alone says what the extension costs; THIS says what the PE
# runs at, which is the number that decides whether a SIMD PE can sit on the
# mesh clock. They are not the same: integration adds the vector stall into the
# MEM stage's own and the reduction's word into the writeback mux, and either
# could be the limit even when the unit alone is not.
#
# At SIMD_EN = 0 it is the shipped controller PE, so the same script measures the
# baseline the extension is charged against.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set den  [lindex $argv 0]
set simd [lindex $argv 1]
set ilan [lindex $argv 2]
set red  [lindex $argv 3]
set rsv4 [lindex $argv 4]
set wbs  [lindex $argv 5]
set per  [lindex $argv 6]
set flt  [lindex $argv 7]
set npt  [lindex $argv 8]
if {$flt eq ""} { set flt 0 }
if {$npt eq ""} { set npt 16 }
# Float LANES. 0 = NOT BUILT, -1 = one per element.
set fln  [lindex $argv 9]
# The receive queue's storage, so its 248 LUTRAM is a priced trade against BRAM
# rather than a default nobody chose.
set rmem [lindex $argv 10]
if {$rmem eq ""} { set rmem distributed }
set dotd [lindex $argv 11]
if {$dotd eq ""} { set dotd 0 }
# The float groups, each its own generic so a row prices ONE of them.
set falu [lindex $argv 12]
set fsfu [lindex $argv 13]
set facc [lindex $argv 14]
set fcvt [lindex $argv 15]
if {$falu eq ""} { set falu 1 }
# A UNIT COUNT, not a boolean: 0 builds no seeds, N builds N seed units out of
# SIMD_FLOAT_LANES. A row from before the split, where 1 meant "every lane", is
# NOT comparable -- that configuration is now fsfu = the float lane count.
if {$fsfu eq ""} { set fsfu 0 }
if {$facc eq ""} { set facc 0 }
if {$fcvt eq ""} { set fcvt 0 }
# `rebuilt` IS WHAT THE SHIP SYNTHESISES AT -- nothing in scripts/tcl sets
# FLATTEN_HIERARCHY on the ship run, so it takes Vivado's own default, which is
# `rebuilt`. This defaulted to `none` and said so in a comment claiming `none`
# ships; that was false and it is why two campaigns measured a PE the ship does
# not build. `none` stays available as a DIAGNOSTIC -- it preserves the
# boundaries the census names -- but a `none` row and a `rebuilt` row must never
# share a table without a column saying which.
set flat [lindex $argv 16]
if {$flat eq ""} { set flat rebuilt }
# The VECTOR FILE's storage, priced the same way the receive queue's is: 8
# entries of VW bits is 1.5% of a BRAM's depth, so this is a LUT-for-BRAM
# trade and not a free choice.
set vprim [lindex $argv 17]
if {$vprim eq ""} { set vprim distributed }
# `default` ships; an area directive is a real option for this design and not a
# measurement artefact, so it is an argument rather than an edit.
set sdir [lindex $argv 18]
if {$sdir eq ""} { set sdir default }
# Cross-lane permute OUTPUT words per pass. 0 = NOT BUILT, -1 = one per word.
set permu [lindex $argv 19]
if {$permu eq ""} { set permu 8 }
# APPENDED, NEVER INSERTED: adding these at the END leaves every existing
# caller's positional arguments where they were.
set nacc [lindex $argv 20]
if {$nacc eq ""} { set nacc 2 }
set vregs [lindex $argv 21]
if {$vregs eq ""} { set vregs 8 }
# THE TWO FORMAT KNOBS ARE GONE from positions 22 and 23: FP32 is the only
# compute type, so a row from before this shifts by two here.
set shiftu [lindex $argv 22]
if {$shiftu eq ""} { set shiftu 8 }
# EVERY FURTHER KNOB GOES HERE, not in a 24th positional. `+`-separated
# NAME:VALUE, the same spelling the rest of this tree uses -- Vivado's .bat
# wrapper splits arguments on `=`, which is why it is `:` and not `=`.
#   -tclargs 1 8 4 1 1 0 3.333 1 16 4 distributed 0 1 1 0 0 rebuilt \
#            distributed default 0 2 8 8 SIMD_RED:0+SIMD_SHROUND:0
set xgen [lindex $argv 23]
set xlist {}
set named_shround 0
if {$xgen ne ""} {
    foreach kv [split $xgen "+"] {
        set p [split $kv ":"]
        if {[llength $p] != 2} { error "bad generic '$kv': want NAME:VALUE" }
        if {[lindex $p 0] eq "SIMD_SHROUND"} { set named_shround 1 }
        lappend xlist -generic [lindex $p 0]=[lindex $p 1]
    }
}

# The round adder lives INSIDE the shifter and rv_pe defaults it to 1, so a
# shiftu=0 row was refused at elaboration -- including the all-zero base.
if {!$named_shround} {
    lappend xlist -generic SIMD_SHROUND=[expr {$shiftu != 0}]
}

if {$den  eq ""} { set den  1 }
if {$simd eq ""} { set simd 8 }
# AFTER $simd's default, which it reads. 0 IS "NOT BUILT" now, so an omitted
# count on a float row must not mean "widest" -- it would refuse to elaborate.
if {$fln eq ""} { set fln [expr {$flt eq "0" ? 0 : 2 * $simd}] }
if {$ilan eq ""} { set ilan 8 }
if {$red  eq ""} { set red  1 }
if {$wbs  eq ""} { set wbs  0 }
if {$per  eq ""} { set per  3.333 }
set ::ooc_period $per

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
    [file join $root src kohakuaccel pe rv32 noc rv_noc_req.v] \
    [file join $root src kohakuaccel pe rv32 core rv_fpu.v] \
    [file join $root src kohakumpe simd khs_lead1.v] \
    [file join $root src kohakumpe simd generated khs_seed_tab.v] \
    [file join $root src kohakumpe simd khs_fp32_sfu.v] \
    [file join $root src kohakumpe simd khs_fp32_alu.v] \
    [file join $root src kohakumpe simd khs_facc.v] \
    [file join $root src kohakumpe simd khs_ffold.v] \
    [file join $root src kohakumpe simd khs_scalar_decode.v] \
    [file join $root src kohakumpe simd khs_mul.v] \
    [file join $root src kohakumpe simd khs_padd32.v] \
    [file join $root src kohakumpe simd khs_pshift32.v] \
    [file join $root src kohakumpe simd khs_lane.v] \
    [file join $root src kohakumpe simd khs_fcvt.v] \
    [file join $root src kohakumpe simd khs_perm.v] \
    [file join $root src kohakumpe simd khs_reduce.v] \
    [file join $root src kohakumpe simd khs_vregfile.v] \
    [file join $root src kohakumpe simd khs_vspad.v] \
    [file join $root src kohakumpe simd khs_unit.v] \
    [file join $root src kohakuaccel pe rv32 rv_pe.v]]

read_xdc [file join $root scripts xdc ooc_rv_pe.xdc]

# `sfuN u`, so a tag from before FSFU became a unit count cannot read as this.
set xtag ""
if {$xgen ne ""} { set xtag "-[string map {: {} + -} $xgen]" }
set tag "simdpe-en$den-s$simd-il${ilan}-shu${shiftu}-pmu${permu}-r${red}-wb$wbs-f$flt.$fln-sfu${fsfu}u-r$rmem-t$per-h$flat-v$vprim-D$sdir-a${nacc}-vr${vregs}$xtag"
puts "@@@ top rv_pe dsp_en $den simd $simd ilanes $ilan shiftu $shiftu permu $permu red $red wb $wbs float $flt flanes $fln npart $npt period $per"

synth_design -top rv_pe -part $part -mode out_of_context \
             -flatten_hierarchy $flat -directive $sdir \
             -include_dirs [file join $root src kohakumpe simd generated] \
             -generic SIMD_EN=$den -generic SIMD_LANES=$simd \
             -generic SIMD_NACC=$nacc -generic SIMD_VREGS=$vregs \
             -generic SIMD_SHIFT_UNITS=$shiftu \
             -generic SIMD_ILANES=$ilan -generic SIMD_RED=$red \
             -generic SIMD_PERM_UNITS=$permu \
             -generic SIMD_WB=$wbs \
             -generic SIMD_NPART=$npt \
             -generic SIMD_FLOAT_LANES=[expr {$flt ? $fln : 0}] \
             -generic SIMD_FALU=$falu -generic SIMD_FSFU=$fsfu \
             -generic SIMD_FACC=$facc -generic SIMD_FCVT=$fcvt \
             -generic RECV_MEM=$rmem \
             -generic SIMD_VREG_PRIM=$vprim {*}$xlist

# DEPTH 4, not 2: khs_unit is 75% of this PE, so a two-level report names the
# one block that matters and then stops exactly where the question starts.
# EVERY knob: the collector's columns come from here, and what was missing it
# read from tclargs POSITIONS -- which still named 2 and 3 `muls` and `hsh`.
# LEAN = AREA ONLY. The forensics below materialise up to 200,000 timing-path
# OBJECTS (ooc_cones) plus 2000 per clock twice; twelve concurrent jobs of that
# on a 3.8M-LUT part stalled the machine. Unset, nothing here changes.
set lean 0
if {[info exists ::env(KOHAKU_OOC_LEAN)]} {
    if {$::env(KOHAKU_OOC_LEAN) ne "0"} { set lean 1 }
}

# ooc_record's Fmax loop keeps only the MINIMUM-slack path, so one path per clock
# is the same number 2000 were. Depth 4 -> 2 for the same reason.
ooc_record $tag \
    "dsp_en=$den simd=$simd ilanes=$ilan shiftu=$shiftu permu=$permu red=$red\
 wb=$wbs float=$flt flanes=$fln npart=$npt falu=$falu fsfu=$fsfu facc=$facc\
 fcvt=$fcvt nacc=$nacc vregs=$vregs rmem=$rmem\
 vprim=$vprim flat=$flat sdir=$sdir period=$per xgen=[expr {$xgen eq "" ? "-" : $xgen}]" \
    [expr {$lean ? 1 : 2000}] [expr {$lean ? 2 : 4}]

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

if {!$lean} {
    puts "@@@ ============================ per unit"
    foreach inst {u_core u_core/u_if u_core/u_id u_core/u_ex u_core/u_mem \
                  u_core/u_rf u_core/g_simd.u_khs u_imem u_spad u_l1 u_req u_base} {
        if {[llength [get_cells -quiet $inst]] == 0} {
            puts "@@@ $inst MISSING"
            continue
        }
        ooc_count $inst $inst
    }

    puts "@@@ ============================ khs_unit LUT census"
    ooc_lut_census khs_unit u_core/g_simd.u_khs 30

    puts "@@@ ============================ control sets"
    ooc_ctrlsets

    puts "@@@ ============================ Fmax per clock"
    ooc_classify 2000

    puts "@@@ ============================ the binding path"
    foreach l [split [report_timing -max_paths 1 -nworst 1 -setup -input_pins \
                          -return_string] "\n"] {
        puts "@@@P $l"
    }

    # The SAME instrument the SIMT PE's campaign was driven by, so "does this fix
    # transfer to the DSP" is a measurement rather than an argument.
    ooc_cones 6
}

puts "@@@ ooc_simd_pe done $tag"
