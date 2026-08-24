# OOC synthesis of the ASSEMBLED SIMD PE -- the whole rv_pe with the extension
# enabled. SYNTH ONLY. Results are the @@@ lines.
#   vivado -mode batch -source scripts/tcl/ooc_simd_pe.tcl -tclargs \
#     <dsp_en> <simd> <muls> <has_shift> <has_perm> <wb> <period_ns>
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
set muls [lindex $argv 2]
set hsh  [lindex $argv 3]
set hpm  [lindex $argv 4]
set wbs  [lindex $argv 5]
set per  [lindex $argv 6]
set flt  [lindex $argv 7]
set npt  [lindex $argv 8]
if {$flt eq ""} { set flt 0 }
if {$npt eq ""} { set npt 16 }
# Float LANES against 2*simd float ELEMENTS. 0 = one per element.
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
# Cross-lane permute OUTPUT words per pass. 0 = one per word, which is every
# row measured before the width existed.
set permu [lindex $argv 19]
if {$permu eq ""} { set permu 0 }
# APPENDED, NEVER INSERTED: adding these at the END leaves every existing
# caller's positional arguments where they were.
set nacc [lindex $argv 20]
if {$nacc eq ""} { set nacc 2 }
set vregs [lindex $argv 21]
if {$vregs eq ""} { set vregs 8 }
set hf16 [lindex $argv 22]
if {$hf16 eq ""} { set hf16 1 }
set hf32 [lindex $argv 23]
if {$hf32 eq ""} { set hf32 1 }
set shiftu [lindex $argv 24]
if {$shiftu eq ""} { set shiftu 0 }

if {$den  eq ""} { set den  1 }
if {$simd eq ""} { set simd 8 }
# AFTER $simd's default, which it reads. 0 IS "NOT BUILT" now, so an omitted
# count on a float row must not mean "widest" -- it would refuse to elaborate.
if {$fln eq ""} { set fln [expr {$flt eq "0" ? 0 : 2 * $simd}] }
if {$muls eq ""} { set muls 4 }
if {$hsh  eq ""} { set hsh  1 }
if {$hpm  eq ""} { set hpm  1 }
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
    [file join $root src kohakutpu matmul mx_fpacc.v] \
    [file join $root src kohakutpu vector vec_dsp.v] \
    [file join $root src kohakutpu vector vec_delay.v] \
    [file join $root src kohakutpu vector vec_tables.v] \
    [file join $root src kohakutpu vector vec_cvt.v] \
    [file join $root src kohakutpu vector vec_alu.v] \
    [file join $root src kohakumpe simd khs_float_lane.v] \
    [file join $root src kohakumpe simd khs_falu.v] \
    [file join $root src kohakumpe simd khs_facc.v] \
    [file join $root src kohakumpe simd khs_ffold.v] \
    [file join $root src kohakumpe simd khs_scalar_decode.v] \
    [file join $root src kohakumpe simd khs_mul.v] \
    [file join $root src kohakumpe simd khs_padd32.v] \
    [file join $root src kohakumpe simd khs_pshift32.v] \
    [file join $root src kohakumpe simd khs_lane.v] \
    [file join $root src kohakumpe simd khs_perm.v] \
    [file join $root src kohakumpe simd khs_reduce.v] \
    [file join $root src kohakumpe simd khs_vregfile.v] \
    [file join $root src kohakumpe simd khs_vspad.v] \
    [file join $root src kohakumpe simd khs_unit.v] \
    [file join $root src kohakuaccel pe rv32 rv_pe.v]]

read_xdc [file join $root scripts xdc ooc_rv_pe.xdc]

# `sfuN u`, so a tag from before FSFU became a unit count cannot read as this.
set tag "simdpe-en$den-s$simd-m$muls-sh$hsh-pm$hpm.${permu}u-wb$wbs-f$flt.$fln-sfu${fsfu}u-r$rmem-d$dotd-t$per-h$flat-v$vprim-D$sdir-a${nacc}-vr${vregs}-fmt${hf16}${hf32}-shu${shiftu}"
puts "@@@ top rv_pe dsp_en $den simd $simd muls $muls shift $hsh perm $hpm wb $wbs float $flt npart $npt period $per"

synth_design -top rv_pe -part $part -mode out_of_context \
             -flatten_hierarchy $flat -directive $sdir \
             -include_dirs [file join $root src kohakumpe simd generated] \
             -generic SIMD_EN=$den -generic SIMD_LANES=$simd \
             -generic SIMD_NACC=$nacc -generic SIMD_VREGS=$vregs \
             -generic SIMD_F16=$hf16 -generic SIMD_F32=$hf32 \
             -generic SIMD_SHIFT_UNITS=$shiftu \
             -generic SIMD_MULS=$muls -generic SIMD_SHIFT=$hsh \
             -generic SIMD_PERM=$hpm -generic SIMD_PERM_UNITS=$permu \
             -generic SIMD_WB=$wbs \
             -generic SIMD_FLOAT=$flt -generic SIMD_NPART=$npt \
             -generic SIMD_FLOAT_LANES=$fln \
             -generic SIMD_FALU=$falu -generic SIMD_FSFU=$fsfu \
             -generic SIMD_FACC=$facc -generic SIMD_FCVT=$fcvt \
             -generic RECV_MEM=$rmem \
             -generic SIMD_VREG_PRIM=$vprim \
             -generic SIMD_DOTDSP=$dotd

# DEPTH 4, not 2: khs_unit is 75% of this PE, so a two-level report names the
# one block that matters and then stops exactly where the question starts.
ooc_record $tag \
    "dsp_en=$den simd=$simd muls=$muls shift=$hsh perm=$hpm wb=$wbs float=$flt period=$per" \
    2000 4

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

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

puts "@@@ ooc_simd_pe done $tag"
