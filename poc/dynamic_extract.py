"""One-off form extraction. No saved template or reference page is used."""
from __future__ import annotations

import io
import importlib.util
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from PIL import Image

from .core import DependencyError
from .core import DATA_DIR, pipeline_lock
from . import core as core_module


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:64] or "field"


def normalize_schema(raw):
    """Accept a key list or a mapping of output keys to descriptions/options."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        if len(raw) != len(set(str(name) for name in raw)):
            raise ValueError("Schema field names must be unique")
        entries = [(name, {}) for name in raw]
    elif isinstance(raw, dict):
        entries = list(raw.items())
    else:
        raise ValueError("Schema must be a JSON object or a list of field names")
    if len(entries) > 100:
        raise ValueError("Schema supports at most 100 fields")
    fields = []
    for key, description in entries:
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key):
            raise ValueError("Schema field names must start with a letter and use only letters, digits, or underscores")
        if isinstance(description, str):
            description = {"description": description}
        if not isinstance(description, dict):
            raise ValueError(f"Schema definition for {key} must be an object or description string")
        kind = description.get("type", "string")
        if kind not in ("string", "date", "number", "boolean", "choice"):
            raise ValueError(f"Unsupported type for {key}")
        aliases = description.get("aliases", [])
        options = description.get("allowed_values", [])
        if not isinstance(aliases, list) or not all(isinstance(x, str) for x in aliases):
            raise ValueError(f"Aliases for {key} must be a string list")
        if not isinstance(options, list) or not all(isinstance(x, (str, int, float, bool)) for x in options):
            raise ValueError(f"Allowed values for {key} must be a scalar list")
        fields.append({"key": key, "description": str(description.get("description", ""))[:300],
                       "type": kind, "aliases": [alias[:100] for alias in aliases[:15]],
                       "allowed_values": options[:50]})
    return fields


def _order_box(points):
    import numpy as np
    pts = np.asarray(points, dtype="float32")
    sums, diffs = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[sums.argmin()], pts[diffs.argmin()], pts[sums.argmax()], pts[diffs.argmax()]], dtype="float32")


def prepare_page(image: Image.Image):
    """Rectify a photographed page and modest tilt, retaining originals when uncertain."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise DependencyError("OpenCV is required for dynamic page correction") from exc
    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    events = []
    # A visible four-sided page occupying a substantial part of a photo is safe to rectify.
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 45, 145)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, .025 * perimeter, True)
        area = cv2.contourArea(polygon)
        if len(polygon) != 4 or area < .38 * width * height or area > .985 * width * height:
            continue
        points = _order_box(polygon.reshape(4, 2))
        # A printed rectangular border on an already square-on scan is not a page edge to crop.
        xs, ys = points[:, 0], points[:, 1]
        if (abs(xs[0] - xs[3]) < width * .025 and abs(xs[1] - xs[2]) < width * .025
                and abs(ys[0] - ys[1]) < height * .025 and abs(ys[2] - ys[3]) < height * .025):
            continue
        top, right, bottom, left = (np.linalg.norm(points[(i + 1) % 4] - points[i]) for i in range(4))
        target_w, target_h = int(max(top, bottom)), int(max(left, right))
        if min(target_w, target_h) < 500 or max(target_w, target_h) / min(target_w, target_h) > 2.3:
            continue
        destination = np.float32([[0, 0], [target_w - 1, 0], [target_w - 1, target_h - 1], [0, target_h - 1]])
        matrix = cv2.getPerspectiveTransform(points, destination)
        rgb = cv2.warpPerspective(rgb, matrix, (target_w, target_h), borderValue=(255, 255, 255))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        height, width = gray.shape
        events.append("page_rectified")
        break
    # Long, near-horizontal printed rules provide a safer tilt estimate than handwriting.
    lines = cv2.HoughLinesP(cv2.Canny(gray, 60, 170), 1, math.pi / 1800, threshold=90,
                            minLineLength=max(100, width // 5), maxLineGap=22)
    angles = []
    if lines is not None:
        for x1, y1, x2, y2 in lines[:, 0]:
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if abs(angle) <= 12 and abs(x2 - x1) >= width * .2:
                angles.append(angle)
    if len(angles) >= 3:
        angle = float(np.median(angles))
        if .6 <= abs(angle) <= 12 and float(np.std(angles)) < 3.5:
            center = (width / 2, height / 2)
            matrix = cv2.getRotationMatrix2D(center, angle, 1)
            abs_cos, abs_sin = abs(matrix[0, 0]), abs(matrix[0, 1])
            new_w, new_h = int(height * abs_sin + width * abs_cos), int(height * abs_cos + width * abs_sin)
            matrix[0, 2] += new_w / 2 - center[0]
            matrix[1, 2] += new_h / 2 - center[1]
            rgb = cv2.warpAffine(rgb, matrix, (new_w, new_h), borderValue=(255, 255, 255))
            events.append("deskewed")
    corrected = Image.fromarray(rgb)
    # Only enlarge genuinely small pages; avoid wasting OCR/model work on already legible pages.
    if min(corrected.size) < 800:
        factor = min(2, 800 / min(corrected.size))
        corrected = corrected.resize((round(corrected.width * factor), round(corrected.height * factor)), Image.Resampling.LANCZOS)
        events.append("upscaled")
    return corrected, events


def _pdf_widgets(pdf_bytes):
    if not pdf_bytes:
        return []
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    except (ImportError, ValueError):
        return []
    found = []
    for page_number, page in enumerate(reader.pages, 1):
        for annotation in page.get("/Annots", []):
            widget = annotation.get_object()
            if widget.get("/Subtype") != "/Widget":
                continue
            node = widget
            names = []
            while node:
                if node.get("/T"):
                    names.insert(0, str(node["/T"]))
                parent = node.get("/Parent")
                node = parent.get_object() if parent else None
            label = str(widget.get("/TU") or ".".join(names)).strip()
            value = widget.get("/V")
            if value is None and widget.get("/Parent"):
                value = widget["/Parent"].get_object().get("/V")
            if label and value is not None:
                found.append({"key": _slug(label), "value": str(value).lstrip("/"),
                              "question": label, "page": page_number, "box": None,
                              "uncertain": False, "source": "pdf_widget"})
    return found


def _ocr_lines(image):
    if not shutil.which("tesseract"):
        raise DependencyError("Install Tesseract for dynamic extraction")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "page.png"
        image.save(path)
        result = subprocess.run(["tesseract", str(path), "stdout", "--psm", "11", "tsv"],
                                capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError("Tesseract could not read this page")
    grouped = {}
    for row in result.stdout.splitlines()[1:]:
        cells = row.split("\t", 11)
        if len(cells) != 12 or not cells[11].strip():
            continue
        try:
            left, top, width, height = map(int, cells[6:10])
            group = tuple(cells[1:5])
        except ValueError:
            continue
        grouped.setdefault(group, []).append((left, top, left + width, top + height, cells[11].strip()))
    lines = []
    for words in grouped.values():
        words.sort(key=lambda word: word[0])
        lines.append({"text": " ".join(word[4] for word in words),
                      "box": [min(w[0] for w in words), min(w[1] for w in words),
                              max(w[2] for w in words), max(w[3] for w in words)]})
    return sorted(lines, key=lambda line: (line["box"][1], line["box"][0]))


def _paddle_lines(image):
    """Read positioned text using the same local PaddleOCR engine as comparison."""
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(DATA_DIR / "paddlex_cache"))
    (DATA_DIR / "paddlex_cache").mkdir(parents=True, exist_ok=True)
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise DependencyError("Install PaddleOCR and PaddlePaddle for dynamic extraction") from exc
    max_side = 1400
    scale = min(1.0, max_side / max(image.size))
    working = image if scale == 1.0 else image.resize((max(1, round(image.width * scale)),
                                                      max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    with pipeline_lock:
        if core_module._paddle is None:
            core_module._paddle = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False,
                                            use_textline_orientation=False, enable_mkldnn=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.png"
            working.save(path)
            results = list(core_module._paddle.predict(str(path)))
    data = results[0].json if results else {}
    data = data.get("res", data)
    texts, boxes = data.get("rec_texts", []), data.get("rec_boxes", [])
    scores = data.get("rec_scores", [])
    lines = []
    for index, (text, box) in enumerate(zip(texts, boxes)):
        if not text or len(box) != 4:
            continue
        coordinates = [round(float(value) / scale) for value in box]
        if coordinates[0] >= coordinates[2] or coordinates[1] >= coordinates[3]:
            continue
        lines.append({"text": str(text).strip(), "box": coordinates,
                      "score": float(scores[index]) if index < len(scores) else None})
    return sorted(lines, key=lambda line: (line["box"][1], line["box"][0]))


def _label_answer(text, name):
    """Find a label near the start of a recognized line, preserving any inline answer."""
    label = name.strip().replace("_", " ")
    if not label:
        return None
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(label).replace(r"\ ", r"\s+") + r"(?![A-Za-z0-9])", re.I)
    match = pattern.search(text[:max(45, len(label) + 22)])
    if match and match.start() <= 22:
        return text[match.end():].lstrip(" :：?._-\t").strip()
    # Word order varies in short labels such as "Birth date" and "Date of birth".
    wanted = {word for word in re.findall(r"[a-z0-9]+", label.lower()) if word not in {"of", "the", "a", "an"}}
    words = list(re.finditer(r"[A-Za-z0-9]+", text))
    if len(wanted) >= 2 and words:
        prefix = words[:min(5, len(words))]
        hits = [word for word in prefix if word.group().lower() in wanted]
        if len({word.group().lower() for word in hits}) == len(wanted) and max(word.end() for word in hits) <= 55:
            return text[max(word.end() for word in hits):].lstrip(" :：?._-\t").strip()
    return None


def _local_candidates(image, page_number, schema=None, lines=None, source="tesseract_ocr"):
    """Conservative printed label/value pairs; uncertain handwriting needs review."""
    lines = _ocr_lines(image) if lines is None else lines
    candidates = []
    for index, line in enumerate(lines):
        text = line["text"].strip()
        match = re.match(r"^(.{2,70}?)\s*[:：]\s*(\S.{0,200})$", text)
        if match:
            question, value = match.group(1).strip(), match.group(2).strip()
            if len(question.split()) <= 12:
                candidates.append({"key": _slug(question), "value": value, "question": question,
                                   "page": page_number, "box": line["box"], "uncertain": True,
                                   "source": source})
            continue
        if not text.endswith((":", "?")) or index + 1 >= len(lines):
            continue
        following = lines[index + 1]
        gap = following["box"][1] - line["box"][3]
        if 0 <= gap <= max(40, image.height * .025) and abs(following["box"][0] - line["box"][0]) < image.width * .13:
            question = text.rstrip(":? ")
            if 1 <= len(question.split()) <= 12 and not following["text"].endswith((":", "?")):
                candidates.append({"key": _slug(question), "value": following["text"],
                                   "question": question, "page": page_number, "box": following["box"],
                                   "uncertain": True, "source": source})
    if schema:
        for spec in schema:
            names = [spec["key"].replace("_", " "), *spec["aliases"]]
            if spec["description"]:
                names.append(spec["description"])
            for name in names:
                if len(name.strip()) < 2:
                    continue
                match_line = None
                for line in lines:
                    text = line["text"].strip()
                    answer = _label_answer(text, name)
                    if answer is not None:
                        match_line = (line, answer)
                        break
                if not match_line:
                    continue
                label_line, inline = match_line
                chosen = None
                if inline and not inline.endswith((":", "?")):
                    chosen = {"text": inline, "box": label_line["box"]}
                else:
                    x1, y1, x2, y2 = label_line["box"]
                    nearby = [line for line in lines if line is not label_line and
                              ((abs((line["box"][1] + line["box"][3]) / 2 - (y1 + y2) / 2) < max(18, image.height * .015)
                                and line["box"][0] >= x2 - 5 and line["box"][0] - x2 < image.width * .42)
                               or (0 <= line["box"][1] - y2 < max(45, image.height * .035)
                                   and abs(line["box"][0] - x1) < image.width * .14))]
                    if nearby:
                        chosen = min(nearby, key=lambda line: (abs(line["box"][1] - y1), abs(line["box"][0] - x2)))
                if chosen:
                    candidates.append({"key": spec["key"], "value": chosen["text"], "question": name,
                                       "page": page_number, "box": chosen["box"], "uncertain": True,
                                       "source": source})
                    break
    return candidates


def _match(field, spec):
    variants = [spec["key"].replace("_", " "), *spec["aliases"]]
    if spec["description"]:
        variants.append(spec["description"])
    target = {_slug(item) for item in variants if item}
    actual = {_slug(str(field.get("key", ""))), _slug(str(field.get("question", "")))}
    return bool(target & actual)


def _typed_value(value, spec):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    allowed = spec["allowed_values"]
    if allowed:
        return next((option for option in allowed if str(option).casefold() == text.casefold()), None)
    if spec["type"] == "boolean":
        if text.casefold() in ("yes", "true", "checked", "selected", "on", "1"):
            return True
        if text.casefold() in ("no", "false", "unchecked", "unselected", "off", "0"):
            return False
        return None
    if spec["type"] == "number":
        cleaned = text.replace(",", "")
        try:
            number = float(cleaned)
            return int(number) if math.isfinite(number) and number.is_integer() else number if math.isfinite(number) else None
        except ValueError:
            return None
    return text


_ocr_cache = OrderedDict()
_cache_lock = threading.Lock()
_build_lock = threading.Lock()
_MAX_CACHED_DOCUMENTS = 8


def _cached_document(key):
    if key is None:
        return None
    with _cache_lock:
        result = _ocr_cache.get(key)
        if result is not None:
            _ocr_cache.move_to_end(key)
        return result


def _remember_document(key, document):
    if key is None:
        return
    with _cache_lock:
        _ocr_cache[key] = document
        _ocr_cache.move_to_end(key)
        while len(_ocr_cache) > _MAX_CACHED_DOCUMENTS:
            _ocr_cache.popitem(last=False)


def _read_document(images, pdf_bytes, schema):
    widgets = _pdf_widgets(pdf_bytes)
    if schema and all(any(_match(widget, spec) for widget in widgets) for spec in schema):
        return {"widgets": widgets, "pages": [
            {"size": image.size, "corrections": [], "paddle": [], "tesseract": []} for image in images],
            "errors": [], "widget_only": True}
    paddle_available = importlib.util.find_spec("paddleocr") is not None and importlib.util.find_spec("paddle") is not None
    tesseract_available = shutil.which("tesseract") is not None
    if not paddle_available and not tesseract_available and not widgets:
        raise DependencyError("Install PaddleOCR or Tesseract to read scanned forms")
    pages, errors = [], []
    for page_number, original in enumerate(images, 1):
        image, corrections = prepare_page(original)
        paddle_lines, tesseract_lines = [], []
        if paddle_available:
            try:
                paddle_lines = _paddle_lines(image)
            except Exception as exc:
                errors.append(f"Page {page_number}: PaddleOCR unavailable ({type(exc).__name__}); tried Tesseract")
        if tesseract_available:
            try:
                tesseract_lines = _ocr_lines(image)
            except Exception as exc:
                errors.append(f"Page {page_number}: Tesseract unavailable ({type(exc).__name__})")
        pages.append({"size": image.size, "corrections": corrections,
                      "paddle": paddle_lines, "tesseract": tesseract_lines})
    return {"widgets": widgets, "pages": pages, "errors": errors, "widget_only": False}


def extract(images, pdf_bytes=None, raw_schema=None, cache_key=None):
    started = time.perf_counter()
    schema = normalize_schema(raw_schema)
    document = _cached_document(cache_key)
    cache_hit = document is not None
    if document is None:
        with _build_lock:
            document = _cached_document(cache_key)
            cache_hit = document is not None
            if document is None:
                document = _read_document(images, pdf_bytes, schema)
                if not document["errors"] and not document["widget_only"]:
                    _remember_document(cache_key, document)
    ocr_ms = round((time.perf_counter() - started) * 1000, 1)
    candidates = list(document["widgets"])
    method_errors = list(document["errors"])
    used_paddle = False
    used_tesseract = False
    page_stats = []
    for page_number, page in enumerate(document["pages"], 1):
        image = Image.new("RGB", tuple(page["size"]))
        page_candidates = _local_candidates(image, page_number, schema, page["paddle"], "paddle_ocr") if page["paddle"] else []
        used_paddle |= bool(page["paddle"])
        candidates.extend(page_candidates)
        missing = [spec for spec in schema if not any(_match(field, spec) and field.get("value") is not None for field in candidates)]
        if page["tesseract"] and (not page_candidates or missing):
            candidates.extend(_local_candidates(image, page_number, missing or schema, page["tesseract"]))
            used_tesseract = True
        page_stats.append({"number": page_number, "corrections": page["corrections"],
                           "paddle_lines": len(page["paddle"]), "tesseract_lines": len(page["tesseract"])})
    if schema:
        values, evidence = {}, []
        for spec in schema:
            matches = [field for field in candidates if _match(field, spec)]
            matches.sort(key=lambda field: (field.get("value") is not None, field["source"] == "pdf_widget",
                                            field["source"] == "paddle_ocr"), reverse=True)
            chosen = matches[0] if matches else None
            conflict = len({str(field["value"]) for field in matches if field["value"] is not None}) > 1
            value = _typed_value(chosen.get("value"), spec) if chosen and not conflict else None
            values[spec["key"]] = value
            evidence.append({"key": spec["key"], "value": value, "question": chosen["question"] if chosen else "",
                             "page": chosen["page"] if chosen else None, "box": chosen["box"] if chosen else None,
                             "uncertain": not chosen or chosen["uncertain"] or conflict or value is None,
                             "source": chosen["source"] if chosen else "unmatched"})
    else:
        values, evidence = {}, []
        for field in candidates:
            key = _slug(field["key"])
            if key in values:
                if values[key] == field["value"]:
                    continue
                serial = 2
                while f"{key}_{serial}" in values:
                    serial += 1
                key = f"{key}_{serial}"
            values[key] = field["value"]
            evidence.append({**field, "key": key})
    return {"values": values, "fields": evidence, "pages": page_stats, "method_errors": method_errors,
            "method": "paddle_ocr" if used_paddle else "tesseract_ocr" if used_tesseract else "pdf_widgets",
            "cache_hit": cache_hit, "timing": {"ocr_ms": 0 if cache_hit else ocr_ms,
                                           "total_ms": round((time.perf_counter() - started) * 1000, 1)}}
