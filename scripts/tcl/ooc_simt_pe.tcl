# OOC synthesis of a KohakuSIMT block. SYNTH ONLY. Results are the @@@ lines.
#   vivado -mode batch -source scripts/tcl/ooc_simt_pe.tcl -tclargs \
#     <top> <lanes> <waves> <has_mask> <has_ipdom> <period_ns> \
#     <vreg_prim> <has_shfl> <flatten> <has_flt> <flanes> <has_fsfu> \
#     <has_ldsbank> <ipdom_d> <mem_prim> <inst_depth> <recv_depth> \
#     <imem_words> <spad_words> <l1_lines> <fmodel>
#
# EVERY KNOB kht_pe HAS, because a price list that covers the eight that were
# already wired is a price list of the harness rather than of the PE.
#
# Only the kht_pe branch below passes has_flt/flanes to synth_design; on the
# other tops they reach the TAG and nothing else.
#
# The ladder is generics on one module, so one script measures every gate and
# the rows are comparable to each other by construction. 3.333 ns because the
# base core's own frontier spent 350-405 LUT for zero megahertz below it.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set top  [lindex $argv 0]
set lns  [lindex $argv 1]
set wvs  [lindex $argv 2]
set msk  [lindex $argv 3]
set ipd  [lindex $argv 4]
set per  [lindex $argv 5]
set prm  [lindex $argv 6]
set shf  [lindex $argv 7]
# `rebuilt` DEFAULTS BECAUSE REBUILT IS THE SHIP. Nothing in scripts/tcl sets
# FLATTEN_HIERARCHY on the ship's synth run, so it takes Vivado's own default,
# which is rebuilt -- and `none` reads 636 LUT high on this PE (22,257 against
# 21,621). A row measured at `none` is not comparable with the record it will be
# diffed against. `none` stays reachable: it preserves every module boundary,
# which is what makes the per-block ladder attributable, and it is the DIAGNOSTIC
# rather than the answer.
set flt  [lindex $argv 8]
# G9. The float tier is the SIMD tier's arithmetic instantiated here, so turning
# it on drags vec_alu and its DSP48E2 into the read list.
set fpu  [lindex $argv 9]
set fln  [lindex $argv 10]
# THE SEED UNIT COUNT, not a boolean. 0 builds none; 1..FLANES builds that many
# seed-capable float units and every other unit loses vec_alu's HAS_POLY --
# a DSP48E2 and 1.5 BRAM apiece. Quarter rate is FSFU_UNITS = FLANES/4.
set sfu  [lindex $argv 11]
set lds  [lindex $argv 12]
set ipdd [lindex $argv 13]
set mprm [lindex $argv 14]
set idep [lindex $argv 15]
set rdep [lindex $argv 16]
set iwds [lindex $argv 17]
set swds [lindex $argv 18]
set l1ln [lindex $argv 19]
set fmdl [lindex $argv 20]
# The RV32M multiply unit count. An add-on to the integer lane, so its width is
# its own purchase and not the thread count.
set mun  [lindex $argv 21]
# APPENDED, NEVER INSERTED: existing callers' positions do not move.
set hf16 [lindex $argv 22]
set hf32 [lindex $argv 23]
if {$hf16 eq ""} { set hf16 1 }
if {$hf32 eq ""} { set hf32 1 }
set shflu [lindex $argv 24]
if {$shflu eq ""} { set shflu 0 }
set ldsb [lindex $argv 25]
if {$ldsb eq ""} { set ldsb 0 }

if {$top eq ""} { set top kht_unit }
if {$lns eq ""} { set lns 8 }
if {$wvs eq ""} { set wvs 16 }
if {$msk eq ""} { set msk 1 }
if {$ipd eq ""} { set ipd 1 }
if {$per eq ""} { set per 3.333 }
if {$prm eq ""} { set prm block }
# G8 defaults OFF so the G0-G3 ladder rows measure the gates they name and
# nothing else. The shipped PE turns it on.
if {$shf eq ""} { set shf 0 }
if {$flt eq ""} { set flt rebuilt }
if {$fpu eq ""} { set fpu 0 }
# `fpu` IS NOW ONLY A SHORTHAND: 0 zeroes the float and multiply counts, which is
# what "no float tier" means, and there is no HAS_FLT generic behind it. A row
# with fpu 1 and fln 0 is a contradiction and the count wins.
if {$fln eq ""} { set fln [expr {$fpu eq "0" ? 0 : $lns}] }
if {$sfu eq ""} { set sfu 0 }
# Every default here is kht_pe's own, so an omitted argument prices the shipped
# PE and never a configuration nobody builds.
if {$lds  eq ""} { set lds  1 }
if {$ipdd eq ""} { set ipdd 8 }
if {$mprm eq ""} { set mprm block }
if {$idep eq ""} { set idep 16 }
if {$rdep eq ""} { set rdep 512 }
if {$iwds eq ""} { set iwds 2048 }
if {$swds eq ""} { set swds 2048 }
if {$l1ln eq ""} { set l1ln 128 }
if {$fmdl eq ""} { set fmdl 0 }
if {$mun  eq ""} { set mun [expr {$fpu eq "0" ? 0 : $lns}] }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
    [file join $root src kohakuaccel common async_fifo.v] \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common sb_skid.v] \
    [file join $root src kohakuaccel noc endpoint noc_cu_base.v] \
    [file join $root src kohakuaccel pe rv32 mem rv_imem.v] \
    [file join $root src kohakuaccel pe rv32 mem rv_l1.v] \
    [file join $root src kohakuaccel pe rv32 noc rv_noc_req.v] \
    [file join $root src kohakumpe simt kht_pe.v] \
    [file join $root src kohakuaccel pe rv32 mem rv_ram_be.v] \
    [file join $root src kohakuaccel pe rv32 mem rv_spad.v] \
    [file join $root src kohakumpe simt kht_valu.v] \
    [file join $root src kohakumpe simt kht_vregfile.v] \
    [file join $root src kohakumpe simt kht_lds.v] \
    [file join $root src kohakumpe simt kht_predec.v] \
    [file join $root src kohakumpe simt kht_unit.v] \
    [file join $root src kohakumpe simt kht_core.v]]

# The multiply units are their own count now, so a float-free build with a
# multiplier still needs kht_imul -- reading only on $fpu left it out.
if {$fln ne "0" || $mun ne "0"} {
    read_verilog [list \
        [file join $root src kohakutpu matmul mx_fpacc.v] \
        [file join $root src kohakutpu vector vec_dsp.v] \
        [file join $root src kohakutpu vector vec_delay.v] \
        [file join $root src kohakutpu vector vec_tables.v] \
        [file join $root src kohakutpu vector vec_cvt.v] \
        [file join $root src kohakutpu vector vec_alu.v] \
        [file join $root src kohakumpe simd khs_float_lane.v] \
        [file join $root src kohakumpe simt kht_fpu.v] \
        [file join $root src kohakumpe simt kht_imul.v]]
}

read_xdc [file join $root scripts xdc ooc_khg.xdc]

# NOT a backslash-newline inside the quotes: Tcl collapses that to a SPACE, and a
# tag with whitespace in it splits when anything downstream parses the @@@ line.
set tag "gpu-$top-l$lns-w$wvs-m$msk-i$ipd-s$shf-$prm-f$flt-fp$fpu.$fln-sf$sfu"
append tag "-lb$lds-id$ipdd-mp$mprm-idep$idep-rd$rdep"
append tag "-iw$iwds-sw$swds-l1$l1ln-fm$fmdl-mu$mun-fmt${hf16}${hf32}"
append tag "-su${shflu}-lb${ldsb}-t$per"
puts "@@@ top $top lanes $lns waves $wvs has_mask $msk has_ipdom $ipd has_shfl $shf vreg $prm period $per"

# Only kht_unit carries the ladder generics; a leaf takes what it has.
# FLANES/MUL_UNITS reach the ladder tops too; both default to 0, so G0-G3 are
# unchanged and a float-tier saving becomes attributable rather than a total.
if {$top eq "kht_unit"} {
    synth_design -top $top -part $part -mode out_of_context \
                 -flatten_hierarchy $flt -directive default \
                 -include_dirs [file join $root src kohakumpe simt generated] \
                 -generic LANES=$lns -generic WAVES=$wvs \
                 -generic HAS_MASK=$msk -generic HAS_IPDOM=$ipd \
                 -generic HAS_SHFL=$shf -generic VREG_PRIM=$prm \
                 -generic FLANES=$fln \
                 -generic FSFU_UNITS=$sfu -generic MUL_UNITS=$mun
} elseif {$top eq "kht_pe"} {
    # THE WHOLE UNIT: core, windows, L1, requestor, fabric port. The gate ladder
    # answers "what does this cost"; only this answers "does the machine close",
    # and a top that is one submodule cannot see a path that leaves it.
    synth_design -top $top -part $part -mode out_of_context \
                 -flatten_hierarchy $flt -directive default \
                 -include_dirs [file join $root src kohakumpe simt generated] \
                 -generic LANES=$lns -generic WAVES=$wvs \
                 -generic HAS_MASK=$msk -generic HAS_IPDOM=$ipd \
                 -generic HAS_SHFL=$shf -generic VREG_PRIM=$prm \
                 -generic FLANES=$fln \
                 -generic FSFU_UNITS=$sfu -generic MUL_UNITS=$mun \
                 -generic HAS_F16=$hf16 -generic HAS_F32=$hf32 \
                 -generic SHFL_UNITS=$shflu \
                 -generic HAS_LDSBANK=$lds -generic LDS_BANKS=$ldsb \
                 -generic IPDOM_D=$ipdd \
                 -generic MEM_PRIM=$mprm -generic INST_DEPTH=$idep \
                 -generic RECV_DEPTH=$rdep -generic IMEM_WORDS=$iwds \
                 -generic SPAD_WORDS=$swds -generic L1_LINES=$l1ln \
                 -generic FMODEL=$fmdl
} elseif {$top eq "kht_core"} {
    # G7 lives in the FRONT END, which kht_unit's top cannot see. Sweeping WAVES
    # here against the same sweep on kht_unit separates scheduling from storage:
    # kht_unit measured +0 for 16 waves, so whatever this shows is the scheduler.
    synth_design -top $top -part $part -mode out_of_context \
                 -flatten_hierarchy $flt -directive default \
                 -include_dirs [file join $root src kohakumpe simt generated] \
                 -generic LANES=$lns -generic WAVES=$wvs \
                 -generic HAS_MASK=$msk -generic HAS_IPDOM=$ipd \
                 -generic HAS_SHFL=$shf -generic VREG_PRIM=$prm \
                 -generic FLANES=$fln \
                 -generic FSFU_UNITS=$sfu -generic MUL_UNITS=$mun
} elseif {$top eq "kht_lds"} {
    # G4 lives here, not in kht_unit: the resolver is LANES x LANES and the
    # return crossbar is per lane, so this is the block the gate is measured on.
    # -include_dirs EVEN THOUGH THIS LEAF HAS NO INCLUDE: the whole read list is
    # elaborated, so without it kht_predec dies on kht_isa.vh before the leaf.
    synth_design -top $top -part $part -mode out_of_context \
                 -flatten_hierarchy $flt -directive default \
                 -include_dirs [file join $root src kohakumpe simt generated] \
                 -generic LANES=$lns -generic MEM_PRIM=$prm
} elseif {$top eq "kht_vregfile"} {
    synth_design -top $top -part $part -mode out_of_context \
                 -flatten_hierarchy $flt -directive default \
                 -include_dirs [file join $root src kohakumpe simt generated] \
                 -generic LANES=$lns -generic WAVES=$wvs -generic PRIM=$prm
} else {
    synth_design -top $top -part $part -mode out_of_context \
                 -flatten_hierarchy $flt -directive default \
                 -include_dirs [file join $root src kohakumpe simt generated] \
                 -generic LANES=$lns
}

# ASSERT BEFORE TRUSTING. A Vivado timing query returns EMPTY on failure rather
# than erroring, and an empty result reads exactly like a clean design: with no
# clock, ooc_record prints no Fmax line and every LUT figure is the
# unconstrained one, so the run reports nothing wrong while measuring the wrong
# thing. This caught exactly that once.
if {[llength [get_clocks -quiet]] == 0} {
    error "no clock: ooc_khg.xdc did not apply, so every figure would be unconstrained"
}

# DEPTH 4: kht_unit is 72% of this PE, so a two-level report names the one block
# that matters and stops exactly where the question starts.
ooc_record $tag "top=$top lanes=$lns waves=$wvs mask=$msk ipdom=$ipd period=$per" \
    2000 4

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

puts "@@@ ============================ the binding path"
foreach l [split [report_timing -max_paths 1 -nworst 1 -setup -input_pins \
                      -return_string] "\n"] {
    puts "@@@P $l"
}

ooc_cones 6

if {$top eq "kht_pe"} {
    ooc_lut_census kht_core u_core 30
}

puts "@@@ ooc_simt_pe done $tag"
