import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBRIDGE_DATA_DIR", str(tmp_path / "appdata"))
    logger = logging.getLogger("nsa")
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    yield
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
