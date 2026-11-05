"""Build Media Background Pane.exe from icon.png.

The exe has to stay in this folder, next to main.py. Shortcut that file
into the Start menu; copying the exe somewhere else will not launch the app.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

ROOT = Path(__file__).resolve().parent
PNG = ROOT / "icon.png"
ICO = ROOT / "launcher" / "icon.ico"
SOURCE = ROOT / "launcher" / "Program.cs"
EXE = ROOT / "Media Background Pane.exe"
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _scaled(image: QImage, size: int) -> QImage:
    scaled = image.scaled(
        size,
        size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return scaled.convertToFormat(QImage.Format.Format_ARGB32)


def _dib_image(image: QImage) -> bytes:
    width = image.width()
    height = image.height()
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height * 2,
        1,
        32,
        0,
        width * height * 4,
        0,
        0,
        0,
        0,
    )
    xor = bytearray(width * height * 4)
    index = 0
    for y in range(height - 1, -1, -1):
        for x in range(width):
            color = image.pixelColor(x, y)
            xor[index] = color.blue()
            xor[index + 1] = color.green()
            xor[index + 2] = color.red()
            xor[index + 3] = color.alpha()
            index += 4

    stride = ((width + 31) // 32) * 4
    mask = bytearray(stride * height)
    row = 0
    for y in range(height - 1, -1, -1):
        base = row * stride
        for x in range(width):
            if image.pixelColor(x, y).alpha() == 0:
                mask[base + (x // 8)] |= 1 << (7 - (x % 8))
        row += 1
    return header + bytes(xor) + bytes(mask)


def write_ico(image: QImage, path: Path) -> None:
    blobs = []
    for size in ICON_SIZES:
        blobs.append(_dib_image(_scaled(image, size)))
    count = len(blobs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = bytearray()
    payload = bytearray()
    for size, blob in zip(ICON_SIZES, blobs):
        stored = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", stored, stored, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        payload += blob
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(entries) + bytes(payload))


def compile_exe() -> None:
    if not CSC.is_file():
        raise SystemExit(f"C# compiler not found: {CSC}")
    command = [
        str(CSC),
        "/nologo",
        "/target:winexe",
        "/optimize+",
        f"/win32icon:{ICO}",
        "/reference:System.Windows.Forms.dll",
        f"/out:{EXE}",
        str(SOURCE),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)


def main() -> None:
    image = QImage(str(PNG))
    if image.isNull():
        raise SystemExit(f"Could not read {PNG}")
    write_ico(image, ICO)
    compile_exe()
    print(EXE)


if __name__ == "__main__":
    main()
