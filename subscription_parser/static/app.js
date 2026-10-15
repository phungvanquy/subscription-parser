"use strict";
const $ = id => document.getElementById(id);
const paths = {
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  layers:'<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
  server:'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"/>',
  link:'<path d="m10 13 4-4M8.5 15.5 6 18a4 4 0 0 1-6-6l3-3a4 4 0 0 1 6 0M15 9a4 4 0 0 1 6 0 4 4 0 0 1 0 6l-3 3a4 4 0 0 1-6 0" transform="translate(2 -2) scale(.9)"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  settings:'<path d="M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1 1-3Z"/><circle cx="12" cy="12" r="3"/>',
  refresh:'<path d="M20 11a8 8 0 0 0-14-5L3 9m0-6v6h6M4 13a8 8 0 0 0 14 5l3-3m0 6v-6h-6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3"/>',
  download:'<path d="M12 3v12m-4-4 4 4 4-4M4 16v4h16v-4"/>',
  edit:'<path d="m15 4 5 5M4 20l4-1L21 6l-4-4L4 15l-1 6ZM13 21h8"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  pause:'<path d="M8 5v14M16 5v14"/>',
  play:'<path d="m7 4 14 8-14 8V4Z"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  network:'<rect x="9" y="3" width="6" height="5" rx="1"/><rect x="2" y="16" width="6" height="5" rx="1"/><rect x="16" y="16" width="6" height="5" rx="1"/><path d="M12 8v4M5 16v-4h14v4"/>'
};
function icon(name){return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]||paths.server}</svg>`;}
document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));
const esc = value => String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state=null,page='overview',toastTimer,pollTimer;
let logRequest=0;
let providerOriginal=null,providerWorking=false,testRuns=new Map(),savedRuns=new Set(),editingDevice=null,replacementDevice=null,revealTimer;
const titles={overview:['SUBSCRIPTION WORKSPACE','Your connections, together.','One library. Multiple providers. Ready for your favorite client.','Overview'],providers:['YOUR SOURCES','Every provider, in one place.','Add subscriptions and keep their device identities consistent.','Providers'],servers:['COMBINED LIBRARY','Find your next connection.','Explore your servers and see which clients can import them.','Server library'],exports:['READY TO IMPORT','One link for your client.','Stable subscription links, updated automatically from your providers.','Subscription links'],activity:['WORKSPACE HISTORY','Activity and logs.','Review recent events and debug problems with subscriptions.','Activity & logs'],devices:['SAVED DEVICES','Your device identities.','See where each device is used and manage its saved ID.','Devices'],settings:['WORKSPACE SETTINGS','Keep things running your way.','Manage your subscription refresh schedule.','Settings']};
async function api(path,options={}){const response=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});let data;try{data=await response.json();}catch{throw new Error('The server returned an unexpected response.');}if(!response.ok){if(response.status===401&&path!='/login')showLogin();throw new Error(data.detail||'Request failed');}return data;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,4000);}
function relative(value){if(!value)return 'Never';const seconds=Math.max(0,(Date.now()-new Date(value).getTime())/1000);if(seconds<60)return 'Just now';if(seconds<3600)return `${Math.floor(seconds/60)}m ago`;if(seconds<86400)return `${Math.floor(seconds/3600)}h ago`;return new Date(value).toLocaleDateString();}
function date(value){return value?new Date(value).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';}
function interval(value){return value===3600?'Hourly':value>=3600?`${value/3600} hours`:`${value/60} min`;}
function showLogin(){clearPrivateDialogs();clearTimeout(pollTimer);logRequest++;$('log-output').textContent='';$('refresh-logs').disabled=false;$('workspace').hidden=true;$('login-view').hidden=false;state=null;}
function selectPage(name){page=name;document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.page===name));const t=titles[name];$('page-eyebrow').textContent=t[0];$('page-title').textContent=t[1];$('page-description').textContent=t[2];$('breadcrumb-page').textContent=t[3];$('overview-stats').hidden=name!=='overview';const visible={overview:['providers','exports','diagnostics'],providers:['providers'],servers:['servers','diagnostics'],exports:['exports','diagnostics'],activity:['logs','activity'],devices:['devices'],settings:['settings']}[name];document.querySelectorAll('.page-section').forEach(s=>s.hidden=!visible.includes(s.id.replace('-section','')));window.scrollTo({top:0,behavior:'instant'});if(name==='activity')loadLogs();}
document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>selectPage(b.dataset.page)));
async function loadLogs(){
  if(!state||page!=='activity')return;
  const request=++logRequest;$('refresh-logs').disabled=true;
  try{
    const data=await api('/logs?level='+encodeURIComponent($('log-level').value));
    if(request!==logRequest||!state||page!=='activity')return;
    $('log-retention').textContent=`Latest ${data.limit} matching events · ${data.retention_days} days · ${data.max_bytes/1048576} MiB maximum. Older entries are removed automatically.`;
    const lines=data.entries.map(event=>{
      const context=Object.entries(event.context||{}).map(([key,value])=>{
        if(key==='provider_id')return `provider=${state.providers.find(p=>p.id===value)?.name||value} (${value})`;
        if(key==='node_id')return `server=${state.inventory.find(n=>n.id===value)?.name||value} (${value})`;
        return `${key}=${Array.isArray(value)?value.join(' ← '):value}`;
      }).join(' · ');
      return `${new Date(event.time).toLocaleString()} [${event.level.toUpperCase()}] ${event.message}${context?'\n  '+context:''}`;
    });
    $('log-output').textContent=(data.available?'':'Log storage is unavailable; check the data volume permissions and free space.\n\n')+(lines.join('\n\n')||'No matching events.');
  }catch(error){if(request===logRequest&&state)$('log-output').textContent=error.message;}
  finally{if(request===logRequest)$('refresh-logs').disabled=false;}
}
$('refresh-logs').addEventListener('click',loadLogs);
$('log-level').addEventListener('change',loadLogs);
async function loadState(){clearTimeout(pollTimer);try{state=await api('/state');$('workspace').hidden=false;$('login-view').hidden=true;render();pollTimer=setTimeout(loadState,state.busy?1500:15000);}catch(error){if(state){$('global-error').textContent=error.message;$('global-error').hidden=false;pollTimer=setTimeout(loadState,10000);}}}
function render(){
  const providers=state.providers,inventory=state.inventory||[],types=[...new Set(inventory.map(n=>n.protocol))].sort();
  $('nav-provider-count').textContent=providers.length;$('provider-total').textContent=providers.length;
  $('stat-providers').textContent=providers.filter(p=>p.enabled).length;$('stat-provider-detail').textContent=`${providers.length} providers in your workspace`;
  $('stat-servers').textContent=state.published_total||0;$('stat-duplicate-detail').textContent=`${state.total} nodes in library · ${state.duplicates_removed||0} duplicates combined`;
  $('stat-protocols').textContent=types.length;$('stat-protocol-detail').textContent=types.map(p=>p.toUpperCase()).join(' · ')||'Original protocols preserved';
  const checking=state.busy_action==='checking',working=checking?`Checking servers (${state.check_progress.done}/${state.check_progress.total})…`:'Refreshing subscriptions…';
  $('stat-interval').textContent=interval(state.refresh_seconds);$('stat-next').textContent=state.busy?working:`Next at ${new Date(state.next_refresh*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;
  $('refresh-button').disabled=state.busy;$('refresh-button').classList.toggle('spinning',state.busy);document.querySelector('.refresh-label').textContent=state.busy?(checking?'Checking…':'Refreshing…'):'Refresh all';
  $('check-servers').disabled=state.busy||!state.total;
  $('connection-summary').textContent=state.busy?working:`${state.published_total||0} of ${state.total} nodes included · ${Math.max(0,state.total-(state.published_total||0))} excluded · Last checked ${relative(state.last_check).toLowerCase()} · Recheck every ${interval(state.check_seconds).toLowerCase()}. Checks use saved subscriptions; HWIDs are not sent.`;
  $('last-refresh').textContent=state.busy?working:`Updated ${relative(state.generated_at).toLowerCase()}`;
  $('global-error').hidden=!state.last_error;if(state.last_error)$('global-error').textContent=state.last_error;
  $('provider-rows').innerHTML=providers.map((p,i)=>`<tr><td><div class="provider-cell"><span class="provider-avatar ${['','violet','blue'][i%3]}">${esc(p.name.slice(0,1))}</span><div><span class="provider-name">${esc(p.name)}</span><span class="provider-host">${esc(p.host)}</span></div></div></td><td><span class="status-pill ${esc(p.status)}" title="${esc(p.error||'')}">${esc({fresh:'Up to date',cached:'Cached',unavailable:'Unavailable',disabled:'Paused',pending:'Pending'}[p.status]||p.status)}</span>${p.error?`<span class="provider-host" title="${esc(p.error)}">${esc(p.error.slice(0,45))}${p.error.length>45?'…':''}</span>`:''}</td><td><span class="server-count">${p.count||0}</span></td><td><span class="device-label" title="${esc(p.hwid_id)}">${esc(p.hwid_id===null?'No device ID':state.hwids.find(h=>h.id===p.hwid_id)?.name||'Missing device')}</span></td><td>${relative(p.fetched_at)}</td><td><div class="row-actions"><button class="icon-button" data-action="refresh" data-id="${esc(p.id)}" title="Refresh provider" aria-label="Refresh ${esc(p.name)}" ${state.busy||!p.enabled?'disabled':''}>${icon('refresh')}</button><button class="icon-button" data-action="toggle" data-id="${esc(p.id)}" title="${p.enabled?'Pause':'Enable'} provider" aria-label="${p.enabled?'Pause':'Enable'} ${esc(p.name)}" ${state.busy?'disabled':''}>${icon(p.enabled?'pause':'play')}</button><button class="icon-button" data-action="edit" data-id="${esc(p.id)}" title="Edit provider" aria-label="Edit ${esc(p.name)}">${icon('edit')}</button><button class="icon-button danger" data-action="delete" data-id="${esc(p.id)}" title="Remove provider" aria-label="Remove ${esc(p.name)}" ${state.busy?'disabled':''}>${icon('trash')}</button></div></td></tr>`).join('');
  $('providers-empty').hidden=providers.length>0;
  const formats=[['hiddify.json','Hiddify','hiddify','h','Native JSON · sing-box','Import the full JSON subscription in Hiddify.'],['mihomo.yaml','Mihomo','mihomo','m','Native YAML · Clash compatible','Import as a configuration or proxy provider.'],['xray.json','Xray','xray','x','Native JSON · outbounds','Preserves original Xray chains and dependencies.'],['v2board.txt','V2Board','v2board','v','Text · proxy URI subscription','Import into External Subscriptions in V2Board.'],['combined.json','Combined archive','combined','{ }','JSON · all client formats','Download all converted formats and compatibility notes.']];
  $('export-cards').innerHTML=formats.map(([file,name,key,mark,format,description])=>`<article class="export-card"><div class="export-top"><span class="client-logo ${key}">${mark}</span><div><div class="export-name">${name}</div><div class="export-format">${format}</div></div><span class="export-count">${key==='combined'?(state.published_total||0):(state.counts[key]||0)} servers</span></div><p>${description}</p><div class="link-display">${icon('link')}<span title="${esc(state.links[file])}">${esc(state.links[file])}</span></div><div class="export-actions"><button class="button secondary" data-copy="${file}">${icon('copy')} Copy link</button><a href="${esc(state.links[file])}" download="${file}">${icon('download')} Download</a></div></article>`).join('');
  const selected=$('protocol-filter').value;$('protocol-filter').innerHTML='<option value="">All protocols</option>'+types.map(t=>`<option value="${esc(t)}">${esc(t.toUpperCase())}</option>`).join('');if(types.includes(selected))$('protocol-filter').value=selected;
  renderServers();renderIssues();
  $('activity-rows').innerHTML=(state.history||[]).map(event=>`<div class="activity-row">${icon(event.action==='refresh'?'refresh':'network')}<div><strong>${event.action==='refresh'?'Subscriptions refreshed':event.action==='connection check'?'Connections checked':'Provider configuration updated'}</strong><p>${event.published??event.servers} included / ${event.servers} nodes · ${event.fresh} sources up to date${event.cached?` · ${event.cached} cached`:''} · ${event.seconds}s</p></div><time>${date(event.at)}</time></div>`).join('')||'<div class="empty-small">Your first refresh will appear here.</div>';
  renderDevices();
  if(document.activeElement!==$('refresh-interval'))$('refresh-interval').value=state.refresh_seconds;
  if(document.activeElement!==$('check-interval'))$('check-interval').value=state.check_seconds;
  $('footer-time').textContent=state.generated_at?`Last updated ${date(state.generated_at)}`:'';
}
function connectionCell(value){
  const status=value?.status||'pending',label={passed:'Passed',failed:'Failed',unsupported:'Unsupported',error:'Check error',pending:'Pending',stale:'Expired'}[status]||status;
  const color=status==='passed'?'fresh':status==='failed'?'unavailable':status==='error'?'cached':'pending';
  return `<span class="status-pill ${color}" title="${esc(value?.reason||'HTTPS request passed')}">${esc(label)}${status==='passed'?` · ${esc(value.latency_ms)} ms`:''}</span><small class="connection-time" title="${esc(value?.checked_at||'')}">${esc(value?.reason||relative(value?.checked_at))}</small>`;
}
function renderServers(){
  if(!state)return;
  const text=$('server-search').value.toLowerCase(),protocol=$('protocol-filter').value,client=$('client-filter').value,connection=$('connection-filter').value;
  const rows=state.inventory.filter(n=>(!text||`${n.name} ${n.sources.join(' ')}`.toLowerCase().includes(text))&&(!protocol||n.protocol===protocol)&&(!client||n.clients.includes(client))&&(!connection||(connection==='passing'?n.clients.length>0:n.clients.length===0)));
  $('server-total').textContent=state.total;
  $('server-rows').innerHTML=rows.slice(0,1000).map(n=>`<tr><td class="provider-name">${esc(n.name)}</td><td><span class="protocol-chip">${esc(n.protocol)}</span></td><td>${esc(n.sources.join(', '))}</td><td>${connectionCell(n.connectivity?.mihomo)}</td><td>${connectionCell(n.connectivity?.xray)}</td><td>${n.clients.map(c=>`<span class="client-badge">${esc(c==='hiddify'?'Hiddify':c==='mihomo'?'Mihomo':c==='v2board'?'V2Board':'Xray')}</span>`).join('')||'<span class="status-pill pending">Excluded</span>'}</td></tr>`).join('')||'<tr><td colspan="6" class="empty-small">No servers match these filters.</td></tr>';
  $('server-shown').textContent=`Showing ${Math.min(rows.length,1000)} of ${state.total} servers`;
}
function renderIssues(){if(!state)return;const client=$('issue-client').value,issues=state.diagnostics.filter(i=>!client||i.client===client);$('issue-total').textContent=issues.length;$('issue-rows').innerHTML=issues.slice(0,150).map(i=>`<div class="issue-row"><span class="issue-dot">!</span><div><strong>${esc(i.name)} <span class="provider-host">${esc(i.source)}</span></strong><p>${esc(i.reason)}</p></div><span class="issue-client">${esc(i.client)}</span></div>`).join('')||'<div class="empty-small">No compatibility exceptions for this selection.</div>';if(issues.length>150)$('issue-rows').insertAdjacentHTML('beforeend',`<div class="empty-small">Showing 150 of ${issues.length} notes. Download the combined archive for the full report.</div>`);}
$('server-search').addEventListener('input',renderServers);$('protocol-filter').addEventListener('change',renderServers);$('client-filter').addEventListener('change',renderServers);$('issue-client').addEventListener('change',renderIssues);
async function refresh(id){try{await api('/refresh'+(id?'?provider_id='+encodeURIComponent(id):''),{method:'POST'});toast('Refreshing subscriptions…');await loadState();}catch(error){toast(error.message);}}
$('refresh-button').addEventListener('click',()=>refresh());
$('connection-filter').addEventListener('change',renderServers);
$('check-servers').addEventListener('click',async()=>{try{await api('/check-servers',{method:'POST'});toast('Checking saved servers…');await loadState();}catch(error){toast(error.message);}});
$('check-schedule-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/settings',{method:'PUT',body:JSON.stringify({check_seconds:Number($('check-interval').value)})});await loadState();toast('Connection check schedule saved');}catch(error){toast(error.message);}});
function deviceOptions(selected=''){
  const options='<option value="" disabled>Choose a device…</option><option value="__none__">No device ID</option>'+state.hwids.map(h=>`<option value="${esc(h.id)}" ${h.configured?'':'disabled'}>${esc(h.name)} · ${esc(h.masked_value)}</option>`).join('');
  return options;
}
function fillHWIDs(selected=''){
  $('provider-hwid').innerHTML=deviceOptions();
  $('provider-hwid').value=selected===null?'__none__':selected;
}
function providerURLs(){return $('provider-url').value.split(/\n/).map(s=>s.trim()).filter(Boolean);}
function rebuildAssignments(){
  const previous=new Map([...document.querySelectorAll('.assignment-row')].map(row=>[row.dataset.url,row.querySelector('select').value]));
  const urls=providerURLs(),bulk=urls.length>1&&!$('provider-id').value;
  $('provider-hwid').hidden=bulk;$('provider-hwid').disabled=bulk;
  $('provider-assignments').hidden=!bulk;
  $('provider-assignments').innerHTML=bulk?urls.map((url,i)=>{
    let host='Check the URL';try{host=new URL(url).hostname;}catch{}
    return `<div class="assignment-row" data-url="${esc(url)}"><label for="assignment-${i}">Link ${i+1} · ${esc(host)}</label><select id="assignment-${i}" required>${deviceOptions()}</select></div>`;
  }).join(''):'';
  document.querySelectorAll('.assignment-row').forEach(row=>row.querySelector('select').value=previous.get(row.dataset.url)||'');
}
function providerRequests(){
  const id=$('provider-id').value,urls=providerURLs(),prefix=$('provider-name').value.trim();
  if(id&&urls.length>1)throw new Error('Use one subscription URL when editing a provider.');
  if(urls.length>50)throw new Error('Add at most 50 subscriptions.');
  if(!id&&!urls.length)throw new Error('Enter a subscription link.');
  if(new Set(urls).size!==urls.length)throw new Error('Remove duplicate links from this import.');
  return (urls.length?urls:['']).map((url,index)=>{
    const choice=urls.length>1?$(`assignment-${index}`).value:$('provider-hwid').value;
    if(!choice)throw new Error(`Choose a device or “No device ID” for link ${index+1}.`);
    const inferred=prefix||(url?new URL(url).hostname:providerOriginal.name);
    return {name:inferred+(urls.length>1?` ${index+1}`:''),url,hwid_id:choice==='__none__'?null:choice,user_agent:$('provider-agent').value,enabled:providerOriginal?.enabled??true};
  });
}
function needsProviderTest(request){return !providerOriginal||request.url!==''||request.hwid_id!==providerOriginal.hwid_id||request.user_agent!==providerOriginal.user_agent;}
function updateProviderButtons(){
  if(providerWorking)return;
  try{$('save-provider').disabled=providerRequests().some(body=>needsProviderTest(body)&&!testRuns.has(JSON.stringify(body)));}
  catch{$('save-provider').disabled=true;}
}
function openProvider(provider){
  providerOriginal=provider?{...provider}:null;testRuns.clear();savedRuns.clear();
  $('provider-form').reset();$('provider-error').textContent='';$('provider-test-result').hidden=true;
  $('provider-id').value=provider?.id||'';$('provider-name').value=provider?.name||'';
  $('provider-agent').value=provider?.user_agent||'Happ/2.3.1';$('provider-url').value='';$('provider-url').required=!provider;
  $('provider-url').placeholder=provider?'Leave blank to keep the saved link':'https://provider.example/subscription\nAdd multiple links, one per line';
  $('provider-url-hint').textContent=provider?'Leave blank to keep the existing subscription URL.':'Assign a device separately for each subscription link.';
  $('provider-dialog-title').textContent=provider?'Edit provider':'Add a provider';
  $('provider-assignments').innerHTML='';fillHWIDs(provider?provider.hwid_id:'');rebuildAssignments();updateProviderButtons();$('provider-dialog').showModal();
}
function providerBusy(busy){
  providerWorking=busy;
  $('provider-form').querySelectorAll('input,textarea,select,button').forEach(el=>el.disabled=busy);
  if(!busy){$('provider-hwid').disabled=!$('provider-assignments').hidden;updateProviderButtons();}
  $('test-provider').textContent=busy?'Working…':'Test fetch';
}
$('add-provider').addEventListener('click',()=>openProvider());$('empty-add').addEventListener('click',()=>openProvider());
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
$('provider-dialog').addEventListener('cancel',event=>{if(providerWorking)event.preventDefault();});
$('provider-form').addEventListener('input',event=>{
  if(event.target===$('provider-url'))rebuildAssignments();
  $('provider-test-result').hidden=true;updateProviderButtons();
});
$('provider-form').addEventListener('change',updateProviderButtons);
$('test-provider').addEventListener('click',async()=>{
  if(!$('provider-form').reportValidity())return;
  $('provider-error').textContent='';
  try{
    const requests=providerRequests();providerBusy(true);
    for(const [i,body] of requests.entries()){
      const key=JSON.stringify(body);if(savedRuns.has(key)||testRuns.has(key))continue;
      $('provider-test-result').hidden=false;$('provider-test-result').textContent=`Testing link ${i+1} of ${requests.length}…`;
      try{testRuns.set(key,await api('/providers/test',{method:'POST',body:JSON.stringify({...body,provider_id:providerOriginal?.id||null})}));}
      catch(error){throw new Error(`Link ${i+1}: ${error.message}`);}
    }
    $('provider-test-result').textContent=requests.map((body,i)=>{
      const result=testRuns.get(JSON.stringify(body));return result?`Link ${i+1}: fetch passed (${Object.entries(result.counts).map(([client,count])=>`${client}: ${count}`).join(', ')}).`:`Link ${i+1}: already saved.`;
    }).join(' ');
  }catch(error){$('provider-test-result').hidden=true;$('provider-error').textContent=error.message;}
  finally{providerBusy(false);}
});
$('provider-form').addEventListener('submit',async event=>{
  event.preventDefault();$('provider-error').textContent='';
  try{
    const requests=providerRequests(),id=$('provider-id').value;
    if(requests.some(body=>needsProviderTest(body)&&!testRuns.has(JSON.stringify(body))))throw new Error('Test the selected device and subscription settings before saving.');
    providerBusy(true);
    for(const body of requests){
      const key=JSON.stringify(body);if(savedRuns.has(key))continue;
      const tested=testRuns.get(key);
      try{await api('/providers'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify({...body,...(tested?{test_token:tested.test_token}:{})})});}
      catch(error){testRuns.delete(key);throw error;}
      savedRuns.add(key);
    }
    $('provider-dialog').close();toast(id?'Provider updated':'Subscriptions saved with tested responses');await loadState();
  }catch(error){$('provider-error').textContent=(savedRuns.size?`${savedRuns.size} subscription(s) already saved. `:'')+error.message;await loadState();}
  finally{providerBusy(false);}
});
$('provider-rows').addEventListener('click',async event=>{const button=event.target.closest('[data-action]');if(!button)return;const provider=state.providers.find(p=>p.id===button.dataset.id);if(!provider)return;try{if(button.dataset.action==='edit')openProvider(provider);if(button.dataset.action==='refresh')await refresh(provider.id);if(button.dataset.action==='toggle'){await api('/providers/'+provider.id,{method:'PUT',body:JSON.stringify({...provider,url:'',enabled:!provider.enabled})});toast(provider.enabled?'Provider paused':'Provider enabled');await loadState();if(!provider.enabled)await refresh(provider.id);}if(button.dataset.action==='delete'&&confirm(`Remove “${provider.name}” and its servers from the combined subscriptions?`)){await api('/providers/'+provider.id,{method:'DELETE'});toast('Provider removed');await loadState();}}catch(error){toast(error.message);}});
function renderDevices(){
  $('device-total').textContent=state.hwids.length;
  $('hwid-rows').innerHTML=state.hwids.map(h=>`<article class="device-card" data-device="${esc(h.id)}"><div class="device-heading">${icon('shield')}<strong>${esc(h.name)}</strong><code>${esc(h.masked_value)}</code><span class="status-pill ${h.configured?'':'unavailable'}">${h.configured?'Saved':'Missing ID'}</span></div><p>${h.providers.length?`Used by ${h.providers.length} subscription(s)`:'Not assigned to any subscriptions'}</p>${h.providers.length?`<ul class="device-usage">${h.providers.map(p=>`<li>${esc(p.name)}${p.enabled?'':' · Paused'}</li>`).join('')}</ul>`:''}<div class="device-actions"><button class="button secondary" data-device-action="rename" ${state.busy?'disabled':''}>Rename</button><button class="button secondary" data-device-action="reveal" ${h.configured?'':'disabled'}>Reveal ID</button><button class="button secondary" data-device-action="copy" ${h.configured?'':'disabled'}>Copy ID</button><button class="button secondary" data-device-action="replace" ${state.busy?'disabled':''}>Replace ID</button><button class="button secondary" data-device-action="delete" ${h.providers.length||state.busy?'disabled':''} title="${h.providers.length?'Reassign subscriptions before deleting this device':'Delete unused device'}">Delete</button></div></article>`).join('')||'<div class="empty-small">Add a device ID already authorized for your subscription, or choose “No device ID” when adding a provider that does not require one.</div>';
}
function clearReveal(){clearTimeout(revealTimer);$('device-revealed-value').value='';$('device-reveal-dialog').close();}
function clearPrivateDialogs(){
  clearReveal();document.querySelectorAll('dialog[open]').forEach(dialog=>dialog.close());
  ['hwid-value','device-replacement-value','provider-url'].forEach(id=>$(id).value='');
  testRuns.clear();savedRuns.clear();
}
document.addEventListener('visibilitychange',()=>{if(document.hidden)clearReveal();});
$('device-reveal-dialog').addEventListener('close',()=>{clearTimeout(revealTimer);$('device-revealed-value').value='';});
$('hwid-dialog').addEventListener('close',()=>$('hwid-value').value='');
$('device-replace-dialog').addEventListener('close',()=>$('device-replacement-value').value='');
$('copy-revealed-device').addEventListener('click',async()=>{try{await copy($('device-revealed-value').value);toast('Device ID copied');}catch(error){toast(error.message);}});
$('hwid-rows').addEventListener('click',async event=>{
  const button=event.target.closest('[data-device-action]');if(!button)return;
  const h=state.hwids.find(h=>h.id===button.closest('[data-device]').dataset.device);if(!h)return;
  try{
    const action=button.dataset.deviceAction;
    if(action==='reveal'||action==='copy'){
      const result=await api(`/hwids/${h.id}/reveal`,{method:'POST'});
      if(action==='copy'){await copy(result.value);toast('Device ID copied');}
      else{$('device-reveal-title').textContent=h.name;$('device-revealed-value').value=result.value;$('device-reveal-dialog').showModal();revealTimer=setTimeout(clearReveal,60000);}
    }
    if(action==='rename'){editingDevice=h.id;$('device-new-name').value=h.name;$('device-rename-error').textContent='';$('device-rename-dialog').showModal();}
    if(action==='delete'&&confirm(`Delete unused device “${h.name}”?`)){await api(`/hwids/${h.id}`,{method:'DELETE'});await loadState();toast('Unused device deleted');}
    if(action==='replace'){
      replacementDevice={id:h.id,revision:h.revision,provider_ids:h.providers.map(p=>p.id)};
      $('device-replace-form').reset();$('device-replace-error').textContent='';$('device-replace-name').textContent=`Device: ${h.name} (${h.masked_value})`;
      $('device-replace-usage').innerHTML=h.providers.map(p=>`<li>${esc(p.name)}${p.enabled?'':' · Paused'}</li>`).join('')||'<li>No subscriptions currently use this device.</li>';
      $('device-replace-dialog').showModal();
    }
  }catch(error){toast(error.message);}
});
$('device-rename-form').addEventListener('submit',async event=>{
  event.preventDefault();try{await api(`/hwids/${editingDevice}`,{method:'PATCH',body:JSON.stringify({name:$('device-new-name').value})});$('device-rename-dialog').close();await loadState();toast('Device renamed; its ID is unchanged');}catch(error){$('device-rename-error').textContent=error.message;}
});
$('device-replace-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try{await api(`/hwids/${replacementDevice.id}/replace`,{method:'POST',body:JSON.stringify({...replacementDevice,value:$('device-replacement-value').value,confirmed:$('device-replace-confirm').checked})});$('device-replace-dialog').close();await loadState();toast('Device ID replaced. Refresh affected subscriptions to verify it.');}
  catch(error){$('device-replace-error').textContent=error.message;}
  finally{button.disabled=false;}
});
function openHWID(){$('hwid-form').reset();$('hwid-error').textContent='';$('hwid-dialog').showModal();}
$('add-hwid').addEventListener('click',openHWID);$('provider-new-hwid').addEventListener('click',openHWID);
$('hwid-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try{const saved=await api('/hwids',{method:'POST',body:JSON.stringify({name:$('hwid-name').value.trim(),value:$('hwid-value').value})});$('hwid-dialog').close();await loadState();if($('provider-dialog').open){fillHWIDs(saved.id);rebuildAssignments();updateProviderButtons();}toast('Device saved locally');}
  catch(error){$('hwid-error').textContent=error.message;}
  finally{button.disabled=false;}
});
async function copy(text){if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);return;}const input=document.createElement('textarea');input.value=text;input.setAttribute('readonly','');document.body.appendChild(input);input.select();const success=document.execCommand('copy');input.remove();if(!success)throw new Error('Clipboard unavailable. Select and copy the subscription URL.');}
$('export-cards').addEventListener('click',async event=>{const button=event.target.closest('[data-copy]');if(!button)return;try{await copy(state.links[button.dataset.copy]);toast('Subscription link copied');}catch(error){toast(error.message);}});
$('rotate-links').addEventListener('click',async()=>{if(!confirm('Generate new subscription links? Existing links will stop working and must be updated in your clients.'))return;try{await api('/rotate-link',{method:'POST'});await loadState();toast('New subscription links are ready');}catch(error){toast(error.message);}});
$('schedule-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/settings',{method:'PUT',body:JSON.stringify({refresh_seconds:Number($('refresh-interval').value)})});await loadState();toast('Refresh schedule saved');}catch(error){toast(error.message);}});
$('login-form').addEventListener('submit',async event=>{event.preventDefault();$('login-error').textContent='';try{await api('/login',{method:'POST',body:JSON.stringify({token:$('admin-key').value})});$('admin-key').value='';await loadState();if(page==='activity')loadLogs();}catch(error){$('login-error').textContent=error.message;}});
$('logout-button').addEventListener('click',async()=>{await api('/logout',{method:'POST'});showLogin();});
(async()=>{try{const session=await api('/session');if(session.authenticated)await loadState();else showLogin();}catch{showLogin();$('login-error').textContent='Unable to reach the server. Please try again.';}})();
