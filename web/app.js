const $ = id => document.getElementById(id);
let documentInfo=null, results={}, currentPage=1, uploadSequence=0;
let previewTemplate=null, previewPage=0, previewSequence=0;
const labels={gemini:'Gemini Vision',tesseract:'Tesseract',paddle:'PaddleOCR',hybrid:'Hybrid'};
const format=n=>n==null?'—':n<1000?`${Math.round(n)} ms`:`${(n/1000).toFixed(1)} s`;
function updateTemplateLink(){$('edit-template-link').href='/templates/editor?template='+encodeURIComponent($('template').value)}
function renderTemplatePreview(){
  if(!previewTemplate)return;
  const count=previewTemplate.pages.length;
  $('template-preview-page-label').textContent=`PAGE ${previewPage+1} / ${count}`;
  $('template-preview-prev').disabled=previewPage===0;
  $('template-preview-next').disabled=previewPage===count-1;
  const image=$('template-preview-image'),message=$('template-preview-message');
  const url=`/api/templates/${encodeURIComponent(previewTemplate.id)}/preview/${previewPage+1}`;
  image.hidden=true;
  message.hidden=false;
  message.textContent='Loading page preview…';
  image.onload=()=>{image.hidden=false;message.hidden=true};
  image.onerror=()=>{image.hidden=true;message.hidden=false;message.textContent='No reference image is available for this page.'};
  image.alt=`${previewTemplate.name}, page ${previewPage+1} of ${count}`;
  image.src=url;
}
async function openTemplatePreview(){
  const id=$('template').value;
  if(!id)return;
  const sequence=++previewSequence;
  previewTemplate=null;
  previewPage=0;
  $('template-preview-title').textContent='Loading template…';
  $('template-preview-description').textContent='';
  $('template-preview-page-label').textContent='';
  $('template-preview-image').hidden=true;
  $('template-preview-message').hidden=false;
  $('template-preview-message').textContent='Loading preview…';
  $('template-preview-prev').disabled=true;
  $('template-preview-next').disabled=true;
  $('template-preview-dialog').showModal();
  try{
    const response=await fetch(`/api/templates/${encodeURIComponent(id)}`);
    if(!response.ok)throw Error('Could not load the selected template.');
    const data=await response.json();
    if(sequence!==previewSequence)return;
    previewTemplate=data;
    $('template-preview-title').textContent=data.name;
    const fields=data.pages.reduce((sum,page)=>sum+page.fields.length,0);
    $('template-preview-description').textContent=`${data.pages.length} page${data.pages.length===1?'':'s'} · ${fields} fields · ${data.source_document_id?'Registered reference form':'Example completed form'}`;
    renderTemplatePreview();
  }catch(error){
    if(sequence===previewSequence){$('template-preview-title').textContent='Preview unavailable';$('template-preview-message').textContent=error.message}
  }
}
$('preview-template').addEventListener('click',openTemplatePreview);
$('close-template-preview').addEventListener('click',()=>$('template-preview-dialog').close());
$('template-preview-dialog').addEventListener('close',()=>{previewSequence++});
$('template-preview-prev').addEventListener('click',()=>{if(previewTemplate&&previewPage>0){previewPage--;renderTemplatePreview()}});
$('template-preview-next').addEventListener('click',()=>{if(previewTemplate&&previewPage<previewTemplate.pages.length-1){previewPage++;renderTemplatePreview()}});
$('template').addEventListener('change',()=>{updateTemplateLink();results={};$('results').hidden=true;if(documentInfo)$('run-status').textContent='Template changed. Run the comparison again.'});
async function init(){if(location.protocol==='file:'){$('launch-notice').hidden=false;$('api-status').textContent='● SERVER REQUIRED';$('file').disabled=true;$('file-name').textContent='Start the Python server to upload a PDF or image';return}const [templateResponse,statusResponse]=await Promise.all([fetch('/api/templates'),fetch('/api/status')]);if(!templateResponse.ok||!statusResponse.ok)throw Error('The API is unavailable');const list=await templateResponse.json(),status=await statusResponse.json();const templateSelect=$('template');if(!templateSelect)throw Error('The comparison page did not load completely. Reload the page.');templateSelect.innerHTML=list.map(t=>`<option value="${escapeHTML(t.id)}">${escapeHTML(t.name)} · ${t.fields} fields</option>`).join('');updateTemplateLink();$('preview-template').disabled=!list.length;$('api-status').textContent='● API CONNECTED';$('api-status').title=status.gemini_configured?'Gemini configured':'Gemini needs an API key';for(const input of document.querySelectorAll('.methods input')){const available=status.opencv_available&&{gemini:status.gemini_configured,tesseract:status.tesseract_available,paddle:status.paddle_available,hybrid:status.gemini_configured&&status.paddle_available}[input.value];input.checked=available;input.disabled=!available;input.closest('label').classList.toggle('unavailable',!available);input.closest('label').title=available?'Ready to run':status.opencv_available?`${labels[input.value]} is not configured on this server`:'OpenCV is not installed on this server';if(!available)input.closest('label').querySelector('.method-tag').textContent='SETUP';}loadGeneratedForm()}
function loadGeneratedForm(){
  const saved=sessionStorage.getItem('formLabGeneratedDocument');
  if(!saved)return;
  sessionStorage.removeItem('formLabGeneratedDocument');
  try{
    const data=JSON.parse(saved);
    if(!data.document_id||!data.filename||!data.page_count||!Array.isArray(data.pages))throw Error('Invalid generated form');
    uploadSequence++;
    documentInfo=data;
    currentPage=1;
    results={};
    if([...$('template').options].some(option=>option.value===data.template_id))$('template').value=data.template_id;
    updateTemplateLink();
    $('file-name').textContent=data.filename;
    $('drop-help').textContent='Generated form ready · click to replace';
    $('run').disabled=false;
    $('run-status').textContent='Generated form loaded. Select methods and run the comparison.';
  }catch(error){$('run-status').textContent='The generated form could not be loaded. Choose a document to compare.'}
}
init().catch(e=>{const pageError=e.message.includes('comparison page did not load');$('api-status').textContent=pageError?'● PAGE ERROR':'● API UNAVAILABLE';$('run-status').textContent=pageError?e.message:'Could not connect to the Python API. Check the server, then reload this page.';console.error('Comparison initialization failed:',e)});
$('file').addEventListener('change',e=>handleFile(e.target.files[0]));
const drop=$('drop');drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('drag')});drop.addEventListener('dragleave',()=>drop.classList.remove('drag'));drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('drag');handleFile(e.dataTransfer.files[0])});
async function handleFile(file){if(!file)return;const sequence=++uploadSequence;documentInfo=null;results={};$('results').hidden=true;$('run').disabled=true;if(!/\.(pdf|jpe?g|png|webp|tiff?)$/i.test(file.name)){$('run-status').textContent='Choose a PDF, JPEG, PNG, WebP, or TIFF file.';return}if(file.size>25*1024*1024){$('run-status').textContent='The file exceeds the 25 MB limit.';return}$('file-name').textContent=`Uploading ${file.name}…`;$('run-status').textContent='Preparing page previews…';try{const form=new FormData();form.append('file',file);const response=await fetch('/api/documents',{method:'POST',body:form});const data=await response.json();if(sequence!==uploadSequence)return;if(!response.ok)throw Error(data.detail||'Upload failed');documentInfo=data;currentPage=1;$('file-name').textContent=file.name;$('drop-help').textContent=`${data.page_count} page${data.page_count===1?'':'s'} ready · click to replace`;$('run').disabled=false;$('run-status').textContent='Document ready. Select methods and run the comparison.'}catch(e){if(sequence!==uploadSequence)return;$('file-name').textContent='Drop a scanned PDF or image here';$('run-status').textContent=e.message}}
$('run').addEventListener('click',async()=>{if(!documentInfo)return;const selected=[...document.querySelectorAll('.methods input:checked')].map(x=>x.value);if(!selected.length){$('run-status').textContent='Select at least one available method.';return}const rateText=$('hourly-rate').value.trim();const hourlyRate=rateText===''?null:Number(rateText);if(hourlyRate!==null&&(!Number.isFinite(hourlyRate)||hourlyRate<0)){$('run-status').textContent='Enter a valid non-negative machine rate.';return}const button=$('run'),documentId=documentInfo.document_id,templateId=$('template').value,sequence=uploadSequence;button.disabled=true;$('results').hidden=true;button.firstChild.textContent='Processing document… ';$('run-status').textContent=`Running ${selected.length} method${selected.length===1?'':'s'} across ${documentInfo.page_count} page${documentInfo.page_count===1?'':'s'}…`;try{const response=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({document_id:documentId,template_id:templateId,pipelines:selected,compute_hourly_rate_usd:hourlyRate})});const data=await response.json();if(sequence!==uploadSequence||documentInfo?.document_id!==documentId)return;if($('template').value!==templateId){$('run-status').textContent='Template changed. Run the comparison again.';return}if(!response.ok)throw Error(data.detail||'Run failed');results=data.results;documentInfo.aligned_pages=data.aligned_pages;documentInfo.alignment_elapsed_ms=data.alignment_elapsed_ms;$('results').hidden=false;render();const success=Object.values(results).filter(r=>r.status==='ok').length;$('run-status').textContent=success?`Complete: ${success} of ${selected.length} methods returned results.`:Object.values(results).map(r=>r.error).filter(Boolean)[0]||'No method completed.';$('results').scrollIntoView({behavior:'smooth'})}catch(e){if(sequence===uploadSequence&&documentInfo?.document_id===documentId)$('run-status').textContent=e.message}finally{button.disabled=!documentInfo;button.firstChild.textContent='Run comparison '}});
$('prev').onclick=()=>{currentPage=Math.max(1,currentPage-1);render()};$('next').onclick=()=>{currentPage=Math.min(documentInfo.page_count,currentPage+1);render()};$('focus').onchange=()=>{$('focus').dataset.selected=$('focus').value;renderOverlay(Object.keys(results).filter(id=>results[id].status==='ok'))};$('json-pipeline').onchange=renderJSON;
function render(){if(!documentInfo)return;const ids=Object.keys(results),ok=ids.filter(id=>results[id].status==='ok');$('run-summary').textContent=`${documentInfo.filename} · ${documentInfo.page_count} PAGE${documentInfo.page_count>1?'S':''}`;const apiEstimates=ok.map(id=>results[id].data.cost?.api_estimate_usd);const totalApi=ok.length&&apiEstimates.every(value=>value!=null)?`EST. API ${formatUsd(apiEstimates.reduce((sum,value)=>sum+value,0))}`:'API ESTIMATE UNAVAILABLE';$('summary').innerHTML=`<span>${ok.length} / ${ids.length} PIPELINES READY</span><span>ALIGNMENT ${format(documentInfo.alignment_elapsed_ms)}</span><span>${totalApi}</span><span>${ids.reduce((n,id)=>n+(results[id].data?.usage?.fallback_fields||0),0)} FALLBACK FIELDS</span><span>${ids.filter(id=>results[id].status==='error').length} UNAVAILABLE</span>`;$('page-image').src=(documentInfo.aligned_pages||documentInfo.pages)[currentPage-1];$('page-number').textContent=`PAGE ${currentPage} / ${documentInfo.page_count}`;$('prev').disabled=currentPage===1;$('next').disabled=currentPage===documentInfo.page_count;
 $('focus').innerHTML='<option value="">ALL METHODS</option>'+ok.map(id=>`<option value="${id}">${labels[id]}</option>`).join('');const focus=$('focus').dataset.selected||'';$('focus').value=ok.includes(focus)?focus:'';$('focus').dataset.selected=$('focus').value;
 $('json-pipeline').innerHTML=ids.map(id=>`<option value="${id}">${labels[id]}</option>`).join('');$('json-pipeline').value=ids.includes($('json-pipeline').dataset.selected)?$('json-pipeline').dataset.selected:ids[0];renderJSON();renderFields(ok);renderOverlay(ok);renderPerformance(ids)}
function renderJSON(){const id=$('json-pipeline').value;$('json-pipeline').dataset.selected=id;$('json').textContent=JSON.stringify(results[id]||{},null,2)}
function renderFields(ok){
  const page=currentPage-1;
  const rows=new Map();
  for(const id of ok){
    for(const field of results[id].data.pages[page]?.fields||[]){
      if(!rows.has(field.name))rows.set(field.name,{label:field.label||field.name,category:field.category||'',fields:{}});
      rows.get(field.name).fields[id]=field;
    }
  }
  const container=$('field-table');
  if(!ok.length){container.innerHTML='<div class="empty-state">No pipeline completed. Inspect the performance panel for dependency or API errors.</div>';return}
  if(!rows.size){container.innerHTML='<div class="empty-state">No fields were returned for this page.</div>';return}
  container.style.setProperty('--table-min-width',`${145+ok.length*150}px`);
  let html=`<table class="comparison-table"><colgroup><col class="field-column">${ok.map(()=>'<col>').join('')}</colgroup><thead><tr><th scope="col">FIELD</th>${ok.map(id=>`<th scope="col">${labels[id]}</th>`).join('')}</tr></thead><tbody>`;
  for(const row of rows.values()){
    const values=new Set(ok.map(id=>{
      const value=row.fields[id]?.value;
      return value==null||String(value).trim()===''?null:String(value).trim();
    }));
    const conflict=values.size>1;
    html+=`<tr><th scope="row" class="field-name">${escapeHTML(row.label)}${row.category?`<small>${escapeHTML(row.category)}</small>`:''}</th>${ok.map(id=>{
      const field=row.fields[id];
      if(!field)return '<td class="cell error">Missing field</td>';
      const missing=field.value==null||String(field.value).trim()==='';
      const value=missing?'Missing':escapeHTML(String(field.value));
      const note=field.fallback?`Gemini ${field.fallback.accepted?'accepted':'review needed'}`:field.uncertain?'Review needed':field.source||'';
      return `<td class="cell${conflict?' conflict':''}${field.uncertain?' uncertain':''}${missing?' error':''}">${value}${note?`<small>${escapeHTML(note)}</small>`:''}</td>`;
    }).join('')}</tr>`;
  }
  container.innerHTML=html+'</tbody></table>';
}
function renderOverlay(ok){const focus=$('focus').value;const id=focus||ok[0];const data=results[id]?.data,fields=data?.pages[currentPage-1]?.fields||[],size=data?.page_size||[850,1100];const overlay=$('overlay');overlay.innerHTML='';for(const f of fields){const boxes=f.kind==='choice'?(f.options||[]).map(o=>({box:o.box,label:`${f.label}: ${o.value}`,uncertain:f.uncertain})):f.box?[{box:f.box,label:f.label,uncertain:f.uncertain}]:[];for(const b of boxes){if(!b.box)continue;const el=document.createElement('div');el.className=`box${b.uncertain?' uncertain':''}`;el.style.left=`${b.box[0]/size[0]*100}%`;el.style.top=`${b.box[1]/size[1]*100}%`;el.style.width=`${(b.box[2]-b.box[0])/size[0]*100}%`;el.style.height=`${(b.box[3]-b.box[1])/size[1]*100}%`;el.title=b.label;overlay.append(el)}}}
function formatUsd(amount){
  if(amount==null)return 'Unavailable';
  if(amount===0)return '$0.00';
  return '$'+amount.toFixed(amount<0.0001?8:amount<0.01?6:4);
}
function renderPerformance(ids){
  $('performance').innerHTML=ids.map(id=>{
    const result=results[id];
    if(result.status!=='ok')return `<article class="performance-item is-error"><div class="perf-heading"><strong>${labels[id]}</strong><span>Unavailable</span></div><p>${escapeHTML(result.error||'Method failed')}</p></article>`;
    const data=result.data, memory=data.memory||{}, usage=data.usage||{}, cost=data.cost||{};
    const apiCalls=usage.api_calls||0;
    const apiPrice=cost.api_estimate_usd==null?'Estimate unavailable':formatUsd(cost.api_estimate_usd);
    const computePrice=cost.compute_estimate_usd==null?'Rate not configured':formatUsd(cost.compute_estimate_usd);
    const model=cost.model?escapeHTML(cost.model):'No API model';
    const rates=cost.rates, breakdown=cost.api_breakdown_usd;
    const pricing=rates?`<span>${escapeHTML(rates.source)} · input $${rates.input_per_million}/1M · output $${rates.output_per_million}/1M${rates.cached_input_per_million==null?'':` · cached $${rates.cached_input_per_million}/1M`}</span>${rates.url?` <a href="${escapeHTML(rates.url)}" target="_blank" rel="noopener noreferrer">Pricing source ↗</a>`:''}`:'<span>No verified rate for this model. Configure model-specific rates to estimate API cost.</span>';
    const breakdownText=breakdown?`Input ${formatUsd(breakdown.input_usd)} · cached ${formatUsd(breakdown.cached_input_usd)} · output and thinking ${formatUsd(breakdown.output_usd)}`:'';
    return `<article class="performance-item">
      <div class="perf-heading"><strong>${labels[id]}</strong><span>${apiCalls?model:'Local processing'}</span></div>
      <div class="perf-metrics">
        <div><small>Elapsed</small><b>${format(data.elapsed_ms)}</b></div>
        <div><small>CPU used</small><b>${format(data.cpu_ms)}</b></div>
        <div><small>RAM at start</small><b>${memory.start_mb??'—'} MB</b></div>
        <div><small>Peak RAM</small><b>${memory.peak_mb??data.process_peak_ram_mb??'—'} MB</b></div>
      </div>
      <div class="perf-memory"><span>RAM rise during run</span><strong>+${memory.increase_mb??'—'} MB</strong></div>
      <div class="perf-costs"><div><small>Estimated API charge</small><strong>${apiPrice}</strong></div><div><small>Estimated machine cost</small><strong>${computePrice}</strong></div></div>${cost.total_estimate_usd!=null?`<div class="perf-total"><span>Estimated combined cost</span><strong>${formatUsd(cost.total_estimate_usd)}</strong></div>`:''}
      <div class="perf-usage">${apiCalls?`${apiCalls} Gemini call${apiCalls===1?'':'s'} · ${usage.input_tokens} input · ${usage.output_tokens} response · ${usage.thinking_tokens} thinking · ${usage.total_tokens_complete===false?"—":usage.total_tokens} total tokens${usage.tool_input_tokens?` · ${usage.tool_input_tokens} tool input`:''}${usage.cached_input_tokens?` · ${usage.cached_input_tokens} cached`:''}${usage.complete===false?' · token report incomplete':''}`:'No external API calls'}${usage.fallback_fields?` · ${usage.fallback_fields} fallback fields`:''}</div>
      <details class="perf-explain"><summary>Measurement and pricing details</summary><div>${apiCalls?pricing:'API charge is $0 for this method. Local CPU and RAM have no universal price.'}${breakdownText?`<br>${breakdownText}`:''}<br>Machine cost = elapsed time × ${cost.compute_hourly_rate_usd==null?'your configured hourly rate':'$'+cost.compute_hourly_rate_usd+'/hour'}. RAM is sampled process-tree RSS during this run; start and peak include memory already loaded.</div></details>
    </article>`;
  }).join('');
}
function escapeHTML(x){return String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
