from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import Config, FPS_CHOICES
from .startup import is_enabled as startup_enabled


HELP = """ProPresenter input (same PC):

1. Settings → Inputs → Video Inputs → +
2. Device: Media Background Pane (NDI)
3. Pick 24 fps, and a mode that matches Quality
   (960×540 for 540p, or 1920×1080 for 1080p)
4. Put that input on your background look
5. Settings → Network → Enable Network
   (the pane auto-triggers the first video input on startup)

540p / 24fps is the default. GPU decode (D3D11VA) plays
1080p files without the pane doing a heavy CPU decode.
The on-screen monitors stay tiny so they do not steal
time from the livestream.

iPad: on the same Wi-Fi as this PC, open the iPad URL shown
below in Safari as http:// (not https://). Chrome often
upgrades to https and fails. Windows Firewall may ask to
allow the pane on the private network — allow it.

Drop files onto the library to copy them into the media
folder. SCALE under the library is Scale to Fill (default),
Scale to Fit, or Actual Size.

If GPU decode is off or ffmpeg is missing, recoding files
to 540p 24fps H.264 (around 2–4 Mbps) still helps a lot.
"""


class SettingsDialog(QDialog):
    def __init__(
        self,
        config: Config,
        ndi_status: str,
        parent: QWidget | None = None,
        remote_url: str = "",
        decoder_status: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.config = config

        self.folder = QLineEdit(config.media_folder)
        browse = QPushButton("Browse…")
        open_folder = QPushButton("Open folder")
        browse.clicked.connect(self._browse)
        open_folder.clicked.connect(self._open_folder)

        folder_row = QHBoxLayout()
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        folder_row.addWidget(open_folder)
        folder_wrap = QWidget()
        folder_wrap.setLayout(folder_row)

        self.subfolders = QCheckBox("Include subfolders")
        self.subfolders.setChecked(config.include_subfolders)

        self.workspace = QLineEdit(config.workspace_name)
        self.quality = QComboBox()
        self.quality.addItem("High — 1080p", "1080p")
        self.quality.addItem("Balanced — 720p", "720p")
        self.quality.addItem("Performance — 540p (recommended)", "540p")
        self.quality.addItem("Lightest — 360p", "360p")
        q_index = self.quality.findData(config.quality)
        self.quality.setCurrentIndex(max(0, q_index))
        self.fps = QComboBox()
        for value in FPS_CHOICES:
            label = f"{value} fps" + (" (recommended)" if value == 24 else "")
            self.fps.addItem(label, value)
        index = self.fps.findData(config.fps)
        if index < 0:
            index = self.fps.findData(24)
        self.fps.setCurrentIndex(max(0, index))

        self.use_gpu = QCheckBox("Use GPU decode (D3D11VA)")
        self.use_gpu.setChecked(config.use_gpu_decode)
        decoder = QLabel(decoder_status or "Unknown")
        decoder.setWordWrap(True)
        decoder.setObjectName("Hint")

        self.always_on_top = QCheckBox("Keep above ProPresenter")
        self.always_on_top.setChecked(config.always_on_top)
        self.always_on_top.setToolTip(
            "Stay in front of ProPresenter, but not in front of other apps you bring forward"
        )
        self.start_windows = QCheckBox("Start with Windows")
        self.start_windows.setChecked(config.start_with_windows or startup_enabled())
        self.auto_show = QCheckBox("Show this pane when the workspace is open in ProPresenter")
        self.auto_show.setChecked(config.auto_show_on_workspace)

        self.ndi_name = QLineEdit(config.ndi_name)
        status = QLabel(ndi_status)
        status.setWordWrap(True)
        status.setObjectName("Hint")
        self.remote_url = QLabel(remote_url or "Unavailable")
        self.remote_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.remote_url.setWordWrap(True)
        remote_hint = QLabel(
            "Type http:// (not https://). Safari is more reliable than Chrome. "
            "Same Wi‑Fi as this PC. Allow the pane in Windows Firewall if asked."
        )
        remote_hint.setWordWrap(True)
        remote_hint.setObjectName("Hint")

        help_box = QTextEdit()
        help_box.setReadOnly(True)
        help_box.setPlainText(HELP)
        help_box.setFixedHeight(210)

        form = QFormLayout()
        form.addRow("Media folder", folder_wrap)
        form.addRow("", self.subfolders)
        form.addRow("Workspace name", self.workspace)
        form.addRow("Quality", self.quality)
        form.addRow("Output frame rate", self.fps)
        form.addRow("", self.use_gpu)
        form.addRow("Decoder", decoder)
        form.addRow("NDI name", self.ndi_name)
        form.addRow("NDI status", status)
        form.addRow("iPad URL", self.remote_url)
        form.addRow("", remote_hint)
        form.addRow(self.always_on_top)
        form.addRow(self.start_windows)
        form.addRow(self.auto_show)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setDefault(True)
            ok.setAutoDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(help_box)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        start = self.folder.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Media folder", start)
        if chosen:
            self.folder.setText(chosen)

    def _open_folder(self) -> None:
        path = Path(self.folder.text().strip())
        if path.is_dir():
            os.startfile(path)

    def apply_to(self, config: Config) -> None:
        config.media_folder = self.folder.text().strip()
        config.include_subfolders = self.subfolders.isChecked()
        config.workspace_name = self.workspace.text().strip() or "Evening Service"
        config.quality = str(self.quality.currentData() or "540p")
        config.fps = int(self.fps.currentData() or 24)
        config.use_gpu_decode = self.use_gpu.isChecked()
        config.ndi_name = self.ndi_name.text().strip() or "Media Background Pane"
        config.always_on_top = self.always_on_top.isChecked()
        config.start_with_windows = self.start_windows.isChecked()
        config.auto_show_on_workspace = self.auto_show.isChecked()
