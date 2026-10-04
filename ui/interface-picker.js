(function(root) {
  'use strict';
  const split = value => String(value || '').split(',').map(x=>x.trim()).filter(Boolean);
  const sort = (a,b) => a.localeCompare(b, undefined, {numeric:true});
  const templateKey = fields => JSON.stringify([String(fields.template_id || '').trim(),String(fields.template_name || '').trim().toLowerCase()]);

  class Catalog {
    constructor(request, changed=()=>{}) {this.request=request;this.changed=changed;this.entries=new Map();this.generation=0;}
    reset() {this.generation++;this.entries.clear();}
    get(fields) {return this.entries.get(templateKey(fields));}
    load(fields, refresh=false) {
      const key=templateKey(fields), existing=this.entries.get(key);
      if(existing && !refresh)return existing.promise || Promise.resolve(existing);
      const generation=this.generation,entry={state:'loading'};
      this.entries.set(key,entry);
      entry.promise=(async()=>{
        try {
          entry.data=await this.request({template_name:String(fields.template_name || '').trim(),template_id:String(fields.template_id || '').trim(),refresh});
          entry.state='ready';
        } catch(error) {entry.state='error';entry.message=error.message;}
        if(generation===this.generation && this.entries.get(key)===entry)this.changed();
        return entry;
      })();
      return entry.promise;
    }
  }

  function choices(data, role, slots, excluded=[]) {
    const kinds=role==='wan' ? ['wan'] : role==='ha' ? ['ha'] : ['lan','management'];
    const lists=slots.map(slot=>(data?.gateways.find(g=>g.id===slot)?.interfaces || [])
      .filter(port=>kinds.includes(port.type) && !port.bond_member && !excluded.includes(port.name)));
    const common=lists.length ? lists[0].filter(port=>lists.every(list=>list.some(p=>p.name===port.name))) : [];
    const result=new Map(common.map(port=>[port.name,{value:port.name,label:`${port.name} — ${port.type==='management' ? 'Management' : port.type.toUpperCase()}`} ]));
    // lo0 is the editor's supported management loopback; template APIs omit implicit interfaces.
    if(role==='vlan')result.set('lo0',{value:'lo0',label:'lo0 — Management loopback'});
    return [...result.values()].sort((a,b)=>sort(a.value,b.value));
  }

  function selection(options, value, verified, multiple) {
    const values=multiple ? split(value) : value ? [value] : [];
    const missing=[...new Set(values.filter(v=>!options.some(o=>o.value===v)))];
    return {options:[...options,...missing.map(value=>({value,label:`${value} — current; ${verified ? 'review' : 'not verified'}`}))],
      warning:missing.length ? (verified ? `Review ${missing.join(', ')}: not available for this role in the selected template.` : 'Current assignment kept; connect and load the template to verify it.') : ''};
  }

  let nextId=0;
  function mount(host, config) {
    const make=(tag,text,cls)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;};
    host.classList.add('interface-picker');
    let manual=config.manual(), select=config.select || null;
    function draw() {
      const value=String(config.value() || ''), state=config.state();
      const result=selection(state.options,value,state.verified,config.multiple);
      const opened=host.querySelector('details')?.open;
      const wasFocused=host.contains(document.activeElement), position=document.activeElement?.selectionStart;
      host.replaceChildren();
      const warning=make('small',result.warning,'interface-warning');warning.hidden=!result.warning;
      warning.id=`interface-note-${++nextId}`;
      const control=manual ? make('input') : config.multiple ? make('details',undefined,'interface-multi') : select || make('select');
      if(manual) {
        control.type='text';control.value=value;control.placeholder=config.multiple ? 'ge2,ge3 or lo0' : 'Exact interface name';
        control.setAttribute('aria-label',config.label+' manual');
        control.oninput=()=>{config.change(control.value);const next=selection(config.state().options,control.value,config.state().verified,config.multiple);warning.textContent=next.warning;warning.hidden=!next.warning;};
      } else if(config.multiple) {
        const summary=make('summary',split(value).join(', ') || 'Choose interfaces');
        summary.setAttribute('aria-label',config.label);summary.setAttribute('aria-describedby',warning.id);
        const menu=make('div',undefined,'interface-menu');menu.setAttribute('role','group');menu.setAttribute('aria-label',config.label+' choices');
        for(const option of result.options) {
          const label=make('label'),box=make('input');box.type='checkbox';box.value=option.value;box.checked=split(value).includes(option.value);
          label.append(box,make('span',option.label));menu.append(label);
          box.onchange=()=>{
            const values=[...menu.querySelectorAll('input:checked')].map(input=>input.value);
            config.change(values.join(','));summary.textContent=values.join(', ') || 'Choose interfaces';
            const next=selection(config.state().options,config.value(),config.state().verified,true);warning.textContent=next.warning;warning.hidden=!next.warning;
          };
        }
        const done=make('button','Done','text-button');done.type='button';done.onclick=()=>{control.open=false;summary.focus();};menu.append(done);
        control.append(summary,menu);control.open=Boolean(opened);
        control.onkeydown=event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();control.open=false;summary.focus();}};
      } else {
        select=control;select.hidden=false;select.replaceChildren(new Option('Choose interface',''),...result.options.map(option=>new Option(option.label,option.value)));
        select.value=value;select.setAttribute('aria-label',config.label);
        select.onchange=()=>{config.change(select.value);draw();};
      }
      control.setAttribute('aria-describedby',warning.id);
      const toggle=make('button',manual ? 'Use interface list' : 'Enter manually','text-button interface-toggle');toggle.type='button';
      toggle.onclick=()=>{manual=!manual;config.setManual(manual);draw();host.querySelector('input,select,summary')?.focus();};
      host.append(control,toggle,warning);
      if(manual && select){select.hidden=true;host.append(select);}
      if(manual && wasFocused){control.focus();if(position!==null)control.setSelectionRange(position,position);}
    }
    host.refreshInterfaces=draw;draw();
  }

  if(typeof document!=='undefined')document.addEventListener('click',event=>{
    document.querySelectorAll('.interface-multi[open]').forEach(el=>{if(!el.contains(event.target))el.open=false;});
  });
  const api={Catalog,choices,selection,split,templateKey,mount};
  if(typeof module!=='undefined' && module.exports)module.exports=api;
  root.ZtbInterfaces=api;
})(globalThis);
