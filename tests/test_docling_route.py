from fastapi.testclient import TestClient
from pathlib import Path
from types import ModuleType, SimpleNamespace
import json
import sys

from poc import docling_route
from poc import docling_worker
from poc.server import app


def test_docling_route_preserves_markdown_and_config(monkeypatch):
    raw = "# Heading\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
    captured = {}

    def fake_convert(data, ocr, tables):
        captured.update(data=data, ocr=ocr, tables=tables)
        return {"markdown": raw, "pages": 1, "metrics": {"total_ms": 123}}

    monkeypatch.setattr(docling_route, "convert_pdf", fake_convert)
    response = TestClient(app).post(
        "/api/docling/convert",
        files={"file": ("example.pdf", b"%PDF-1.7\nexample", "application/pdf")},
        data={"ocr": "false", "tables": "true"},
    )
    assert response.status_code == 200
    assert response.json()["markdown"] == raw
    assert captured == {"data": b"%PDF-1.7\nexample", "ocr": False, "tables": True}


def test_docling_route_rejects_non_pdf():
    response = TestClient(app).post(
        "/api/docling/convert",
        files={"file": ("fake.pdf", b"not a PDF", "application/pdf")},
    )
    assert response.status_code == 400


def test_worker_does_not_inherit_app_python_packages(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", ".deps")
    environment = docling_route.worker_environment()
    assert environment["PYTHONPATH"] == str(docling_route.ROOT)
    assert environment["HF_HOME"].endswith("runs/docling-cache/huggingface")
    assert environment["PADDLE_PDX_CACHE_HOME"].endswith("runs/docling-cache/paddlex")


def test_worker_uses_docling_export_without_modifying_it(monkeypatch, tmp_path):
    raw = "# Exact\n\nText  with  spaces\n"
    base = ModuleType("docling.datamodel.base_models")
    base.InputFormat = SimpleNamespace(PDF="pdf")
    options = ModuleType("docling.datamodel.pipeline_options")
    options.PdfPipelineOptions = lambda **kwargs: SimpleNamespace(**kwargs)
    paddle_adapter = ModuleType("poc.docling_paddleocr")
    paddle_adapter.PaddleOcrOptions = lambda **kwargs: SimpleNamespace(**kwargs)
    paddle_adapter.register_paddleocr = lambda: None
    converter_module = ModuleType("docling.document_converter")
    converter_module.PdfFormatOption = lambda **kwargs: kwargs
    converter_module.DocumentConverter = lambda **kwargs: SimpleNamespace(
        convert=lambda source: SimpleNamespace(
            status="success", document=SimpleNamespace(
                pages=[1], export_to_markdown=lambda: raw)))
    for name, module in {"docling": ModuleType("docling"),
                         "docling.datamodel": ModuleType("docling.datamodel"),
                         "docling.datamodel.base_models": base,
                         "docling.datamodel.pipeline_options": options,
                         "docling.document_converter": converter_module,
                         "poc.docling_paddleocr": paddle_adapter}.items():
        monkeypatch.setitem(sys.modules, name, module)
    output = tmp_path / "output.json"
    monkeypatch.setattr(sys, "argv", ["worker", str(tmp_path / "input.pdf"), str(output),
                                      json.dumps({"ocr": True, "tables": True})])
    docling_worker.main()
    assert json.loads(output.read_text())["markdown"] == raw
