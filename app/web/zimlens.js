/* ZimLens local workspace. No network assets, analytics, or third-party libraries. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const qs = selector => document.querySelector(selector);
  const categories = [
    ['Workspace', [['general','General'],['appearance','Appearance'],['system','System stats']]],
    ['Account & privacy', [['profile','Local profile'],['privacy','Data & privacy']]],
    ['AI & models', [['context','Context management'],['inference','Inference settings']]],
    ['Library & storage', [['library','Library'],['storage','Storage & archives'],['indexing','Indexing']]],
    ['Extensions', [['themes','Custom themes'],['mcp','MCP servers'],['skills','Skills'],['projects','Projects'],['sandbox','Sandbox OS']]],
  ];
  const defaultPrefs = {theme:'dark',reduced_motion:true,compact_navigation:false,resume_last_page:true,confirm_actions:true,auto_compact:true,compact_free_percent:15};
  let prefs = {...defaultPrefs};
  let selected = 'general', busyStats = false, pollHandle = null;
  const sessionKey = 'zimlens-chat-session';
  let sid = null;
  try {
    sid = sessionStorage.getItem(sessionKey);
    if (!sid || !/^[A-Za-z0-9_-]{12,80}$/.test(sid)) {
      const raw = new Uint8Array(18);
      crypto.getRandomValues(raw);
      sid = Array.from(raw, n => n.toString(16).padStart(2,'0')).join('');
      sessionStorage.setItem(sessionKey,sid);
    }
  } catch (_) { sid = 'session' + Date.now().toString(36) + Math.random().toString(36).slice(2).padEnd(12,'0'); }
  window.zimlensSessionId = sid;

  async function api(path, method='GET', payload=null) {
    const request = {method, cache:'no-store', headers:{'Accept':'application/json'}};
    if (payload !== null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = JSON.stringify(payload);
    }
    const r = await fetch(path, request);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }
  const fmt = bytes => {
    if (!Number.isFinite(bytes) || bytes < 0) return 'Unavailable';
    const gb = 1024 ** 3;
    return bytes >= gb ? (bytes/gb).toFixed(1)+' GB' : (bytes/(1024**2)).toFixed(0)+' MB';
  };
  const number = v => typeof v === 'number' && Number.isFinite(v) ? v.toFixed(v%1?1:0)+'%' : 'Sampling…';
  const percent = (used,total) => total > 0 ? Math.min(100, Math.round(used*100/total)) : null;

  function applyPreferences() {
    document.documentElement.dataset.zlTheme = prefs.theme;
    document.documentElement.dataset.zlReduced = String(prefs.reduced_motion);
    document.body.classList.toggle('zl-nav-compact',prefs.compact_navigation);
  }
  async function loadPrefs() {
    try {prefs = {...defaultPrefs,...await api('/api/zimlens/preferences')};}
    catch (_) { /* Keep usable local defaults if backend is temporarily unavailable. */ }
    applyPreferences();
  }
  async function updatePref(key,value) {
    const before = {...prefs};
    prefs[key] = value; applyPreferences();
    try {prefs = {...prefs,...await api('/api/zimlens/preferences','POST',{[key]:value})};}
    catch (err) {prefs = before;applyPreferences();throw err;}
  }
  function stat(name, value) { return `<div class="zl-stat-line"><span>${name}</span><b data-zl-stat="${value}">Loading…</b></div>`; }

  function createSettings() {
    const page = document.createElement('section');
    page.id = 'settings'; page.className = 'page';
    page.innerHTML = `<h1>Settings</h1><p class="lede">Manage your local workspace, models, archives, and preferences.</p>
      <div class="zl-settings-grid"><nav class="zl-settings-menu" aria-label="Settings categories">
        <label for="zl-settings-search" class="zl-note">Find a setting</label>
        <input type="search" id="zl-settings-search" placeholder="Search settings…" autocomplete="off">
        <div id="zl-settings-nav"></div></nav>
        <div id="zl-settings-content" class="zl-settings-content" aria-live="polite"></div>
      </div>`;
    const viewport = qs('.viewport');
    if (viewport) viewport.appendChild(page);
    page.addEventListener('click', event => {
      const btn = event.target.closest('[data-zl-category]');
      if (btn) {selected = btn.dataset.zlCategory; renderNav(); renderSettings(); return;}
      const theme = event.target.closest('[data-zl-theme]');
      if (theme) saveSetting('theme',theme.dataset.zlTheme);
      const jump = event.target.closest('[data-zl-jump]');
      if (jump && typeof go === 'function') go(jump.dataset.zlJump);
      if (event.target.closest('#zl-compact-now')) compactNow();
      if (event.target.closest('#zl-new-context')) newChatSession();
    });
    page.addEventListener('change',event => {
      const inp = event.target;
      if (inp.hasAttribute('data-zl-pref')) saveSetting(inp.dataset.zlPref,inp.type==='checkbox'?inp.checked:Number(inp.value));
    });
    $('zl-settings-search').addEventListener('input',renderNav);
    renderNav(); renderSettings();
  }
  function renderNav() {
    const search = ($('zl-settings-search')?.value || '').toLowerCase();
    const target = $('zl-settings-nav');
    if (!target) return;
    target.replaceChildren();
    for (const [group, links] of categories) {
      const filtered = links.filter(([,name]) => (group+' '+name).toLowerCase().includes(search));
      if (!filtered.length) continue;
      const heading = document.createElement('div'); heading.className='zl-settings-group'; heading.textContent=group; target.appendChild(heading);
      for (const [key,name] of filtered) {
        const button = document.createElement('button'); button.type='button'; button.className='zl-settings-link'+(key===selected?' active':'');
        button.dataset.zlCategory=key; button.textContent=name; button.setAttribute('aria-current', key===selected?'page':'false'); target.appendChild(button);
      }
    }
    if (!target.children.length) {const empty=document.createElement('p');empty.className='zl-settings-empty';empty.textContent='No matching settings.';target.appendChild(empty);}
  }
  function toggle(key,label,help,locked=false) {
    const checked = prefs[key] ? ' checked' : '';
    return `<div class="zl-option"><div><strong>${label}</strong><small>${help}</small></div>
      <input type="checkbox" class="zl-toggle" role="switch" aria-label="${label}" data-zl-pref="${key}" ${checked}${locked?' disabled':''}></div>`;
  }
  function card(title,help,body) {return `<article class="zl-settings-card"><h3>${title}</h3>${help?`<p>${help}</p>`:''}${body}</article>`;}
  function liveStats() {
    const rows = [['CPU usage','cpu'],['RAM usage','ram'],['GPU','gpu'],['Disk usage','disk'],['CPU temperature','temp']];
    return card('Live system stats','Measured on this machine. Updates while the page is open.',
      rows.map(([label,id])=>`<div class="zl-meter"><label>${label}</label><output id="zl-stat-${id}">Loading…</output><progress id="zl-bar-${id}" max="100" value="0" aria-label="${label}"></progress></div>`).join('')+
      `<span id="zl-stats-foot" class="zl-note">Waiting for local measurements…</span>`);
  }
  function contextCard() {
    return card('Context management','Conversation memory is kept in this running server process. Token usage is estimated.',
      `<div class="zl-option"><div><strong>Estimated context usage</strong><small id="zl-context-window">Loading usage…</small></div><output id="zl-context-pct">—</output></div>
      <progress class="zl-context-progress" id="zl-context-progress" value="0" max="100" aria-label="Context used"></progress>
      <span id="zl-context-summary" class="zl-note">Context resets when the server restarts.</span>
      ${toggle('auto_compact','Auto-compact conversation','Summarize earlier turns when the remaining token budget reaches the selected threshold.')}
      <div class="zl-context-actions"><div><label for="zl-threshold">Compact when context remaining</label>
      <select id="zl-threshold" data-zl-pref="compact_free_percent" aria-label="Auto-compaction threshold">
      ${[10,15,20,25].map(n=>`<option value="${n}"${prefs.compact_free_percent===n?' selected':''}>${n}% or less</option>`).join('')}</select></div>
      <button class="secondary" type="button" id="zl-compact-now">Compact now</button>
      <button class="secondary" type="button" id="zl-new-context">New conversation</button></div>
      <span id="zl-context-result" class="zl-status-text" role="status"></span>`);
  }
  function themeCard() {
    return card('Appearance','Choose a comfortable theme. No neon colors or decorative effects.',
      `<div class="zl-theme-choice">${['light','dark','system'].map(n=>`<button type="button" data-zl-theme="${n}" aria-pressed="${prefs.theme===n}">${n[0].toUpperCase()+n.slice(1)}</button>`).join('')}</div>
      ${toggle('reduced_motion','Reduced motion','Disable decorative animations and transitions.')}
      ${toggle('compact_navigation','Compact navigation','Use icon-only app navigation on larger screens.')}`);
  }
  const navButton = (section,label) => `<button type="button" class="secondary" data-zl-jump="${section}">${label}</button>`;
  function renderSettings() {
    const target = $('zl-settings-content'); if (!target) return;
    let title='',description='',content='';
    switch(selected) {
      case 'general':
        title='General'; description='Everyday preferences, system activity and available chat context.';
        content=`<div class="zl-settings-columns">${card('Workspace preferences','Local controls that take effect immediately.',
          toggle('resume_last_page','Resume last page','Restore your last open workspace page in this browser.')+
          toggle('confirm_actions','Confirm important actions','Require confirmation for supported destructive operations.')+
          toggle('compact_navigation','Compact navigation','Show app icons rather than full labels.')+
          toggle('reduced_motion','Reduced motion','Avoid nonessential animations.')+
          `<div class="zl-option"><div><strong>Open at login</strong><small>Managed through your existing systemd service, not controlled by this web page.</small></div><span class="zl-note">System setting</span></div>`)}${liveStats()}</div>${contextCard()}
          ${card('Theme','Choose how ZimLens looks.',`<div class="zl-theme-choice">${['light','dark','system'].map(n=>`<button type="button" data-zl-theme="${n}" aria-pressed="${prefs.theme===n}">${n[0].toUpperCase()+n.slice(1)}</button>`).join('')}</div>`)}`;break;
      case 'appearance':title='Appearance';description='Display and navigation options.';content=themeCard();break;
      case 'system':title='System stats';description='Local CPU, RAM, GPU and storage information.';content=liveStats();break;
      case 'context':title='Context management';description='Control conversation memory and automatic compaction.';content=contextCard();break;
      case 'profile':title='Local profile';description='Device-only identity.';content=card('Workspace profile','This older DoomsdayOS backend does not offer user accounts. No online identity is required.', '<span class="zl-note">All current activity stays on this machine.</span>');break;
      case 'privacy':title='Data & privacy';description='Local-first data protection.';content=card('Your data','Archives remain on their enrolled storage datasets and are not modified by this settings page.',`<div class="zl-option"><div><strong>Local chat memory</strong><small>Held in server RAM; not written to disk by this feature.</small></div><span>Process only</span></div>`)+card('Permissions','The existing browser and desktop agent approval checks are not bypassed by this UI.','<span class="zl-note">Your browser controls microphone and location permissions.</span>');break;
      case 'inference':title='Inference settings';description='Local model configuration and hardware controls.';content=card('AI & hardware','Use the existing model manager and hardware view.',navButton('models','Open model settings'));break;
      case 'library':title='Offline library';description='Browse local ZIM content.';content=card('Kiwix library','Read original ZIM archives using your local Kiwix server.','<a class="secondary" href="http://localhost:8080/" target="_blank" rel="noopener noreferrer">Open Kiwix</a>');break;
      case 'storage':title='Storage & archives';description='Downloads, verification, and archive inventory.';content=card('ZIM download manager','Access the existing integrity-checked archive download queue.',navButton('downloads','Open downloads'));break;
      case 'indexing':title='Indexing';description='Check local article indexing and search.';content=card('Index and search','Use the existing indexed source tools and status.',navButton('search','Open search'));break;
      case 'themes':title='Custom themes';description='Build, save, edit and use your own non-neon themes.';content='<div id="zl-extensions"></div>';break;
      case 'mcp':title='MCP servers';description='Register and validate server definitions with explicit approval.';content='<div id="zl-extensions"></div>';break;
      case 'skills':title='Skills';description='Draft skill instructions with the local LLM, validate and approve them.';content='<div id="zl-extensions"></div>';break;
      case 'projects':title='Projects';description='Organize local conversations and attachments.';content='<div id="zl-extensions"></div>';break;
      case 'sandbox':title='Sandbox OS';description='Inspect isolation availability; host execution remains disabled.';content='<div id="zl-extensions"></div>';break;
    }
    target.innerHTML=`<h2 class="zl-settings-section-title">${title}</h2><p class="zl-settings-help">${description}</p>${content}`;
    window.zlSettingsCategory=selected;
    if (['themes','mcp','skills','projects','sandbox'].includes(selected))window.ZimLensExtras?.render(selected);
    refreshStats(); refreshContext();
  }
  async function saveSetting(key,value) {
    try {await updatePref(key,value);renderSettings();}
    catch (err) {renderSettings();setStatus('Settings were not saved: '+err.message);}
  }
  function setStatus(message) {const n=$('zl-context-result');if(n)n.textContent=message;}
  function writeStat(id,value,progress) {
    const output=$('zl-stat-'+id),bar=$('zl-bar-'+id);
    if(output)output.textContent=value;
    if(bar){bar.value=typeof progress==='number'?Math.min(100,Math.max(0,progress)):0;bar.style.visibility=typeof progress==='number'?'visible':'hidden';}
  }
  async function refreshStats() {
    if (busyStats || document.visibilityState === 'hidden') return;
    const page=qs('.page.active');if(!page || !['settings','home'].includes(page.id))return;
    busyStats=true;
    try {
      const d=await api('/api/zimlens/system-stats');
      writeStat('cpu',number(d.cpu_percent),d.cpu_percent);
      const mem=d.memory;writeStat('ram',mem?`${fmt(mem.used)} / ${fmt(mem.total)}`:'Unavailable',mem?percent(mem.used,mem.total):null);
      const gpu=d.gpu;writeStat('gpu',gpu?.usage_percent == null ? (gpu?.name||'Unavailable') : `${gpu.name}: ${number(gpu.usage_percent)}`,gpu?.usage_percent);
      const disk=d.disk;writeStat('disk',disk?`${fmt(disk.used)} / ${fmt(disk.total)}`:'Unavailable',disk?percent(disk.used,disk.total):null);
      writeStat('temp',d.cpu_temperature_c == null ? 'Unavailable' : `${d.cpu_temperature_c} °C`,null);
      const footer=$('zl-stats-foot');if(footer)footer.textContent=`Storage mount: ${disk?.mount||'Unavailable'} · Unavailable sensors are not estimated.`;
      const cpu=$('zl-home-cpu');if(cpu)cpu.textContent=number(d.cpu_percent);
      const ram=$('zl-home-ram');if(ram)ram.textContent=mem?`${fmt(mem.used)} / ${fmt(mem.total)}`:'Unavailable';
      const diskEl=$('zl-home-disk');if(diskEl)diskEl.textContent=disk?`${fmt(disk.free)} free (${disk.mount})`:'Unavailable';
    } catch (_) {
      const footer=$('zl-stats-foot');if(footer)footer.textContent='System telemetry unavailable. Check the local gateway.';
    } finally {busyStats=false;}
  }
  function renderContext(d) {
    if (!d) return;
    const used=d.used_estimated??0,windowSize=d.window??0;
    const progress=$('zl-context-progress');if(progress)progress.value=d.used_percent_estimated||0;
    const pct=$('zl-context-pct');if(pct)pct.textContent=(d.used_percent_estimated||0)+'% used (est.)';
    const detail=$('zl-context-window');if(detail)detail.textContent=`~${used.toLocaleString()} / ${windowSize.toLocaleString()} tokens · ~${(d.remaining_estimated||0).toLocaleString()} remaining`;
    const summary=$('zl-context-summary');if(summary)summary.textContent=`Compactions: ${d.compactions||0} · Estimation only; reserves ${d.reserved_output_tokens||0} output tokens. Chat history is not written to disk.`;
    const chat=$('zl-chat-context');if(chat)chat.textContent=`Context ~${d.used_percent_estimated||0}% (estimated)`;
  }
  window.zimlensContextUpdated = renderContext;
  async function refreshContext() {
    const page=qs('.page.active'); if(!page || !['settings','chat'].includes(page.id))return;
    try {renderContext(await api('/api/zimlens/context?session_id='+encodeURIComponent(sid)));}
    catch (_) {const n=$('zl-context-window');if(n)n.textContent='Available once the local gateway is running.';}
  }
  async function compactNow() {
    const b=$('zl-compact-now');if(b)b.disabled=true;
    setStatus('Compacting earlier conversation turns with the local model…');
    try {
      const d=await api('/api/zimlens/context/compact','POST',{session_id:sid});
      renderContext(d);
      setStatus(d.compacted?'Earlier turns summarized. Recent turns preserved.':'Not enough previous conversation to compact.');
    } catch (e) {setStatus(e.message);} finally {if(b)b.disabled=false;}
  }
  function newChatSession() {
    const random=new Uint8Array(18);crypto.getRandomValues(random);
    sid=Array.from(random,n=>n.toString(16).padStart(2,'0')).join('');
    try{sessionStorage.setItem(sessionKey,sid);}catch(_){}
    window.zimlensSessionId=sid;
    if($('chatLog'))$('chatLog').replaceChildren();
    setStatus('New conversation started. Previous session remains in server memory until restart/eviction.');
    refreshContext();
  }
  window.zimlensNewChat = newChatSession;
  async function refreshHome() {
    if(!$('zl-home-archive-count'))return;
    try {
      const d=await api('/api/zims/downloads');
      const installed=d.installed||d.inventory||d.archives||[];
      if(Array.isArray(installed))$('zl-home-archive-count').textContent=installed.length.toLocaleString();
      else if(typeof installed === 'number')$('zl-home-archive-count').textContent=installed.toLocaleString();
    }catch(_){$('zl-home-archive-count').textContent='Unavailable';}
  }
  function restorePage() {
    if (!prefs.resume_last_page || location.hash) return;
    try {
      const page=localStorage.getItem('zimlens-last-page');
      if(page && document.getElementById(page) && typeof go === 'function')go(page);
    } catch(_){/* Browser storage can be disabled. */}
  }
  function setupActivity() {
    const store=[];
    function track(name) {
      const labels={home:'Overview',search:'Search',research:'Research',chat:'Chat',lens:'Visual Lens',map:'Maps',downloads:'Downloads',maintenance:'System health',models:'AI & hardware',settings:'Settings'};
      if(!labels[name])return;
      store.unshift(`${labels[name]} opened`);store.splice(5);
      const log=$('zl-recent-activity');if(log){log.replaceChildren();for(const item of store){const li=document.createElement('li');li.textContent=item;log.appendChild(li);}}
      if(prefs.resume_last_page)try{localStorage.setItem('zimlens-last-page',name)}catch(_){}
    }
    document.addEventListener('click', event => {
      const btn=event.target.closest('[data-tab],[data-goto],[data-zl-jump]');
      if(btn)track(btn.dataset.tab||btn.dataset.goto||btn.dataset.zlJump);
    });
  }
  document.addEventListener('DOMContentLoaded',async()=>{
    createSettings(); setupActivity();
    const chat=$('chatForm');if(chat){const p=document.createElement('div');p.id='zl-chat-context';p.className='zl-chat-context';p.setAttribute('aria-live','polite');p.textContent='Context usage available after connection';chat.insertAdjacentElement('beforebegin',p);}
    await loadPrefs();restorePage();
    if(location.hash==='#settings' && typeof go==='function')go('settings');
    refreshHome();refreshStats();refreshContext();
    pollHandle=setInterval(()=>{refreshStats();refreshContext();},5000);
    window.addEventListener('hashchange',()=>{refreshStats();refreshContext();});
  });
})();