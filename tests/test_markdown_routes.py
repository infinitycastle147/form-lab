from fastapi.testclient import TestClient
from pypdf import PdfWriter

from poc import markdown_routes
from poc.server import app


def test_new_pages_and_pdf_routes(monkeypatch):
    captured = []

    def fake_convert(engine, data):
        captured.append((engine, data))
        return {"markdown": "# exact\n\n", "pages": 1, "metrics": {"total_ms": 2}}

    monkeypatch.setattr(markdown_routes, "convert_pdf", fake_convert)
    client = TestClient(app)
    for engine in ("dolphin", "mineru"):
        page = client.get(f"/{engine}")
        assert page.status_code == 200
        assert '/static/docling.css?v=3' in page.text
        assert client.get('/static/docling.css').status_code == 200
        assert client.get(f'/static/{engine}.js').status_code == 200
        response = client.post(f"/api/{engine}/convert",
                               files={"file": ("sample.pdf", b"%PDF-1.7\nexample", "application/pdf")})
        assert response.status_code == 200
        assert response.json()["markdown"] == "# exact\n\n"
        assert captured[-1] == (engine, b"%PDF-1.7\nexample")
        bad = client.post(f"/api/{engine}/convert",
                          files={"file": ("bad.pdf", b"not a PDF", "application/pdf")})
        assert bad.status_code == 400


def test_cli_commands_preserve_written_markdown(monkeypatch, tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(b"%PDF-1.7")
    raw = "# Title\n\nTwo  spaces\n"

    class FakeProcess:
        pid = 99999999
        returncode = 0

        def poll(self):
            return 0

        def wait(self):
            return 0

    commands = []

    def fake_popen(command, **kwargs):
        commands.append(command)
        path = tmp_path / "markdown" / "input.md" if "demo_page.py" in str(command) else tmp_path / "result.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(raw)
        return FakeProcess()

    monkeypatch.setattr(markdown_routes.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(markdown_routes, "dolphin_paths", lambda: (tmp_path, tmp_path / "model", "python"))
    monkeypatch.setattr(markdown_routes, "mineru_command", lambda: "mineru")
    for engine in ("dolphin", "mineru"):
        result = markdown_routes._run(engine, source, tmp_path / "result.md", tmp_path)
        assert result["markdown"] == raw
    assert "--input_path" in commands[0] and "--save_dir" in commands[0]
    assert commands[1][1:3] == ["parse", str(source)]
    assert commands[1][3:5] == ["--pages", "all"]


def test_full_pdf_page_count(monkeypatch, tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    pdf = tmp_path / "two.pdf"
    with pdf.open("wb") as output:
        writer.write(output)
    monkeypatch.setattr(markdown_routes, "_run", lambda *args: {"markdown": "ok", "metrics": {}})
    assert markdown_routes.convert_pdf("mineru", pdf.read_bytes())["pages"] == 2
