"""SQLite backed form templates and validation."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .core import DATA_DIR, TEMPLATE_DIR

DB_PATH = DATA_DIR / "templates.sqlite3"
DEFAULT_SETTINGS = {
    "canny_low": 50, "canny_high": 150, "min_page_area": 0.3,
    "edge_margin": 0.01, "mark_dark_threshold": 150,
    "mark_inset_ratio": 0.25, "mark_selected_ratio": 0.12,
    "mark_ambiguous_ratio": 0.05, "text_confidence_threshold": 0.6,
    "ocr_contrast": 1.6, "ocr_scale": 2, "tesseract_psm": 7,
    "detect_adaptive_block": 31, "detect_adaptive_c": 12,
    "detect_checkbox_min": 18, "detect_checkbox_max": 54,
    "detect_text_min_width": 95, "detect_row_tolerance": 22,
}


@contextmanager
def connection():
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS templates (id TEXT PRIMARY KEY, name TEXT NOT NULL, data TEXT NOT NULL, source_document_id TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            if db.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
                for path in TEMPLATE_DIR.glob("*.json"):
                    data = json.loads(path.read_text())
                    data["settings"] = DEFAULT_SETTINGS.copy()
                    db.execute("INSERT OR IGNORE INTO templates (id, name, data) VALUES (?, ?, ?)",
                               (data["id"], data["name"], json.dumps(data)))
            yield db
    finally:
        db.close()


def all_templates():
    with connection() as db:
        result = {}
        for template_id, data, source in db.execute("SELECT id, data, source_document_id FROM templates ORDER BY name COLLATE NOCASE"):
            result[template_id] = json.loads(data)
            result[template_id]["source_document_id"] = source
        return result


def get_template(template_id):
    with connection() as db:
        row = db.execute("SELECT data, source_document_id FROM templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        return None
    data = json.loads(row[0])
    data["source_document_id"] = row[1]
    return data


def validated_settings(raw):
    if not isinstance(raw, dict):
        raise ValueError("Settings must be an object")
    settings = DEFAULT_SETTINGS | raw
    limits = {"canny_low": (0, 255), "canny_high": (1, 255), "min_page_area": (0.05, 1),
              "edge_margin": (0, 0.2), "mark_dark_threshold": (1, 254),
              "mark_inset_ratio": (0, 0.45), "mark_selected_ratio": (0.001, 1),
              "mark_ambiguous_ratio": (0, 1), "text_confidence_threshold": (0, 1),
              "ocr_contrast": (0.1, 5), "ocr_scale": (1, 4), "tesseract_psm": (3, 13),
              "detect_adaptive_block": (3, 101), "detect_adaptive_c": (-30, 30),
              "detect_checkbox_min": (8, 100), "detect_checkbox_max": (10, 150),
              "detect_text_min_width": (30, 500), "detect_row_tolerance": (5, 100)}
    for key, (low, high) in limits.items():
        value = settings.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Invalid setting: {key}")
    if (settings["canny_low"] >= settings["canny_high"] or settings["mark_ambiguous_ratio"] >= settings["mark_selected_ratio"]
            or settings["detect_checkbox_min"] >= settings["detect_checkbox_max"]):
        raise ValueError("Low threshold must be below high threshold")
    for key in ("canny_low", "canny_high", "mark_dark_threshold", "ocr_scale", "tesseract_psm",
                "detect_adaptive_block", "detect_checkbox_min", "detect_checkbox_max", "detect_text_min_width", "detect_row_tolerance"):
        if int(settings[key]) != settings[key]:
            raise ValueError(f"{key} must be a whole number")
    if settings["detect_adaptive_block"] % 2 != 1:
        raise ValueError("Adaptive threshold block size must be odd")
    return settings


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("Template must be an object")
    name = str(data.get("name", "")).strip()
    identifier = str(data.get("id", "")).strip()
    if not name or len(name) > 100 or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", identifier):
        raise ValueError("Enter a name and an ID using 2–64 lowercase letters, digits or underscores")
    size = data.get("page_size")
    if (not isinstance(size, list) or len(size) != 2
            or any(type(v) is not int or v < 200 or v > 5000 for v in size)
            or size[0] * size[1] > 10_000_000):
        raise ValueError("Page width and height must be 200–5000 pixels, with at most 10 million pixels total")
    settings = validated_settings(data.get("settings", {}))
    pages = data.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 20:
        raise ValueError("Template needs 1–20 pages")
    for page_number, page in enumerate(pages, 1):
        fields = page.get("fields") if isinstance(page, dict) else None
        if not isinstance(fields, list) or len(fields) > 300:
            raise ValueError(f"Page {page_number} needs a field list of at most 300")
        names = set()
        for field in fields:
            if not isinstance(field, dict):
                raise ValueError(f"Invalid field on page {page_number}")
            key = str(field.get("name", ""))
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) or key in names:
                raise ValueError(f"Invalid or duplicate field name on page {page_number}: {key}")
            names.add(key)
            if not str(field.get("label", "")).strip() or field.get("kind") not in ("text", "choice", "checkbox"):
                raise ValueError(f"Field {key} needs a label and valid kind")
            if field["kind"] == "text":
                check_box(field.get("region"), size)
                if field.get("category", "printed") not in ("printed", "handwriting"):
                    raise ValueError(f"Invalid category for {key}")
                allowed = field.get("allowed_values", [])
                if not isinstance(allowed, list) or len(allowed) > 100 or any(not isinstance(v, str) or not v for v in allowed):
                    raise ValueError(f"Invalid allowed values for {key}")
            elif field["kind"] == "checkbox":
                check_box(field.get("region"), size)
                if not str(field.get("checked_value", "")).strip() or not str(field.get("unchecked_value", "")).strip():
                    raise ValueError(f"Checkbox {key} needs checked and unchecked values")
                if field["checked_value"] == field["unchecked_value"]:
                    raise ValueError(f"Checkbox {key} needs different checked and unchecked values")
            else:
                options = field.get("options")
                if not isinstance(options, list) or not 1 <= len(options) <= 30:
                    raise ValueError(f"Choice {key} needs 1–30 options")
                values = set()
                for option in options:
                    if not isinstance(option, dict):
                        raise ValueError(f"Invalid option in {key}")
                    value = str(option.get("value", "")).strip()
                    if not value or value in values or not str(option.get("label", "")).strip():
                        raise ValueError(f"Choice {key} has a missing or duplicate option")
                    values.add(value)
                    check_box(option.get("region"), size)
    return {"id": identifier, "name": name, "page_size": size, "pages": pages, "settings": settings}


def check_box(box, size):
    if not isinstance(box, list) or len(box) != 4 or any(not isinstance(n, (int, float)) or isinstance(n, bool) or not math.isfinite(n) for n in box):
        raise ValueError("Each region needs four finite coordinates")
    x1, y1, x2, y2 = box
    if not (0 <= x1 < x2 <= size[0] and 0 <= y1 < y2 <= size[1]):
        raise ValueError("Field region must fit inside the page")
    if x2 - x1 < 2 or y2 - y1 < 2:
        raise ValueError("Field region must be at least two pixels wide and high")


def save_template(data, source_document_id=None, update=False):
    data = validate(data)
    with connection() as db:
        exists = db.execute("SELECT 1 FROM templates WHERE id=?", (data["id"],)).fetchone()
        if update and not exists:
            raise KeyError(data["id"])
        if not update and exists:
            raise FileExistsError(data["id"])
        if update:
            db.execute("UPDATE templates SET name=?, data=?, source_document_id=COALESCE(?, source_document_id), updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (data["name"], json.dumps(data), source_document_id, data["id"]))
        else:
            db.execute("INSERT INTO templates (id, name, data, source_document_id) VALUES (?, ?, ?, ?)",
                       (data["id"], data["name"], json.dumps(data), source_document_id))
    return get_template(data["id"])
