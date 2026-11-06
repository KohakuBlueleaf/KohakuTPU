"""Dispatch retirement frontiers, transport deadlines and mover WAIT windows."""


def test_dispatch_guards(validation):
    validation("dispatch_guards")


def test_ack_is_not_retirement(validation):
    validation("ack_is_not_retirement")


def test_enumeration_deadline(validation):
    validation("enumeration_deadline")


def test_source_mover_wait(validation):
    validation("source_mover_wait")
