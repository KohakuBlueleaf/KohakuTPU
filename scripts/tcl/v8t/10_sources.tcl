# RTL for multimesh v8t. Named, not globbed: a stray file under src/ must not
# silently enter the build. No mesh, so no router, cluster or vector core is
# here -- a node top that referenced one would fail to elaborate.

set V8_SOURCES {
    src/kohakuaccel/common/sync_fifo.v
    src/kohakuaccel/common/async_fifo.v
    src/kohakuaccel/common/kohaku_sdpram.v
    src/kohakuaccel/common/kohaku_sdpram_be.v
    src/kohakuaccel/common/kh_rst_sync.v

    src/kohakuaccel/common/sb_skid.v
    src/kohakuaccel/axi/station/sb_hub.v
    src/kohakuaccel/axi/station/sb_nmu.v
    src/kohakuaccel/axi/station/sb_nsu.v
    src/kohakuaccel/axi/station/sb_axi2lite.v
    src/kohakuaccel/axi/link/sb_link.v
    src/kohakuaccel/axi/link/sb_link_cdc.v
    src/kohakuaccel/axi/topo/sb_stn_line.v
    src/kohakuaccel/axi/topo/sb_line4.v
    src/kohakuaccel/axi/bd/sb_v6_bus.v

    src/kohakuaxi/xache/edge/kx_scdc.v
    src/kohakuaxi/xache/edge/kx_link.v
    src/kohakuaxi/xache/edge/kx_perm.v
    src/kohakuaxi/xache/array/kx_carray.v
    src/kohakuaxi/xache/engine/kx_rd_pipe.v
    src/kohakuaxi/xache/engine/kx_wr_engine.v
    src/kohakuaxi/pxache/lane/kx_hop.v
    src/kohakuaxi/pxache/lane/kx_lane.v
    src/kohakuaxi/pxache/kx_pxache.v
    xilinx-fpga/xcvu13p/bd/kx_pbd_4x4.v
    xilinx-fpga/xcvu13p/bd/xcvu13p_rst_tree.v

    src/kohakuaccel/noc/router/noc_inport.v
    src/kohakuaccel/noc/router/noc_outport.v
    src/kohakuaccel/noc/router/noc_router.v
    src/kohakuaccel/noc/endpoint/noc_cu_base.v
    src/kohakuaccel/noc/ctrl/noc_orchestrator.v
    src/kohakuaccel/noc/endpoint/noc_local_cdc.v

    src/kohakuaccel/sysnode/mover/mx_tdesc.v
    src/kohakutpu/transform/mx_quant.v
    src/kohakuaccel/sysnode/core/mag_mem_port.v
    src/kohakuaccel/sysnode/core/mag_stage.v
    src/kohakuaccel/sysnode/mover/mm_prng.v
    src/kohakuaccel/sysnode/mover/mm_mover.v
    src/kohakutransmit/prim/kts_fifo.v
    src/kohakutransmit/link/kts_tx.v
    src/kohakutransmit/link/kts_rx.v
    src/kohakutransmit/carrier/kts_pipe.v
    src/kohakuaccel/sysnode/interlink/il_pkt_arb.v
    src/kohakuaccel/sysnode/interlink/mag_link.v
    src/kohakuaccel/sysnode/interlink/mag_switch.v
    src/kohakuaccel/sysnode/interlink/mag_ilink.v
    src/kohakuaccel/sysnode/core/mag_xform.v
    src/kohakutpu/transform/xform_bank.v
    src/kohakuaccel/sysnode/core/mag.v
    src/kohakuaccel/sysnode/core/mag_dram_port.v

    src/kohakuaccel/pe/rv64-sys/core/rv64_regfile.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_alu.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_muldiv.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_bpred.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_decode.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_csr.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_core.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_ram_be.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_l1.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_icache.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_mmu.v
    src/kohakuaccel/pe/rv64-sys/core/rv64_nport.v
    src/kohakuaccel/pe/rv64-sys/rv64_noc_mbox.v
    src/kohakuaccel/pe/rv64-sys/rv64_syscore.v
    src/kohakuaccel/sysnode/mover/mv_exec.v
    src/kohakuaccel/sysnode/cpu/rv64_mag_pe.v
    src/kohakuaccel/sysnode/cpu/rv64_load_win.v
    src/kohakuaccel/sysnode/cpu/rv64_load_axi.v
    src/kohakuaccel/axi/station/sb_axi_deconcentrate.v

    src/kohakuaccel/sysnode/core/sn_hub.v
    src/kohakuaccel/sysnode/sysnode.v
    xilinx-fpga/xcvu13p/bd/ktpu_node_v8t.v
}

set v8_missing {}
foreach f $V8_SOURCES {
    if {[file exists $root/$f]} {
        add_files -norecurse -fileset sources_1 $root/$f
    } else {
        lappend v8_missing $f
    }
}
if {[llength $v8_missing]} {
    error "v8t sources missing: [join $v8_missing {, }]"
}
update_compile_order -fileset sources_1
puts "@@@ v8t sources: [llength $V8_SOURCES] files"
