# RTL for multimesh v6. Order is irrelevant; update_compile_order sorts it.

set V6_SOURCES {
    src/common/sync_fifo.v
    src/common/async_fifo.v
    src/common/kohaku_sdpram.v

    src/kohakuaxi/station/sb_skid.v
    src/kohakuaxi/station/sb_hub.v
    src/kohakuaxi/station/sb_nmu.v
    src/kohakuaxi/station/sb_nsu.v
    src/kohakuaxi/station/sb_link.v
    src/kohakuaxi/station/sb_link_cdc.v
    src/kohakuaxi/station/sb_stn_line.v
    src/kohakuaxi/station/sb_line4.v
    src/synth_top/sb_v6_bus.v

    src/kohakunoc/noc_inport.v
    src/kohakunoc/noc_outport.v
    src/kohakunoc/noc_router.v
    src/kohakunoc/noc_cu_base.v
    src/kohakunoc/noc_orchestrator.v
    src/kohakunoc/noc_l2_adapter.v
    src/kohakunoc/noc_local_cdc.v

    src/kohakutpu/matmul/mx_mac.v
    src/kohakutpu/matmul/mx_tcu.v
    src/kohakutpu/matmul/mx_fpacc.v
    src/kohakutpu/matmul/mx_acu_fp.v
    src/kohakutpu/matmul/mx_acu_fp_pump.v
    src/kohakutpu/matmul/mx_cluster_core.v
    src/kohakutpu/matmul/mx_cluster_mgr.v
    src/kohakutpu/matmul/mx_cluster_mgr_pump.v
    src/kohakutpu/matmul/mx_cluster_node.v
    src/kohakutpu/matmul/mx_cluster_node_pump.v
    src/kohakutpu/matmul/mx_cluster_cu.v
    src/kohakutpu/matmul/mx_cluster_cu_pump.v
    src/kohakutpu/matmul/mx_tdesc.v

    src/kohakutpu/vector/vec_dsp.v
    src/kohakutpu/vector/vec_delay.v
    src/kohakutpu/vector/vec_tables.v
    src/kohakutpu/vector/vec_alu.v
    src/kohakutpu/vector/vec_cvt.v
    src/kohakutpu/vector/vec_regfile.v
    src/kohakutpu/vector/vec_lanes.v
    src/kohakutpu/vector/vec_agu.v
    src/kohakutpu/vector/vec_core.v
    src/kohakutpu/vector/vec_cu.v

    src/kohakumas/mx_quant.v
    src/kohakumas/mag_mem_port.v
    src/kohakumas/mag_stage.v
    src/kohakumas/mag_stage_port.v
    src/kohakumas/mm_prng.v
    src/kohakumas/mm_mover.v
    src/kohakumas/il_pkt_arb.v
    src/kohakumas/mag_link.v
    src/kohakumas/mag_link_cdc.v
    src/kohakumas/mag_link_pipe.v
    src/kohakumas/mag_switch.v
    src/kohakumas/mag_ilink.v
    src/kohakumas/mag.v
    src/kohakumas/mag_dram_port.v

    src/synth_top/mag_1m.v
    src/synth_top/ktpu_div2.v
    src/synth_top/ktpu_ship_2x2_7c2v_1m_pump.v
    src/synth_top/ktpu_ship_2x2_6c2v_1m_pump.v
}

# Named, not globbed: a stray file under src/ must not silently enter the build.
set v6_missing {}
foreach f $V6_SOURCES {
    if {[file exists $root/$f]} {
        add_files -norecurse -fileset sources_1 $root/$f
    } else {
        lappend v6_missing $f
    }
}
if {[llength $v6_missing]} {
    error "v6 sources missing: [join $v6_missing {, }]"
}
update_compile_order -fileset sources_1
puts "@@@ v6 sources: [llength $V6_SOURCES] files"
