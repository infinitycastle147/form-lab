from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import resource
import threading
import math
from contextvars import ContextVar
from datetime import date
from pathlib import Path

import psutil
from PIL import Image, ImageEnhance, ImageOps

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FORM_LAB_DATA_DIR", ROOT / "runs"))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass
TEMPLATE_DIR = ROOT / "templates"
PIPELINES = ("gemini", "tesseract", "paddle", "hybrid")
GEMINI_PRICING_URL = "https://ai.google.dev/gemini-api/docs/pricing"
active_memory_sampler = ContextVar("active_memory_sampler", default=None)
pipeline_lock = threading.Lock()
MAX_RENDERED_PIXELS = 60_000_000
MAX_PAGE_PIXELS = 20_000_000


class DependencyError(RuntimeError):
    pass


def cpu_seconds():
    """CPU time in this process and completed OCR child processes."""
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time() + children.ru_utime + children.ru_stime


class MemorySampler:
    """Sample resident memory of the API process and its OCR children during a run."""

    def __init__(self, interval=0.01):
        self.process = psutil.Process()
        self.interval = interval
        self.stop = threading.Event()
        self.child_pids = set()
        self.child_lock = threading.Lock()
        self.start_bytes = self._rss()
        self.peak_bytes = self.start_bytes
        self.thread = threading.Thread(target=self._monitor, daemon=True)

    def _rss(self):
        total = 0
        with self.child_lock:
            pids = tuple(self.child_pids)
        for pid in (self.process.pid, *pids):
            try:
                total += psutil.Process(pid).memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError):
                pass
        return total

    def add_child(self, pid):
        with self.child_lock:
            self.child_pids.add(pid)

    def remove_child(self, pid):
        with self.child_lock:
            self.child_pids.discard(pid)

    def _monitor(self):
        while not self.stop.wait(self.interval):
            self.peak_bytes = max(self.peak_bytes, self._rss())

    def __enter__(self):
        self.context_token = active_memory_sampler.set(self)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()
        self.peak_bytes = max(self.peak_bytes, self._rss())
        active_memory_sampler.reset(self.context_token)

    def metrics(self):
        divisor = 1_000_000
        return {"start_mb": round(self.start_bytes / divisor, 1),
                "peak_mb": round(self.peak_bytes / divisor, 1),
                "increase_mb": round(max(0, self.peak_bytes - self.start_bytes) / divisor, 1),
                "scope": "API process plus tracked Tesseract child processes; sampled every 10 ms"}


def pricing_rates(model):
    """Use explicit overrides or the time-limited official paid standard rate."""
    input_override = os.getenv("GEMINI_INPUT_USD_PER_MILLION") or None
    output_override = os.getenv("GEMINI_OUTPUT_USD_PER_MILLION") or None
    if input_override is not None or output_override is not None:
        if input_override is None or output_override is None:
            return None
        try:
            input_rate, output_rate = float(input_override), float(output_override)
            cached_rate = os.getenv("GEMINI_CACHED_INPUT_USD_PER_MILLION") or None
            cached_rate = float(cached_rate) if cached_rate is not None else None
        except ValueError:
            return None
        if (not all(math.isfinite(rate) and rate >= 0 for rate in (input_rate, output_rate))
                or cached_rate is not None and (not math.isfinite(cached_rate) or cached_rate < 0)):
            return None
        return {"input_per_million": input_rate, "output_per_million": output_rate,
                "cached_input_per_million": cached_rate, "source": "configured rates", "url": None}
    if model == "gemini-3.8-flash" and date.today() <= date(2026, 12, 31):
        return {"input_per_million": 0.75, "output_per_million": 3.75,
                "cached_input_per_million": 0.075,
                "source": "Google paid standard rate through 2026-12-31", "url": GEMINI_PRICING_URL}
    return None


def calculate_cost(pipeline, model, usage, elapsed_ms, compute_hourly_rate_usd=None):
    """Estimate API and optional machine cost, never presenting either as an invoice."""
    api_calls = usage["api_calls"]
    rates = pricing_rates(model) if api_calls else None
    api_estimate = 0.0 if not api_calls else None
    api_breakdown = {"input_usd": 0.0, "cached_input_usd": 0.0, "output_usd": 0.0} if not api_calls else None
    if api_calls and usage["complete"] and rates:
        cached = usage["cached_input_tokens"]
        if cached <= usage["input_tokens"] and (not cached or rates["cached_input_per_million"] is not None):
            input_tokens = usage["input_tokens"] - cached + usage["tool_input_tokens"]
            output_tokens = usage["output_tokens"] + usage["thinking_tokens"]
            api_breakdown = {"input_usd": input_tokens * rates["input_per_million"] / 1_000_000,
                             "cached_input_usd": cached * (rates["cached_input_per_million"] or 0) / 1_000_000,
                             "output_usd": output_tokens * rates["output_per_million"] / 1_000_000}
            api_estimate = sum(api_breakdown.values())
    hourly = compute_hourly_rate_usd if compute_hourly_rate_usd is not None else os.getenv("LOCAL_COMPUTE_USD_PER_HOUR")
    try:
        hourly_rate = float(hourly) if hourly is not None else None
        if hourly_rate is not None and (not math.isfinite(hourly_rate) or hourly_rate < 0):
            hourly_rate = None
    except ValueError:
        hourly_rate = None
    compute_estimate = elapsed_ms / 3_600_000 * hourly_rate if hourly_rate is not None else None
    return {"api_estimate_usd": api_estimate, "api_breakdown_usd": api_breakdown,
            "compute_estimate_usd": compute_estimate,
            "total_estimate_usd": api_estimate + compute_estimate if api_estimate is not None and compute_estimate is not None else None,
            "compute_hourly_rate_usd": hourly_rate, "rates": rates,
            "model": model if api_calls else None,
            "basis": "Paid-tier estimate; actual billing depends on account tier and discounts" if api_calls else "No external API calls"}


def templates():
    from .template_store import all_templates
    return all_templates()


def render_pdf(data: bytes, dpi=150):
    try:
        import pypdfium2 as pdfium
    except ImportError:
        if not shutil.which("pdftoppm") or not shutil.which("pdfinfo"):
            raise DependencyError("Install pypdfium2 or Poppler (pdftoppm and pdfinfo) to render PDFs")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(data)
            info = subprocess.run(["pdfinfo", str(source)], capture_output=True, text=True)
            if info.returncode:
                raise ValueError(info.stderr.strip() or "Invalid PDF")
            page_count = next((int(line.split(":", 1)[1].strip()) for line in info.stdout.splitlines() if line.startswith("Pages:")), None)
            if page_count is None or not 1 <= page_count <= 20:
                raise ValueError("PDF must contain 1–20 pages")
            result = subprocess.run(["pdftoppm", "-f", "1", "-l", "20", "-r", str(dpi), "-png", str(source), str(Path(directory) / "page")], capture_output=True, text=True)
            if result.returncode:
                raise ValueError(result.stderr.strip() or "Invalid PDF")
            pages = []
            pixels = 0
            for path in sorted(Path(directory).glob("page-*.png")):
                with Image.open(path) as image:
                    page_pixels = image.width * image.height
                    pixels += page_pixels
                    if page_pixels > MAX_PAGE_PIXELS:
                        raise ValueError("A PDF page is too large to render safely")
                    if pixels > MAX_RENDERED_PIXELS:
                        raise ValueError("PDF pages are too large to render safely")
                    pages.append(image.convert("RGB").copy())
            if not 1 <= len(pages) <= 20:
                raise ValueError("PDF must contain 1–20 pages")
            return pages
    pdf = pdfium.PdfDocument(data)
    try:
        if not 1 <= len(pdf) <= 20:
            raise ValueError("PDF must contain 1–20 pages")
        output = []
        pixels = 0
        for page in pdf:
            try:
                width, height = page.get_size()
                page_pixels = math.ceil(width * dpi / 72) * math.ceil(height * dpi / 72)
                pixels += page_pixels
                if page_pixels > MAX_PAGE_PIXELS:
                    raise ValueError("A PDF page is too large to render safely")
                if pixels > MAX_RENDERED_PIXELS:
                    raise ValueError("PDF pages are too large to render safely")
                bitmap = page.render(scale=dpi / 72)
                try:
                    output.append(bitmap.to_pil().convert("RGB").copy())
                finally:
                    bitmap.close()
            finally:
                page.close()
        return output
    finally:
        pdf.close()


def to_png(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def crop(image, box, size):
    sx, sy = image.width / size[0], image.height / size[1]
    x1, y1, x2, y2 = box
    return image.crop((round(x1*sx), round(y1*sy), round(x2*sx), round(y2*sy)))


def align(image, template, page_index=0, source_dir=None):
    from .alignment import align as register_page
    return register_page(image, template, page_index, source_dir)


def preprocess(image, settings=None):
    return ImageEnhance.Contrast(ImageOps.grayscale(image)).enhance((settings or {}).get("ocr_contrast", 1.6))


def tesseract(image, settings=None):
    if not shutil.which("tesseract"):
        raise DependencyError("Tesseract executable is unavailable")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "crop.png"
        settings = settings or {}
        scale = int(settings.get("ocr_scale", 2))
        preprocess(image, settings).resize((max(1, image.width*scale), max(1, image.height*scale))).save(path)
        with subprocess.Popen(["tesseract", str(path), "stdout", "--psm", str(int(settings.get("tesseract_psm", 7))), "tsv"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            sampler = active_memory_sampler.get()
            if sampler is not None:
                sampler.add_child(process.pid)
            try:
                stdout, stderr = process.communicate(timeout=60)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.communicate()
                raise RuntimeError("Tesseract timed out after 60 seconds") from exc
            finally:
                if sampler is not None:
                    sampler.remove_child(process.pid)
            if process.returncode:
                raise RuntimeError(stderr.strip())
    rows = [r.split("\t") for r in stdout.splitlines()[1:]]
    words, scores = [], []
    for row in rows:
        if len(row) >= 12 and row[11].strip():
            words.append(row[11].strip())
            try:
                score = float(row[10])
                if score >= 0:
                    scores.append(score / 100)
            except ValueError:
                pass
    return " ".join(words), (sum(scores)/len(scores) if scores else None)


_paddle = None
def paddle(image, settings=None):
    global _paddle
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(DATA_DIR / "paddlex_cache"))
    (DATA_DIR / "paddlex_cache").mkdir(parents=True, exist_ok=True)
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise DependencyError("Install paddlepaddle and paddleocr>=3 to run this pipeline") from exc
    if _paddle is None:
        _paddle = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False,
                            use_textline_orientation=False, enable_mkldnn=False)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "crop.png"
        preprocess(image, settings).save(path)
        result = list(_paddle.predict(str(path)))
    data = result[0].json if result else {}
    data = data.get("res", data)
    words = data.get("rec_texts", [])
    scores = data.get("rec_scores", [])
    return " ".join(words).strip(), (sum(scores)/len(scores) if scores else None)


def mark_score(image, region, size, settings=None):
    box = crop(image, region, size).convert("L")
    settings = settings or {}
    inset = max(1, round(min(box.size) * settings.get("mark_inset_ratio", 0.25)))
    if box.width <= inset*2 or box.height <= inset*2:
        return 0.0
    inside = box.crop((inset, inset, box.width-inset, box.height-inset))
    threshold = settings.get("mark_dark_threshold", 150)
    dark = sum(v for v in inside.point(lambda x: 255 if x < threshold else 0).histogram()[255:])
    return dark / max(1, inside.width * inside.height)


def local_fields(image, template, page_index, recognizer):
    fields = []
    settings = template.get("settings", {})
    page_spec = template["pages"][min(page_index, len(template["pages"])-1)]
    for spec in page_spec["fields"]:
        if spec["kind"] == "text":
            value, score = recognizer(crop(image, spec["region"], template["page_size"]), settings)
            fields.append({"name": spec["name"], "label": spec["label"], "category": spec.get("category", "printed"), "kind": "text", "value": value or None, "box": spec["region"], "confidence": score, "uncertain": not value or score is None or score < settings.get("text_confidence_threshold", 0.6), "source": recognizer.__name__})
        elif spec["kind"] == "choice":
            options = []
            borderline = False
            for item in spec["options"]:
                score = mark_score(image, item["region"], template["page_size"], settings)
                borderline |= settings.get("mark_ambiguous_ratio", 0.05) <= score < settings.get("mark_selected_ratio", 0.12)
                options.append({"value": item["value"], "box": item["region"], "ink_ratio": round(score, 4), "selected": score >= settings.get("mark_selected_ratio", 0.12)})
            selected = [o["value"] for o in options if o["selected"]]
            ambiguous = borderline or len(selected) != 1
            fields.append({"name": spec["name"], "label": spec["label"], "category": "selection", "kind": "choice", "value": selected[0] if len(selected) == 1 else None, "box": None, "options": options, "confidence": None, "uncertain": ambiguous, "source": "omr"})
        else:
            score = mark_score(image, spec["region"], template["page_size"], settings)
            checked = score >= settings.get("mark_selected_ratio", 0.12)
            fields.append({"name": spec["name"], "label": spec["label"], "category": "selection", "kind": "checkbox",
                           "value": spec.get("checked_value", "true") if checked else spec.get("unchecked_value", "false"),
                           "box": spec["region"], "ink_ratio": round(score, 4), "confidence": None,
                           "uncertain": settings.get("mark_ambiguous_ratio", 0.05) <= score < settings.get("mark_selected_ratio", 0.12),
                           "source": "omr"})
    return fields


def gemini_fields(image, template, page_index, specs=None):
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise DependencyError("Install google-genai to run Gemini") from exc
    if not os.environ.get("GEMINI_API_KEY"):
        raise DependencyError("Set GEMINI_API_KEY to run Gemini")
    specs = specs or template["pages"][min(page_index, len(template["pages"])-1)]["fields"]
    wanted = [{"name": f["name"], "label": f["label"], "kind": f["kind"],
               "options": [o["value"] for o in f.get("options", [])],
               "allowed_values": f.get("allowed_values", []), "region": f.get("region"),
               "checked_value": f.get("checked_value"), "unchecked_value": f.get("unchecked_value")}
              for f in specs]
    schema = {"type": "object", "properties": {"fields": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": ["string", "null"]}, "uncertain": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["name", "value", "uncertain", "reason"]}}}, "required": ["fields"]}
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), contents=["Read this scanned form. Extract only these fields. Use null for unreadable or conflicting selections. Mark ambiguity explicitly. Field definitions: " + json.dumps(wanted), types.Part.from_bytes(data=to_png(image), mime_type="image/png")], config={"response_mime_type": "application/json", "response_json_schema": schema})
    raw = {f["name"]: f for f in json.loads(response.text).get("fields", [])}
    fields = []
    for spec in specs:
        item = raw.get(spec["name"], {})
        value = item.get("value")
        if spec["kind"] == "choice" and value not in [o["value"] for o in spec["options"]]:
            value = None
        if spec["kind"] == "checkbox" and value not in (spec.get("checked_value", "true"), spec.get("unchecked_value", "false")):
            value = None
        fields.append({"name": spec["name"], "label": spec["label"], "category": spec.get("category", "selection"), "kind": spec["kind"], "value": value, "box": spec.get("region"), "options": [{"value": o["value"], "box": o["region"], "selected": o["value"] == value} for o in spec.get("options", [])], "confidence": None, "uncertain": bool(item.get("uncertain", True)) or value is None, "reason": item.get("reason", ""), "source": "gemini"})
    metadata = response.usage_metadata
    return fields, {
        "input_tokens": getattr(metadata, "prompt_token_count", None),
        "output_tokens": getattr(metadata, "candidates_token_count", None),
        "thinking_tokens": getattr(metadata, "thoughts_token_count", None),
        "cached_input_tokens": getattr(metadata, "cached_content_token_count", None),
        "tool_input_tokens": getattr(metadata, "tool_use_prompt_token_count", None),
        "total_tokens": getattr(metadata, "total_token_count", None),
        "model": getattr(response, "model_version", None),
    }


def _process_pages(images, template, pipeline, source_dir=None, aligned_pages=None):
    pages = []
    usage = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
             "cached_input_tokens": 0, "tool_input_tokens": 0, "total_tokens": 0,
             "fallback_fields": 0, "api_calls": 0, "complete": True,
             "total_tokens_complete": True}
    observed_model = None
    for index, original in enumerate(images):
        page_start = time.perf_counter()
        page_cpu = cpu_seconds()
        tokens = None
        aligned, alignment = aligned_pages[index] if aligned_pages is not None else align(original, template, index, source_dir)
        if pipeline == "gemini":
            fields, tokens = gemini_fields(aligned, template, index)
        else:
            if pipeline == "tesseract":
                fields = local_fields(aligned, template, index, tesseract)
            else:
                fields = local_fields(aligned, template, index, paddle)
            if pipeline == "hybrid":
                uncertain = [field for field in fields if field["uncertain"]]
                if uncertain:
                    specs = template["pages"][min(index, len(template["pages"])-1)]["fields"]
                    fallback, tokens = gemini_fields(
                        aligned, template, index,
                        [next(spec for spec in specs if spec["name"] == field["name"]) for field in uncertain],
                    )
                    by_name = {field["name"]: field for field in fallback}
                    for field in fields:
                        if field["name"] in by_name:
                            opinion = by_name[field["name"]]
                            field["fallback"] = {"original": field["value"], "gemini": opinion["value"],
                                                 "reason": opinion.get("reason", ""), "accepted": not opinion["uncertain"]}
                            if not opinion["uncertain"]:
                                field["value"] = opinion["value"]
                                field["uncertain"] = False
                                field["source"] = "gemini_fallback"
                    usage["fallback_fields"] += len(uncertain)
        if tokens is not None:
            usage["api_calls"] += 1
            if tokens["input_tokens"] is None or tokens["output_tokens"] is None:
                usage["complete"] = False
            if tokens["total_tokens"] is None:
                usage["total_tokens_complete"] = False
            for key in ("input_tokens", "output_tokens", "thinking_tokens",
                        "cached_input_tokens", "tool_input_tokens", "total_tokens"):
                usage[key] += tokens.get(key) or 0
            model = tokens.get("model") or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
            observed_model = model if observed_model in (None, model) else "mixed models"
        pages.append({"number": index+1, "fields": fields, "alignment": alignment,
                      "elapsed_ms": round((time.perf_counter()-page_start)*1000, 1),
                      "cpu_ms": round((cpu_seconds()-page_cpu)*1000, 1)})
    return pages, usage, observed_model


def run_pipeline(images, template, pipeline, compute_hourly_rate_usd=None, source_dir=None, aligned_pages=None):
    if pipeline not in PIPELINES:
        raise ValueError("Unknown pipeline")
    if len(images) != len(template["pages"]):
        raise ValueError(f"Document has {len(images)} pages; template requires {len(template['pages'])}")
    if pipeline in ("gemini", "hybrid"):
        if not os.environ.get("GEMINI_API_KEY"):
            raise DependencyError("Set GEMINI_API_KEY to run this pipeline")
        try:
            from google import genai  # noqa: F401
        except ImportError as exc:
            raise DependencyError("Install google-genai to run this pipeline") from exc
    with pipeline_lock:
        start = time.perf_counter()
        cpu_start = cpu_seconds()
        with MemorySampler() as memory_sampler:
            pages, usage, model = _process_pages(images, template, pipeline, source_dir, aligned_pages)
        elapsed_ms = round((time.perf_counter()-start)*1000, 1)
        memory = memory_sampler.metrics()
        cost = calculate_cost(pipeline, model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), usage,
                              elapsed_ms, compute_hourly_rate_usd)
        return {"pipeline": pipeline, "template": template["id"], "page_size": template["page_size"], "pages": pages,
                "elapsed_ms": elapsed_ms, "cpu_ms": round((cpu_seconds()-cpu_start)*1000, 1),
                "memory": memory, "process_peak_ram_mb": memory["peak_mb"],
                "usage": usage, "cost": cost, "cost_usd": cost["api_estimate_usd"],
                "note": "RAM is sampled process-tree RSS during this run, including OCR children; it includes memory already loaded before the run. Costs are estimates, not billed charges."}
