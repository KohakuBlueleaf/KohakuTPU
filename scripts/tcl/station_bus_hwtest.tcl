# Drive the station-bus validation bitstream from the JTAG console:
#   source scripts/tcl/station_bus_hwtest.tcl

# Map: top 4 address bits select the SLR, bit 16 the endpoint.
set EPS {
    slr0_wide 00000000  slr0_lite 00010000
    slr1_wide 10000000  slr1_lite 10010000
    slr2_wide 20000000  slr2_lite 20010000
    slr3_wide 30000000  slr3_lite 30010000
}

set pass 0
set fail 0

proc axi_wr {core addr data len} {
    catch {delete_hw_axi_txn wr_t}
    create_hw_axi_txn wr_t [get_hw_axis $core] -address $addr -data $data \
        -len $len -type write
    run_hw_axi wr_t
    delete_hw_axi_txn wr_t
}

proc axi_rd {core addr len} {
    catch {delete_hw_axi_txn rd_t}
    create_hw_axi_txn rd_t [get_hw_axis $core] -address $addr -len $len \
        -type read
    run_hw_axi rd_t
    set d [get_property DATA [get_hw_axi_txns rd_t]]
    delete_hw_axi_txn rd_t
    return $d
}

proc check {core name addr len data} {
    global pass fail
    axi_wr $core $addr $data $len
    set got [axi_rd $core $addr $len]
    # run_hw_axi returns the whole burst as one hex string, MSB word first.
    set exp [string tolower [string map {_ {}} $data]]
    set got [string tolower [string map {_ {}} $got]]
    if {$got eq $exp} {
        incr pass
        puts [format "PASS  %-10s %-8s @%s" $core $name $addr]
    } else {
        incr fail
        puts [format "FAIL  %-10s %-8s @%s\n  exp %s\n  got %s" \
                  $core $name $addr $exp $got]
    }
}

set cores [lsort [get_hw_axis]]
puts "@@@ hw_axi cores: $cores"
if {[llength $cores] < 3} {
    error "expected 3 JTAG masters, found [llength $cores] -- is the station-bus bitstream programmed?"
}

# Every master sees the SAME map, so do not guess which core is which: the
# order get_hw_axis returns is not the BD instantiation order.
set slot 0
foreach core $cores {
    foreach {name addr} $EPS {
        check $core $name [format %08X [expr {0x$addr + $slot*0x40}]] 1 \
            [format %08X [expr {0xDEADBE00 + $slot}]]
    }
    # A burst exercises the wide path and the 4 KB rule; the lite endpoints are
    # 32-bit and take the same burst one beat at a time.
    foreach {name addr} $EPS {
        if {![string match *_wide $name]} { continue }
        check $core $name [format %08X [expr {0x$addr + 0x100 + $slot*0x40}]] 4 \
            {11111111_22222222_33333333_44444444}
    }
    incr slot
}

set c_ctrl [lindex $cores 0]

# ---- an unmapped address must return DECERR, not hang --------------------
catch {delete_hw_axi_txn de_t}
create_hw_axi_txn de_t [get_hw_axis $c_ctrl] -address 70000000 -len 1 -type read
if {[catch {run_hw_axi de_t} msg]} {
    incr pass
    puts "PASS  decode  unmapped read reported an error response"
} else {
    incr fail
    puts "FAIL  decode  unmapped read returned OKAY: $msg"
}
delete_hw_axi_txn de_t

puts "@@@ station bus hardware test: $pass passed, $fail failed"
