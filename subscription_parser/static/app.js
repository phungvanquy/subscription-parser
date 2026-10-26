"use strict";
const $ = id => document.getElementById(id);
const paths = {
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  more:'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  chevron:'<path d="m9 5 7 7-7 7"/>',
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
let logRequest=0,selectedSubscription='default',subscriptionWorking=false;
let subscriptionPage=0,subscriptionListScroll=0;
const subscriptionCollator=new Intl.Collator(undefined,{numeric:true,sensitivity:'base'});
let securityAction='setup',securityWorking=false,securityRequest=0;
let providerOriginal=null,providerWorking=false,testRuns=new Map(),savedRuns=new Set(),editingDevice=null,replacementDevice=null,revealTimer;
let providerPage=0;
let serverDetail=null,serverDetailId=null,serverDetailOrigin=null,serverDetailRequest=0,serverDetailIds=[];
const expandedProviders=new Set();
const titles={
  overview:['YOUR WORKSPACE','Overview','Your providers, connection checks and subscriptions at a glance.','Overview'],
  providers:['SUBSCRIPTION SOURCES','Providers','Manage your sources and see what’s working.','Providers'],
  servers:['CONNECTION CHECKS','Server library','See which servers pass, which fail and the formats they support.','Server library'],
  exports:['YOUR SUBSCRIPTIONS','Subscriptions','Choose providers. Copy a link. Keep your clients up to date.','Subscription links'],
  activity:['WORKSPACE HISTORY','Activity & logs','Recent refreshes, connection checks and debug messages.','Activity & logs'],
  devices:['PROVIDER ACCESS','Devices','Manage saved device IDs and see which providers use them.','Devices'],
  settings:['WORKSPACE SETTINGS','Settings','Manage account security, provider refreshes and connection checks.','Settings']
};
function setHTML(id,html){const el=$(id);if(el.innerHTML!==html)el.innerHTML=html;}

async function api(path,options={}){const response=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});let data;try{data=await response.json();}catch{throw new Error('The server returned an unexpected response.');}if(!response.ok){if(response.status===401&&path!='/login')showLogin();throw new Error(data.detail||'Request failed');}return data;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,4000);}
function relative(value){if(!value)return 'Never';const seconds=Math.max(0,(Date.now()-new Date(value).getTime())/1000);if(seconds<60)return 'Just now';if(seconds<3600)return `${Math.floor(seconds/60)}m ago`;if(seconds<86400)return `${Math.floor(seconds/3600)}h ago`;return new Date(value).toLocaleDateString();}
function date(value){return value?new Date(value).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';}
function interval(value){return value===3600?'Hourly':value>=3600?`${value/3600} hours`:`${value/60} min`;}
function showLogin(){
  clearPrivateDialogs();clearTimeout(pollTimer);logRequest++;securityRequest++;
  $('log-output').textContent='';$('refresh-logs').disabled=false;
  $('workspace').hidden=true;$('login-view').hidden=false;
  $('login-factor').hidden=true;$('login-code').disabled=true;$('login-code').required=false;
  $('login-code').value='';$('admin-key').value='';$('login-error').textContent='';
  $('login-submit-label').textContent='Open workspace';state=null;
  providerPage=0;expandedProviders.clear();$('provider-search').value='';$('provider-status-filter').value='';
  selectedSubscription='default';subscriptionPage=0;subscriptionListScroll=0;
  $('subscription-search').value='';$('subscription-status-filter').value='';$('subscription-sort').value='name';$('subscription-page-size').value='25';
}
function selectPage(name){
  page=name;
  document.querySelectorAll('.provider-actions[open]').forEach(menu=>menu.open=false);
  document.querySelectorAll('.nav-item').forEach(b=>{b.classList.toggle('active',b.dataset.page===name);if(b.dataset.page===name)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
  const t=titles[name];$('page-eyebrow').textContent=t[0];$('page-title').textContent=t[1];$('page-description').textContent=t[2];$('breadcrumb-page').textContent=t[3];
  $('overview-stats').hidden=name!=='overview';
  $('add-provider').hidden=!['overview','providers'].includes(name);
  $('add-subscription').hidden=name!=='exports';
  $('refresh-button').hidden=!['overview','providers','servers'].includes(name);
  const visible={overview:['providers','subscription-overview'],providers:['providers'],servers:['servers','diagnostics'],exports:['exports'],activity:['logs','activity'],devices:['devices'],settings:['settings']}[name];
  document.querySelectorAll('.page-section').forEach(s=>s.hidden=!visible.includes(s.id.replace(/-section$/,'')));
  $('subscriptions-layout').classList.remove('show-detail');$('subscription-menu').open=false;
  window.scrollTo({top:0,behavior:'instant'});
  if(name==='activity')loadLogs();
  if(name==='settings')loadSecurity();
}
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
async function loadState(conditional=false){clearTimeout(pollTimer);try{const firstLoad=!state;const update=await api('/state'+(conditional&&state?.revision?'?revision='+encodeURIComponent(state.revision):''));state=update.unchanged?{...state,...update}:update;$('workspace').hidden=false;$('login-view').hidden=true;render(!update.unchanged);if(firstLoad){selectPage(page);if(page!=='settings')loadSecurity();}pollTimer=setTimeout(()=>loadState(true),state.busy||state.check_queued?1500:15000);}catch(error){if(state){$('global-error').textContent=error.message;$('global-error').hidden=false;pollTimer=setTimeout(()=>loadState(true),10000);}}}
function renderProgress(){
  const downloading=state.refresh_running,checking=state.check_running;
  const refreshProvider=state.providers.find(p=>p.id===state.refresh_provider_id);
  const {done:downloaded=0,total:downloadTotal=0}=state.refresh_progress||{};
  const downloadTitle=`${state.refresh_scheduled?'Scheduled refresh ·':'Refreshing'} ${refreshProvider?.name||'all providers'}`;
  const downloadDetail=downloadTotal?`${downloaded} of ${downloadTotal} providers downloaded. Selected servers are queued for connection checks.`:'Preparing provider downloads…';
  $('refresh-progress').hidden=!(downloading&&checking);
  if(downloading&&checking){
    const percent=downloadTotal?Math.floor(downloaded/downloadTotal*100):0;
    $('refresh-progress-title').textContent=downloadTitle;
    $('refresh-progress-detail').textContent=downloadDetail;
    $('refresh-progress-percent').textContent=`${percent}%`;
    $('refresh-progress-bar').setAttribute('aria-valuenow',percent);
    $('refresh-progress-fill').style.width=`${percent}%`;
  }
  $('job-progress').hidden=!state.busy;
  if(!state.busy)return '';
  const provider=state.providers.find(p=>p.id===(checking?(state.check_provider_ids?.length===1?state.check_provider_ids[0]:null):state.busy_provider_id));
  let title='Updating subscriptions',detail='Saving results and rebuilding subscription links…',percent=null;
  if(downloading&&!checking){
    title=downloadTitle;detail=downloadDetail;
  }else if(checking&&state.check_phase!=='updating'){
    title=`Checking connections${provider?` · ${provider.name}`:''}`;
    const {done=0,total=0}=state.check_progress||{};
    detail=total?`${done} of ${total} client checks processed. A server can have checks for multiple clients.`:'Preparing saved servers for connection checks…';
    if(total>0)percent=Math.min(100,Math.max(0,Math.floor(done/total*100)));
  }else if(state.busy_action==='testing'){
    title=`Testing provider fetch${provider?` · ${provider.name}`:''}`;
    detail='Downloading a preview and checking compatible formats…';
  }
  if($('job-progress-title').textContent!==title)$('job-progress-title').textContent=title;
  $('job-progress-detail').textContent=detail;
  $('job-progress-percent').textContent=percent===null?'In progress':`${percent}%`;
  $('job-progress-bar').classList.toggle('indeterminate',percent===null);
  if(percent===null)$('job-progress-bar').removeAttribute('aria-valuenow');
  else $('job-progress-bar').setAttribute('aria-valuenow',percent);
  $('job-progress-fill').style.width=percent===null?'':`${percent}%`;
  return `${title} · ${detail}`;
}
function render(inventoryChanged=true){
  const providers=state.providers,inventory=state.inventory||[],types=[...new Set(inventory.map(n=>n.protocol))].sort();
  $('nav-provider-count').textContent=providers.length;$('provider-total').textContent=providers.length;
  $('stat-providers').textContent=providers.filter(p=>p.enabled).length;$('stat-provider-detail').textContent=`${providers.length} providers in your workspace`;
  $('stat-servers').textContent=state.published_total||0;$('stat-duplicate-detail').textContent=`${state.total} nodes in library · ${state.duplicates_removed||0} duplicates combined`;
  $('stat-protocols').textContent=types.length;$('stat-protocol-detail').textContent=types.map(p=>p.toUpperCase()).join(' · ')||'Original protocols preserved';
  const working=renderProgress();
  const downloadingAll=state.refresh_running&&!state.refresh_provider_id;
  const exclusive=state.busy&&!state.refresh_running&&!state.check_running;
  $('stat-interval').textContent=interval(state.refresh_seconds);$('stat-next').textContent=downloadingAll?'Downloading providers…':`Next download at ${new Date(state.next_refresh*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;
  $('refresh-button').disabled=state.refresh_running||exclusive;$('refresh-button').classList.toggle('spinning',downloadingAll);document.querySelector('.refresh-label').textContent=downloadingAll?'Refreshing…':'Refresh all';
  $('check-servers').disabled=state.check_running||exclusive||!(state.total||providers.some(p=>p.count));
  $('connection-summary').textContent=state.busy?working:`${state.published_total||0} of ${state.total} nodes included · ${Math.max(0,state.total-(state.published_total||0))} excluded · Last checked ${relative(state.last_check).toLowerCase()} · Next check at ${new Date(state.next_check*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})} · Scheduled every ${interval(state.check_seconds).toLowerCase()}. Checks use saved subscriptions; HWIDs are not sent.`;
  $('last-refresh').textContent=`Providers downloaded: ${relative(state.last_download)}`;
  $('last-refresh').title=date(state.last_download);
  $('last-connection-check').textContent=`Connections checked: ${relative(state.last_check)}`;
  $('last-connection-check').title=date(state.last_check);
  $('global-error').hidden=!state.last_error;if(state.last_error)$('global-error').textContent=state.last_error;
  renderProviders();
  $('providers-empty').hidden=providers.length>0;
  renderSubscriptions();
  const selectedProvider=$('provider-filter').value;$('provider-filter').innerHTML='<option value="">All providers</option>'+providers.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');if(providers.some(p=>p.id===selectedProvider))$('provider-filter').value=selectedProvider;
  const selected=$('protocol-filter').value;$('protocol-filter').innerHTML='<option value="">All protocols</option>'+types.map(t=>`<option value="${esc(t)}">${esc(t.toUpperCase())}</option>`).join('');if(types.includes(selected))$('protocol-filter').value=selected;
  if(inventoryChanged){renderServers();renderIssues();}
  else document.querySelectorAll('.connection-time[data-checked-at]').forEach(el=>{el.textContent=relative(el.dataset.checkedAt);});
  $('activity-rows').innerHTML=(state.history||[]).map(event=>`<div class="activity-row">${icon(event.action==='refresh'?'refresh':'network')}<div><strong>${esc(event.action==='refresh'?(event.provider_id?`${providers.find(p=>p.id===event.provider_id)?.name||'Provider'} refreshed`:event.scheduled?'Scheduled provider refresh completed':'Subscriptions refreshed'):event.action==='connection check'?'Connections checked':'Subscription configuration updated')}</strong><p>${event.action==='refresh'?`${event.fresh} sources up to date${event.cached?` · ${event.cached} cached`:''} · Selected servers queued for checks`:`${event.published??event.servers} included / ${event.servers} nodes${event.checked?` · ${event.checked} client checks`:''}`} · ${event.seconds}s</p></div><time>${date(event.at)}</time></div>`).join('')||'<div class="empty-small">Your first refresh will appear here.</div>';
  renderDevices();
  if(document.activeElement!==$('refresh-interval'))$('refresh-interval').value=state.refresh_seconds;
  if(document.activeElement!==$('check-interval'))$('check-interval').value=state.check_seconds;
  $('footer-time').textContent=state.generated_at?`Feeds rebuilt ${date(state.generated_at)}`:'';
}
function renderProviders(){
  if(!state)return;
  const providerServers=new Map(state.providers.map(p=>[p.name,[]]));
  (state.inventory||[]).forEach(n=>n.sources.forEach(source=>providerServers.get(source)?.push(n)));
  const query=$('provider-search').value.trim().toLocaleLowerCase(),filter=$('provider-status-filter').value;
  const matches=state.providers.map(p=>{
    const servers=providerServers.get(p.name),working=servers.filter(n=>n.clients.length>0).length;
    const device=p.hwid_id===null?'No device ID':state.hwids.find(h=>h.id===p.hwid_id)?.name||'Missing device';
    return {p,servers,working,device};
  }).filter(({p,working,device})=>`${p.name} ${p.host} ${device}`.toLocaleLowerCase().includes(query)&&(
    !filter||filter==='enabled'&&p.enabled||filter==='paused'&&!p.enabled||filter==='passing'&&p.enabled&&working>0||
    filter==='attention'&&p.enabled&&(p.error||['cached','unavailable'].includes(p.status)||!working)));
  const size=Number($('provider-page-size').value),pages=Math.max(1,Math.ceil(matches.length/size));
  providerPage=Math.max(0,Math.min(providerPage,pages-1));
  const start=providerPage*size,visible=matches.slice(start,start+size);
  for(const id of expandedProviders)if(!state.providers.some(p=>p.id===id))expandedProviders.delete(id);
  const rows=$('provider-rows'),expanded=new Set([...rows.querySelectorAll('details[open]')].map(el=>el.id));
  const focused=rows.contains(document.activeElement)?document.activeElement.id:null;
  setHTML('provider-rows',visible.map(({p,servers,working,device})=>{
    const status={fresh:'Up to date',cached:'Cached',unavailable:'Unavailable',disabled:'Paused',pending:'Pending'}[p.status]||p.status;
    const menuId=`provider-menu-${p.id}`,detailsId=`provider-clients-${p.id}`,open=expandedProviders.has(p.id);
    const note=providerCheckNote(p,servers,working),total=p.selection_pending&&servers.length?servers.length:p.count||0;
    return `<tr class="provider-row ${open?'is-expanded':''}" data-provider="${esc(p.id)}">
      <td class="provider-identity"><div class="provider-cell"><button id="provider-breakdown-${esc(p.id)}" class="icon-button provider-expand" data-provider-details="${esc(p.id)}" aria-expanded="${open}" aria-controls="${esc(detailsId)}" aria-label="Details for ${esc(p.name)}" title="Provider details">${icon('chevron')}</button><div class="provider-identity-copy"><span class="provider-name" title="${esc(p.name)}">${esc(p.name)}</span><span class="provider-host" title="${esc(p.host)}">${esc(p.host)}</span></div></div></td>
      <td class="provider-servers"><div class="provider-server-summary"><button id="provider-working-${esc(p.id)}" class="provider-working ${working?'has-working':''}" data-provider-servers="${esc(p.id)}" aria-label="View ${working} working servers from ${esc(p.name)}"><strong>${working}</strong> working</button><span class="provider-server-total">/ <span class="server-count">${total}</span> <span class="provider-total-kind">${p.selection_pending&&servers.length?'active':p.server_limit?'selected':'total'}</span></span></div></td>
      <td class="provider-health"><span class="provider-check-note ${!note&&working?'is-ready':''}" title="${esc(note||(working?'Ready':'No servers'))}">${esc(note||(working?'Ready':'No servers'))}</span>${p.error?`<span class="provider-fetch-warning" title="${esc(p.error)}">${esc(status)} · fetch issue</span>`:''}</td>
      <td class="provider-fetch"><span class="status-pill ${esc(p.status)}" title="${esc(p.error||'')}">${esc(status)}</span><span class="provider-fetch-time" title="${esc(date(p.fetched_at))}">${relative(p.fetched_at)}</span></td>
      <td class="provider-actions-cell"><details id="${esc(menuId)}" class="action-menu provider-actions" ${expanded.has(menuId)?'open':''}><summary id="provider-actions-${esc(p.id)}" aria-label="Actions for ${esc(p.name)}" title="Provider actions">${icon('more')}</summary><div class="action-menu-items">
        <button id="provider-refresh-${esc(p.id)}" data-action="refresh" data-id="${esc(p.id)}" ${state.refresh_running||(state.busy&&!state.check_running)||!p.enabled?'disabled':''}>${icon('refresh')}Refresh provider</button>
        <button id="provider-edit-${esc(p.id)}" data-action="edit" data-id="${esc(p.id)}">${icon('edit')}Edit provider</button>
        <button id="provider-toggle-${esc(p.id)}" data-action="toggle" data-id="${esc(p.id)}" ${state.busy?'disabled':''}>${icon(p.enabled?'pause':'play')}${p.enabled?'Pause':'Enable'} provider</button>
        <button id="provider-delete-${esc(p.id)}" class="danger-text" data-action="delete" data-id="${esc(p.id)}" ${state.busy?'disabled':''}>${icon('trash')}Remove provider</button>
      </div></details></td>
    </tr><tr id="${esc(detailsId)}" class="provider-detail-row" ${open?'':'hidden'}><td colspan="5"><div class="provider-detail-content">
      <div class="provider-detail-heading"><strong>${esc(p.name)}</strong><span>${esc(p.host)}</span></div>
      <div class="provider-detail-grid"><div><span class="provider-detail-label">Selection</span><span class="provider-sample-note">${p.fetched_count??p.count??0} fetched · ${p.count||0} selected · ${p.server_limit?`random limit ${p.server_limit}`:'all servers'}</span>${p.selection_pending?`<span class="provider-sample-note">${p.count||0} selected for next checks${servers.length?' · previous selection stays active':''}</span>`:''}</div>
      <div><span class="provider-detail-label">Device</span><span class="device-label">${esc(device)}</span></div>
      <div><span class="provider-detail-label">Last fetch</span><span>${esc(status)} · ${esc(date(p.fetched_at))}</span>${p.error?`<span class="provider-fetch-error">${esc(p.error)}</span>`:''}</div>
      <div><span class="provider-detail-label">Connections</span><span>${esc(note||(working?'Ready':'No servers'))}</span></div></div>
      <span class="provider-detail-label">Working servers by client</span>${providerClientCounts(servers)}
    </div></td></tr>`;
  }).join(''));
  $('provider-toolbar').hidden=!state.providers.length;
  $('provider-table-wrap').hidden=!matches.length;
  $('providers-no-results').hidden=!state.providers.length||!!matches.length;
  $('provider-list-footer').hidden=!state.providers.length;
  const range=matches.length?`${start+1}–${start+visible.length} of ${matches.length}`:'0';
  $('provider-footer').textContent=`${range} providers${query||filter?` · ${state.providers.length} total`:''}`;
  $('provider-page-label').textContent=`${providerPage+1} / ${pages}`;
  $('provider-previous').disabled=providerPage===0;$('provider-next').disabled=providerPage>=pages-1;
  if(focused)$(focused)?.focus({preventScroll:true});
}
function providerCheckNote(provider,servers,working){
  const statuses=servers.flatMap(n=>Object.values(n.connectivity||{}).map(value=>value.status));
  const fetching=state.refresh_running&&(state.refresh_provider_ids?.includes(provider.id)??(!state.refresh_provider_id||state.refresh_provider_id===provider.id));
  const checking=state.check_running&&(!state.check_provider_ids||state.check_provider_ids.includes(provider.id));
  const refreshNote=state.refresh_scheduled?'Scheduled refresh':'Refreshing';
  return !provider.enabled?'Paused':fetching?`${refreshNote}${checking?' and checking':''}…`:checking?'Checking…':provider.selection_pending?'New selection awaiting checks':statuses.includes('stale')?'Checks expired':statuses.includes('pending')?'Awaiting checks':!working&&provider.count?'No passing checks':'';
}
function providerClientCounts(servers){
  const clients=[['hiddify','Hiddify'],['mihomo','Mihomo'],['xray','Xray'],['v2board','V2Board JSON'],['v2board_uri','V2Board URI']];
  return `<div class="provider-client-counts" role="group" aria-label="Working servers by client">${clients.map(([client,label])=>`<span>${label}<strong>${servers.filter(n=>n.clients.includes(client)).length}</strong></span>`).join('')}</div>`;
}
$('provider-search').addEventListener('input',()=>{providerPage=0;renderProviders();});
$('provider-status-filter').addEventListener('change',()=>{providerPage=0;renderProviders();});
$('provider-page-size').addEventListener('change',()=>{providerPage=0;renderProviders();});
$('clear-provider-filters').addEventListener('click',()=>{$('provider-search').value='';$('provider-status-filter').value='';providerPage=0;renderProviders();$('provider-search').focus();});
function changeProviderPage(delta){providerPage+=delta;renderProviders();$('provider-toolbar').scrollIntoView({block:'start'});}
$('provider-previous').addEventListener('click',()=>changeProviderPage(-1));
$('provider-next').addEventListener('click',()=>changeProviderPage(1));
$('provider-rows').addEventListener('click',event=>{
  const details=event.target.closest('[data-provider-details]');
  if(details){
    const id=details.dataset.providerDetails;
    if(expandedProviders.has(id))expandedProviders.delete(id);else expandedProviders.add(id);
    renderProviders();return;
  }
  const button=event.target.closest('[data-provider-servers]');if(!button)return;
  $('provider-filter').value=button.dataset.providerServers;
  $('server-search').value='';$('protocol-filter').value='';$('client-filter').value='';$('connection-filter').value='passing';
  selectPage('servers');renderServers();$('provider-filter').focus();
});
document.addEventListener('click',event=>{
  document.querySelectorAll('.provider-actions[open]').forEach(menu=>{if(!menu.contains(event.target))menu.open=false;});
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape')document.querySelectorAll('.provider-actions[open]').forEach(menu=>{menu.open=false;menu.querySelector('summary').focus();});
});
$('provider-rows').addEventListener('focusout',event=>{
  const menu=event.target.closest('.provider-actions');if(menu&&!menu.contains(event.relatedTarget))menu.open=false;
});
function currentSubscription(){return state?.subscriptions.find(s=>s.id===selectedSubscription);}
function subscriptionProviders(subscription){return state.providers.filter(p=>subscription.all_providers||subscription.provider_ids.includes(p.id));}
function subscriptionStatus(subscription,providers=subscriptionProviders(subscription)){
  if(!providers.some(p=>p.enabled))return {label:'No active providers',tone:'paused'};
  if(!subscription.ready)return {label:'Checks pending',tone:'pending'};
  if(!subscription.published_total)return {label:'No passing servers',tone:'empty'};
  return {label:'Ready to import',tone:'ready'};
}
function providerSummary(subscription){
  if(subscription.all_providers)return 'All providers';
  const providers=subscriptionProviders(subscription);
  return providers.length?providers.map(p=>p.name).join(', '):'No providers selected';
}
function subscriptionProviderTag(name,tone){
  const status=tone==='active'?'Active':tone==='paused'?'Paused':'No providers available';
  return `<span class="subscription-provider-tag ${tone}" title="${esc(`${name} · ${status}`)}">${tone==='paused'?icon('pause'):`<span class="subscription-dot ${tone==='active'?'ready':'paused'}" aria-hidden="true"></span>`}<span class="subscription-provider-tag-name">${esc(name)}</span><span class="sr-only"> · ${status}</span></span>`;
}
function subscriptionMatches(){
  const query=$('subscription-search').value.trim().toLocaleLowerCase(),filter=$('subscription-status-filter').value,sort=$('subscription-sort').value;
  const byId=new Map(state.providers.map(p=>[p.id,p]));
  return state.subscriptions.map(subscription=>{
    const providers=subscription.all_providers?state.providers:subscription.provider_ids.map(id=>byId.get(id)).filter(Boolean);
    return {subscription,providers,status:subscriptionStatus(subscription,providers)};
  }).filter(({subscription:s,providers,status})=>(!filter||status.tone===filter)&&(!query||`${s.name} ${s.all_providers?'All providers':''} ${providers.map(p=>p.name).join(' ')}`.toLocaleLowerCase().includes(query)))
    .sort(({subscription:a},{subscription:b})=>{
      const name=subscriptionCollator.compare(a.name,b.name)||subscriptionCollator.compare(a.id,b.id);
      if(sort==='servers')return b.published_total-a.published_total||name;
      if(sort==='updated')return (Date.parse(b.generated_at)||0)-(Date.parse(a.generated_at)||0)||name;
      return sort==='name-desc'?-name:name;
    });
}
function renderSubscriptionList(){
  if(!state)return;
  const matches=subscriptionMatches(),size=Number($('subscription-page-size').value),pages=Math.max(1,Math.ceil(matches.length/size));
  subscriptionPage=Math.max(0,Math.min(subscriptionPage,pages-1));
  const start=subscriptionPage*size,visible=matches.slice(start,start+size);
  const focused=document.activeElement?.closest('#subscription-list button'),focusAttribute=focused?.hasAttribute('data-subscription-id')?'data-subscription-id':'data-open-subscription',focusedId=focused?.getAttribute(focusAttribute);
  setHTML('subscription-list',visible.map(({subscription:s,providers,status})=>{
    const active=providers.filter(p=>p.enabled).length;
    const all=s.all_providers||(providers.length>0&&providers.length===state.providers.length),paused=providers.length-active;
    const tags=all?subscriptionProviderTag('All providers',active?'active':providers.length?'paused':'empty'):providers.map(p=>subscriptionProviderTag(p.name,p.enabled?'active':'paused')).join('');
    const summary=all?`<small>${providers.length?`${active} active${paused?` · ${paused} paused`:''}`:'No providers available'}</small>`:'';
    return `<tr class="subscription-row ${s.id===selectedSubscription?'selected':''}"><td class="subscription-name-cell"><button class="subscription-open" data-subscription-id="${esc(s.id)}" ${s.id===selectedSubscription?'aria-current="true"':''} title="${esc(s.name)}"><span>${esc(s.name)}</span>${s.id==='default'?'<span class="original-badge">Original</span>':''}</button></td><td class="subscription-provider-cell"><div class="subscription-provider-tags">${tags||'<span class="subscription-provider-empty">No providers selected</span>'}</div>${summary}</td><td class="subscription-status-cell"><span class="subscription-status ${status.tone}">${status.label}</span></td><td class="subscription-server-cell"><strong>${s.published_total} <span>included</span></strong><small>${s.total} total</small></td><td class="subscription-link-cell"><button class="text-button subscription-view" data-open-subscription="${esc(s.id)}" aria-label="View links for ${esc(s.name)}"><span>View links</span>${icon('chevron')}</button></td></tr>`;
  }).join(''));
  $('subscriptions-no-results').hidden=matches.length>0;
  $('subscription-table-wrap').hidden=!matches.length;
  $('subscription-search-summary').textContent=(matches.length?`${start+1}–${start+visible.length} of ${matches.length} subscriptions`:'0 subscriptions')+(matches.length!==state.subscriptions.length?` · ${state.subscriptions.length} total`:'');
  $('subscription-page-label').textContent=`${subscriptionPage+1} / ${pages}`;
  $('subscription-page-label').setAttribute('aria-label',`Page ${subscriptionPage+1} of ${pages}`);
  $('subscription-previous').disabled=subscriptionPage===0;$('subscription-next').disabled=subscriptionPage>=pages-1;
  if(focusedId)(document.querySelector(`#subscription-list [${focusAttribute}="${CSS.escape(focusedId)}"]`)||document.querySelector('#subscription-list .subscription-open')||$('subscription-search')).focus({preventScroll:true});
}
function renderSubscriptions(){
  const removed=!currentSubscription();
  if(removed)selectedSubscription='default';
  const subscription=currentSubscription(),providers=subscriptionProviders(subscription),active=providers.filter(p=>p.enabled).length,status=subscriptionStatus(subscription);
  $('subscription-total').textContent=state.subscriptions.length;
  $('overview-subscription-total').textContent=state.subscriptions.length;
  setHTML('subscription-overview',state.subscriptions.slice(0,3).map(s=>`<button class="subscription-preview" data-open-subscription="${esc(s.id)}"><span class="preview-icon">${icon('link')}</span><span class="preview-copy"><strong>${esc(s.name)}</strong><small>${esc(providerSummary(s))}</small></span><span class="preview-count"><strong>${s.published_total}</strong><small>included</small></span><span aria-hidden="true">→</span></button>`).join(''));
  renderSubscriptionList();
  $('subscription-detail-name').textContent=subscription.name;
  $('subscription-updated').textContent=subscription.generated_at?`Updated ${relative(subscription.generated_at).toLowerCase()}`:'Not generated yet';
  $('subscription-status').textContent=status.label;$('subscription-status').className='subscription-status '+status.tone;
  $('subscription-summary').textContent=`${subscription.published_total} of ${subscription.total} servers included`;
  $('subscription-published').textContent=subscription.published_total;
  $('subscription-excluded').textContent=Math.max(0,subscription.total-subscription.published_total);
  $('subscription-active-providers').textContent=active;
  $('subscription-provider-metric').textContent=`Active of ${providers.length} provider${providers.length===1?'':'s'}`;
  $('subscription-scope').textContent=subscription.all_providers?'New providers join automatically':'Selected providers only';
  setHTML('subscription-providers-summary',providers.map(p=>`<span class="provider-chip ${p.enabled?'':'paused'}"><span class="subscription-dot ${p.enabled?'ready':'paused'}"></span><span>${esc(p.name)}</span>${p.enabled?'':'<small>Paused</small>'}</span>`).join('')||'<p class="no-sources">No providers selected. Use Edit to add a source.</p>');
  $('add-subscription').disabled=state.busy;$('edit-subscription').disabled=state.busy;
  $('customize-subscription').disabled=state.busy;
  $('subscription-server-summary').textContent=subscription.server_policy?.all_servers===false||subscription.server_policy?.all_servers===0
    ?'Only your saved, selected servers are included when their checks pass.'
    :'All working servers from the selected providers, with any saved overrides.';
  $('delete-subscription').disabled=state.busy||subscription.id==='default';
  $('original-subscription-note').hidden=subscription.id!=='default';
  $('subscription-empty-notice').hidden=!!subscription.published_total;
  const formats=[['hiddify.json','Hiddify','hiddify','h','JSON · sing-box'],['mihomo.yaml','Mihomo','mihomo','m','YAML · Clash compatible'],['xray.json','Xray','xray','x','JSON · native outbounds'],['v2board.json','V2Board','v2board','v','JSON · External Subscriptions'],['v2board.txt','V2Board URI','v2board_uri','v','Legacy share-link format'],['combined.json','Combined archive','combined','{ }','JSON · all formats & diagnostics']];
  setHTML('export-cards',formats.map(([file,name,key,mark,format])=>{
    const count=key==='combined'?subscription.published_total:(subscription.counts[key]||0),available=subscription.ready&&(key==='combined'||count>0);
    return `<article class="export-card format-row ${key==='combined'?'archive-row':''}"><div class="format-main"><span class="client-logo ${key}" aria-hidden="true">${mark}</span><div class="format-description"><h4 class="export-name">${name}</h4><p class="export-format">${format}</p></div><span class="export-count ${count?'':'empty'}">${count} servers</span><div class="export-actions"><button class="button secondary" data-copy="${file}" aria-label="Copy ${name} subscription link">${icon('copy')}<span>Copy link</span></button>${available?`<a class="icon-button download-link" href="${esc(subscription.links[file])}" download="${file}" aria-label="Download ${name}" title="Download ${name}">${icon('download')}</a>`:`<button class="icon-button download-link" disabled aria-label="Download ${name} unavailable" title="No current servers to download">${icon('download')}</button>`}</div></div><label class="export-url" ${$('show-subscription-urls').checked?'':'hidden'}><span class="sr-only">${name} subscription URL</span><input readonly value="${esc(subscription.links[file])}" spellcheck="false"></label></article>`;
  }).join(''));
  if(removed&&$('subscriptions-layout').classList.contains('show-detail'))showSubscriptionList();
}
function showSubscription(identifier,focus=true){
  if(!$('subscriptions-layout').classList.contains('show-detail'))subscriptionListScroll=window.scrollY;
  selectedSubscription=identifier;$('subscription-menu').open=false;renderSubscriptions();
  $('subscriptions-layout').classList.add('show-detail');
  $('exports-section').scrollIntoView({block:'start'});if(focus)$('subscription-detail-name').focus({preventScroll:true});
}
function showSubscriptionList(){
  $('subscriptions-layout').classList.remove('show-detail');$('subscription-menu').open=false;
  window.scrollTo({top:subscriptionListScroll,behavior:'instant'});
  (document.querySelector(`#subscription-list [data-subscription-id="${CSS.escape(selectedSubscription)}"]`)||document.querySelector('#subscription-list .subscription-open')||$('subscription-search')).focus({preventScroll:true});
}
$('subscription-list').addEventListener('click',event=>{const button=event.target.closest('[data-subscription-id],[data-open-subscription]');if(button)showSubscription(button.dataset.subscriptionId||button.dataset.openSubscription);});
$('subscription-overview').addEventListener('click',event=>{const button=event.target.closest('[data-open-subscription]');if(button){selectPage('exports');showSubscription(button.dataset.openSubscription);}});
$('subscription-search').addEventListener('input',()=>{subscriptionPage=0;renderSubscriptionList();});
['subscription-status-filter','subscription-sort','subscription-page-size'].forEach(id=>$(id).addEventListener('change',()=>{subscriptionPage=0;renderSubscriptionList();}));
$('clear-subscription-search').addEventListener('click',()=>{$('subscription-search').value='';$('subscription-status-filter').value='';subscriptionPage=0;renderSubscriptionList();$('subscription-search').focus();});
function turnSubscriptionPage(delta){subscriptionPage+=delta;renderSubscriptionList();$('subscription-toolbar').scrollIntoView({block:'start'});}
$('subscription-previous').addEventListener('click',()=>turnSubscriptionPage(-1));
$('subscription-next').addEventListener('click',()=>turnSubscriptionPage(1));
$('back-to-subscriptions').addEventListener('click',showSubscriptionList);
$('show-subscription-urls').addEventListener('change',()=>document.querySelectorAll('.export-url').forEach(el=>el.hidden=!$('show-subscription-urls').checked));
$('export-cards').addEventListener('click',event=>{if(event.target.matches('.export-url input'))event.target.select();});
document.addEventListener('click',event=>{if(!event.target.closest('#subscription-menu'))$('subscription-menu').open=false;});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&$('subscription-menu').open){$('subscription-menu').open=false;$('subscription-menu').querySelector('summary').focus();}});
function updateProviderSelection(){
  const all=$('subscription-all').checked,query=$('subscription-provider-search').value.trim().toLocaleLowerCase();
  $('subscription-provider-options').disabled=all;$('subscription-provider-options').hidden=all;
  const choices=[...$('subscription-provider-choices').querySelectorAll('input')];
  choices.forEach(input=>{input.closest('label').hidden=!input.closest('label').textContent.toLocaleLowerCase().includes(query);});
  $('provider-selection-no-results').hidden=!choices.length||choices.some(input=>!input.closest('label').hidden);
  const chosen=all?state.providers:state.providers.filter(p=>choices.some(input=>input.checked&&input.value===p.id));
  $('subscription-selection-summary').textContent=`${all?'All ':''}${chosen.length} provider${chosen.length===1?'':'s'} selected · ${chosen.filter(p=>p.enabled).length} active`;
}
function openSubscription(subscription){
  $('subscription-menu').open=false;$('subscription-form').reset();$('subscription-error').textContent='';
  $('subscription-id').value=subscription?.id||'';$('subscription-name').value=subscription?.name||'';$('subscription-all').checked=subscription?.all_providers||false;
  $('subscription-dialog-title').textContent=subscription?'Edit subscription':'New subscription';
  $('subscription-provider-choices').innerHTML=state.providers.map(p=>`<label class="check-label provider-choice"><input type="checkbox" value="${esc(p.id)}" ${subscription?.provider_ids.includes(p.id)?'checked':''}><span><strong>${esc(p.name)}</strong><small>${p.count||0} servers</small></span><span class="provider-choice-status ${p.enabled?'':'paused'}">${p.enabled?'Active':'Paused'}</span></label>`).join('')||'<p class="hint">Add a provider first, or use all providers to include sources you add later.</p>';
  updateProviderSelection();$('subscription-dialog').showModal();$('subscription-name').focus();
}
$('add-subscription').addEventListener('click',()=>openSubscription());
$('edit-subscription').addEventListener('click',()=>openSubscription(currentSubscription()));
$('subscription-all').addEventListener('change',updateProviderSelection);
$('subscription-provider-search').addEventListener('input',updateProviderSelection);
$('subscription-provider-choices').addEventListener('change',updateProviderSelection);
$('select-active-providers').addEventListener('click',()=>{$('subscription-provider-choices').querySelectorAll('input').forEach(el=>el.checked=state.providers.some(p=>p.id===el.value&&p.enabled));updateProviderSelection();});
$('clear-selected-providers').addEventListener('click',()=>{$('subscription-provider-choices').querySelectorAll('input').forEach(el=>el.checked=false);updateProviderSelection();});
$('subscription-dialog').addEventListener('cancel',event=>{if(subscriptionWorking)event.preventDefault();});
$('subscription-form').addEventListener('submit',async event=>{
  event.preventDefault();$('subscription-error').textContent='';
  const all=$('subscription-all').checked,providers=all?[]:[...$('subscription-provider-choices').querySelectorAll('input:checked')].map(el=>el.value);
  if(!all&&!providers.length){$('subscription-error').textContent='Choose one or more providers.';return;}
  const id=$('subscription-id').value;
  subscriptionWorking=true;$('subscription-form').querySelectorAll('input,button').forEach(el=>el.disabled=true);$('save-subscription').textContent='Saving…';
  try{
    const result=await api('/subscriptions'+(id?'/'+encodeURIComponent(id):''),{method:id?'PUT':'POST',body:JSON.stringify({name:$('subscription-name').value,all_providers:all,provider_ids:providers})});
    selectedSubscription=result.id;
    if(!id){$('subscription-search').value='';$('subscription-status-filter').value='';subscriptionListScroll=0;}
    $('subscription-dialog').close();await loadState();
    if(!id){const index=subscriptionMatches().findIndex(({subscription:s})=>s.id===result.id);subscriptionPage=Math.max(0,Math.floor(index/Number($('subscription-page-size').value)));}
    showSubscription(result.id);toast(id?'Subscription updated; its links are unchanged':'Subscription created');
  }catch(error){$('subscription-error').textContent=error.message;}
  finally{subscriptionWorking=false;$('subscription-form').querySelectorAll('input,button').forEach(el=>el.disabled=false);$('save-subscription').textContent='Save subscription';}
});
$('delete-subscription').addEventListener('click',async()=>{
  const subscription=currentSubscription();$('subscription-menu').open=false;
  if(!subscription||subscription.id==='default'||!confirm(`Delete “${subscription.name}”? Its import links will stop working.`))return;
  try{await api('/subscriptions/'+encodeURIComponent(subscription.id),{method:'DELETE'});selectedSubscription='default';await loadState();showSubscriptionList();toast('Subscription deleted');}catch(error){toast(error.message);}
});
function connectionCell(value){
  const status=value?.status||'pending',label={passed:'Passed',failed:'Failed',unsupported:'Unsupported',error:'Check error',pending:'Pending',stale:'Expired'}[status]||status;
  const color=status==='passed'?'fresh':status==='failed'?'unavailable':status==='error'?'cached':'pending';
  return `<span class="status-pill ${color}" title="${esc(value?.reason||(status==='passed'?'HTTPS request passed':label))}">${esc(label)}${status==='passed'?` · ${esc(value.latency_ms)} ms`:''}</span><small class="connection-time" ${!value?.reason&&value?.checked_at?`data-checked-at="${esc(value.checked_at)}"`:''} title="${esc(value?.checked_at||'')}">${esc(value?.reason||relative(value?.checked_at))}</small>`;
}
function serverMatches(){
  if(!state)return [];
  const text=$('server-search').value.toLowerCase(),protocol=$('protocol-filter').value,client=$('client-filter').value,connection=$('connection-filter').value;
  const provider=state.providers.find(p=>p.id===$('provider-filter').value);
  return state.inventory.filter(n=>(!provider||n.sources.includes(provider.name))&&(!text||`${n.name} ${n.sources.join(' ')}`.toLowerCase().includes(text))&&(!protocol||n.protocol===protocol)&&(!client||n.clients.includes(client))&&(!connection||(connection==='passing'?n.clients.length>0:n.clients.length===0)));
}
function renderServers(){
  if(!state)return;
  const rows=serverMatches(),focused=document.activeElement?.dataset.serverId;
  $('server-total').textContent=state.total;
  setHTML('server-rows',rows.slice(0,1000).map(n=>`<tr><td class="provider-name"><button class="server-open" data-server-id="${esc(n.id)}" aria-label="View configuration for ${esc(n.name)}" aria-haspopup="dialog"><span>${esc(n.name)}</span><small>View configuration ${icon('chevron')}</small></button></td><td><span class="protocol-chip">${esc(n.protocol)}</span></td><td>${esc(n.sources.join(', '))}</td><td>${connectionCell(n.connectivity?.mihomo)}</td><td>${connectionCell(n.connectivity?.xray)}</td><td>${connectionCell(n.connectivity?.hiddify)}</td><td>${n.clients.map(c=>`<span class="client-badge">${esc(c==='hiddify'?'Hiddify':c==='mihomo'?'Mihomo':c==='v2board'?'V2Board JSON':c==='v2board_uri'?'V2Board URI':'Xray')}</span>`).join('')||'<span class="status-pill pending">Excluded</span>'}</td></tr>`).join('')||'<tr><td colspan="7" class="empty-small">No servers match these filters.</td></tr>');
  if(focused)serverButton(focused)?.focus({preventScroll:true});
  $('server-shown').textContent=`Showing ${Math.min(rows.length,1000)} of ${state.total} servers`;
  if($('server-dialog').open){
    const current=state.inventory.find(n=>n.id===serverDetailId);
    if(!current){serverDetailRequest++;clearServerConfiguration();$('server-detail-message').textContent='This server is no longer in the library. Close this panel or choose another server.';}
    else renderServerHealth(current);
  }
}
function serverButton(id){return [...$('server-rows').querySelectorAll('[data-server-id]')].find(button=>button.dataset.serverId===id);}
function clearServerConfiguration(){
  serverDetail=null;$('server-detail-content').hidden=true;$('server-detail-fields').replaceChildren();
  $('server-config-text').textContent='';$('server-config-format').replaceChildren();$('server-config-feedback').textContent='';
  $('server-config-copy').disabled=true;$('server-config-download').disabled=true;$('server-config-actions').hidden=true;$('server-detail-retry').hidden=true;
}
function renderServerHealth(server){
  $('server-detail-checks').innerHTML=[['mihomo','Mihomo'],['xray','Xray'],['hiddify','Hiddify']].map(([key,label])=>`<div><strong>${label}</strong>${connectionCell(server.connectivity?.[key])}</div>`).join('');
  $('server-detail-inclusion').textContent=server.clients.length?'Passing formats: '+server.clients.map(c=>({hiddify:'Hiddify',mihomo:'Mihomo',xray:'Xray',v2board:'V2Board JSON',v2board_uri:'V2Board URI'}[c]||c)).join(', '):'Excluded from subscription links. You can still inspect its configuration.';
}
async function loadServerDetail(id){
  const server=state?.inventory.find(n=>n.id===id),request=++serverDetailRequest;
  serverDetailId=id;clearServerConfiguration();
  $('server-detail-name').textContent=server?.name||'Server unavailable';
  $('server-detail-protocol').textContent=server?.protocol||'';$('server-detail-sources').textContent=server?.sources.join(', ')||'';
  const position=serverDetailIds.indexOf(id);
  $('server-detail-position').textContent=`${position+1} of ${serverDetailIds.length} servers`;
  $('server-detail-previous').disabled=position<=0;$('server-detail-next').disabled=position>=serverDetailIds.length-1;
  $('server-detail-message').textContent='Loading configuration…';$('server-detail-body').scrollTop=0;
  if(!server){$('server-detail-message').textContent='This server is no longer in the library. Choose another server or close this panel.';return;}
  try{
    const detail=await api('/servers/'+encodeURIComponent(id));
    if(request!==serverDetailRequest||!$('server-dialog').open||!state)return;
    serverDetail=detail;$('server-detail-message').textContent='';
    $('server-detail-fields').innerHTML=detail.fields.map(field=>`<div><dt>${esc(field.label)}</dt><dd>${esc(field.value)}</dd></div>`).join('');
    renderServerHealth(state.inventory.find(n=>n.id===id)||server);
    $('server-config-format').innerHTML=detail.configurations.map(config=>`<option value="${esc(config.id)}">${esc(config.label)}</option>`).join('');
    renderServerConfiguration();$('server-detail-content').hidden=false;
  }catch(error){if(request===serverDetailRequest&&$('server-dialog').open){$('server-detail-message').textContent=error.message;$('server-detail-retry').hidden=false;}}
}
function selectedServerConfiguration(){return serverDetail?.configurations.find(config=>config.id===$('server-config-format').value);}
function renderServerConfiguration(){
  const config=selectedServerConfiguration();
  $('server-config-text').textContent=config?.content||'';$('server-config-description').textContent=config?.description||'';
  $('server-config-feedback').textContent='';$('server-config-code').scrollTop=0;$('server-config-code').scrollLeft=0;
  $('server-config-copy').disabled=!config;$('server-config-download').disabled=!config;$('server-config-actions').hidden=!config;
}
$('server-rows').addEventListener('click',event=>{
  const button=event.target.closest('[data-server-id]');if(!button)return;
  serverDetailOrigin=button.dataset.serverId;serverDetailIds=serverMatches().slice(0,1000).map(n=>n.id);
  $('server-detail-name').textContent=state.inventory.find(n=>n.id===serverDetailOrigin)?.name||'Server configuration';
  $('server-dialog').showModal();$('server-detail-name').focus();loadServerDetail(serverDetailOrigin);
});
$('server-detail-previous').addEventListener('click',()=>loadServerDetail(serverDetailIds[serverDetailIds.indexOf(serverDetailId)-1]));
$('server-detail-next').addEventListener('click',()=>loadServerDetail(serverDetailIds[serverDetailIds.indexOf(serverDetailId)+1]));
$('server-detail-retry').addEventListener('click',()=>loadServerDetail(serverDetailId));
$('server-config-format').addEventListener('change',renderServerConfiguration);
$('server-config-copy').addEventListener('click',async()=>{
  const config=selectedServerConfiguration();if(!config)return;
  try{await copy(config.content);if(config===selectedServerConfiguration())$('server-config-feedback').textContent='Configuration copied';}
  catch{if(config===selectedServerConfiguration())$('server-config-feedback').textContent='Select the configuration text to copy it manually.';}
});
$('server-config-download').addEventListener('click',()=>{
  const config=selectedServerConfiguration();if(!config)return;
  const url=URL.createObjectURL(new Blob([config.content],{type:'text/plain;charset=utf-8'})),link=document.createElement('a');
  link.href=url;link.download=config.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
$('server-dialog').addEventListener('close',()=>{
  serverDetailRequest++;clearServerConfiguration();serverDetailId=null;serverDetailIds=[];
  if(state&&page==='servers')(serverButton(serverDetailOrigin)||$('server-search')).focus({preventScroll:true});
  serverDetailOrigin=null;
});
function renderIssues(){if(!state)return;const client=$('issue-client').value,issues=state.diagnostics.filter(i=>!client||i.client===client);$('issue-total').textContent=issues.length;$('issue-rows').innerHTML=issues.slice(0,150).map(i=>`<div class="issue-row"><span class="issue-dot">!</span><div><strong>${esc(i.name)} <span class="provider-host">${esc(i.source)}</span></strong><p>${esc(i.reason)}</p></div><span class="issue-client">${esc(i.client)}</span></div>`).join('')||'<div class="empty-small">No compatibility exceptions for this selection.</div>';if(issues.length>150)$('issue-rows').insertAdjacentHTML('beforeend',`<div class="empty-small">Showing 150 of ${issues.length} notes. Download the combined archive for the full report.</div>`);}
$('server-search').addEventListener('input',renderServers);$('protocol-filter').addEventListener('change',renderServers);$('client-filter').addEventListener('change',renderServers);$('issue-client').addEventListener('change',renderIssues);
$('provider-filter').addEventListener('change',renderServers);
async function refresh(id){try{await api('/refresh'+(id?'?provider_id='+encodeURIComponent(id):''),{method:'POST'});toast(`Refreshing ${id?(state.providers.find(p=>p.id===id)?.name||'provider'):'all providers'}…`);await loadState();}catch(error){toast(error.message);}}
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
  const serverLimit=$('provider-server-limit').value===''?null:Number($('provider-server-limit').value);
  if(serverLimit!==null&&(!Number.isInteger(serverLimit)||serverLimit<1||serverLimit>100000))throw new Error('Enter a whole server limit from 1 to 100000, or leave it blank for unlimited.');
  if(id&&urls.length>1)throw new Error('Use one subscription URL when editing a provider.');
  if(urls.length>50)throw new Error('Add at most 50 providers at a time.');
  if(!id&&!urls.length)throw new Error('Enter a subscription link.');
  if(new Set(urls).size!==urls.length)throw new Error('Remove duplicate links from this import.');
  return (urls.length?urls:['']).map((url,index)=>{
    const choice=urls.length>1?$(`assignment-${index}`).value:$('provider-hwid').value;
    if(!choice)throw new Error(`Choose a device or “No device ID” for link ${index+1}.`);
    const inferred=prefix||(url?new URL(url).hostname:providerOriginal.name);
    return {name:inferred+(urls.length>1?` ${index+1}`:''),url,hwid_id:choice==='__none__'?null:choice,user_agent:$('provider-agent').value,enabled:providerOriginal?.enabled??true,server_limit:serverLimit};
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
  $('provider-server-limit').value=provider?.server_limit??'';
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
      const result=testRuns.get(JSON.stringify(body));return result?`Link ${i+1}: fetch passed${body.server_limit?` (${result.selected_count} randomly selected from ${result.fetched_count} servers)`:''} (${Object.entries(result.counts).map(([client,count])=>`${client}: ${count}`).join(', ')}).`:`Link ${i+1}: already saved.`;
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
$('provider-rows').addEventListener('click',async event=>{
  const button=event.target.closest('[data-action]');if(!button)return;
  const provider=state.providers.find(p=>p.id===button.dataset.id);if(!provider)return;
  const menu=button.closest('.provider-actions');menu.open=false;menu.querySelector('summary').focus();
  try{
    if(button.dataset.action==='edit')openProvider(provider);
    if(button.dataset.action==='refresh')await refresh(provider.id);
    if(button.dataset.action==='toggle'){
      await api('/providers/'+provider.id,{method:'PUT',body:JSON.stringify({...provider,url:'',enabled:!provider.enabled})});
      toast(provider.enabled?'Provider paused':'Provider enabled');await loadState();if(!provider.enabled)await refresh(provider.id);
    }
    if(button.dataset.action==='delete'&&confirm(`Remove “${provider.name}” and its servers from the combined subscriptions?`)){
      await api('/providers/'+provider.id,{method:'DELETE'});toast('Provider removed');await loadState();
    }
  }catch(error){toast(error.message);}
});
function renderDevices(){
  $('device-total').textContent=state.hwids.length;
  $('hwid-rows').innerHTML=state.hwids.map(h=>`<article class="device-card" data-device="${esc(h.id)}"><div class="device-heading">${icon('shield')}<strong>${esc(h.name)}</strong><code>${esc(h.masked_value)}</code><span class="status-pill ${h.configured?'':'unavailable'}">${h.configured?'Saved':'Missing ID'}</span></div><p>${h.providers.length?`Used by ${h.providers.length} subscription(s)`:'Not assigned to any subscriptions'}</p>${h.providers.length?`<ul class="device-usage">${h.providers.map(p=>`<li>${esc(p.name)}${p.enabled?'':' · Paused'}</li>`).join('')}</ul>`:''}<div class="device-actions"><button class="button secondary" data-device-action="rename" ${state.busy?'disabled':''}>Rename</button><button class="button secondary" data-device-action="reveal" ${h.configured?'':'disabled'}>Reveal ID</button><button class="button secondary" data-device-action="copy" ${h.configured?'':'disabled'}>Copy ID</button><button class="button secondary" data-device-action="replace" ${state.busy?'disabled':''}>Replace ID</button><button class="button secondary" data-device-action="delete" ${h.providers.length||state.busy?'disabled':''} title="${h.providers.length?'Reassign subscriptions before deleting this device':'Delete unused device'}">Delete</button></div></article>`).join('')||'<div class="empty-small">Add a device ID already authorized for your subscription, or choose “No device ID” when adding a provider that does not require one.</div>';
}
function clearReveal(){clearTimeout(revealTimer);$('device-revealed-value').value='';$('device-reveal-dialog').close();}
function clearPrivateDialogs(){
  clearReveal();document.querySelectorAll('dialog[open]').forEach(dialog=>dialog.close());
  clearSecuritySecrets();$('recovery-codes').textContent='';
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
async function copy(text){if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);return;}const input=document.createElement('textarea');input.value=text;input.setAttribute('readonly','');(document.querySelector('dialog[open]')||document.body).appendChild(input);input.select();const success=document.execCommand('copy');input.remove();if(!success)throw new Error('Clipboard unavailable. Select and copy the text manually.');}
$('export-cards').addEventListener('click',async event=>{const button=event.target.closest('[data-copy]');if(!button)return;try{await copy(currentSubscription().links[button.dataset.copy]);toast('Subscription link copied');}catch(error){toast(error.message);}});
$('rotate-links').addEventListener('click',async()=>{const subscription=currentSubscription();$('subscription-menu').open=false;if(!subscription||!confirm(`Rotate links for “${subscription.name}”? Only its existing links will stop working.`))return;try{await api('/subscriptions/'+encodeURIComponent(subscription.id)+'/rotate-link',{method:'POST'});await loadState();toast('New subscription links are ready');}catch(error){toast(error.message);}});
$('schedule-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/settings',{method:'PUT',body:JSON.stringify({refresh_seconds:Number($('refresh-interval').value)})});await loadState();toast('Refresh schedule saved');}catch(error){toast(error.message);}});
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();$('login-error').textContent='';$('login-submit').disabled=true;
  try{
    // Browser autofill can restore a code even while its field is hidden.
    const code=$('login-factor').hidden?'':$('login-code').value.trim();
    const result=await api('/login',{method:'POST',body:JSON.stringify({token:$('admin-key').value,code})});
    if(result.requires_two_factor){
      $('login-code').value='';$('login-code').disabled=false;$('login-code').required=true;
      $('login-factor').hidden=false;$('login-submit-label').textContent='Verify and open workspace';
      $('login-code').focus();return;
    }
    $('admin-key').value='';$('login-code').value='';await loadState();if(page==='activity')loadLogs();
    if(result.recovery_used){selectPage('settings');toast('Recovery code used. Check your remaining codes in Account security.');}
  }catch(error){$('login-error').textContent=error.message;}
  finally{$('login-submit').disabled=false;}
});
$('logout-button').addEventListener('click',async()=>{try{await api('/logout',{method:'POST'});showLogin();}catch(error){toast(error.message);}});

async function loadSecurity(){
  if(!state)return;const request=++securityRequest;$('security-error').textContent='';
  try{
    const security=await api('/security');if(request!==securityRequest||!state)return;
    $('two-factor-status').textContent=security.enabled?'Enabled':'Not enabled';
    $('two-factor-status').className='status-pill'+(security.enabled?'':' unavailable');
    $('two-factor-description').textContent=security.enabled?`Your admin key and an authenticator code are required to sign in. ${security.recovery_codes_remaining} recovery codes remaining.`:'Add a time-based authenticator such as Google Authenticator, Aegis or 2FAS. You will confirm a code before protection is enabled.';
    $('security-reminder').hidden=security.enabled;
    $('setup-two-factor').hidden=security.enabled;$('setup-two-factor').disabled=false;
    $('replace-recovery').hidden=!security.enabled;$('disable-two-factor').hidden=!security.enabled;
  }catch(error){if(request===securityRequest&&state)$('security-error').textContent=error.message;}
}
function clearSecuritySecrets(){
  ['security-key','security-code','security-secret','security-confirm-code'].forEach(id=>$(id).value='');
  $('security-qr').removeAttribute('src');
}
function openSecurity(action){
  securityAction=action;clearSecuritySecrets();$('security-form').reset();$('security-dialog-error').textContent='';
  $('security-credentials').hidden=false;$('security-enrollment').hidden=true;
  $('security-key').required=true;$('security-confirm-code').required=false;
  $('security-factor').hidden=action==='setup';$('security-code').required=action!=='setup';
  $('security-dialog-title').textContent=action==='setup'?'Set up authenticator':action==='disable'?'Disable two-factor?':'Replace recovery codes';
  $('security-dialog-description').textContent=action==='setup'?'Confirm your admin key, then scan the QR code with your authenticator.':action==='disable'?'Your admin key will be the only factor needed to sign in. Confirm both factors to continue. Other sessions will be signed out.':'Your old recovery codes will stop working. Confirm both factors to create a new set and sign out other sessions.';
  $('security-submit').textContent=action==='setup'?'Continue':action==='disable'?'Disable two-factor':'Create new codes';
  $('security-dialog').showModal();$('security-key').focus();
}
$('setup-two-factor').addEventListener('click',()=>openSecurity('setup'));
$('replace-recovery').addEventListener('click',()=>openSecurity('recovery'));
$('disable-two-factor').addEventListener('click',()=>openSecurity('disable'));
$('security-dialog').addEventListener('close',clearSecuritySecrets);
$('security-secret').addEventListener('click',event=>event.target.select());
$('security-dialog').addEventListener('cancel',event=>{if(securityWorking)event.preventDefault();});
$('recovery-dialog').addEventListener('close',()=>$('recovery-codes').textContent='');
$('copy-recovery').addEventListener('click',async()=>{try{await copy($('recovery-codes').textContent);toast('Recovery codes copied. Store them somewhere safe.');}catch(error){toast(error.message);}});
$('security-form').addEventListener('submit',async event=>{
  event.preventDefault();if(securityWorking)return;securityWorking=true;$('security-dialog-error').textContent='';
  const button=$('security-submit'),label=button.textContent;
  $('security-form').querySelectorAll('button').forEach(b=>b.disabled=true);button.textContent='Verifying…';
  try{
    const body=securityAction==='confirm'?{code:$('security-confirm-code').value}:{token:$('security-key').value,code:$('security-code').value.trim()};
    const result=await api('/security/2fa/'+securityAction,{method:'POST',body:JSON.stringify(body)});
    if(securityAction==='setup'){
      $('security-key').value='';$('security-key').required=false;$('security-credentials').hidden=true;
      $('security-secret').value=result.secret;$('security-qr').src=result.qr;$('security-enrollment').hidden=false;
      $('security-confirm-code').required=true;$('security-dialog-description').textContent='Scan the code, then enter the 6-digit code from your app to enable protection.';
      securityAction='confirm';$('security-confirm-code').focus();
    }else{
      $('security-dialog').close();await loadSecurity();
      if(result.recovery_codes){$('recovery-codes').textContent=result.recovery_codes.join('\n');$('recovery-dialog').showModal();}
      else toast('Two-factor authentication disabled. Other sessions have been signed out.');
    }
  }catch(error){$('security-dialog-error').textContent=error.message;}
  finally{securityWorking=false;$('security-form').querySelectorAll('button').forEach(b=>b.disabled=false);button.textContent=securityAction==='confirm'?'Enable two-factor':label;}
});
(async()=>{try{const session=await api('/session');if(session.authenticated)await loadState();else showLogin();}catch{showLogin();$('login-error').textContent='Unable to reach the server. Please try again.';}})();
