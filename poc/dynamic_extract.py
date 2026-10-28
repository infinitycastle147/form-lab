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
from difflib import SequenceMatcher
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
        if kind not in ("string", "date", "number", "boolean", "choice", "array", "table"):
            raise ValueError(f"Unsupported type for {key}")
        aliases = description.get("aliases", [])
        options = description.get("allowed_values", [])
        if not isinstance(aliases, list) or not all(isinstance(x, str) for x in aliases):
            raise ValueError(f"Aliases for {key} must be a string list")
        if not isinstance(options, list) or not all(isinstance(x, (str, int, float, bool)) for x in options):
            raise ValueError(f"Allowed values for {key} must be a scalar list")
        columns = description.get("columns", {})
        # A JSON-Schema-style array of objects represents repeated table rows.
        items = description.get("items")
        if kind == "array" and isinstance(items, dict) and items.get("type") == "object":
            columns = items.get("properties", {})
            kind = "table"
        if kind == "table":
            if not isinstance(columns, dict) or not 2 <= len(columns) <= 20:
                raise ValueError(f"Table {key} needs 2 to 20 columns")
            parsed_columns = []
            for column_key, column in columns.items():
                if not isinstance(column_key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", column_key):
                    raise ValueError(f"Invalid column key in {key}")
                names = column.get("aliases", []) if isinstance(column, dict) else column
                if isinstance(names, str):
                    names = [names]
                if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                    raise ValueError(f"Column {column_key} in {key} needs label aliases")
                parsed_columns.append({"key": column_key, "names": [column_key.replace("_", " "),
                                                                  *[name[:100] for name in names[:10]]]})
        else:
            parsed_columns = []
        fields.append({"key": key, "description": str(description.get("description", ""))[:300],
                       "type": kind, "aliases": [alias[:100] for alias in aliases[:15]],
                       "allowed_values": options[:50], "columns": parsed_columns})
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


def _detect_marks(image):
    """Find small printed selection controls and measure ink inside their borders."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 31, 12)
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    minimum = max(11, round(image.width * .009))
    maximum = max(32, round(image.width * .034))
    boxes = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if not (minimum <= width <= maximum and minimum <= height <= maximum
                and .7 <= width / height <= 1.4 and
                cv2.contourArea(contour) >= width * height * .25):
            continue
        box = [x, y, x + width, y + height]
        # RETR_LIST reports the inside and outside of the same printed control.
        if any(abs((box[0] + box[2]) - (old["box"][0] + old["box"][2])) < 12
               and abs((box[1] + box[3]) - (old["box"][1] + old["box"][3])) < 12 for old in boxes):
            continue
        inset = max(3, round(min(width, height) * .24))
        inside = gray[y + inset:y + height - inset, x + inset:x + width - inset]
        if not inside.size:
            continue
        surround = gray[max(0, y - 3):min(gray.shape[0], y + height + 3),
                        max(0, x - 3):min(gray.shape[1], x + width + 3)]
        dark_threshold = min(150, max(80, float(np.percentile(surround, 75)) - 35))
        boxes.append({"box": box, "ink_ratio": round(float((inside < dark_threshold).mean()), 3)})
    return boxes


def _detect_tables(image):
    """Find ruled cell grids on a scan; geometry only, with no OCR or schema coupling."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 31, 12)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                 cv2.getStructuringElement(cv2.MORPH_RECT, (max(24, width // 28), 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                               cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(18, height // 70))))
    grid = cv2.morphologyEx(cv2.bitwise_or(horizontal, vertical), cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))
    contours, _ = cv2.findContours(grid, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cells = []
    for contour in contours:
        x, y, cell_width, cell_height = cv2.boundingRect(contour)
        if not (width * .055 <= cell_width <= width * .55 and
                height * .012 <= cell_height <= height * .12 and
                cv2.contourArea(contour) >= cell_width * cell_height * .68):
            continue
        box = [x, y, x + cell_width, y + cell_height]
        if any(_box_iou(box, other) > .82 for other in cells):
            continue
        cells.append(box)
    rows = []
    for cell in sorted(cells, key=lambda box: ((box[1] + box[3]) / 2, box[0])):
        center = (cell[1] + cell[3]) / 2
        row = next((row for row in rows if abs(center - row["center"]) <=
                    max(12, min(cell[3] - cell[1], row["height"]) * .42)), None)
        if row is None:
            rows.append({"center": center, "height": cell[3] - cell[1], "cells": [cell]})
        else:
            row["cells"].append(cell)
            row["center"] = sum((box[1] + box[3]) / 2 for box in row["cells"]) / len(row["cells"])
    cleaned_rows = []
    for row in rows:
        heights = sorted(box[3] - box[1] for box in row["cells"])
        median_height = heights[len(heights) // 2]
        cells_in_row = [box for box in row["cells"] if box[3] - box[1] <= median_height * 1.55]
        if len(cells_in_row) >= 2:
            cleaned_rows.append(sorted(cells_in_row, key=lambda box: box[0]))
    rows = cleaned_rows
    tables = []
    current = []
    for row in sorted(rows, key=lambda group: min(box[1] for box in group)):
        aligned = current and sum(any(abs(a[0] - b[0]) <= width * .035 and
                                      abs(a[2] - b[2]) <= width * .035 for b in current[-1])
                                  for a in row) >= max(2, min(len(row), len(current[-1])) - 1)
        adjacent = current and min(box[1] for box in row) - max(box[3] for box in current[-1]) <= height * .018
        if current and not (aligned and adjacent):
            if len(current) >= 2:
                tables.append(_table_grid(current))
            current = []
        current.append(row)
    if len(current) >= 2:
        tables.append(_table_grid(current))
    return tables


def _box_iou(first, second):
    intersection = max(0, min(first[2], second[2]) - max(first[0], second[0])) * \
                   max(0, min(first[3], second[3]) - max(first[1], second[1]))
    area_a = (first[2] - first[0]) * (first[3] - first[1])
    area_b = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / max(1, area_a + area_b - intersection)


def _table_grid(rows):
    reference = max(rows, key=len)
    tolerance = max(12, (reference[-1][2] - reference[0][0]) * .025)
    completed = []
    for row in rows:
        top, bottom = min(cell[1] for cell in row), max(cell[3] for cell in row)
        completed.append([next((cell for cell in row if abs(cell[0] - example[0]) <= tolerance and
                               abs(cell[2] - example[2]) <= tolerance),
                               [example[0], top, example[2], bottom]) for example in reference])
    rows = completed
    return {"rows": rows, "box": [min(cell[0] for row in rows for cell in row),
                                    min(cell[1] for row in rows for cell in row),
                                    max(cell[2] for row in rows for cell in row),
                                    max(cell[3] for row in rows for cell in row)]}


def _table_candidates(lines, tables, page_number, schema):
    """Map page OCR to detected cells, then map the header to requested columns."""
    candidates = []
    for spec in schema:
        if spec["type"] != "table":
            continue
        names = [spec["key"].replace("_", " "), *spec["aliases"]]
        if spec["description"]:
            names.append(spec["description"])
        ranked = []
        for table in tables:
            row_count, col_count = len(table["rows"]), len(table["rows"][0])
            if row_count < 2 or col_count != len(spec["columns"]):
                continue
            heading = next((line for line in reversed(lines) if
                            0 <= table["box"][1] - line["box"][3] <= 120 and
                            any(_name_score(line["text"], name) >= .72 for name in names)), None)
            cell_lines = [[[] for _ in row] for row in table["rows"]]
            ambiguous = False
            for line in lines:
                box = line["box"]
                if box[2] < table["box"][0] or box[0] > table["box"][2] or \
                        box[3] < table["box"][1] or box[1] > table["box"][3]:
                    continue
                # A small margin retains handwriting that extends over a printed rule.
                overlaps = []
                for row_index, row in enumerate(table["rows"]):
                    for col_index, cell in enumerate(row):
                        intersection = max(0, min(box[2], cell[2] + 6) - max(box[0], cell[0] - 6)) * \
                                       max(0, min(box[3], cell[3] + 6) - max(box[1], cell[1] - 6))
                        if intersection:
                            overlaps.append((intersection, row_index, col_index))
                if not overlaps:
                    continue
                overlaps.sort(reverse=True)
                if len(overlaps) > 1 and overlaps[1][0] >= overlaps[0][0] * .72:
                    ambiguous = True
                    continue
                _, row_index, col_index = overlaps[0]
                cell_lines[row_index][col_index].append(line)
            text_grid = [[" ".join(line["text"] for line in sorted(group,
                          key=lambda item: (item["box"][1], item["box"][0])))
                          for group in row] for row in cell_lines]
            header = text_grid[0]
            column_map, header_score = _table_column_map(header, spec["columns"])
            # A visible heading or strongly recognized headers must identify the table.
            if heading is None and header_score < .65:
                continue
            rows = []
            for row_index in range(1, row_count):
                values = {column["key"]: text_grid[row_index][column_map[index]].strip() or None
                          for index, column in enumerate(spec["columns"])}
                if any(value is not None for value in values.values()):
                    rows.append(values)
            ranked.append((int(heading is not None) + header_score, {
                "key": spec["key"], "value": rows or None,
                "question": heading["text"] if heading else " / ".join(header),
                "page": page_number, "box": table["box"], "uncertain": True,
                "source": "table_ocr", "headers": header, "cell_boxes": table["rows"]}))
        if ranked:
            candidates.append(max(ranked, key=lambda item: item[0])[1])
    return candidates


def _name_score(text, name):
    words = set(re.findall(r"[a-z0-9]+", name.lower()))
    actual = set(re.findall(r"[a-z0-9]+", text.lower()))
    if not words:
        return 0.0
    return max(len(words & actual) / len(words), SequenceMatcher(None, _slug(name), _slug(text)).ratio())


def _table_column_map(header, columns):
    scores = [[max(_name_score(text, name) for name in column["names"]) for text in header]
              for column in columns]
    remaining = set(range(len(header)))
    mapping = {}
    for column_index in sorted(range(len(columns)), key=lambda index: max(scores[index]), reverse=True):
        cell_index = max(remaining, key=lambda index: scores[column_index][index])
        mapping[column_index] = cell_index
        remaining.remove(cell_index)
    score = sum(scores[index][mapping[index]] for index in mapping) / len(columns)
    # If OCR mangles the header, its printed order remains useful but uncertain.
    if score < .65:
        return list(range(len(columns))), score
    return [mapping[index] for index in range(len(columns))], score


def _selection_candidates(lines, marks, page_number, schema):
    """Associate OCR option labels with nearby visual controls and schema headings."""
    candidates = []
    for spec in schema:
        if spec["type"] not in ("boolean", "choice", "array", "string"):
            continue
        names = [spec["key"].replace("_", " "), *spec["aliases"]]
        if spec["description"]:
            names.append(spec["description"])
        headings = [line for line in lines if any(_label_answer(line["text"], name) is not None
                                              for name in names if len(name.strip()) >= 2)]
        if not headings:
            continue
        heading = min(headings, key=lambda line: line["box"][1])
        options = spec["allowed_values"] or (["Yes", "No"] if spec["type"] == "boolean" or
                                              spec["type"] == "string" and "?" in heading["text"] else [])
        if not options:
            continue
        found = []
        for option in options:
            label = str(option)
            option_pattern = re.compile(r"(?:^|_)" + re.escape(_slug(label)) + r"(?:_|$)")
            matching = [line for line in lines if option_pattern.search(_slug(line["text"]))
                        and 0 <= line["box"][1] - heading["box"][1] <=
                        (110 if spec["type"] == "string" else 190)]
            for line in matching:
                x1, y1, _, y2 = line["box"]
                nearby = [mark for mark in marks
                          if abs((mark["box"][1] + mark["box"][3] - y1 - y2) / 2) <= 20
                          and -45 <= x1 - mark["box"][2] <= 60]
                if nearby:
                    mark = min(nearby, key=lambda item: (item["box"][0],
                               abs((item["box"][1] + item["box"][3] - y1 - y2) / 2)))
                    found.append({"value": option, "label": line["text"], "box": mark["box"],
                                  "ink_ratio": mark["ink_ratio"],
                                  "selected": mark["ink_ratio"] >= .12,
                                  "ambiguous": .05 <= mark["ink_ratio"] < .12})
                    break
        if not found or spec["type"] == "string" and len(found) < 2:
            continue
        selected = [option for option in found if option["selected"]]
        if spec["type"] == "array":
            value = [option["value"] for option in selected]
        elif spec["type"] == "boolean":
            value = selected[0]["value"] if len(selected) == 1 else None
        else:
            value = selected[0]["value"] if len(selected) == 1 else None
        candidates.append({"key": spec["key"], "value": value, "question": heading["text"],
                           "page": page_number, "box": selected[0]["box"] if selected else heading["box"],
                           "options": found, "uncertain": len(found) < len(options) or
                           any(option["ambiguous"] for option in found) or
                           (spec["type"] != "array" and len(selected) != 1), "source": "omr"})
    return candidates


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
    for index, line in enumerate(lines if not schema else []):
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
            if spec["type"] == "table":
                continue
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
                        # An exact label is safer than a short alias contained in
                        # another question (for example Date vs Date Received).
                        exact = bool(re.match(r"^\s*" + re.escape(name).replace(r"\ ", r"\s+")
                                              + r"\s*[:：?]", text, re.I))
                        rank = (0 if exact else 1, line["box"][1])
                        if match_line is None or rank < match_line[0]:
                            match_line = (rank, line, answer)
                if not match_line:
                    continue
                _, label_line, inline = match_line
                chosen = None
                if inline and not inline.endswith((":", "?")):
                    chosen = {"text": inline, "box": label_line["box"]}
                else:
                    x1, y1, x2, y2 = label_line["box"]
                    nearby = [line for line in lines if line is not label_line and
                              not _is_printed_prompt_or_option(line["text"], schema) and
                              not any(other is not label_line and other is not line and
                                      y2 < other["box"][1] <= line["box"][1] + 2 and
                                      other["box"][0] <= line["box"][0] and
                                      _is_printed_prompt_or_option(other["text"], schema)
                                      for other in lines) and
                              ((abs((line["box"][1] + line["box"][3]) / 2 - (y1 + y2) / 2) < max(18, image.height * .015)
                                and line["box"][0] >= x2 - 5 and line["box"][0] - x2 < image.width * .42)
                               or (0 <= line["box"][1] - y2 < max(45, image.height * .035)
                                   and abs(line["box"][0] - x1) < image.width * .14))]
                    if nearby:
                        chosen = min(nearby, key=lambda line: (abs(line["box"][1] - y1), abs(line["box"][0] - x2)))
                if chosen:
                    value_lines = [chosen["text"]]
                    last = chosen
                    for following in lines:
                        if following is chosen or following is label_line:
                            continue
                        gap = following["box"][1] - last["box"][3]
                        other_label = following["text"].rstrip().endswith((":", "?")) or bool(
                            re.match(r"^[^:：]{2,45}[:：]", following["text"]))
                        intervening_label = any(line is not following and line is not label_line
                            and last["box"][1] < line["box"][1] <= following["box"][1] + 2
                            and line["box"][0] <= chosen["box"][0]
                            and (line["text"].rstrip().endswith((":", "?")) or
                                 re.match(r"^[^:：]{2,45}[:：]", line["text"])) for line in lines)
                        if not other_label and not intervening_label and -5 <= gap <= max(18, image.height * .012) and \
                                abs(following["box"][0] - chosen["box"][0]) <= max(32, image.width * .035) and \
                                following["box"][0] < chosen["box"][2] and \
                                not re.match(r"^\s*\d+[.]\s+[A-Z]", following["text"]):
                            value_lines.append(following["text"])
                            last = following
                            if len(value_lines) >= 3:
                                break
                    value = " ".join(value_lines)
                    candidates.append({"key": spec["key"], "value": value, "question": name,
                                       "page": page_number, "box": [chosen["box"][0], chosen["box"][1],
                                       max(chosen["box"][2], last["box"][2]), last["box"][3]], "uncertain": True,
                                       "source": source})
                    break
    return candidates


def _is_printed_prompt_or_option(text, schema):
    """A neighboring question or choice is not an answer to an empty input."""
    stripped = text.strip()
    if not stripped or stripped.endswith((":", "?")):
        return True
    normalized = _slug(stripped)
    for spec in schema:
        options = spec["allowed_values"] or (["Yes", "No"] if spec["type"] == "boolean" else [])
        if any(normalized == _slug(str(option)) for option in options):
            return True
    return False


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
    if spec["type"] == "table":
        return value if isinstance(value, list) else None
    if spec["type"] == "array":
        if not isinstance(value, list):
            return None
        allowed = spec["allowed_values"]
        return [next((option for option in allowed if str(option).casefold() == str(item).casefold()), item)
                for item in value]
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
    if schema and not any(spec["type"] == "table" for spec in schema) and \
            all(any(_match(widget, spec) for widget in widgets) for spec in schema):
        return {"widgets": widgets, "pages": [
            {"size": image.size, "corrections": [], "paddle": [], "tesseract": [], "marks": [], "tables": []}
            for image in images],
            "errors": [], "widget_only": True}
    paddle_available = importlib.util.find_spec("paddleocr") is not None and importlib.util.find_spec("paddle") is not None
    tesseract_available = shutil.which("tesseract") is not None
    if not paddle_available and not tesseract_available and not widgets:
        raise DependencyError("Install PaddleOCR or Tesseract to read scanned forms")
    pages, errors = [], []
    for page_number, original in enumerate(images, 1):
        image, corrections = prepare_page(original)
        marks = _detect_marks(image)
        tables = _detect_tables(image)
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
                      "paddle": paddle_lines, "tesseract": tesseract_lines,
                      "marks": marks, "tables": tables})
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
        candidates.extend(_table_candidates(page["paddle"] or page["tesseract"],
                                           page.get("tables", []), page_number, schema))
        candidates.extend(_selection_candidates(page["paddle"] or page["tesseract"],
                                              page.get("marks", []), page_number, schema))
        page_candidates = _local_candidates(image, page_number, schema, page["paddle"], "paddle_ocr") if page["paddle"] else []
        used_paddle |= bool(page["paddle"])
        candidates.extend(page_candidates)
        missing = [spec for spec in schema if not any(_match(field, spec) and field.get("value") is not None for field in candidates)]
        if page["tesseract"] and (not page_candidates or missing):
            candidates.extend(_local_candidates(image, page_number, missing or schema, page["tesseract"]))
            used_tesseract = True
        page_stats.append({"number": page_number, "size": page["size"], "corrections": page["corrections"],
                           "paddle_lines": len(page["paddle"]), "tesseract_lines": len(page["tesseract"]),
                           "tables": len(page.get("tables", []))})
    if schema:
        values, evidence = {}, []
        for spec in schema:
            matches = [field for field in candidates if _match(field, spec)]
            if spec["type"] == "table":
                tables_found = sorted((field for field in matches if field["source"] == "table_ocr"),
                                      key=lambda field: field["page"])
                rows = [row for field in tables_found for row in (field["value"] or [])]
                chosen = tables_found[0] if tables_found else None
                value = rows or None
                values[spec["key"]] = value
                evidence.append({"key": spec["key"], "value": value,
                                 "question": chosen["question"] if chosen else "",
                                 "page": chosen["page"] if chosen else None,
                                 "box": chosen["box"] if chosen else None,
                                 "uncertain": True, "source": chosen["source"] if chosen else "unmatched",
                                 "options": [], "cell_boxes": chosen.get("cell_boxes", []) if chosen else []})
                continue
            if any(field["source"] == "omr" for field in matches):
                matches = [field for field in matches if field["source"] == "omr"]
            matches.sort(key=lambda field: (field.get("value") is not None, field["source"] == "pdf_widget",
                                            field["source"] == "omr", field["source"] == "paddle_ocr"), reverse=True)
            chosen = matches[0] if matches else None
            conflict = len({str(field["value"]) for field in matches if field["value"] is not None}) > 1
            value = _typed_value(chosen.get("value"), spec) if chosen and not conflict else None
            values[spec["key"]] = value
            evidence.append({"key": spec["key"], "value": value, "question": chosen["question"] if chosen else "",
                             "page": chosen["page"] if chosen else None, "box": chosen["box"] if chosen else None,
                             "uncertain": not chosen or chosen["uncertain"] or conflict or value is None,
                             "source": chosen["source"] if chosen else "unmatched",
                             "options": chosen.get("options", []) if chosen else [],
                             "cell_boxes": chosen.get("cell_boxes", []) if chosen else []})
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
