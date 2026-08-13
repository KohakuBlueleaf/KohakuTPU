"""A 64-bit AXI window driven over PCIe, through the Xilinx XDMA driver.

Blocks only. The 2017.1 Windows driver BSODs -- PAGE_FAULT_IN_NONPAGED_AREA in
xdma.sys -- on transfers it cannot map, which includes a small one off an
unaligned host buffer. Measured 2026-08-12: 16 MB per channel at 1.4 GB/s H2C
and 1.7 GB/s C2H, while an 8-byte request returns zero bytes. Pair with a
control transport for word access.

Never read DRAM that was not written first: uninitialised ECC answers SLVERR,
and any error response wedges the channel until the device is reset.
"""

import ctypes
import mmap
from ctypes import wintypes

from kohakuaccel.transport.base import (
    Transport,
    TransportUnavailable,
    require_words,
)

#: {74c7e4a9-6d5d-4a70-bc0d-20691dff9e9d}, the driver's device interface.
XDMA_GUID = "{74C7E4A9-6D5D-4A70-BC0D-20691DFF9E9D}"

#: MEASURED CEILING of the 2017.1 driver: 8 MB succeeds, 16 MB fails with
#: ERROR_INTERNAL_ERROR before a byte moves.
MAX_XFER = 8 << 20

#: Below this a transfer goes to the control transport instead. The engine moves
#: 512-bit beats, so a sub-beat request is the shape the driver mishandles.
MIN_XFER = 64


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.POINTER(wintypes.ULONG)),
    ]


#: What SetupDiGetClassDevs returns on failure, as a 64-bit unsigned value.
INVALID_HANDLE = ctypes.c_void_p(-1).value


def _bind(dll):
    """Declare SetupAPI's signatures on `dll`; returns it.

    NOT OPTIONAL. ctypes defaults every WinDLL function to a 32-bit int return,
    which TRUNCATES the 64-bit HDEVINFO -- and the truncated value is a plausible
    handle rather than an error, so enumeration then reports no device present.
    """
    dll.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    dll.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(_GUID),
        wintypes.LPCWSTR,
        wintypes.HWND,
        wintypes.DWORD,
    ]
    dll.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    dll.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        wintypes.DWORD,
        ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
    ]
    dll.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    dll.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    dll.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
    dll.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    return dll


def find_device(index: int = 0) -> str:
    """The interface path of the `index`-th XDMA device.

    Raises :class:`TransportUnavailable` if the host is not Windows, the driver
    is absent, or no device is enumerated at `index`. The path carries the PCI
    location, so it changes when the card is re-enumerated.
    """
    try:
        setupapi = _bind(ctypes.WinDLL("setupapi"))
        ole32 = ctypes.WinDLL("ole32")
    except (OSError, AttributeError) as exc:
        raise TransportUnavailable(f"not a Windows host with SetupAPI ({exc})") from exc

    guid = _GUID()
    if ole32.CLSIDFromString(ctypes.c_wchar_p(XDMA_GUID), ctypes.byref(guid)):
        raise TransportUnavailable("could not parse the XDMA interface GUID")

    # DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    handle = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)
    if handle in (None, INVALID_HANDLE):
        raise TransportUnavailable("SetupDiGetClassDevs failed for the XDMA class")
    try:
        ifd = _SP_DEVICE_INTERFACE_DATA()
        ifd.cbSize = ctypes.sizeof(ifd)
        if not setupapi.SetupDiEnumDeviceInterfaces(
            handle, None, ctypes.byref(guid), index, ctypes.byref(ifd)
        ):
            raise TransportUnavailable(
                f"no XDMA device at index {index}; is the driver installed and the "
                f"card enumerated on PCIe?"
            )
        need = wintypes.DWORD()
        setupapi.SetupDiGetDeviceInterfaceDetailW(
            handle, ctypes.byref(ifd), None, 0, ctypes.byref(need), None
        )
        buf = ctypes.create_string_buffer(need.value)
        # 8, the fixed header size on 64-bit Windows -- passing the buffer's size
        # returns ERROR_INVALID_PARAMETER and an empty path.
        ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(
            handle, ctypes.byref(ifd), buf, need, None, None
        ):
            raise TransportUnavailable("SetupDiGetDeviceInterfaceDetail failed")
        return ctypes.wstring_at(ctypes.addressof(buf) + 4)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(handle)


class XdmaTransport(Transport):
    """The card's AXI window over PCIe, in blocks.

    `base` is added to every address. `device` names an already-known interface
    path, else the `index`-th is found. `max_xfer` splits long transfers;
    `min_xfer` is the floor below which :meth:`write_block` and
    :meth:`read_block` raise :class:`ValueError`. :meth:`write64` and
    :meth:`read64` always raise :class:`NotImplementedError`.
    """

    bulk = True

    def __init__(
        self,
        base: int = 0,
        device: str | None = None,
        index: int = 0,
        max_xfer: int = MAX_XFER,
        min_xfer: int = MIN_XFER,
    ) -> None:
        self.base = base
        self.max_xfer, self.min_xfer = max_xfer, min_xfer
        self.device = device if device is not None else find_device(index)
        self.bytes_read = self.bytes_written = 0
        h2c, c2h = f"{self.device}\\h2c_0", f"{self.device}\\c2h_0"
        try:
            # Held for the transport's life and shut by `close()`; a context
            # manager would close the channel the moment it opened.
            self._h2c = open(h2c, "rb+", buffering=0)  # noqa: SIM115
            self._c2h = open(c2h, "rb", buffering=0)  # noqa: SIM115
        except OSError as exc:
            raise TransportUnavailable(f"cannot open XDMA channels ({exc})") from exc
        # Page-aligned staging: the driver pins these pages directly, and a
        # buffer that is not page-aligned is what faults xdma.sys.
        self._staging = mmap.mmap(-1, self.max_xfer)

    def close(self) -> None:
        for f in (getattr(self, "_h2c", None), getattr(self, "_c2h", None)):
            if f is not None and not f.closed:
                f.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 -- a finaliser must not raise
            pass

    def _refuse(self, nbytes: int) -> None:
        if nbytes < self.min_xfer:
            raise ValueError(
                f"{nbytes} bytes is below XDMA's {self.min_xfer}-byte floor; the "
                f"driver faults on sub-beat transfers. Route small access to a "
                f"control transport (kohakuaccel.transport.split)."
            )

    def write64(self, addr: int, data: int) -> None:
        raise NotImplementedError(
            "XDMA moves blocks, not words. Pair it with a control transport."
        )

    def read64(self, addr: int) -> int:
        raise NotImplementedError(
            "XDMA moves blocks, not words. Pair it with a control transport."
        )

    def write_block(self, addr: int, data: bytes) -> None:
        require_words(len(data))
        self._refuse(len(data))
        for off in range(0, len(data), self.max_xfer):
            chunk = data[off : off + self.max_xfer]
            self._staging[: len(chunk)] = chunk
            self._h2c.seek(self.base + addr + off)
            view = memoryview(self._staging)[: len(chunk)]
            if self._h2c.write(view) != len(chunk):
                raise OSError(f"short XDMA write at {self.base + addr + off:#x}")
            self.bytes_written += len(chunk)

    def read_block(self, addr: int, nbytes: int) -> bytes:
        require_words(nbytes)
        self._refuse(nbytes)
        out = bytearray()
        for off in range(0, nbytes, self.max_xfer):
            n = min(self.max_xfer, nbytes - off)
            self._c2h.seek(self.base + addr + off)
            view = memoryview(self._staging)[:n]
            if self._c2h.readinto(view) != n:
                raise OSError(f"short XDMA read at {self.base + addr + off:#x}")
            out += bytes(view)
            self.bytes_read += n
        return bytes(out)

    def __repr__(self) -> str:
        return (
            f"XdmaTransport(base={self.base:#x}, "
            f"read={self.bytes_read}, written={self.bytes_written})"
        )
