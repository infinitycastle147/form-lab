# Form Lab: scanned form extraction comparison

This POC runs four independent extraction routes against a configured fixed form template. Every route returns the same field schema. The original three intake PDFs are synthetic, typed examples. Two additional one-page bank application PDFs use a handwriting-style font and dense checkbox layout. They exercise the software; they do **not** establish real handwriting performance or production accuracy.

## One-command container deployment

Install Docker with Compose, then run from the project directory:

```sh
docker compose up --build -d
```

Open http://127.0.0.1:8000. Check readiness with `docker compose ps` or `curl http://127.0.0.1:8000/api/health`. The first build installs the CPU Paddle runtime and can take several minutes. The image uses Linux amd64 so it also runs on Apple Silicon through emulation. The Paddle model may download on its first use, so that route needs outbound network access at least once. Tesseract, PDF rendering, OpenCV detection, and the form generator are available immediately. To enable Gemini, copy `.env.example` to `.env` and set `GEMINI_API_KEY`, then run `docker compose up -d` again. Keep that file private. `FORM_LAB_PORT` changes the host port.

The service binds to loopback on the host. For access control, set both `FORM_LAB_AUTH_USER` and `FORM_LAB_AUTH_PASSWORD` in `.env`; the browser will prompt for them. Use HTTPS through a reverse proxy for remote access because HTTP Basic credentials must be protected in transit. `/api/health` remains accessible to the container health check. Documents, generated forms, the template registry, and the Paddle cache are stored in the `form_lab_data` Docker volume. `docker compose down` keeps the data; `docker compose down -v` removes it. Define a retention and backup policy before using real personal data. To run the bundled benchmark in the image:

```sh
docker compose run --rm app python -m poc.evaluate --dataset data/sample --output /data/benchmark.json
```

The benchmark output remains in the data volume. For a local non-container run, the default storage path is `runs/`; set `FORM_LAB_DATA_DIR` to override it. To regenerate the three synthetic intake PDFs in the container, run `docker compose run --rm app python scripts/make_sample.py`; they are written to `/data/sample`.

## Run

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Install the Tesseract executable separately (for example, brew install tesseract).
# For Paddle routes, also install platform-compatible paddlepaddle and paddleocr>=3,<4.
python scripts/make_sample.py
uvicorn poc.server:app --reload
```

Open http://127.0.0.1:8000. Do not open `web/index.html` as a `file://` page: the upload and comparison need the Python API. Upload a PDF (up to 20 pages) or a single JPEG, PNG, WebP, or TIFF image; the upload limit is 25 MB. Animated or multi-page image files are rejected; use PDF for multiple pages. Images over 20 million pixels are reduced to that limit before alignment, and images over 60 million pixels are rejected. Select the matching template and run one or all routes. Gemini routes also require `GEMINI_API_KEY` in the shell or in a workspace `.env` file; the `.env` file is ignored by Git. `GEMINI_MODEL` defaults to `gemini-3.8-flash`.

The results summary shows shared alignment time separately. The performance panel shows extraction-method elapsed time, CPU time including completed Tesseract children, and resident RAM sampled every 10 ms during each method. Method timings and machine cost estimates exclude the shared alignment step. RAM includes the API process and tracked Tesseract child processes. Start and peak are actual sampled process memory, not a lifetime high-water mark; the increase is their difference. Shared server memory is included, so simultaneous requests can affect the reading. A brief spike between samples may be missed.

API cost is an **estimate**, not an invoice. Gemini input, response, thinking, and cached token counts come from the SDK response. For `gemini-3.8-flash`, the app uses [Google's paid standard rates](https://ai.google.dev/gemini-api/docs/pricing) of $0.75 per million input tokens, $3.75 per million output tokens (including thinking), and $0.075 per million cached input tokens through December 31, 2026. Other models and dates require `GEMINI_INPUT_USD_PER_MILLION` and `GEMINI_OUTPUT_USD_PER_MILLION` for an estimate; set `GEMINI_CACHED_INPUT_USD_PER_MILLION` too if cached tokens are used. Tesseract and PaddleOCR incur $0 in external API charges. Their machine cost depends on your hardware or hosting bill; enter a **Machine rate** in the run form or set `LOCAL_COMPUTE_USD_PER_HOUR` as a default to show an elapsed-time estimate for every route. The UI keeps API and machine estimates separate. Free tier, discounts, taxes, and actual invoices are not available to this app.

## Generate a one-page bank form

Open **Form generator** from the top navigation or visit `/generator`. Pick one of three synthetic applicants, edit any text field or checkbox choice, and click **Generate one-page form**. The preview and downloads provide a 1700 × 2200 PNG, a one-page PDF, and a ground-truth JSON file for the same 23-field form. Click **Use this form in comparison** to open the comparison page with the generated PDF loaded and the `bank_account` template selected. Generated files are stored under `runs/<document_id>/`.

Two ready-made examples are in `data/sample/bank-1.*` and `data/sample/bank-2.*`. Both use the same [bank template](templates/bank_account.json). Their blue entries imitate handwriting using a font; they are not pen-written samples. The PDF version is provided because the comparison API accepts PDFs. Each image/PDF has one page.

To run the labeled benchmark:

```sh
python3 -m poc.evaluate --dataset data/sample --output reports/benchmark.json
```

The program records unavailable routes as such, preserving comparable document rows. It reports field exact match, whole-document exact match, printed and handwriting CER/WER, selection precision/recall/F1, missed fields, high-confidence error rate, latency, CPU time, sampled RAM, Gemini token counts, estimated costs, and fallback corrections/regressions. The UI displays the uploaded page, field comparison, uncertainty, template boxes, fallback decisions in JSON, timings, and dependency errors. `/docs` provides the API schema.

## Register and edit form templates

Open **Template studio** from the top navigation or visit `/templates/editor`. Upload a flat, complete blank PDF or image as the reference form; detection starts automatically. Use the page selector to inspect every page, remove false detections, add missed fields, drag or resize boxes, and set stable output names, display labels, text categories, checkbox values, and dropdown values. **Re-detect all pages** reruns detection after changing settings. Interactive PDFs supply exact field rectangles through their AcroForm widgets; scanned pages use OpenCV candidates. The meaning of a field cannot always be inferred reliably, so review is part of registration. Click **Register template**; it appears immediately in the comparison form. Open any registered template there to revise it later. Keep the reference page and mapped boxes in the same orientation.

Templates live in `runs/templates.sqlite3`. On first use the app imports the bundled `templates/*.json` examples into SQLite; after that, SQLite is the source of truth. Back up this database and the reference PDFs in `runs/` if you need to move a registry to another installation. The API exposes `GET /api/templates`, `GET /api/templates/{id}`, `POST /api/templates/detect`, `POST /api/templates`, and `PUT /api/templates/{id}`. Detection drafts are never used for evaluation until saved. OpenCV is required for visual detection; pypdf supplies [PDF widget rectangles](https://pypdf.readthedocs.io/en/stable/user/forms.html); Tesseract improves proposed labels when installed. Visual detection uses [OpenCV adaptive thresholding and morphology](https://docs.opencv.org/4.13.0/dd/dd7/tutorial_morph_lines_detection.html) and [contour extraction](https://docs.opencv.org/4.13.0/d4/d73/tutorial_py_contours_begin.html); the API uses [FastAPI request bodies](https://fastapi.tiangolo.com/tutorial/body/).

## Template and output contract

The bundled `templates/intake.json` maps field names to canonical 850 × 1100 coordinates and serves as seed data. Choose a registered template in the UI; automatic template identification is not provided. Use **Preview** beside the template selector to inspect its reference page (or a sample completed page for bundled templates). Text fields have `kind: text`, `region`, and `category`; PDF dropdowns also retain `allowed_values`. Choice fields have `kind: choice` and option regions. Independent checkbox fields use `kind: checkbox`, a region, and checked/unchecked output values. Each output field includes `value`, `box` or option boxes, `uncertain`, `source`, and `confidence` where the OCR engine supplies a score. Tesseract's recognition score and Paddle's score are **not calibrated probabilities**. Gemini never receives an invented numerical confidence. The local OMR uses interior ink ratio and flags borderline marks.

OpenCV matches each uploaded page to the template's saved reference using ORB features and a RANSAC homography. It can correct translation, rotation, scale, perspective, surrounding background, and modest whitespace cropping. If direct matching fails, it tries page-outline rectification and matches again. It rejects weak matches, low resolution, missing field coverage, and severe blur with a page-specific error instead of extracting from a guessed resize. The bundled templates use bundled sample pages as references; newly registered templates require an uploaded reference whose pages pass an alignment check. The result viewer displays the aligned page used for extraction. A reference is required for local and Gemini routes. Individual aligned previews and results are stored per run, so simultaneous comparisons do not overwrite one another. PDFium (`pypdfium2`) renders PDFs when installed; local `pdftoppm` is a development fallback. Neither PyMuPDF nor OMRChecker is used. The custom OMR is intentionally small and inspectable for fixed templates.

## Dataset and interpretation

The included synthetic PDFs are fully labeled and can be shared with this code. Their "handwriting" fields contain rendered type, so the handwriting CER/WER number is only a wiring check. For a real comparison, collect consented scans of each target form and manually transcribe every field and selection. Include clean and messy scans, multiple kinds of ticks/crosses/circles, erasures, conflicting marks, skew, and all pages. Keep the same held-out documents for every route and audit ground truth twice. Record per-category sample counts and confidence calibration before drawing a production conclusion.

[FUNSD](https://github.com/crcresearch/FUNSD) is a useful noisy-form reference for OCR and layout, but its original dataset is restricted to non-commercial research/education and its labels do not directly map to these fixed fields or checkbox states. It is therefore **not bundled or used in the benchmark**. The reproducible fixed-template benchmark requires a separately labeled collection. Do not mix a source's annotation license with its code license.

## Dependency and license notes

The code uses [pypdfium2 (Apache-2.0 / BSD-3-Clause; PDFium BSD-style)](https://github.com/pypdfium2-team/pypdfium2/blob/main/README.md), [OpenCV (Apache-2.0)](https://github.com/opencv/opencv/blob/5.x/LICENSE), [Tesseract (Apache-2.0)](https://github.com/tesseract-ocr/tesseract), [PaddleOCR (Apache-2.0)](https://pypi.org/project/paddleocr/), [FastAPI (MIT)](https://github.com/fastapi/fastapi), and [Google Gen AI SDK (Apache-2.0)](https://github.com/googleapis/python-genai/blob/main/pyproject.toml). Check the installed versions, model weights, and transitive packages before commercial distribution. Gemini service data terms and pricing are separate from the SDK license.

## Current evidence and limitations

The pre-existing `reports/benchmark.json` covers the intake baseline before reference registration was added. A local Tesseract smoke check after the review obtained field exact match **1.00, 1.00, 1.00** on the three intake samples and **0.783, 0.826** on the two bank samples. These synthetic examples check integration, not accuracy on real handwriting or phone photos. Handwriting-style names, ID characters, phone digits, and dates remain OCR error sources. Re-run a held-out benchmark after changing templates or thresholds. OpenCV, PaddleOCR, and PDFium were installed into `.deps/` after the base environment initially lacked them; run with `PYTHONPATH=.deps` if using that local installation.

For deployment, use the local pipeline first on fixed templates only after a real held-out benchmark proves its error rate is acceptable. Registration quality checks are heuristic: glare, occlusion, changed form revisions, and legible-looking but wrong OCR can still pass; lost image detail cannot be reconstructed. Route uncertain fields to review or Gemini only if fallback improvement and false high-confidence rates are measured. The pre-existing `reports/benchmark.json` predates the new alignment flow and contains process lifetime high-water RAM readings; rerun it with real captures and the new sampled RAM fields. Pipeline runs are serialized within the API process so simultaneous requests do not corrupt shared Paddle state or process-level measurements; for strict per-route RAM comparison, use isolated worker processes. Configure authentication, HTTPS, access logs, backups, and a retention policy before handling real personal data.
