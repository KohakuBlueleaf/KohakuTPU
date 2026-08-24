"""Pure-python RGBImage container supporting export to PNG/BMP/PPM without external dependencies."""

import os
import struct
import zlib

import numpy as np


class RGBImage:
    """RGB Image container (H, W, 3) uint8 with built-in export to PNG/BMP/PPM."""

    def __init__(self, data: np.ndarray):
        if data.ndim == 3 and data.shape[0] == 3:  # (3, H, W) -> (H, W, 3)
            data = np.transpose(data, (1, 2, 0))
        self.data = np.ascontiguousarray(data, dtype=np.uint8)
        self.height, self.width, self.channels = self.data.shape
        self.size = (self.width, self.height)

    def to_numpy(self) -> np.ndarray:
        return self.data

    def save(self, path: str) -> None:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".bmp":
            self._save_bmp(path)
        elif ext == ".ppm":
            self._save_ppm(path)
        else:
            self._save_png(path)

    def _save_ppm(self, path: str) -> None:
        header = f"P6\n{self.width} {self.height}\n255\n".encode("ascii")
        with open(path, "wb") as f:
            f.write(header)
            f.write(self.data.tobytes())

    def _save_bmp(self, path: str) -> None:
        row_padded = (self.width * 3 + 3) & ~3
        pad_len = row_padded - self.width * 3
        pad = b"\x00" * pad_len

        image_size = row_padded * self.height
        file_size = 54 + image_size

        bmp_header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
        dib_header = struct.pack(
            "<IIIHHIIIIII",
            40,
            self.width,
            self.height,
            1,
            24,
            0,
            image_size,
            2835,
            2835,
            0,
            0,
        )

        with open(path, "wb") as f:
            f.write(bmp_header)
            f.write(dib_header)
            for y in range(self.height - 1, -1, -1):
                row_rgb = self.data[y]
                row_bgr = row_rgb[:, ::-1]
                f.write(row_bgr.tobytes())
                if pad_len > 0:
                    f.write(pad)

    def _save_png(self, path: str) -> None:
        def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + chunk_type
                + data
                + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
            )

        png_sig = b"\x89PNG\r\n\x1a\n"
        ihdr_data = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        ihdr_chunk = make_chunk(b"IHDR", ihdr_data)

        raw_rows = []
        for y in range(self.height):
            raw_rows.append(b"\x00" + self.data[y].tobytes())
        raw_data = b"".join(raw_rows)
        compressed_data = zlib.compress(raw_data)
        idat_chunk = make_chunk(b"IDAT", compressed_data)
        iend_chunk = make_chunk(b"IEND", b"")

        with open(path, "wb") as f:
            f.write(png_sig)
            f.write(ihdr_chunk)
            f.write(idat_chunk)
            f.write(iend_chunk)
