"""Dispatching from the host, with no node firmware: a kick list run as the
machine's control program."""

from kohakuaccel.driver.runtime.loader import execute, load

__all__ = ["execute", "load"]
