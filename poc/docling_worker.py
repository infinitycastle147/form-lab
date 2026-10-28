"""One-shot Docling PDF conversion, isolated from the Form Lab pipelines."""

from __future__ import annotations

import json
import resource
import sys
import time
import traceback
from pathlib import Path

import psutil


def main() -> None:
    source, output, config_json = sys.argv[1:4]
    config = json.loads(config_json)
    started = time.perf_counter()
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        options = PdfPipelineOptions(
            do_ocr=config["ocr"],
            do_table_structure=config["tables"],
        )
        if config["ocr"]:
            from poc.docling_paddleocr import PaddleOcrOptions, register_paddleocr

            register_paddleocr()
            options.allow_external_plugins = True
            options.ocr_options = PaddleOcrOptions(lang=[])
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        conversion_started = time.perf_counter()
        result = converter.convert(Path(source))
        if str(result.status).lower().split(".")[-1] != "success":
            raise RuntimeError(f"Docling conversion status: {result.status}")
        markdown = result.document.export_to_markdown()
        payload = {
            "ok": True,
            "markdown": markdown,
            "pages": len(result.document.pages),
            "conversion_ms": round((time.perf_counter() - conversion_started) * 1000, 1),
        }
    except Exception as exc:
        traceback.print_exc()
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    usage = resource.getrusage(resource.RUSAGE_SELF)
    payload["worker_ms"] = round((time.perf_counter() - started) * 1000, 1)
    payload["cpu_seconds"] = round(usage.ru_utime + usage.ru_stime, 3)
    payload["ram_at_export_mb"] = round(psutil.Process().memory_info().rss / 1048576, 1)
    Path(output).write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    main()
