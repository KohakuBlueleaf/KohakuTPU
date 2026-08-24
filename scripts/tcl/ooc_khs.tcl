# OOC synthesis of the KohakuSIMD vector unit. SYNTH ONLY. Results are the @@@ lines.
#   vivado -mode batch -source scripts/tcl/ooc_khs.tcl -tclargs \
#     <simd> <vregs> <nacc> <vspad_entries> <muls> <has_shift> <has_perm> \
#     <use_dsp> <vreg_prim> <period_ns>
#
# The unit ALONE, not the assembled PE, and that is the point: the marginal cost
# of making a controller PE a SIMD PE is what the configuration matrix is about,
# and measuring it inside a 2,491-LUT core would leave every row carrying the
# same base and the same framework attach.
#
# The period is an argument because LUT is not independent of it -- the base
# core measured 92 LUT spent chasing a target it was already meeting.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set simd [lindex $argv 0]
set vreg [lindex $argv 1]
set nacc [lindex $argv 2]
set vspe [lindex $argv 3]
set muls [lindex $argv 4]
set hsh  [lindex $argv 5]
set hpm  [lindex $argv 6]
set udsp [lindex $argv 7]
set vprm [lindex $argv 8]
set per  [lindex $argv 9]
set wbs  [lindex $argv 10]
set flt  [lindex $argv 11]
set npt  [lindex $argv 12]
# The float feature groups, each its own generic so a row measures ONE of them.
# FALU is the base a SIMD PE cannot sensibly ship without; the rest are the
# additions, and the accumulator is off unless a row asks for it.
set fln  [lindex $argv 13]
set falu [lindex $argv 14]
# A UNIT COUNT, not a boolean: 0 builds no seeds, N builds N seed units out of
# FLOAT_LANES and a seed then walks 2*SIMD/N passes. Quarter rate is FLANES/4.
set fsfu [lindex $argv 15]
set facc [lindex $argv 16]
set fcvt [lindex $argv 17]
if {$wbs eq ""} { set wbs 0 }
if {$flt eq ""} { set flt 0 }
if {$npt eq ""} { set npt 16 }
if {$falu eq ""} { set falu 1 }
if {$fsfu eq ""} { set fsfu 0 }
if {$facc eq ""} { set facc 0 }
if {$fcvt eq ""} { set fcvt 0 }

if {$simd eq ""} { set simd 8 }
# AFTER $simd's default, which it reads. 0 IS "NOT BUILT" now, so an omitted
# count on a float row must not mean "widest" -- it would refuse to elaborate.
if {$fln eq ""} { set fln [expr {$flt eq "0" ? 0 : 2 * $simd}] }
if {$vreg eq ""} { set vreg 8 }
if {$nacc eq ""} { set nacc 2 }
if {$vspe eq ""} { set vspe 1024 }
if {$muls eq ""} { set muls 4 }
if {$hsh  eq ""} { set hsh  1 }
if {$hpm  eq ""} { set hpm  1 }
if {$udsp eq ""} { set udsp yes }
if {$vprm eq ""} { set vprm distributed }
if {$per  eq ""} { set per  3.333 }
set ::ooc_period $per

set_param general.maxThreads 4

source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
    [file join $root src kohakuaccel pe rv32 mem rv_ram_be.v] \
    [file join $root src kohakutpu matmul mx_fpacc.v] \
    [file join $root src kohakutpu vector vec_dsp.v] \
    [file join $root src kohakutpu vector vec_delay.v] \
    [file join $root src kohakutpu vector vec_tables.v] \
    [file join $root src kohakutpu vector vec_cvt.v] \
    [file join $root src kohakutpu vector vec_alu.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_float_lane.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_falu.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_facc.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_ffold.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_mul.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_padd32.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_pshift32.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_lane.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_perm.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_reduce.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_vregfile.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_vspad.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_unit.v]]

read_xdc [file join $root scripts xdc ooc_khs.xdc]

# `sfuN u` and not `sfuN`: FSFU is a UNIT COUNT now, so a tag from before the
# split -- where 1 meant "every lane" -- must not read as the same row.
set tag "khs-s$simd-fl$fln-v$vreg-a$nacc-e$vspe-m$muls-sh$hsh-pm$hpm-wb$wbs-f$flt-alu$falu-sfu${fsfu}u-acc$facc-cvt$fcvt-p$npt-$udsp-$vprm-t$per"
puts "@@@ top khs_unit simd $simd flanes $fln vregs $vreg nacc $nacc vspad $vspe muls $muls shift $hsh perm $hpm wb $wbs float $flt falu $falu fsfu $fsfu facc $facc fcvt $fcvt npart $npt use_dsp $udsp vreg_prim $vprm period $per"

# The include path carries the GENERATED decode header; without it the decode
# constants are undefined and elaboration fails rather than guessing.
synth_design -top khs_unit -part $part -mode out_of_context \
             -flatten_hierarchy none -directive default \
             -include_dirs [file join $root src kohakuaccel pe rv32 simd generated] \
             -generic SIMD=$simd -generic VREGS=$vreg -generic NACC=$nacc \
             -generic VSPAD_ENTRIES=$vspe -generic MULS=$muls \
             -generic HAS_SHIFT=$hsh -generic HAS_PERM=$hpm \
             -generic WB_STAGE=$wbs \
             -generic HAS_FLOAT=$flt -generic NPART=$npt \
             -generic FLOAT_LANES=$fln \
             -generic HAS_FALU=$falu -generic FSFU_UNITS=$fsfu \
             -generic HAS_FACC=$facc -generic HAS_FCVT=$fcvt \
             -generic "USE_DSP=\"$udsp\"" -generic "MEM_PRIM=\"block\"" \
             -generic "VREG_PRIM=\"$vprm\""

ooc_record $tag \
    "simd=$simd vregs=$vreg nacc=$nacc vspad=$vspe muls=$muls shift=$hsh perm=$hpm wb=$wbs use_dsp=$udsp vreg_prim=$vprm period=$per" \
    2000 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

puts "@@@ ============================ per unit"
set insts {u_vrf u_vspad u_perm u_red}
for {set i 0} {$i < $simd} {incr i} { lappend insts "g_lane\[$i\].u_lane" }
# The float tier, broken out block by block: the audit that decides whether the
# tier fits is per BLOCK, not per unit, and one lane's aligner/normaliser/round
# is where the fabric work either sits or moves onto a DSP.
if {$flt != 0} {
    # Separately gated, so each is asked for separately: a missing one reports
    # MISSING rather than silently folding into the total.
    if {$falu != 0 || $fsfu != 0} {
        lappend insts "g_fel.u_falu"
        foreach s {0 1} {
            lappend insts "g_fel.u_falu/g_lane\[$s\].u_lane" \
                          "g_fel.u_falu/g_lane\[$s\].u_lane/u_alu"
        }
    }
    if {$facc != 0} {
        lappend insts "g_float.u_facc" "g_float.u_ffold"
        foreach s {0 1} {
            lappend insts "g_float.g_flane\[$s\].u_fl" \
                          "g_float.g_flane\[$s\].u_fl/u_alu"
        }
    }
}
foreach inst $insts {
    if {[llength [get_cells -quiet $inst]] == 0} {
        puts "@@@ $inst MISSING"
        continue
    }
    ooc_count $inst $inst
}

# An array that earns a BRAM tile must fill the tile's natural depth at its
# aspect, so report the depth used rather than asserting it.
puts "@@@ ============================ BRAM depth utilization"
set tiles [expr {$simd * int(ceil($vspe / 1024.0))}]
set depth [expr {$tiles > 0 ? 100.0 * $vspe / (int(ceil($vspe / 1024.0)) * 1024.0) : 0.0}]
puts [format "@@@ bram vspad  %d rows x %d bits = %d banks of 32, %d tile(s) at 1Kx36, depth %.1f%%" \
          $vspe [expr {32 * $simd}] $simd $tiles $depth]

puts "@@@ ============================ control sets"
ooc_ctrlsets

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

# The binding path CELL BY CELL. `ooc_classify` gives the endpoints and the
# level count, which is enough to say a path is long and not enough to say
# which operation is in it -- and guessing between the shifter, the adder and
# the result mux is exactly the kind of guess that gets optimised wrongly.
puts "@@@ ============================ the binding path"
foreach l [split [report_timing -max_paths 1 -nworst 1 -setup -input_pins \
                      -return_string] "\n"] {
    puts "@@@P $l"
}

puts "@@@ ooc_khs done $tag"
