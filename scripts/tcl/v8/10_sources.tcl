# RTL for multimesh v8. Order is irrelevant; update_compile_order sorts it.
# Named, not globbed: a stray file under src/ must not silently enter the
# build, and a module the design must not carry (noc_l2_adapter) is simply
# absent -- a top that still referenced it would fail to elaborate here.

set V8_SOURCES {
    src/kohakuaccel/common/sync_fifo.v
    src/kohakuaccel/common/async_fifo.v
    src/kohakuaccel/common/kohaku_sdpram.v
    src/kohakuaccel/common/kohaku_sdpram_be.v
    src/kohakuaccel/common/kh_rst_sync.v
    src/kohakuaccel/common/clk/ktpu_div2.v

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
    src/kohakuaxi/xache/engine/kx_rd_engine.v
    src/kohakuaxi/xache/engine/kx_rd_pipe.v
    src/kohakuaxi/xache/engine/kx_wr_engine.v
    src/kohakuaxi/xache/kx_xache.v
    src/kohakuaxi/pxache/lane/kx_hop.v
    src/kohakuaxi/pxache/lane/kx_lane.v
    src/kohakuaxi/pxache/kx_pxache.v
    xilinx-fpga/xcvu13p/bd/kx_bd_4x4.v
    xilinx-fpga/xcvu13p/bd/xcvu13p_rst_tree.v

    src/kohakuaccel/noc/router/noc_inport.v
    src/kohakuaccel/noc/router/noc_outport.v
    src/kohakuaccel/noc/router/noc_router.v
    src/kohakuaccel/noc/endpoint/noc_cu_base.v
    src/kohakuaccel/noc/ctrl/noc_orchestrator.v
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
    src/kohakuaccel/sysnode/mover/mx_tdesc.v

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
    src/kohakuaccel/sysnode/core/mag_mem_port.v
    src/kohakuaccel/sysnode/core/mag_stage.v
    src/kohakuaccel/sysnode/core/mag_stage_port.v
    src/kohakuaccel/sysnode/mover/mm_prng.v
    src/kohakuaccel/sysnode/mover/mm_mover.v
    src/kohakuaccel/sysnode/interlink/il_pkt_arb.v
    src/kohakuaccel/sysnode/interlink/mag_link.v
    src/kohakuaccel/sysnode/interlink/mag_link_pipe.v
    xilinx-fpga/xcvu13p/bd/mag_link_pipe_bd.v
    src/kohakuaccel/sysnode/interlink/mag_switch.v
    src/kohakuaccel/sysnode/interlink/mag_ilink.v
    src/kohakuaccel/sysnode/core/mag_xform.v
    src/kohakutpu/transform/xform_bank.v
    src/kohakuaccel/sysnode/core/mag.v
    src/kohakuaccel/sysnode/core/mag_dram_port.v

    src/kohakuaccel/pe/rv32/mem/rv_ram_be.v
    src/kohakuaccel/pe/rv32/mem/rv_imem.v
    src/kohakuaccel/pe/rv32/mem/rv_spad.v
    src/kohakuaccel/pe/rv32/mem/rv_l1.v
    src/kohakuaccel/pe/rv32/core/rv_regfile.v
    src/kohakuaccel/pe/rv32/core/rv_bpred.v
    src/kohakuaccel/pe/rv32/core/rv_if.v
    src/kohakuaccel/pe/rv32/core/rv_id.v
    src/kohakuaccel/pe/rv32/core/rv_ex.v
    src/kohakuaccel/pe/rv32/core/rv_mem.v
    src/kohakuaccel/pe/rv32/core/rv_wb.v
    src/kohakuaccel/pe/rv32/core/rv_core.v
    src/kohakuaccel/pe/rv32/noc/rv_noc_req.v
    src/kohakuaccel/pe/rv32/noc/rv_mag_req.v
    src/kohakuaccel/sysnode/mover/mv_exec.v
    src/kohakuaccel/sysnode/cpu/rv_mag_pe.v

    src/kohakuaccel/sysnode/core/sn_hub.v
    src/kohakuaccel/sysnode/sysnode.v
    src/kohakutpu/top/generated/ktpu_ship_2x2_6c2v_1m_nol2_pump.v
    src/kohakutpu/top/generated/ktpu_ship_2x2_2c2v_1m_nol2_pump.v
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
    error "v8 sources missing: [join $v8_missing {, }]"
}
update_compile_order -fileset sources_1
puts "@@@ v8 sources: [llength $V8_SOURCES] files"
