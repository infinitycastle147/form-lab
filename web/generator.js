const $ = id => document.getElementById(id);
const storageKey = 'formLabGeneratorState';
let generatorSchema = null;
let generatedInfo = null;
let generationSequence = 0;

function escapeHTML(value) {
  return String(value).replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
}

function readState() {
  try { return JSON.parse(sessionStorage.getItem(storageKey) || 'null'); }
  catch { return null; }
}

function saveState() {
  try {
    sessionStorage.setItem(storageKey, JSON.stringify({
      preset: $('preset').value,
      values: Object.fromEntries(new FormData($('generator-form')).entries()),
      generatedInfo,
    }));
  } catch { /* Form generation still works when browser storage is unavailable. */ }
}

function showGenerated(data) {
  generatedInfo = data;
  $('generated-preview').src = data.pages[0];
  $('preview-placeholder').hidden = true;
  $('downloads').hidden = false;
  for (const format of ['png', 'pdf', 'json']) $('download-' + format).href = data.downloads[format];
  $('test-generated').disabled = false;
  $('generator-message').textContent = 'One-page form ready. Download it or open it in comparison.';
  saveState();
}

function invalidateGenerated() {
  generationSequence++;
  generatedInfo = null;
  $('test-generated').disabled = true;
  $('downloads').hidden = true;
  $('generated-preview').removeAttribute('src');
  $('preview-placeholder').hidden = false;
  $('generator-message').textContent = 'Changes ready. Generate to update the preview.';
  saveState();
}

function loadPreset(index) {
  const values = generatorSchema.presets[index];
  for (const [name, value] of Object.entries(values)) {
    const field = $('generator-form').elements.namedItem(name);
    if (field) field.value = value;
  }
  invalidateGenerated();
  $('generator-message').textContent = 'Preset loaded. Edit any field, then generate.';
}

function initGenerator() {
  $('generator-fields').innerHTML = generatorSchema.text.map(field =>
    `<label class="form-control"><span>${escapeHTML(field.label)}</span><input name="${escapeHTML(field.name)}" maxlength="55" required autocomplete="off"></label>`
  ).join('');
  $('generator-choices').innerHTML = generatorSchema.choices.map(field =>
    `<label class="form-control"><span>${escapeHTML(field.label)}</span><select name="${escapeHTML(field.name)}">${field.options.map(option => `<option value="${escapeHTML(option.value)}">${escapeHTML(option.label)}</option>`).join('')}</select></label>`
  ).join('');

  const saved = readState();
  const preset = Number(saved?.preset);
  $('preset').value = Number.isInteger(preset) && generatorSchema.presets[preset] ? String(preset) : '0';
  loadPreset(Number($('preset').value));
  if (saved?.values && typeof saved.values === 'object') {
    for (const [name, value] of Object.entries(saved.values)) {
      const field = $('generator-form').elements.namedItem(name);
      if (field && typeof value === 'string') field.value = value;
    }
  }
  if (saved?.generatedInfo?.document_id && saved.generatedInfo.pages?.[0] && saved.generatedInfo.downloads) {
    showGenerated(saved.generatedInfo);
  } else {
    saveState();
    if (saved?.values) $('generator-message').textContent = 'Draft restored. Generate to update the preview.';
  }

  $('preset').addEventListener('change', () => loadPreset(Number($('preset').value)));
  $('generator-form').addEventListener('submit', generateForm);
  $('generator-form').addEventListener('input', invalidateGenerated);
  $('generator-form').addEventListener('change', invalidateGenerated);
  $('test-generated').addEventListener('click', () => {
    if (!generatedInfo) return;
    try {
      sessionStorage.setItem('formLabGeneratedDocument', JSON.stringify({
        ...generatedInfo, template_id: generatorSchema.template_id,
      }));
      location.assign('/#workspace-setup');
    } catch {
      $('generator-message').textContent = 'Could not open the comparison. Check that browser storage is available.';
    }
  });
}

async function generateForm(event) {
  event.preventDefault();
  const sequence = ++generationSequence;
  const button = $('generate');
  button.disabled = true;
  $('generator-message').textContent = 'Drawing the form…';
  try {
    const values = Object.fromEntries(new FormData($('generator-form')).entries());
    const response = await fetch('/api/generator', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({values, seed: Date.now()}),
    });
    const data = await response.json();
    if (sequence !== generationSequence) return;
    if (!response.ok) throw Error(data.detail || 'Generation failed');
    showGenerated(data);
    if (matchMedia('(max-width:900px)').matches) {
      const reduceMotion = matchMedia('(prefers-reduced-motion:reduce)').matches;
      document.querySelector('.generator-preview').scrollIntoView({behavior:reduceMotion?'auto':'smooth', block:'start'});
    }
  } catch (error) {
    if (sequence === generationSequence) $('generator-message').textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function init() {
  if (location.protocol === 'file:') {
    $('launch-notice').hidden = false;
    $('api-status').textContent = '● SERVER REQUIRED';
    $('generator-message').textContent = 'Start the Python server to generate a form.';
    $('generate').disabled = true;
    return;
  }
  const response = await fetch('/api/generator/schema');
  if (!response.ok) throw Error('The generator API is unavailable');
  generatorSchema = await response.json();
  $('api-status').textContent = '● GENERATOR READY';
  initGenerator();
}

init().catch(error => {
  $('api-status').textContent = '● API UNAVAILABLE';
  $('generator-message').textContent = error.message;
  $('generate').disabled = true;
});
