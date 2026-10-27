import pytest
from fastapi.testclient import TestClient

from poc.core import render_pdf
from poc.dynamic_extract import extract, normalize_schema, prepare_page
from poc.server import app


def test_schema_requires_valid_keys_and_keeps_descriptions():
    schema = normalize_schema({"applicant_name": {"description": "Person applying", "aliases": ["Full name"]}})
    assert schema[0]["key"] == "applicant_name"
    assert schema[0]["aliases"] == ["Full name"]
    with pytest.raises(ValueError):
        normalize_schema({"bad key": "Name"})


def test_dynamic_route_requires_uploaded_pdf_and_schema():
    client = TestClient(app)
    assert client.get("/dynamic").status_code == 200
    assert client.post("/api/dynamic/extract", json={"document_id": "0" * 32, "schema": {"name": "Name"}}).status_code == 404


def test_local_ocr_reads_requested_fields_on_unregistered_form(monkeypatch):
    import poc.dynamic_extract as dynamic
    monkeypatch.setattr(dynamic, "_paddle_lines", lambda image: [])
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    pdf = open("data/sample/intake-1.pdf", "rb").read()
    pages = render_pdf(pdf)
    _, events = prepare_page(pages[0])
    assert events == []  # a full-page scan does not need corrective cropping
    result = extract(pages, pdf, {"full_name": {"aliases": ["Full name"]},
                                  "date": {"aliases": ["Date"]},
                                  "reference": {"aliases": ["Reference"]}})
    assert result["values"] == {"full_name": "Ada Lovelace", "date": "16 Oct 2026", "reference": "REF-1042"}
    assert all(field["uncertain"] for field in result["fields"])


def test_upload_then_dynamic_extract_without_registration(monkeypatch, tmp_path):
    import poc.server as server
    import poc.dynamic_extract as dynamic
    monkeypatch.setattr(server, "STORE", tmp_path)
    monkeypatch.setattr(dynamic, "_paddle_lines", lambda image: [])
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = TestClient(app)
    with open("data/sample/intake-1.pdf", "rb") as source:
        uploaded = client.post("/api/documents", files={"file": ("intake.pdf", source, "application/pdf")})
    assert uploaded.status_code == 200
    response = client.post("/api/dynamic/extract", json={"document_id": uploaded.json()["document_id"],
        "schema": {"full_name": {"aliases": ["Full name"]}, "reference": {"aliases": ["Reference"]}}})
    assert response.status_code == 200
    assert response.json()["values"] == {"full_name": "Ada Lovelace", "reference": "REF-1042"}
    with open("data/sample/intake-1.pdf", "rb") as source:
        repeated = client.post("/api/documents", files={"file": ("again.pdf", source, "application/pdf")})
    repeat_result = client.post("/api/dynamic/extract", json={"document_id": repeated.json()["document_id"],
        "schema": {"date": {"aliases": ["Date"]}}})
    assert repeat_result.json()["values"] == {"date": "16 Oct 2026"}
    assert repeat_result.json()["cache_hit"] is True


def test_photo_page_is_rectified_when_perspective_is_visible():
    import cv2
    import numpy as np
    from PIL import Image
    with open("data/sample/intake-1.pdf", "rb") as source:
        image = np.asarray(render_pdf(source.read())[0])
    height, width = image.shape[:2]
    matrix = cv2.getPerspectiveTransform(np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]),
                                         np.float32([[100, 80], [1030, 105], [970, 1360], [90, 1320]]))
    photo = Image.fromarray(cv2.warpPerspective(image, matrix, (1120, 1460), borderValue=(80, 80, 80)))
    _, corrections = prepare_page(photo)
    assert "page_rectified" in corrections


def test_dynamic_route_stays_local_even_when_gemini_is_configured(monkeypatch):
    import poc.dynamic_extract as dynamic
    monkeypatch.setattr(dynamic, "_paddle_lines", lambda image: [])
    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    with open("data/sample/intake-1.pdf", "rb") as source:
        pdf = source.read()
    result = extract(render_pdf(pdf), pdf, {"full_name": {"aliases": ["Full name"]},
                                            "unlisted_field": "Not present on this form"})
    assert result["method"] == "tesseract_ocr"
    assert result["values"] == {"full_name": "Ada Lovelace", "unlisted_field": None}
    assert result["fields"][1]["uncertain"] is True


def test_paddle_positioned_text_maps_inline_and_adjacent_answers(monkeypatch):
    from PIL import Image
    import poc.dynamic_extract as dynamic
    monkeypatch.setattr(dynamic, "_paddle_lines", lambda image: [
        {"text": "Full name Ada Lovelace", "box": [20, 20, 250, 50]},
        {"text": "Date", "box": [20, 80, 70, 110]},
        {"text": "16 Oct 2026", "box": [160, 80, 300, 110]},
    ])
    result = extract([Image.new("RGB", (1000, 1400), "white")], None,
                     {"applicant_name": {"aliases": ["Full name"]}, "date": {"aliases": ["Date"]}})
    assert result["method"] == "paddle_ocr"
    assert result["values"] == {"applicant_name": "Ada Lovelace", "date": "16 Oct 2026"}
    assert result["pages"][0]["paddle_lines"] == 3


def test_schema_edits_reuse_document_ocr(monkeypatch):
    from PIL import Image
    import poc.dynamic_extract as dynamic
    calls = {"paddle": 0, "tesseract": 0}

    def paddle_lines(image):
        calls["paddle"] += 1
        return [{"text": "Full name Ada Lovelace", "box": [20, 20, 250, 50]},
                {"text": "Reference REF-1042", "box": [20, 80, 250, 110]}]

    def tesseract_lines(image):
        calls["tesseract"] += 1
        return []

    monkeypatch.setattr(dynamic, "_paddle_lines", paddle_lines)
    monkeypatch.setattr(dynamic, "_ocr_lines", tesseract_lines)
    image = Image.new("RGB", (1000, 1400), "white")
    first = extract([image], raw_schema={"full_name": {"aliases": ["Full name"]}}, cache_key="test-schema-edit-cache")
    second = extract([image], raw_schema={"reference": {"aliases": ["Reference"]}}, cache_key="test-schema-edit-cache")
    assert first["values"] == {"full_name": "Ada Lovelace"}
    assert second["values"] == {"reference": "REF-1042"}
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert calls == {"paddle": 1, "tesseract": 1}
