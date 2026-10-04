const {test}=require('node:test');
const assert=require('node:assert/strict');
const {Saver}=require('../ui/projects.js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const initial={id:'a'.repeat(32),name:'Project',revision:1};

test('edits made during a save are serialized and acknowledged only after the newest save',async()=>{
  let value='first';const pending=[],states=[];
  const saver=new Saver({request:(url,body)=>new Promise(resolve=>pending.push({body,resolve})),
    snapshot:()=>({name:'Project',workspace:{value}}),onState:s=>states.push(s.state)});
  saver.adopt(initial);saver.touch();const saving=saver.flush();
  value='second';saver.touch();
  pending[0].resolve({...initial,revision:2});await tick();
  assert.equal(saver.dirty,true);assert.equal(pending.length,2);
  assert.equal(pending[1].body.revision,2);assert.equal(pending[1].body.workspace.value,'second');
  assert.notEqual(states.at(-1),'saved');
  pending[1].resolve({...initial,revision:3});await saving;
  assert.equal(saver.dirty,false);assert.equal(saver.project.revision,3);assert.equal(states.at(-1),'saved');
});

test('a lost response retries the original snapshot before saving newer edits',async()=>{
  let value='first';const calls=[];
  const saver=new Saver({snapshot:()=>({name:'Project',workspace:{value}}),request:async(url,body)=>{
    calls.push(structuredClone(body));
    if(calls.length===1)throw new Error('Connection lost');
    return {...initial,revision:body.revision+1};
  }});
  saver.adopt(initial);saver.touch();await assert.rejects(saver.flush(),/Connection lost/);
  assert.equal(saver.dirty,true);assert.equal(saver.state,'error');
  value='second';saver.touch();await saver.flush();
  assert.deepEqual(calls[0],calls[1]);assert.equal(calls[2].workspace.value,'second');
  assert.equal(calls[2].revision,2);assert.equal(saver.dirty,false);
});

test('a conflict keeps local edits and prevents further automatic overwrite attempts',async()=>{
  let calls=0;
  const saver=new Saver({snapshot:()=>({name:'Project',workspace:{value:'local'}}),request:async()=>{
    calls++;throw Object.assign(new Error('Another tab saved'),{status:409});
  }});
  saver.adopt(initial);saver.touch();await assert.rejects(saver.flush());
  saver.touch();await assert.rejects(saver.flush());
  assert.equal(calls,1);assert.equal(saver.dirty,true);assert.equal(saver.state,'conflict');
  saver.adopt({...initial,id:'b'.repeat(32)});
  assert.equal(saver.dirty,false);assert.equal(saver.state,'saved');
});

test('opening a project cancels a pending timer and never writes an empty startup draft',async()=>{
  let calls=0;
  const saver=new Saver({request:async()=>{calls++;},snapshot:()=>({}),delay:5});
  saver.touch();await saver.flush();assert.equal(calls,0);
  saver.adopt(initial);saver.touch();saver.adopt({...initial,id:'b'.repeat(32)});
  await new Promise(resolve=>setTimeout(resolve,15));assert.equal(calls,0);
});
