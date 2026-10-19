import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from poc.alignment import AlignmentError
from poc.core import align, render_pdf, templates
from poc.server import app


pytest.importorskip("cv2")
SAMPLE = Path(__file__).resolve().parents[1] / "data/sample/intake-2.pdf"


def sample_page():
    return render_pdf(SAMPLE.read_bytes())[0]


def test_background_rotation_and_whitespace_crop_align():
    page = sample_page()
    rotated = page.rotate(7, expand=True, fillcolor="#777777")
    photo = Image.new("RGB", (1250, 1550), "#777777")
    photo.paste(rotated, ((photo.width - rotated.width)//2, (photo.height - rotated.height)//2))
    for image in (photo, page.crop((40, 40, 810, 1060))):
        aligned, info = align(image, templates()["intake"])
        assert aligned.size == (850, 1100)
        assert info["method"] == "reference"
        assert info["inliers"] >= 15


def test_perspective_photo_aligns():
    import cv2
    import numpy as np

    page = np.asarray(sample_page())
    height, width = page.shape[:2]
    source = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
    destination = np.float32([[120, 100], [900, 60], [980, 1250], [80, 1200]])
    transform = cv2.getPerspectiveTransform(source, destination)
    photo = cv2.warpPerspective(page, transform, (1100, 1350), borderValue=(100, 100, 100))
    aligned, info = align(Image.fromarray(photo), templates()["intake"])
    assert aligned.size == (850, 1100)
    assert info["median_error_px"] < 4


def test_page_outline_fallback_is_verified_against_reference(monkeypatch):
    import poc.alignment as registration

    page = sample_page()
    photo = Image.new("RGB", (1250, 1500), "#777777")
    photo.paste(page, (200, 200))
    original_register = registration._register
    attempts = 0

    def skip_direct_match(*args):
        nonlocal attempts
        attempts += 1
        return None if attempts == 1 else original_register(*args)

    monkeypatch.setattr(registration, "_register", skip_direct_match)
    _, info = align(photo, templates()["intake"])
    assert attempts == 2
    assert info["page_detection"] == "page_outline"


def test_wrong_form_and_cut_field_are_rejected():
    template = templates()["intake"]
    with pytest.raises(AlignmentError, match="Could not match"):
        align(Image.new("RGB", (850, 1100), "white"), template)
    with pytest.raises(AlignmentError, match="outside the image"):
        align(sample_page().crop((200, 0, 851, 1100)), template)


def test_dense_bank_sample_aligns_without_accepting_a_different_template():
    bank = render_pdf((SAMPLE.parent / "bank-2.pdf").read_bytes())[0]
    aligned, info = align(bank, templates()["bank_account"])
    assert aligned.size == (850, 1100)
    assert info["inliers"] >= 50
    with pytest.raises(AlignmentError, match="Could not match"):
        align(bank, templates()["intake"])


def test_image_upload_and_user_facing_alignment_error(tmp_path, monkeypatch):
    from poc import server
    import poc.template_store as store

    monkeypatch.setattr(server, "STORE", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "templates.sqlite3")
    client = TestClient(app)
    page = sample_page()
    data = io.BytesIO()
    page.save(data, format="PNG")
    uploaded = client.post("/api/documents", files={"file": ("form.png", data.getvalue(), "image/png")})
    assert uploaded.status_code == 200
    document_id = uploaded.json()["document_id"]
    assert (tmp_path / document_id / "source.png").exists()
    run = client.post("/api/run", json={"document_id": document_id,
                                         "template_id": "intake", "pipelines": ["tesseract"]})
    assert run.status_code == 200
    assert run.json()["aligned_pages"]
    aligned = client.get(run.json()["aligned_pages"][0])
    assert aligned.status_code == 200
    assert aligned.headers["content-type"].startswith("image/png")
    repeated = client.post("/api/run", json={"document_id": document_id,
                                              "template_id": "intake", "pipelines": ["tesseract"]})
    assert repeated.status_code == 200
    assert repeated.json()["run_id"] != run.json()["run_id"]
    assert client.get(run.json()["aligned_pages"][0]).content == aligned.content
    draft = templates()["intake"].copy()
    draft["id"] = "image_reference"
    draft["name"] = "Image reference"
    registered = client.post("/api/templates", json={"template": draft,
                                                     "source_document_id": document_id})
    assert registered.status_code == 201
    custom_run = client.post("/api/run", json={"document_id": document_id,
                                                "template_id": "image_reference",
                                                "pipelines": ["tesseract"]})
    assert custom_run.status_code == 200
    assert custom_run.json()["results"]["tesseract"]["status"] == "ok"

    blank = io.BytesIO()
    Image.new("RGB", (850, 1100), "white").save(blank, format="PNG")
    uploaded = client.post("/api/documents", files={"file": ("blank.png", blank.getvalue(), "image/png")})
    rejected = client.post("/api/run", json={"document_id": uploaded.json()["document_id"],
                                              "template_id": "intake", "pipelines": ["tesseract"]})
    assert rejected.status_code == 422
    assert "Could not match" in rejected.json()["detail"]
    bad_reference = client.post("/api/templates", json={"template": {**draft, "id": "blank_reference"},
                                                        "source_document_id": uploaded.json()["document_id"]})
    assert bad_reference.status_code == 422
    assert "Reference page 1" in bad_reference.json()["detail"]


def test_multi_page_image_is_rejected(tmp_path, monkeypatch):
    from poc import server

    monkeypatch.setattr(server, "STORE", tmp_path)
    first = Image.new("RGB", (850, 1100), "white")
    second = Image.new("RGB", (850, 1100), "black")
    data = io.BytesIO()
    first.save(data, format="TIFF", save_all=True, append_images=[second])
    response = TestClient(app).post("/api/documents", files={"file": ("pages.tiff", data.getvalue(), "image/tiff")})
    assert response.status_code == 400
    assert "Multi-page" in response.json()["detail"]


def test_large_image_is_reduced_before_storage(tmp_path, monkeypatch):
    from poc import server

    monkeypatch.setattr(server, "STORE", tmp_path)
    monkeypatch.setattr(server, "MAX_PAGE_PIXELS", 500_000)
    data = io.BytesIO()
    sample_page().save(data, format="PNG")
    response = TestClient(app).post("/api/documents", files={"file": ("form.png", data.getvalue(), "image/png")})
    assert response.status_code == 200
    path = tmp_path / response.json()["document_id"] / "page-1.png"
    with Image.open(path) as saved:
        assert saved.width * saved.height <= 500_000
