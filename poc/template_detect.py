"""Suggest editable field regions from an unfilled form PDF."""
from __future__ import annotations

import re
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from .template_store import validated_settings


def _near(a, b, gap=8):
    return all(abs(x-y) <= gap for x, y in zip(a, b))


def _words(image):
    if not shutil.which("tesseract"):
        return []
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "page.png"
        image.save(path)
        try:
            result = subprocess.run(["tesseract", str(path), "stdout", "--psm", "11", "tsv"],
                                    capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return []
    if result.returncode:
        return []
    words = []
    for line in result.stdout.splitlines()[1:]:
        cells = line.split("\t", 11)
        if len(cells) == 12 and cells[11].strip():
            try:
                words.append((int(cells[6]), int(cells[7]), int(cells[8]), int(cells[9]), cells[11].strip()))
            except ValueError:
                pass
    return words


def _label(box, words, fallback):
    x1, y1, _, y2 = box
    nearby = [w for w in words if w[0]+w[2] <= x1+12 and 0 <= x1-(w[0]+w[2]) < 230
              and abs((w[1]+w[3]/2)-(y1+y2)/2) < 24]
    if not nearby:
        nearby = [w for w in words if 0 <= y1-(w[1]+w[3]) < 40 and abs(w[0]-x1) < 85]
    nearby = sorted(nearby, key=lambda w: w[0])[-4:]
    text = " ".join(w[4] for w in nearby).strip(" :._-")
    return text[:70] or fallback


def _slug(label, fallback):
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:45]
    return slug if slug and slug[0].isalpha() else fallback


def _widget_fields(reader, page_index, size):
    """Use exact AcroForm rectangles when the PDF exposes widgets."""
    if reader is None:
        return []
    if page_index >= len(reader.pages):
        return []
    page = reader.pages[page_index]
    crop = page.cropbox
    left, bottom, right, top = map(float, crop)
    page_width, page_height = right-left, top-bottom
    if page_width <= 0 or page_height <= 0:
        return []
    rotation = page.rotation % 360

    def coordinates(x, y):
        if rotation == 90:
            return (top-y)/page_height, (right-x)/page_width
        if rotation == 180:
            return (right-x)/page_width, (y-bottom)/page_height
        if rotation == 270:
            return (y-bottom)/page_height, (x-left)/page_width
        return (x-left)/page_width, (top-y)/page_height

    fields, used = [], set()
    for annotation in page.get("/Annots", []):
        widget = annotation.get_object()
        if widget.get("/Subtype") != "/Widget" or int(widget.get("/F", 0)) & 3:
            continue
        rect = widget.get("/Rect")
        if not rect or len(rect) != 4:
            continue
        points = [coordinates(x, y) for x in (float(rect[0]), float(rect[2])) for y in (float(rect[1]), float(rect[3]))]
        box = [round(max(0, min(1, min(p[0] for p in points)))*size[0]),
               round(max(0, min(1, min(p[1] for p in points)))*size[1]),
               round(max(0, min(1, max(p[0] for p in points)))*size[0]),
               round(max(0, min(1, max(p[1] for p in points)))*size[1])]
        if box[2]-box[0] < 2 or box[3]-box[1] < 2:
            continue
        node, parts = widget, []
        while node:
            if node.get("/T"):
                parts.insert(0, str(node["/T"]))
            parent = node.get("/Parent")
            node = parent.get_object() if parent else None
        raw_name = ".".join(parts)
        label = str(widget.get("/TU") or raw_name or f"Field {len(fields)+1}")
        label = re.sub(r"_[A-Za-z0-9*~\-]{12,}.*$", "", label).replace("_edit;", "").strip(" :_")
        label = label[:100] or f"Field {len(fields)+1}"
        base = _slug(label, f"field_{len(fields)+1}")
        key, serial = base, 2
        while key in used:
            key = f"{base}_{serial}"; serial += 1
        used.add(key)
        field_type = widget.get("/FT")
        parent = widget.get("/Parent")
        parent = parent.get_object() if parent else {}
        field_type = field_type or parent.get("/FT")
        flags = int(widget.get("/Ff", parent.get("/Ff", 0)))
        if field_type == "/Btn" and flags & 65536:
            continue  # push button, not an input value
        if field_type == "/Btn":
            field = {"name": key, "label": label, "kind": "checkbox", "region": box,
                     "checked_value": "true", "unchecked_value": "false"}
        else:
            field = {"name": key, "label": label, "kind": "text", "category": "printed", "region": box}
            if field_type == "/Ch":
                options = widget.get("/Opt", parent.get("/Opt", []))
                field["allowed_values"] = [str(option[-1] if isinstance(option, (list, tuple)) else option)
                                           for option in options]
        field["origin"] = "pdf_widget"
        fields.append(field)
    return fields


def detect_pages(images, name="New form", identifier="new_form", settings=None, pdf_data=None):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for automatic template detection") from exc
    settings = validated_settings(settings or {})
    width, height = 850, 1100
    reader = None
    if pdf_data:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf_data), strict=False)
        except (ImportError, ValueError):
            pass
    pages = []
    for page_index, image in enumerate(images):
        native = _widget_fields(reader, page_index, (width, height))
        if native:
            pages.append({"fields": native})
            continue
        scaled = image.convert("RGB").resize((width, height))
        gray = cv2.cvtColor(np.array(scaled), cv2.COLOR_RGB2GRAY)
        binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY_INV, int(settings["detect_adaptive_block"]), settings["detect_adaptive_c"])
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes, lines = [], []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if x < 8 or y < 8 or x+w > width-8 or y+h > height-8:
                continue
            aspect = w/max(h, 1)
            if settings["detect_checkbox_min"] <= w <= settings["detect_checkbox_max"] and settings["detect_checkbox_min"] <= h <= settings["detect_checkbox_max"] and .72 <= aspect <= 1.38:
                area = cv2.contourArea(contour)
                if area >= w*h*.45:
                    box = [x, y, x+w, y+h]
                    if not any(_near(box, old, 7) for old in boxes):
                        boxes.append(box)
            elif w >= settings["detect_text_min_width"] and 1 <= h <= 95 and aspect >= 3.2:
                box = [x, max(0, y-23) if h < 10 else y, x+w, min(height, y+h+5)]
                if not any(_near(box, old, 12) for old in lines):
                    lines.append(box)
        boxes.sort(key=lambda b: (b[1], b[0]))
        groups = []
        for box in boxes:
            group = next((g for g in groups if abs((g[0][1]+g[0][3])/2-(box[1]+box[3])/2) < settings["detect_row_tolerance"]), None)
            if group is None:
                groups.append([box])
            else:
                group.append(box)
        words = _words(scaled)
        fields = []
        used = set()
        for index, group in enumerate(groups, 1):
            group.sort(key=lambda b: b[0])
            label = _label(group[0], words, f"Choice group {index}")
            base = _slug(label, f"choice_{index}")
            key = base
            serial = 2
            while key in used:
                key = f"{base}_{serial}"; serial += 1
            used.add(key)
            fields.append({"name": key, "label": label, "kind": "choice", "origin": "opencv", "options": [
                {"value": f"option_{n}", "label": f"Option {n}", "region": box}
                for n, box in enumerate(group, 1)]})
        lines.sort(key=lambda b: (b[1], b[0]))
        lines = [box for box in lines if not any(
            other is not box and max(0, min(box[2], other[2])-max(box[0], other[0]))
            * max(0, min(box[3], other[3])-max(box[1], other[1]))
            > .45 * (box[2]-box[0]) * (box[3]-box[1]) and
            (other[2]-other[0])*(other[3]-other[1]) < (box[2]-box[0])*(box[3]-box[1])
            for other in lines)]
        for index, box in enumerate(lines, 1):
            if any(abs(box[1]-check[1]) < 12 and box[0] <= check[2]+12 <= box[2] for check in boxes):
                continue
            label = _label(box, words, f"Text field {index}")
            base = _slug(label, f"text_{index}")
            key = base
            serial = 2
            while key in used:
                key = f"{base}_{serial}"; serial += 1
            used.add(key)
            fields.append({"name": key, "label": label, "kind": "text", "category": "handwriting", "origin": "opencv", "region": box})
        fields.sort(key=lambda f: (f.get("region") or f["options"][0]["region"])[1])
        pages.append({"fields": fields})
    return {"id": identifier, "name": name, "page_size": [width, height], "settings": settings, "pages": pages}
