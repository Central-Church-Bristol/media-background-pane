from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths

APP_NAME = "Media Background Pane"
FADE_DURATIONS = (1, 3, 5, 10, 20, 30)
DEFAULT_FADE_SECONDS = 10
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
QUALITY_SIZES = {
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "540p": (960, 540),
    "360p": (640, 360),
}
FPS_CHOICES = (24, 25, 30, 50, 60)
GUI_WIDTH = 160
GUI_HEIGHT = 90
GUI_FPS = 5
DEFAULTS_REVISION = 2


def app_data_dir() -> Path:
    root = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
    if not root.name:
        root = Path.home() / "AppData" / "Local" / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def thumb_cache_dir() -> Path:
    path = app_data_dir() / "thumbs"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Config:
    def __init__(self) -> None:
        self.media_folder: str = ""
        self.include_subfolders: bool = False
        self.fade_index: int = FADE_DURATIONS.index(DEFAULT_FADE_SECONDS)
        self.fps: int = 24
        self.workspace_name: str = "Evening Service"
        self.always_on_top: bool = True
        self.start_with_windows: bool = True
        self.auto_show_on_workspace: bool = True
        self.ndi_name: str = APP_NAME
        self.quality: str = "540p"
        self.thumb_scale: int = 112
        self.geometry: list[int] | None = None
        self.collapsed: bool = False
        self.defaults_revision: int = DEFAULTS_REVISION

    @property
    def process_size(self) -> tuple[int, int]:
        return QUALITY_SIZES.get(self.quality, QUALITY_SIZES["540p"])

    @property
    def fade_seconds(self) -> int:
        return FADE_DURATIONS[self.fade_index % len(FADE_DURATIONS)]

    def cycle_fade(self) -> int:
        self.fade_index = (self.fade_index + 1) % len(FADE_DURATIONS)
        return self.fade_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_folder": self.media_folder,
            "include_subfolders": self.include_subfolders,
            "fps": self.fps,
            "workspace_name": self.workspace_name,
            "always_on_top": self.always_on_top,
            "start_with_windows": self.start_with_windows,
            "auto_show_on_workspace": self.auto_show_on_workspace,
            "ndi_name": self.ndi_name,
            "quality": self.quality,
            "thumb_scale": self.thumb_scale,
            "geometry": self.geometry,
            "collapsed": self.collapsed,
            "defaults_revision": DEFAULTS_REVISION,
        }

    def update(self, data: dict[str, Any]) -> None:
        incoming_rev = int(data.get("defaults_revision", 0) or 0)
        for key, value in data.items():
            if key in ("defaults_revision", "fade_index"):
                continue
            if hasattr(self, key):
                setattr(self, key, value)
        self.fade_index = FADE_DURATIONS.index(DEFAULT_FADE_SECONDS)
        if incoming_rev < DEFAULTS_REVISION and int(self.fps) == 30:
            self.fps = 24
        if self.fps not in FPS_CHOICES:
            self.fps = 24
        if self.quality not in QUALITY_SIZES:
            self.quality = "540p"
        self.thumb_scale = max(64, min(200, int(self.thumb_scale or 112)))
        self.defaults_revision = DEFAULTS_REVISION

    @classmethod
    def load(cls) -> Config:
        cfg = cls()
        path = settings_path()
        if path.exists():
            try:
                cfg.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        return cfg

    def save(self) -> None:
        settings_path().write_text(
            json.dumps(self.to_dict(), indent=2),
            encoding="utf-8",
        )
