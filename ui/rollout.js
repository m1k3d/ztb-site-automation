/* Overview presentation; deployment approval remains in app.js and the server. */
function selectBranches(batch, indices, on, canSelect, replace=false) {
  // Resolve eligibility before any selection mutation changes the readiness fingerprint.
  const allowed=indices.filter(i=>!on || canSelect(i));
  if(replace)for(const site of batch)site.fields.post='0';
  for(const i of allowed)batch[i].fields.post=on?'1':'0';
}
const ZtbRollout = {install(ctx) {
  const $=id=>document.getElementById(id);
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  let checks=[],results=[],tenant=null,key='',pending='',timer=null,sequence=0,error='',actionSite=null;
  const normalize=s=>String(s || '').trim().toLowerCase();
  const fingerprint=()=>JSON.stringify([ctx.projectId(),ctx.batch()]);
  const country=value=>{
    const clean=normalize(value).replaceAll('_',' ').replace(/^the /,'');
    const option=[...$('country-options').options].find(o=>[o.value,...(o.dataset.search || '').split('|')].some(v=>normalize(v).replaceAll('_',' ').replace(/^the /,'')===clean));
    return option?.value || String(value || '').trim() || 'Country not set';
  };
  const resultFor=site=>results.find(r=>normalize(r.name)===normalize(site.fields.site_name));
  const blocked=site=>['success','already_exists'].includes(resultFor(site)?.status);
  const eligible=index=>checks[index]?.ready && !blocked(ctx.batch()[index]);
  const groupEligible=index=>eligible(index) && !['partial','template_only','failed'].includes(resultFor(ctx.batch()[index])?.status);
  function select(indices,on,replace=false){if(ctx.active() || (on && key!==fingerprint()))return;selectBranches(ctx.batch(),indices,on,eligible,replace);ctx.changed();refresh();}
  function button(text,action,cls='text-button'){const b=el('button',text,cls);b.type='button';b.onclick=action;return b;}
  function render(){
    const batch=ctx.batch(),indices=batch.map((s,i)=>i).filter(i=>!batch[i].reference),valid=key===fingerprint();
    const available=valid?indices.filter(groupEligible):[],selected=indices.filter(i=>String(batch[i].fields.post)==='1');
    const complete=indices.filter(i=>resultFor(batch[i])?.status==='success').length;
    const needs=indices.filter(i=>checks[i]?.issues.length).length;
    $('rollout-summary').textContent=`${indices.length} ${indices.length===1?'branch':'branches'} · ${valid?available.length+' ready · '+needs+(needs===1?' needs changes':' need changes'):'Checking readiness…'} · ${complete} completed`;
    $('rollout-selected').textContent=`${selected.length} ${selected.length===1?'branch':'branches'} selected`;
    $('rollout-review').disabled=!selected.length || ctx.active();
    $('rollout-select-ready').disabled=!available.length || ctx.active();
    const all=$('rollout-select-all');all.disabled=!available.length || ctx.active();all.checked=!!available.length && available.every(i=>selected.includes(i));all.indeterminate=!all.checked && available.some(i=>selected.includes(i));
    $('rollout-group').checked=ctx.grouped();
    $('rollout-check-note').textContent=error || (tenant?`Recorded results for ${tenant}. Other sessions and earlier runs are checked during tenant preview.`:'Connect to a tenant in Reference site to match recorded results. Readiness checks local inputs only.');
    $('rollout-check-note').classList.toggle('deployment-issue',!!error);
    const rows=$('rollout-rows');rows.replaceChildren();
    function row(i){
      const site=batch[i],f=site.fields,check=valid?checks[i]:null,result=resultFor(site),tr=el('tr');tr.dataset.selected=String(selected.includes(i));
      const cell=el('td'),box=el('input');box.type='checkbox';box.checked=selected.includes(i);box.disabled=ctx.active() || (!box.checked && (!valid || !eligible(i)));box.setAttribute('aria-label',`Select ${f.site_name || 'Untitled branch'}`);box.onchange=()=>select([i],box.checked);cell.append(box);
      const name=el('td'),heading=el('div',undefined,'branch-heading'),actions=el('div',undefined,'branch-actions');
      actions.id=`rollout-actions-${i}`;actions.hidden=actionSite!==site || ctx.active();
      const toggle=button('⋯',()=>{
        if(ctx.active())return;
        actionSite=actionSite===site?null:site;render();$(`rollout-action-toggle-${i}`)?.focus();
      },'branch-actions-toggle');
      toggle.id=`rollout-action-toggle-${i}`;toggle.disabled=ctx.active();
      toggle.setAttribute('aria-label',`Actions for ${f.site_name || 'Untitled branch'}`);
      toggle.setAttribute('aria-expanded',String(!actions.hidden));toggle.setAttribute('aria-controls',actions.id);
      const remove=button('Remove from rollout',()=>{if(!ctx.active())ctx.remove(site);},'text-button danger');
      remove.disabled=ctx.active();actions.append(remove);
      name.onkeydown=event=>{if(event.key==='Escape' && actionSite===site){event.preventDefault();actionSite=null;render();$(`rollout-action-toggle-${i}`)?.focus();}};
      heading.append(button(f.site_name || 'Untitled branch',()=>ctx.edit(i),'branch-link'),toggle);
      name.append(heading,el('small',`${country(f.country)} · ${site.vlans.length} VLANs`),actions);
      const config=el('td',undefined,'rollout-config');config.append(el('span',f.template_mode==='clone'?`New: ${f.new_template_name || f.site_name}`:f.template_name || f.template_id || 'Template not set'),el('small',`${check?.ha?'HA · ':''}WAN · ${site.wan_modes?.['0']==='static' || f.wan0_ip?'Static IP':'DHCP'}`));
      const readiness=el('td');
      if(check?.issues.length){const b=button(`${check.issues.length} ${check.issues.length===1?'issue':'issues'} to resolve`,()=>ctx.edit(i),'readiness-warning');b.title=check.issues.map(x=>`${x.field}: ${x.message}`).join('\n');readiness.append(b);}
      else readiness.append(el('span',check?(check.ha?'Ready · CSV only':'✓ Ready'):'Checking…','readiness-label'));
      const deployment=el('td');const labels={success:'✓ Completed',already_exists:'Existing site',partial:'Incomplete',template_only:'Template created',failed:'Inspect result',template_failed:'Template failed',lookup_failed:'Check blocked'};
      deployment.append(el('span',labels[result?.status] || 'No recorded result',result?.status==='success'?'result-complete':'result-label'));
      if(result){const detail=el('details');detail.append(el('summary','Details'),el('p',result.next_action || 'Inspect the report before retrying.'));deployment.append(detail);}
      tr.append(cell,name,config,readiness,deployment);rows.append(tr);
    }
    if(ctx.grouped()){
      const groups=[...new Set(indices.map(i=>country(batch[i].fields.country)))].sort((a,b)=>a.localeCompare(b));
      for(const group of groups){const members=indices.filter(i=>country(batch[i].fields.country)===group),ready=valid?members.filter(groupEligible):[],count=ready.filter(i=>selected.includes(i)).length;
        const tr=el('tr',undefined,'country-group'),td=el('td'),label=el('label'),box=el('input');td.colSpan=5;box.type='checkbox';box.checked=!!ready.length && count===ready.length;box.indeterminate=count>0 && count<ready.length;box.disabled=!ready.length || ctx.active();box.setAttribute('aria-label',`Select all ready branches in ${group}`);box.onchange=()=>select(ready,box.checked);
        label.append(box,el('span',`${group} · ${members.length} ${members.length===1?'branch':'branches'}`),el('small',`${ready.length} ready · ${members.filter(i=>selected.includes(i)).length} selected`));td.append(label);tr.append(td);rows.append(tr);members.forEach(row);
      }
    }else indices.forEach(row);
    if(!indices.length){const tr=el('tr'),td=el('td','Create branches from a reference site, import CSVs, or add a blank branch.','overview-empty');td.colSpan=5;tr.append(td);rows.append(tr);}
    const refs=$('reference-cards');refs.replaceChildren();
    batch.forEach((s,i)=>{if(!s.reference)return;const card=el('article',undefined,'reference-card');card.append(el('h2',s.fields.site_name || s.reference.name),el('p',`${s.fields.template_name || 'Template'} · ${s.vlans.length} VLANs`),button('Review reference and create branches →',()=>ctx.edit(i),'button'));refs.append(card);});
    $('overview-create').textContent=batch.some(s=>s.reference)?'Create branches':'Choose reference site';
  }
  async function refresh(force=false){
    clearTimeout(timer);const next=fingerprint();
    if(!force && (key===next || pending===next)){render();return;}
    const id=++sequence;pending=next;key='';render();
    try{const data=await ctx.api('/api/rollout/check',{batch:ctx.batch(),project_id:ctx.projectId()});
      if(id!==sequence || next!==fingerprint())return;
      checks=data.branches;results=data.results;tenant=data.tenant;key=next;error='';
      // Recorded creation outcomes must not remain selected after returning to the overview.
      let changed=false;ctx.batch().forEach(s=>{if((s.reference || blocked(s)) && String(s.fields.post)==='1'){s.fields.post='0';changed=true;}});
      if(changed){key=fingerprint();ctx.changed();}
    }catch(e){if(id===sequence){error=`Readiness unavailable. ${e.message}`;checks=[];results=[];}}
    finally{if(id===sequence){pending='';render();}}
  }
  function schedule(){clearTimeout(timer);timer=setTimeout(()=>refresh(),250);}
  $('rollout-select-ready').onclick=()=>select(ctx.batch().map((s,i)=>i).filter(groupEligible),true,true);
  $('rollout-select-all').onchange=()=>select(ctx.batch().map((s,i)=>i).filter(i=>$('rollout-select-all').checked?groupEligible(i):!ctx.batch()[i].reference),$('rollout-select-all').checked,true);
  $('rollout-group').onchange=()=>{ctx.setGrouped($('rollout-group').checked);render();};
  $('rollout-review').onclick=ctx.review;$('rollout-refresh').onclick=()=>refresh(true);
  $('overview-create').onclick=()=>{const i=ctx.batch().findIndex(s=>s.reference);if(i<0)ctx.reference();else{ctx.edit(i);$('duplicate').click();}};
  for(const [target,source] of [['reference-pull','pull-reference'],['reference-import','import'],['overview-import','import'],['reference-blank','add-site'],['overview-add','add-site'],['reference-example','example']])$(target).onclick=()=>$(source).click();
  $('help-open').onclick=()=>$('help-dialog').showModal();$('help-close').onclick=()=>$('help-dialog').close();
  return {render,refresh,schedule,reset(){sequence++;key='';pending='';checks=[];results=[];tenant=null;error='';actionSite=null;},blocked};
}};

if(typeof module!=="undefined")module.exports={selectBranches};
else window.ZtbRollout=ZtbRollout;
