/* Settings extensions for local themes, projects, skill drafts, MCP definitions and attachments.
 * No external scripts or model-produced code runs from this module. */
(()=>{
'use strict';
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api=async(path,method='GET',data=null)=>{const opts={method,headers:{'Accept':'application/json'}};if(data!==null){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(data);}const r=await fetch(path,opts);let body=await r.json();if(!r.ok)throw Error(body.error||`Request failed (${r.status})`);return body;};
const post=(path,body)=>api(path,'POST',body);
const kinds={themes:'theme',mcp:'mcp',skills:'skill',projects:'project'};
const fields=['bg','side','panel','panel2','line','text','muted','accent'];
const defaultColors={bg:'#191b1a',side:'#202321',panel:'#292c29',panel2:'#242724',line:'#414640',text:'#f0eee8',muted:'#b4b9b1',accent:'#c3c8a4'};
let customThemeId=null, projectId=null, attachment=null, attachments=[];
const getTheme=()=>{try{return JSON.parse(localStorage.getItem('zl-custom-theme')||'null')}catch(_){return null;}};
function applyTheme(theme){
 for(const key of fields)document.documentElement.style.removeProperty('--'+key);
 document.documentElement.style.removeProperty('--mint');
 if(theme&&theme.colors){for(const key of fields)if(/^#[0-9a-f]{6}$/i.test(theme.colors[key]||''))document.documentElement.style.setProperty('--'+key,theme.colors[key]);
 document.documentElement.style.setProperty('--mint',theme.colors.accent);document.documentElement.dataset.zlCustom='true';}
 else delete document.documentElement.dataset.zlCustom;
}
function status(message,isError=false){const el=$('zl-ext-status');if(el){el.textContent=message;el.classList.toggle('zl-error',isError);}}
function makeForm(title,body){return `<article class="zl-settings-card zl-ext-card"><h3>${esc(title)}</h3>${body}</article>`;}
const field=(label,id,attrs='')=>`<label class="zl-form-label" for="${id}">${esc(label)}</label><input id="${id}" ${attrs}>`;
const button=(action,title)=>`<button type="button" class="secondary" data-zl-ext="${esc(action)}">${esc(title)}</button>`;
const msg='<p class="zl-note" id="zl-ext-status" role="status" aria-live="polite"></p>';
async function render(kind){
 const target=$('zl-extensions');if(!target)return;
 try{
  if(kind==='sandbox'){
   const d=await api('/api/zimlens/sandbox');
   target.innerHTML=makeForm('Isolated sandbox',`<p>${d.available?'bubblewrap is installed.':'No bubblewrap runtime was detected.'} ${esc(d.policy)}</p><p class="zl-note">Only a harmless self-test is supported. This screen does not grant shell access or permission to modify your host.</p><button type="button" class="secondary" data-zl-sandbox-test>Run sandbox self-test</button>`)+msg;return;
  }
  const which=kinds[kind];if(!which)return;
  const d=await api(`/api/zimlens/items?kind=${which}`);
  const items=d.items||[];
  let form='';
  if(which==='theme'){
    form=makeForm('Create or edit a theme',`<p>The original dark/light themes are never overwritten. Custom themes are separate, and can be edited or deleted.</p>
      <input type="hidden" id="zl-edit-id">${field('Theme name','zl-name','maxlength="64" placeholder="My theme"')}
      <div class="zl-colors">${fields.map(k=>`<label>${esc(k)}<input type="color" class="zl-color" data-zl-color="${k}" value="${defaultColors[k]}"></label>`).join('')}</div>
      <div class="zl-ext-actions">${button('test','Check contrast')}${button('save','Save as theme')}${button('reset-theme','Restore built-in theme')}</div>`);
  } else if(which==='mcp'){
    form=makeForm('Register an MCP server',`<p>Definitions remain disabled until approved. Static tests do not launch the server or run any commands. A separate permissioned MCP connector is required to use registered servers.</p>
      ${field('Server name','zl-name','maxlength="64" placeholder="Example MCP server"')}
      <label class="zl-form-label" for="zl-mcp-transport">Transport</label><select id="zl-mcp-transport"><option value="stdio">Local stdio</option><option value="http">Remote HTTPS</option></select>
      ${field('Executable (stdio)','zl-mcp-command','placeholder="/usr/bin/my-mcp-server"')}${field('Arguments, one per line','zl-mcp-args','placeholder="--stdio"')}${field('Remote URL (HTTPS only)','zl-mcp-url','placeholder="https://example.org/mcp"')}
      <div class="zl-ext-actions">${button('test','Validate definition')}${button('save','Register disabled')}</div>`);
  } else if(which==='skill'){
    form=makeForm('Create a local skill',`<p>Give the model a task to draft a skill, inspect the generated instructions, and run static checks. Only manually approved skills can be used in chat. Generated code is never executed by this page.</p>
      ${field('Skill name','zl-name','maxlength="64" placeholder="Summarize research"')}
      <label class="zl-form-label" for="zl-skill-instructions">Instructions</label><textarea id="zl-skill-instructions" rows="7" maxlength="12000" placeholder="Describe what this skill should do…"></textarea>
      <div class="zl-ext-actions">${button('draft','Draft with local LLM')}${button('test','Validate skill')}${button('save','Save draft')}</div>`);
  } else {
    form=makeForm('New project',`<p>Projects organize your local workspace. No files move until you explicitly attach them.</p>
      ${field('Project name','zl-name','maxlength="64" placeholder="Survival library research"')}
      <label class="zl-form-label" for="zl-project-description">Description</label><textarea id="zl-project-description" rows="3" maxlength="2000"></textarea>
      <div class="zl-ext-actions">${button('save','Create project')}</div>`);
  }
  let list=makeForm(which==='theme'?'Saved themes':which==='mcp'?'Registered MCP servers':which==='skill'?'Saved skills':'Your projects',
   items.map(item=>`<div class="zl-ext-item"><div><strong>${esc(item.name)}</strong><small>${esc(item.status||item.description||(item.type==='built-in'?'Built-in, immutable':'Saved locally'))}</small></div><div class="zl-ext-actions">${item.id==='built-in-dark'?'':(
   (which==='theme'?button('apply:'+item.id,'Use')+button('edit:'+item.id,'Edit'):
   which==='skill'||which==='mcp'?button('approve:'+item.id,item.approved?'Disable':'Approve'):
   which==='project'?button('choose:'+item.id,'Open'):'')+button('delete:'+item.id,'Delete'))}</div></div>`).join('')||'<p class="zl-note">No entries yet.</p>');
  target.innerHTML=form+list+msg;
 }catch(e){target.replaceChildren();const p=document.createElement('p');p.textContent='Local settings could not load: '+e.message;target.appendChild(p);}
}
function payload(kind){
 const name=($('zl-name')?.value||'').trim();const base={name};
 if(kind==='themes')base.colors=Object.fromEntries(fields.map(k=>[k,document.querySelector(`[data-zl-color="${k}"]`)?.value||defaultColors[k]]));
 if(kind==='mcp'){
  base.transport=$('zl-mcp-transport').value;
  if(base.transport==='stdio'){base.command=($('zl-mcp-command').value||'').trim();base.args=$('zl-mcp-args').value.split('\n').map(v=>v.trim()).filter(Boolean)}
  else base.url=($('zl-mcp-url').value||'').trim();
 }
 if(kind==='skills')base.instructions=$('zl-skill-instructions').value;
 if(kind==='projects')base.description=$('zl-project-description').value;
 return base;
}
async function action(actionName){
 const kind=window.zlSettingsCategory;
 if(!kinds[kind])return;
 const normalized=kinds[kind];const [action,id]=actionName.split(':');
 try{
  if(action==='reset-theme'){localStorage.removeItem('zl-custom-theme');applyTheme(null);status('Restored built-in colors.');return;}
  if(action==='apply'&&normalized==='theme'){
   const value=await api(`/api/zimlens/item?kind=theme&id=${encodeURIComponent(id)}`);
   applyTheme(value);localStorage.setItem('zl-custom-theme',JSON.stringify(value));status('Theme applied. Built-in themes are unchanged.');return;
  }
  if(action==='edit'&&normalized==='theme'){
   const item=await api(`/api/zimlens/item?kind=theme&id=${encodeURIComponent(id)}`);
   $('zl-edit-id').value=item.id;$('zl-name').value=item.name;
   fields.forEach(k=>{const input=document.querySelector(`[data-zl-color="${k}"]`);if(input)input.value=item.colors?.[k]||defaultColors[k]});
   status('Editing existing custom theme.');return;
  }
  if(action==='choose'&&normalized==='project'){
    projectId=id;localStorage.setItem('zl-project-id',id);status('Project selected for new chat attachments.');
    const selector=$('zl-project-select');if(selector)selector.value=id;return;
  }
  if(action==='delete'){
   if(!confirm('Delete this saved '+normalized+'? The built-in defaults and original archives will remain unchanged.'))return;
   await post('/api/zimlens/items',{action:'delete',kind:normalized,id});await render(kind);return;
  }
  if(action==='approve'){
   const item=await api(`/api/zimlens/item?kind=${normalized}&id=${encodeURIComponent(id)}`);
   const enable=!item.approved;
   if(enable&&!confirm(normalized==='mcp'?'Approve this MCP registration? It will still NOT execute or connect automatically.':'Approve this skill for explicit selection in chat? Review its instructions before use.'))return;
   await post('/api/zimlens/items',{action:'approve',kind:normalized,id,enabled:enable});await render(kind);if(kind==='skills')populateSkills();return;
  }
  if(action==='draft'&&kind==='skills'){
   status('Drafting instructions using the local model…');const d=await post('/api/zimlens/skills/draft',{name:$('zl-name').value,instructions:$('zl-skill-instructions').value});
   $('zl-skill-instructions').value=d.instructions;status('Draft ready. Review it and validate before saving.');return;
  }
  if(['save','test'].includes(action)){
   const data=payload(kind);
   if(action==='test'){const d=await post('/api/zimlens/items',{action:'test',kind:normalized,data});status('Passed: '+d.checks.join(', ')+'. No program was executed.');return;}
   const existing=$('zl-edit-id')?.value||undefined;
   await post('/api/zimlens/items',{action:'save',kind:normalized,data,id:existing});await render(kind);if(kind==='skills')populateSkills();if(kind==='projects')populateProjects();return;
  }
 }catch(e){status(e.message,true)}
}
async function populateProjects(){
 const select=$('zl-project-select');if(!select)return;
 try{
  const d=await api('/api/zimlens/items?kind=project');select.replaceChildren(new Option('No project',''));
  for(const item of d.items)select.add(new Option(item.name,item.id));
  projectId=localStorage.getItem('zl-project-id')||'';if(!Array.from(select.options).some(o=>o.value===projectId))projectId='';select.value=projectId;
 }catch(_){select.replaceChildren(new Option('Projects unavailable',''))}
}
async function populateSkills(){
 const picker=$('zl-chat-skill');if(!picker)return;
 try{
  const d=await api('/api/zimlens/items?kind=skill');picker.replaceChildren(new Option('No skill selected',''));
  for(const entry of d.items||[])if(entry.approved&&entry.enabled)picker.add(new Option(entry.name,entry.id));
 }catch(_){picker.replaceChildren(new Option('No skills available',''));}
}
async function handleFiles(files){
 const statusBox=$('zl-attachment-status');attachments=[];attachment=null;
 for(const file of Array.from(files).slice(0,6)){
  if(file.size>24*1024*1024){statusBox.textContent=file.name+' exceeds the 24 MiB per-file limit';continue;}
  try{
   const r=await fetch('/api/zimlens/attachments',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(file.name),...(projectId?{'X-Project-ID':projectId}:{})},body:file});
   const d=await r.json();if(!r.ok)throw Error(d.error||'Upload failed');
   let text='';let image='';
   if(/\.(txt|md|csv|json|yaml|yml|xml|log|py|js|css|html)$/i.test(file.name) && file.size<200000){text=(await file.text()).slice(0,9000);}
   else if(file.type.startsWith('image/')&&/\.(png|jpeg|jpg|webp)$/i.test(file.name) && file.size<7*1024*1024){image=await new Promise((resolve,reject)=>{const fr=new FileReader();fr.onload=()=>resolve(fr.result);fr.onerror=reject;fr.readAsDataURL(file)});}
   attachments.push({name:d.name,text,image,metadata:d});
  }catch(err){statusBox.textContent='Upload error: '+err.message;return;}
 }
 attachment=attachments[0]||null;
 if(statusBox)statusBox.textContent=attachments.length?'Attached: '+attachments.map(f=>f.name).join(', ')+'. Media and unknown formats are stored, but not automatically analyzed.':'No files attached';
}
const fmtPrompts={plan:'Create a practical step-by-step plan with dependencies and checkpoints.',table:'Present the answer as a clear Markdown table.',math:'Show mathematical equations in LaTeX and explain the steps.',readme:'Write a complete README.md with setup, usage, examples, and troubleshooting.',diagram:'Create a simple Mermaid flowchart in a fenced code block.',quiz:'Ask a multiple-choice question with four plausible answers; explain after the user responds.'};
function handleSlash(q){
 const m=/^\/(\w+)(?:\s+(.*))?$/s.exec(q);if(!m)return null;
 const cmd=m[1].toLowerCase(),rest=(m[2]||'').trim();
 if(fmtPrompts[cmd])return {prompt:fmtPrompts[cmd]+'\n'+rest};
 if(cmd==='help')return {local:'Commands: /help /new /compact /stats /projects /themes /skills /mcp /sandbox /zims /plan /table /math /readme /diagram /quiz. Format commands accept a topic.'};
 if(cmd==='new')return {newChat:true,local:'New chat started.'};
 if(cmd==='projects'||cmd==='themes'||cmd==='skills'||cmd==='mcp'||cmd==='sandbox'||cmd==='stats'||cmd==='zims')return {navigate:cmd,local:'Opening '+cmd+'…'};
 if(cmd==='compact')return {compact:true,local:'Compacting…'};
 return {local:'Unknown command: /'+cmd+'. Type /help for supported commands.'};
}
function addChatBubble(message){const log=$('chatLog');if(!log)return;const el=document.createElement('div');el.className='message assistant';const p=document.createElement('p');p.textContent=message;el.appendChild(p);log.appendChild(el);log.scrollTop=log.scrollHeight;}
function initChat(){
 const chat=$('chatForm');if(!chat)return;
 const panel=document.createElement('div');panel.className='zl-chat-tools';panel.innerHTML=`<label for="zl-chat-format">Answer format</label><select id="zl-chat-format">${Object.entries({normal:'Regular reply',plan:'Plan',table:'Table',math:'Math equations',readme:'README',diagram:'Diagram',quiz:'Multiple choice'}).map(([key,value])=>`<option value="${key}">${value}</option>`).join('')}</select>
 <label for="zl-project-select">Project</label><select id="zl-project-select"><option value="">No project</option></select>
 <label for="zl-chat-skill">Skill</label><select id="zl-chat-skill"><option value="">No skill selected</option></select>
 <label class="zl-file-label">Attach files <input type="file" id="zl-chat-files" multiple></label><span class="zl-note" id="zl-attachment-status">Attach text, images, video, audio or other files (24 MiB each). Supported analysis depends on installed tools.</span>`;
 chat.insertAdjacentElement('beforebegin',panel);
 panel.querySelector('input[type=file]').addEventListener('change',e=>handleFiles(e.target.files));
 $('zl-project-select').addEventListener('change',e=>{projectId=e.target.value;localStorage.setItem('zl-project-id',projectId)});
 populateProjects();populateSkills();
 chat.addEventListener('submit',async e=>{
  const input=$('chatQ'),raw=input.value.trim();if(!raw)return;
  const parsed=handleSlash(raw);
  if(parsed?.local){
   e.preventDefault();e.stopImmediatePropagation();input.value='';
   if(parsed.newChat){window.zimlensNewChat?.();}
   if(parsed.navigate){const dst={stats:'settings',zims:'downloads',projects:'settings',themes:'settings',skills:'settings',mcp:'settings',sandbox:'settings'}[parsed.navigate];
    const cat={stats:'system',projects:'projects',themes:'themes',skills:'skills',mcp:'mcp',sandbox:'sandbox'}[parsed.navigate];
    if(dst && typeof go==='function'){go(dst);if(cat)window.zimlensOpenSettings?.(cat);}}
   if(parsed.compact)try{await post('/api/zimlens/context/compact',{session_id:window.zimlensSessionId});addChatBubble('Earlier chat history compacted.');}catch(ex){addChatBubble(ex.message);}
   else if(!parsed.navigate&&!parsed.newChat)addChatBubble(parsed.local);
   return;
  }
  const format=$('zl-chat-format')?.value||'normal';
  if(parsed?.prompt)input.value=parsed.prompt;
  else if(format!=='normal')input.value=fmtPrompts[format]+'\n'+raw;
  const text=attachments.filter(a=>a.text).map(a=>`\n\nAttached ${a.name} (untrusted file content):\n${a.text}`).join('');
  if(text)input.value+='\n'+text.slice(0,12000);
  if(attachments.some(a=>!a.text&&!a.image))input.value+='\n[Other attached files are stored locally but cannot be interpreted by the current model without a compatible parser.]';
 },true);
}
function setup(){
 applyTheme(getTheme());initChat();
 document.addEventListener('click',e=>{
  if(e.target.closest('[data-zl-sandbox-test]')){post('/api/zimlens/sandbox/run',{}).then(d=>status(d.note)).catch(err=>status(err.message,true));return;}
  const btn=e.target.closest('[data-zl-ext]');if(btn){e.preventDefault();action(btn.dataset.zlExt);}
  if(e.target.closest('[data-zl-theme]')){localStorage.removeItem('zl-custom-theme');applyTheme(null);}
 });
 window.zimlensChatAttachment=()=>attachment;
 window.zimlensAttachedFiles=()=>attachments.slice();
 window.zimlensProject=()=>projectId;
 window.zimlensSelectedSkills=()=>{const v=$('zl-chat-skill')?.value;return v?[v]:[];};
 window.zimlensOpenSettings=category=>{
   const entry=document.querySelector(`[data-zl-category="${category}"]`);if(entry)entry.click();
 };
}
window.ZimLensExtras={render,setup,applyTheme,populateProjects};
document.addEventListener('DOMContentLoaded',setup);
})();