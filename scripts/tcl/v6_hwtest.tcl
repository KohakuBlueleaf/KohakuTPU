# Drive the v6 station-bus bitstream from the JTAG console:
#   source scripts/tcl/v6_hwtest.tcl

# Station s owns the top 4 address bits; port p is bit 16. 4 stations x 4 ports.
# Shifts follow AW: an address assuming 40 lands inside station 0, silently.
set NQ 4
set AW 43
set STN_SH [expr {$AW - 4}]
set AHEX   [expr {($AW + 3) / 4}]

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
    set got [string tolower [string map {_ {}} [axi_rd $core $addr $len]]]
    set exp [string tolower [string map {_ {}} $data]]
    if {$got eq $exp} {
        incr pass
        puts [format "PASS  %-10s %-10s @%s" $core $name $addr]
    } else {
        incr fail
        puts [format "FAIL  %-10s %-10s @%s\n  exp %s\n  got %s" \
                  $core $name $addr $exp $got]
    }
}

set cores [lsort [get_hw_axis]]
puts "@@@ hw_axi cores: $cores"
if {[llength $cores] < 3} {
    error "expected 3 JTAG masters, found [llength $cores] -- is the v6 bitstream programmed?"
}

# Every master sees the SAME map, so do not guess which core is which: the
# order get_hw_axis returns is not the BD instantiation order.
set slot 0
foreach core $cores {
    for {set s 0} {$s < 4} {incr s} {
        for {set p 0} {$p < $NQ} {incr p} {
            set base [expr {($s << $STN_SH) | ($p << 16)}]
            set a [format %0${AHEX}X [expr {$base + 0x40 + $slot*0x40}]]
            check $core "stn${s}_p${p}" $a 1 \
                [format %08X [expr {0xC0DE0000 + ($s << 8) + $p}]]
        }
    }
    incr slot
}

# A burst through the wide path into each station's 256-bit port 0.
set c_wide [lindex $cores 1]
for {set s 0} {$s < 4} {incr s} {
    set a [format %0${AHEX}X [expr {($s << $STN_SH) + 0x400}]]
    check $c_wide "stn${s}_burst" $a 4 \
        {11111111_22222222_33333333_44444444}
}

# An unmapped station must return an error rather than hang the fabric.
catch {delete_hw_axi_txn de_t}
# Top bit, above the 2-bit station field, so no segment can match it. A literal
# taken from the AW=40 map decodes to station 0 port 0 and quietly succeeds.
create_hw_axi_txn de_t [get_hw_axis [lindex $cores 0]] \
    -address [format %0${AHEX}X [expr {1 << ($AW - 1)}]] -len 1 -type read
if {[catch {run_hw_axi de_t} msg]} {
    incr pass
    puts "PASS  decode     unmapped read reported an error response"
} else {
    incr fail
    puts "FAIL  decode     unmapped read returned OKAY: $msg"
}
delete_hw_axi_txn de_t

puts "@@@ v6 station bus hardware test: $pass passed, $fail failed"
