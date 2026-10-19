FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORM_LAB_DATA_DIR=/data \
    PADDLE_PDX_CACHE_HOME=/data/paddlex_cache \
    HOME=/data \
    XDG_CACHE_HOME=/data/cache \
    MPLCONFIGDIR=/data/matplotlib

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr libglib2.0-0 libgl1 libgomp1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip install --no-cache-dir 'paddlepaddle==3.3.1' 'paddleocr==3.7.0'

COPY poc /app/poc
COPY web /app/web
COPY templates /app/templates
COPY scripts /app/scripts
COPY data/sample /app/data/sample

RUN groupadd --system formlab && useradd --system --gid formlab --home-dir /app formlab \
    && mkdir -p /data && chown -R formlab:formlab /data

USER formlab
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["uvicorn", "poc.server:app", "--host", "0.0.0.0", "--port", "8000"]
