// app.js — la interfaz de estudio. Vive en un archivo aparte (antes iba dentro
// de index.html) para que la política de contenido pueda prohibir los scripts
// en línea: el HTML de las fichas viene de archivos de Anki y no debe poder
// ejecutar nada.
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
// Todo lo que viene de la base y se mete como HTML pasa por aquí. Escapa
// también la comilla simple: un valor dentro de un atributo con '…' no debe
// poder cerrarlo.
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(url, opts){
  const r = await fetch(url, Object.assign({credentials:'same-origin'}, opts));
  // sin sesión (caducó, o la cerró el admin): a la pantalla de entrada
  if (r.status === 401){ location.href = '/entrar'; throw new Error('Tu sesión terminó.'); }
  if(!r.ok){
    const d = await r.json().catch(()=>({}));
    throw new Error(typeof d.detail === 'string' ? d.detail : r.statusText);
  }
  return r.json();
}
const post = (u,b) => api(u,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(b||{})});
const del  = u => api(u,{method:'DELETE'});
let tt; function toast(m){ const t=$('#toast'); t.textContent=m; t.classList.add('on');
  clearTimeout(tt); tt=setTimeout(()=>t.classList.remove('on'),2600); }

const S = {user:{}, folders:[], topics:[], stats:{}, settings:{}, pdf_ok:true,
           sec:'inicio', topic:null, railTab:'postit', placing:false,
           queue:[], card:null, revealed:false};

/* ------------------------------------------------------------------ tema */
async function boot(){
  const d = await api('/api/bootstrap');
  Object.assign(S, d);
  S.admin = S.user.role === 'admin';
  // Lo que cambia el material compartido solo se le muestra al admin. El
  // servidor lo exige igual (403): esto es solo para no ofrecer botones inútiles.
  $$('.solo-admin').forEach(el => { el.hidden = !S.admin; });
  // En producción el material se agrega con git: la carga no existe allí.
  $('#uploadBox').hidden = !(S.admin && S.subidas);
  $('#quien').textContent = S.user.nombre || S.user.username || '';
  applyTheme(S.settings.theme || 'auto');
  paintTree(); paintStats(); go(S.sec);
  vigilarIngesta();
}

/* Justo después de un despliegue (o de que el servidor despierte) la ingesta
   del material corre unos segundos en segundo plano y la biblioteca puede
   llegar vacía o a medias. Se avisa y se vuelve a pedir, sin tocar lo que se
   esté estudiando: solo la barra lateral y, si está a la vista, el inicio. */
let vigilando = null;
function vigilarIngesta(){
  if (!S.actualizando || vigilando) return;
  toast('Actualizando el material…');
  vigilando = setTimeout(async () => {
    vigilando = null;
    try {
      const d = await api('/api/bootstrap');
      Object.assign(S, {folders: d.folders, topics: d.topics, orphans: d.orphans,
                        stats: d.stats, actualizando: d.actualizando});
      paintTree(); paintStats();
      if (S.sec === 'inicio') renderInicio();
    } catch(e){ toast(e.message); return; }
    vigilarIngesta();
  }, 3000);
}

$('#salir').onclick = async () => {
  try { await post('/api/auth/logout'); } catch(e){ /* se sale igual */ }
  location.href = '/entrar';
};

function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  $$('#themeSeg button').forEach(b => b.classList.toggle('on', b.dataset.theme === t));
}
$$('#themeSeg button').forEach(b => b.onclick = async () => {
  applyTheme(b.dataset.theme);
  try { S.settings = await post('/api/settings', {theme: b.dataset.theme}); }
  catch(e){ toast(e.message); }
});

function paintStats(){
  const s = S.stats, due = (s.vencidas||0) + (s.aprendiendo||0);
  const badge = $('#dueBadge');
  badge.textContent = due; badge.style.display = due ? '' : 'none';
  $('#streakNum').textContent = s.racha || 0;
  $('#flame').style.filter = s.racha ? 'none' : 'grayscale(1)';
  $('#flame').style.opacity = s.racha ? 1 : .5;
}

/* --------------------------------------------------------------- sidebar */
function paintTree(){
  const sinCarpeta = S.topics.filter(t => !t.folder_id);
  const bloques = S.folders.map(f => ({f, ts: S.topics.filter(t => t.folder_id === f.id)}));
  if (sinCarpeta.length) bloques.push({f:{id:0,name:'Sin carpeta',color:'#8a8378'}, ts:sinCarpeta});

  $('#tree').innerHTML = bloques.map(({f,ts}) => `
    <div class="folder">
      <div class="folder-row" data-folder="${f.id}">
        <span class="dot" style="background:${esc(f.color)}"></span>
        <span>${esc(f.name)}</span>
        ${f.id && S.admin ? `<span class="gear" data-edit="${f.id}">⋯</span>` : ''}
        <span class="n">${ts.length}</span>
      </div>
      ${ts.map(t => `
        <div class="topic-row ${S.topic===t.id?'on':''}" data-topic="${t.id}">
          <span>${esc(t.title)}</span>
          ${t.vencidas ? `<span class="due">${t.vencidas}</span>` : ''}
        </div>`).join('')}
    </div>`).join('');

  $$('[data-topic]').forEach(el => el.onclick = () => openTopic(+el.dataset.topic));
  $$('[data-folder]').forEach(el => el.onclick = e => {
    if (e.target.dataset.edit) return;
    S.folderView = +el.dataset.folder || null;
    go('inicio');
  });
  $$('[data-edit]').forEach(el => el.onclick = async e => {
    e.stopPropagation();
    const f = S.folders.find(x => x.id === +el.dataset.edit);
    const name = await uiPrompt('Déjalo vacío para borrar la carpeta. Sus temas no se borran.',
                                f.name, {title:'Nombre de la carpeta'});
    if (name === null) return;
    try {
      if (!name.trim()){
        const si = await uiConfirm(`Los temas de «${f.name}» quedarán sin carpeta.`,
                                   {title:'¿Borrar la carpeta?', ok:'Borrar', danger:true});
        if (!si) return;
        const r = await del('/api/folders/' + f.id);
        S.folders = r.folders; S.topics = r.topics; paintTree(); toast('Carpeta borrada');
      } else {
        const r = await post('/api/folders/' + f.id, {name: name.trim()});
        S.folders = r.folders; paintTree();
      }
    } catch(err){ toast(err.message); }
  });
}

$('#addFolder').onclick = async () => {
  const name = await uiPrompt('', '', {title:'Carpeta nueva', ok:'Crear'});
  if (!name || !name.trim()) return;
  try {
    const r = await post('/api/folders', {name: name.trim()});
    S.folders = r.folders; paintTree(); toast('Carpeta creada');
  } catch(err){ toast(err.message); }
};

$$('.navbtn').forEach(b => b.onclick = () => go(b.dataset.sec));

/* ---------------------------------------------------------------- rutas */
function go(sec, opts){
  S.sec = sec;
  $$('.navbtn').forEach(b => b.classList.toggle('active', b.dataset.sec === sec));
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'v-' + sec));
  if (sec === 'inicio')  renderInicio();
  if (sec === 'repaso')  startStudy(opts || {});
  if (sec === 'ajustes') renderAjustes();
}

/* --------------------------------------------------------------- inicio */
function renderInicio(){
  S.topic = null;
  const fv = S.folderView ? S.folders.find(f => f.id === S.folderView) : null;
  const ambito = fv ? {folder_id: fv.id, name: fv.name}
                    : {name: 'Todos los temas'};
  const temas = fv ? S.topics.filter(t => t.folder_id === fv.id) : S.topics;
  const fichas = temas.reduce((a,t) => a + t.total, 0);
  // Lo que de verdad toca hoy: las vencidas más lo que queda del tope diario
  // de nuevas. Sumar TODAS las nuevas daba «1692 para hoy» con un tope de 20.
  const nuevasQuedan = Math.max(0, (S.stats.nuevas_limite || 20) - (S.stats.nuevas_hoy || 0));
  const hoy    = temas.reduce((a,t) => a + t.vencidas, 0)
               + Math.min(temas.reduce((a,t) => a + t.nuevas, 0), nuevasQuedan);

  $('#ttl').textContent = fv ? fv.name : 'Temas';
  $('#sub').textContent = `${temas.length} temas · ${fichas} fichas · ${hoy} para hoy`;
  $('#topActions').innerHTML = `
    <button class="btn solid" id="goStudy">Repasar${fv ? ' la carpeta' : ' todo'} (${hoy})</button>
    <button class="btn" id="goFree" title="No altera la programación">🔁 Repaso libre</button>
    ${fv ? `<button class="btn ghost" id="allTopics">Ver todo</button>`
         : S.admin ? `<button class="btn ghost" id="rescan">Re-escanear</button>` : ''}`;
  $('#goStudy').onclick = () => go('repaso', ambito);
  $('#goFree').onclick  = () => go('repaso', {...ambito, mode:'libre'});
  if (fv) $('#allTopics').onclick = () => { S.folderView = null; go('inicio'); };
  else if (S.admin) $('#rescan').onclick = rescan;

  if (fv){
    $('#v-inicio').innerHTML = temas.length
      ? `<div class="grid">${temas.map(cardHtml).join('')}</div>`
      : `<div class="empty">Esta carpeta todavía no tiene temas.</div>`;
  } else {
    const sinCarpeta = S.topics.filter(t => !t.folder_id);
    const bloques = S.folders.map(f => ({f, ts: S.topics.filter(t => t.folder_id === f.id)}))
                             .filter(b => b.ts.length);
    if (sinCarpeta.length) bloques.push({f:{name:'Sin carpeta'}, ts:sinCarpeta});
    // Lo recién desplegado va primero: temas nuevos y temas con material nuevo
    const recientes = S.topics.filter(t => t.is_new || t.is_updated);
    if (recientes.length) bloques.unshift({f:{name:'Recién agregado'}, ts:recientes, reciente:true});
    $('#v-inicio').innerHTML = bloques.length ? bloques.map(({f,ts}) => `
      <div class="sec-h"><h2>${esc(f.name)}</h2><span class="line"></span>
        <span class="muted" style="font-size:12px">${ts.reduce((a,t)=>a+t.total,0)} fichas</span></div>
      <div class="grid">${ts.map(cardHtml).join('')}</div>`).join('')
      : `<div class="empty">No hay temas todavía.${S.admin
         ? ' Carga un archivo de fichas o pulsa «Re-escanear».' : ''}</div>`;
  }
  $$('[data-open]').forEach(el => el.onclick = () => openTopic(+el.dataset.open));
  setRail(null);
}

function cardHtml(t){
  const mats = [['resumen','resumen'],['anki','fichas'],['articulo','artículos']];
  const tag = t.is_new ? '<span class="nuevo">Nuevo</span>'
            : t.is_updated ? '<span class="nuevo act">Actualizado</span>' : '';
  return `<button class="tcard" data-open="${t.id}">
    <h3>${tag}${esc(t.title)}</h3>
    <div class="chips">
      <span class="chip ${t.vencidas?'due':''}">${t.vencidas} pendientes</span>
      <span class="chip ${t.nuevas?'new':''}">${t.nuevas} nuevas</span>
      <span class="chip">${t.total} fichas</span>
    </div>
    <div class="mats">${mats.filter(([k]) => k !== 'articulo' || t.assets[k]).map(([k,l]) =>
      `<span class="mat ${t.assets[k]?'':'miss'}">${k === 'articulo' ? t.assets[k].length + ' ' : ''}${l}</span>`).join('')}</div>
  </button>`;
}

/* ----------------------------------------------------------------- tema */
async function openTopic(id){
  const t = await api('/api/topics/' + id);
  S.topic = id; S.topicData = t; S.placing = false;
  paintTree();
  $$('.navbtn').forEach(b => b.classList.remove('active'));
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'v-tema'));
  S.sec = 'tema';

  const resumen = (t.assets.resumen || [])[0];
  $('#ttl').textContent = t.title;
  $('#sub').textContent = `${t.cards.length} fichas · ${t.pages || 0} págs. de resumen`;
  $('#topActions').innerHTML =
    `<button class="btn solid" id="tStudy">Repasar</button>
     <button class="btn" id="tFree" title="No altera la programación">🔁 Libre</button>
     ${resumen && S.pdf_ok ? `<button class="btn" id="tPin">📌 Pegar post-it</button>` : ''}`;
  $('#tStudy').onclick = () => go('repaso', {topic_id:id, name:t.title});
  $('#tFree').onclick  = () => go('repaso', {topic_id:id, name:t.title, mode:'libre'});
  if (resumen && S.pdf_ok) $('#tPin').onclick = () => {
    S.placing = !S.placing;
    $('#tPin').classList.toggle('btn', true);
    $('#tPin').textContent = S.placing ? '✕ Cancelar' : '📌 Pegar post-it';
    $$('.page').forEach(p => p.classList.toggle('placing', S.placing));
    if (S.placing) toast('Haz clic donde quieras pegar la nota');
  };

  renderPages();
  setRail('tema');
}

function renderPages(){
  const t = S.topicData, resumen = (t.assets.resumen || [])[0];
  if (!resumen){
    $('#v-tema').innerHTML = `<div class="empty">Este tema no tiene resumen en PDF.${S.admin
      ? (S.subidas ? '<br>Cárgalo con «＋ Cargar archivo».'
                   : '<br>Agrégalo con git en contenido/ y aparece en el siguiente despliegue.')
      : ''}</div>`;
    return;
  }
  if (!S.pdf_ok){
    $('#v-tema').innerHTML = `<div class="pages">
      <div class="empty" style="margin-bottom:14px">Para pegar post-it sobre el resumen
        falta poppler: <code>brew install poppler</code>. Mientras, el PDF se ve así:</div>
      <iframe src="/api/assets/${resumen.id}/file" style="width:100%;height:78vh;
        border:1px solid var(--line);border-radius:10px"></iframe></div>`;
    return;
  }
  const n = t.pages || 1;
  const arts = (t.assets.articulo || []);
  const artsHtml = arts.length ? `<div class="arts"><span class="lbl">Artículos relacionados</span>${
    arts.map(a => `<a class="btn" href="/api/assets/${a.id}/file" target="_blank" rel="noopener noreferrer">${esc(a.filename.replace(/\.pdf$/i,''))}</a>`).join('')}</div>` : '';
  $('#v-tema').innerHTML = artsHtml + `<div class="pages">${
    Array.from({length:n}, (_,i) => `
      <div class="page ${S.placing?'placing':''}" data-page="${i+1}">
        <img src="/api/assets/${resumen.id}/page/${i+1}" alt="Página ${i+1}" loading="lazy">
      </div>`).join('')}</div>`;
  $$('.page').forEach(p => {
    p.onclick = e => onPageClick(e, p, resumen.id);
    paintStickies(p, resumen.id);
  });
}

function paintStickies(pageEl, assetId){
  const page = +pageEl.dataset.page;
  pageEl.querySelectorAll('.sticky').forEach(el => el.remove());
  S.topicData.notes.filter(nt => nt.kind === 'sticky' && nt.page === page)
    .forEach(nt => pageEl.appendChild(stickyEl(nt, pageEl)));
}

function stickyEl(nt, pageEl){
  const el = document.createElement('div');
  el.className = 'sticky';
  el.style.left = (nt.x * 100) + '%';
  el.style.top  = (nt.y * 100) + '%';
  el.innerHTML = `<button class="x" title="Borrar">×</button>
    <textarea placeholder="Escribe aquí…">${esc(nt.text)}</textarea>`;
  el.onclick = e => e.stopPropagation();      // no dispara la colocación de otra nota

  const ta = el.querySelector('textarea');
  let save;
  ta.oninput = () => { clearTimeout(save);
    save = setTimeout(() => post('/api/notes/' + nt.id, {text: ta.value})
      .then(() => {
        nt.text = ta.value;
        // la pestaña «Post-its» del panel derecho muestra el mismo texto
        if (S.railMode === 'tema' && S.railTab === 'postit') renderRail();
      })
      .catch(e => toast(e.message)), 600); };

  el.querySelector('.x').onclick = async e => {
    e.stopPropagation();
    await del('/api/notes/' + nt.id);
    S.topicData.notes = S.topicData.notes.filter(x => x.id !== nt.id);
    el.remove(); renderRail(); toast('Nota borrada');
  };

  // Arrastrar: la posición se guarda en porcentaje, así que la nota se queda
  // en su sitio aunque cambie el tamaño de la ventana o la escala del PDF.
  el.onpointerdown = e => {
    if (e.target.tagName === 'TEXTAREA' || e.target.classList.contains('x')) return;
    e.preventDefault();
    const rect = pageEl.getBoundingClientRect();
    const move = ev => {
      nt.x = Math.min(Math.max((ev.clientX - rect.left) / rect.width, 0), 1);
      nt.y = Math.min(Math.max((ev.clientY - rect.top) / rect.height, 0), 1);
      el.style.left = (nt.x*100) + '%'; el.style.top = (nt.y*100) + '%';
    };
    const up = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      post('/api/notes/' + nt.id, {x: nt.x, y: nt.y});
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  };
  return el;
}

async function onPageClick(e, pageEl, assetId){
  if (!S.placing) return;
  const rect = pageEl.getBoundingClientRect();
  const x = (e.clientX - rect.left) / rect.width;
  const y = (e.clientY - rect.top)  / rect.height;
  const r = await post(`/api/topics/${S.topic}/notes`,
    {kind:'sticky', asset_id:assetId, page:+pageEl.dataset.page, x, y, text:''});
  S.topicData.notes = r.notes;
  S.placing = false;
  $('#tPin').textContent = '📌 Pegar post-it';
  $$('.page').forEach(p => p.classList.remove('placing'));
  paintStickies(pageEl, assetId); renderRail();
  pageEl.querySelector('.sticky:last-child textarea')?.focus();
}

/* ----------------------------------------------------------------- rail */
function setRail(mode){
  S.railMode = mode;
  $('#app').classList.toggle('no-rail', !mode);
  if (!mode) return;
  S.railTab = mode === 'tema' ? 'postit' : 'resumen';
  renderRail();
}

function renderRail(){
  if (S.railMode === 'tema'){
    const tabs = [['postit','📌 Post-its'],['general','📝 Notas'],['fichas','🎴 Fichas']];
    $('#railtabs').innerHTML = tabs.map(([k,l]) =>
      `<button class="${S.railTab===k?'on':''}" data-tab="${k}">${l}</button>`).join('');
    $$('#railtabs button').forEach(b => b.onclick = () => { S.railTab=b.dataset.tab; renderRail(); });
    $('#railbody').className = 'railbody';
    $('#railbody').innerHTML = railTemaBody();
    wireRailNotes();
  } else if (S.railMode === 'repaso'){
    $('#railtabs').innerHTML = `<button class="on">📄 Resumen del tema</button>`;
    $('#railbody').className = 'railbody railpdf';
    $('#railbody').innerHTML = railResumen();
  }
}

function railTemaBody(){
  const t = S.topicData;
  if (S.railTab === 'fichas'){
    return `<div class="fichas">${t.cards.map(c =>
      `<div class="f"><div class="q">${c.front}</div><div class="a">${c.back}</div></div>`
    ).join('') || '<div class="empty">Sin fichas todavía.</div>'}</div>`;
  }
  const kind = S.railTab === 'postit' ? 'sticky' : 'general';
  const notes = t.notes.filter(n => n.kind === kind);
  const add = kind === 'general'
    ? `<button class="btn" style="width:100%;margin-bottom:11px" id="addGeneral">＋ Nota nueva</button>`
    : `<div class="muted" style="font-size:12px;margin-bottom:11px">
         Pulsa «📌 Pegar post-it» y haz clic sobre el resumen.</div>`;
  return add + (notes.map(n => `
    <div class="notecard" data-note="${n.id}">
      <button class="x">×</button>
      ${kind==='sticky' ? `<div class="meta">Página ${n.page}</div>` : ''}
      <textarea placeholder="Escribe aquí…">${esc(n.text)}</textarea>
    </div>`).join('') || `<div class="empty">Sin notas todavía.</div>`);
}

function wireRailNotes(){
  const ga = $('#addGeneral');
  if (ga) ga.onclick = async () => {
    const r = await post(`/api/topics/${S.topic}/notes`, {kind:'general', text:''});
    S.topicData.notes = r.notes; renderRail();
    $('#railbody textarea')?.focus();
  };
  $$('#railbody [data-note]').forEach(el => {
    const id = +el.dataset.note, ta = el.querySelector('textarea');
    let save;
    ta.oninput = () => { clearTimeout(save);
      save = setTimeout(() => post('/api/notes/' + id, {text: ta.value}).then(() => {
        const n = S.topicData.notes.find(x => x.id === id); if (n) n.text = ta.value;
        if (S.railTab === 'postit') $$('.page').forEach(p =>
          paintStickies(p, (S.topicData.assets.resumen||[])[0]?.id));
      }), 600); };
    el.querySelector('.x').onclick = async () => {
      await del('/api/notes/' + id);
      S.topicData.notes = S.topicData.notes.filter(x => x.id !== id);
      renderRail();
      $$('.page').forEach(p => paintStickies(p, (S.topicData.assets.resumen||[])[0]?.id));
    };
  });
}

function railResumen(){
  const t = S.topics.find(x => x.id === S.studyTopic);
  const res = t && t.assets.resumen ? t.assets.resumen[0] : null;
  if (!res) return `<div class="empty" style="margin:14px">Este repaso mezcla varios temas,
    o el tema no tiene resumen.</div>`;
  if (!S.pdf_ok) return `<iframe src="/api/assets/${res.id}/file"
    style="width:100%;height:100%;border:0"></iframe>`;
  const n = S.studyPages || 1;
  return Array.from({length:n}, (_,i) =>
    `<img src="/api/assets/${res.id}/page/${i+1}" loading="lazy" alt="Página ${i+1}">`).join('');
}

/* --------------------------------------------------------------- repaso */
async function startStudy(opts){
  S.studyOpts  = opts;
  S.studyTopic = opts.topic_id || null;
  S.mode = opts.mode || 'normal';

  const qs = new URLSearchParams();
  if (opts.topic_id)  qs.set('topic_id', opts.topic_id);
  if (opts.folder_id) qs.set('folder_id', opts.folder_id);
  if (S.mode !== 'normal') qs.set('mode', S.mode);
  const d = await api('/api/queue' + (qs.toString() ? '?' + qs : ''));
  S.queue = d.cards; S.stats = d.stats; S.scope = d.scope; paintStats();

  const name = opts.name || 'Todos los temas';
  const libre = S.mode === 'libre';
  $('#ttl').textContent = libre ? 'Repaso libre' : 'Repaso';
  $('#sub').textContent = libre
    ? `${name} · no altera la programación`
    : `${name} · ${d.scope.total} fichas en total`;
  $('#topActions').innerHTML = libre
    ? `<button class="btn ghost" id="toNormal">Volver al repaso normal</button>`
    : `<button class="btn" id="toFree" title="No altera la programación">🔁 Repaso libre</button>`;
  const swap = m => go('repaso', {...S.studyOpts, mode:m});
  if (libre) $('#toNormal').onclick = () => swap('normal');
  else       $('#toFree').onclick   = () => swap('libre');
  $('#sTopic').textContent = libre ? name + ' · libre' : name;

  if (opts.topic_id){
    const t = await api('/api/topics/' + opts.topic_id);
    S.studyPages = t.pages;
    setRail('repaso');
  } else setRail(null);

  nextCard();
}

function nextCard(){
  S.card = S.queue.shift() || null; S.revealed = false;
  $('#sLeft').textContent = S.card ? `${S.queue.length + 1} en la cola` : '';
  renderCard();
}

function renderCard(){
  if (!S.card){
    const n = (S.scope && S.scope.total) || 0;
    $('#qcard').innerHTML = S.mode === 'libre'
      ? `<div class="front">Fin de la vuelta.<br>
          <span style="font-size:14.5px;color:var(--muted)">Nada de esto cambió
          tus fechas de repaso.</span></div>`
      : `<div class="front">Por hoy has terminado.<br>
          <span style="font-size:14.5px;color:var(--muted)">Puedes volver a pasar
          las fichas cuantas veces quieras: el repaso libre no adelanta ninguna
          fecha ni gasta el cupo de mañana.</span></div>`;
    $('#sControls').innerHTML = `<div class="grades" style="grid-template-columns:1fr 1fr">
      <button class="g3" id="again">🔁 Repasar de nuevo${n ? ` (${n})` : ''}</button>
      <button id="toTopics">Volver a los temas</button></div>`;
    $('#again').onclick = () => go('repaso', {...S.studyOpts, mode:'libre'});
    $('#toTopics').onclick = () => go('inicio');
    return;
  }
  $('#qcard').innerHTML = S.revealed
    ? `<div class="front">${S.card.front}</div><hr><div>${S.card.back}</div>`
    : `<div class="front">${S.card.front}</div>`;
  if (S.revealed){
    const iv = S.card.intervals || {};
    const fmt = d => d===0 ? 'hoy' : d<30 ? d+' d' : d<365 ? Math.round(d/30)+' mes'
                    : (d/365).toFixed(1)+' a';
    // En repaso libre no se muestran intervalos: no se programa nada.
    const b = (g,l,d) => `<button class="g${g}" data-g="${g}"><span>${l}</span>
      ${S.mode === 'libre' ? '' : `<span class="iv">${fmt(d)}</span>`}</button>`;
    $('#sControls').innerHTML = `<div class="grades">${b(1,'Otra vez',iv.again)}
      ${b(2,'Difícil',iv.hard)}${b(3,'Bien',iv.good)}${b(4,'Fácil',iv.easy)}</div>`;
    $$('.grades button').forEach(x => x.onclick = () => grade(+x.dataset.g));
  } else {
    $('#sControls').innerHTML =
      `<button class="btn solid" style="width:100%;margin-top:15px;padding:13px"
        id="rev">Mostrar respuesta</button>`;
    $('#rev').onclick = () => { S.revealed = true; renderCard(); };
  }
}

async function grade(rating){
  if (!S.card) return;
  const card = S.card;
  const r = await post(`/api/cards/${card.id}/grade`, {rating, mode: S.mode});
  S.stats = r.stats; paintStats();
  // 'Otra vez' vuelve al final de la cola: se repasa otra vez en esta sesión.
  if (rating === 1) S.queue.push(Object.assign({}, card, r.result || {}));
  nextCard();
}

document.addEventListener('keydown', e => {
  if (S.sec !== 'repaso' || ['INPUT','TEXTAREA'].includes(e.target.tagName)) return;
  if ((e.key === ' ' || e.key === 'Enter') && !S.revealed && S.card){
    e.preventDefault(); S.revealed = true; renderCard();
  } else if (S.revealed && ['1','2','3','4'].includes(e.key)) grade(+e.key);
});

/* -------------------------------------------------------------- cargar */
$('#uploadBtn').onclick = () => $('#fileInput').click();
$('#fileInput').onchange = async e => {
  const file = e.target.files[0]; if (!file) return;
  e.target.value = '';
  const esPdf = file.name.toLowerCase().endsWith('.pdf');
  let url = '/api/upload';
  if (esPdf){
    const destino = S.topic || await uiElegir('El PDF se guarda como resumen del tema que elijas.',
      S.topics.map(t => ({value: t.id, label: t.title})), {title:'¿De qué tema es este PDF?', ok:'Cargar'});
    if (!destino) return;
    url += '?topic_id=' + parseInt(destino, 10);
  }
  const fd = new FormData(); fd.append('file', file);
  toast('Cargando…');
  try {
    const r = await api(url, {method:'POST', body: fd});
    await boot();
    toast(r.fichas_nuevas !== undefined
      ? `${r.titulo}: ${r.fichas_nuevas} fichas nuevas, ${r.fichas_actualizadas} actualizadas`
      : `${r.archivo} cargado`);
  } catch(err){ toast(err.message); }
};

/* -------------------------------------------------------------- ajustes */
async function rescan(){
  toast('Escaneando…');
  try {
    const r = await post('/api/ingest');
    await boot();
    toast(`${r.temas} temas · ${r.tarjetas_nuevas} fichas nuevas · ${r.materiales} materiales`);
  } catch(e){ toast(e.message); }
}

function renderAjustes(){
  S.topic = null; paintTree(); setRail(null);
  const s = S.settings;
  $('#ttl').textContent = 'Ajustes';
  $('#sub').textContent = 'Tu ritmo de estudio y tu cuenta';
  $('#topActions').innerHTML = '';
  $('#v-ajustes').innerHTML = `
    <div class="panel">
      <h3>Ritmo diario</h3>
      <p>Es tuyo: no cambia el de nadie más. El límite de fichas nuevas es el freno
         importante: si entran más de las que se pueden repasar, el atraso crece solo.</p>
      <div class="field"><label>Fichas nuevas al día</label>
        <input type="number" id="s-new" min="0" max="200" value="${s.new_limit}"></div>
      <div class="field"><label>Repasos máximos al día</label>
        <input type="number" id="s-rev" min="0" max="2000" value="${s.review_limit}"></div>
      <div class="field"><label>Retención objetivo</label>
        <input type="number" id="s-ret" min="0.70" max="0.97" step="0.01" value="${s.retention}"></div>
      <button class="btn solid" id="saveS">Guardar</button>
    </div>
    <div class="panel">
      <h3>Tu cuenta</h3>
      <p>Entras como <b>${esc(S.user.username)}</b>${S.user.email ? ' · ' + esc(S.user.email) : ''}.
         Al cambiar la contraseña se cierran tus otras sesiones.</p>
      <div class="field"><label for="s-cur">Contraseña actual</label>
        <input type="password" id="s-cur" autocomplete="current-password" maxlength="256"></div>
      <div class="field"><label for="s-pw">Contraseña nueva</label>
        <input type="password" id="s-pw" autocomplete="new-password" minlength="10" maxlength="256"></div>
      <button class="btn" id="savePw">Cambiar contraseña</button>
    </div>
    ${S.admin ? `<div class="panel">
      <h3>Material</h3>
      <p>Se vuelve a escanear la carpeta de material del servidor. Solo los
         administradores ven esto.</p>
      <button class="btn" id="reS">Re-escanear</button>
    </div>` : ''}`;
  $('#saveS').onclick = async () => {
    try {
      S.settings = await post('/api/settings', {new_limit:+$('#s-new').value,
        review_limit:+$('#s-rev').value, retention:+$('#s-ret').value});
      toast('Guardado');
    } catch(e){ toast(e.message); }
  };
  $('#savePw').onclick = async () => {
    try {
      const r = await post('/api/auth/password', {current: $('#s-cur').value, new: $('#s-pw').value});
      $('#s-cur').value = ''; $('#s-pw').value = '';
      toast(r.mensaje);
    } catch(e){ toast(e.message); }
  };
  if (S.admin) $('#reS').onclick = async () => { await rescan(); go('ajustes'); };
}

boot();
