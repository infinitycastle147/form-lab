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


def test_unregistered_marked_choices_use_pixels_not_ocr_text(monkeypatch):
    from PIL import Image, ImageDraw
    import poc.dynamic_extract as dynamic
    image = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(image)
    for y in (150, 190, 320, 360):
        draw.rectangle((100, y, 122, y + 22), outline="black", width=2)
    for y in (150, 320, 360):
        draw.line([(103, y + 10), (110, y + 18), (124, y + 2)], fill="#182866", width=4)
    lines = [{"text": text, "box": box} for text, box in [
        ("Type of Application:", [90, 105, 290, 130]),
        ("New Application", [140, 150, 300, 173]),
        ("Renewal", [140, 190, 245, 213]),
        ("Programs:", [90, 275, 220, 300]),
        ("Art", [140, 320, 190, 343]),
        ("Music", [140, 360, 210, 383]),
    ]]
    monkeypatch.setattr(dynamic, "prepare_page", lambda page: (page, []))
    monkeypatch.setattr(dynamic, "_paddle_lines", lambda page: lines)
    monkeypatch.setattr(dynamic, "_ocr_lines", lambda page: [])
    result = extract([image], raw_schema={
        "application_type": {"type": "choice", "aliases": ["Type of Application"],
                             "allowed_values": ["New Application", "Renewal"]},
        "programs": {"type": "array", "aliases": ["Programs"],
                     "allowed_values": ["Art", "Music"]}})
    assert result["values"] == {"application_type": "New Application", "programs": ["Art", "Music"]}
    assert all(field["source"] == "omr" and not field["uncertain"] for field in result["fields"])


def test_multiline_answer_stops_at_next_label(monkeypatch):
    from PIL import Image
    import poc.dynamic_extract as dynamic
    lines = [{"text": text, "box": box} for text, box in [
        ("Full Name:", [100, 100, 180, 125]),
        ("Alex Taylor", [220, 100, 370, 130]),
        ("Address:", [100, 140, 175, 165]),
        ("12 Maple Grove", [220, 140, 370, 170]),
        ("Riverton, LA 70821", [220, 170, 430, 200]),
        ("Phone Number: 555-0198-427", [100, 205, 400, 230]),
    ]]
    monkeypatch.setattr(dynamic, "prepare_page", lambda page: (page, []))
    monkeypatch.setattr(dynamic, "_paddle_lines", lambda page: lines)
    monkeypatch.setattr(dynamic, "_ocr_lines", lambda page: [])
    result = extract([Image.new("RGB", (1000, 1400), "white")], raw_schema={
        "full_name": {"aliases": ["Full Name"]}, "address": {"aliases": ["Address"]}})
    assert result["values"] == {"full_name": "Alex Taylor", "address": "12 Maple Grove Riverton, LA 70821"}


def test_ruled_table_becomes_array_of_rows_with_ocr_overflow(monkeypatch):
    from PIL import Image, ImageDraw
    import poc.dynamic_extract as dynamic

    image = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(image)
    for y in (400, 450, 500, 550):
        draw.line((100, y, 900, y), fill="black", width=2)
    for x in (100, 300, 500, 900):
        draw.line((x, 400, x, 550), fill="black", width=2)
    # A pen stroke or weak scan can erase one divider while the other columns survive.
    draw.line((103, 450, 297, 450), fill="white", width=4)
    lines = [{"text": text, "box": box} for text, box in [
        ("Employment History", [100, 355, 370, 382]),
        ("From", [120, 410, 220, 440]), ("To", [320, 410, 400, 440]),
        ("Employer", [530, 410, 700, 440]),
        ("06/2020", [120, 460, 270, 490]), ("08/2022", [320, 460, 470, 490]),
        ("Riverton Mart", [530, 460, 725, 490]),
        ("09/2022", [120, 510, 270, 540]), ("Present", [320, 510, 475, 540]),
        ("BrightPath Co.", [530, 510, 905, 540]),
    ]]
    monkeypatch.setattr(dynamic, "prepare_page", lambda page: (page, []))
    monkeypatch.setattr(dynamic, "_paddle_lines", lambda page: lines)
    monkeypatch.setattr(dynamic, "_ocr_lines", lambda page: [])
    result = extract([image], raw_schema={"jobs": {"type": "table", "aliases": ["Employment History"],
        "columns": {"from_date": ["From"], "to_date": ["To"], "employer": ["Employer"]}}})
    assert result["values"]["jobs"] == [
        {"from_date": "06/2020", "to_date": "08/2022", "employer": "Riverton Mart"},
        {"from_date": "09/2022", "to_date": "Present", "employer": "BrightPath Co."},
    ]
    assert result["fields"][0]["source"] == "table_ocr"
    assert result["pages"][0]["tables"] == 1


def test_table_schema_requires_columns():
    with pytest.raises(ValueError, match="needs 2 to 20 columns"):
        normalize_schema({"employment": {"type": "table"}})


def test_array_of_objects_schema_maps_to_table_columns():
    schema = normalize_schema({"jobs": {"type": "array", "aliases": ["Employment History"],
        "items": {"type": "object", "properties": {
            "from_date": {"aliases": ["From (MM/YYYY)"]},
            "employer": {"aliases": ["Employer"]}}}}})
    assert schema[0]["type"] == "table"
    assert [column["key"] for column in schema[0]["columns"]] == ["from_date", "employer"]


def test_blank_inputs_do_not_take_neighboring_labels_or_choices(monkeypatch):
    from PIL import Image
    import poc.dynamic_extract as dynamic
    lines = [{"text": text, "box": box} for text, box in [
        ("Ref No:", [100, 100, 170, 120]),
        ("Date Received:", [100, 130, 230, 150]),
        ("Processed By:", [100, 160, 210, 180]),
        ("Date of Birth (DD/MM/YYYY):", [100, 205, 330, 225]),
        ("14/06/1993", [340, 205, 460, 230]),
        ("Other:", [100, 300, 165, 320]),
        ("No Preference", [100, 330, 250, 350]),
        ("Yes (please specify):", [100, 400, 260, 420]),
        ("No", [100, 430, 135, 450]),
    ]]
    monkeypatch.setattr(dynamic, "prepare_page", lambda page: (page, []))
    monkeypatch.setattr(dynamic, "_paddle_lines", lambda page: lines)
    monkeypatch.setattr(dynamic, "_ocr_lines", lambda page: [])
    result = extract([Image.new("RGB", (1000, 1400), "white")], raw_schema={
        "office_reference": {"aliases": ["Ref No"]},
        "office_date": {"aliases": ["Date Received"]},
        "office_processed": {"aliases": ["Processed By"]},
        "other_details": {"aliases": ["Other"]},
        "preferred_schedule": {"type": "choice", "allowed_values": ["No Preference"]},
        "requirements_details": {"aliases": ["Yes (please specify)"]},
        "requirements": {"type": "boolean"},
    })
    assert result["values"]["office_reference"] is None
    assert result["values"]["office_date"] is None
    assert result["values"]["office_processed"] is None
    assert result["values"]["other_details"] is None
    assert result["values"]["requirements_details"] is None
