from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
import sys

from PySide6.QtCore import QLockFile, QStandardPaths, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .auth import parse_folder
from .jobs import JobController
from .models import SyncConfig
from .storage import app_data, atomic_json, export_diagnostics, profile_dir, read_json, setup_logging


STYLE = """
QWidget { font-family: 'Segoe UI'; font-size: 10pt; color: #20332f; }
QMainWindow, QDialog { background: #f5f7f6; }
QLabel#title { font-size: 23pt; font-weight: 650; }
QLabel#heading { font-size: 17pt; font-weight: 650; }
QLabel#muted { color: #566762; }
QLabel#error { color: #a33029; }
QFrame#card { background: white; border: 1px solid #dce5df; border-radius: 12px; }
QPushButton { background: white; padding: 9px 15px; border: 1px solid #bccbc4; border-radius: 6px; }
QPushButton:hover { background: #eaf1ed; }
QPushButton:focus { border: 2px solid #19836a; }
QPushButton:disabled { color: #7a8781; background: #edf0ee; }
QPushButton#primary { background: #16654f; color: white; border: 1px solid #16654f; font-weight: 600; }
QPushButton#primary:hover { background: #0c503d; }
QPushButton#primary:disabled { background: #8ba89d; border-color: #8ba89d; }
QLineEdit, QComboBox, QListWidget, QTableWidget { background: white; border: 1px solid #bccbc4; border-radius: 5px; padding: 7px; selection-background-color: #d2eadd; selection-color: #153f30; }
QHeaderView::section { background: #edf3ef; border: none; padding: 8px; font-weight: 600; }
QProgressBar { border: none; background: #dde8e1; border-radius: 4px; min-height: 8px; max-height: 8px; }
QProgressBar::chunk { background: #258264; border-radius: 4px; }
"""


def label(text: str, name: str = "", wrap: bool = True) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if name:
        widget.setObjectName(name)
    return widget


def button(text: str, callback, primary: bool = False) -> QPushButton:
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def row(*widgets) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    for child in widgets:
        layout.addWidget(child)
    return widget


class FolderBrowser(QDialog):
    """Native, paginated Drive folder picker; all requests run outside Qt."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose a Google Drive folder")
        self.resize(620, 470)
        self.path = [("root", "My Drive")]
        self.selected_folder = None
        self.next_token = None
        self.jobs = JobController(self)
        self.jobs.completed.connect(self.completed)
        self.jobs.failed.connect(self.failed)
        self.jobs.busy_changed.connect(self.set_busy)
        layout = QVBoxLayout(self)
        self.breadcrumb = QComboBox()
        self.breadcrumb.setAccessibleName("Folder breadcrumbs")
        self.breadcrumb.activated.connect(self.jump)
        self.up = button("&Up", self.go_up)
        layout.addWidget(row(self.up, self.breadcrumb))
        self.folders = QListWidget()
        self.folders.setAccessibleName("Google Drive subfolders")
        self.folders.itemDoubleClicked.connect(self.enter)
        layout.addWidget(self.folders, 1)
        self.message = label("", "muted")
        layout.addWidget(self.message)
        self.more = button("Load &more", lambda: self.load(append=True))
        self.open_button = button("&Open folder", lambda: self.enter(self.folders.currentItem()))
        self.choose = button("&Use this folder", self.select, True)
        layout.addWidget(row(self.more, self.open_button, self.choose))
        layout.addWidget(label("Browse into your backup folder, then choose Use this folder. Shared folders can also be selected by pasting their link in setup.", "muted"))
        QTimer.singleShot(0, self.load)

    def load(self, append=False):
        if self.jobs.busy:
            return
        if not append:
            self.folders.clear()
            self.next_token = None
        self.breadcrumb.clear()
        for _, name in self.path:
            self.breadcrumb.addItem(name)
        self.breadcrumb.setCurrentIndex(len(self.path) - 1)
        self.message.setText("Loading folders…")
        self.jobs.start("folders", {"folder_id": self.path[-1][0], "token": self.next_token})

    def completed(self, kind, data):
        for folder in data.get("files", []):
            item = QListWidgetItem(folder["name"])
            item.setData(Qt.ItemDataRole.UserRole, folder)
            self.folders.addItem(item)
        self.next_token = data.get("nextPageToken")
        self.more.setEnabled(bool(self.next_token))
        self.message.setText("Double-click a folder to browse inside." if self.folders.count() else "This folder has no subfolders. You can select it.")

    def failed(self, kind, message):
        self.message.setText(message)
        self.choose.setEnabled(False)

    def set_busy(self, busy):
        for control in (self.folders, self.breadcrumb, self.open_button, self.choose):
            control.setEnabled(not busy)
        self.up.setEnabled(not busy and len(self.path) > 1)
        self.more.setEnabled(not busy and bool(self.next_token))

    def enter(self, item):
        if item is not None and not self.jobs.busy:
            folder = item.data(Qt.ItemDataRole.UserRole)
            self.path.append((folder["id"], folder["name"]))
            self.load()

    def go_up(self):
        if len(self.path) > 1:
            self.path.pop()
            self.load()

    def jump(self, index):
        self.path = self.path[:index + 1]
        self.load()

    def select(self):
        self.selected_folder = self.path[-1]
        self.accept()

    def done(self, result):
        self.jobs.shutdown()
        super().done(result)


class SetupDialog(QDialog):
    def __init__(self, config: SyncConfig | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings — NoteBridge" if config else "Welcome to NoteBridge")
        self.resize(700, 620)
        self.editing = config is not None
        self.config = config
        self.run_now = False
        self.validated_folder = None
        self.pending_review = False
        self.jobs = JobController(self)
        self.jobs.completed.connect(self.completed)
        self.jobs.failed.connect(self.failed)
        self.jobs.busy_changed.connect(self.set_busy)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        layout.addWidget(label("Your notes, ready to read.", "heading"))
        layout.addWidget(label("Choose your backup folder and where you want the PDFs. Everything is converted on this computer.", "muted"))
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        form_page = QWidget()
        form_layout = QVBoxLayout(form_page)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setVerticalSpacing(18)
        self.format = QComboBox()
        self.format.addItem("Noteshelf (.nsa)", "noteshelf")
        self.format.addItem("Notein (.notein / ZIP)", "notein")
        self.provider = QComboBox()
        self.provider.addItem("Google Drive", "gdrive")
        self.provider.addItem("Local folder", "local")
        form.addRow("Note &format", self.format)
        form.addRow("&Source", self.provider)
        self.local_path = QLineEdit()
        self.local_path.setPlaceholderText("Choose the folder containing your backups")
        self.local_browse = button("&Browse…", self.browse_local)
        self.local_row = row(self.local_path, self.local_browse)
        self.local_label = label("Backup folder")
        self.local_label.setBuddy(self.local_path)
        form.addRow(self.local_label, self.local_row)
        self.drive_widget = QWidget()
        drive_layout = QVBoxLayout(self.drive_widget)
        drive_layout.setContentsMargins(0, 0, 0, 0)
        self.connect = button("&Connect Google Drive", lambda: self.jobs.start("connect"))
        self.browse_drive = button("Choose &Drive folder…", self.choose_drive)
        drive_layout.addWidget(row(self.connect, self.browse_drive))
        self.drive_path = QLineEdit()
        self.drive_path.setPlaceholderText("Or paste a Google Drive folder link")
        self.drive_path.setAccessibleName("Google Drive folder link or ID")
        self.drive_path.textChanged.connect(lambda: setattr(self, "validated_folder", None))
        drive_layout.addWidget(self.drive_path)
        self.drive_label = label("Google Drive")
        form.addRow(self.drive_label, self.drive_widget)
        self.output_path = QLineEdit()
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        self.output_path.setText(str(Path(documents or Path.home() / "Documents") / "Converted Notes"))
        self.output_browse = button("&Choose…", self.browse_output)
        form.addRow("&Save PDFs to", row(self.output_path, self.output_browse))
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(True)
        form.addRow("", self.recursive)
        form_layout.addLayout(form)
        form_layout.addWidget(label("First enable backups in Noteshelf or Notein. This app reads those backups; it does not sync directly with your tablet.", "muted"))
        form_layout.addStretch()
        self.pages.addWidget(form_page)
        review_page = QWidget()
        review_layout = QVBoxLayout(review_page)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.addWidget(label("Ready when you are", "heading"))
        self.review_text = label("")
        self.review_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        review_layout.addWidget(self.review_text)
        review_layout.addWidget(label("Only changed notes will be converted. Existing PDFs are retained when a source note is removed or renamed. Syncing starts only when you click Sync now.", "muted"))
        review_layout.addStretch()
        self.pages.addWidget(review_page)
        self.message = label("", "error")
        layout.addWidget(self.message)
        self.disconnect_button = button("Disconnect Google", lambda: self.jobs.start("disconnect"))
        self.back = button("&Back", self.go_back)
        self.cancel_button = button("Cancel", self.reject)
        self.next_button = button("&Review setup", self.next, True)
        self.save_button = button("&Save settings", self.save)
        self.save_button.setVisible(False)
        layout.addWidget(row(self.disconnect_button, self.back, self.cancel_button, self.save_button, self.next_button))
        self.back.setVisible(False)
        self.provider.currentIndexChanged.connect(self.source_changed)
        if config:
            self.format.setCurrentIndex(self.format.findData(config.note_format))
            self.provider.setCurrentIndex(self.provider.findData(config.provider))
            if config.provider == "gdrive":
                self.drive_path.setText(config.source)
                self.validated_folder = (config.source, config.source_name or config.source)
            else:
                self.local_path.setText(config.source)
            self.output_path.setText(config.output_dir)
            self.recursive.setChecked(config.recursive)
        self.source_changed()

    def source_changed(self):
        drive = self.provider.currentData() == "gdrive"
        for control in (self.drive_widget, self.drive_label, self.disconnect_button):
            control.setVisible(drive)
        for control in (self.local_row, self.local_label):
            control.setVisible(not drive)

    def browse_local(self):
        path = QFileDialog.getExistingDirectory(self, "Choose backup folder", self.local_path.text())
        if path:
            self.local_path.setText(path)

    def browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Choose PDF destination", self.output_path.text())
        if path:
            self.output_path.setText(path)

    def choose_drive(self):
        dialog = FolderBrowser(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            folder_id, name = dialog.selected_folder
            self.drive_path.setText(folder_id)
            # Root alias resolves to the actual folder ID before saving.
            self.jobs.start("folder", {"folder_id": folder_id})

    def set_busy(self, busy):
        self.pages.setEnabled(not busy)
        self.next_button.setEnabled(not busy)
        self.disconnect_button.setEnabled(not busy)
        self.save_button.setEnabled(not busy)
        if busy:
            self.message.setText("Complete sign-in in your browser…" if self.jobs.kind == "connect" else "Contacting Google Drive…")

    def completed(self, kind, data):
        if kind == "connect":
            self.message.setText("Google Drive connected. Choose your backup folder.")
            self.connect.setText("&Reconnect Google Drive")
        elif kind == "folder":
            self.drive_path.setText(data["id"])
            self.validated_folder = (data["id"], data["name"])
            self.message.setText("Selected: " + data["name"])
            if self.pending_review:
                self.pending_review = False
                self.next()
        elif kind == "disconnect":
            self.validated_folder = None
            self.message.setText("Google credentials removed." if data["revoked"] else
                                 "Local credentials removed. Google could not confirm revocation; you can remove access in your Google account settings.")

    def failed(self, kind, message):
        self.pending_review = False
        self.message.setText(message)

    def next(self):
        if self.pages.currentIndex() == 1:
            self.run_now = True
            self.accept()
            return
        try:
            output = self.output_path.text().strip()
            if not output or not Path(output).is_absolute():
                raise ValueError("Choose a full destination folder path for the PDFs.")
            provider = self.provider.currentData()
            if provider == "gdrive":
                folder_id = parse_folder(self.drive_path.text())
                if not self.validated_folder or self.validated_folder[0] != folder_id:
                    self.pending_review = True
                    self.jobs.start("folder", {"folder_id": folder_id})
                    return
                source, source_name = self.validated_folder
            else:
                source = str(Path(self.local_path.text().strip()).expanduser().resolve())
                if not self.local_path.text().strip() or not Path(source).is_dir():
                    raise ValueError("Choose an existing backup folder.")
                if Path(output).resolve() == Path(source):
                    raise ValueError("Choose a PDF destination different from the backup folder.")
                source_name = Path(source).name
            self.config = SyncConfig(provider=provider, note_format=self.format.currentData(), source=source,
                                     output_dir=str(Path(output).resolve()), source_name=source_name,
                                     recursive=self.recursive.isChecked())
            self.config.validate()
            self.review_text.setText(f"Format: {self.format.currentText()}\n\nSource: {self.provider.currentText()} — {source_name}\n\nPDF folder: {self.config.output_dir}\n\nInclude subfolders: {'Yes' if self.config.recursive else 'No'}")
            self.pages.setCurrentIndex(1)
            self.next_button.setText("&Sync now")
            self.save_button.setVisible(self.editing)
            self.disconnect_button.hide()
            self.back.show()
            self.message.clear()
        except (ValueError, OSError) as exc:
            self.message.setText(str(exc))

    def go_back(self):
        self.pages.setCurrentIndex(0)
        self.next_button.setText("&Review setup")
        self.back.hide()
        self.save_button.hide()
        self.source_changed()

    def save(self):
        self.run_now = False
        self.accept()

    def done(self, result):
        self.jobs.shutdown()
        super().done(result)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NoteBridge")
        self.resize(940, 710)
        self.setMinimumSize(740, 560)
        self.config = None
        self.close_when_done = False
        self.rows = {}
        self.jobs = JobController(self)
        self.jobs.progress.connect(self.on_progress)
        self.jobs.completed.connect(self.on_completed)
        self.jobs.failed.connect(self.on_failed)
        self.jobs.busy_changed.connect(self.set_busy)
        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(32, 28, 32, 22)
        layout.setSpacing(18)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(label("NoteBridge", "title"))
        titles.addWidget(label("Note converter", "muted"))
        header.addLayout(titles, 1)
        self.settings_button = button("&Settings", self.open_settings)
        header.addWidget(self.settings_button)
        layout.addLayout(header)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 20, 24, 20)
        card_layout.setSpacing(12)
        self.status = label("Make your notes easier to read.", "heading")
        self.detail = label("Connect a backup folder to create PDFs on this computer.", "muted")
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setValue(0)
        self.source_label = label("Source: Not configured", "muted")
        self.destination_label = label("PDF folder: Not configured", "muted")
        self.last_label = label("No successful sync yet", "muted")
        for widget in (self.status, self.detail, self.progress_bar, self.source_label, self.destination_label, self.last_label):
            card_layout.addWidget(widget)
        actions = QHBoxLayout()
        self.sync_button = button("&Get started", self.start_sync, True)
        self.cancel_button = button("&Cancel", self.cancel_sync)
        self.cancel_button.hide()
        self.reconnect_button = button("&Reconnect Google", lambda: self.jobs.start("connect"))
        self.reconnect_button.hide()
        self.open_button = button("&Open PDFs", self.open_pdfs)
        actions.addWidget(self.sync_button)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.reconnect_button)
        actions.addStretch()
        actions.addWidget(self.open_button)
        card_layout.addLayout(actions)
        layout.addWidget(card)
        self.force = QCheckBox("Reconvert all notes on the next sync")
        self.force.setToolTip("Use this if you want to rebuild PDFs even when the backups have not changed.")
        layout.addWidget(self.force)
        layout.addWidget(label("Sync results", "heading"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Note", "Result", "Details"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        footer.addWidget(label(f"Public beta {__version__}  ·  Converted on your computer", "muted"), 1)
        self.diagnostics_button = button("Export &diagnostics…", self.diagnostics)
        footer.addWidget(self.diagnostics_button)
        layout.addLayout(footer)
        try:
            data = read_json(app_data() / "settings.json")
            if data:
                allowed = {"provider", "note_format", "source", "source_name", "output_dir", "recursive"}
                self.config = SyncConfig(**{key: value for key, value in data.items() if key in allowed})
                self.config.validate()
        except (ValueError, TypeError, OSError):
            self.config = None
        self.refresh_config()

    def refresh_config(self):
        if self.config:
            name = self.config.source_name or self.config.source
            self.source_label.setText(f"Source: {'Google Drive' if self.config.provider == 'gdrive' else 'Local folder'} / {name}")
            self.destination_label.setText(f"PDF folder: {self.config.output_dir}")
            self.sync_button.setText("&Sync now")
            self.status.setText("Ready to sync")
            self.detail.setText("Click Sync now to check for new or changed notes.")
            try:
                state = read_json(profile_dir(self.config) / "state.json")
                self.show_last_success(state.get("last_success"))
            except OSError:
                self.show_last_success(None)
        self.open_button.setEnabled(self.config is not None)
        self.force.setEnabled(self.config is not None)

    def show_last_success(self, value):
        try:
            formatted = datetime.fromisoformat(value).astimezone().strftime("%d %b %Y, %H:%M") if value else None
        except (ValueError, TypeError):
            formatted = None
        self.last_label.setText("Last successful sync: " + formatted if formatted else "No successful sync yet")

    def open_settings(self):
        if self.jobs.busy:
            return
        dialog = SetupDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                data = asdict(dialog.config)
                for key in ("force", "cache_dir", "legacy_state", "oauth_client"):
                    data.pop(key, None)
                atomic_json(app_data() / "settings.json", data)
            except OSError as exc:
                QMessageBox.warning(self, "Could not save settings", str(exc))
                return
            self.config = dialog.config
            self.refresh_config()
            if dialog.run_now:
                self.start_sync()

    def start_sync(self):
        if not self.config:
            self.open_settings()
            return
        if self.jobs.busy:
            return
        self.table.setRowCount(0)
        self.rows.clear()
        self.status.setText("Syncing your notes…")
        self.detail.setText("Looking for backed-up notes…")
        self.progress_bar.setRange(0, 0)
        self.reconnect_button.hide()
        payload = asdict(self.config)
        payload["force"] = self.force.isChecked()
        self.jobs.start("sync", payload)

    def set_busy(self, busy):
        for control in (self.sync_button, self.settings_button, self.force, self.reconnect_button, self.diagnostics_button):
            control.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy)

    def cancel_sync(self):
        self.jobs.cancel()
        self.detail.setText("Cancelling… Completed PDFs will be kept.")
        self.cancel_button.setEnabled(False)

    def on_progress(self, event):
        if event["stage"] == "finished":
            return
        self.detail.setText((event.get("file", "") + " — " if event.get("file") else "") + event.get("message", ""))
        total = event.get("total", 0)
        if total:
            # Scale byte totals to avoid overflowing Qt's signed 32-bit range.
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(1000 * event.get("completed", 0) / total))
        else:
            self.progress_bar.setRange(0, 0)
        if event["stage"] == "file":
            index = self.table.rowCount()
            self.table.insertRow(index)
            for column, text in enumerate((event.get("file", ""), event.get("status", "").replace("_", " ").title(), event.get("message", ""))):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(index, column, item)
            self.table.scrollToBottom()

    def on_completed(self, kind, result):
        if kind == "connect":
            self.status.setText("Google Drive reconnected")
            self.detail.setText("Click Sync now to retry.")
            self.reconnect_button.hide()
        elif kind == "sync":
            titles = {"success": "Your PDFs are ready", "up_to_date": "Already up to date", "no_files": "No supported notes found",
                      "partial_failure": "Some notes need attention", "failed": "Sync could not finish", "cancelled": "Sync cancelled",
                      "authentication_required": "Reconnect Google Drive"}
            self.status.setText(titles.get(result["status"], "Sync finished"))
            self.detail.setText(f"{result.get('converted', 0)} converted · {result.get('skipped', 0)} unchanged · {result.get('failed', 0)} failed\n" + result.get("message", ""))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100 if result["status"] in ("success", "up_to_date", "no_files") else 0)
            self.reconnect_button.setVisible(result["status"] == "authentication_required")
            self.force.setChecked(False)
            if result.get("last_success"):
                self.show_last_success(result["last_success"])
        if self.close_when_done:
            self.close()

    def on_failed(self, kind, message):
        self.status.setText("The operation could not finish")
        self.detail.setText(message)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        if self.close_when_done:
            self.close()

    def open_pdfs(self):
        if self.config:
            path = Path(self.config.output_dir)
            if path.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            else:
                QMessageBox.information(self, "PDF folder", "The PDF folder will be created when you sync.")

    def diagnostics(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export anonymous diagnostics", "NoteBridge-diagnostics.json", "JSON (*.json)")
        if path:
            try:
                export_diagnostics(Path(path))
                QMessageBox.information(self, "Diagnostics saved", "This file contains app versions and sync counts. Notes, folder paths, and credentials are excluded.")
            except OSError as exc:
                QMessageBox.warning(self, "Could not export diagnostics", str(exc))

    def closeEvent(self, event):
        if self.jobs.busy:
            box = QMessageBox(self)
            box.setWindowTitle("A job is still running")
            box.setText("Keep this window open to finish, or cancel and exit?")
            keep = box.addButton("Keep syncing", QMessageBox.ButtonRole.RejectRole)
            box.addButton("Cancel and exit", QMessageBox.ButtonRole.DestructiveRole)
            box.setDefaultButton(keep)
            box.exec()
            if box.clickedButton() is not keep:
                self.close_when_done = True
                self.cancel_sync()
            event.ignore()
        else:
            event.accept()


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("NoteBridge")
    application.setOrganizationName("NoteBridge")
    application.setApplicationVersion(__version__)
    application.setStyle("Fusion")
    application.setStyleSheet(STYLE)
    setup_logging()
    lock = QLockFile(str(app_data() / "desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "NoteBridge is already open", "Use the existing NoteBridge window.")
        return 0
    window = MainWindow()
    application.aboutToQuit.connect(window.jobs.shutdown)
    window.show()
    if not window.config:
        QTimer.singleShot(100, window.open_settings)
    code = application.exec()
    lock.unlock()
    return code
