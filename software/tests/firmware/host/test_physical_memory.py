"""Device DMA grants exclude OS staging and do not invent CPU shared aliases."""

import native


def test_physical_dma_grants_preserve_reserved_staging_and_user_aliases(tmp_path):
    exe = native.on_double(tmp_path, "kos_memory", ["os/kernel/mem.c"])
    native.run(exe, [])
