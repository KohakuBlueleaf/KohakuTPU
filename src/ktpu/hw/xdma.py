"""The PCIe backend: a :class:`~ktpu.hw.device.Transport` over Windows XDMA.

A FILE OFFSET IS AN AXI ADDRESS. The Xilinx driver's per-channel nodes --
``h2c_0``, ``c2h_0`` -- are seekable files, and a seek names a point in the
FPGA's address map. So the backend is CreateFile, SetFilePointerEx, ReadFile
and WriteFile, all four of which `ctypes` reaches: THERE IS NO C SHIM AND
NOTHING TO BUILD. Ported from tools/win/xdma_win.h in JTAG-DMA-test quirk for
quirk -- 64-bit offsets, a hard ~8 MB ceiling, and sub-1 MB transfers that fail
INTERMITTENTLY with win32 1359 then succeed moments later. All three are
measured (that project's docs/XDMA_WINDOWS.md s4) and each is a wrong answer if
ignored. The last is why the control plane is the risky part: an 8-byte
register access is exactly the size that misbehaves. Retries are counted, not
hidden -- a backend that papers over driver flakiness is lying about the device.
"""

import ctypes
import sys
import time

from ktpu.hw.device import (
    MASK64,
    WORD_BYTES,
    Transport,
    TransportUnavailable,
    require_words,
)

# {74c7e4a9-6d5d-4a70-bc0d-20691dff9e9d}, as printed by xdma_info.exe.
INTERFACE_GUID = (
    0x74C7E4A9,
    0x6D5D,
    0x4A70,
    (0xBC, 0x0D, 0x20, 0x69, 0x1D, 0xFF, 0x9E, 0x9D),
)

# MEASURED CEILING of the 2017.1 driver: 8 MB succeeds, 16 MB fails with win32
# 1359 before a byte moves. Also where throughput peaks, at ~1.8 GB/s.
MAX_XFER = 8 << 20
RETRIES = 6
RETRY_PAUSE = 0.010

GENERIC_READ = 0x8000_0000
GENERIC_WRITE = 0x4000_0000
OPEN_EXISTING = 3
FILE_BEGIN = 0
INVALID_HANDLE = ctypes.c_void_p(-1).value
DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
ERROR_INTERNAL_ERROR = 1359


class XdmaError(OSError):
    """A transfer failed on a device that opened. Carries the win32 code."""


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _IfaceData(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("InterfaceClassGuid", _GUID),
        ("Flags", ctypes.c_uint32),
        ("Reserved", ctypes.POINTER(ctypes.c_ulonglong)),
    ]


class _IfaceDetail(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("DevicePath", ctypes.c_wchar * 1024)]


_api = None


def _win():
    """kernel32 and setupapi with argtypes declared, loaded once, or refuse.

    Loaded LAZILY so this module imports on any platform: `open_transport`
    has to be able to try XDMA and be told no, which it cannot do if the
    import itself is the failure.

    Declaring argtypes is not tidiness. Without them ctypes passes a 64-bit
    file offset as a 32-bit int and the seek lands somewhere else entirely --
    which is the same bug that limits xdma_rw.exe to the first DDR bank.
    """
    global _api
    if _api is not None:
        return _api
    if sys.platform != "win32":
        raise TransportUnavailable(
            f"XDMA is a Windows driver and this is {sys.platform}; "
            "use the JTAG transport, or a simulator"
        )
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sa = ctypes.WinDLL("setupapi", use_last_error=True)
    dword, hnd, lpvoid = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p

    k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        dword,
        dword,
        lpvoid,
        dword,
        dword,
        hnd,
    ]
    k32.CreateFileW.restype = hnd
    k32.SetFilePointerEx.argtypes = [hnd, ctypes.c_longlong, lpvoid, dword]
    k32.SetFilePointerEx.restype = ctypes.c_int
    k32.ReadFile.argtypes = [hnd, lpvoid, dword, ctypes.POINTER(dword), lpvoid]
    k32.ReadFile.restype = ctypes.c_int
    k32.WriteFile.argtypes = [hnd, lpvoid, dword, ctypes.POINTER(dword), lpvoid]
    k32.WriteFile.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [hnd]
    k32.CloseHandle.restype = ctypes.c_int

    sa.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_wchar_p,
        hnd,
        dword,
    ]
    sa.SetupDiGetClassDevsW.restype = hnd
    sa.SetupDiEnumDeviceInterfaces.argtypes = [
        hnd,
        lpvoid,
        ctypes.POINTER(_GUID),
        dword,
        ctypes.POINTER(_IfaceData),
    ]
    sa.SetupDiEnumDeviceInterfaces.restype = ctypes.c_int
    sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        hnd,
        ctypes.POINTER(_IfaceData),
        ctypes.POINTER(_IfaceDetail),
        dword,
        ctypes.POINTER(dword),
        lpvoid,
    ]
    sa.SetupDiGetDeviceInterfaceDetailW.restype = ctypes.c_int
    sa.SetupDiDestroyDeviceInfoList.argtypes = [hnd]
    sa.SetupDiDestroyDeviceInfoList.restype = ctypes.c_int
    _api = (k32, sa)
    return _api


def _guid():
    a, b, c, d = INTERFACE_GUID
    return _GUID(a, b, c, (ctypes.c_ubyte * 8)(*d))


def find(index: int = 0) -> str:
    """The interface path of the Nth XDMA device.

    Discovered rather than hardcoded, so the driver survives a
    re-enumeration or a second card. Raises :class:`TransportUnavailable`
    when there is no such device.
    """
    _, sa = _win()
    guid = _guid()
    dev_set = sa.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if dev_set is None or dev_set == INVALID_HANDLE:
        raise TransportUnavailable("SetupDiGetClassDevs found no XDMA device class")
    try:
        ifd = _IfaceData()
        ifd.cbSize = ctypes.sizeof(_IfaceData)
        if not sa.SetupDiEnumDeviceInterfaces(
            dev_set, None, ctypes.byref(guid), index, ctypes.byref(ifd)
        ):
            raise TransportUnavailable(
                f"no XDMA device at index {index}. Is the card enumerated and the "
                "Xilinx driver installed? PCIe enumerates at boot, so an FPGA "
                "programmed after the host booted will not appear."
            )
        detail = _IfaceDetail()
        # cbSize is the size of the C struct's HEADER, not of our buffer: 8 on
        # x64. The buffer size is the separate argument that follows.
        detail.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        if not sa.SetupDiGetDeviceInterfaceDetailW(
            dev_set,
            ctypes.byref(ifd),
            ctypes.byref(detail),
            ctypes.sizeof(detail),
            None,
            None,
        ):
            raise TransportUnavailable(
                f"XDMA device {index} exists but its path could not be read, "
                f"win32 {ctypes.get_last_error()}"
            )
        return detail.DevicePath
    finally:
        sa.SetupDiDestroyDeviceInfoList(dev_set)


def available(index: int = 0) -> bool:
    """Whether :func:`find` would succeed. Never raises."""
    try:
        find(index)
    except TransportUnavailable:
        return False
    return True


class XdmaTransport(Transport):
    """A 64-bit AXI window on an XDMA card, with a real bulk path.

    ``base`` is added to every address, so a caller addresses a WINDOW rather
    than the card: the control registers live at one board offset and MAG's
    memory window at another, and neither belongs in the driver as a constant.

    Writes go out the H2C channel and reads come back on C2H, which are
    separate handles and separate engines -- so one transport is two files,
    and a card with several engines is several transports.
    """

    bulk = True

    def __init__(self, index: int = 0, channel: int = 0, base: int = 0, path=None):
        self.base = base
        self.path = path or find(index)
        self.retries = 0
        self.bytes_out = 0
        self.bytes_in = 0
        self.h2c = self._open(f"h2c_{channel}", GENERIC_WRITE)
        try:
            self.c2h = self._open(f"c2h_{channel}", GENERIC_READ)
        except TransportUnavailable:
            self.close()
            raise

    def _open(self, node, access):
        k32, _ = _win()
        h = k32.CreateFileW(
            f"{self.path}\\{node}", access, 0, None, OPEN_EXISTING, 0, None
        )
        if h is None or h == INVALID_HANDLE:
            raise TransportUnavailable(
                f"cannot open {node} on {self.path}, win32 {ctypes.get_last_error()}"
            )
        return h

    # ---- lifecycle ------------------------------------------------------
    def close(self):
        k32, _ = _win()
        for name in ("h2c", "c2h"):
            h = getattr(self, name, None)
            if h is not None:
                k32.CloseHandle(h)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- the transport --------------------------------------------------
    def write64(self, addr: int, data: int) -> None:
        self.write_block(addr, (data & MASK64).to_bytes(WORD_BYTES, "little"))

    def read64(self, addr: int) -> int:
        return int.from_bytes(self.read_block(addr, WORD_BYTES), "little")

    def write_block(self, addr: int, data: bytes) -> None:
        require_words(len(data))
        # `from_buffer` needs something writable, and copying an operand image
        # to satisfy that would double the upload's memory for nothing.
        buf = data if isinstance(data, bytearray) else bytearray(data)
        self._xfer(self.h2c, self.base + addr, buf, write=True)
        self.bytes_out += len(buf)

    def read_block(self, addr: int, nbytes: int) -> bytes:
        require_words(nbytes)
        buf = bytearray(nbytes)
        self._xfer(self.c2h, self.base + addr, buf, write=False)
        self.bytes_in += nbytes
        return bytes(buf)

    def _xfer(self, handle, addr, buf, write):
        """Move `buf` to or from AXI address `addr`, split and retried.

        Each chunk re-seeks: the driver's file position after a short transfer
        is not something to rely on, and a seek costs nothing next to a DMA.
        """
        k32, _ = _win()
        done, total = 0, len(buf)
        while done < total:
            want = min(total - done, MAX_XFER)
            if not k32.SetFilePointerEx(handle, addr + done, None, FILE_BEGIN):
                raise XdmaError(
                    f"seek to {addr + done:#x} failed, "
                    f"win32 {ctypes.get_last_error()}"
                )
            chunk = (ctypes.c_char * want).from_buffer(buf, done)
            moved = ctypes.c_uint32(0)
            got = ctypes.pointer(moved)
            ok = False
            for attempt in range(RETRIES):
                if attempt:
                    self.retries += 1
                    time.sleep(RETRY_PAUSE)
                call = k32.WriteFile if write else k32.ReadFile
                ok = bool(call(handle, chunk, want, got, None))
                # Only 1359 is the settling failure worth retrying; anything
                # else is a real error and retrying it just delays the report.
                if not ok and ctypes.get_last_error() != ERROR_INTERNAL_ERROR:
                    break
                if ok and moved.value == 0:
                    ok = False
                if ok:
                    break
            if not ok:
                raise XdmaError(
                    f"{'write' if write else 'read'} of {want} B at "
                    f"{addr + done:#x} failed, win32 {ctypes.get_last_error()}"
                    + (f" after {RETRIES} attempts" if RETRIES > 1 else "")
                )
            done += moved.value

    def __repr__(self):
        return (
            f"XdmaTransport(base={self.base:#x}, out={self.bytes_out} B, "
            f"in={self.bytes_in} B, retries={self.retries})"
        )
