"use strict";
const $ = (id) => document.getElementById(id);
let token = document.querySelector('meta[name="local-token"]').content;
let batch = [], current = -1, activeTab = "site", dirty = false, busy = false, pendingFiles = [];
let projectSaver=null,projectWorking=true;
let rollout=null,workspaceView="reference",groupByCountry=false;
let tenantSites = [], selectedReference = "", referenceBusy = false;
let zpaBusy = false;
let tenantZones = [], zoneLookupState = 'idle', zoneLookupMessage = '', zoneLookupRequest = 0;
let zoneConnectionKey = null;
const manualZoneEdits = new WeakSet();
let deploymentJob={state:'idle'},deploymentStarting=false,previewSnapshot=null,deploymentPoll=null,localReviewReady=false;
const deploymentActive=()=>deploymentStarting || ['previewing','deploying'].includes(deploymentJob.state);
let diagrams=null,diagramFinishedRun='';
let diagramLogo=null;
const deploymentSnapshot=()=>JSON.stringify({batch,diagram_logo:diagramLogo});
let branchDrafts = [], branchSource = null;
const truthy = (v) => ["1", "true", "yes", "y"].includes(String(v).toLowerCase());
const currentSite = () => batch[current];
const gatewayNameModes = new WeakMap();
function syncGatewayNames(site) {
  if(!gatewayNameModes.has(site)) {
    const name=String(site.fields.site_name || '').trim();
    const modes={};
    for(const [field,suffix] of [['gateway_name','-GW'],['gateway_name_b','-GW-B']]) {
      const value=String(site.fields[field] || '').trim();
      modes[field]=!value || value===name+suffix ? suffix : value===name ? '' : null;
    }
    gatewayNameModes.set(site,modes);
  }
  const name=String(site.fields.site_name || '').trim();
  const modes=gatewayNameModes.get(site);
  for(const field of ['gateway_name','gateway_name_b']) {
    if(field==='gateway_name_b' && !(site.ha_enabled ?? Boolean(site.fields.gateway_name_b || site.fields.wan1_interface_name)))continue;
    if(modes[field]!==null)site.fields[field]=name ? name+modes[field] : '';
  }
}
function renderGatewayNames() {
  const site=currentSite();if(!site)return;
  syncGatewayNames(site);
  for(const field of ['gateway_name','gateway_name_b'])document.querySelector(`[data-field="${field}"]`).value=site.fields[field] || '';
}
const locationNameModes = new WeakMap();
function syncLocationName(site) {
  if (!locationNameModes.has(site)) {
    const name=String(site.fields.zia_location_name || '').trim();
    locationNameModes.set(site,!name || name===String(site.fields.site_name || '').trim());
  }
  if (['new','auto'].includes(site.fields.location_type) && locationNameModes.get(site)) {
    site.fields.zia_location_name=String(site.fields.site_name || '').trim();
  }
}
function renderLocationName() {
  const site=currentSite();if(!site)return;
  syncLocationName(site);
  const eligible=['new','auto'].includes(site.fields.location_type);
  $('location-name-option').hidden=!eligible;
  $('location-use-site-name').checked=eligible && locationNameModes.get(site);
  const input=document.querySelector('[data-field="zia_location_name"]');
  input.value=site.fields.zia_location_name || '';
  input.readOnly=eligible && locationNameModes.get(site);
  input.placeholder=site.fields.location_type==='existing' ? 'Exact name of an existing location' : 'Defaults to site name for a new location';
  document.querySelectorAll('[data-location]').forEach(el=>el.hidden=site.fields.location_type==='none');
  document.querySelectorAll('[data-location-template]').forEach(el=>el.hidden=!eligible);
  const name=String(site.fields.zia_location_name || '').trim();
  const help={
    none:'This site will be deployed without a ZIA location.',
    new:name ? `On deployment, create a new ZIA location named “${name}”.` : 'On deployment, create a new ZIA location using the site name.',
    existing:name ? `On deployment, use the existing ZIA location “${name}”. Stop if no unique match is found.` : 'Enter an existing ZIA location name. Deployment stops if no unique match is found.',
    auto:name ? `On deployment, use “${name}” if it exists; otherwise, create a new ZIA location with that name.` : 'On deployment, create a new ZIA location using the site name. Enter a location name to look for an existing location first.'
  };
  $('location-choice-help').textContent=help[site.fields.location_type] || help.auto;
}
const templateNameModes = new WeakMap();
function syncTemplateName(site) {
  site.fields.template_mode ||= 'existing';
  if(!templateNameModes.has(site)) {
    const name=String(site.fields.new_template_name || '').trim();
    templateNameModes.set(site,!name || name===String(site.fields.site_name || '').trim());
  }
  if(site.fields.template_mode==='clone' && templateNameModes.get(site))site.fields.new_template_name=String(site.fields.site_name || '').trim();
}
function renderTemplateName() {
  const site=currentSite();if(!site)return;
  syncTemplateName(site);
  const clone=site.fields.template_mode==='clone';
  document.querySelector('[data-field="template_mode"]').value=site.fields.template_mode;
  $('source-template-label').textContent=clone ? 'Template to copy' : 'Existing template';
  $('new-template-field').hidden=!clone;$('template-name-option').hidden=!clone;
  $('template-use-site-name').checked=templateNameModes.get(site);
  const input=document.querySelector('[data-field="new_template_name"]');
  input.value=site.fields.new_template_name || '';input.readOnly=templateNameModes.get(site);
  const source=site.fields.template_id ? `template ID ${site.fields.template_id}` : site.fields.template_name || 'the reference template';
  const name=site.fields.new_template_name || site.fields.site_name || 'the new site name';
  $('template-choice-help').textContent=clone
    ? `When you deploy, first copy “${source}” into a new template named “${name}”, then deploy this site using that copy. Preview creates nothing. A name already in use will block deployment.`
    : `Deploy this site using “${source}”. No new template will be created.`;
}
const newSite = () => ({fields: {site_name: "", gateway_name: "", template_name: "", template_mode:"existing", city: "", country: "", wan_interface_name: "", location_type: "none", post: "0", appc_provision: "0"}, vlans: []});
const node = (tag, text, className) => {const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el;};
let interfacesConnected=false, interfaceConnectionKey=null, interfaceDebounce=null;
let ucaasCatalog=null, ucaasCatalogState='idle', ucaasCatalogMessage='';
const interfaceManualModes=new WeakMap();
const interfaceCatalog=new ZtbInterfaces.Catalog(payload=>api('/api/tenant/interfaces',payload),renderInterfaceFeedback);
function interfaceManual(object,key,value) {
  if(!interfaceManualModes.has(object))interfaceManualModes.set(object,new Set());
  const modes=interfaceManualModes.get(object);
  if(value!==undefined && modes.has(key)!==value){if(value)modes.add(key);else modes.delete(key);projectSaver?.touch();}
  return modes.has(key);
}
function interfaceState(site,role,index=0,gatewayTarget='all') {
  const entry=interfaceCatalog.get(site.fields);
  const slots=[site.interface_gateways?.[0] || 'Gateway-1',site.interface_gateways?.[1] || 'Gateway-2'];
  const selectedSlots=role==='vlan' && ['a','b'].includes(gatewayTarget) ? [slots[gatewayTarget==='b' ? 1 : 0]] :
    role==='vlan' || role==='ha' ? slots.slice(0,site.ha_enabled ? 2 : 1) : [slots[index] || `Gateway-${index+1}`];
  const excluded=role==='vlan' ? ['wan_interface_name','wan1_interface_name','vrrp_link_interface'].flatMap(k=>ZtbInterfaces.split(site.fields[k])) : [];
  return {options:ZtbInterfaces.choices(entry?.state==='ready' ? entry.data : null,role,selectedSlots,excluded),verified:entry?.state==='ready'};
}
function renderInterfaceFeedback() {
  const site=currentSite(),entry=site && interfaceCatalog.get(site.fields);
  const text=!interfacesConnected ? 'Connect to a tenant to load template interfaces. Existing assignments are kept.' :
    !site || !(site.fields.template_name || site.fields.template_id) ? 'Choose a site template to load its interfaces.' :
    !entry || entry.state==='loading' ? 'Loading template interfaces… Existing assignments are kept.' :
    entry.state==='error' ? `${entry.message} You can still enter interfaces manually.` :
    `${entry.data.template.name}${entry.data.template.platform ? ' · '+entry.data.template.platform.toUpperCase() : ''} · Interfaces ready. Existing assignments are kept.`;
  for(const id of ['interface-status','vlan-interface-status'])$(id).textContent=text;
  for(const id of ['refresh-interfaces','refresh-vlan-interfaces'])$(id).disabled=!interfacesConnected || !site || entry?.state==='loading';
  document.querySelectorAll('.interface-picker').forEach(host=>host.refreshInterfaces?.());
  renderUcaas();
}
function ensureInterfaceChoices(refresh=false) {
  const site=currentSite();
  if(interfacesConnected && site && (site.fields.template_name || site.fields.template_id))interfaceCatalog.load(site.fields,refresh);
  renderInterfaceFeedback();
}
function renderWanInterfacePickers() {
  const site=currentSite();if(!site)return;
  for(const [key,role,index,label] of [['wan_interface_name','wan',0,'Gateway A WAN interface'],['wan1_interface_name','wan',1,'Gateway B WAN interface'],['vrrp_link_interface','ha',0,'HA link interface']]) {
    const select=document.querySelector(`[data-field="${key}"]`),host=select.closest('.interface-picker');
    ZtbInterfaces.mount(host,{select,label,value:()=>site.fields[key],state:()=>interfaceState(site,role,index),
      manual:()=>interfaceManual(site,key),setManual:value=>interfaceManual(site,key,value),
      change:value=>{site.fields[key]=value;markDirty();renderUcaas();diagrams?.renderOptions();document.querySelectorAll('[data-vlan-interface],[data-additional-wan-interface]').forEach(el=>el.refreshInterfaces?.());}});
  }
}
for(const id of ['refresh-interfaces','refresh-vlan-interfaces'])$(id).onclick=()=>ensureInterfaceChoices(true);

function renderDns() {
  const site=currentSite();if(!site)return;
  $('dns-options').hidden=site.fields.dns_split!=='1';
  const list=value=>(value || '').split(/[\s,;]+/).filter(Boolean).join(', ');
  $('dns-preview-domains').textContent=list(site.fields.dns_private_domains) || 'Enter private domains above';
  $('dns-preview-private').textContent=list(site.fields.private_dns) || 'Enter Private DNS servers above';
  const wan=list(site.fields.wan_dns) || 'Enter WAN DNS servers above';
  $('dns-preview-wan-system').textContent=wan;
  $('dns-preview-wan-other').textContent=wan;
}
function ucaasSelected(site) {return (site.fields.ucaas_services ?? 'teams,zoom,webex').split(',').filter(Boolean);}
function renderUcaas() {
  const site=currentSite();if(!site)return;
  const enabled=site.fields.ucaas_local_breakout==='1';
  $('ucaas-options').hidden=!enabled;
  if(!enabled)return;
  const selected=ucaasSelected(site), primary=site.fields.wan_interface_name || '';
  document.querySelectorAll('[data-ucaas-service]').forEach(input=>input.checked=selected.includes(input.dataset.ucaasService));
  $('ucaas-primary').textContent=primary || 'Choose the site WAN above';
  const state=interfaceState(site,'wan'), alternatives=state.options.filter(o=>o.value!==primary);
  const secondary=document.querySelector('[data-field="ucaas_secondary_wan"]'), value=site.fields.ucaas_secondary_wan || '';
  secondary.replaceChildren(new Option(alternatives.length===1 ? `Automatic · ${alternatives[0].value}` : 'Automatic · use the other WAN',''));
  alternatives.forEach(o=>secondary.add(new Option(o.label,o.value)));
  if(value && !alternatives.some(o=>o.value===value))secondary.add(new Option(`${value} · verify during preview`,value));
  secondary.value=value;
  const path=site.fields.ucaas_path_selection || 'best';
  const pathLabels={best:'Best',none:'None',balanced:'Balanced'};
  document.querySelector('[data-field="ucaas_path_selection"]').value=path;
  $('ucaas-path-help').textContent={
    best:'Uses the WAN with the best link quality, based on loss, latency and jitter.',
    none:'Uses the primary WAN. Switches to the secondary if the primary goes down.',
    balanced:'Balances traffic across the primary and secondary WANs.'
  }[path] || 'Choose Best, None or Balanced.';
  $('ucaas-wan-help').textContent=site.ha_enabled ? 'HA local breakout must be verified before it can be deployed.' :
    !state.verified ? 'Connect and load the site template to identify the other WAN. Tenant preview verifies both interfaces.' :
    !primary || !state.options.some(o=>o.value===primary) ? 'Choose a valid primary WAN above.' :
    !alternatives.length ? 'UCaaS local breakout needs two distinct WAN interfaces.' :
    !value && alternatives.length>1 ? 'More than one other WAN is available. Select the secondary WAN.' :
    `Primary ${primary} · Secondary ${value || alternatives[0].value} · ${pathLabels[path] || 'Choose path selection'}. Uses each WAN’s configured next hop.`;
  $('refresh-ucaas').disabled=ucaasCatalogState==='loading' || deploymentActive();
  $('ucaas-catalog-status').textContent=ucaasCatalogState==='loading' ? 'Loading destinations…' :
    ucaasCatalogMessage || (ucaasCatalog ? `Vendor lists retrieved ${new Date(ucaasCatalog.retrieved_at).toLocaleDateString()}. Refresh before rollout if needed; lists older than 30 days block preview. IPv6 addresses are excluded.` : 'Vendor destinations have not loaded.');
  const target=$('ucaas-destinations');target.replaceChildren();
  if(ucaasCatalog)for(const service of ucaasCatalog.services.filter(s=>selected.includes(s.id))) {
    const details=node('details',undefined,'ucaas-service');
    details.append(node('summary',`${service.name} · ${service.ipv4.length} IPv4 ranges · ${service.domains.length} domains`),node('p',service.scope,'addressing-help'));
    details.append(node('p',`IPv6 ranges excluded: ${service.excluded_ipv6}`,'addressing-help'));
    for(const endpoint of service.endpoints) {
      details.append(node('h4',endpoint.label),node('p',endpoint.ports.join(' · '),'ucaas-ports'));
      const values=node('div',undefined,'ucaas-values');
      if(endpoint.ipv4.length)values.append(node('pre',endpoint.ipv4.join('\n')));
      if(endpoint.domains.length)values.append(node('pre',endpoint.domains.join('\n')));
      details.append(values);
    }
    for(const url of service.sources) {const link=node('a','Vendor source');link.href=url;link.target='_blank';link.rel='noopener noreferrer';details.append(link);}
    target.append(details);
  }
  if(ucaasCatalogState==='idle')loadUcaasCatalog();
}
async function loadUcaasCatalog(refresh=false) {
  ucaasCatalogState='loading';ucaasCatalogMessage='';renderUcaas();
  try {
    ucaasCatalog=await api(refresh ? '/api/ucaas/refresh' : '/api/ucaas/catalog',{});
    ucaasCatalogState='ready';
    if(refresh)markDirty();
  } catch(error) {ucaasCatalogState='error';ucaasCatalogMessage=error.message;}
  renderUcaas();
}
$('refresh-ucaas').onclick=()=>loadUcaasCatalog(true);
document.querySelectorAll('[data-ucaas-service]').forEach(input=>input.addEventListener('change',()=>{
  const site=currentSite();if(!site)return;
  site.fields.ucaas_services=[...document.querySelectorAll('[data-ucaas-service]:checked')].map(el=>el.dataset.ucaasService).join(',');
  markDirty();renderUcaas();
}));

function notice(message = "", tone = "info") {
  $("notice").textContent=message;$("notice").hidden=!message;
  $("notice").dataset.tone=tone;$("notice").setAttribute('role',tone==='error' ? 'alert' : 'status');
}
function markDirty() {
  dirty=true;$("export").disabled=true;localReviewReady=false;previewSnapshot=null;
  if(deploymentJob.state==='ready')deploymentJob={...deploymentJob,state:'expired',message:'Rollout edited. Preview again before deploying.'};
  renderDeployment();updateSummary();
  projectSaver?.touch();
}
function updateSummary() {
  rollout?.schedule();
  const selected = batch.filter(s => String(s.fields.post) === "1");
  const vlans = selected.reduce((n, s) => n + s.vlans.length, 0);
  $("site-count").textContent = batch.length;
  $("batch-summary").textContent = `${selected.length} site${selected.length === 1 ? "" : "s"} selected · ${vlans} VLAN${vlans === 1 ? "" : "s"}`;
  $("validate").disabled = !selected.length || busy || deploymentActive();
}
function renderList() {
  $("site-list").replaceChildren();
  batch.forEach((site, index) => {
    const button = node("button", undefined, "site-row" + (index === current ? " active" : ""));
    button.setAttribute("aria-current", index === current ? "true" : "false");
    button.append(node("strong", site.fields.site_name || "Untitled site"));
    button.append(node("small", [site.fields.city, site.fields.country].filter(Boolean).join(", ") || "Location not set"));
    button.append(node("span", `${site.vlans.length} VLANs · ${site.reference ? "Reference" : String(site.fields.post) === "1" ? "Selected" : "Not selected"}`, "site-indicator"));
    button.addEventListener("click", () => {current = index; render();projectSaver?.touch();});
    $("site-list").append(button);
  });
  updateSummary();
}
function setTab(tab) {
  activeTab = tab;
  $("site-form").hidden = tab !== "site";
  $("vlan-editor").hidden = tab !== "vlans";
  for (const key of ["site", "vlans"]) {$("tab-" + key).classList.toggle("active", key === tab); $("tab-" + key).setAttribute("aria-selected", String(key === tab));}
}
function render() {
  renderList();
  const site = currentSite();
  $("empty").hidden = Boolean(site); $("editor").hidden = !site;
  if (!site) return;
  $("editor-name").textContent = site.fields.site_name || "Untitled site";
  $("selected").disabled = Boolean(site.reference) || Boolean(rollout?.blocked(site));
  $("vlan-count").textContent = site.vlans.length;
  document.querySelectorAll("[data-field]").forEach(input => {
    const value = site.fields[input.dataset.field] ?? "";
    if (input.type === "checkbox") input.checked = truthy(value);
    else {if (input.tagName === "SELECT" && value && ![...input.options].some(o => o.value === value)) input.add(new Option(value, value)); input.value = value;}
  });
  renderLocationName();renderGatewayNames();renderTemplateName();renderDns();
  renderWanModes();renderAdditionalWans();
  renderUcaas();diagrams?.renderOptions();
  renderWanInterfacePickers();renderVlans(); setTab(activeTab);ensureInterfaceChoices();
}
function renderWanModes() {
  const site=currentSite();if(!site)return;
  site.ha_enabled ??= Boolean(site.fields.gateway_name_b || site.fields.wan1_interface_name);
  $("gateway-setup").value=site.ha_enabled ? 'ha' : 'standalone';
  $("gateway-b-panel").hidden=!site.ha_enabled;
  $("gateway-a-label").textContent=site.ha_enabled ? 'Gateway A' : 'Gateway';
  site.wan_modes ||= {};
  for(const index of ['0','1']) {
    site.wan_modes[index] ||= ['ip','mask','gw'].some(part=>String(site.fields[`wan${index}_${part}`] || '').trim()) ? 'static' : 'dhcp';
    document.querySelector(`[data-wan-mode="${index}"]`).value=site.wan_modes[index];
    document.querySelectorAll(`[data-wan-static="${index}"]`).forEach(label=>label.hidden=site.wan_modes[index]!=='static');
  }
}
function renderAdditionalWans() {
  const site=currentSite();if(!site)return;
  let wans=[];
  try {wans=JSON.parse(site.fields.additional_wans_json || '[]');if(!Array.isArray(wans))wans=[];}catch{}
  $('additional-wans').hidden=!wans.length;
  $('additional-wans-summary').textContent=`Additional WANs · ${wans.length} configured`;
  const enabled=truthy(site.fields.copy_additional_wans),host=$('additional-wan-rows');host.replaceChildren();
  const save=()=>{site.fields.additional_wans_json=JSON.stringify(wans);markDirty();diagrams?.renderOptions();};
  wans.forEach((wan,index)=>{
    const card=node('fieldset',undefined,'wan-gateway');
    card.disabled=!enabled;
    card.append(node('legend',`${site.ha_enabled ? 'Gateway '+wan.gateway_target.toUpperCase()+' · ' : ''}${wan.name}`));
    const grid=node('div',undefined,'form-grid');
    for(const [key,label] of [['interface','WAN interface'],['tag','VLAN tag'],['mode','WAN addressing'],['ip','WAN IP address'],['mask','Subnet / mask'],['gateway','Default gateway'],['dns','DNS servers']]) {
      if(key==='interface') {
        const wrapper=node('div',undefined,'interface-field'),host=node('div');
        wrapper.append(node('span',label),host);host.dataset.additionalWanInterface=String(index);
        const target=wan.gateway_target==='b' ? 1 : 0,manualKey=`additional-wan-${index}`;
        ZtbInterfaces.mount(host,{label:`Additional WAN ${index+1} interface`,value:()=>wan.interface,
          state:()=>{
            const state=interfaceState(site,'wan',target);
            const activation=site.fields[target ? 'wan1_interface_name' : 'wan_interface_name'];
            return {...state,options:state.options.filter(option=>option.value!==activation)};
          },
          manual:()=>interfaceManual(site,manualKey),setManual:value=>interfaceManual(site,manualKey,value),
          change:value=>{wan.interface=value;save();}});
        grid.append(wrapper);continue;
      }
      const wrapper=node('label');wrapper.append(node('span',label));
      const input=node(key==='mode' ? 'select' : 'input');
      if(key==='mode'){input.add(new Option('DHCP','dhcp'));input.add(new Option('Static IP','static'));}
      input.value=wan[key] || '';input.disabled=!enabled;
      input.setAttribute('aria-label',`Additional WAN ${index+1} ${label}`);
      wrapper.hidden=['ip','mask','gateway'].includes(key) && wan.mode==='dhcp';
      input.oninput=()=>{wan[key]=input.value;save();};
      if(key==='mode')input.onchange=()=>{wan.mode=input.value;save();renderAdditionalWans();};
      wrapper.append(input);grid.append(wrapper);
    }
    card.append(grid);host.append(card);
  });
}
$("gateway-setup").addEventListener('change',()=>{
  const site=currentSite();if(!site)return;
  const fields=['gateway_name_b','wan1_interface_name','wan1_ip','wan1_mask','wan1_gw','vrrp_link_interface'];
  site.ha_enabled=$("gateway-setup").value==='ha';
  if(!site.ha_enabled) {
    site.gateway_b_draft={fields:Object.fromEntries(fields.map(key=>[key,site.fields[key] || ''])),mode:site.wan_modes['1']};
    fields.forEach(key=>site.fields[key]='');site.wan_modes['1']='dhcp';
  } else if(site.gateway_b_draft) {
    Object.assign(site.fields,site.gateway_b_draft.fields);site.wan_modes['1']=site.gateway_b_draft.mode;
  }
  markDirty();render();
});
document.querySelectorAll('[data-wan-mode]').forEach(select=>select.addEventListener('change',()=>{
  const site=currentSite();if(!site)return;
  const index=select.dataset.wanMode;site.wan_modes ||= {};site.wan_modes[index]=select.value;
  if(select.value==='dhcp') {
    for(const part of ['ip','mask','gw']) {const field=`wan${index}_${part}`;site.fields[field]='';document.querySelector(`[data-field="${field}"]`).value='';}
  }
  renderWanModes();markDirty();
}));
function showView(view,remember=true) {
  workspaceView=view;
  for(const name of ['reference','overview','sites','review'])$(name+'-view').hidden=name!==view;
  const active=view==='sites' ? (currentSite()?.reference?'reference':'overview') : view;
  for(const [id,name] of [['nav-reference','reference'],['nav-sites','overview'],['nav-review','review']]) {
    $(id).classList.toggle('active',active===name);
    if(active===name)$(id).setAttribute('aria-current','page');else $(id).removeAttribute('aria-current');
  }
  $('page-label').textContent={reference:'Reference site',overview:'Rollout overview',sites:'Branch settings',review:'Review rollout'}[view];
  if(['reference','overview'].includes(view))rollout?.refresh();
  if(remember)projectSaver?.touch();
}

function addSite() {batch.push(newSite()); current = batch.length - 1; activeTab = "site"; markDirty(); showView("sites"); render(); document.querySelector('[data-field="site_name"]').focus();}

document.querySelectorAll("[data-field]").forEach(input => input.addEventListener("input", () => {
  if (!currentSite()) return;
  if(['wan_interface_name','wan1_interface_name','vrrp_link_interface'].includes(input.dataset.field))return;
  // Infer the naming choice before changing the name used for comparison.
  syncLocationName(currentSite());
  syncGatewayNames(currentSite());
  syncTemplateName(currentSite());
  const oldLocationType=currentSite().fields.location_type;
  currentSite().fields[input.dataset.field] = input.type === "checkbox" ? (input.checked ? "1" : "0") : input.value;
  if(['gateway_name','gateway_name_b'].includes(input.dataset.field)) {
    gatewayNameModes.get(currentSite())[input.dataset.field]=null;
  }
  if(input.dataset.field==='location_type' && oldLocationType!==input.value &&
     (input.value==='new' || (['none','existing'].includes(oldLocationType) && input.value==='auto')))locationNameModes.set(currentSite(),true);
  $("editor-name").textContent = currentSite().fields.site_name || "Untitled site";
  renderLocationName();renderGatewayNames();renderTemplateName();renderDns();
  markDirty(); renderList();
  renderUcaas();
  if(input.dataset.field==='appc_provision')renderVlans();
  if(input.dataset.field==='copy_additional_wans'){renderAdditionalWans();diagrams?.renderOptions();}
  if(['template_name','template_id'].includes(input.dataset.field)) {
    if(input.dataset.field==='template_name') {
      delete currentSite().fields.template_id;
      document.querySelector('[data-field="template_id"]').value='';
      renderTemplateName();
    }
    delete currentSite().interface_gateways;
    renderInterfaceFeedback();clearTimeout(interfaceDebounce);interfaceDebounce=setTimeout(ensureInterfaceChoices,400);
  }
}));
$('location-use-site-name').addEventListener('change',()=>{
  const site=currentSite();if(!site)return;
  locationNameModes.set(site,$('location-use-site-name').checked);
  renderLocationName();markDirty();
});
$('template-use-site-name').addEventListener('change',()=>{
  const site=currentSite();if(!site)return;
  templateNameModes.set(site,$('template-use-site-name').checked);
  renderTemplateName();markDirty();
});
$("site-form").addEventListener("submit", event => event.preventDefault());

const dhcpRangeModes = new WeakMap();
const vlanDnsModes = new WeakMap();
function copySite(source) {
  const copy=structuredClone(source);
  if(locationNameModes.has(source))locationNameModes.set(copy,locationNameModes.get(source));
  if(gatewayNameModes.has(source))gatewayNameModes.set(copy,{...gatewayNameModes.get(source)});
  if(templateNameModes.has(source))templateNameModes.set(copy,templateNameModes.get(source));
  source.vlans.forEach((vlan,index)=>{
    if(dhcpRangeModes.has(vlan))dhcpRangeModes.set(copy.vlans[index],dhcpRangeModes.get(vlan));
    if(vlanDnsModes.has(vlan))vlanDnsModes.set(copy.vlans[index],vlanDnsModes.get(vlan));
  });
  return copy;
}
function dhcpService(vlan) {
  const raw = String(vlan.dhcp_service || '').trim().toLowerCase().replaceAll('-', '_');
  return ({on:'inherit',no_dhcp:'off'})[raw] || raw || (vlan.dhcp_start && vlan.dhcp_end ? 'inherit' : 'off');
}
function setDhcpRange(vlan, start, end) {
  vlan.dhcp_start=start;vlan.dhcp_end=end;
  delete vlan.dhcp_range;delete vlan.range_list;
}
function initializeDhcpRange(vlan) {
  if(!dhcpRangeModes.has(vlan))dhcpRangeModes.set(vlan,'auto');
  // Preserve an inferred service before Automatic replaces the imported endpoints.
  vlan.dhcp_service=dhcpService(vlan);
  if(vlan.dhcp_service==='off')setDhcpRange(vlan,'','');
}
function updateVlanDns(vlan) {
  if(!vlanDnsModes.has(vlan)) {
    const dns=String(vlan.per_network_dns || '').trim();
    vlanDnsModes.set(vlan,!dns || dns===String(vlan.default_gateway || '').trim() ? 'gateway' : 'custom');
  }
  if(vlanDnsModes.get(vlan)==='gateway' && ['inherit','non_airgapped'].includes(dhcpService(vlan)))vlan.per_network_dns=String(vlan.default_gateway || '').trim();
}
function calculateDhcpRange(vlan) {
  const range=ZtbNetwork.automaticRange(vlan.default_gateway || '',vlan.subnet || '',vlan.interface || '');
  setDhcpRange(vlan,range.start,range.end);
  return range;
}
function automaticRangeIssues() {
  const issues=[];
  batch.forEach((site,siteIndex)=>{
    if(String(site.fields.post)!=='1')return;
    site.vlans.forEach((vlan,index)=>{
      initializeDhcpRange(vlan);
      updateVlanDns(vlan);
      if(dhcpRangeModes.get(vlan)!=='auto' || !['inherit','non_airgapped'].includes(dhcpService(vlan)))return;
      try{calculateDhcpRange(vlan);}catch(error){
        setDhcpRange(vlan,'','');
        issues.push({source:`Workspace row ${siteIndex+2} VLANs`,row:index+1,field:'dhcp_start/dhcp_end',message:error.message});
      }
    });
  });
  return issues;
}
function renderVlans() {
  renderZoneStatus();
  const vlans=currentSite().vlans;
  $('change-addressing').disabled=!vlans.length;
  $('vlan-rows').replaceChildren();$('no-vlans').hidden=vlans.length>0;
  vlans.forEach((vlan,index)=>{
    initializeDhcpRange(vlan);
    updateVlanDns(vlan);
    const card=node('section',undefined,'vlan-card');card.id=`vlan-card-${index}`;
    const zpaHint=node('p',undefined,'addressing-help');zpaHint.id=`vlan-zpa-help-${index}`;zpaHint.setAttribute('role','status');
    const connectorSettings=node('button','Go to App Connector settings','text-button');connectorSettings.type='button';
    connectorSettings.onclick=()=>{activeTab='site';render();document.querySelector('[data-field="appc_provision"]').focus();};
    function updateZpaHint() {
      const selected=truthy(vlan.zpa_include),connector=truthy(currentSite().fields.appc_provision);
      const loopback=String(vlan.interface || '').split(',').some(port=>port.trim().toLowerCase()==='lo0');
      const management=loopback || ['mgmt','mgmtzone','management','managementzone'].includes(String(vlan.zone || '').toLowerCase().replace(/[^a-z0-9]/g,''));
      const messages=[];
      if(selected && !connector)messages.push('Turn on App Connector provisioning in Site details to include this network in the ZPA application segment.');
      if(management) {
        messages.push(loopback ? `Management is supported: selecting this network includes only ${vlan.default_gateway || 'the management IP'}/32. Keep lo0 at /32 with DHCP off.` : 'Management networks can be included in the ZPA application segment.');
        messages.push('Branch copies keep the reference management IP. Set the address intended for this branch; tenant preview checks for conflicts with existing ZPA segments.');
      }
      zpaHint.textContent=messages.join(' ');zpaHint.hidden=!messages.length;
      zpaHint.classList.toggle('deployment-issue',selected && !connector);
      connectorSettings.hidden=!selected || connector;
    }
    const heading=node('div',undefined,'vlan-card-heading'),title=node('h4');
    const updateTitle=()=>{title.textContent=`${vlan.name || 'Unnamed network'} · VLAN ${vlan.tag || '—'}`;};updateTitle();
    const remove=node('button','Remove VLAN','text-button danger');remove.setAttribute('aria-label',`Remove VLAN ${index+1}`);
    remove.onclick=()=>{if(!confirm(`Remove ${vlan.name || 'this VLAN'} from the local rollout?`))return;vlans.splice(index,1);markDirty();render();};
    heading.append(title,remove);card.append(heading);
    const primary=node('div',undefined,'vlan-main-fields'),controls={};
    function field(label,key,options) {
      const wrapper=node('label',undefined,'vlan-field');wrapper.append(node('span',label));
      const input=node(options ? 'select' : 'input');
      if(options)options.forEach(([value,text])=>input.add(new Option(text,value)));
      input.setAttribute('aria-label',`VLAN ${index+1} ${key.replaceAll('_',' ')}`);
      wrapper.append(input);controls[key]=input;return wrapper;
    }
    const gatewaySpecific=String(vlan.zone || '').toLowerCase()==='management zone' || ZtbInterfaces.split(vlan.interface).includes('lo0');
    if(gatewaySpecific && (currentSite().ha_enabled || ['a','b'].includes(vlan.gateway_target))) {
      const assignment=field('Apply network to','gateway_target',[
        ['all',currentSite().ha_enabled ? 'Both gateways' : 'Gateway'],['a','Gateway A only'],['b','Gateway B only']]);
      assignment.classList.add('vlan-gateway-target');
      const target=controls.gateway_target;target.value=vlan.gateway_target || 'all';
      target.onchange=()=>{vlan.gateway_target=target.value;markDirty();renderVlans();};
      card.append(assignment);
    }
    for(const [key,label] of [['name','Name'],['tag','VLAN tag'],['default_gateway','Gateway IP'],['subnet','Subnet / mask'],['interface','Interface'],['zone','Zone']]) {
      if(key==='interface') {
        const wrapper=node('div',undefined,'vlan-field');wrapper.append(node('span',label));
        const picker=node('div');picker.dataset.vlanInterface=String(index);wrapper.append(picker);primary.append(wrapper);
        const site=currentSite();
        ZtbInterfaces.mount(picker,{multiple:true,label:`VLAN ${index+1} interfaces`,value:()=>vlan.interface,state:()=>interfaceState(site,'vlan',0,vlan.gateway_target || 'all'),
          manual:()=>interfaceManual(vlan,'interface'),setManual:value=>interfaceManual(vlan,'interface',value),
          change:value=>{vlan.interface=value;updateRange();markDirty();}});
        continue;
      }
      const zoneOptions=key==='zone' ? [['','LAN Zone (default)'],...tenantZones.map(zone=>[zone.name,zone.name])] : undefined;
      if(zoneOptions && vlan.zone && !zoneOptions.some(([value])=>value===vlan.zone))zoneOptions.push([vlan.zone,vlan.zone+' (current; not verified)']);
      const wrapper=field(label,key,zoneOptions);primary.append(wrapper);const input=controls[key];input.value=vlan[key] || '';
      input.oninput=()=>{vlan[key]=input.value;if(key==='name' && 'display_name' in vlan)vlan.display_name=input.value;updateTitle();if(['default_gateway','subnet','interface'].includes(key))updateRange();markDirty();};
      if(key==='zone') {
        const manual=node('input');manual.value=vlan.zone || '';manual.placeholder='Exact zone name';manual.hidden=!manualZoneEdits.has(vlan);
        manual.setAttribute('aria-label',`VLAN ${index+1} custom zone`);
        const edit=node('button',manual.hidden ? 'Enter a zone name' : 'Use zone list','text-button zone-entry-toggle');edit.type='button';
        input.hidden=!manual.hidden;
        edit.onclick=()=>{manual.hidden=!manual.hidden;input.hidden=!manual.hidden;edit.textContent=manual.hidden ? 'Enter a zone name' : 'Use zone list';
          if(manual.hidden){manualZoneEdits.delete(vlan);if(![...input.options].some(option=>option.value===(vlan.zone || '')))input.add(new Option(vlan.zone+' (current; not verified)',vlan.zone));input.value=vlan.zone || '';input.focus();}
          else{manualZoneEdits.add(vlan);manual.value=vlan.zone || '';manual.focus();}projectSaver?.touch();};
        manual.oninput=()=>{vlan.zone=manual.value;updateZpaHint();markDirty();};
        input.oninput=()=>{vlan.zone=input.value;manual.value=input.value;updateZpaHint();markDirty();};
        wrapper.append(manual,edit);
      }
    }
    card.append(primary);
    const dhcp=node('div',undefined,'vlan-dhcp-fields');
    dhcp.append(field('DHCP service','dhcp_service',[['off','Off'],['inherit','On / Inherit'],['non_airgapped','Non-airgapped']]));
    const service=controls.dhcp_service, currentService=dhcpService(vlan);
    if(![...service.options].some(o=>o.value===currentService))service.add(new Option(currentService,currentService));
    service.value=currentService;
    dhcp.append(field('Address range','range_mode',[['auto','Automatic'],['custom','Custom']]));
    controls.range_mode.value=dhcpRangeModes.get(vlan);
    dhcp.append(field('DHCP range start','dhcp_start'),field('DHCP range end','dhcp_end'));
    const dns=node('div',undefined,'vlan-dns-fields');
    dns.append(field('DNS source','dns_mode',[['gateway','VLAN gateway (automatic)'],['custom','Custom']]));
    dns.append(field('DNS servers','per_network_dns'));
    controls.dns_mode.value=vlanDnsModes.get(vlan);
    controls.per_network_dns.placeholder='IPv4 addresses, separated by commas';
    controls.dns_mode.onchange=()=>{vlanDnsModes.set(vlan,controls.dns_mode.value);updateRange();markDirty();};
    controls.per_network_dns.oninput=()=>{vlan.per_network_dns=controls.per_network_dns.value;markDirty();};
    const hint=node('p',undefined,'dhcp-hint');hint.setAttribute('role','status');
    function updateRange() {
      updateZpaHint();
      const enabled=['inherit','non_airgapped'].includes(dhcpService(vlan)),automatic=enabled && dhcpRangeModes.get(vlan)==='auto';
      updateVlanDns(vlan);
      controls.dns_mode.disabled=!enabled;
      controls.per_network_dns.disabled=!enabled;
      controls.per_network_dns.readOnly=vlanDnsModes.get(vlan)==='gateway';
      controls.per_network_dns.value=vlan.per_network_dns || '';
      controls.range_mode.disabled=!enabled;hint.classList.remove('range-error');
      if(automatic) {
        try {calculateDhcpRange(vlan);hint.textContent='Updates with the gateway and subnet, ending at the last usable address. Use Custom to exclude static or reserved addresses.';}
        catch(error){setDhcpRange(vlan,'','');hint.textContent=error.message;hint.classList.add('range-error');}
      } else hint.textContent=enabled ? 'Custom range is preserved when addressing changes. Review both endpoints after changing the gateway or subnet.' : 'DHCP is off. Address range fields are ignored and excluded from deployment.';
      for(const key of ['dhcp_start','dhcp_end']) {controls[key].value=vlan[key] || '';controls[key].readOnly=automatic;controls[key].disabled=!enabled;}
    }
    service.onchange=()=>{
      const previous=dhcpService(vlan);vlan.dhcp_service=service.value;
      if(service.value==='off')setDhcpRange(vlan,'','');
      else if(previous==='off'){dhcpRangeModes.set(vlan,'auto');controls.range_mode.value='auto';}
      updateRange();markDirty();
    };
    controls.range_mode.onchange=()=>{dhcpRangeModes.set(vlan,controls.range_mode.value);updateRange();markDirty();};
    for(const key of ['dhcp_start','dhcp_end'])controls[key].oninput=()=>{vlan[key]=controls[key].value;delete vlan.dhcp_range;delete vlan.range_list;markDirty();};
    card.append(dhcp,hint,dns);
    const flags=node('div',undefined,'vlan-flags');
    for(const [key,label,aria] of [['enabled','VLAN enabled',`VLAN ${index+1} enabled`],['share_over_vpn','Share over VPN',`VLAN ${index+1} share over VPN`],['zpa_include','Include in ZPA segment',`Include VLAN ${index+1} in ZPA`]]) {
      const wrapper=node('label',undefined,'check-label'),input=node('input');input.type='checkbox';input.checked=key==='enabled' && !('enabled' in vlan) ? true : truthy(vlan[key]);input.setAttribute('aria-label',aria);
      if(key==='zpa_include')input.setAttribute('aria-describedby',zpaHint.id);
      input.onchange=()=>{vlan[key]=input.checked ? 'true' : 'false';updateZpaHint();markDirty();};wrapper.append(input,node('span',label));flags.append(wrapper);
    }
    card.append(flags,zpaHint,connectorSettings);$('vlan-rows').append(card);updateRange();
  });
}
let addressingSite=null,addressingPlan=null;
const addressingSelection=new Map();
function addressingRole(vlan,site) {
  const zone=String(vlan.zone || '').toLowerCase().replace(/[^a-z0-9]/g,'');
  const ports=String(vlan.interface || '').split(',').map(port=>port.trim().toLowerCase());
  const excluded=[site.fields.wan_interface_name,site.fields.wan1_interface_name,site.fields.vrrp_link_interface].filter(Boolean).flatMap(value=>value.toLowerCase().split(',').map(port=>port.trim()));
  if(['wan','wanzone','ha','hazone','hainternal'].includes(zone) || ports.some(port=>excluded.includes(port)))return 'excluded';
  if(ports.includes('lo0') || ['mgmt','mgmtzone','management','managementzone'].includes(zone))return 'management';
  return 'lan';
}
function renderAddressingPreview() {
  addressingPlan=null;$('addressing-apply').disabled=true;$('addressing-preview').replaceChildren();
  const from=$('addressing-from').value.trim(),to=$('addressing-to').value.trim();
  const status=$('addressing-status');status.classList.remove('range-error');
  if(!from || !to){status.textContent='Enter both prefixes to preview matching VLANs.';return;}
  try{ZtbNetwork.addressPrefixes(from,to);}catch(error){status.textContent=error.message;return;}
  const changes=[],errors=[];let matches=0;
  addressingSite.vlans.forEach((vlan,index)=>{
    const role=addressingRole(vlan,addressingSite);
    if(role==='excluded')return;
    let next,error;
    try{next=ZtbNetwork.changeAddressing(vlan,from,to,dhcpRangeModes.get(vlan) || 'auto',vlanDnsModes.get(vlan));}
    catch(exc){error=exc.message;}
    if(!next && !error)return;
    matches++;
    if(!addressingSelection.has(index))addressingSelection.set(index,role!=='management');
    const selected=addressingSelection.get(index);
    const row=node('div',undefined,'addressing-row'),label=node('label',undefined,'check-label'),check=node('input');
    check.type='checkbox';check.checked=selected;check.setAttribute('aria-label',`Change addressing for VLAN ${index+1}`);
    check.onchange=()=>{addressingSelection.set(index,check.checked);renderAddressingPreview();};
    label.append(check,node('span',`${vlan.name || 'Unnamed network'} · VLAN ${vlan.tag}${role==='management' ? ' · Management (opt in)' : ''}`));row.append(label);
    const values=node('div',undefined,'addressing-values');
    const describe=(title,network)=>{
      const block=node('div');block.append(node('span',title,'addressing-value-label'),node('strong',`${network.default_gateway} / ${network.subnet}`));
      const service=dhcpService(network),range=service==='off' ? 'DHCP off' : network.dhcp_start && network.dhcp_end ? `${network.dhcp_start} – ${network.dhcp_end}` : 'No DHCP range';
      block.append(node('small',range));
      if(service!=='off')block.append(node('small',`DNS: ${network.per_network_dns || 'Site default'}`));
      return block;
    };
    values.append(describe('Current',vlan));
    if(next)values.append(describe(selected ? 'After applying' : 'Not selected',selected ? next : vlan));
    else values.append(node('p',error,'addressing-error'));
    row.append(values);$('addressing-preview').append(row);
    if(selected){if(error)errors.push(`${vlan.name || 'VLAN'}: ${error}`);else changes.push({index,next});}
  });
  const proposed=addressingSite.vlans.map((vlan,index)=>changes.find(change=>change.index===index)?.next || vlan);
  const networks=proposed.map(vlan=>{try{return ZtbNetwork.addressSubnet(vlan);}catch{return null;}});
  for(const {index} of changes) {
    const network=networks[index];
    networks.forEach((other,j)=>{
      if(j===index || !other || (changes.some(change=>change.index===j) && j<index))return;
      if(network.first<=other.last && other.first<=network.last)errors.push(`${proposed[index].name} would overlap ${proposed[j].name}. Adjust the selection or use individual edits.`);
    });
  }
  if(errors.length){status.textContent=errors.join(' ');status.classList.add('range-error');}
  else status.textContent=matches ? `${changes.length} VLAN${changes.length===1 ? '' : 's'} selected. Review the addresses, then apply. Management VLANs require selection.` : 'No VLAN gateways match this prefix. WAN and HA networks are excluded.';
  if(changes.length && !errors.length){addressingPlan={site:addressingSite,before:JSON.stringify(addressingSite.vlans),changes};$('addressing-apply').disabled=false;}
}
$('change-addressing').onclick=()=>{
  addressingSite=currentSite();addressingSelection.clear();addressingPlan=null;
  $('addressing-site').textContent=`${addressingSite.fields.site_name || 'Untitled site'} · Preview and apply a shared address prefix to selected VLANs.`;
  $('addressing-from').value='';$('addressing-to').value='';renderAddressingPreview();$('addressing-dialog').showModal();$('addressing-from').focus();
};
for(const id of ['addressing-from','addressing-to'])$(id).oninput=renderAddressingPreview;
for(const id of ['addressing-close','addressing-cancel'])$(id).onclick=()=>$('addressing-dialog').close();
$('addressing-dialog').addEventListener('close',()=>{
  if($('addressing-dialog').open)return;
  addressingPlan=null;addressingSite=null;addressingSelection.clear();
});
$('addressing-apply').onclick=()=>{
  const plan=addressingPlan;if(!plan)return;
  if(currentSite()!==plan.site || JSON.stringify(plan.site.vlans)!==plan.before){renderAddressingPreview();return;}
  for(const {index,next} of plan.changes){
    const vlan=plan.site.vlans[index];Object.assign(vlan,next);delete vlan.dhcp_range;delete vlan.range_list;
  }
  $('addressing-dialog').close();markDirty();render();
  notice(`Updated addressing for ${plan.changes.length} VLAN${plan.changes.length===1 ? '' : 's'} in ${plan.site.fields.site_name}. Review the rollout before exporting.`, 'success');
};
function editVlan(index) {
  const card=document.getElementById(`vlan-card-${index}`);
  card?.scrollIntoView({block:'center'});
  card?.querySelector('select[aria-label$="dhcp service"]')?.focus();
}
async function api(path, body) {
  if(['/api/diagrams/preview','/api/deployment/preview','/api/deployment/start','/api/export'].includes(path) && !Object.hasOwn(body,'diagram_logo'))body={...body,diagram_logo:diagramLogo};
  const send=()=>fetch(path, {method:"POST", headers:{"Content-Type":"application/json", "X-Local-Token":token}, body:JSON.stringify(body)});
  let response = await send();
  if(response.status===403 && path.startsWith('/api/projects/')) {
    // A local server restart rotates its token. Refresh it without reloading a draft.
    const page=await fetch('/',{cache:'no-store'});
    if(page.ok) {
      const fresh=new DOMParser().parseFromString(await page.text(),'text/html').querySelector('meta[name="local-token"]')?.content;
      if(fresh && fresh!==token){token=fresh;response=await send();}
    }
  }
  const data = await response.json();
  if (!response.ok) {const error = new Error(data.error || "Request failed"); error.missingFiles = data.missing_files; error.status=response.status; throw error;}
  return data;
}
async function review() {
  if(deploymentActive()){showView('review');return;}
  if (busy) return;
  busy = true; updateSummary(); notice(); $("validate").textContent = "Checking inputs…";
  try {
    const rangeIssues = automaticRangeIssues();
    const result = await api("/api/validate", {batch});
    if(rangeIssues.length){result.valid=false;result.issues.push(...rangeIssues);}
    const target = $("review-content"); target.replaceChildren();
    const ready = result.valid && result.selected_sites > 0;
    const status = node("div", undefined, "review-status" + (result.issues.length ? " invalid" : ""));
    status.append(node("h2", ready ? "Your inputs pass offline validation." : result.issues.length ? "A few details need attention." : "Select a site to continue."));
    status.append(node("p", ready ? `${result.selected_sites} selected site(s). Tenant templates, zones, and existing resources have not yet been checked for deployment.` : result.issues.length ? `${result.issues.length} input issue(s). Select an issue to return to its site.` : 'Return to New branches and choose Include in rollout for at least one branch.')); target.append(status);
    if (result.issues.length) {
      const list = node("ul", undefined, "issues");
      result.issues.forEach(issue => {
        const li = node("li"), button = node("button");
        const match = issue.source.match(/Workspace row (\d+) VLANs/);
        const siteIndex = (match ? Number(match[1]) : issue.row) - 2;
        const site = batch[siteIndex], vlan = match ? site?.vlans[issue.row-1] : null;
        const labels = {"dhcp_start/dhcp_end":"DHCP range",appc_provision:"App Connector provisioning",zpa_include:"ZPA selection",post:"Rollout selection",subnet:"Subnet prefix",tag:"VLAN tag","template_name/template_id":"Site template"};
        const label = labels[issue.field] || issue.field.replaceAll("_"," ");
        button.append(node("strong", `${label}: ${issue.message}`), node("span", `${site?.fields.site_name || "Untitled site"}${vlan ? " · "+(vlan.name || "Unnamed VLAN")+" (VLAN "+vlan.tag+")" : ""}`));
        button.addEventListener("click", () => {current = siteIndex; if (current < 0 || current >= batch.length) current = 0; activeTab = match ? "vlans" : "site"; showView("sites"); render(); if(vlan && /dhcp|zone|enabled|share_over_vpn/.test(issue.field)) editVlan(issue.row-1);});
        li.append(button); list.append(li);
      }); target.append(list);
    } else if (ready) {
      const wrapper = node("div", undefined, "review-table"), table = node("table"), head = node("thead"), headRow = node("tr"), body = node("tbody");
      ["Site", "Template action", "VLANs", "Additional WANs", "ZPA application segment", "UCaaS local breakout", "DNS policy"].forEach(label => headRow.append(node("th", label))); head.append(headRow);
      result.sites.forEach(site => {
        const row = node("tr"), name = node("td", site.name), count = node("td", String(site.vlans)), zpa = node("td");
        if (site.zpa) {
          zpa.className='review-zpa';
          zpa.append(node('span','Will be created disabled','zpa-plan-status'),node('strong',site.zpa.application_name,'zpa-plan-name'));
          const networks=node('ul',undefined,'zpa-plan-networks');
          for(const subnet of site.zpa.subnets)networks.append(node('li',subnet));
          if(site.zpa.subnets.length>3){const details=node('details');details.append(node('summary',`${site.zpa.subnets.length} networks`),networks);zpa.append(details);}
          else zpa.append(networks);
        }
        else zpa.textContent = "Not requested";
        const template=node('td',site.template.mode==='clone' ? `Create “${site.template.name}” from “${site.template.source}”, then deploy site.` : `Use “${site.template.source}”.`);
        row.append(name,template,count,node('td',(site.additional_wans || []).map(w=>`Gateway ${w.gateway_target.toUpperCase()}: ${w.interface} (${w.mode==='dhcp' ? 'DHCP' : w.ip+'/'+w.mask})`).join('; ') || 'None'),zpa,node('td',site.ucaas ? `Requested: ${site.ucaas.services} · ${site.ucaas.path_selection || 'Best'}. WANs and objects checked during tenant preview.` : 'Off'),node('td',site.dns ? `Private domains: ${site.dns.domains}. After ZPA, before all other domains → WAN DNS.` : 'Automatic defaults')); body.append(row);
      }); table.append(head,body); wrapper.append(table); target.append(wrapper);
    }
    localReviewReady=ready;$("export").disabled = !ready;showView("review");renderDeployment();refreshConnectionStatus();
  } catch (error) {localReviewReady=false;notice(error.message,'error');} finally {busy = false; $("validate").textContent = "Validate & review →"; updateSummary();renderDeployment();}
}

function renderZoneStatus() {
  $('zone-lookup-status').textContent=zoneLookupState==='loading' ? 'Loading tenant zones… Existing selections are kept.' :
    zoneLookupState==='ready' ? `${tenantZones.length} tenant zones available. Reference-site selections are kept.` :
    zoneLookupMessage || 'Connect to a tenant to browse zones, or enter an exact zone name. Existing selections are kept.';
  $('refresh-zones').disabled=zoneLookupState==='loading' || referenceBusy;
}
function clearZoneChoices() {
  zoneLookupRequest++;tenantZones=[];zoneLookupState='idle';zoneLookupMessage='';zoneConnectionKey=null;
  renderZoneStatus();if(currentSite())renderVlans();
}
async function refreshZoneChoices() {
  const request=++zoneLookupRequest;zoneLookupState='loading';renderZoneStatus();
  try {
    const result=await api('/api/tenant/zones',{});
    if(request!==zoneLookupRequest)return;
    tenantZones=result.zones;zoneLookupState='ready';zoneLookupMessage='';
  } catch(error) {
    if(request!==zoneLookupRequest)return;
    tenantZones=[];zoneLookupState='error';zoneLookupMessage=`${error.message} Existing VLAN zones are kept; you can still enter a zone name.`;
  }
  renderZoneStatus();if(currentSite())renderVlans();
}
$('refresh-zones').onclick=refreshZoneChoices;
async function refreshConnectionStatus() {
  try {
    const result=await api('/api/connections',{});
    rollout?.refresh(true);
    $('connection-summary').textContent=result.tenant ? `ZTB: ${result.tenant} · ZPA: ${result.zpa_verified ? 'connection checked' : 'checked during preview if requested'}` : 'Connect to a tenant to preview deployment.';
    const key=result.tenant ? `${result.tenant}:${result.revision}` : null;
    interfacesConnected=Boolean(result.tenant);
    if(key!==interfaceConnectionKey){interfaceConnectionKey=key;interfaceCatalog.reset();ensureInterfaceChoices();}
    if(key!==zoneConnectionKey) {clearZoneChoices();zoneConnectionKey=key;if(key)refreshZoneChoices();}
  } catch(error){$('connection-summary').textContent=error.message;}
}
function renderDeployment() {
  const active=deploymentActive(),ready=deploymentJob.state==='ready' && previewSnapshot===deploymentSnapshot();
  const currentRun=deploymentJob.id && (previewSnapshot===deploymentSnapshot() || !batch.length);
  $('review-content').hidden=Boolean(currentRun && !['idle','expired'].includes(deploymentJob.state));
  $('csv-handoff').hidden=!batch.length;
  $('deployment-title').textContent=deploymentJob.state==='deploying' ? 'Deployment in progress' : ['completed','incomplete','interrupted'].includes(deploymentJob.state) ? 'Deployment results' : deploymentJob.state==='ready' ? 'Ready to deploy' : 'Deploy your rollout';
  for(const name of ['sites','overview','reference'])$(name+'-view').inert=active || projectWorking || !projectSaver?.project;
  $('project-bar').inert=active || projectWorking;
  $('back').disabled=active;$('nav-sites').disabled=active;$('nav-reference').disabled=active || projectWorking || !projectSaver?.project;
  $('deployment-connections').disabled=active;$('export').disabled=active || !localReviewReady;
  $('preview-deployment').disabled=active || busy || !localReviewReady;
  $('preview-deployment').hidden=!batch.length;
  $('preview-deployment').classList.toggle('primary',deploymentJob.state!=='ready');
  $('preview-deployment').textContent=deploymentJob.state==='previewing' ? 'Checking tenant…' : 'Preview deployment';
  $('start-deployment').hidden=deploymentJob.state!=='ready';$('start-deployment').disabled=!ready || active;
  $('start-deployment').textContent=`Deploy ${deploymentJob.site_count || 0} site${deploymentJob.site_count===1 ? '' : 's'}`;
  $('deployment-report').hidden=!deploymentJob.report || active;
  $('deployment-message').textContent=deploymentJob.message || 'Preview checks the tenant without creating resources.';
  if(deploymentJob.state==='ready' && !ready)$('deployment-message').textContent='The workspace changed or was reloaded. Restore your inputs and preview again before deploying.';
  if(deploymentJob.tenant)$('connection-summary').textContent=`Destination: ${deploymentJob.tenant}${deploymentJob.zpa_cloud ? ' · ZPA: '+deploymentJob.zpa_cloud+(deploymentJob.zpa_customer ? ' · Customer '+deploymentJob.zpa_customer : '') : ''}`;
  const target=$('deployment-results');target.replaceChildren();
  for(const issue of deploymentJob.issues || [])target.append(node('p',`${issue.row ? 'Row '+issue.row+' · ' : ''}${issue.message}`,'deployment-issue'));
  const sites=deploymentJob.sites?.length ? deploymentJob.sites : (deploymentJob.details || []).map(detail=>({name:detail.name,status:active ? 'pending' : 'preview',stages:{}}));
  for(const site of (deploymentJob.project_id && deploymentJob.project_id!==projectSaver?.project?.id ? [] : sites)) {
    const row=node('div',undefined,'deployment-site'),heading=node('div',undefined,'deployment-site-heading');
    const labels={preview:'Ready',success:'Configuration created',partial:'Incomplete',failed:'Failed',already_exists:'Already exists',lookup_failed:'Lookup failed',pending:'Pending',template_failed:'Template blocked',template_only:'Template created · site incomplete'};
    const tone=site.status==='success' ? 'success' : ['failed','lookup_failed','template_failed'].includes(site.status) ? 'error' : ['partial','already_exists','template_only'].includes(site.status) ? 'warning' : 'info';
    const badge=node('span',labels[site.status] || site.status,'pill');badge.dataset.tone=tone;
    heading.append(node('strong',site.name),badge);row.append(heading);
    if(site.artifacts)row.append(diagrams.actions(site.artifacts,site.name));
    if(site.diagram_warning)row.append(node('p',site.diagram_warning,'deployment-warning'));
    if(site.status==='success')row.append(node('p','Configuration created. Appliance activation and tunnel health have not been tested.','addressing-help'));
    const configuration=node('details',undefined,'deployment-detail');configuration.append(node('summary','Configuration details'));
    configuration.open=['partial','failed','template_only','template_failed'].includes(site.status);
    const summaryChildren=row.children.length;
    const stages={...(deploymentJob.stage_progress?.[site.name] || {}),...(site.stages || {})};
    if(Object.keys(stages).length) {
      const list=node('ul',undefined,'deployment-stages');
      for(const [name,state] of Object.entries(stages)) {
        const pendingBinding=name==='Loopback binding' && (state===false || state==='failed');
        const done=state===true || state==='success',failed=state===false || state==='failed';
        const item=node('li',undefined,'stage-result');
        item.dataset.tone=pendingBinding ? 'warning' : done ? 'success' : failed ? 'error' : 'info';
        item.append(node('span',done ? '✓' : pendingBinding ? '!' : failed ? '×' : '·','stage-symbol'),
          node('span',`${name}: ${pendingBinding ? 'needs verification' : done ? 'completed' : failed ? 'failed' : state}`));
        list.append(item);
      }
      row.append(list);
    }
    const detail=(deploymentJob.details || []).find(value=>value.name===site.name);
    if(detail) {
      row.append(node('p',detail.template_clone ? `First create template “${detail.template_clone.name}” from “${detail.template_clone.source_name}”, then deploy this site using the new template.` : `Use existing template “${detail.template}”.`,'addressing-help'));
      row.append(node('p',`${detail.vlans} VLANs · App Connector: ${detail.app_connector ? 'create' : 'not requested'}${detail.segment ? ' · Disabled ZPA segment: '+detail.segment : ''}`,'addressing-help'));
    }
    if(site.template?.id)row.append(node('p',`Created template: ${site.template.name} · ID: ${site.template.id}`,'addressing-help'));
    if(site.segment?.subnets?.length)row.append(node('p',`ZPA segment destinations: ${site.segment.subnets.join(', ')}`,'addressing-help'));
    const extraWans=site.additional_wans?.requested || detail?.additional_wans || [];
    if(extraWans.length){
      row.append(node('h4','Additional WANs'));
      for(const wan of extraWans)row.append(node('p',`Gateway ${wan.gateway_target.toUpperCase()} · ${wan.interface} · ${wan.mode==='dhcp' ? 'DHCP' : wan.ip+'/'+wan.mask+' · Next hop '+wan.gateway}`,'addressing-help'));
    }
    const dns=site.dns?.domains ? site.dns : detail?.dns;
    if(dns?.domains) {
      row.append(node('h4','Private-domain DNS'),node('p',dns.domains.join(', '),'addressing-help'));
      const order=node('ol',undefined,'dns-rule-review');
      for(const rule of dns.order || [])order.append(node('li',rule));
      row.append(order,node('p',`Private DNS: ${dns.private_dns.join(', ')}. WAN DNS: ${dns.wan_dns.join(', ')}. Site WAN DNS settings will be configured and verified.`,'addressing-help'));
      if(dns.domain_object)row.append(node('p',`${dns.domain_object.status || dns.domain_object.action}: ${dns.domain_object.name}${dns.domain_object.id ? ' · ID '+dns.domain_object.id : ''}`,'addressing-help'));
      for(const policy of dns.policies || [])row.append(node('p',`${policy.position ? policy.position+'. ' : ''}${policy.name} · ID ${policy.id} · ${policy.status}`,'addressing-help'));
    }
    const breakout=site.ucaas?.objects?.length ? site.ucaas : detail?.ucaas || site.ucaas;
    if(breakout?.primary) {
      row.append(node('p',`UCaaS local breakout · Primary ${breakout.primary} · Secondary ${breakout.secondary} · ${breakout.path_selection || 'Best'} · ${breakout.rules?.length || 0} rules · IPv4 address objects only`,'addressing-help'));
      row.append(node('p',breakout.rule_layout==='web_media' ? 'Two shared rules combine web ports across selected destinations and media ports across media destinations. All selected vendor ports are retained. Both rules bypass ZIA and sit above template forwarding rules.' : 'Selected destination and port combinations go directly to the internet, bypassing ZIA. Site rules are placed above template forwarding rules.','addressing-help'));
      const rules=node('ul',undefined,'ucaas-rule-review');
      const plannedObjects=new Map((breakout.objects || []).map(object=>[object.key,object]));
      for(const rule of breakout.rules || []) {
        const item=node('li'), destinations=rule.destination_keys.map(key=>plannedObjects.get(key)?.name || key);
        item.append(node('strong',rule.name),node('p',rule.ports.join(' · '),'ucaas-ports'));
        const pairing=node('details');pairing.append(node('summary','Destination and Port objects'),node('p',destinations.join(' + ')),node('p',plannedObjects.get(rule.port_key)?.name || rule.port_key));item.append(pairing);rules.append(item);
      }
      row.append(rules,node('h4','Reusable objects'));
      const objects=node('ul',undefined,'ucaas-object-review');
      for(const object of breakout.objects || []) {
        const item=node('li'), description=`${object.status || object.action}: ${object.name}${object.id ? ' · ID '+object.id : ''}${object.values ? ' · '+object.values.length+(object.type==='l4port' ? ' protocol/port sets' : ' destinations') : ''}`;
        if(object.values) {const destinations=node('details');destinations.append(node('summary',description),node('pre',object.values.join('\n')));item.append(destinations);}
        else item.textContent=description;
        objects.append(item);
      }
      row.append(objects);
      for(const policy of site.ucaas?.policies || [])row.append(node('p',`${policy.display_name} · ${policy.id ? 'ID '+policy.id : 'ID unconfirmed'} · ${policy.status}`,'addressing-help'));
    }
    while(row.children.length>summaryChildren)configuration.append(row.children[summaryChildren]);
    for(const [stage,message] of Object.entries(site.diagnostics || {}))row.append(node('p',`${stage}: ${message}`,stage==='Loopback binding' ? 'deployment-warning' : 'deployment-issue'));
    if(site.next_action)row.append(node('p',site.next_action,'addressing-help'));
    row.append(configuration);target.append(row);
  }
  if(deploymentJob.report && !active && diagramFinishedRun!==deploymentJob.report){diagramFinishedRun=deploymentJob.report;diagrams?.refresh();}
  if(deploymentJob.progress)$('deployment-message').textContent=`${deploymentJob.progress.site} · ${deploymentJob.progress.stage}: ${deploymentJob.progress.state}`;
}
async function pollDeployment(restore=false) {
  clearTimeout(deploymentPoll);
  try {
    const previous=deploymentJob.state;
    deploymentJob=await api('/api/deployment/status',{});renderDeployment();updateSummary();
    if(previous!==deploymentJob.state && !deploymentActive())rollout?.refresh(true);
    if(restore && deploymentJob.state!=='idle')showView('review');
    if(deploymentActive()){showView('review');deploymentPoll=setTimeout(pollDeployment,1000);}
  } catch(error) {
    $('deployment-message').textContent='Connection to the local app was lost. Keep it running; reconnecting will not start another deployment.';
    if(deploymentActive())deploymentPoll=setTimeout(pollDeployment,2000);
  }
}
$('deployment-connections').onclick=()=>openReference();
$('preview-deployment').onclick=async()=>{
  if(deploymentActive() || busy)return;
  await review();if(!localReviewReady)return;
  deploymentStarting=true;renderDeployment();
  try {previewSnapshot=deploymentSnapshot();deploymentJob=await api('/api/deployment/preview',{batch,project_id:projectSaver?.project?.id});}
  catch(error){deploymentJob={state:'blocked',message:error.message};previewSnapshot=null;}
  finally{deploymentStarting=false;renderDeployment();updateSummary();}
  if(deploymentActive())pollDeployment();
};
$('start-deployment').onclick=async()=>{
  if(deploymentActive() || deploymentJob.state!=='ready' || previewSnapshot!==deploymentSnapshot())return;
  if(!confirm(`Deploy ${deploymentJob.site_count} selected site(s) to ${deploymentJob.tenant}? This creates tenant resources.`))return;
  deploymentStarting=true;renderDeployment();
  try{deploymentJob=await api('/api/deployment/start',{preview_id:deploymentJob.id,batch,project_id:projectSaver?.project?.id});}
  catch(error){
    if(error.status){deploymentJob={...deploymentJob,state:'expired',message:error.message};previewSnapshot=null;}
    else deploymentJob={...deploymentJob,state:'deploying',message:'Checking whether deployment started. Do not submit another run.'};
  }
  finally{deploymentStarting=false;renderDeployment();updateSummary();}
  if(deploymentActive())pollDeployment();
};
$('deployment-report').onclick=async()=>{
  try {
    const report=await api('/api/deployment/report',{}),url=URL.createObjectURL(new Blob([report.content],{type:'text/plain'}));
    const link=node('a');link.href=url;link.download=report.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
  }catch(error){$('deployment-message').textContent=error.message;}
};

$("add-site").onclick = addSite; $("empty-add").onclick = addSite;
$("tab-site").onclick = () => {setTab("site");projectSaver?.touch();}; $("tab-vlans").onclick = () => {setTab("vlans");projectSaver?.touch();};
$("nav-sites").onclick = () => showView("overview"); $("back").onclick = () => showView("overview");
$("editor-overview").onclick=()=>showView("overview");
$("nav-review").onclick = review; $("validate").onclick = review;
$("remove-site").onclick = () => {if (!confirm("Remove this site from the local rollout? This does not change your tenant.")) return; batch.splice(current,1); current = Math.min(current,batch.length-1); markDirty(); render();};
$("duplicate").onclick = () => {
  branchSource=copySite(currentSite());branchDrafts=[];$("branch-count").value="1";
  $('branches-template-mode').value=branchSource.fields.template_mode || 'existing';
  $("branches-source").textContent=`From ${branchSource.fields.site_name || "Untitled site"} · ${branchSource.vlans.length} VLANs · ${branchSource.fields.template_name || "Template ID "+(branchSource.fields.template_id || "not set")}`;
  renderBranchDrafts();$("branches-dialog").showModal();
};
function renderBranchDrafts() {
  globalThis.CountryPicker?.close();
  const source=branchSource.fields.template_name || branchSource.fields.template_id || 'the reference template';
  $('branches-template-help').textContent=$('branches-template-mode').value==='clone'
    ? `Each branch gets its own copy of “${source}”, named exactly like that branch. On deployment, its template is created first, then the site. Creating drafts here does not create tenant resources.`
    : `All branches will use “${source}”. You can choose a separate template in each branch’s site details.`;
  $('branches-location-help').textContent=branchSource.fields.location_type==='none'
    ? 'These copies keep No ZIA location. You can change this in each branch’s site details before deployment.'
    : 'Each branch defaults to Create a new location, using its site name. You can change this in site details. Locations are created only when you deploy.';
  const count=Number($("branch-count").value);
  if(!Number.isInteger(count) || count<1 || count>100)return;
  const names=new Set([...batch.map(s=>String(s.fields.site_name || "").toLowerCase()),...branchDrafts.map(s=>s.site_name.toLowerCase())]);
  while(branchDrafts.length<count) {
    let number=branchDrafts.length+1, name;
    do{name=`${branchSource.fields.site_name || "Branch"}-copy-${String(number++).padStart(2,"0")}`;}while(names.has(name.toLowerCase()));
    names.add(name.toLowerCase());
    branchDrafts.push({site_name:name,gateway_name:`${name}-GW`,city:branchSource.fields.city || "",country:branchSource.fields.country || ""});
  }
  branchDrafts.length=count;$("branch-rows").replaceChildren();
  branchDrafts.forEach((draft,index)=>{
    const row=node("tr");
    const controls={};
    for(const field of ["site_name","gateway_name","city","country"]) {
      const cell=node("td"),input=node("input");input.value=draft[field];input.required=["site_name","gateway_name"].includes(field);
      if(field==='country') {input.setAttribute('list','country-options');input.setAttribute('autocomplete','off');input.placeholder='Type or choose a country';}
      controls[field]=input;
      input.setAttribute("aria-label",`Branch ${index+1} ${field.replaceAll("_"," ")}`);
      input.oninput=()=>{
        const generated=draft.gateway_name===`${draft.site_name.trim()}-GW` || !draft.gateway_name;
        draft[field]=input.value;
        if(field==='site_name' && generated) {
          draft.gateway_name=draft.site_name.trim() ? `${draft.site_name.trim()}-GW` : '';
          controls.gateway_name.value=draft.gateway_name;
        }
      };cell.append(input);row.append(cell);
      if(field==='country') globalThis.CountryPicker?.attach(input);
    }
    $("branch-rows").append(row);
  });
  $("create-branches").textContent=`Create ${count} branch${count===1 ? "" : "es"}`;
}
$("branch-count").onchange=renderBranchDrafts;
$('branches-template-mode').onchange=renderBranchDrafts;
$("branches-close").onclick=()=>$("branches-dialog").close();
$("branches-form").onsubmit=event=>{
  event.preventDefault();
  if(!branchDrafts.length)return;
  const first=batch.length;
  for(const draft of branchDrafts) {
    const copy=copySite(branchSource);delete copy.reference;
    Object.assign(copy.fields,draft,{post:"0"});
    gatewayNameModes.delete(copy);
    copy.fields.template_mode=$('branches-template-mode').value || branchSource.fields.template_mode || 'existing';
    delete copy.fields.new_template_name;
    templateNameModes.set(copy,true);syncTemplateName(copy);
    if(copy.fields.location_type!=='none') {
      copy.fields.location_type='new';
      locationNameModes.set(copy,true);syncLocationName(copy);
    }
    if(copy.ha_enabled || copy.fields.gateway_name_b)copy.fields.gateway_name_b=`${draft.gateway_name}-B`;
    batch.push(copy);
  }
  current=first;activeTab="site";markDirty();render();$("branches-dialog").close();
  if(typeof showView==='function')showView("overview");
  notice(`${branchDrafts.length} branch ${branchDrafts.length===1 ? "copy" : "copies"} ready. Review addressing and ZPA choices, then select branches in the rollout overview.`, 'success');
};
$("add-vlan").onclick = () => {
  currentSite().vlans.push({name:"",tag:"",subnet:"24",default_gateway:"",interface:"ge2",zone:"LAN Zone",enabled:"true",share_over_vpn:"false",dhcp_service:"off",zpa_include:"0"});
  markDirty(); render(); setTab("vlans");
};
$("example").onclick = () => {
  if (batch.length && !confirm("Replace this local rollout with the example? Export any work you want to keep first.")) return;
  batch = [{fields:{site_name:"Utrecht-NL-BR",gateway_name:"Utrecht-NL-BR-GW",template_name:"Your-Site-Template",city:"Utrecht",country:"Netherlands",wan_interface_name:"ge5",wan_dns:"1.1.1.1,8.8.8.8",location_type:"none",post:"1",appc_provision:"1"},vlans:[
    {name:"Printers",tag:"20",subnet:"24",default_gateway:"10.20.0.1",interface:"ge2",zone:"LAN Zone",enabled:"true",share_over_vpn:"false",dhcp_service:"off",zpa_include:"1"},
    {name:"Servers",tag:"30",subnet:"24",default_gateway:"10.30.0.1",interface:"ge2",zone:"LAN Zone",enabled:"true",share_over_vpn:"false",dhcp_service:"off",zpa_include:"1"},
    {name:"Guest",tag:"40",subnet:"24",default_gateway:"10.40.0.1",interface:"ge2",zone:"Guest-Zone",enabled:"true",share_over_vpn:"false",dhcp_service:"off",zpa_include:"0"}]}];
  current = 0; activeTab = "site"; markDirty(); render(); showView("sites"); notice("Example data loaded. Replace the template, interfaces, and addresses before deployment.", 'warning');
};
$("import").onclick = () => $("csv-import-dialog").showModal();
$("csv-import-close").onclick = () => $("csv-import-dialog").close();
$("csv-import-choose").onclick = () => {pendingFiles = []; $("import-pending").hidden = true; $("csv-import-dialog").close(); notice("Choose sites.csv, then the matching VLAN CSVs when prompted."); $("file-input").click();};
document.querySelectorAll('[data-csv-templates]').forEach(button=>button.onclick=()=>$('csv-import-dialog').showModal());
document.querySelectorAll('[data-csv-download]').forEach(button=>button.onclick=async()=>{
  button.disabled=true;
  try {
    const result=await api('/api/csv-templates',{mode:button.dataset.csvDownload});
    const bytes=Uint8Array.from(atob(result.content),c=>c.charCodeAt(0));
    const url=URL.createObjectURL(new Blob([bytes],{type:'application/zip'}));
    const link=node('a');link.href=url;link.download=result.filename;link.click();
    setTimeout(()=>URL.revokeObjectURL(url),10000);
    $('csv-template-status').textContent=`${button.dataset.csvDownload==='ha' ? 'HA' : 'Standalone'} download started. Extract the ZIP and open READ-ME-FIRST.txt for column definitions.`;
    if(!$('csv-import-dialog').open)$('csv-import-dialog').showModal();
  } catch(error) { $('csv-template-status').textContent=error.message;if(!$('csv-import-dialog').open)$('csv-import-dialog').showModal(); }
  finally {button.disabled=false;}
});
$("import-more").onclick = () => $("file-input").click();
$("import-cancel").onclick = () => {pendingFiles = []; $("import-pending").hidden = true; notice("Import canceled. Your current rollout is unchanged.");};
$("file-input").onchange = async (event) => {
  const files = [...event.target.files]; if (!files.length) return;
  try {
    if (files.reduce((n,f) => n+f.size,0) > 3*1024*1024) throw new Error("Choose CSV files smaller than 3 MB in total.");
    const additions = await Promise.all(files.map(async f => ({name:f.name,content:await f.text()})));
    const merged = [...pendingFiles.filter(f => !additions.some(a => a.name === f.name)), ...additions];
    if (new Blob([JSON.stringify(merged)]).size > 3*1024*1024) throw new Error("Choose CSV files smaller than 3 MB in total.");
    pendingFiles = merged;
    const data = await api("/api/import", {files:pendingFiles});
    if (batch.length && !confirm("Replace the current local rollout with these files? Export first if you want to keep your changes.")) return;
    pendingFiles = []; $("import-pending").hidden = true;
    if(Object.hasOwn(data,'diagram_logo'))diagramLogo=data.diagram_logo;
    batch = data.batch; current = batch.length ? 0 : -1; activeTab="site"; markDirty(); render(); showView("sites"); notice(`Imported ${batch.length} site(s). Review which sites are selected before exporting.`, 'success');
  } catch(error) {
    if (error.missingFiles) {$("import-message").textContent = error.message; $("import-pending").hidden = false; notice();}
    else {notice(error.message,'error'); pendingFiles = []; $("import-pending").hidden = true;}
  } finally {event.target.value="";}
};
$("export").onclick = async () => {
  $("export").disabled = true;
  try {
    const result = await api("/api/export",{batch});
    const bytes = Uint8Array.from(atob(result.content), c => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes],{type:"application/zip"}));
    const link = node("a"); link.href=url; link.download=result.filename; link.click(); setTimeout(() => URL.revokeObjectURL(url),10000);
    notice("CSV bundle download started with sites.csv and a VLAN folder. No tenant resources were created.", 'success');
  } catch(error) {notice(error.message,'error');} finally {$("export").disabled=false;}
};
window.addEventListener("beforeunload", event => {if (projectSaver?.dirty || projectWorking && batch.length) {event.preventDefault(); event.returnValue="";}});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')projectSaver?.flush().catch(()=>{});});

function referenceError(message="") {$("reference-error").textContent=message; $("reference-error").hidden=!message;}
function referenceWorking(working) {
  referenceBusy=working;
  for (const id of ["connect-tenant","connect-saved","change-connection"]) $(id).disabled=working;
  $("reference-close").disabled=working || zpaBusy;
  $("connect-tenant").textContent=working ? "Connecting…" : "Connect to tenant";
  $("load-reference").disabled=working || !selectedReference;
  $("load-reference").textContent=working ? "Loading…" : "Pull selected site";
}
function openReference() {referenceError(); $("reference-dialog").showModal();}
function renderReferences() {
  const query=$("reference-search").value.trim().toLowerCase();
  const sites=tenantSites.filter(s=>[s.name,s.city,s.country,s.template].join(" ").toLowerCase().includes(query));
  $("reference-list").replaceChildren();
  for(const site of sites) {
    const button=node("button",undefined,"reference-row"+(selectedReference===site.id ? " active" : ""));
    button.setAttribute("aria-pressed",String(selectedReference===site.id));
    button.append(node("strong",site.name),node("span",[site.city,site.country].filter(Boolean).join(", ") || "Location not set"),node("small",site.template || "Template not available"));
    button.onclick=()=>{if(referenceBusy)return;selectedReference=site.id;renderReferences();$("load-reference").disabled=false;};
    $("reference-list").append(button);
  }
  if(!sites.length)$("reference-list").append(node("p",tenantSites.length ? "No sites match your search." : "No sites were returned by this tenant.","muted-empty"));
}
async function connectReference(connection) {
  if(referenceBusy)return;
  referenceWorking(true);referenceError();
  clearZoneChoices();
  interfacesConnected=false;interfaceConnectionKey=null;interfaceCatalog.reset();renderInterfaceFeedback();
  try {
    const result=await api("/api/tenant/connect",connection ? {connection} : {});
    tenantSites=result.sites;selectedReference="";$("tenant-key").value="";
    $("connected-tenant").textContent=result.tenant;$("reference-limit").hidden=!result.limited;
    $("reference-search").value="";$("connection-fields").hidden=true;$("reference-picker").hidden=false;
    renderReferences();previewSnapshot=null;refreshConnectionStatus();pollDeployment();
  } catch(error) {tenantSites=[];selectedReference="";referenceError(error.message);}
  finally {referenceWorking(false);renderZoneStatus();}
}
for(const id of ["pull-reference","empty-reference"])$(id).onclick=openReference;
$("nav-reference").onclick=()=>showView("reference");
$("reference-close").onclick=()=>$("reference-dialog").close();
$("reference-dialog").addEventListener("cancel",event=>{if(referenceBusy || zpaBusy)event.preventDefault();});
$("reference-search").oninput=renderReferences;
$("connection-form").onsubmit=event=>{event.preventDefault();connectReference({tenant_url:$("tenant-url").value,api_key:$("tenant-key").value});};
$("connect-saved").onclick=()=>connectReference();
$("change-connection").onclick=()=>{$("connection-fields").hidden=false;$("reference-picker").hidden=true;referenceError();};
async function connectZpa(connection) {
  if(zpaBusy)return;
  zpaBusy=true;$("zpa-connection-fields").disabled=true;$("reference-close").disabled=true;
  $("connect-zpa").textContent="Checking…";
  $("zpa-connection-status").hidden=true;$("zpa-connection-error").hidden=true;
  try {
    const result=await api('/api/zpa/connect',connection ? {connection} : {});
    $("zpa-connection-status").textContent=`Authentication and certificate verified · ${result.cloud} · Customer ${result.customer_id} · Certificate ${result.certificate}. Resource-creation permissions have not been verified.`;
    $("zpa-connection-status").hidden=false;
    previewSnapshot=null;refreshConnectionStatus();pollDeployment();
  } catch(error) {
    $("zpa-connection-error").textContent=error.message;$("zpa-connection-error").hidden=false;
  } finally {
    $("zpa-client-secret").value="";
    zpaBusy=false;$("zpa-connection-fields").disabled=false;$("reference-close").disabled=referenceBusy;
    $("connect-zpa").textContent="Check ZPA connection";
  }
}
$("zpa-connection-form").onsubmit=event=>{
  event.preventDefault();
  connectZpa({base_url:$("zpa-url").value,client_id:$("zpa-client-id").value,client_secret:$("zpa-client-secret").value,
    customer_id:$("zpa-customer-id").value,enrollment_cert_name:$("zpa-certificate").value});
};
$("connect-zpa-saved").onclick=()=>connectZpa();
$("zpa-connection-form").oninput=()=>{$("zpa-connection-status").hidden=true;$("zpa-connection-error").hidden=true;};
$("load-reference").onclick=async()=>{
  if(referenceBusy || !selectedReference)return;
  referenceWorking(true);referenceError();
  try {
    const result=await api("/api/tenant/pull",{site_id:selectedReference});
    batch.push(result.site);current=batch.length-1;activeTab="site";markDirty();render();showView("sites");
    $("reference-dialog").close();
    notice(`Reference loaded with ${result.site.vlans.length} VLANs. Choose Create branches to prepare your rollout.${result.loopbacks ? " Loopback VLANs are prepared as /32 with DHCP off." : ""}${result.warnings?.length ? ' '+result.warnings.join(' ') : ''}`, result.warnings?.length ? 'info' : 'success');
  }catch(error){referenceError(error.message);}finally{referenceWorking(false);}
};
function projectSnapshot() {
  const saved=batch.map(site=>{
    const copy=structuredClone(site);
    // WeakMap choices affect future edits and must survive reopening the draft.
    copy.editor={
      gateway_names:gatewayNameModes.get(site),
      location_use_site_name:locationNameModes.get(site),
      template_use_site_name:templateNameModes.get(site),
      interface_manual:[...(interfaceManualModes.get(site) || [])],
      vlans:site.vlans.map(vlan=>({range_mode:dhcpRangeModes.get(vlan),dns_mode:vlanDnsModes.get(vlan),
        interface_manual:interfaceManualModes.get(vlan)?.has('interface') || false,zone_manual:manualZoneEdits.has(vlan)}))
    };
    return copy;
  });
  return {name:$('project-name').value.trim() || 'Untitled rollout',workspace:{version:1,batch:saved,current,active_tab:activeTab,rollout_view:workspaceView,group_by_country:groupByCountry,...(diagramLogo ? {diagram_logo:diagramLogo} : {})}};
}
function restoreProjectWorkspace(workspace) {
  rollout?.reset();
  batch=workspace.batch;current=workspace.current;activeTab=workspace.active_tab;
  groupByCountry=workspace.group_by_country===true;
  diagramLogo=workspace.diagram_logo || null;
  for(const site of batch) {
    const editor=site.editor || {};delete site.editor;
    const modes=editor.gateway_names;
    if(modes && ['gateway_name','gateway_name_b'].every(key=>modes[key]===null || ['', '-GW','-GW-B','-ZT','-ZT-B'].includes(modes[key])))gatewayNameModes.set(site,modes);
    if(typeof editor.location_use_site_name==='boolean')locationNameModes.set(site,editor.location_use_site_name);
    if(typeof editor.template_use_site_name==='boolean')templateNameModes.set(site,editor.template_use_site_name);
    if(Array.isArray(editor.interface_manual))interfaceManualModes.set(site,new Set(editor.interface_manual.filter(key=>['wan_interface_name','wan1_interface_name','vrrp_link_interface'].includes(key))));
    site.vlans.forEach((vlan,index)=>{
      const modes=Array.isArray(editor.vlans) ? editor.vlans[index] || {} : {};
      if(['auto','custom'].includes(modes.range_mode))dhcpRangeModes.set(vlan,modes.range_mode);
      if(['gateway','custom'].includes(modes.dns_mode))vlanDnsModes.set(vlan,modes.dns_mode);
      if(modes.interface_manual===true)interfaceManualModes.set(vlan,new Set(['interface']));
      if(modes.zone_manual===true)manualZoneEdits.add(vlan);
    });
  }
  pendingFiles=[];$('import-pending').hidden=true;dirty=false;localReviewReady=false;previewSnapshot=null;
  $('review-content').replaceChildren();render();showView(workspace.rollout_view || (batch.length?'overview':'reference'),false);renderDeployment();updateSummary();
}
function rememberProject(id) {
  try{sessionStorage.setItem('ztb-project-id',id);localStorage.setItem('ztb-last-project-id',id);}catch(_){}
}
function projectSaveState(saver) {
  const label=$('project-save-status'),wrapper=label.parentElement;
  wrapper.dataset.state=saver.state;
  label.textContent=saver.state==='saved' ? 'Saved on this computer' : saver.state==='saving' ? 'Saving…' : saver.state==='unsaved' ? 'Unsaved changes' :
    saver.state==='conflict' ? saver.error.message : saver.state==='error' ? `Not saved. ${saver.error.status ? saver.error.message : 'The local app is unavailable. Keep this tab open, start the app, then retry.'}` : 'Opening your workspace…';
  $('project-save-retry').hidden=!['error','conflict'].includes(saver.state);
  $('project-save-retry').textContent=saver.state==='conflict' ? 'Keep both versions' : 'Retry save';
  $('edit-status').textContent=saver.state==='saved' ? 'Draft saved on this computer.' : saver.state==='saving' ? 'Saving draft…' : 'Draft has unsaved changes.';
  if(saver.state==='saved'){dirty=false;rememberProject(saver.project.id);}
}
function selectProject(project) {
  $('project-name').value=project.name;$('project-name').disabled=false;$('project-copy').disabled=false;
  projectSaver.adopt(project);restoreProjectWorkspace(project.workspace);
  rememberProject(project.id);diagrams?.refresh();
}
function emptyWorkspace(){return {version:1,batch:[],current:-1,active_tab:'site',rollout_view:'reference',group_by_country:false};}
function newProjectId(){return crypto.randomUUID().replaceAll('-','');}
async function initializeProjects() {
  projectWorking=true;renderDeployment();
  try {
    const {projects}=await api('/api/projects/list',{});
    let preferred;try{preferred=sessionStorage.getItem('ztb-project-id') || localStorage.getItem('ztb-last-project-id');}catch(_){}
    const previous=projects.find(project=>project.id===preferred) || projects[0];
    if(previous)selectProject(await api('/api/projects/load',{id:previous.id}));
    else {
      const workspace=emptyWorkspace();
      const project=await api('/api/projects/create',{id:newProjectId(),name:'Untitled rollout',workspace});
      selectProject({...project,workspace});
    }
  } catch(error) {
    $('project-save-status').textContent=`Could not open saved projects. ${error.message}`;
    $('project-save-status').parentElement.dataset.state='error';$('project-save-retry').hidden=false;
  } finally {projectWorking=false;renderDeployment();}
}
async function openProjectPicker() {
  if(projectWorking || deploymentActive())return;
  $('projects-error').hidden=true;$('projects-list').replaceChildren();$('projects-dialog').showModal();
  try {
    const {projects}=await api('/api/projects/list',{});
    for(const project of projects){
      const button=node('button',undefined,'reference-row');
      button.append(node('strong',project.name),node('small',`${project.site_count} site${project.site_count===1 ? '' : 's'} · Saved ${new Date(project.updated_at).toLocaleString()}${project.id===projectSaver.project?.id ? ' · Current project' : ''}`));
      button.onclick=async()=>{
        if(projectWorking || deploymentActive())return;
        projectWorking=true;$('projects-list').inert=true;renderDeployment();
        try {
          await projectSaver.flush();
          selectProject(await api('/api/projects/load',{id:project.id}));
          $('projects-dialog').close();notice();
        } catch(error){$('projects-error').textContent=`${error.message} Your current draft is still open.`;$('projects-error').hidden=false;}
        finally{projectWorking=false;$('projects-list').inert=false;renderDeployment();}
      };
      $('projects-list').append(button);
    }
    if(!projects.length)$('projects-list').append(node('p','No saved projects yet.','muted-empty'));
  } catch(error){$('projects-error').textContent=error.message;$('projects-error').hidden=false;}
}
let projectCreation=null;
function openProjectName(copy=false) {
  if(projectWorking || deploymentActive())return;
  projectCreation={copy,id:newProjectId(),request:null};
  $('project-name-title').textContent=copy ? 'Save this rollout as a copy' : 'New rollout project';
  $('project-create').textContent=copy ? 'Save copy' : 'Create project';
  $('project-new-name').disabled=false;
  $('project-new-name').value=copy ? `${$('project-name').value} copy`.slice(0,120) : '';
  $('project-name-error').hidden=true;$('project-name-dialog').showModal();$('project-new-name').focus();
}
$('project-name-form').onsubmit=async event=>{
  event.preventDefault();if(projectWorking || deploymentActive() || !projectCreation)return;
  projectWorking=true;renderDeployment();$('project-create').disabled=true;$('project-name-close').disabled=true;
  try {
    clearTimeout(projectSaver.timer);
    if(projectSaver.inFlight)await projectSaver.inFlight.catch(()=>{});
    if(!projectCreation.copy)await projectSaver.flush();
    // Keep the request ID and snapshot for a safe retry if the response is lost.
    projectCreation.request ||= {id:projectCreation.id,name:$('project-new-name').value.trim(),workspace:projectCreation.copy ? projectSnapshot().workspace : emptyWorkspace()};
    $('project-new-name').disabled=true;
    const project=await api('/api/projects/create',projectCreation.request);
    selectProject({...project,workspace:projectCreation.request.workspace});
    $('project-name-dialog').close();notice();
  } catch(error){
    if(error.status && error.status<500){projectCreation.request=null;$('project-new-name').disabled=false;}
    $('project-name-error').textContent=error.message;$('project-name-error').hidden=false;
  }
  finally{projectWorking=false;$('project-create').disabled=false;$('project-name-close').disabled=false;renderDeployment();}
};
$('project-name').oninput=()=>projectSaver.touch();
$('project-name').onblur=()=>{if(!$('project-name').value.trim()){$('project-name').value='Untitled rollout';projectSaver.touch();}};
$('project-new').onclick=()=>openProjectName();$('project-copy').onclick=()=>openProjectName(true);
$('project-open').onclick=openProjectPicker;
$('projects-close').onclick=()=>{if(!projectWorking)$('projects-dialog').close();};
$('project-name-close').onclick=()=>{if(!projectWorking)$('project-name-dialog').close();};
for(const id of ['projects-dialog','project-name-dialog'])$(id).addEventListener('cancel',event=>{if(projectWorking)event.preventDefault();});
$('project-save-retry').onclick=()=>{
  if(!projectSaver.project){initializeProjects();return;}
  if(projectSaver.state==='conflict')openProjectName(true);else projectSaver.flush().catch(()=>{});
};
projectSaver=new ZtbProjects.Saver({request:api,snapshot:projectSnapshot,onState:projectSaveState});
rollout=ZtbRollout.install({batch:()=>batch,projectId:()=>projectSaver?.project?.id,api,
  active:()=>deploymentActive() || projectWorking,changed:()=>{markDirty();renderList();},
  grouped:()=>groupByCountry,setGrouped:value=>{groupByCountry=value;projectSaver?.touch();},
  edit:index=>{current=index;render();showView('sites');},reference:()=>showView('reference'),review});
render();renderDeployment();
initializeProjects().then(async()=>{
  await refreshConnectionStatus();await pollDeployment();
  if(workspaceView==='review' && !deploymentActive())await review();
});

diagrams=ZtbDiagrams.install({api,currentSite,projectId:()=>projectSaver?.project?.id,touch:markDirty,
  getLogo:()=>diagramLogo,setLogo:value=>{diagramLogo=value;markDirty();}});
