from pathlib import Path

from PIL import Image, ImageChops, ImageStat
import pymupdf
import pytest

from notein_extract import notein_to_pdf
from nsa_convertor import nsa_to_pdf
from tests.fixtures import make_nsa, make_notein


@pytest.mark.parametrize("kind", ["noteshelf", "notein"])
def test_synthetic_rendering(tmp_path, kind):
    destination = tmp_path / "output.pdf"
    if kind == "noteshelf":
        source = make_nsa(tmp_path / "note.nsa", rich=True)
        nsa_to_pdf(str(source), str(destination), verbose=False)
    else:
        source = make_notein(tmp_path / "note.notein")
        notein_to_pdf(source, destination)
    with pymupdf.open(destination) as document:
        assert document.page_count == 1
        assert "Synthetic" in document[0].get_text()
        assert len(document[0].get_images()) >= 1
        assert len(document[0].get_drawings()) >= 2
        pixmap = document[0].get_pixmap(alpha=False)
        actual = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    baseline = Path(__file__).parent / "baselines" / (kind + ".png")
    assert baseline.exists(), "Reviewed baseline is required"
    expected = Image.open(baseline).convert("RGB")
    assert actual.size == expected.size
    assert max(ImageStat.Stat(ImageChops.difference(actual, expected)).mean) < 0.5
