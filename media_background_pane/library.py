from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink

from .bus import IMAGE_EXT, is_media
from .config import thumb_cache_dir


def media_files(folder: Path, recursive: bool) -> list[Path]:
    if not folder.is_dir():
        return []
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    files = [path for path in iterator if path.is_file() and is_media(path)]
    files.sort(key=lambda p: p.name.casefold())
    return files


def thumb_cache_path(source: Path) -> Path:
    try:
        info = source.stat()
        stamp = f"{source.resolve()}|{info.st_mtime_ns}|{info.st_size}"
    except OSError:
        stamp = str(source)
    digest = hashlib.sha1(stamp.encode("utf-8")).hexdigest()
    return thumb_cache_dir() / f"{digest}.jpg"


def image_thumbnail(source: Path, width: int = 160, height: int = 90) -> QImage:
    cached = thumb_cache_path(source)
    if cached.is_file():
        image = QImage(str(cached))
        if not image.isNull():
            return image
    reader = QImageReader(str(source))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > 0:
        size.scale(width, height, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(size)
    image = reader.read()
    if image.isNull():
        return QImage()
    image.save(str(cached), "JPG", 80)
    return image


class Thumbnailer(QObject):
    ready = Signal(str, QImage)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: list[Path] = []
        self._busy = False
        self._player: QMediaPlayer | None = None
        self._sink: QVideoSink | None = None
        self._current: Path | None = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._fail_current)

    def request(self, path: Path) -> QImage | None:
        cached = thumb_cache_path(path)
        if cached.is_file():
            image = QImage(str(cached))
            if not image.isNull():
                return image
        if path.suffix.lower() in IMAGE_EXT:
            image = image_thumbnail(path)
            return None if image.isNull() else image
        if path not in self._queue and path != self._current:
            self._queue.append(path)
            QTimer.singleShot(0, self._pump)
        return None

    def _pump(self) -> None:
        if self._busy or not self._queue:
            return
        self._current = self._queue.pop(0)
        self._busy = True
        self._player = QMediaPlayer(self)
        self._sink = QVideoSink(self)
        self._player.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.setSource(QUrl.fromLocalFile(str(self._current)))
        self._player.play()
        self._timeout.start(2500)

    def _on_frame(self, frame: QVideoFrame) -> None:
        if not frame.isValid() or self._current is None:
            return
        self._finish(frame.toImage())

    def _fail_current(self) -> None:
        self._finish(QImage())

    def _finish(self, image: QImage) -> None:
        self._timeout.stop()
        path = self._current
        if self._player is not None:
            if self._sink is not None:
                try:
                    self._sink.videoFrameChanged.disconnect(self._on_frame)
                except (RuntimeError, TypeError):
                    pass
            self._player.stop()
            self._player.deleteLater()
            self._player = None
        if self._sink is not None:
            self._sink.deleteLater()
            self._sink = None
        self._busy = False
        self._current = None
        if path is not None and not image.isNull():
            scaled = image.scaled(
                160,
                90,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            scaled.save(str(thumb_cache_path(path)), "JPG", 80)
            self.ready.emit(str(path), scaled)
        QTimer.singleShot(0, self._pump)
