import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent


def build_icons() -> None:
    renderer = QSvgRenderer(str(ROOT / "multilayer/assets/multilayer.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid application logo")
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    output = ROOT / "build"
    output.mkdir(exist_ok=True)
    if not image.save(str(output / "multilayer.png")):
        raise RuntimeError("Could not render application icon")
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = []
    entries = []
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        scaled = image.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not scaled.save(buffer, "ICO"):
            raise RuntimeError("Could not render ICO image")
        encoded = bytes(buffer.data())
        length, start = struct.unpack_from("<II", encoded, 14)
        payload = encoded[start : start + length]
        entries.append(
            struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, length, offset)
        )
        images.append(payload)
        offset += length
    (output / "multilayer.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries + images)
    )


if __name__ == "__main__":
    build_icons()
