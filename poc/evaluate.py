from __future__ import annotations

import json
from pathlib import Path
from .core import PIPELINES, render_pdf, run_pipeline, templates


def edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j-1] + (x != y)))
        previous = current
    return previous[-1]


def evaluate(result, truth):
    actual = {(p["number"], f["name"]): f for p in result["pages"] for f in p["fields"]}
    expected = {(p["number"], name): value for p in truth["pages"] for name, value in p["fields"].items()}
    field_specs = {(index, f["name"]): f for index, page in enumerate(templates()[truth["template"]]["pages"], 1) for f in page["fields"]}
    exact = 0
    misses = []
    text_stats = {"printed": [0, 0, 0, 0], "handwriting": [0, 0, 0, 0]}
    tp = fp = fn = 0
    high_conf_errors = high_conf_total = 0
    all_pages_correct = True
    for page in truth["pages"]:
        page_correct = True
        for name, value in page["fields"].items():
            field = actual.get((page["number"], name))
            prediction = field["value"] if field else None
            match = str(prediction or "").casefold().strip() == str(value or "").casefold().strip()
            exact += match
            page_correct &= match
            if not match:
                misses.append({"page": page["number"], "field": name, "expected": value, "actual": prediction})
            if field and not field.get("uncertain"):
                high_conf_total += 1
                high_conf_errors += not match
            spec = field_specs.get((page["number"], name), field or {})
            if spec.get("kind") in ("choice", "checkbox"):
                tp += bool(value is not None and prediction == value)
                fp += bool(prediction is not None and prediction != value)
                fn += bool(value is not None and prediction != value)
            elif spec.get("kind") == "text":
                category = spec.get("category", "printed")
                if category in text_stats:
                    stats = text_stats[category]
                    stats[0] += edit_distance(str(prediction or "").casefold(), str(value or "").casefold())
                    stats[1] += len(str(value or ""))
                    stats[2] += edit_distance(str(prediction or "").casefold().split(), str(value or "").casefold().split())
                    stats[3] += len(str(value or "").split())
        all_pages_correct &= page_correct
    precision = tp/(tp+fp) if tp+fp else None
    recall = tp/(tp+fn) if tp+fn else None
    fallback = [((p["number"], f["name"]), f) for p in result["pages"] for f in p["fields"] if f.get("fallback")]
    improved = sum(str(f["fallback"]["original"] or "").casefold() != str(expected.get(key) or "").casefold() and str(f["value"] or "").casefold() == str(expected.get(key) or "").casefold() for key, f in fallback)
    worsened = sum(str(f["fallback"]["original"] or "").casefold() == str(expected.get(key) or "").casefold() and str(f["value"] or "").casefold() != str(expected.get(key) or "").casefold() for key, f in fallback)
    f1 = 2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None
    return {"field_exact_match": exact/len(expected) if expected else None, "document_exact_match": float(all_pages_correct) if expected else None, "selection_precision": precision, "selection_recall": recall, "selection_f1": f1, "selection_false_positives": fp, "selection_false_negatives": fn, "missed_fields": sum(f["actual"] is None for f in misses), "cer_wer": {k: {"cer": v[0]/v[1] if v[1] else None, "wer": v[2]/v[3] if v[3] else None} for k, v in text_stats.items()}, "high_confidence_error_rate": high_conf_errors/high_conf_total if high_conf_total else None, "errors": misses, "elapsed_ms": result["elapsed_ms"], "cpu_ms": result["cpu_ms"], "memory": result.get("memory"), "process_peak_ram_mb": result["process_peak_ram_mb"], "cost": result.get("cost"), "fallback_fields": len(fallback), "fallback_improved": improved, "fallback_worsened": worsened}


def benchmark(dataset: Path, output: Path):
    records = []
    for truth_path in sorted(dataset.glob("*.json")):
        truth = json.loads(truth_path.read_text())
        pdf_path = truth_path.with_suffix(".pdf")
        if not pdf_path.exists():
            continue
        images = render_pdf(pdf_path.read_bytes())
        template = templates()[truth["template"]]
        for pipeline in PIPELINES:
            try:
                result = run_pipeline(images, template, pipeline)
                records.append({"document": pdf_path.name, "pipeline": pipeline, "status": "ok", "metrics": evaluate(result, truth), "result": result})
            except Exception as exc:
                records.append({"document": pdf_path.name, "pipeline": pipeline, "status": "unavailable", "error": str(exc)})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, indent=2))
    return records


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("data/sample"))
    parser.add_argument("--output", type=Path, default=Path("reports/benchmark.json"))
    args = parser.parse_args()
    for row in benchmark(args.dataset, args.output):
        print(row["document"], row["pipeline"], row["status"], row.get("metrics", {}).get("field_exact_match"), row.get("error", ""))
