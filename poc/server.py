from __future__ import annotations

import base64
import binascii
import hashlib
import io
import importlib.util
import json
import logging
import math
import os
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .alignment import AlignmentError
from .bank_form import PRESETS, TEXT, CHOICES, export as export_bank
from .core import DATA_DIR, DependencyError, MAX_PAGE_PIXELS, PIPELINES, ROOT, align, render_pdf, run_pipeline, templates, to_png
from .dynamic_extract import extract as extract_dynamic, normalize_schema, prepare_page
from .docling_route import router as docling_router
from .template_detect import detect_pages
from .template_store import get_template, save_template, validate

app = FastAPI(title="Form Lab")
app.include_router(docling_router)
logger = logging.getLogger(__name__)
STORE = DATA_DIR
STORE.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
DOCUMENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


def valid_document_id(value: str) -> bool:
    return bool(DOCUMENT_ID_PATTERN.fullmatch(value))


@app.middleware("http")
async def basic_auth(request, call_next):
    if request.url.path == "/api/health":
        return await call_next(request)
    username = os.getenv("FORM_LAB_AUTH_USER", "")
    password = os.getenv("FORM_LAB_AUTH_PASSWORD", "")
    if not username and not password:
        return await call_next(request)
    if not username or not password or ":" in username:
        return PlainTextResponse("Authentication is incompletely configured", status_code=503)
    header = request.headers.get("authorization", "")
    try:
        scheme, token = header.split(" ", 1)
        supplied_user, supplied_password = base64.b64decode(token, validate=True).split(b":", 1)
        user_matches = secrets.compare_digest(supplied_user, username.encode("utf-8"))
        password_matches = secrets.compare_digest(supplied_password, password.encode("utf-8"))
        valid = scheme.lower() == "basic" and user_matches and password_matches
    except (ValueError, binascii.Error, UnicodeEncodeError):
        valid = False
    if not valid:
        return PlainTextResponse("Authentication required", status_code=401,
                                 headers={"WWW-Authenticate": 'Basic realm="Form Lab"'})
    return await call_next(request)


class RunRequest(BaseModel):
    document_id: str
    template_id: str
    pipelines: list[str]
    compute_hourly_rate_usd: float | None = None


class GenerateRequest(BaseModel):
    values: dict[str, str]
    seed: int = 0


class DetectRequest(BaseModel):
    document_id: str
    settings: dict | None = None


class TemplateRequest(BaseModel):
    template: dict
    source_document_id: str | None = None


class DynamicRequest(BaseModel):
    document_id: str
    field_schema: dict | list = Field(alias="schema")


def check_source_document(source: str | None, template: dict):
    if source is None:
        return
    directory = STORE / source
    if not valid_document_id(source) or not source_file(directory):
        raise HTTPException(400, "Source document not found")
    pages = template.get("pages")
    if isinstance(pages, list) and len(list(directory.glob("page-*.png"))) != len(pages):
        raise HTTPException(400, "Source document page count must match the template")
    reference_template = {**template, "source_document_id": source}
    for index in range(len(pages)):
        path = directory / f"page-{index + 1}.png"
        try:
            with Image.open(path) as image:
                align(image, reference_template, index, STORE)
        except AlignmentError as exc:
            raise HTTPException(422, f"Reference page {index + 1}: {exc}") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(422, f"Reference page {index + 1} cannot be read") from exc


def source_file(directory):
    return next((path for path in (directory / "source.pdf", directory / "source.png") if path.is_file()), None)


@app.get("/")
def home():
    return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/generator")
def form_generator():
    return FileResponse(ROOT / "web" / "generator.html")


@app.get("/templates/editor")
def template_editor():
    return FileResponse(ROOT / "web" / "template.html")


@app.get("/dynamic")
def dynamic_page():
    return FileResponse(ROOT / "web" / "dynamic.html", headers={"Cache-Control": "no-store"})


@app.get("/docling")
def docling_page():
    return FileResponse(ROOT / "web" / "docling.html", headers={"Cache-Control": "no-store"})


@app.get("/style.css", include_in_schema=False)
def stylesheet():
    return FileResponse(ROOT / "web" / "style.css", media_type="text/css")


@app.get("/app.js", include_in_schema=False)
def javascript():
    return FileResponse(ROOT / "web" / "app.js", media_type="text/javascript")


@app.get("/api/templates")
def list_templates():
    return [{"id": t["id"], "name": t["name"], "fields": sum(len(p["fields"]) for p in t["pages"])} for t in templates().values()]


@app.get("/api/templates/{template_id}")
def template_detail(template_id: str):
    template = get_template(template_id)
    if template is None:
        raise HTTPException(404, "Template not found")
    return template


@app.get("/api/templates/{template_id}/preview/{page}")
def template_preview(template_id: str, page: int):
    template = get_template(template_id)
    if template is None or not 1 <= page <= len(template["pages"]):
        raise HTTPException(404, "Template page not found")

    source = template.get("source_document_id")
    if source and valid_document_id(source):
        path = STORE / source / f"page-{page}.png"
        if path.is_file():
            return FileResponse(path, media_type="image/png")

    if template_id == "bank_account" and page == 1:
        return FileResponse(ROOT / "data" / "sample" / "bank-1.png", media_type="image/png")
    if template_id == "intake" and page == 1:
        image = render_pdf((ROOT / "data" / "sample" / "intake-1.pdf").read_bytes())[0]
        return Response(to_png(image), media_type="image/png")
    raise HTTPException(404, "No reference image is available for this template")


@app.post("/api/templates/detect")
def detect_template(request: DetectRequest):
    directory = STORE / request.document_id
    if not valid_document_id(request.document_id) or not source_file(directory):
        raise HTTPException(404, "Document not found")
    images = [Image.open(p).convert("RGB") for p in sorted(directory.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))]
    try:
        pdf_path = directory / "source.pdf"
        draft = detect_pages(images, settings=request.settings,
                             pdf_data=pdf_path.read_bytes() if pdf_path.exists() else None)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"template": draft, "source_document_id": request.document_id,
            "pages": [f"/api/documents/{request.document_id}/pages/{i}" for i in range(1, len(images)+1)]}


@app.post("/api/templates", status_code=201)
def create_template(request: TemplateRequest):
    source = request.source_document_id
    try:
        template = validate(request.template)
        if source is None:
            raise HTTPException(400, "Upload a reference form before registering this template")
        check_source_document(source, template)
        return save_template(template, source)
    except FileExistsError:
        raise HTTPException(409, "Template ID already exists")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/templates/{template_id}")
def update_template(template_id: str, request: TemplateRequest):
    if request.template.get("id") != template_id:
        raise HTTPException(400, "Template ID cannot be changed")
    try:
        source = request.source_document_id
        template = validate(request.template)
        check_source_document(source, template)
        return save_template(template, source, update=True)
    except KeyError:
        raise HTTPException(404, "Template not found")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/generator/schema")
def generator_schema():
    return {"template_id": "bank_account", "text": [{"name": key, "label": label} for key, label, _ in TEXT],
            "choices": [{"name": key, "label": label.title(), "options": [{"value": value, "label": title} for value, title, _ in options]} for key, label, _, options in CHOICES],
            "presets": PRESETS}


@app.post("/api/generator")
def generate_form(request: GenerateRequest):
    try:
        png, pdf, truth = export_bank(request.values, request.seed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    document_id = uuid.uuid4().hex
    directory = STORE / document_id
    directory.mkdir()
    (directory / "source.pdf").write_bytes(pdf)
    (directory / "page-1.png").write_bytes(png)
    (directory / "truth.json").write_bytes(truth)
    base = f"/api/generator/{document_id}"
    return {"document_id": document_id, "filename": "bank-account-sample.pdf", "page_count": 1,
            "pages": [f"/api/documents/{document_id}/pages/1"],
            "downloads": {"png": base+"/png", "pdf": base+"/pdf", "json": base+"/json"}}


@app.get("/api/generator/{document_id}/{format}")
def generated_download(document_id: str, format: str):
    files = {"png": ("page-1.png", "image/png"), "pdf": ("source.pdf", "application/pdf"), "json": ("truth.json", "application/json")}
    if not valid_document_id(document_id) or format not in files:
        raise HTTPException(404)
    name, media_type = files[format]
    path = STORE / document_id / name
    if not path.exists() or not (STORE / document_id / "truth.json").exists():
        raise HTTPException(404)
    return FileResponse(path, media_type=media_type, filename=f"bank-account-sample.{format}")


@app.get("/api/status")
def status():
    return {"gemini_configured": bool(os.getenv("GEMINI_API_KEY")) and importlib.util.find_spec("google") is not None and importlib.util.find_spec("google.genai") is not None,
            "tesseract_available": shutil.which("tesseract") is not None,
            "paddle_available": importlib.util.find_spec("paddleocr") is not None and importlib.util.find_spec("paddle") is not None,
            "opencv_available": importlib.util.find_spec("cv2") is not None}


@app.get("/api/health", include_in_schema=False)
def health():
    templates()
    return {"status": "ok"}


@app.post("/api/documents")
async def upload(file: UploadFile = File(...)):
    data = await file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "Upload a PDF or image of at most 25 MB")
    is_pdf = data.startswith(b"%PDF-")
    try:
        if is_pdf:
            images = render_pdf(data)
        else:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ("JPEG", "PNG", "WEBP", "TIFF"):
                    raise ValueError("Upload a PDF, JPEG, PNG, WebP, or TIFF image")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Multi-page or animated images are not supported; upload a PDF instead")
                if image.width * image.height > 60_000_000:
                    raise ValueError("Image has too many pixels")
                if image.width * image.height > MAX_PAGE_PIXELS:
                    scale = math.sqrt(MAX_PAGE_PIXELS / (image.width * image.height))
                    image.thumbnail((max(1, math.floor(image.width * scale)),
                                     max(1, math.floor(image.height * scale))), Image.Resampling.LANCZOS)
                images = [ImageOps.exif_transpose(image).convert("RGB")]
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, "Upload a valid PDF, JPEG, PNG, WebP, or TIFF image") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to decode uploaded document")
        raise HTTPException(400, "Could not read this document. Upload a valid PDF or image.") from exc
    document_id = uuid.uuid4().hex
    directory = STORE / document_id
    directory.mkdir()
    (directory / ("source.pdf" if is_pdf else "source.png")).write_bytes(data if is_pdf else to_png(images[0]))
    for index, image in enumerate(images, 1):
        (directory / f"page-{index}.png").write_bytes(to_png(image))
    return {"document_id": document_id, "filename": Path(file.filename or "document.pdf").name, "page_count": len(images), "pages": [f"/api/documents/{document_id}/pages/{i}" for i in range(1, len(images)+1)]}


@app.get("/api/documents/{document_id}/pages/{page}")
def page_image(document_id: str, page: int):
    if not valid_document_id(document_id) or page < 1 or page > 20:
        raise HTTPException(404)
    path = STORE / document_id / f"page-{page}.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


@app.get("/api/documents/{document_id}/pages/{page}/corrected")
def corrected_page_image(document_id: str, page: int):
    if not valid_document_id(document_id) or page < 1 or page > 20:
        raise HTTPException(404)
    path = STORE / document_id / f"corrected-page-{page}.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


@app.post("/api/dynamic/extract")
def dynamic_run(request: DynamicRequest):
    directory = STORE / request.document_id
    pdf_path = directory / "source.pdf"
    if not valid_document_id(request.document_id) or not pdf_path.is_file():
        raise HTTPException(404, "Uploaded PDF not found")
    try:
        if not normalize_schema(request.field_schema):
            raise ValueError("Add at least one schema field")
        images = [Image.open(path).convert("RGB") for path in sorted(
            directory.glob("page-*.png"), key=lambda path: int(path.stem.split("-")[-1]))]
        if not images:
            raise ValueError("PDF has no rendered pages")
        pdf_bytes = pdf_path.read_bytes()
        result = extract_dynamic(images, pdf_bytes, request.field_schema,
                                 cache_key=hashlib.sha256(pdf_bytes).hexdigest())
        for index, page in enumerate(result["pages"], 1):
            if page["corrections"]:
                corrected_path = directory / f"corrected-page-{index}.png"
                if not corrected_path.is_file():
                    corrected, _ = prepare_page(images[index - 1])
                    corrected.save(corrected_path)
                page["preview"] = f"/api/documents/{request.document_id}/pages/{index}/corrected"
            else:
                page["preview"] = f"/api/documents/{request.document_id}/pages/{index}"
        return result
    except DependencyError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.exception("Dynamic extraction failed for %s", request.document_id)
        raise HTTPException(500, "Dynamic extraction failed. Check server logs.") from exc


@app.post("/api/run")
def run(request: RunRequest):
    directory = STORE / request.document_id
    if not valid_document_id(request.document_id) or not source_file(directory):
        raise HTTPException(404, "Document not found")
    template = templates().get(request.template_id)
    if not template:
        raise HTTPException(400, "Unknown template")
    if len(list(directory.glob("page-*.png"))) != len(template["pages"]):
        raise HTTPException(400, f"Document page count must match the template ({len(template['pages'])} pages)")
    if not request.pipelines or any(p not in PIPELINES for p in request.pipelines):
        raise HTTPException(400, "Select at least one valid pipeline")
    if request.compute_hourly_rate_usd is not None and (not math.isfinite(request.compute_hourly_rate_usd) or request.compute_hourly_rate_usd < 0):
        raise HTTPException(400, "Machine hourly rate must be a non-negative number")
    images = [Image.open(p).convert("RGB") for p in sorted(directory.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))]
    aligned_pages = []
    alignment_start = time.perf_counter()
    for index, image in enumerate(images):
        try:
            aligned_pages.append(align(image, template, index, STORE))
        except AlignmentError as exc:
            raise HTTPException(422, f"Page {index + 1}: {exc}") from exc
    alignment_elapsed_ms = round((time.perf_counter() - alignment_start) * 1000, 1)
    run_id = uuid.uuid4().hex
    run_dir = directory / "runs" / run_id
    run_dir.mkdir(parents=True)
    for index, (image, _) in enumerate(aligned_pages, 1):
        (run_dir / f"aligned-page-{index}.png").write_bytes(to_png(image))
    results = {}
    for pipeline in dict.fromkeys(request.pipelines):
        try:
            results[pipeline] = {"status": "ok", "data": run_pipeline(images, template, pipeline,
                                                                         request.compute_hourly_rate_usd, STORE, aligned_pages)}
        except (DependencyError, ValueError) as exc:
            results[pipeline] = {"status": "error", "error": str(exc)}
        except Exception:
            logger.exception("Pipeline %s failed for run %s", pipeline, run_id)
            results[pipeline] = {"status": "error", "error": "Pipeline failed. Check server logs."}
    (run_dir / "results.json").write_text(json.dumps(results, indent=2))
    return {"document_id": request.document_id, "run_id": run_id,
            "alignment_elapsed_ms": alignment_elapsed_ms, "results": results,
            "aligned_pages": [f"/api/documents/{request.document_id}/runs/{run_id}/pages/{i}" for i in range(1, len(images)+1)]}


@app.get("/api/documents/{document_id}/runs/{run_id}/pages/{page}")
def aligned_page_image(document_id: str, run_id: str, page: int):
    if (not valid_document_id(document_id) or not valid_document_id(run_id)
            or page < 1 or page > 20):
        raise HTTPException(404)
    path = STORE / document_id / "runs" / run_id / f"aligned-page-{page}.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")
