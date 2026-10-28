const $ = id => document.getElementById(id);
let documentInfo = null;
let result = null;
let pageIndex = 0;
let sequence = 0;
const sampleSchema = {
  full_name: {aliases: ['Full Name']},
  date_of_birth: {type: 'date', aliases: ['Date of Birth']},
  address: {aliases: ['Address']},
  phone_number: {aliases: ['Phone Number']},
  email_address: {aliases: ['Email Address']},
  application_type: {type: 'choice', aliases: ['Type of Application'], allowed_values: ['New Application', 'Renewal', 'Update / Change', 'Other']},
  purpose_of_application: {aliases: ['Purpose of Application']},
  programs: {type: 'array', aliases: ['Which program(s) are you interested in?'], allowed_values: ['Art & Creativity', 'Health & Wellness', 'Computer Skills', 'Language Classes', 'Career Development', 'Other']},
  preferred_schedule: {type: 'choice', aliases: ['Preferred Schedule'], allowed_values: ['Morning', 'Afternoon', 'Evening', 'No Preference']},
  highest_level_completed: {type: 'choice', aliases: ['Highest Level Completed'], allowed_values: ['High School', 'Some College', 'Associate Degree', "Bachelor's Degree", "Master's Degree", 'Other']},
  field_of_study: {aliases: ['Field of Study']},
  reason_for_interest: {aliases: ['Why are you interested in this program?']},
  special_requirements: {type: 'boolean', aliases: ['Do you have any special requirements or accommodations?']},
  employment_history: {type: 'table', aliases: ['Employment History'], columns: {
    from: ['From (MM/YYYY)'], to: ['To (MM/YYYY)'], position: ['Position / Job Title'],
    employer: ['Employer'], responsibilities: ['Key Responsibilities (brief)']
  }},
  signature: {aliases: ['Signature']},
  signature_date: {type: 'date', aliases: ['Date']}
};

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
  const matched = result.fields.filter(field => field.source !== 'unmatched' && field.value !== null).length;
  $('result-summary').textContent = `${matched} of ${entries.length} fields found · ${uncertain} need review`;
  $('evidence-summary').textContent = `${entries.length} REQUESTED`;
  $('json-output').textContent = JSON.stringify(result.values, null, 2);
  const notes = [];
  if (result.timing) notes.push(result.cache_hit ? `Reused OCR from this upload; extraction ${Math.round(result.timing.total_ms)} ms.` : `First extraction ${Math.round(result.timing.total_ms / 1000)} s; schema edits reuse OCR.`);
  const paddleLines = result.pages.reduce((sum, page) => sum + (page.paddle_lines || 0), 0);
  const tesseractLines = result.pages.reduce((sum, page) => sum + (page.tesseract_lines || 0), 0);
  if (entries.every(([, value]) => value === null)) notes.push(`No requested fields matched. OCR found ${paddleLines} Paddle lines and ${tesseractLines} Tesseract lines. Check the schema labels and add aliases matching the printed questions.`);
  if (result.fields.some(field => field.source === 'omr')) notes.push('OpenCV read marked choices from the page image. Review any conflicting or faint marks.');
  if (result.method === 'paddle_ocr') notes.push('Local PaddleOCR read labels and handwriting. Review uncertain text before using it.');
  if (result.method === 'tesseract_ocr') notes.push('Tesseract OCR read the page. PaddleOCR was unavailable or missed the requested labels; review uncertain matches.');
  for (const page of result.pages) if (page.corrections.length) notes.push(`Page ${page.number}: ${page.corrections.join(', ').replaceAll('_', ' ')}.`);
  notes.push(...result.method_errors);
  $('result-note').textContent = notes.length ? notes.join(' ') : 'No page correction was necessary. Review uncertain values before use.';
  const list = $('field-list');
  list.replaceChildren();
  for (const field of result.fields) {
    const item = document.createElement('button'); item.className = 'field-item'; item.type = 'button';
    const head = document.createElement('div'); head.className = 'field-head';
    const key = document.createElement('strong'); key.textContent = field.key;
    const badge = document.createElement('span'); badge.className = field.uncertain ? 'uncertain' : 'verified';
    badge.textContent = field.source === 'unmatched' ? 'NOT FOUND' : field.uncertain ? 'REVIEW' : 'MARK READ';
    head.append(key, badge);
    const value = document.createElement('p');
    value.className = field.value === null ? 'missing-value' : 'field-value';
    const isTable = Array.isArray(field.value) && field.value.some(row => row && typeof row === 'object' && !Array.isArray(row));
    value.textContent = field.value === null ? 'No reliable value' : isTable ? `${field.value.length} ${field.value.length === 1 ? 'row' : 'rows'} extracted` : Array.isArray(field.value) ? field.value.length ? field.value.join(' · ') : 'None selected' : String(field.value);
    const question = document.createElement('small'); question.textContent = field.question ? `MATCHED LABEL  ${field.question}` : 'NO MATCHING LABEL';
    const source = document.createElement('small'); source.className = 'field-source'; source.textContent = `PAGE ${field.page || '—'}  ·  ${field.source.replaceAll('_', ' ').toUpperCase()}`;
    item.append(head, value, question, source);
    if (isTable) {
      const table = document.createElement('div'); table.className = 'field-table';
      for (const [index, row] of field.value.entries()) {
        const heading = document.createElement('strong'); heading.textContent = `ROW ${index + 1}`; table.append(heading);
        for (const [column, text] of Object.entries(row)) {
          const cell = document.createElement('div');
          const name = document.createElement('span'); name.textContent = column.replaceAll('_', ' ');
          const content = document.createElement('span'); content.textContent = text ?? 'Not read';
          cell.append(name, content); table.append(cell);
        }
      }
      item.append(table);
    }
    if (field.options?.length) {
      const choices = document.createElement('div'); choices.className = 'field-options';
      for (const option of field.options) {
        const chip = document.createElement('span'); chip.className = option.selected ? 'selected' : '';
        chip.textContent = `${option.selected ? '✓' : '○'} ${option.value}`;
        choices.append(chip);
      }
      item.append(choices);
    }
    item.addEventListener('click', () => { if (field.page) focusField(field, item); });
    list.append(item);
  }
  pageIndex = 0;
  updatePage();
  $('run-hint').textContent = 'Edit the schema and run again when needed.';
}

function updatePage() {
  if (!documentInfo) return;
  const preview = result?.pages[pageIndex]?.preview || documentInfo.pages[pageIndex];
  $('page-image').src = preview;
  $('full-page').href = preview;
  $('page-label').textContent = `PAGE ${pageIndex + 1} / ${documentInfo.page_count}`;
  $('prev-page').disabled = pageIndex === 0;
  $('next-page').disabled = pageIndex >= documentInfo.page_count - 1;
  $('field-overlay').hidden = true;
  document.querySelector('.preview-image').scrollTop = 0;
  for (const item of $('field-list').children) item.classList.remove('active');
}

function focusField(field, item) {
  pageIndex = field.page - 1;
  updatePage();
  item.classList.add('active');
  const size = result?.pages[pageIndex]?.size;
  if (!field.box || !size) return;
  const [x1, y1, x2, y2] = field.box;
  const overlay = $('field-overlay');
  overlay.style.left = `${100 * x1 / size[0]}%`;
  overlay.style.top = `${100 * y1 / size[1]}%`;
  overlay.style.width = `${100 * (x2 - x1) / size[0]}%`;
  overlay.style.height = `${100 * (y2 - y1) / size[1]}%`;
  overlay.hidden = false;
  const scrollToSource = () => {
    const preview = document.querySelector('.preview-image');
    preview.scrollTop = overlay.offsetTop - preview.clientHeight / 2 + overlay.offsetHeight / 2;
  };
  if ($('page-image').complete) scrollToSource();
  else $('page-image').addEventListener('load', scrollToSource, {once: true});
}

$('schema-input').addEventListener('input', () => { parseSchema(); if (documentInfo) $('run-hint').textContent = 'Schema changed. Run extraction to refresh the result.'; });
$('sample-schema').addEventListener('click', () => { $('schema-input').value = JSON.stringify(sampleSchema, null, 2); parseSchema(); $('run-hint').textContent = documentInfo ? 'Sample schema loaded. Run extraction to refresh the result.' : 'Sample schema loaded. Upload the PDF to start.'; });
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
