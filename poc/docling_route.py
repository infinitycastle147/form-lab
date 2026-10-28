"""Independent upload route and resource sampler for the Docling page."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

router = APIRouter()
log = logging.getLogger(__name__)
MAX_PDF_BYTES = 25 * 1024 * 1024
TIMEOUT_SECONDS = 900
ROOT = Path(__file__).resolve().parent.parent


def worker_python() -> str:
    configured = os.getenv("DOCLING_PYTHON")
    if configured:
        return configured
    local = ROOT / ".venv-docling" / "bin" / "python"
    return str(local) if local.is_file() else sys.executable


def worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    cache = Path(os.getenv("DOCLING_CACHE_DIR", ROOT / "runs" / "docling-cache"))
    environment["HF_HOME"] = str(cache / "huggingface")
    environment["XDG_CACHE_HOME"] = str(cache / "xdg")
    environment["TORCH_HOME"] = str(cache / "torch")
    environment["PADDLE_PDX_CACHE_HOME"] = str(cache / "paddlex")
    return environment


def convert_pdf(data: bytes, ocr: bool, tables: bool) -> dict:
    cache = Path(os.getenv("DOCLING_CACHE_DIR", ROOT / "runs" / "docling-cache"))
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="form-lab-docling-") as temporary:
        directory = Path(temporary)
        source = directory / "input.pdf"
        output = directory / "result.json"
        source.write_bytes(data)
        started = time.perf_counter()
        with (directory / "worker.log").open("wb") as log_file:
            process = subprocess.Popen(
                [worker_python(), "-m", "poc.docling_worker", str(source), str(output),
                 json.dumps({"ocr": ocr, "tables": tables})],
                stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True,
                env=worker_environment(),
            )
            worker = psutil.Process(process.pid)
            peak_rss = 0
            while process.poll() is None:
                try:
                    processes = [worker, *worker.children(recursive=True)]
                    rss = sum(item.memory_info().rss for item in processes if item.is_running())
                    peak_rss = max(peak_rss, rss)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                if time.perf_counter() - started > TIMEOUT_SECONDS:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise HTTPException(504, "Docling timed out after 15 minutes")
                time.sleep(0.05)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        if not output.is_file():
            log.error("Docling worker exited %s: %s", process.returncode,
                      (directory / "worker.log").read_text(errors="replace")[-4000:])
            raise HTTPException(503, "Docling could not start. Check the server log and installation.")
        result = json.loads(output.read_text(encoding="utf-8"))
        if not result["ok"]:
            log.error("Docling conversion failed: %s", (directory / "worker.log").read_text(errors="replace")[-4000:])
            raise HTTPException(422, result["error"])
        return {
            "markdown": result["markdown"],
            "pages": result["pages"],
            "config": {"ocr": ocr, "tables": tables, "ocr_engine": "PaddleOCR" if ocr else None},
            "metrics": {
                "total_ms": elapsed_ms,
                "conversion_ms": result["conversion_ms"],
                "cpu_seconds": result["cpu_seconds"],
                "cpu_percent_one_core": round(result["cpu_seconds"] / (elapsed_ms / 1000) * 100, 1) if elapsed_ms else 0,
                "ram_at_export_mb": result["ram_at_export_mb"],
                "ram_peak_mb": round(peak_rss / 1048576, 1),
            },
        }


@router.post("/api/docling/convert")
async def docling_convert(file: UploadFile = File(...), ocr: bool = Form(True), tables: bool = Form(True)):
    data = await file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "PDF must be 25 MB or smaller")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "Upload a valid PDF file")
    return await run_in_threadpool(convert_pdf, data, ocr, tables)


@router.get("/api/docling/status")
def docling_status():
    try:
        check = subprocess.run(
            [worker_python(), "-c", "import importlib.util; raise SystemExit(any(importlib.util.find_spec(name) is None for name in ('docling', 'paddleocr', 'paddle')))"],
            env=worker_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        return {"available": check.returncode == 0}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
