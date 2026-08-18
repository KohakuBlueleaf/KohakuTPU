# Classify failing paths by what they actually drive, so "useless" is measured
# rather than asserted. Sourced with a design already open.

# path_census <outdir> ?nsample? ?ntop?

proc pc_class {ep} {
    set leaf [lindex [split $ep /] end]
    regsub {\[\d+\]$} $leaf "" leaf
    if {[regexp {^(R|S|CLR|PRE|RSTA|RSTB|RSTRAMB?|RSTRAMARSTRAM|RSTREGB?|RSTREGARSTREG|RST|RST_[A-Z]+)$} $leaf]} {
        return rst
    }
    if {[regexp {^(CE|CEA[12]|CEB[12]|CEM|CEP|CEAD|CEALUMODE|CECARRYIN|CECTRL|CEINMODE|EN|ENA|ENB|ENARDEN|ENBWREN)$} $leaf]} {
        return ce
    }
    if {[regexp {^(WE|WEA|WEB|WEBWE)$} $leaf]} { return we }
    if {[regexp {^(ADDR|ADDRA|ADDRB|ADDR_[A-Z]+)$} $leaf]} { return addr }
    return data
}

proc path_census {outdir {nsample 10000} {ntop 60}} {
    file mkdir $outdir
    set paths [get_timing_paths -max_paths $nsample -nworst 1 -delay_type max \
                                -slack_lesser_than 0 -sort_by slack -quiet]
    set n [llength $paths]

    array set wns {} ; array set tns {} ; array set cnt {}
    foreach c {data rst ce we addr} {
        set wns($c) 0.0 ; set tns($c) 0.0 ; set cnt($c) 0
    }
    set fh [open $outdir/worst_paths.tsv w]
    puts $fh "slack\tclass\tlevels\tdatapath_ns\tlogic_ns\troute_ns\troute_pct\tsrc_clk\tdst_clk\tstartpoint\tendpoint"
    set i 0
    foreach p $paths {
        set s  [get_property SLACK $p]
        set ep [get_property ENDPOINT_PIN $p]
        set c  [pc_class $ep]
        if {$s < $wns($c)} { set wns($c) $s }
        set tns($c) [expr {$tns($c) + $s}]
        incr cnt($c)
        if {[incr i] <= $ntop} {
            set dp [get_property DATAPATH_DELAY $p]
            # Absent properties ERROR rather than return empty, and lg=0 over
            # N>0 levels makes route_pct 100.0 by construction; -1 marks absent.
            set have 1
            set lv [get_property LOGIC_LEVELS $p]
            if {[catch {get_property LOGIC_DELAY $p} lg]} { set lg -1 ; set have 0 }
            # 0.000 across N>0 levels is an UNPOPULATED property, not a real
            # zero, and it is what put 100.0 on all 60 of m82_c2's rows.
            if {$have && $lg <= 0 && $lv > 0} { set lg -1 ; set have 0 }
            if {[catch {get_property NET_DELAY $p} nt]} {
                if {$have} { set nt [expr {$dp - $lg}] } else { set nt -1 }
            }
            puts $fh [format "%.3f\t%s\t%s\t%.3f\t%.3f\t%.3f\t%.1f\t%s\t%s\t%s\t%s" \
                $s $c [get_property LOGIC_LEVELS $p] $dp $lg $nt \
                [expr {$have && $dp > 0 ? 100.0 * $nt / $dp : -1}] \
                [get_property STARTPOINT_CLOCK $p] [get_property ENDPOINT_CLOCK $p] \
                [get_property STARTPOINT_PIN $p] $ep]
        }
    }
    close $fh

    # A sample, not the whole population: TNS here covers the worst $n failing
    # paths only, and says so, because the design TNS counts every endpoint.
    set fh [open $outdir/by_pin_class.tsv w]
    puts $fh "class\tworst_slack\tpaths\tsample_tns\tsampled\trequested"
    foreach c {data rst ce we addr} {
        puts $fh [format "%s\t%.3f\t%d\t%.1f\t%d\t%d" \
                         $c $wns($c) $cnt($c) $tns($c) $n $nsample]
    }
    close $fh

    # Target configs sit at 96-99% CLB, so LUT headroom gates any pipelining.
    # Only -hierarchical says which module holds them; no run report does.
    report_utilization -hierarchical -hierarchical_depth 4 \
                       -file $outdir/util_hier.rpt
    report_control_sets -verbose -file $outdir/control_sets.rpt

    set real $wns(data)
    set junk 0.0
    foreach c {rst ce we addr} { if {$wns($c) < $junk} { set junk $wns($c) } }
    puts "@@@ census sampled $n of the failing paths"
    puts "@@@ census WNS all [format %.3f [expr {$real < $junk ? $real : $junk}]] \
data-only [format %.3f $real] control-pins [format %.3f $junk]"
    foreach c {data rst ce we addr} {
        puts "@@@ census $c wns [format %.3f $wns($c)] paths $cnt($c) \
sample_tns [format %.1f $tns($c)]"
    }
}
