"""Lets tinygrad find the KTPU device with no import in the script: tinygrad
opens ``DEV=KTPU`` by importing ``tinygrad.runtime.ops_ktpu``, and this
meta-path finder answers that one name by importing `ktpugrad` (which
installs the seam and registers the device). Importing this module costs
nothing else; a ``.pth`` line written by ``python -m
kohakutpu.application.ktpugrad --autoload`` imports it at startup."""

import importlib
import importlib.abc
import importlib.machinery
import sys

MODULE = "tinygrad.runtime.ops_ktpu"
PACKAGE = "kohakutpu.application.ktpugrad"


class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        # The import machinery has put `module` in sys.modules under MODULE;
        # ktpugrad's register() fills that one with KTPUDevice.
        importlib.import_module(PACKAGE)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name != MODULE:
            return None
        return importlib.machinery.ModuleSpec(name, _Loader())


def install() -> None:
    if not any(isinstance(f, Finder) for f in sys.meta_path):
        sys.meta_path.append(Finder())


install()

__all__ = ["MODULE", "Finder", "install"]
