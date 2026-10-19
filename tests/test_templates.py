from pathlib import Path
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import poc.template_store as store
from poc.server import app


def test_template_previews_use_examples_and_registered_reference_pages(tmp_path, monkeypatch):
    from poc import server

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "templates.sqlite3")
    monkeypatch.setattr(server, "STORE", tmp_path)
    client = TestClient(app)

    for template_id in ("bank_account", "intake"):
        response = client.get(f"/api/templates/{template_id}/preview/1")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        with Image.open(BytesIO(response.content)) as image:
            assert image.width > 200 and image.height > 200

    source_id = "a" * 32
    source = tmp_path / source_id
    source.mkdir()
    Image.new("RGB", (400, 500), "red").save(source / "page-1.png")
    Image.new("RGB", (400, 500), "blue").save(source / "page-2.png")
    draft = {**store.get_template("intake"), "id": "custom_preview", "name": "Custom preview"}
    draft["pages"] = draft["pages"] * 2
    store.save_template(draft, source_document_id=source_id)
    response = client.get("/api/templates/custom_preview/preview/2")
    assert response.status_code == 200
    with Image.open(BytesIO(response.content)) as image:
        assert image.getpixel((0, 0)) == (0, 0, 255)
    assert client.get("/api/templates/custom_preview/preview/3").status_code == 404
    assert client.get("/api/templates/missing/preview/1").status_code == 404


def test_registry_seed_update_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "templates.sqlite3")
    baseline = store.all_templates()
    assert {"intake", "bank_account"} <= baseline.keys()
    draft = {**baseline["intake"], "id": "custom_intake", "name": "Custom intake"}
    saved = store.save_template(draft, source_document_id="example_source")
    assert store.get_template("custom_intake")["name"] == saved["name"]
    assert saved["source_document_id"] == "example_source"
    draft["pages"][0]["fields"][0]["region"] = [180, 218, 760, 275]
    store.save_template(draft, update=True)
    assert store.get_template("custom_intake")["pages"][0]["fields"][0]["region"] == [180, 218, 760, 275]
    assert store.get_template("custom_intake")["source_document_id"] == "example_source"
    draft["pages"][0]["fields"][0]["region"] = [760, 218, 180, 275]
    with pytest.raises(ValueError, match="region"):
        store.save_template(draft, update=True)
    assert store.get_template("custom_intake")["pages"][0]["fields"][0]["region"] == [180, 218, 760, 275]


def test_pdf_detection_registration_and_evaluation(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    from poc import server

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "templates.sqlite3")
    monkeypatch.setattr(server, "STORE", tmp_path)
    client = TestClient(app)
    source = Path(__file__).resolve().parents[1] / "data/sample/bank-1.pdf"
    with source.open("rb") as handle:
        uploaded = client.post("/api/documents", files={"file": ("bank-1.pdf", handle, "application/pdf")})
    assert uploaded.status_code == 200
    document_id = uploaded.json()["document_id"]
    detected = client.post("/api/templates/detect", json={"document_id": document_id})
    assert detected.status_code == 200
    draft = detected.json()["template"]
    assert any(field["kind"] == "choice" for field in draft["pages"][0]["fields"])
    draft["id"] = "detected_bank"
    draft["name"] = "Detected bank"
    draft["pages"][0]["fields"] = [next(f for f in draft["pages"][0]["fields"] if f["kind"] == "text")]
    registered = client.post("/api/templates", json={"template": draft, "source_document_id": document_id})
    assert registered.status_code == 201
    assert client.get("/api/templates/detected_bank").json()["source_document_id"] == document_id
    result = client.post("/api/run", json={"document_id": document_id, "template_id": "detected_bank", "pipelines": ["tesseract"]})
    assert result.status_code == 200
    assert result.json()["results"]["tesseract"]["status"] == "ok"


def test_second_page_acroform_fields_are_mapped(tmp_path):
    pytest.importorskip("pypdf")
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    pytest.importorskip("cv2")
    from poc.core import render_pdf
    from poc.template_detect import detect_pages

    path = tmp_path / "two-pages.pdf"
    canvas = reportlab.Canvas(str(path), pagesize=(612, 792))
    canvas.drawString(40, 740, "Cover page")
    canvas.showPage()
    canvas.drawString(40, 740, "Details")
    canvas.acroForm.textfield(name="large_notes", tooltip="Large notes", x=40, y=390,
                              width=520, height=250, borderWidth=1)
    canvas.acroForm.checkbox(name="approved", tooltip="Approved", x=40, y=340,
                             size=20, borderWidth=1)
    canvas.save()
    data = path.read_bytes()
    draft = detect_pages(render_pdf(data), pdf_data=data)
    assert len(draft["pages"]) == 2
    second = {field["name"]: field for field in draft["pages"][1]["fields"]}
    assert second["large_notes"]["kind"] == "text"
    assert second["large_notes"]["region"][3] - second["large_notes"]["region"][1] > 200
    assert second["approved"]["kind"] == "checkbox"
    store.validate(draft)


def test_independent_checkbox_values():
    from PIL import Image, ImageDraw
    from poc.core import local_fields

    image = Image.new("RGB", (200, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 50, 50), outline="black", width=2)
    draw.rectangle((70, 20, 100, 50), outline="black", width=2)
    draw.rectangle((78, 28, 92, 42), fill="black")
    template = {"page_size": [200, 200], "pages": [{"fields": [
        {"name": "first", "label": "First", "kind": "checkbox", "region": [20, 20, 51, 51],
         "checked_value": "yes", "unchecked_value": "no"},
        {"name": "second", "label": "Second", "kind": "checkbox", "region": [70, 20, 101, 51],
         "checked_value": "yes", "unchecked_value": "no"},
    ]}]}
    fields = local_fields(image, template, 0, lambda *_: (None, None))
    assert [field["value"] for field in fields] == ["no", "yes"]


def test_checkbox_output_values_must_differ():
    template = {"id": "checkbox_form", "name": "Checkbox form", "page_size": [200, 200],
                "pages": [{"fields": [{"name": "consent", "label": "Consent", "kind": "checkbox",
                                       "region": [10, 10, 30, 30], "checked_value": "yes",
                                       "unchecked_value": "yes"}]}]}
    with pytest.raises(ValueError, match="different checked and unchecked"):
        store.validate(template)


def test_subpixel_field_region_is_rejected():
    template = {"id": "tiny_form", "name": "Tiny form", "page_size": [200, 200],
                "pages": [{"fields": [{"name": "name", "label": "Name", "kind": "text",
                                        "region": [10, 10, 10.5, 20]}]}]}
    with pytest.raises(ValueError, match="at least two pixels"):
        store.validate(template)


def test_pipeline_rejects_page_count_mismatch():
    from PIL import Image
    from poc.core import run_pipeline, templates

    with pytest.raises(ValueError, match="template requires 1"):
        run_pipeline([Image.new("RGB", (850, 1100))] * 2, templates()["intake"], "tesseract")


def test_borderline_choice_uses_unrounded_ink_score(monkeypatch):
    from PIL import Image
    from poc.core import local_fields
    import poc.core as core

    scores = iter((0.12, 0.1196))
    monkeypatch.setattr(core, "mark_score", lambda *_: next(scores))
    template = {"page_size": [200, 200], "pages": [{"fields": [{
        "name": "selection", "label": "Selection", "kind": "choice", "options": [
            {"value": "one", "region": [10, 10, 20, 20]},
            {"value": "two", "region": [30, 10, 40, 20]},
        ]
    }]}]}
    field = local_fields(Image.new("RGB", (200, 200)), template, 0, lambda *_: (None, None))[0]
    assert field["value"] == "one"
    assert field["uncertain"] is True


def test_template_reference_page_count_must_match(tmp_path, monkeypatch):
    from poc import server

    monkeypatch.setattr(server, "STORE", tmp_path)
    client = TestClient(app)
    source = Path(__file__).resolve().parents[1] / "data/sample/intake-1.pdf"
    with source.open("rb") as handle:
        uploaded = client.post("/api/documents", files={"file": ("intake-1.pdf", handle, "application/pdf")})
    assert uploaded.status_code == 200
    draft = {"id": "wrong_pages", "name": "Wrong pages", "page_size": [850, 1100],
             "pages": [{"fields": []}, {"fields": []}]}
    response = client.post("/api/templates", json={"template": draft,
                                                      "source_document_id": uploaded.json()["document_id"]})
    assert response.status_code == 400
    assert "page count" in response.json()["detail"]
