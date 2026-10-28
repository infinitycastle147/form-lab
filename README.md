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

## Dynamic extraction (separate workflow)

Open **Dynamic extraction** at `/dynamic`. Enter the required output schema as JSON, then upload a completed PDF. The page uploads the PDF and starts extraction automatically; editing the schema afterward enables a new run. Results appear in a separate full-width review section with a large page preview and field evidence. Selecting a field highlights its source on the corrected page. The result is a JSON object with exactly the requested top-level keys; table values contain row objects. Missing, conflicting, or invalid values are `null`; the inspection panel shows the matched question, page, source, and review status. Use **Download JSON** for the values object.

The schema can be a list of keys or an object with descriptions, types (`string`, `date`, `number`, `boolean`, `choice`, `array`, `table`), aliases, and `allowed_values`. Descriptions and aliases are especially useful when a form question uses different wording from the output key. Set `allowed_values` for visual choices; `array` returns all marked selections. A `table` field uses a `columns` map and returns an array of row objects. An `array` with `items.type: "object"` and `items.properties` is also accepted for table rows. The page has a **Load sample form schema** button for the supplied general application form. For example:

```json
{
  "applicant_name": {"description": "Full name of the applicant", "aliases": ["Name", "Customer name"]},
  "date_of_birth": {"description": "Applicant birth date", "type": "date", "aliases": ["DOB", "Birth date"]},
  "employment_history": {"type": "table", "aliases": ["Employment History"],
    "columns": {"from": ["From (MM/YYYY)"], "to": ["To (MM/YYYY)"], "employer": ["Employer"]}}
}
```

This route does not save or require a template. OpenCV rectifies a clear photographed page boundary or moderate tilt, enlarges only small pages, detects marked selection controls near OCR option labels, and finds ruled table cells. It reconstructs a missing cell boundary when the neighboring rows establish the column grid, then assigns positioned OCR text to cells. Extraction uses PDF form widgets and positioned text from local PaddleOCR, with Tesseract fallback for missing fields. The dynamic route never calls Gemini, even if `GEMINI_API_KEY` is configured; the existing comparison routes still work as before. Supply aliases when the form wording differs from a schema key. OCR text and table rows remain flagged for review, and handwriting or unfamiliar wording may remain `null`. Marked selections are reliable only when the option labels and nearby controls are both detected; faint or conflicting marks are flagged. Borderless, merged-cell, or badly damaged tables may still require manual review. Results need validation against real completed forms before production use.

The first dynamic run can be slow because it recognizes text across the full page and loads the local Paddle models. The app holds a bounded in-memory OCR cache for the latest eight distinct PDFs, so rerunning with an edited schema or re-uploading the same PDF reuses recognized text. Restarting the server clears this cache. The dynamic route shares the Paddle engine with the comparison route to avoid loading two copies into memory.

## Docling Markdown (isolated workflow)

Open **Docling Markdown** at `/docling` to upload a PDF and receive the unmodified string returned by `result.document.export_to_markdown()`. This page has its own API route (`POST /api/docling/convert`) and starts a separate Python worker for each conversion; it does not use templates, alignment, dynamic extraction, or the four comparison pipelines. The page offers Docling PDF options for PaddleOCR and table structure recognition. Both are enabled by default. A small OCR adapter feeds PaddleOCR's recognized text and boxes into Docling's OCR stage; the exported Markdown is not changed afterward. The worker uses the same PaddleOCR 3 package versions and initialization settings as the application, but has its own process and environment. The container installs Docling and PaddleOCR in a dedicated Python environment at `/opt/docling-venv`. For local use, create a separate worker environment:

```sh
uv venv .venv-docling --python 3.12
uv pip install --python .venv-docling/bin/python -r requirements-docling.txt
```

The server detects `.venv-docling/bin/python` automatically on the next status check. Set `DOCLING_PYTHON` to another interpreter path if preferred. The environment must be able to import Docling and the project source. Refresh `/docling` after installation.

The page previews the selected PDF locally in the browser before conversion, with previous/next controls for multiple pages. The viewer uses a bundled copy of Mozilla PDF.js and does not change the Docling conversion. The UI shows total worker time, Docling conversion time, worker CPU time, and sampled worker RAM. CPU percentage expresses CPU seconds as a share of one core's elapsed time, so it can exceed 100% when work uses several cores. RAM is sampled from the worker and its children every 50 ms; short peaks can be missed. The first run may take longer while Docling downloads or loads its models. The raw Markdown viewer, copy button, and `.md` download preserve the exported string. Uploads live only in a temporary directory for the conversion and are deleted afterward.

## Dolphin and MinerU Markdown (isolated workflows)

Open `/dolphin` or `/mineru` for the same 25 MB PDF upload, local page preview, raw Markdown viewer, copy/download actions, and sampled worker metrics as the Docling page. Each has its own `POST /api/<engine>/convert` and `GET /api/<engine>/status` routes. The commands process all PDF pages and return the exact `.md` file they write. The app does not run templates, alignment, or field extraction on these outputs. Relative image references in generated Markdown refer to temporary parser assets and will not resolve after the request ends.

For **Dolphin-v2**, follow the [official installation and model instructions](https://github.com/bytedance/Dolphin). Clone the repository into `./Dolphin`, create `./.venv-dolphin`, install its `requirements.txt` there, and download the `ByteDance/Dolphin-v2` weights into `./Dolphin/hf_model`. The route invokes `demo_page.py` from that checkout and reads `markdown/input.md` from its temporary output directory. Set `DOLPHIN_REPO`, `DOLPHIN_MODEL_PATH`, or `DOLPHIN_PYTHON` to use other paths. For example:

Dolphin-v2's model weights are about 7.5 GB, and its current CPU path expands them to float32. Use a machine with substantially more than 8 GB RAM for this worker; leave it uninstalled on smaller laptops.

```sh
git clone https://github.com/bytedance/Dolphin.git Dolphin
python3 -m venv .venv-dolphin
.venv-dolphin/bin/pip install -r Dolphin/requirements.txt
# Download Dolphin-v2 weights using the repository's Hugging Face instructions.
```

For **MinerU 4**, follow the [official installation instructions](https://github.com/opendatalab/MinerU#installation). Install `mineru>=4.0,<5` into `./.venv-mineru` or set `MINERU_COMMAND` to the installed `mineru` executable. The route runs `mineru parse input.pdf --pages all -o result.md`; `--pages all` avoids the CLI's ten-page PDF default. For example:

```sh
uv venv .venv-mineru --python 3.12
uv pip install --python .venv-mineru/bin/python 'mineru>=4.0,<5'
```

These model runtimes and weights are not included in the default Docker image. Install them in the server environment and configure the paths above before using the new pages. The status check disables conversion until the corresponding command is available. Uploads and parser output live in a temporary directory and are deleted after the response. Run metrics sample the parser process and its children every 50 ms, so short CPU and RAM peaks can be missed.

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
