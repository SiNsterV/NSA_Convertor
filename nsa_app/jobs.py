from __future__ import annotations

import multiprocessing
import queue
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .worker import run_job


class JobController(QObject):
    progress = Signal(dict)
    completed = Signal(str, dict)
    failed = Signal(str, str)
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.messages = None
        self.cancel_event = None
        self.kind = ""
        self.callback_received = False
        self.cancel_time = None
        self.dead_time = None
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.poll)

    @property
    def busy(self):
        return self.process is not None

    def start(self, kind: str, payload: dict | None = None):
        if self.busy:
            return False
        context = multiprocessing.get_context("spawn")
        self.messages = context.Queue()
        self.cancel_event = context.Event()
        self.kind = kind
        self.callback_received = False
        self.cancel_time = None
        self.dead_time = None
        self.process = context.Process(target=run_job, args=(kind, payload or {}, self.messages, self.cancel_event), daemon=True)
        try:
            self.process.start()
        except Exception as exc:
            self.process = None
            self.failed.emit(kind, str(exc))
            return False
        self.busy_changed.emit(True)
        self.timer.start()
        return True

    def cancel(self):
        if self.busy and self.cancel_time is None:
            self.cancel_event.set()
            self.cancel_time = time.monotonic()

    def poll(self):
        if not self.busy:
            return
        for _ in range(200):
            try:
                category, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if category == "progress":
                self.progress.emit(value)
            else:
                self.callback_received = True
                self.pending_response = (category, value)
        if self.cancel_time and time.monotonic() - self.cancel_time > 5 and self.process.is_alive():
            self.process.terminate()
        if not self.process.is_alive():
            if not self.callback_received and self.dead_time is None:
                self.dead_time = time.monotonic()
                return
            if not self.callback_received and time.monotonic() - self.dead_time < 0.3:
                return
            kind = self.kind
            response = getattr(self, "pending_response", None) if self.callback_received else None
            cancelled = self.cancel_time is not None
            self.process.join(timeout=0.1)
            self.process.close()
            self.process = None
            self.messages.close()
            self.messages = None
            self.timer.stop()
            self.busy_changed.emit(False)
            if response and response[0] == "result":
                self.completed.emit(kind, response[1])
            elif cancelled:
                if kind == "sync":
                    self.completed.emit(kind, {"status": "cancelled", "message": "Sync cancelled. Completed PDFs were kept.", "converted": 0, "skipped": 0, "failed": 0})
                else:
                    self.failed.emit(kind, "Operation cancelled.")
            else:
                message = response[1]["message"] if response else "The worker stopped unexpectedly. Completed PDFs were kept. Please retry."
                self.failed.emit(kind, message)

    def shutdown(self):
        if self.busy:
            self.cancel()
            self.process.join(timeout=0.2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1)
            self.process.close()
            self.process = None
            self.messages.close()
            self.timer.stop()
