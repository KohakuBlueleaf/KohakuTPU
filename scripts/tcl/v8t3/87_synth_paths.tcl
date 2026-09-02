# Every setup path under a slack margin, in one read-only open beside the
# implementation: one TSV row per endpoint, then the same paths bucketed by
# clock, by start/end register class (bit indices stripped, generate indices
# kept), by the same class merged across the four dies, by owning module and
# by logic level, and a node-by-node report for the worst path of every
# failing class. On the SYNTHESIZED design (the default) synthesis slack is
# optimistic against route, so V8_PATH_MARGIN (ns, default 1.0; `-tclargs
# margin X`) collects what the router will be handed, not only what fails
# here; hold is not reported, it is meaningless before placement. With V8_DCP
# set the census runs on that ROUTED checkpoint instead, tagged `_routed`, and
# adds the router's congestion table (report_design_analysis -congestion).
# With V8_BASE (`-tclargs base FILE`) naming an earlier run's
# *_classes_merged.txt, every merged class is set against that run in
# *_classes_diff.txt (design and clock names normalised v8tN -> vN).
#   vivado -mode batch -source scripts/tcl/v8t5_paths.tcl ?-tclargs margin 0.75?
#   vivado -mode batch -source scripts/tcl/v8t5_routed_paths.tcl -tclargs margin 0.5

if {![info exists V8_PATH_MARGIN]} { set V8_PATH_MARGIN 1.0 }
if {[set i [lsearch $argv margin]] >= 0} { set V8_PATH_MARGIN [lindex $argv [expr {$i + 1}]] }
if {[set i [lsearch $argv base]] >= 0} { set V8_BASE [lindex $argv [expr {$i + 1}]] }
if {![info exists V8_CLASS_FULL]} { set V8_CLASS_FULL 400 }
if {![info exists V8_NEAR_FULL]}  { set V8_NEAR_FULL 60 }
set MAXP 200000

if {[info exists V8_DCP]} {
    open_checkpoint $V8_DCP
    set V8_TAG routed
} else {
    open_run synth_1 -name ${design_name}_paths
    set V8_TAG synth
}
set out $root/build
file mkdir $out
set top ${design_name}_i
set tag $out/${design_name}_$V8_TAG
proc v8_t {} { return [clock format [clock seconds] -format %H:%M:%S] }
puts "@@@ $V8_TAG paths: margin $V8_PATH_MARGIN ns  [v8_t]"

# The module a pin belongs to: the top's child and two more levels, the
# module-reference `inst` skipped -- mesh_1/u_mag/u_pe, xache/u_kx/g_home[2].u_c,
# station_bus/u_line/g_stn[1].g_nsu[0].u_nsu.
proc v8_owner {name} {
    set o {}
    foreach s [lrange [split $name /] 1 end-1] {
        if {$s eq "inst" && [llength $o] == 1} { continue }
        lappend o $s
        if {[llength $o] == 3} { break }
    }
    return [join $o /]
}
# A register class: the cell without bit indices, generate indices kept, the
# pin dropped (scripts/tcl/ooc_paths.tcl's grouping).
proc v8_group {name} {
    regsub {/[A-Z0-9_]+$} $name "" name
    regsub -all {_reg(\[[0-9]+\])*$} $name "" name
    regsub -all {_rep(_[0-9]+)?$} $name "" name
    regsub -all {\[[0-9]+\](?=/|$)} $name "" name
    return $name
}
# The same class on any die: die-indexed names and generate indices to N.
proc v8_merge {key} {
    regsub -all {mesh_[0-3]} $key "mesh_N" key
    regsub -all {mesh[0-3]_0} $key "meshN_0" key
    regsub -all {mmcm_clkout([0-9]+)(_[123])?} $key "mmcm_clkout\\1_N" key
    regsub -all {(ddr4|rst_sys|rst_bus|rst_ddr4|dwc_ctrl|div2_mesh|dclr_mesh|bus_rst_inv|clk_wiz_mesh|c)_?[0-3](_|/|$)} $key "\\1_N\\2" key
    regsub -all {\[[0-9]+\]} $key "\[N\]" key
    return $key
}
# Accumulate one path into table T under key K: count, failing count, worst
# slack and the levels / delays / path object at that worst.
proc v8_acc {T k sl lv dp ld nd po} {
    upvar #0 ${T}_cnt cnt ${T}_fail fail ${T}_worst worst ${T}_lv lvv ${T}_dp dpv \
        ${T}_ld ldv ${T}_nd ndv ${T}_po pov ${T}_tns tns
    if {![info exists cnt($k)]} { set cnt($k) 0 ; set fail($k) 0 ; set tns($k) 0.0 }
    incr cnt($k)
    if {$sl < 0} { incr fail($k) ; set tns($k) [expr {$tns($k) + $sl}] }
    if {![info exists worst($k)] || $sl < $worst($k)} {
        set worst($k) $sl ; set lvv($k) $lv ; set dpv($k) $dp ; set ldv($k) $ld
        set ndv($k) $nd ; set pov($k) $po
    }
}
proc v8_rows {T} {
    upvar #0 ${T}_cnt cnt ${T}_fail fail ${T}_worst worst ${T}_lv lvv ${T}_dp dpv \
        ${T}_ld ldv ${T}_nd ndv ${T}_po pov ${T}_tns tns
    set rows {}
    foreach k [array names cnt] {
        lappend rows [list $worst($k) $cnt($k) $fail($k) $tns($k) $lvv($k) $dpv($k) $ldv($k) $ndv($k) $k $pov($k)]
    }
    return [lsort -real -index 0 $rows]
}
proc v8_table {fh title rows} {
    puts $fh $title
    puts $fh [format "%8s %7s %6s %9s %4s %7s %6s %6s  %s" worst n fail tns lvl dpath logic net "clock | start -> end"]
    foreach r $rows {
        puts $fh [format "%8.3f %7d %6d %9.3f %4d %7.3f %6.3f %6.3f  %s" \
            [lindex $r 0] [lindex $r 1] [lindex $r 2] [lindex $r 3] [lindex $r 4] \
            [lindex $r 5] [lindex $r 6] [lindex $r 7] [string map {"\t" " | "} [lindex $r 8]]]
    }
    puts $fh ""
}

# ---- 1. collect: every clock, every endpoint under the margin ------------
set COLS {SLACK LOGIC_LEVELS REQUIREMENT DATAPATH_DELAY DATAPATH_LOGIC_DELAY DATAPATH_NET_DELAY SKEW STARTPOINT_CLOCK ENDPOINT_CLOCK}
set tsv [open ${tag}_paths.tsv w]
puts $tsv [join {CLOCK SLACK LEVELS REQ DPATH LOGIC NET SKEW S_CLK E_CLK S_OWNER E_OWNER S_CLASS E_CLASS START END} \t]
set N 0
set PC {}
array set LV {}
foreach c [lsort -dictionary [get_property NAME [get_clocks]]] {
    set ps [get_timing_paths -quiet -delay_type max -max_paths $MAXP -nworst 1 \
                -slack_lesser_than $V8_PATH_MARGIN -to [get_clocks $c]]
    set n [llength $ps]
    if {!$n} { continue }
    foreach col $COLS { set V($col) [get_property -quiet $col $ps] }
    set fail 0 ; set tns 0.0 ; set u25 0 ; set u50 0 ; set u75 0 ; set wns 99.0
    for {set i 0} {$i < $n} {incr i} {
        set p  [lindex $ps $i]
        set sp [get_property NAME [get_property STARTPOINT_PIN $p]]
        set ep [get_property NAME [get_property ENDPOINT_PIN $p]]
        set sl [lindex $V(SLACK) $i]
        set lv [lindex $V(LOGIC_LEVELS) $i]
        set rq [lindex $V(REQUIREMENT) $i]
        set dp [lindex $V(DATAPATH_DELAY) $i]
        set ld [lindex $V(DATAPATH_LOGIC_DELAY) $i]
        set nd [lindex $V(DATAPATH_NET_DELAY) $i]
        set sk [lindex $V(SKEW) $i]
        set sc [lindex $V(STARTPOINT_CLOCK) $i]
        set ec [lindex $V(ENDPOINT_CLOCK) $i]
        set so [v8_owner $sp] ; set eo [v8_owner $ep]
        set sg [v8_group $sp] ; set eg [v8_group $ep]
        puts $tsv [join [list $c $sl $lv $rq $dp $ld $nd $sk $sc $ec $so $eo $sg $eg $sp $ep] \t]
        set k "$c\t$sg -> $eg"
        v8_acc CL $k $sl $lv $dp $ld $nd $p
        v8_acc CM [v8_merge $k] $sl $lv $dp $ld $nd $p
        v8_acc OW "$c\t$eo" $sl $lv $dp $ld $nd $p
        v8_acc OM [v8_merge "$c\t$eo"] $sl $lv $dp $ld $nd $p
        v8_acc OP "$c\t$so -> $eo" $sl $lv $dp $ld $nd $p
        incr LV($lv,[expr {$sl < 0 ? "fail" : "near"}])
        if {$sl < 0} { incr fail ; set tns [expr {$tns + $sl}] }
        if {$sl < 0.25} { incr u25 }
        if {$sl < 0.50} { incr u50 }
        if {$sl < 0.75} { incr u75 }
        if {$sl < $wns} { set wns $sl }
    }
    incr N $n
    lappend PC [list $c $rq $wns $tns $fail $u25 $u50 $u75 $n]
    puts [format "  %-52s req %6.3f WNS %+7.3f TNS %9.3f fail %6d  <0.25 %6d  <0.5 %6d  <0.75 %6d  <%.2f %6d  %s" \
          $c $rq $wns $tns $fail $u25 $u50 $u75 $V8_PATH_MARGIN $n [v8_t]]
}
close $tsv
puts "@@@ $V8_TAG paths: $N endpoints under $V8_PATH_MARGIN ns in ${tag}_paths.tsv  [v8_t]"

# ---- 2. the tables --------------------------------------------------------
set fh [open ${tag}_perclock.txt w]
puts $fh "$V8_TAG setup paths under $V8_PATH_MARGIN ns, per clock (nworst 1 = one path per endpoint)"
puts $fh [format "%-52s %6s %8s %10s %6s %6s %6s %6s %7s" clock req WNS TNS fail "<0.25" "<0.50" "<0.75" "<margin"]
foreach r [lsort -real -index 2 $PC] {
    puts $fh [format "%-52s %6.3f %+8.3f %10.3f %6d %6d %6d %6d %7d" {*}[lrange $r 0 8]]
}
close $fh

set CLR [v8_rows CL]
set fh [open ${tag}_classes.txt w]
v8_table $fh "every class (clock, start register class -> end register class) with a path under $V8_PATH_MARGIN ns; [llength $CLR] classes, $N endpoints" $CLR
close $fh
set CMR [v8_rows CM]
set fh [open ${tag}_classes_merged.txt w]
v8_table $fh "the same classes merged across dies (mesh_N, \[N\]); [llength $CMR] classes" $CMR
close $fh
# ---- 2b. against a baseline census (an earlier run's *_classes_merged.txt):
# dTNS and dWNS are here minus there, so negative means worse here; classes
# only here are NEW, the baseline's missing ones are listed last as gone.
proc v8_norm {k} { regsub -all {v8t[0-9]+} $k "vN" k ; return $k }
if {[info exists V8_BASE] && [file exists $V8_BASE] && [catch {
    array set B {}
    set bf [open $V8_BASE r]
    while {[gets $bf line] >= 0} {
        if {[regexp {^\s*(-?[0-9.]+)\s+(\d+)\s+(\d+)\s+(-?[0-9.]+)\s+(\d+)\s+(-?[0-9.]+)\s+(-?[0-9.]+)\s+(-?[0-9.]+)\s+(.*\S)\s*$} $line -> w n f t lv dp ld nd key]} {
            set B([v8_norm $key]) [list $w $n $f $t]
        }
    }
    close $bf
    set drows {} ; set gone {}
    array set seen {}
    foreach r $CMR {
        set key [v8_norm [string map {"\t" " | "} [lindex $r 8]]]
        set seen($key) 1
        lassign $r w n f t
        if {[info exists B($key)]} {
            lassign $B($key) bw bn bfl bt
            lappend drows [list [expr {$t - $bt}] [expr {$w - $bw}] $w $n $f $t $bw $bn $bfl $bt $key]
        } else {
            lappend drows [list $t $w $w $n $f $t - - - - "NEW $key"]
        }
    }
    foreach key [array names B] {
        if {[info exists seen($key)]} { continue }
        lassign $B($key) bw bn bfl bt
        lappend gone [list [expr {0.0 - $bt}] $bw $bn $bfl $bt $key]
    }
    set sd [lsort -real -index 0 $drows]
    set fh [open ${tag}_classes_diff.txt w]
    puts $fh "merged classes against $V8_BASE: [llength $drows] classes here ([llength $gone] only in the baseline); dTNS = tns here - tns there, negative = worse here"
    puts $fh [format "%9s %7s | %8s %7s %6s %9s | %8s %7s %6s %9s  %s" dTNS dWNS worst n fail tns b_worst b_n b_fail b_tns "clock | start -> end"]
    foreach r $sd {
        lassign $r dt dw w n f t bw bn bfl bt key
        if {$bw eq "-"} {
            puts $fh [format "%9.3f %7.3f | %8.3f %7d %6d %9.3f | %8s %7s %6s %9s  %s" $dt $dw $w $n $f $t - - - - $key]
        } else {
            puts $fh [format "%9.3f %7.3f | %8.3f %7d %6d %9.3f | %8.3f %7d %6d %9.3f  %s" $dt $dw $w $n $f $t $bw $bn $bfl $bt $key]
        }
    }
    puts $fh ""
    puts $fh "only in the baseline, worst first (dTNS = 0 - b_tns)"
    foreach r [lsort -real -index 1 $gone] {
        lassign $r dt bw bn bfl bt key
        puts $fh [format "%9.3f %7s | %8s %7s %6s %9s | %8.3f %7d %6d %9.3f  %s" $dt - - - - - $bw $bn $bfl $bt $key]
    }
    close $fh
    puts "@@@ diff against $V8_BASE in ${tag}_classes_diff.txt  [v8_t]"
    puts "\n=== against the baseline: 30 worse here, then 30 better here (by dTNS) ==="
    foreach r [concat [lrange $sd 0 29] [lreverse [lrange $sd end-29 end]]] {
        lassign $r dt dw w n f t bw bn bfl bt key
        puts [format "  dTNS %9.3f dWNS %+7.3f  now %+7.3f n %5d fail %5d  base %8s n %5s fail %5s  %s" $dt $dw $w $n $f $bw $bn $bfl $key]
    }
} err]} { puts "@@@ diff against $V8_BASE FAILED: $err" }

set fh [open ${tag}_owners.txt w]
v8_table $fh "by END owner (clock | module), three levels deep" [v8_rows OW]
v8_table $fh "by END owner merged across dies" [v8_rows OM]
v8_table $fh "by owner PAIR (clock | start module -> end module): a pair with two modules is a boundary" [v8_rows OP]
close $fh
set fh [open ${tag}_levels.txt w]
puts $fh "logic levels of the endpoints under $V8_PATH_MARGIN ns: failing / near (0 <= slack < margin)"
set lvs {}
foreach k [array names LV] { lappend lvs [lindex [split $k ,] 0] }
foreach lv [lsort -integer -unique $lvs] {
    set f [expr {[info exists LV($lv,fail)] ? $LV($lv,fail) : 0}]
    set m [expr {[info exists LV($lv,near)] ? $LV($lv,near) : 0}]
    puts $fh [format "%3d levels: %7d failing %7d near" $lv $f $m]
}
close $fh
puts "@@@ tables written  [v8_t]"

puts "\n=== failing classes, worst first (all of them) ==="
set nf 0
foreach r $CLR {
    if {[lindex $r 0] >= 0} { break }
    incr nf
    if {$nf <= 60} {
        puts [format "  %+7.3f n %5d lv %2d dp %6.3f  %s" [lindex $r 0] [lindex $r 1] [lindex $r 4] [lindex $r 5] [string map {"\t" " | "} [lindex $r 8]]]
    }
}
puts "@@@ failing classes: $nf of [llength $CLR]"
puts "\n=== merged across dies, worst 40 ==="
foreach r [lrange $CMR 0 39] {
    puts [format "  %+7.3f n %5d fail %5d lv %2d dp %6.3f  %s" [lindex $r 0] [lindex $r 1] [lindex $r 2] [lindex $r 4] [lindex $r 5] [string map {"\t" " | "} [lindex $r 8]]]
}
puts "\n=== by end owner, worst 25 ==="
foreach r [lrange [v8_rows OW] 0 24] {
    puts [format "  %+7.3f n %6d fail %5d  %s" [lindex $r 0] [lindex $r 1] [lindex $r 2] [string map {"\t" " | "} [lindex $r 8]]]
}

# ---- 3. node by node: the worst path of every failing class, then the near ones
proc v8_full {file rows cap label} {
    file delete -force $file
    set idx [open "${file}.index" w]
    puts $idx "$label: one report_timing block per class, in this order"
    set n 0
    foreach r $rows {
        if {$n >= $cap} { break }
        incr n
        puts $idx [format "%4d %+8.3f n %6d  %s" $n [lindex $r 0] [lindex $r 1] [string map {"\t" " | "} [lindex $r 8]]]
        report_timing -quiet -of_objects [lindex $r 9] -input_pins -append -file $file
    }
    close $idx
    return $n
}
set failing {} ; set near {}
foreach r $CLR {
    if {[lindex $r 0] < 0} { lappend failing $r } else { lappend near $r }
}
set n1 [v8_full ${tag}_classes_full.rpt $failing $V8_CLASS_FULL "failing classes, worst first"]
puts "@@@ node-by-node: $n1 failing classes in ${tag}_classes_full.rpt  [v8_t]"
set n2 [v8_full ${tag}_near_full.rpt $near $V8_NEAR_FULL "near-failing classes (0 <= slack < $V8_PATH_MARGIN), worst first"]
puts "@@@ node-by-node: $n2 near classes in ${tag}_near_full.rpt  [v8_t]"
report_timing -quiet -max_paths 500 -nworst 1 -delay_type max -slack_lesser_than 0 \
    -input_pins -file ${tag}_failing500_full.rpt
if {$V8_TAG eq "routed"} {
    report_design_analysis -congestion -file ${tag}_congestion.rpt
    puts "@@@ congestion table in ${tag}_congestion.rpt  [v8_t]"
}
puts "@@@ $V8_TAG paths done  [v8_t]"
