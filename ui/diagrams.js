/* Local diagram controls. SVG is displayed as an image, never injected as markup. */
(function(root){
  'use strict';
  function uplinks(site){
    const f=site.fields, rows=[{key:`a:${f.wan_interface_name || ''}:`,label:`Gateway A · ${f.wan_interface_name || 'WAN'}`}];
    if(site.ha_enabled ?? Boolean(f.gateway_name_b))rows.push({key:`b:${f.wan1_interface_name || ''}:`,label:`Gateway B · ${f.wan1_interface_name || 'WAN'}`});
    if(f.copy_additional_wans==='1'){
      try{for(const w of JSON.parse(f.additional_wans_json || '[]'))rows.push({key:`${w.gateway_target}:${w.interface}:${w.tag || '1'}`,label:`Gateway ${w.gateway_target.toUpperCase()} · ${w.interface} · VLAN ${w.tag || '1'}`});}catch(_){}
    }
    return rows;
  }
  function install({api,currentSite,projectId,touch,getLogo=()=>null,setLogo=()=>{}}){
    const $=id=>document.getElementById(id);
    const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
    let chosen=null,runs=[],request=0,loadedProject='',viewRun='';
    let logoRequest=0,logoBusy=false;
    function renderLogo(){
      const logo=getLogo();$('diagram-logo-image').hidden=!logo;$('diagram-logo-default').hidden=Boolean(logo);
      if(logo)$('diagram-logo-image').src='data:image/png;base64,'+logo.content;
      else $('diagram-logo-image').removeAttribute('src');
      $('diagram-logo-reset').hidden=!logo;$('diagram-logo-reset').disabled=logoBusy;
      $('diagram-logo-upload').disabled=logoBusy;
      $('diagram-logo-upload').textContent=logoBusy ? 'Preparing logo…' : logo ? 'Replace customer logo' : 'Upload customer logo';
    }
    $('diagram-logo-upload').onclick=()=>$('diagram-logo-file').click();
    $('diagram-logo-reset').onclick=()=>{logoRequest++;setLogo(null);renderLogo();$('diagram-logo-feedback').textContent='Zscaler logo restored for this project.';};
    $('diagram-logo-file').onchange=async()=>{
      const file=$('diagram-logo-file').files[0];$('diagram-logo-file').value='';if(!file)return;
      const pid=projectId(),ticket=++logoRequest;logoBusy=true;renderLogo();$('diagram-logo-feedback').textContent='';
      try{
        if(file.size>2*1024*1024)throw new Error('Choose a JPG or PNG logo up to 2 MB.');
        if(!/\.(jpe?g|png)$/i.test(file.name))throw new Error('Choose a JPG or PNG logo.');
        const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('Could not read this logo file.'));reader.readAsDataURL(file);});
        const result=await api('/api/diagrams/logo',{logo:{content}});
        if(ticket!==logoRequest || pid!==projectId())return;
        setLogo(result.logo);$('diagram-logo-feedback').textContent='Customer logo selected for this project. Preview a site diagram to see it.';
      }catch(e){if(ticket===logoRequest && pid===projectId())$('diagram-logo-feedback').textContent=e.message;}
      finally{logoBusy=false;renderLogo();}
    };
    function download(file){
      const bytes=Uint8Array.from(atob(file.content),c=>c.charCodeAt(0));
      const url=URL.createObjectURL(new Blob([bytes],{type:file.mime}));
      const link=el('a');link.href=url;link.download=file.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
    }
    function message(value){$('diagram-feedback').textContent=value;$('diagram-editor-feedback').textContent=value;}
    async function fetchFile(ref,format){return api('/api/diagrams/download',{project_id:projectId(),run:ref.run,site:ref.site,format});}
    async function show(ref,name){
      try{
        const pid=projectId();const file=await fetchFile(ref,'svg');if(pid!==projectId())return;
        chosen={ref,name};$('diagram-title').textContent=name;$('diagram-image').src='data:image/svg+xml;base64,'+file.content;
        $('diagram-image').alt=`${name}: WAN topology and VLAN gateway legend`;
        $('diagram-zoom').checked=false;$('diagram-canvas').classList.remove('actual-size');
        $('diagram-save-svg').disabled=false;$('diagram-save-visio').hidden=false;
        $('diagram-save-png').disabled=!ref.formats?.includes('png');$('diagram-save-package').hidden=false;
        $('diagram-modal-note').textContent='Configuration captured at deployment. Physical cabling follows the intended design; live service connection status is not shown.';
        $('diagram-dialog').showModal();
      }catch(e){message(e.message);}
    }
    function actions(ref,name){
      const box=el('div',undefined,'diagram-actions');
      const view=el('button','View diagram','button');view.type='button';view.onclick=()=>show(ref,name);view.disabled=!ref.formats?.includes('svg');
      const menu=el('details',undefined,'diagram-download');menu.append(el('summary','Download site diagram'));
      for(const [format,label] of [['zip','All formats (.zip)'],['png','PNG preview'],['svg','SVG image'],['vsdx','Editable Visio (.vsdx)']]){
        const button=el('button',label,'text-button');button.type='button';button.disabled=format==='zip' ? !ref.formats?.length : !ref.formats?.includes(format);
        button.onclick=async()=>{try{download(await fetchFile(ref,format));menu.open=false;}catch(e){message(e.message);}};menu.append(button);
      }
      box.append(view,menu);
      {
        const retry=el('button',ref.warning ? 'Regenerate diagram' : 'Update diagram layout','text-button');retry.type='button';retry.onclick=async()=>{
          retry.disabled=true;
          try{await api('/api/diagrams/regenerate',{project_id:projectId(),run:ref.run,site:ref.site});menu.open=false;await refresh();message('Diagram exports updated from the saved configuration snapshot.');}
          catch(e){message(e.message);}finally{retry.disabled=false;}
        };menu.append(retry);if(ref.warning)box.append(el('p',ref.warning,'deployment-warning'));
      }
      return box;
    }
    function renderOptions(){
      renderLogo();
      const site=currentSite();if(!site)return;
      if(!site.diagram && site.fields.diagram_options_json){try{site.diagram=JSON.parse(site.fields.diagram_options_json);}catch(_){}}
      const opts=site.diagram || {switch:'',uplinks:{}};
      $('diagram-switch').value=opts.switch || '';
      const host=$('diagram-uplinks');host.replaceChildren();
      for(const [i,wan] of uplinks(site).entries()){
        const row=el('div',undefined,'diagram-uplink');row.append(el('strong',wan.label));
        for(const [key,label,placeholder] of [['name','ISP name',`ISP ${i+1}`],['circuit','Shared circuit (optional)','e.g. Primary circuit']]){
          const field=el('label',label);const input=el('input');input.maxLength=120;input.value=opts.uplinks?.[wan.key]?.[key] || '';input.placeholder=placeholder;
          input.setAttribute('aria-label',`${wan.label} ${label}`);
          input.oninput=()=>{site.diagram ||= {switch:'',uplinks:{}};site.diagram.uplinks ||= {};site.diagram.uplinks[wan.key] ||= {};site.diagram.uplinks[wan.key][key]=input.value;touch();};
          field.append(input);row.append(field);
        }
        host.append(row);
      }
      $('ha-deployment-note').hidden=!site.ha_enabled;
    }
    $('diagram-switch').oninput=()=>{const site=currentSite();site.diagram ||= {switch:'',uplinks:{}};site.diagram.switch=$('diagram-switch').value;touch();};
    $('diagram-preview').closest('details').ontoggle=event=>{if(event.target.open)renderOptions();};
    $('diagram-preview').onclick=async()=>{
      const button=$('diagram-preview');button.disabled=true;
      try{
        const site=currentSite(),batch=structuredClone([site]),logo=structuredClone(getLogo());const file=await api('/api/diagrams/preview',{batch,diagram_logo:logo});
        chosen={file,batch,logo,name:site.fields.site_name || 'Planned site'};
        $('diagram-title').textContent=chosen.name+' · planned';$('diagram-image').src='data:image/svg+xml;base64,'+file.content;
        $('diagram-image').alt='Planned site topology with WAN and VLAN addressing';
        $('diagram-zoom').checked=false;$('diagram-canvas').classList.remove('actual-size');
        $('diagram-save-svg').disabled=false;$('diagram-save-visio').hidden=true;
        $('diagram-save-png').disabled=false;$('diagram-save-package').hidden=true;
        $('diagram-modal-note').textContent='Planned configuration. This preview does not create resources or verify a deployed site. Load template interfaces to identify HA mode and ports.';
        $('diagram-dialog').showModal();
      }catch(e){message(e.message);}finally{button.disabled=false;}
    };
    $('diagram-close').onclick=()=>$('diagram-dialog').close();
    for(const [id,format] of [['diagram-save-svg','svg'],['diagram-save-png','png'],['diagram-save-visio','vsdx'],['diagram-save-package','zip']])$(id).onclick=async()=>{
      const button=$(id);button.disabled=true;
      try{
        const selection=chosen;
        const file=selection.file ? (format==='svg' ? selection.file : await api('/api/diagrams/preview',{batch:selection.batch,diagram_logo:selection.logo,format})) : await fetchFile(selection.ref,format);
        download(file);
      }catch(e){$('diagram-modal-note').textContent=e.message;}finally{button.disabled=false;}
    };
    $('diagram-zoom').onchange=()=>{$('diagram-canvas').classList.toggle('actual-size',$('diagram-zoom').checked);};
    function renderHistory(){
      const select=$('diagram-runs');select.replaceChildren();
      runs.forEach(run=>select.add(new Option(`${new Date(run.finished_at).toLocaleString()} · ${run.sites.length} site(s)`,run.run)));
      if(runs.some(r=>r.run===viewRun))select.value=viewRun;
      viewRun=select.value;
      $('saved-diagrams').hidden=!runs.length;
      const host=$('saved-diagram-sites');host.replaceChildren();
      const run=runs.find(r=>r.run===viewRun);if(!run)return;
      for(const site of run.sites){const row=el('div',undefined,'saved-diagram-site');row.append(el('strong',site.name),actions({run:run.run,site:site.id,formats:site.formats,warning:site.warning},site.name));host.append(row);}
    }
    async function refresh(){
      const pid=projectId(),ticket=++request;if(!pid)return;
      if(loadedProject!==pid){runs=[];viewRun='';loadedProject=pid;renderHistory();}
      try{const result=await api('/api/diagrams/list',{project_id:pid});if(ticket!==request || pid!==projectId())return;runs=result.runs;renderHistory();}
      catch(e){if(ticket===request)message(e.message);}
    }
    $('diagram-runs').onchange=()=>{viewRun=$('diagram-runs').value;renderHistory();};
    $('diagram-history-refresh').onclick=refresh;
    $('diagram-download-all').onclick=async()=>{try{download(await fetchFile({run:viewRun},'zip'));}catch(e){message(e.message);}};
    return {renderOptions,actions,refresh};
  }
  root.ZtbDiagrams={install,uplinks};
  if(typeof module!=='undefined')module.exports=root.ZtbDiagrams;
})(typeof globalThis!=='undefined'?globalThis:this);
