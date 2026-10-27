const $ = id => document.getElementById(id);
let documentInfo = null;
let result = null;
let pageIndex = 0;
let sequence = 0;

function parseSchema() {
  try {
    const schema = JSON.parse($('schema-input').value);
    if (!schema || (typeof schema !== 'object') || Array.isArray(schema) && schema.length === 0 || !Array.isArray(schema) && Object.keys(schema).length === 0) throw new Error('Add at least one field.');
    const entries = Array.isArray(schema) ? schema.map(key => [key, {}]) : Object.entries(schema);
    if (entries.length > 100) throw new Error('Use no more than 100 fields.');
    for (const [key, options] of entries) {
      if (typeof key !== 'string' || !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(key)) throw new Error('Keys must start with a letter and contain only letters, numbers, or underscores.');
      if (typeof options !== 'string' && (options === null || typeof options !== 'object' || Array.isArray(options))) throw new Error(`Definition for ${key} must be an object or description string.`);
    }
    $('schema-error').hidden = true;
    $('schema-count').textContent = `${entries.length} ${entries.length === 1 ? 'key' : 'keys'}`;
    return schema;
  } catch (error) {
    $('schema-error').textContent = error.message;
    $('schema-error').hidden = false;
    $('schema-count').textContent = 'INVALID JSON';
    return null;
  }
}

function state(name, message) {
  $('empty-state').hidden = name !== 'empty';
  $('busy-state').hidden = name !== 'busy';
  $('error-state').hidden = name !== 'error';
  $('results').hidden = name !== 'result';
  $('inspection').hidden = name !== 'result';
  if (name === 'error') $('error-state').textContent = message;
  if (name === 'busy') $('busy-detail').textContent = message;
  $('engine-state').textContent = name === 'busy' ? '◌ READING DOCUMENT' : name === 'error' ? '● NEEDS ATTENTION' : name === 'result' ? '● EXTRACTION COMPLETE' : '● READY TO SCAN';
}

async function uploadFile(file) {
  if (!file) return;
  if (!(file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf'))) {
    state('error', 'Upload a PDF document.'); return;
  }
  if (file.size > 25 * 1024 * 1024) { state('error', 'PDFs must be 25 MB or smaller.'); return; }
  const schema = parseSchema();
  if (!schema) { state('error', 'Fix the schema JSON, then upload the PDF again.'); return; }
  const current = ++sequence;
  result = null;
  documentInfo = null;
  $('file-name').textContent = file.name;
  $('run-again').disabled = true;
  state('busy', 'Uploading and rendering the PDF');
  try {
    const form = new FormData();
    form.append('file', file);
    const response = await fetch('/api/documents', {method: 'POST', body: form});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not upload the PDF.');
    if (current !== sequence) return;
    documentInfo = data;
    $('run-again').disabled = false;
    await runExtraction(schema, current);
  } catch (error) {
    if (current === sequence) state('error', error.message || 'Could not upload the PDF.');
  }
}

async function runExtraction(schema = parseSchema(), current = ++sequence) {
  if (!documentInfo || !schema) return;
  $('run-again').disabled = true;
  state('busy', `Finding requested fields across ${documentInfo.page_count} ${documentInfo.page_count === 1 ? 'page' : 'pages'}`);
  try {
    const response = await fetch('/api/dynamic/extract', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({document_id: documentInfo.document_id, schema})});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Extraction failed.');
    if (current !== sequence) return;
    result = data;
    showResult();
  } catch (error) {
    if (current === sequence) state('error', error.message || 'Extraction failed.');
  } finally {
    if (current === sequence) $('run-again').disabled = false;
  }
}

function showResult() {
  state('result');
  const entries = Object.entries(result.values);
  const uncertain = result.fields.filter(field => field.uncertain).length;
  $('result-summary').textContent = `${entries.length} fields · ${uncertain} need review`;
  $('json-output').textContent = JSON.stringify(result.values, null, 2);
  const notes = [];
  if (result.timing) notes.push(result.cache_hit ? `Reused OCR from this upload; extraction ${Math.round(result.timing.total_ms)} ms.` : `First extraction ${Math.round(result.timing.total_ms / 1000)} s; schema edits reuse OCR.`);
  const paddleLines = result.pages.reduce((sum, page) => sum + (page.paddle_lines || 0), 0);
  const tesseractLines = result.pages.reduce((sum, page) => sum + (page.tesseract_lines || 0), 0);
  if (entries.every(([, value]) => value === null)) notes.push(`No requested fields matched. OCR found ${paddleLines} Paddle lines and ${tesseractLines} Tesseract lines. Check the schema labels and add aliases matching the printed questions.`);
  if (result.method === 'paddle_ocr') notes.push('Local PaddleOCR read the page. Add schema aliases when form wording differs; review handwriting and uncertain matches.');
  if (result.method === 'tesseract_ocr') notes.push('Tesseract OCR read the page. PaddleOCR was unavailable or missed the requested labels; review uncertain matches.');
  for (const page of result.pages) if (page.corrections.length) notes.push(`Page ${page.number}: ${page.corrections.join(', ').replaceAll('_', ' ')}.`);
  notes.push(...result.method_errors);
  $('result-note').textContent = notes.length ? notes.join(' ') : 'No page correction was necessary. Review uncertain values before use.';
  const list = $('field-list');
  list.replaceChildren();
  for (const field of result.fields) {
    const item = document.createElement('div'); item.className = 'field-item';
    const head = document.createElement('div'); head.className = 'field-head';
    const key = document.createElement('strong'); key.textContent = field.key;
    const badge = document.createElement('span'); badge.className = field.uncertain ? 'uncertain' : '';
    badge.textContent = field.uncertain ? 'REVIEW' : `PAGE ${field.page}`;
    head.append(key, badge);
    const value = document.createElement('p'); value.textContent = field.value === null ? 'null' : String(field.value);
    const question = document.createElement('small'); question.textContent = field.question ? `Matched: ${field.question} · ${field.source}` : `No matching question · ${field.source}`;
    item.append(head, value, question);
    item.addEventListener('click', () => { if (field.page) { pageIndex = field.page - 1; updatePage(); } });
    list.append(item);
  }
  pageIndex = 0;
  updatePage();
  $('run-hint').textContent = 'Edit the schema and run again when needed.';
}

function updatePage() {
  if (!documentInfo) return;
  $('page-image').src = documentInfo.pages[pageIndex];
  $('page-label').textContent = `PAGE ${pageIndex + 1} / ${documentInfo.page_count}`;
  $('prev-page').disabled = pageIndex === 0;
  $('next-page').disabled = pageIndex >= documentInfo.page_count - 1;
  for (const item of $('field-list').children) item.classList.remove('active');
}

$('schema-input').addEventListener('input', () => { parseSchema(); if (documentInfo) $('run-hint').textContent = 'Schema changed. Run extraction to refresh the result.'; });
$('pdf-file').addEventListener('change', event => uploadFile(event.target.files[0]));
const drop = $('pdf-drop');
drop.addEventListener('dragover', event => {event.preventDefault(); drop.classList.add('dragover');});
drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
drop.addEventListener('drop', event => {event.preventDefault(); drop.classList.remove('dragover'); uploadFile(event.dataTransfer.files[0]);});
$('run-again').addEventListener('click', () => runExtraction());
$('prev-page').addEventListener('click', () => {pageIndex--; updatePage();});
$('next-page').addEventListener('click', () => {pageIndex++; updatePage();});
$('copy-json').addEventListener('click', async () => {if (result) {await navigator.clipboard.writeText(JSON.stringify(result.values, null, 2)); $('copy-json').textContent = 'Copied'; setTimeout(() => $('copy-json').textContent = 'Copy', 1800);}});
$('download-json').addEventListener('click', () => {if (!result) return; const url = URL.createObjectURL(new Blob([JSON.stringify(result.values, null, 2)], {type: 'application/json'})); const link = document.createElement('a'); link.href = url; link.download = 'extracted-fields.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);});
parseSchema();
