'use strict';
// Mode switch comes from the installed configuration, never from a remote site.
async function installModeUI(){
  try{
    const r=await fetch('/api/status',{cache:'no-store'});
    if(!r.ok) return;
    const cfg=await r.json();
    if(cfg.ai_enabled!==false)return;
    document.body.classList.add('core-mode');
    for(const name of ['chat','translate','models']){
      document.querySelectorAll('[data-tab="'+name+'"],[data-goto="'+name+'"]').forEach(node=>node.hidden=true);
    }
    document.querySelectorAll('input[name="searchMode"][value="rag"],#ragVisualSearch,input[name="lensMode"][value="vision"],input[name="lensMode"][value="auto"]').forEach(node=>{
      const label=node.closest('label');if(label)label.hidden=true;
    });
    const classic=document.querySelector('input[name="lensMode"][value="image_search"]');if(classic)classic.checked=true;
    const lede=document.querySelector('#home .lede');if(lede)lede.textContent='Browse offline ZIM archives, PDFs, maps, and indexed images without AI or model downloads.';
    const engine=document.querySelector('#home .info-strip b');if(engine)engine.textContent='Offline indexes + scripted image matching';
    const research=document.querySelector('#research .lede');if(research)research.textContent='Find indexed ZIM articles by name, import and search PDFs, and open cited source excerpts. No AI model required.';
    const answer=document.querySelector('#researchAskForm button');if(answer)answer.textContent='Find cited excerpts';
    const lens=document.querySelector('#lens .lede');if(lens)lens.textContent='Classic hash-based image matching — no model or GPU needed.';
    if(['chat','translate','models'].includes(location.hash.slice(1)))go('home');
  }catch(_){ /* Keep offline interface readable while system starts. */ }
}
window.addEventListener('DOMContentLoaded',installModeUI);
const $=id=>document.getElementById(id);
const nameMap={home:'Overview',search:'Knowledge Search',chat:'Local Chat',lens:'Visual Lens',translate:'Translate',map:'World Maps',models:'AI & Hardware'};
function go(name){document.querySelectorAll('.page').forEach(el=>el.classList.toggle('active',el.id===name));document.querySelectorAll('[data-tab]').forEach(el=>el.classList.toggle('active',el.dataset.tab===name));$('pageName').textContent=nameMap[name]||name;location.hash=name==='home'?'':name;if(name==='models')hardware();}
document.querySelectorAll('[data-tab]').forEach(el=>el.addEventListener('click',()=>go(el.dataset.tab)));
document.querySelectorAll('[data-goto]').forEach(el=>el.addEventListener('click',()=>go(el.dataset.goto)));
if(nameMap[location.hash.slice(1)])go(location.hash.slice(1));
function el(tag,text,cls){const x=document.createElement(tag);if(text!=null)x.textContent=text;if(cls)x.className=cls;return x;}
function clear(id){const box=$(id);box.replaceChildren();return box;}
function error(target,msg){target.appendChild(el('p',msg,'error'));}
function link(target,title,url){const a=el('a',title);a.href=url;a.target='_blank';a.rel='noopener noreferrer';target.appendChild(a);return a;}
async function ask(payload){const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d;}
function displaySources(box,res){if(res.kiwix){const p=el('p');p.append('Offline archive: ');link(p,'Open Kiwix search',res.kiwix);box.appendChild(p);}if(res.sources?.length||res.results?.length){const list=el('ul');(res.sources||res.results).forEach(s=>{const item=el('li');if(s.link)link(item,s.title||s.name,s.link);else item.textContent=s.title||s.name||'Result';if(s.snippet)item.appendChild(el('span',' — '+s.snippet));if(s.match_percent!=null)item.appendChild(el('span',' — '+s.match_percent+'% hash similarity'));list.appendChild(item)});box.appendChild(list);}const photos=res.images?.length?res.images:(res.mode==='classic_ahash_dhash'?res.results?.filter(s=>s.media_link).slice(0,12).map(s=>({image_url:s.media_link,article_link:s.link,title:s.name,alt:s.name})):[]);if(photos?.length){const title=el('h3',res.images?.length?'Images from the selected articles only':'Scripted similar-image matches');box.appendChild(title);const gallery=el('div',null,'article-gallery');for(const src of photos){const tile=el('div',null,'article-photo');const picture=el('img');picture.src=src.image_url;picture.alt=src.alt||src.title||'Article image';picture.loading='lazy';picture.referrerPolicy='no-referrer';tile.appendChild(picture);const caption=el('div');link(caption,src.title||'Open source article',src.article_link);tile.appendChild(caption);gallery.appendChild(tile)}box.appendChild(gallery);}if(res.visual_note)box.appendChild(el('p',res.visual_note,'muted'));if(res.note)box.appendChild(el('p',res.note,'muted'));}
$('searchForm').addEventListener('submit',async e=>{e.preventDefault();const box=clear('searchResult'),q=$('searchQ').value,mode=document.querySelector('[name=searchMode]:checked').value;box.appendChild(el('p','Searching local sources...','muted'));try{const d=await ask({action:mode==='algorithmic'?'search':'chat',mode, prompt:q,visual_rag:$('ragVisualSearch').checked});clear('searchResult');if(d.answer)box.appendChild(el('p',d.answer));displaySources(box,d);}catch(ex){clear('searchResult');error(box,ex.message)}});
$('chatForm').addEventListener('submit',async e=>{e.preventDefault();const q=$('chatQ').value,mode=document.querySelector('[name=chatMode]:checked').value; if(!q.trim())return;$('chatQ').value='';const log=$('chatLog');const you=el('div',null,'message user');you.appendChild(el('small','YOU'));you.appendChild(el('p',q));log.appendChild(you);const ai=el('div',null,'message assistant');ai.appendChild(el('small','DOOMSDAY AI'));const content=el('p','Generating locally...');ai.appendChild(content);log.appendChild(ai);try{let loc=null;
  if(mode==='agent'&&$('agentUseLocation').checked){
   if(!navigator.geolocation)throw Error('Browser geolocation unavailable; enter start coordinates instead.');
   loc=await new Promise((resolve,reject)=>navigator.geolocation.getCurrentPosition(p=>resolve({lat:p.coords.latitude,lon:p.coords.longitude}),e=>reject(Error('Location not available: '+e.message)),{enableHighAccuracy:false,timeout:9000}));
  }
  const d=await ask({action:mode==='agent'?'agent_chat':mode==='research'?'research_answer':'chat',mode,prompt:q,visual_rag:$('ragVisualChat').checked,browser_location:loc});
  content.textContent=d.answer||'No generated text';displaySources(ai,d);
  if(d.tools_used?.length){const u=el('ul');d.tools_used.forEach(t=>u.appendChild(el('li',t.name+(t.ok?' ✓':': '+t.error))));ai.appendChild(u)}
  if(d.route?.coordinates?.length){const b=el('button','View route in World Maps','secondary');b.type='button';b.addEventListener('click',()=>{mapState.route=d.route.coordinates;go('map');syncVectorOverlay();drawMap();focusRoute(d.route.coordinates);$('routeResult').textContent='AI calculated offline route: '+Math.round(d.route.distance_m/1000*10)/10+' km, '+Math.round(d.route.duration_s/60)+' minutes (no traffic)';});ai.appendChild(b)}
 }catch(ex){content.textContent='Error: '+ex.message;}log.scrollTop=log.scrollHeight;});
let currentImage='';
$('lensImage').addEventListener('change',e=>{const file=e.target.files[0];currentImage='';$('lensPreview').hidden=true;if(!file)return;if(file.size>12*1024*1024){error(clear('lensResult'),'Image exceeds 12 MB');return}const reader=new FileReader();reader.onload=()=>{currentImage=String(reader.result);$('lensPreview').src=currentImage;$('lensPreview').hidden=false};reader.readAsDataURL(file);});
$('lensForm').addEventListener('submit',async e=>{e.preventDefault();const box=clear('lensResult');if(!currentImage){error(box,'Choose an image first.');return}const q=$('lensQ').value;try{let mode=document.querySelector('[name=lensMode]:checked').value;if(mode==='auto'){const r=await ask({action:'route',prompt:q,image:currentImage});box.appendChild(el('p','Decision: '+r.decision.route+' ('+r.decision.method+')','muted'));mode=r.decision.route==='image_search'?'image_search':'vision';}const d=await ask({action:mode==='image_search'?'image_search':'vision',prompt:q,image:currentImage});if(d.answer)box.appendChild(el('p',d.answer));displaySources(box,d);}catch(ex){error(box,ex.message)}});
const translatorLanguages={auto:'Automatic detection',en:'English',pl:'Polish',de:'German',fr:'French',es:'Spanish',it:'Italian',pt:'Portuguese',nl:'Dutch',sv:'Swedish',fi:'Finnish',da:'Danish',no:'Norwegian',is:'Icelandic',et:'Estonian',lv:'Latvian',lt:'Lithuanian',cs:'Czech',sk:'Slovak',sl:'Slovenian',hr:'Croatian',bg:'Bulgarian',ro:'Romanian',hu:'Hungarian',el:'Greek',mt:'Maltese',ga:'Irish',ca:'Catalan',ru:'Russian',uk:'Ukrainian',be:'Belarusian',sr:'Serbian',bs:'Bosnian',tr:'Turkish',ar:'Arabic',zh:'Chinese',ja:'Japanese'};
for(const [code,name] of Object.entries(translatorLanguages)){
 const s=el('option',name);s.value=code;$('sourceLanguage').appendChild(s);
 if(code!=='auto'){const target=el('option',name);target.value=name;$('targetLanguage').appendChild(target)}
}
$('sourceLanguage').value='auto';$('targetLanguage').value='Polish';
$('translateForm').addEventListener('submit',async e=>{e.preventDefault();const box=clear('translateResult');try{const d=await ask({action:'translate',prompt:$('translateText').value,language:$('targetLanguage').value,source_language:$('sourceLanguage').value});box.appendChild(el('p',d.answer));}catch(ex){error(box,ex.message)}});
let speechRecorder=null,speechChunks=[],speechStream=null;
async function transcribeBlob(blob){const box=clear('asrResult');try{
 if(!blob||blob.size===0||blob.size>16*1024*1024)throw Error('Audio file must be 1 byte to 16 MiB');
 const resp=await fetch('/api/asr?language='+encodeURIComponent($('sourceLanguage').value),{method:'POST',headers:{'Content-Type':blob.type||'audio/webm'},body:blob});
 const res=await resp.json();if(!resp.ok)throw Error(res.error||'ASR unavailable');
 $('translateText').value=res.transcript||'';box.appendChild(el('p',res.transcript||'No recognized speech'));
 }catch(err){error(box,err.message)} }
$('asrUpload').addEventListener('click',async()=>{const f=$('asrFile').files[0];if(!f)return error(clear('asrResult'),'Select an audio file first');await transcribeBlob(f)});
$('asrRecord').addEventListener('click',async()=>{try{
 if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder)throw Error('Microphone recording is unavailable in this browser');
 speechStream=await navigator.mediaDevices.getUserMedia({audio:true});speechChunks=[];
 speechRecorder=new MediaRecorder(speechStream);speechRecorder.ondataavailable=e=>{if(e.data.size)speechChunks.push(e.data)};
 speechRecorder.onstop=async()=>{speechStream.getTracks().forEach(t=>t.stop());$('asrRecord').disabled=false;$('asrStop').disabled=true;await transcribeBlob(new Blob(speechChunks,{type:speechRecorder.mimeType||'audio/webm'}))};
 speechRecorder.start();$('asrRecord').disabled=true;$('asrStop').disabled=false;
 }catch(err){error(clear('asrResult'),err.message)} });
$('asrStop').addEventListener('click',()=>{if(speechRecorder?.state==='recording')speechRecorder.stop()});
$('mapSearch').addEventListener('click',()=>{const q=$('mapQ').value.trim()||'world map';window.open('http://localhost:8080/search?pattern='+encodeURIComponent(q),'_blank','noopener,noreferrer');});
$('locate').addEventListener('click',()=>{const out=$('positionResult');if(!navigator.geolocation){out.textContent='This browser cannot access geolocation. Enter a place manually.';return;}out.textContent='Requesting browser location permission...';navigator.geolocation.getCurrentPosition(p=>{const lat=p.coords.latitude.toFixed(5),lon=p.coords.longitude.toFixed(5);out.textContent='Local coordinates: '+lat+', '+lon+' (accuracy ~'+Math.round(p.coords.accuracy)+' m). Start point filled for offline routing.';$('mapQ').value=lat+', '+lon;$('routeStart').value=lat+', '+lon;mapState.lat=Number(lat);mapState.lon=Number(lon);mapState.z=12;if(mapState.vectorReady){mapState.vector.flyTo({center:[Number(lon),Number(lat)],zoom:12});syncVectorOverlay();}else drawMap();},e=>{out.textContent='Location unavailable or permission denied: '+e.message;},{enableHighAccuracy:false,timeout:9000,maximumAge:60000})});
async function hardware(){const box=clear('hardware');try{const r=await fetch('/api/status');const d=await r.json();for(const [label,v] of [['Profile',d.profile.tier],['Available RAM',d.profile.ram_free_mib+' MiB'],['Free NVIDIA VRAM',d.profile.vram_free_mib+' MiB'],['Context',d.profile.context+' tokens'],['KV cache',d.profile.kv],['Media index',d.profile.index_mode],['Model loading',d.model_residency?.policy||'unknown'],['Loaded models',(d.model_residency?.loaded||[]).join(', ')||'none'],['Active inference',(d.model_residency?.busy||[]).join(', ')||'idle']]){const card=el('div',null,'spec');card.appendChild(el('small',label));card.appendChild(el('b',v));box.appendChild(card)}$('residencyStatus').textContent='Available RAM budget: '+(d.model_residency?.usable_budget_mib??'unknown')+' MiB. Registered models: '+(d.model_residency?.registered||[]).join(', ')+'. Text + decision share memory when they fit, otherwise switch one at a time.';}catch(e){error(box,e.message)}}
$('refreshHardware').addEventListener('click',hardware);

// DoomsdayOS offline map UI. No external maps, scripts or geocoding services.
const mapState={lat:0,lon:0,z:2,tilesReady:false,vectorTiles:false,routerReady:false,route:null,
  dragging:false,pointer:null,moved:false,refresh:0,vector:null,vectorReady:false,vectorData:null};
let stopValues=[];
function mercator(lat,lon,z){const n=2**z;const y=(1-Math.asinh(Math.tan(Math.max(-85.05,Math.min(85.05,lat))*Math.PI/180))/Math.PI)/2*n;return [(lon+180)/360*n,y];}
function fromMercator(x,y,z){const n=2**z;return [Math.atan(Math.sinh(Math.PI*(1-2*y/n)))*180/Math.PI,((x/n*360+540)%360)-180];}
function pointValue(lat,lon){return Number(lat).toFixed(6)+','+Number(lon).toFixed(6)}
function routePointIds(){return ['routeStart',...stopValues.map((_,i)=>'routeVia'+(i+1)),'routeEnd'];}
function setRoutePoint(target,lat,lon){
  let n=Number(String(target).replace('routeVia',''));
  if(target.startsWith('routeVia')&&Number.isInteger(n)&&n>=1&&n<=3){while(stopValues.length<n)stopValues.push('');renderStops();}
  if(!$(target))throw Error('Add that stop first');$(target).value=pointValue(lat,lon);
  mapState.lat=lat;mapState.lon=lon;
  if(mapState.vectorReady)mapState.vector.flyTo({center:[lon,lat],zoom:Math.max(mapState.vector.getZoom(),8)});
  else {mapState.z=Math.max(8,mapState.z);drawMap()}
}
function renderStops(){const parent=clear('stopRows');stopValues.forEach((value,i)=>{
  const row=el('div',null,'stop-row');
  const lab=el('label','Stop '+(i+1));lab.htmlFor='routeVia'+(i+1);row.appendChild(lab);
  const inp=document.createElement('input');inp.id='routeVia'+(i+1);inp.placeholder='Intermediate place or lat,lon';inp.value=value;inp.addEventListener('input',()=>stopValues[i]=inp.value);row.appendChild(inp);
  const remove=el('button','Remove','secondary');remove.type='button';remove.addEventListener('click',()=>{stopValues.splice(i,1);renderStops()});row.appendChild(remove);parent.appendChild(row);
});$('addStop').disabled=stopValues.length>=3;}
$('addStop').addEventListener('click',()=>{if(stopValues.length<3){stopValues.push('');renderStops()}});
function parsePosition(value){const match=String(value).trim().match(/^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$/);
  if(!match)return null;const lat=+match[1],lon=+match[2];if(!Number.isFinite(lat)||!Number.isFinite(lon)||Math.abs(lat)>90||Math.abs(lon)>180)throw Error('Coordinates outside latitude/longitude limits');return {lat,lon};}
async function resolvePlace(input){const raw=input.trim();if(!raw)throw Error('Enter start, each stop, and destination');
  const pair=parsePosition(raw);if(pair)return {...pair,name:raw};
  if(/^(usa|us|united states|united kingdom|uk|europe|asia)$/i.test(raw))throw Error('A country or continent is not a specific destination. Choose a city, address, or map point.');
  const response=await ask({action:'find_places',prompt:raw});const hits=response.results||[];
  if(!hits.length)throw Error('No offline match for "'+raw+'". Import a local gazetteer, or enter lat,lon or click the map.');
  const same=hits.filter(x=>x.name.toLowerCase()===raw.toLowerCase());const choices=same.length?same:hits;
  if(choices.length!==1)throw Error('"'+raw+'" matches several places. Use Find offline, choose the exact result, then plan again.');
  return {...choices[0],name:choices[0].name};}
function routeOverlayPoints(){const result=[];for(const id of routePointIds()){
  const v=$(id)?.value;if(!v)continue;try{const point=parsePosition(v);if(point)result.push([point.lon,point.lat]);}catch(e){/* Ignore incomplete input while drawing */}}
 return result;}
function drawOverlay(ctx,cx,cy){const cv=$('offlineCanvas'),W=cv.width,H=cv.height,z=mapState.z;
 if(mapState.route?.length){ctx.save();ctx.beginPath();mapState.route.forEach(([lon,lat],i)=>{const [px,py]=mercator(lat,lon,z),x=(px-cx)*256+W/2,y=(py-cy)*256+H/2;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.strokeStyle='#80f4c4';ctx.lineWidth=5;ctx.stroke();ctx.restore()}
 routeOverlayPoints().forEach(([lon,lat],index)=>{const [px,py]=mercator(lat,lon,z),x=(px-cx)*256+W/2,y=(py-cy)*256+H/2;
 ctx.fillStyle=index===0?'#b4eb88':'#f0cc6d';ctx.beginPath();ctx.arc(x,y,6,0,Math.PI*2);ctx.fill();});}
function drawMap(){if(mapState.vectorReady)return;
 const cv=$('offlineCanvas'),ctx=cv.getContext('2d'),W=cv.width,H=cv.height,z=mapState.z,n=2**z;
 ctx.clearRect(0,0,W,H);ctx.fillStyle='#101e28';ctx.fillRect(0,0,W,H);const [cx,cy]=mercator(mapState.lat,mapState.lon,z);const epoch=++mapState.refresh;
 const left=Math.floor(cx-W/512)-1,right=Math.ceil(cx+W/512)+1,top=Math.max(0,Math.floor(cy-H/512)-1),bottom=Math.min(n-1,Math.ceil(cy+H/512)+1);
 for(let ty=top;ty<=bottom;ty++)for(let tx=left;tx<=right;tx++){
  const x=(tx-cx)*256+W/2,y=(ty-cy)*256+H/2;ctx.strokeStyle='rgba(180,214,235,.16)';ctx.strokeRect(x,y,256,256);
  if(!mapState.tilesReady||mapState.vectorTiles)continue;
  const img=new Image();img.onload=()=>{if(epoch===mapState.refresh){ctx.drawImage(img,x,y,256,256);drawOverlay(ctx,cx,cy)}};
  img.src='/api/maps/tiles/'+z+'/'+((tx%n+n)%n)+'/'+ty;
 }
 drawOverlay(ctx,cx,cy);
}
function vectorStyle(status){const minz=Math.max(0,+status.minzoom||0),maxz=Math.min(22,+status.maxzoom||14);
 return {version:8,name:'Local offline OpenMapTiles',sources:{local:{type:'vector',tiles:[location.origin+'/api/maps/vector/{z}/{x}/{y}'],minzoom:minz,maxzoom:maxz},
  ...(status.terrain_ready?{terrain:{type:'raster-dem',tiles:[location.origin+'/api/maps/terrain/{z}/{x}/{y}'],minzoom:0,maxzoom:+status.terrain_maxzoom||13,encoding:'mapbox'}}:{})},
  layers:[
   {id:'ground',type:'background',paint:{'background-color':'#1a2930'}},
   {id:'green',type:'fill',source:'local','source-layer':'landcover',paint:{'fill-color':'#233e31','fill-opacity':.84}},
   {id:'parks',type:'fill',source:'local','source-layer':'landuse',paint:{'fill-color':'#344d39','fill-opacity':.65}},
   {id:'water',type:'fill',source:'local','source-layer':'water',paint:{'fill-color':'#275d75'}},
   {id:'waterline',type:'line',source:'local','source-layer':'waterway',paint:{'line-color':'#4387a3','line-width':1.5}},
   {id:'minor-roads',type:'line',source:'local','source-layer':'transportation',paint:{'line-color':'#9caab1','line-width':['interpolate',['linear'],['zoom'],5,.35,14,2.0]}},
   {id:'buildings',type:'fill',source:'local','source-layer':'building',paint:{'fill-color':'#82959c','fill-opacity':.64}},
   {id:'extruded-buildings',type:'fill-extrusion',source:'local','source-layer':'building',layout:{visibility:'none'},
    paint:{'fill-extrusion-color':'#678b85','fill-extrusion-height':['coalesce',['to-number',['get','render_height']],['to-number',['get','height']],8],
           'fill-extrusion-base':0,'fill-extrusion-opacity':.8}}
  ]};}
function routeGeoJson(){return {type:'FeatureCollection',features:[
 ...(mapState.route?.length?[{type:'Feature',geometry:{type:'LineString',coordinates:mapState.route},properties:{kind:'route'}}]:[]),
 ...routeOverlayPoints().map((p,i)=>({type:'Feature',geometry:{type:'Point',coordinates:p},properties:{number:i+1}}))]};}
function syncVectorOverlay(){if(!mapState.vectorReady)return;const map=mapState.vector;if(!map.isStyleLoaded())return;const collection=routeGeoJson();
 if(map.getSource('trip'))map.getSource('trip').setData(collection);else{
  map.addSource('trip',{type:'geojson',data:collection});
  map.addLayer({id:'trip-line',type:'line',source:'trip',filter:['==',['geometry-type'],'LineString'],paint:{'line-color':'#d0ed88','line-width':5}});
  map.addLayer({id:'trip-points',type:'circle',source:'trip',filter:['==',['geometry-type'],'Point'],paint:{'circle-color':'#f0cc6d','circle-radius':7,'circle-stroke-color':'#14292a','circle-stroke-width':2}});
 }
}
function focusRoute(points){if(mapState.vectorReady){const bounds=new maplibregl.LngLatBounds();points.forEach(p=>bounds.extend(p));mapState.vector.fitBounds(bounds,{padding:65,maxZoom:14});return;}
 const lon=points.map(p=>p[0]),lat=points.map(p=>p[1]);mapState.lon=(Math.min(...lon)+Math.max(...lon))/2;mapState.lat=(Math.min(...lat)+Math.max(...lat))/2;
 for(let z=12;z>=1;z--){const a=mercator(Math.min(...lat),Math.min(...lon),z),b=mercator(Math.max(...lat),Math.max(...lon),z);
 if(Math.abs(a[0]-b[0])*256<720&&Math.abs(a[1]-b[1])*256<380){mapState.z=z;break}}drawMap();}
function setupVectorMap(status){if(!window.maplibregl)return;
 try{
  const view=$('vectorMap');view.hidden=false;$('offlineCanvas').hidden=true;
  const map=new maplibregl.Map({container:view,style:vectorStyle(status),center:[mapState.lon,mapState.lat],zoom:mapState.z,
                              attributionControl:false,dragRotate:true,pitchWithRotate:true});
  mapState.vector=map;mapState.vectorReady=true;
  map.addControl(new maplibregl.NavigationControl({showCompass:true}),'top-right');
  map.on('load',()=>{syncVectorOverlay();view.classList.add('ready')});
  map.on('click',e=>{setRoutePoint($('mapTarget').value,e.lngLat.lat,e.lngLat.lng);syncVectorOverlay()});
  if(mapState.route?.length)focusRoute(mapState.route);
 }catch(e){mapState.vector=null;mapState.vectorReady=false;$('vectorMap').hidden=true;$('offlineCanvas').hidden=false;drawMap();$('offlineMapStatus').textContent='Vector renderer could not start: '+e.message;}
}
async function loadLocalMapLibre(status){
 const css=document.createElement('link');css.rel='stylesheet';css.href='/static/vendor/maplibre-gl.css';document.head.appendChild(css);
 const script=document.createElement('script');script.src='/static/vendor/maplibre-gl.js';script.onload=()=>setupVectorMap(status);
 script.onerror=()=>{$('offlineMapStatus').textContent='Local MapLibre assets could not load; install them once with setup-maplibre.sh.'};document.head.appendChild(script);
}
async function mapInfo(){try{const r=await fetch('/api/maps/status');const status=await r.json();
 mapState.tilesReady=!!status.tiles_ready;mapState.vectorTiles=!!status.vector_tiles_ready;
 const names=[status.tile_name||'No local tiles',status.router_data_ready?'car graph ready':'car graph missing',status.walking_graph_ready?'walk graph ready':'walk graph missing',status.offroad_trail_data_ready?'off-road trails ready':'trails missing'];
 if(status.vector_tiles_ready&&!status.maplibre_assets_ready)names.push('install MapLibre assets for vector view');
 $('offlineMapStatus').textContent=names.join(' · ');mapState.z=Math.min(+status.maxzoom||18,mapState.z);
 if(status.vector_tiles_ready&&status.maplibre_assets_ready)loadLocalMapLibre(status);
 else drawMap();
 }catch(e){$('offlineMapStatus').textContent='Offline maps: '+e.message;drawMap()}}
$('mapZoomIn').addEventListener('click',()=>{if(mapState.vectorReady)mapState.vector.zoomIn();else{mapState.z=Math.min(18,mapState.z+1);drawMap()}});
$('mapZoomOut').addEventListener('click',()=>{if(mapState.vectorReady)mapState.vector.zoomOut();else{mapState.z=Math.max(0,mapState.z-1);drawMap()}});
$('map3D').addEventListener('click',()=>{if(mapState.vectorReady){const map=mapState.vector;const next=map.getPitch()<10;
 map.easeTo({pitch:next?55:0,bearing:next?map.getBearing()+15:0});if(map.getLayer('extruded-buildings'))map.setLayoutProperty('extruded-buildings','visibility',next?'visible':'none');
 if(map.getSource('terrain'))map.setTerrain(next?{source:'terrain',exaggeration:1.2}:null);
 }else $('offlineMapStatus').textContent='3D requires installed local MapLibre assets and vector tiles containing building heights. StreetZim offers terrain in its own viewer.'});
$('offlineCanvas').addEventListener('pointerdown',e=>{e.target.setPointerCapture(e.pointerId);mapState.dragging=true;mapState.pointer=[e.offsetX,e.offsetY];mapState.moved=false});
$('offlineCanvas').addEventListener('pointerup',e=>{if(mapState.dragging&&!mapState.moved){const cv=$('offlineCanvas'),scale=cv.width/cv.clientWidth,
  x=e.offsetX*scale,y=e.offsetY*(cv.height/cv.clientHeight),[cx,cy]=mercator(mapState.lat,mapState.lon,mapState.z);
  const [lat,lon]=fromMercator(cx+(x-cv.width/2)/256,cy+(y-cv.height/2)/256,mapState.z);setRoutePoint($('mapTarget').value,lat,lon)}
 mapState.dragging=false;mapState.pointer=null});
$('offlineCanvas').addEventListener('pointercancel',()=>{mapState.dragging=false;mapState.pointer=null});
$('offlineCanvas').addEventListener('pointermove',e=>{if(!mapState.dragging||!mapState.pointer)return;const cv=$('offlineCanvas'),scale=cv.width/cv.clientWidth,
 dx=(e.offsetX-mapState.pointer[0])*scale,dy=(e.offsetY-mapState.pointer[1])*scale;mapState.pointer=[e.offsetX,e.offsetY];
 if(Math.abs(dx)+Math.abs(dy)>1)mapState.moved=true;const [cx,cy]=mercator(mapState.lat,mapState.lon,mapState.z);
 [mapState.lat,mapState.lon]=fromMercator(cx-dx/256,cy-dy/256,mapState.z);drawMap()});
$('offlineCanvas').addEventListener('wheel',e=>{e.preventDefault();mapState.z=Math.max(0,Math.min(18,mapState.z+(e.deltaY<0?1:-1)));drawMap()},{passive:false});
$('routeGo').addEventListener('click',async()=>{const box=clear('routeResult');box.appendChild(el('p','Finding route using installed offline networks…','muted'));
 try{
 const points=[];for(const id of routePointIds())points.push(await resolvePlace($(id).value));
 const mode=$('routeMode').value;const d=await ask({action:'route_multi',points,mode});
 if(!Array.isArray(d.coordinates)||!d.coordinates.length)throw Error('Local router returned no geometry');
 mapState.route=d.coordinates;focusRoute(d.coordinates);syncVectorOverlay();clear('routeResult');
 const summary=el('p',(d.distance_m/1000).toFixed(1)+' km · about '+Math.round(d.duration_s/60)+' minutes · '+(mode==='offroad'?'Trail-only, road crossings excluded where mapped':mode==='car'?'Road driving':'Walking'));
 box.appendChild(summary);box.appendChild(el('p',d.note||'Offline estimate','muted'));
 (d.legs||[]).forEach((leg,i)=>{box.appendChild(el('h3','Leg '+(i+1)+' · '+(leg.distance_m/1000).toFixed(1)+' km'));
 const list=el('ol');(leg.steps||[]).slice(0,35).forEach(s=>list.appendChild(el('li',s.instruction+' '+s.name+' · '+Math.round(s.distance_m)+' m')));box.appendChild(list) });
 }catch(e){clear('routeResult');mapState.route=null;syncVectorOverlay();drawMap();error(box,e.message)}});
$('routeClear').addEventListener('click',()=>{mapState.route=null;stopValues=[];renderStops();$('routeStart').value='';$('routeEnd').value='';clear('routeResult');syncVectorOverlay();drawMap()});
$('offlinePlaceFind').addEventListener('click',async()=>{const box=clear('offlinePlaceResults');try{
 const r=await ask({action:'find_places',prompt:$('offlinePlace').value});if(!r.results.length){box.appendChild(el('p','No offline match. Import a gazetteer CSV with doomsday-maps places.'));return;}
 r.results.forEach(p=>{const b=el('button',p.name+' · '+p.lat.toFixed(4)+', '+p.lon.toFixed(4),'secondary');b.type='button';b.addEventListener('click',()=>{
   setRoutePoint($('mapTarget').value,p.lat,p.lon);syncVectorOverlay();});box.appendChild(b)});
 }catch(e){error(box,e.message)}});
renderStops();mapInfo();

$('mediaSearchForm').addEventListener('submit',async e=>{e.preventDefault();const box=clear('mediaSearchResult');try{const data=await ask({action:'media_search',prompt:$('mediaSearchQ').value,kind:$('mediaKind').value});displaySources(box,data);if(!data.results.length)box.appendChild(el('p','No indexed media matched. The background ZIM index may still be incomplete.'));}catch(ex){error(box,ex.message)}});
async function recoveryStatus(){const box=$('recoveryWatchStatus');if(!box)return;try{const r=await fetch('/api/recovery/status');const d=await r.json();box.textContent=d.recovery_plan_ready?'Recovery plan ready after '+d.incomplete_boots+' incomplete boots. Inspect: sudo doomsday-recovery-watch plan':'Recovery watcher: '+d.incomplete_boots+' previous incomplete boots. No automatic destructive rollback.';}catch(e){box.textContent='Recovery status temporarily unavailable.'}}
recoveryStatus();


// v7.1 source-grounded research: article names, PDF pages, and explicit HTTPS imports.
let researchSources=[];
async function refreshResearchStatus(){
 const out=$('researchStatus');if(!out)return;
 try{const d=await (await fetch('/api/research/status')).json();
 out.textContent=`Local sources: ${d.sources} · PDFs: ${d.pdfs} · webpages: ${d.webpages} · indexed passages: ${d.indexed_chunks}. Internet: disabled by default.`;
 }catch(e){out.textContent='Research library is currently unavailable: '+e.message;}
}
function showResearchResults(results){
 researchSources=results;const box=clear('researchResults');
 if(!results.length){box.appendChild(el('p','No indexed sources matched. Try an article title or import a document.','muted'));return;}
 for(const item of results){
   const row=el('div',null,'research-hit');const top=el('div',null,'research-hit-head');
   const choose=el('input');choose.type='checkbox';choose.className='research-choice';choose.value=item.id;choose.setAttribute('aria-label','Use source: '+item.title);
   top.appendChild(choose);
   const label=el('div');const title=el('strong');link(title,item.title,item.link);label.appendChild(title);
   label.appendChild(el('small',item.kind.toUpperCase()+(item.page?' · PDF page '+item.page:'')));
   top.appendChild(label);row.appendChild(top);
   row.appendChild(el('p',item.snippet||'Indexed article — open the source to inspect it.','muted'));
   const show=el('button','Read indexed text','secondary');show.type='button';
   show.addEventListener('click',async()=>{const preview=el('div',null,'research-preview');preview.textContent='Opening local excerpt…';row.appendChild(preview);
    try{const r=await ask({action:'research_open',source_id:item.id});preview.textContent=r.source.text||'No extracted text';}
    catch(e){preview.textContent='Source unavailable: '+e.message;}});
   row.appendChild(show);box.appendChild(row);
 }
}
$('researchSearchForm').addEventListener('submit',async e=>{e.preventDefault();const out=clear('researchResults');out.appendChild(el('p','Searching indexed sources…','muted'));
 try{const data=await ask({action:'research_search',prompt:$('researchQuery').value});showResearchResults(data.results||[])}
 catch(err){clear('researchResults');error(out,err.message)}
});
$('researchPdfForm').addEventListener('submit',async e=>{e.preventDefault();const out=clear('researchPdfResult'),file=$('researchPdf').files[0];
 if(!file)return;if(file.size>40*1024*1024){error(out,'PDF exceeds 40 MiB');return;}
 out.appendChild(el('p','Extracting page text locally…','muted'));
 try{const res=await fetch('/api/research/upload',{method:'POST',headers:{'Content-Type':'application/pdf','X-File-Name':encodeURIComponent(file.name)},body:file});
   const data=await res.json();if(!res.ok)throw Error(data.error||'Upload failed');
   clear('researchPdfResult');out.appendChild(el('p','Indexed '+data.source.title+' · '+data.source.pages+' pages · '+data.source.chunks+' passages.'));
   await refreshResearchStatus();
 }catch(err){clear('researchPdfResult');error(out,err.message)}
});
$('researchWebForm').addEventListener('submit',async e=>{e.preventDefault();const out=clear('researchWebResult');
 try{const d=await ask({action:'research_import_url',url:$('researchUrl').value,allow_online:$('researchOnline').checked});
 out.appendChild(el('p','Indexed: '+d.source.title));$('researchOnline').checked=false;await refreshResearchStatus();
 }catch(err){error(out,err.message)}
});
$('researchAskForm').addEventListener('submit',async e=>{e.preventDefault();const out=clear('researchAnswer');out.appendChild(el('p','Collecting evidence…','muted'));
 try{const selected=$('researchSelectedOnly').checked?Array.from(document.querySelectorAll('.research-choice:checked')).map(c=>c.value):null;
   if(selected&&selected.length===0)throw Error('Select at least one indexed source above.');
   const data=await ask({action:'research_answer',prompt:$('researchQuestion').value,source_ids:selected});
   clear('researchAnswer');out.appendChild(el('p',data.answer||'No model answer.'));
   displaySources(out,data);
 }catch(err){clear('researchAnswer');error(out,err.message)}
});
refreshResearchStatus();
