from dataclasses import asdict
from pathlib import Path

from PySide6.QtWidgets import QDialog

from nsa_app.gui import MainWindow, SetupDialog
from nsa_app.jobs import JobController
from nsa_app.models import SyncConfig
from nsa_app.storage import atomic_json, app_data, export_diagnostics
from sync_and_convert import main
from tests.fixtures import make_nsa


def test_local_setup_review_and_settings(qtbot, tmp_path):
    source = tmp_path / "notes"
    source.mkdir()
    dialog = SetupDialog()
    qtbot.addWidget(dialog)
    dialog.provider.setCurrentIndex(dialog.provider.findData("local"))
    dialog.local_path.setText(str(source))
    dialog.output_path.setText(str(tmp_path / "PDFs"))
    dialog.next()
    assert dialog.pages.currentIndex() == 1
    assert dialog.config.source == str(source)
    dialog.next()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.run_now


def test_saved_dashboard_does_not_auto_sync(qtbot, tmp_path):
    cfg = SyncConfig(source=str(tmp_path), output_dir=str(tmp_path / "PDFs"))
    atomic_json(app_data() / "settings.json", asdict(cfg))
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.config.source == str(tmp_path)
    assert not window.jobs.busy
    assert "Sync now" in window.sync_button.text()


def test_spawned_gui_worker_converts_and_reports(qtbot, tmp_path):
    source = tmp_path / "notes"
    make_nsa(source / "note.nsa")
    cfg = SyncConfig(source=str(source), output_dir=str(tmp_path / "PDFs"))
    controller = JobController()
    try:
        with qtbot.waitSignal(controller.completed, timeout=30000) as signal:
            assert controller.start("sync", asdict(cfg))
            assert not controller.start("sync", asdict(cfg))
        assert signal.args[1]["status"] == "success", signal.args
        assert (tmp_path / "PDFs" / "note.pdf").exists()
        assert not controller.busy
    finally:
        controller.shutdown()


def test_cli_flags_and_exit_codes(tmp_path):
    source = tmp_path / "notes"
    make_nsa(source / "note.nsa")
    arguments = ["--provider", "local", "--local-dir", str(source), "--output-dir", str(tmp_path / "PDFs"), "--quiet"]
    assert main(arguments) == 0
    assert main(arguments + ["--force", "--no-recursive"]) == 0
    (source / "broken.nsa").write_text("bad archive")
    assert main(arguments) == 1


def test_diagnostics_excludes_settings_and_notes(tmp_path):
    secret = "private-name-and-token"
    atomic_json(app_data() / "settings.json", {"source": secret})
    (app_data() / "app.log").write_text("an arbitrary sensitive exception " + secret)
    destination = tmp_path / "diagnostics.json"
    export_diagnostics(destination)
    assert secret not in destination.read_text()
