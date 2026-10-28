"""Isolated PDF-to-Markdown routes for Dolphin and MinerU."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil
from fastapi import APIRouter, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from .docling_route import MAX_PDF_BYTES, ROOT

router = APIRouter()
log = logging.getLogger(__name__)
TIMEOUT_SECONDS = 900


def worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.setdefault("MINERU_HOME", str(ROOT / "runs" / "mineru"))
    environment.setdefault("HF_HOME", str(ROOT / "runs" / "huggingface"))
    return environment


def dolphin_paths() -> tuple[Path, Path, str]:
    repo = Path(os.getenv("DOLPHIN_REPO", ROOT / "Dolphin")).expanduser().resolve()
    model = Path(os.getenv("DOLPHIN_MODEL_PATH", repo / "hf_model")).expanduser().resolve()
    local_python = ROOT / ".venv-dolphin" / "bin" / "python"
    python = os.getenv("DOLPHIN_PYTHON", str(local_python if local_python.is_file() else sys.executable))
    return repo, model, python


def mineru_command() -> str:
    local = ROOT / ".venv-mineru" / "bin" / "mineru"
    return os.getenv("MINERU_COMMAND", str(local if local.is_file() else shutil.which("mineru") or "mineru"))


def available(engine: str) -> bool:
    if engine == "dolphin":
        repo, model, python = dolphin_paths()
        if not (repo / "demo_page.py").is_file() or not model.exists():
            return False
        command = [python, "-c", "import torch, transformers, pymupdf"]
    else:
        command = [mineru_command(), "--help"]
    try:
        return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=10, check=False, env=worker_environment()).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run(engine: str, source: Path, markdown_file: Path, directory: Path) -> dict:
    if engine == "dolphin":
        repo, model, python = dolphin_paths()
        command = [python, str(repo / "demo_page.py"), "--model_path", str(model),
                   "--input_path", str(source), "--save_dir", str(directory)]
        output_file = directory / "markdown" / "input.md"
        cwd = repo
    else:
        command = [mineru_command(), "parse", str(source), "--pages", "all", "-o", str(markdown_file)]
        output_file = markdown_file
        cwd = directory

    started = time.perf_counter()
    peak_rss = last_rss = cpu_seconds = 0.0
    with (directory / "worker.log").open("wb") as log_file:
        try:
            process = subprocess.Popen(command, cwd=cwd, stdout=log_file,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       env=worker_environment())
        except OSError as exc:
            raise HTTPException(503, f"{engine.title()} is unavailable. Check its installation.") from exc
        try:
            worker = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            worker = None
        while process.poll() is None:
            try:
                processes = [worker, *worker.children(recursive=True)] if worker else []
                live = [item for item in processes if item.is_running()]
                last_rss = sum(item.memory_info().rss for item in live)
                cpu_seconds = max(cpu_seconds, sum(item.cpu_times().user + item.cpu_times().system for item in live))
                peak_rss = max(peak_rss, last_rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            if time.perf_counter() - started > TIMEOUT_SECONDS:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise HTTPException(504, f"{engine.title()} timed out after 15 minutes")
            time.sleep(0.05)
        process.wait()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if process.returncode or not output_file.is_file():
        detail = (directory / "worker.log").read_text(errors="replace")[-4000:]
        log.error("%s conversion failed (%s): %s", engine, process.returncode, detail)
        raise HTTPException(422, f"{engine.title()} conversion failed. Check the server log.")
    return {
        "markdown": output_file.read_text(encoding="utf-8"),
        "metrics": {
            "total_ms": elapsed_ms,
            "conversion_ms": elapsed_ms,
            "cpu_seconds": round(cpu_seconds, 3),
            "cpu_percent_one_core": round(cpu_seconds / (elapsed_ms / 1000) * 100, 1) if elapsed_ms else 0,
            "ram_at_export_mb": round(last_rss / 1048576, 1),
            "ram_peak_mb": round(peak_rss / 1048576, 1),
        },
    }


def convert_pdf(engine: str, data: bytes) -> dict:
    from pypdf import PdfReader

    with tempfile.TemporaryDirectory(prefix=f"form-lab-{engine}-") as temporary:
        directory = Path(temporary)
        source = directory / "input.pdf"
        source.write_bytes(data)
        try:
            pages = len(PdfReader(source).pages)
        except Exception as exc:
            raise HTTPException(400, "Upload a valid PDF file") from exc
        result = _run(engine, source, directory / "result.md", directory)
        return {**result, "pages": pages}


async def _convert(engine: str, file: UploadFile) -> dict:
    data = await file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "PDF must be 25 MB or smaller")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "Upload a valid PDF file")
    return await run_in_threadpool(convert_pdf, engine, data)


@router.post("/api/dolphin/convert")
async def dolphin_convert(file: UploadFile = File(...)):
    return await _convert("dolphin", file)


@router.post("/api/mineru/convert")
async def mineru_convert(file: UploadFile = File(...)):
    return await _convert("mineru", file)


@router.get("/api/dolphin/status")
def dolphin_status():
    return {"available": available("dolphin")}


@router.get("/api/mineru/status")
def mineru_status():
    return {"available": available("mineru")}
