"use strict";
// Only visible rows and one editor are rendered; all edits stay in the local draft.
(() => {
  let draft=null,available=[],availableTotal=0,availableOffset=0,savedPage=0,expandedIndex=null;
  let request=0,searchTimer,working=false,relinkIndex=null,subscriptionId=null;
  const pageSize=25;
  const binding=row=>`${row.provider_id}:${row.source_identity}`;
  const hasOverrides=row=>['address','port','name'].some(key=>row[key]!==null&&row[key]!==undefined&&row[key]!=='');
  const endpoint=source=>`${String(source.address||'Unknown address').includes(':')?`[${source.address}]`:source.address||'Unknown address'}:${source.port??'Unknown port'}`;
  const effectiveEndpoint=row=>endpoint({address:row.address||row.source.address,port:row.port??row.source.port});
  const statusLabels={unavailable:'Source unavailable',source_failed:'Source failed',pending_or_failed:'Pending / failed',working:'Working',unsaved:'Edited'};
  const statusDetails={unavailable:'Not in the current provider selection. Your saved edits are kept.',source_failed:'The source check failed or expired.',pending_or_failed:'Custom connection checks are pending or failed.',working:'A compatible connection check passed.',unsaved:'These changes have not been saved yet.'};
  const statusClass=row=>row.status==='working'?'fresh':row.status==='unsaved'?'pending':'unavailable';

  function filteredRecords(){
    if(!draft)return [];
    const query=$('policy-saved-search').value.trim().toLocaleLowerCase(),provider=$('policy-provider-filter').value,filter=$('policy-status-filter').value;
    return draft.records.map((row,index)=>({row,index})).filter(({row})=>{
      const text=[row.name,row.address,row.port,row.source.name,row.source.address,row.source.port,row.source.protocol,row.provider_name].join(' ').toLocaleLowerCase();
      return text.includes(query)&&(!provider||row.provider_id===provider)&&(!filter||
        (filter==='selected'?row.selected:filter==='unselected'?!row.selected:filter==='overrides'?hasOverrides(row):row.status===filter));
    });
  }
  function renderCounts(){
    const matches=filteredRecords();
    $('policy-tab-count').textContent=String(draft.records.length);
    $('policy-saved-count').textContent=`${draft.records.length} saved · ${draft.all_servers?'all working included':draft.records.filter(row=>row.selected).length+' selected'}`;
    $('policy-bulk-selection').hidden=draft.all_servers;
    for(const [id,label] of [['policy-include-matches','Include'],['policy-exclude-matches','Exclude']]){
      $(id).textContent=`${label} ${matches.length} matches`;
      $(id).disabled=!matches.length;
      $(id).title='Applies to all filtered saved servers, across every page.';
    }
  }
  function renderProviders(){
    const selected=$('policy-provider-filter').value;
    const providers=[...new Map(draft.records.map(row=>[row.provider_id,row.provider_name])).entries()].sort((a,b)=>a[1].localeCompare(b[1]));
    $('policy-provider-filter').innerHTML='<option value="">All providers</option>'+providers.map(([id,name])=>`<option value="${esc(id)}">${esc(name)}</option>`).join('');
    $('policy-provider-filter').value=providers.some(([id])=>id===selected)?selected:'';
  }
  function editor(row,index){
    const fields=[['address','Address','text',253],['port','Port','number',null],['name','Name','text',200]].map(([key,label,type,max])=>
      `<div class="policy-field"><label for="policy-${index}-${key}">${label} override</label><div class="policy-field-input"><input id="policy-${index}-${key}" data-field="${key}" type="${type}" ${max?`maxlength="${max}"`:'min="1" max="65535" step="1"'} value="${esc(row[key]??'')}" placeholder="${esc(row.source[key]??'Provider value')}" autocomplete="off"><button type="button" class="text-button" data-reset="${key}" aria-label="Reset ${label.toLowerCase()} to provider value">Reset</button></div><small>Provider: ${esc(row.source[key]??'Not specified')}</small></div>`).join('');
    const checks=Object.entries(row.checks||{}).map(([core,value])=>`${core}: ${value.status}`).join(' · ');
    const bound=new Set(draft.records.map(binding));
    const suggestions=(row.suggestions||[]).map((candidate,i)=>bound.has(binding(candidate))?'':`<button type="button" class="text-button policy-suggestion" data-suggestion="${i}">Relink to ${esc(candidate.source.name)} · ${esc(endpoint(candidate.source))}</button>`).join('');
    return `<tr class="policy-editor-row" data-policy-index="${index}"><td colspan="5"><div class="policy-record-editor" id="policy-editor-${index}"><div class="policy-edit-heading"><h4>Edit ${esc(row.name||row.source.name)}</h4><button type="button" class="text-button" data-done>Done editing</button></div><p class="hint policy-status-detail">${esc(statusDetails[row.status]||'')}</p><div class="policy-fields">${fields}</div><p class="hint">Blank fields use provider values. Address and port changes need a passing connection check.</p>${checks?`<p class="hint policy-health">${esc(checks)}</p>`:''}${suggestions?`<div class="policy-suggestions"><p class="hint">Possible source matches. Relinking keeps your overrides.</p>${suggestions}</div>`:''}<div class="policy-record-actions"><button type="button" class="text-button" data-relink>Relink source…</button><button type="button" class="text-button danger-text" data-remove>Forget saved server</button></div></div></td></tr>`;
  }
  function renderRecords(){
    if(!draft)return;
    const matches=filteredRecords(),pages=Math.max(1,Math.ceil(matches.length/pageSize));
    savedPage=Math.max(0,Math.min(savedPage,pages-1));
    renderCounts();
    $('policy-records').innerHTML=matches.slice(savedPage*pageSize,(savedPage+1)*pageSize).map(({row,index})=>{
      const expanded=expandedIndex===index;
      return `<tr class="policy-record${expanded?' is-editing':''}" data-policy-index="${index}"><td class="policy-include">${draft.all_servers?'<span class="policy-auto" title="Included automatically while working">Auto</span>':`<input type="checkbox" data-selected ${row.selected?'checked':''} aria-label="Include ${esc(row.name||row.source.name)}">`}</td><td class="policy-server"><strong class="policy-name">${esc(row.name||row.source.name)}</strong><small>${esc(row.provider_name)} · ${esc(row.source.protocol)}</small></td><td class="policy-address"><span class="policy-endpoint" title="${esc(effectiveEndpoint(row))}">${esc(effectiveEndpoint(row))}</span><small class="policy-customized" ${hasOverrides(row)?'':'hidden'}>${row.status==='unsaved'?'Overrides in draft':'Customized'}</small></td><td class="policy-connection"><span class="status-pill policy-status ${statusClass(row)}" title="${esc(statusDetails[row.status]||'')}">${esc(statusLabels[row.status]||'Saved')}</span></td><td class="policy-edit"><button type="button" class="text-button" data-edit aria-expanded="${expanded}" ${expanded?`aria-controls="policy-editor-${index}"`:''} aria-label="${expanded?'Close editor for':'Edit'} ${esc(row.name||row.source.name)}">${expanded?'Close':'Edit'}</button></td></tr>${expanded?editor(row,index):''}`;
    }).join('');
    $('policy-saved-empty').hidden=matches.length>0;
    $('policy-saved-empty').textContent=draft.records.length?'No saved servers match these filters.':'No saved servers yet. Open Add servers to choose or customize a working server.';
    $('policy-saved-page').textContent=`${matches.length?savedPage*pageSize+1:0}–${Math.min((savedPage+1)*pageSize,matches.length)} of ${matches.length} matches`;
    $('policy-saved-page-select').innerHTML=Array.from({length:pages},(_,page)=>`<option value="${page}" ${page===savedPage?'selected':''}>Page ${page+1} of ${pages}</option>`).join('');
    $('policy-saved-previous').disabled=savedPage===0;$('policy-saved-next').disabled=savedPage>=pages-1;
  }
  function availableCandidates(){
    const bound=new Set(draft.records.map(binding));
    return available.map((row,index)=>({row,index})).filter(({row})=>!bound.has(binding(row)));
  }
  function renderAvailable(){
    if(!draft)return;
    const candidates=availableCandidates();
    $('policy-available').innerHTML=candidates.map(({row,index})=>
      `<div class="policy-available-row"><div><strong>${esc(row.source.name)}</strong><p>${esc(row.provider_name)} · ${esc(row.source.protocol)} · ${esc(endpoint(row.source))}</p></div><button type="button" class="button secondary small" data-add="${index}">${relinkIndex!==null?'Relink to this server':draft.all_servers?'Customize':'Select server'}</button></div>`).join('')||`<p class="hint policy-empty">${available.length?'All servers on this page are already saved.':'No additional working servers match this search.'}</p>`;
    $('policy-available-page').textContent=`${availableTotal?availableOffset+1:0}–${Math.min(availableOffset+50,availableTotal)} of ${availableTotal} results`;
    $('policy-available-previous').disabled=availableOffset===0;
    $('policy-available-next').disabled=availableOffset+50>=availableTotal;
    $('policy-add-page').hidden=relinkIndex!==null;
    $('policy-add-page').textContent=`Add page (${candidates.length})`;
    $('policy-add-page').disabled=!candidates.length||draft.records.length>=draft.max_records;
    $('policy-relink-notice').hidden=relinkIndex===null;
    if(relinkIndex!==null){const row=draft.records[relinkIndex];$('policy-relink-label').textContent=`Choose a replacement source for “${row.name||row.source.name}”. Your overrides will be kept.`;}
  }
  function showTab(name,focus=false){
    for(const tab of ['saved','available']){
      const selected=tab===name,button=$(`policy-tab-${tab}`);
      button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;
      $(`policy-${tab}-panel`).hidden=!selected;
    }
    document.querySelector('.policy-editor-body').scrollTop=0;
    if(focus)$(`policy-tab-${name}`).focus();
  }
  function resetSavedFilters(){
    $('policy-saved-search').value='';$('policy-provider-filter').value='';$('policy-status-filter').value='';
  }
  function focusEditor(index,field='address'){
    const input=$(`policy-${index}-${field}`);
    if(input){input.focus({preventScroll:true});input.scrollIntoView({block:'center'});}
  }
  function changeSavedPage(page){
    savedPage=page;expandedIndex=null;renderRecords();
    $('policy-saved-panel').scrollIntoView({block:'start'});
  }
  async function loadAvailable(initial=false){
    const sequence=++request,id=subscriptionId;
    $('policy-loading').hidden=false;$('policy-loading').textContent='Loading working servers…';
    $('policy-retry').hidden=true;
    try{
      const params=new URLSearchParams({q:$('policy-available-search').value,offset:String(availableOffset),limit:'50'});
      const data=await api(`/subscriptions/${encodeURIComponent(id)}/servers?${params}`);
      if(sequence!==request||!$('policy-dialog').open||!state)return;
      if(initial){draft=data;$('policy-mode').value=data.all_servers?'all':'selected';$('policy-mode').disabled=false;savedPage=0;renderProviders();renderRecords();showTab(data.records.length?'saved':'available');}
      if(data.revision!==draft.revision)throw new Error('Server settings changed elsewhere. Close and reopen this editor before saving.');
      available=data.available;availableTotal=data.available_total;renderAvailable();
      $('policy-content').hidden=false;$('policy-save').disabled=false;$('policy-loading').hidden=true;
    }catch(error){
      if(sequence===request&&$('policy-dialog').open){$('policy-error').textContent=error.message;$('policy-loading').hidden=true;$('policy-retry').hidden=false;}
    }
  }
  $('customize-subscription').addEventListener('click',()=>{
    const subscription=currentSubscription();if(!subscription)return;
    draft=null;available=[];availableOffset=0;savedPage=0;expandedIndex=null;relinkIndex=null;subscriptionId=subscription.id;
    $('policy-form').reset();$('policy-error').textContent='';$('policy-content').hidden=true;
    $('policy-save').disabled=true;$('policy-mode').disabled=true;$('policy-description').textContent=`Choose and customize servers for “${subscription.name}”.`;
    $('policy-dialog').showModal();loadAvailable(true);
  });
  $('policy-dialog').addEventListener('cancel',event=>{if(working)event.preventDefault();});
  $('policy-dialog').addEventListener('close',()=>{
    request++;clearTimeout(searchTimer);draft=null;available=[];subscriptionId=null;relinkIndex=null;expandedIndex=null;
    $('policy-records').textContent='';$('policy-available').textContent='';$('policy-error').textContent='';$('policy-provider-filter').innerHTML='<option value="">All providers</option>';
    if(state)$('customize-subscription').focus({preventScroll:true});
  });
  for(const name of ['saved','available']){
    $(`policy-tab-${name}`).addEventListener('click',()=>showTab(name));
    $(`policy-tab-${name}`).addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();showTab(event.key==='Home'?'saved':event.key==='End'?'available':name==='saved'?'available':'saved',true);
    });
  }
  $('policy-mode').addEventListener('change',()=>{if(draft){draft.all_servers=$('policy-mode').value==='all';renderRecords();renderAvailable();}});
  for(const [id,event] of [['policy-saved-search','input'],['policy-provider-filter','change'],['policy-status-filter','change']]){
    $(id).addEventListener(event,()=>{savedPage=0;expandedIndex=null;renderRecords();});
  }
  $('policy-saved-previous').addEventListener('click',()=>changeSavedPage(savedPage-1));
  $('policy-saved-next').addEventListener('click',()=>changeSavedPage(savedPage+1));
  $('policy-saved-page-select').addEventListener('change',()=>changeSavedPage(Number($('policy-saved-page-select').value)));
  for(const [id,selected] of [['policy-include-matches',true],['policy-exclude-matches',false]]){
    $(id).addEventListener('click',()=>{
      if(!draft||draft.all_servers)return;
      const matches=filteredRecords();matches.forEach(({row})=>row.selected=selected);renderRecords();
      toast(`${matches.length} matching servers ${selected?'included':'excluded'} in your draft. Save to apply.`);
    });
  }
  $('policy-available-search').addEventListener('input',()=>{clearTimeout(searchTimer);request++;availableOffset=0;searchTimer=setTimeout(()=>loadAvailable(),250);});
  $('policy-available-previous').addEventListener('click',()=>{availableOffset=Math.max(0,availableOffset-50);loadAvailable();});
  $('policy-available-next').addEventListener('click',()=>{availableOffset+=50;loadAvailable();});
  $('policy-refresh').addEventListener('click',()=>{availableOffset=0;loadAvailable();});
  $('policy-retry').addEventListener('click',()=>loadAvailable(!draft));
  $('policy-cancel-relink').addEventListener('click',()=>{relinkIndex=null;renderAvailable();showTab('saved');focusEditor(expandedIndex);});
  function updateSummary(index){
    const row=draft.records[index],element=$('policy-records').querySelector(`.policy-record[data-policy-index="${index}"]`);
    if(element){
      element.querySelector('.policy-name').textContent=row.name||row.source.name;
      const address=element.querySelector('.policy-endpoint');address.textContent=effectiveEndpoint(row);address.title=effectiveEndpoint(row);
      const badge=element.querySelector('.policy-status');badge.textContent=statusLabels[row.status]||'Saved';badge.className=`status-pill policy-status ${statusClass(row)}`;badge.title=statusDetails[row.status]||'';
      const customized=element.querySelector('.policy-customized');customized.hidden=!hasOverrides(row);customized.textContent='Overrides in draft';
    }
    const edit=$(`policy-editor-${index}`);
    if(edit){edit.querySelector('.policy-status-detail').textContent=statusDetails[row.status]||'';const health=edit.querySelector('.policy-health');if(health)health.textContent='';}
    renderCounts();
  }
  $('policy-records').addEventListener('input',event=>{
    const card=event.target.closest('[data-policy-index]');if(!card||!draft)return;
    const index=Number(card.dataset.policyIndex),row=draft.records[index],field=event.target.dataset.field;
    if(field){
      row[field]=event.target.value===''?null:field==='port'?Number(event.target.value):event.target.value;
      if(field==='port')row.invalidPort=event.target.validity.badInput;
      row.status='unsaved';row.checks={};updateSummary(index);
    }
    if(event.target.hasAttribute('data-selected')){row.selected=event.target.checked;if(['selected','unselected'].includes($('policy-status-filter').value))renderRecords();else renderCounts();}
  });
  function relink(index,candidate){
    if(draft.records.some((row,i)=>i!==index&&binding(row)===binding(candidate))){$('policy-error').textContent='This source is already saved. Choose another source.';return;}
    Object.assign(draft.records[index],candidate,{relink:true,source_present:true,status:'unsaved',suggestions:[],checks:{}});
    relinkIndex=null;expandedIndex=index;resetSavedFilters();savedPage=Math.floor(index/pageSize);renderProviders();renderRecords();renderAvailable();showTab('saved');focusEditor(index);
    toast('Source relinked in your draft. Save to apply it.');
  }
  $('policy-records').addEventListener('click',event=>{
    const button=event.target.closest('button'),card=button?.closest('[data-policy-index]');if(!card||!draft)return;
    const index=Number(card.dataset.policyIndex),row=draft.records[index];
    if(button.hasAttribute('data-edit')||button.hasAttribute('data-done')){
      expandedIndex=expandedIndex===index?null:index;renderRecords();
      if(expandedIndex!==null)focusEditor(index);else $('policy-records').querySelector(`[data-policy-index="${index}"] [data-edit]`)?.focus({preventScroll:true});
    }
    if(button.dataset.reset){const field=button.dataset.reset;row[field]=null;if(field==='port')row.invalidPort=false;row.status='unsaved';row.checks={};const input=$(`policy-${index}-${field}`);input.value='';input.focus();updateSummary(index);}
    if(button.hasAttribute('data-remove')){draft.records.splice(index,1);expandedIndex=null;relinkIndex=null;renderProviders();renderRecords();renderAvailable();$('policy-saved-search').focus();}
    if(button.hasAttribute('data-relink')){relinkIndex=index;availableOffset=0;$('policy-available-search').value='';renderAvailable();showTab('available');loadAvailable();$('policy-available-search').focus();}
    if(button.hasAttribute('data-suggestion'))relink(index,row.suggestions[Number(button.dataset.suggestion)]);
  });
  function addCandidates(candidates,expand){
    if(draft.records.length+candidates.length>draft.max_records){$('policy-error').textContent=`Only ${draft.max_records-draft.records.length} saved-server slots remain. Add fewer servers or forget unused entries.`;return;}
    const first=draft.records.length;
    for(const candidate of candidates)draft.records.push({...candidate,id:null,selected:true,address:null,port:null,name:null,source_present:true,status:'unsaved',checks:{},suggestions:[]});
    resetSavedFilters();savedPage=Math.floor(first/pageSize);expandedIndex=expand?first:null;renderProviders();renderRecords();renderAvailable();showTab('saved');
    if(expand)focusEditor(first);else toast(`${candidates.length} servers added to your draft. Save to apply.`);
  }
  $('policy-available').addEventListener('click',event=>{
    const button=event.target.closest('[data-add]');if(!button||!draft)return;
    const candidate=available[Number(button.dataset.add)];
    if(draft.records.some(row=>binding(row)===binding(candidate)))return;
    if(relinkIndex!==null){relink(relinkIndex,candidate);return;}
    addCandidates([candidate],true);
  });
  $('policy-add-page').addEventListener('click',()=>{
    if(draft&&relinkIndex===null){const candidates=availableCandidates();if(candidates.length)addCandidates(candidates.map(({row})=>row),false);}
  });
  $('policy-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!draft||working)return;
    const invalid=draft.records.findIndex(row=>row.invalidPort||(row.port!==null&&row.port!==undefined&&(!Number.isInteger(row.port)||row.port<1||row.port>65535)));
    if(invalid!==-1){
      resetSavedFilters();savedPage=Math.floor(invalid/pageSize);expandedIndex=invalid;renderRecords();showTab('saved');focusEditor(invalid,'port');
      $('policy-error').textContent='Each custom port must be an integer between 1 and 65535.';return;
    }
    request++;clearTimeout(searchTimer);
    const id=subscriptionId,body={revision:draft.revision,all_servers:draft.all_servers,records:draft.records.map(row=>{
      const result={};['id','provider_id','source_identity','selected','address','port','name','relink'].forEach(key=>{if(row[key]!==undefined)result[key]=row[key];});return result;
    })};
    working=true;$('policy-error').textContent='';$('policy-save').textContent='Saving…';
    $('policy-form').querySelectorAll('input,select,button').forEach(el=>el.disabled=true);
    try{
      await api(`/subscriptions/${encodeURIComponent(id)}/servers`,{method:'PUT',body:JSON.stringify(body)});
      if(!state||subscriptionId!==id)return;
      $('policy-dialog').close();await loadState();toast('Server settings saved. Changed connections are queued for checks.');
    }catch(error){if(subscriptionId===id&&state)$('policy-error').textContent=error.message;}
    finally{
      working=false;$('policy-save').textContent='Save server settings';
      $('policy-form').querySelectorAll('input,select,button').forEach(el=>el.disabled=false);
      if(draft){renderRecords();renderAvailable();}
    }
  });
})();
