"""Malformed package ranges and unit references on the firmware host double."""


def test_table_extents(validation):
    validation("table_extents")


def test_total_alignment(validation):
    validation("total_alignment")


def test_engine_extent(validation):
    validation("engine_extent")


def test_repeat_extent(validation):
    validation("repeat_extent")


def test_unit_references(validation):
    validation("unit_references")


def test_patch_unit(validation):
    validation("patch_unit")


def test_local_repeat_field_mask(validation):
    validation("local_repeat_field_mask")
