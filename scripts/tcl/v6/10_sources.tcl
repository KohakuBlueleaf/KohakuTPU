# RTL for multimesh v6. Order is irrelevant; update_compile_order sorts it.

set V6_SOURCES {
    src/kohakuaccel/common/sync_fifo.v
    src/kohakuaccel/common/async_fifo.v
    src/kohakuaccel/common/kohaku_sdpram.v

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

    src/kohakuaccel/noc/router/noc_inport.v
    src/kohakuaccel/noc/router/noc_outport.v
    src/kohakuaccel/noc/router/noc_router.v
    src/kohakuaccel/noc/endpoint/noc_cu_base.v
    src/kohakuaccel/noc/ctrl/noc_orchestrator.v
    src/kohakuaccel/noc/endpoint/noc_l2_adapter.v
    src/kohakuaccel/noc/endpoint/noc_local_cdc.v

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
    src/kohakuaccel/mas/mover/mx_tdesc.v

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

    src/kohakutpu/transform/mx_quant.v
    src/kohakuaccel/mas/core/mag_mem_port.v
    src/kohakuaccel/mas/core/mag_stage.v
    src/kohakuaccel/mas/core/mag_stage_port.v
    src/kohakuaccel/mas/mover/mm_prng.v
    src/kohakuaccel/mas/mover/mm_mover.v
    src/kohakuaccel/mas/interlink/il_pkt_arb.v
    src/kohakuaccel/mas/interlink/mag_link.v
    src/kohakuaccel/mas/interlink/mag_link_cdc.v
    src/kohakuaccel/mas/interlink/mag_link_pipe.v
    src/kohakuaccel/mas/interlink/mag_switch.v
    src/kohakuaccel/mas/interlink/mag_ilink.v
    src/kohakuaccel/mas/core/mag.v
    src/kohakuaccel/mas/core/mag_dram_port.v

    src/kohakutpu/top/mag_1m.v
    src/kohakuaccel/common/clk/ktpu_div2.v
    src/kohakuaccel/common/kh_rst_sync.v
    src/kohakutpu/top/generated/ktpu_ship_2x2_7c2v_1m_pump.v
    src/kohakutpu/top/generated/ktpu_ship_2x2_6c2v_1m_pump.v
    src/kohakutpu/top/generated/ktpu_ship_2x2_8c2v_1m_pump.v
    src/kohakutpu/top/generated/ktpu_ship_1x1_2c2v_1m_pump.v
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
