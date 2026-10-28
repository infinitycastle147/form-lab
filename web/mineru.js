import * as pdfjsLib from '/static/vendor/pdfjs/pdf.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.mjs';

const $ = id => document.getElementById(id);
const fileInput = $('file');
const dropzone = $('dropzone');
let selectedFile = null;
let rawMarkdown = '';
let resultFileName = '';
let timer = null;
let available = true;
let busy = false;
let previewUrl = null;
let previewDocument = null;
let previewLoadingTask = null;
let previewPage = 1;
let previewToken = 0;
let previewRenderToken = 0;

function clearPreview() {
  previewToken++;
  previewRenderToken++;
  if (previewLoadingTask) previewLoadingTask.destroy().catch(() => {});
  previewLoadingTask = null;
  previewDocument = null;
  $('preview').hidden = true;
  $('preview-canvas').replaceChildren();
  $('preview-open').removeAttribute('href');
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
}

async function renderPreviewPage() {
  const pdfDocument = previewDocument;
  if (!pdfDocument) return;
  const renderToken = ++previewRenderToken;
  $('preview-message').hidden = false;
  $('preview-message').textContent = 'Rendering PDF preview…';
  $('preview-prev').disabled = previewPage <= 1;
  $('preview-next').disabled = previewPage >= pdfDocument.numPages;
  $('preview-page').textContent = `${previewPage} / ${pdfDocument.numPages}`;
  try {
    const page = await pdfDocument.getPage(previewPage);
    const pageWidth = page.getViewport({scale: 1}).width;
    const scale = Math.min(1.5, Math.max(0.4, ($('preview-body').clientWidth - 32) / pageWidth));
    const viewport = page.getViewport({scale});
    const outputScale = Math.min(window.devicePixelRatio || 1, 2);
    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(viewport.width * outputScale);
    canvas.height = Math.floor(viewport.height * outputScale);
    canvas.style.width = `${Math.floor(viewport.width)}px`;
    canvas.style.height = `${Math.floor(viewport.height)}px`;
    const context = canvas.getContext('2d');
    await page.render({canvasContext: context, viewport,
      transform: outputScale === 1 ? null : [outputScale, 0, 0, outputScale, 0, 0]}).promise;
    if (renderToken !== previewRenderToken) return;
    $('preview-canvas').replaceChildren(canvas);
    $('preview-message').hidden = true;
  } catch (error) {
    if (renderToken !== previewRenderToken) return;
    $('preview-message').textContent = 'Preview unavailable. You can still convert this PDF.';
    $('preview-canvas').replaceChildren();
  }
}

async function showPreview(file) {
  clearPreview();
  const token = previewToken;
  previewUrl = URL.createObjectURL(file);
  $('preview-name').textContent = file.name;
  $('preview-open').href = previewUrl;
  $('preview').hidden = false;
  $('preview-message').hidden = false;
  $('preview-message').textContent = 'Loading PDF preview…';
  $('preview-page').textContent = 'Loading…';
  $('preview-prev').disabled = true;
  $('preview-next').disabled = true;
  try {
    const data = new Uint8Array(await file.arrayBuffer());
    if (token !== previewToken) return;
    const task = pdfjsLib.getDocument({data, cMapUrl: '/static/vendor/pdfjs/cmaps/', cMapPacked: true,
      standardFontDataUrl: '/static/vendor/pdfjs/standard_fonts/', wasmUrl: '/static/vendor/pdfjs/wasm/'});
    previewLoadingTask = task;
    const document = await task.promise;
    if (token !== previewToken) return;
    previewDocument = document;
    previewPage = 1;
    await renderPreviewPage();
  } catch (error) {
    if (token !== previewToken) return;
    $('preview-message').textContent = 'Preview unavailable. You can still convert this PDF.';
  }
}

$('preview-prev').addEventListener('click', () => {
  if (!previewDocument || previewPage <= 1) return;
  previewPage--;
  renderPreviewPage();
});
$('preview-next').addEventListener('click', () => {
  if (!previewDocument || previewPage >= previewDocument.numPages) return;
  previewPage++;
  renderPreviewPage();
});

fetch('/api/mineru/status').then(response => response.json()).then(data => {
  available = data.available;
  $('convert').disabled = !available || !selectedFile || !selectedFile.name.toLowerCase().endsWith('.pdf') || selectedFile.size > 25 * 1048576;
  if (!available) {
    $('convert').disabled = true;
    $('status').textContent = 'MinerU worker is unavailable. Install and configure MinerU on the server, then refresh.';
  }
}).catch(() => { $('status').textContent = 'Cannot reach the MinerU API. Start the Python server.'; });

function setFile(file) {
  if (!file || busy) return;
  selectedFile = file;
  rawMarkdown = '';
  $('copy').disabled = true;
  $('download').disabled = true;
  $('metrics').hidden = true;
  $('output-title').textContent = 'Waiting for a document';
  showState('empty');
  $('file-label').textContent = `${file.name} · ${(file.size / 1048576).toFixed(2)} MB`;
  const valid = file.name.toLowerCase().endsWith('.pdf') && file.size <= 25 * 1048576;
  if (valid) showPreview(file);
  else clearPreview();
  $('convert').disabled = !valid || !available;
  $('status').textContent = !available ? 'MinerU worker is unavailable. Install and configure MinerU on the server, then refresh.' : valid ? 'Ready to convert.' : 'Choose a PDF no larger than 25 MB.';
}

fileInput.addEventListener('change', () => setFile(fileInput.files[0]));
for (const eventName of ['dragenter', 'dragover']) {
  dropzone.addEventListener(eventName, event => { event.preventDefault(); dropzone.classList.add('dragging'); });
}
for (const eventName of ['dragleave', 'drop']) {
  dropzone.addEventListener(eventName, event => { event.preventDefault(); dropzone.classList.remove('dragging'); });
}
dropzone.addEventListener('drop', event => setFile(event.dataTransfer.files[0]));
window.addEventListener('pagehide', clearPreview);

function showState(state) {
  for (const id of ['empty', 'loading', 'error', 'markdown']) $(id).hidden = id !== state;
}

function duration(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms.toFixed(0)} ms`;
}

$('convert').addEventListener('click', async () => {
  if (!selectedFile || busy) return;
  busy = true;
  fileInput.disabled = true;
  $('convert').disabled = true;
  $('copy').disabled = true;
  $('download').disabled = true;
  $('metrics').hidden = true;
  $('output-title').textContent = selectedFile.name;
  $('status').textContent = 'Conversion running…';
  showState('loading');
  const started = performance.now();
  timer = setInterval(() => {
    const seconds = Math.floor((performance.now() - started) / 1000);
    $('live-time').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')} elapsed`;
  }, 250);
  const form = new FormData();
  form.append('file', selectedFile);
  try {
    const response = await fetch('/api/mineru/convert', { method: 'POST', body: form });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Conversion failed.');
    rawMarkdown = result.markdown;
    resultFileName = selectedFile.name;
    $('markdown').textContent = rawMarkdown;
    $('output-title').textContent = `${result.pages} page${result.pages === 1 ? '' : 's'} · ${rawMarkdown.length.toLocaleString()} characters`;
    $('total-time').textContent = duration(result.metrics.total_ms);
    $('conversion-time').textContent = duration(result.metrics.conversion_ms);
    $('cpu-time').textContent = `${result.metrics.cpu_seconds.toFixed(2)} s`;
    $('cpu-percent').textContent = `${result.metrics.cpu_percent_one_core.toFixed(1)}%`;
    $('ram-end').textContent = `${result.metrics.ram_at_export_mb.toFixed(1)} MB`;
    $('ram-peak').textContent = `${result.metrics.ram_peak_mb.toFixed(1)} MB`;
    $('metrics').hidden = false;
    $('copy').disabled = false;
    $('download').disabled = false;
    $('status').textContent = 'Conversion complete. Markdown is shown exactly as written by MinerU.';
    showState('markdown');
  } catch (error) {
    $('error').textContent = error.message;
    $('status').textContent = 'Conversion failed.';
    showState('error');
  } finally {
    clearInterval(timer);
    busy = false;
    fileInput.disabled = false;
    $('convert').disabled = !available || !selectedFile || !selectedFile.name.toLowerCase().endsWith('.pdf') || selectedFile.size > 25 * 1048576;
  }
});

$('copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(rawMarkdown);
    $('copy').textContent = 'Copied';
    setTimeout(() => { $('copy').textContent = 'Copy'; }, 1800);
  } catch {
    $('status').textContent = 'Clipboard access failed. Use Download .md instead.';
  }
});
$('download').addEventListener('click', () => {
  const url = URL.createObjectURL(new Blob([rawMarkdown], { type: 'text/markdown;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `${resultFileName.replace(/\.pdf$/i, '') || 'mineru-output'}.md`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
