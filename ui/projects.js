/* Serialize draft saves and detect stale tabs without overwriting newer work. */
(function(root) {
  'use strict';
  class Saver {
    constructor({request, snapshot, onState=()=>{}, delay=500}) {
      Object.assign(this,{request,snapshot,onState,delay});
      this.project=null;this.changed=0;this.saved=0;this.timer=null;this.inFlight=null;this.pending=null;this.error=null;this.state='loading';
    }
    get dirty(){return this.changed!==this.saved || Boolean(this.pending);}
    notify(state){this.state=state;this.onState(this);}
    adopt(project){
      clearTimeout(this.timer);this.project=project;this.changed=0;this.saved=0;this.pending=null;this.error=null;this.notify('saved');
    }
    touch(){
      if(!this.project)return;
      this.changed++;
      clearTimeout(this.timer);
      if(this.error?.status===409){this.notify('conflict');return;}
      this.notify(this.inFlight ? 'saving' : 'unsaved');
      this.timer=setTimeout(()=>{this.flush().catch(()=>{});},this.delay);
      this.timer.unref?.();
    }
    async flush(){
      clearTimeout(this.timer);
      if(this.inFlight){await this.inFlight;return this.dirty ? this.flush() : this.project;}
      if(!this.project || !this.dirty)return this.project;
      if(this.error?.status===409)throw this.error;
      this.inFlight=this.saveChanges();
      try{return await this.inFlight;}finally{this.inFlight=null;}
    }
    async saveChanges(){
      this.error=null;this.notify('saving');
      try {
        while(this.dirty){
          // Keep this exact request after failure: the server accepts safe replays.
          if(!this.pending)this.pending={sequence:this.changed,body:{id:this.project.id,revision:this.project.revision,...this.snapshot()}};
          const result=await this.request('/api/projects/save',this.pending.body);
          this.project=result;this.saved=this.pending.sequence;this.pending=null;
        }
        this.notify('saved');return this.project;
      } catch(error){this.error=error;this.notify(error.status===409 ? 'conflict' : 'error');throw error;}
    }
  }
  const api={Saver};
  if(typeof module!=='undefined' && module.exports)module.exports=api;
  else root.ZtbProjects=api;
})(typeof globalThis!=='undefined' ? globalThis : this);
