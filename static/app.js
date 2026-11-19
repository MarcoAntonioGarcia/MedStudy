// app.js — la interfaz de estudio: el hospital. Vive en un archivo aparte
// para que la política de contenido pueda prohibir los scripts en línea: el
// HTML de las fichas viene de archivos de Anki y no debe poder ejecutar nada.
//
// Recorrido, solo con clics: hospital (un piso por carpeta) → planta del piso
// (un consultorio por tema) → consultorio en 3D (escritorio con el resumen, el
// expediente y el librero; el pizarrón en la pared izquierda, donde se repasan
// las fichas). "Repasar hoy" abre la sala de repaso sin recorrer nada.
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
// Nada se arma como cadena de HTML: todo texto que viene de la base se pinta con
// textContent. La única excepción es el HTML de las fichas (front/back), que el
// servidor sanea al importar (core.limpiar_html).

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
  clearTimeout(tt); tt=setTimeout(()=>t.classList.remove('on'),3200); }
// Un fallo en algo que corre solo (una animación, un clic) nunca se queda en
// silencio: se le dice a la persona.
const avisar = e => toast((e && e.message) || 'Algo salió mal. Intenta de nuevo.');

const S = {user:{}, folders:[], topics:[], stats:{}, settings:{}, pdf_ok:true,
           FL:[], level:'hospital', floor:null, ctx:null, wall:'desk', busy:false,
           floorId:null, panel:null, topicData:null, topicP:null, railTab:'postit', placing:false,
           study:null, studySeq:0, queue:[], card:null, revealed:false, mode:'normal',
           grading:false, dirty:false};

/* ------------------------------------------------------------- utilidades */
const NS = 'http://www.w3.org/2000/svg';
function E(tag, at, p){ const e=document.createElementNS(NS,tag);
  for (const k in at) e.setAttribute(k, at[k]); if (p) p.appendChild(e); return e; }
// El texto siempre con textContent: los nombres vienen de los archivos.
function T(p, x, y, s, cls, anchor){ const t=E('text',{x, y, class:cls, 'text-anchor':anchor||'start'},p);
  t.textContent = String(s); return t; }
function H(tag, cls, p, txt){ const e=document.createElement(tag); if (cls) e.className=cls;
  if (txt != null) e.textContent = txt; if (p) p.appendChild(e); return e; }
function wrap(s, max){ const out=[]; let cur='';
  for (const w of String(s).split(/\s+/)){ if ((cur+' '+w).trim().length > max && cur){ out.push(cur); cur=w; }
    else cur=(cur+' '+w).trim(); }
  if (cur) out.push(cur); return out; }
function TL(p, x, y, lines, cls, lh, anchor){ lines.forEach((l,k) => T(p, x, y+k*lh, l, cls, anchor)); }
function cut(s, n){ s = String(s); return s.length > n ? s.slice(0, n-1) + '…' : s; }
function grad(defs, id, kind, at, stops){ const g=E(kind, Object.assign({id}, at), defs);
  stops.forEach(([o,c,a]) => E('stop',{offset:o,'stop-color':c,'stop-opacity':a},g)); }
function hash(s){ let h=2166136261; for (const c of String(s)){ h^=c.codePointAt(0); h=Math.imul(h,16777619); } return h>>>0; }
const reduce = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const wait = ms => new Promise(r => setTimeout(r, ms));
const frames = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
const viewsEl = $('#views');
function vsize(){ const r=viewsEl.getBoundingClientRect(); return {w:r.width, h:r.height}; }
function portrait(){ const {w,h}=vsize(); return w < 700 || w/h < .9; }
function hot(g, label, fn){
  g.classList.add('hot'); g.setAttribute('role','button'); g.setAttribute('tabindex','0');
  g.setAttribute('aria-label', label);
  const run = e => { e.stopPropagation(); Promise.resolve().then(fn).catch(avisar); };
  g.addEventListener('click', run);
  g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); run(e); } });
}
function say(s){ $('#live').textContent = s; }
// El texto de un HTML de ficha, para leerlo en voz alta. DOMParser no carga
// imágenes ni ejecuta nada.
const textoDe = html => new DOMParser().parseFromString(String(html || ''), 'text/html').body.textContent || '';

/* --------------------------------------------------- datos del hospital */
// Lo que de verdad toca hoy: las vencidas más lo que queda del tope diario de
// nuevas. Sumar TODAS las nuevas daría «1692 para hoy» con un tope de 20.
function nuevasQuedan(){ return Math.max(0, (S.stats.nuevas_limite || 20) - (S.stats.nuevas_hoy || 0)); }
function paraHoy(temas){
  return temas.reduce((a,t) => a + t.vencidas, 0)
       + Math.min(temas.reduce((a,t) => a + t.nuevas, 0), nuevasQuedan());
}
function estado(t){ return t.vencidas ? 'hoy' : t.nuevas ? 'pend' : 'aldia'; }
function estadoTexto(t){
  return t.vencidas ? `${t.vencidas} por repasar` : t.nuevas ? `${t.nuevas} nuevas` : 'al día';
}
// Conteos sin tope, con nombre claro: lo que vence y lo que falta por empezar.
// El tope diario de nuevas solo aplica a la cola de hoy («Repasar hoy · N»).
function conteos(temas){
  return {rep: temas.reduce((a,t) => a + t.vencidas, 0), nue: temas.reduce((a,t) => a + t.nuevas, 0)};
}
function conteosTexto(temas){ const c=conteos(temas); return `${c.rep} por repasar · ${c.nue} nuevas`; }
// Un piso por carpeta; los temas sin carpeta van a un piso aparte.
function floors(){
  const fl = S.folders.map(f => ({id:f.id, name:f.name, temas:S.topics.filter(t => t.folder_id === f.id)}));
  const ids = new Set(S.folders.map(f => f.id));
  const sin = S.topics.filter(t => !t.folder_id || !ids.has(t.folder_id));
  if (sin.length) fl.push({id:0, name:'Sin carpeta', temas:sin, virtual:true});
  fl.forEach((f,i) => {
    f.num = i + 1;
    f.temas.forEach((t,j) => { t.num = f.num + '-' + String(j+1).padStart(2,'0'); });
    f.hoy = paraHoy(f.temas);
  });
  return fl;
}
const topicById = id => S.topics.find(t => t.id === id) || null;
function ctxTopic(){ return S.ctx && S.ctx.kind === 'tema' ? topicById(S.ctx.id) : null; }

/* --------------------------------------------- cámara 2D (anima el viewBox) */
function cam(svg){ return {svg, vb:null, anim:0, done:null}; }
function fitBox(c, b){ const r=c.svg.getBoundingClientRect(); const a=(r.width||1)/(r.height||1);
  let [x,y,w,h]=b;
  if (w/h < a){ const nw=h*a; x-=(nw-w)/2; w=nw; } else { const nh=w/a; y-=(nh-h)/2; h=nh; }
  return [x,y,w,h]; }
function camera(c, box, ms){
  const to=fitBox(c,box), from=c.vb||to; cancelAnimationFrame(c.anim);
  // una animación cortada por otra también termina: quien la esperaba sigue
  if (c.done){ const d=c.done; c.done=null; d(); }
  const set = v => { c.vb=v; c.svg.setAttribute('viewBox', v.map(n => n.toFixed(2)).join(' ')); };
  if (!ms || reduce()){ set(to); c.svg.style.filter=''; return Promise.resolve(); }
  return new Promise(res => { const t0=performance.now(); c.done=res;
    const ease = t => t<.5 ? 4*t*t*t : 1-Math.pow(-2*t+2,3)/2;
    const step = now => { const t=Math.min(1,(now-t0)/ms), e=ease(t);
      set(from.map((v,i) => v+(to[i]-v)*e));
      // desenfoque de movimiento: máximo a mitad del viaje
      const b=2.8*Math.sin(Math.PI*t); c.svg.style.filter = b>.2 ? `blur(${b.toFixed(2)}px)` : '';
      if (t<1) c.anim=requestAnimationFrame(step); else { c.svg.style.filter=''; c.done=null; res(); } };
    c.anim=requestAnimationFrame(step); });
}

let HC=null, PC=null, HG=null, PG=null, R3=null;
function show(name, arrive){
  for (const [id,n] of [['#vHospital','hospital'],['#vPlanta','planta'],['#vConsultorio','consultorio']]){
    const v=$(id), on=n===name; v.classList.toggle('on',on); v.inert=!on;
    v.setAttribute('aria-hidden', on ? 'false' : 'true');
    v.classList.toggle('arrive', on && !!arrive && !reduce());
  }
}

/* ------------------------------------------- 1. el hospital, en corte */
function buildHospital(){
  const FL=S.FL, P=portrait();
  const G = P ? {PAD:30,BW:560,FH:224,ROOF:150,LH:360,GR:70} : {PAD:90,BW:900,FH:196,ROOF:170,LH:236,GR:90};
  const nF=FL.length; G.lobbyY=G.ROOF+nF*G.FH; G.groundY=G.lobbyY+G.LH; G.W=G.BW+2*G.PAD; G.H=G.groundY+G.GR; G.P=P;
  const host=$('#vHospital'); host.textContent='';
  const svg=E('svg',{preserveAspectRatio:'xMidYMid meet',role:'group',
    'aria-label':`Hospital MedStudy: ${nF} pisos de rotación y la recepción`},host);
  const defs=E('defs',{},svg);
  grad(defs,'gDoor','linearGradient',{x1:0,y1:0,x2:0,y2:1},[[0,'#66d9ea',.4],[1,'#66d9ea',.04]]);
  grad(defs,'gBeam','linearGradient',{x1:0,y1:1,x2:0,y2:0},[[0,'#aeeef7',.32],[.6,'#66d9ea',.07],[1,'#66d9ea',0]]);
  grad(defs,'gHalo','radialGradient',{cx:.5,cy:.5,r:.5},[[0,'#66d9ea',.35],[1,'#66d9ea',0]]);
  const fb=E('filter',{id:'fFar',x:'-10%',y:'-10%',width:'120%',height:'120%'},defs); E('feGaussianBlur',{stdDeviation:3},fb);
  const fs=E('filter',{id:'fSoft',x:'-50%',y:'-50%',width:'200%',height:'200%'},defs); E('feGaussianBlur',{stdDeviation:14},fs);
  // la ciudad al fondo, fuera de foco (profundidad de campo)
  const sky=E('g',{'aria-hidden':'true',filter:'url(#fFar)'},svg);
  let x=-1700, k=0;
  while (x < G.W+1700){ const h=hash('b'+k); const w=90+h%120, hh=140+(h>>>5)%300;
    if (x+w < G.PAD-40 || x > G.PAD+G.BW+40){ E('rect',{x,y:G.groundY-hh,width:w,height:hh,class:'sky-b'},sky);
      for (let r=0;r<Math.floor(hh/46);r++) for (let c=0;c<Math.floor(w/34);c++) if (hash(k+':'+r+':'+c)%5===0)
        E('rect',{x:x+12+c*34,y:G.groundY-hh+16+r*46,width:12,height:18,class:'sky-w'},sky); }
    x+=w+18+(h>>>9)%40; k++; }
  // haces de luz desde el suelo, detrás del edificio
  const beams=E('g',{'aria-hidden':'true',filter:'url(#fSoft)'},svg);
  for (const [bx,dir] of [[G.PAD-40,-1],[G.PAD+G.BW+40,1]])
    E('polygon',{points:`${bx-14},${G.groundY} ${bx+14},${G.groundY} ${bx+dir*520+170},-900 ${bx+dir*520-170},-900`,fill:'url(#gBeam)'},beams);
  E('ellipse',{cx:G.W/2,cy:G.ROOF-40,rx:300,ry:120,fill:'url(#gHalo)','aria-hidden':'true'},svg);
  E('ellipse',{cx:G.W/2,cy:G.groundY+16,rx:G.BW*.6,ry:20,class:'shadow'},svg);
  E('line',{x1:-2200,y1:G.groundY,x2:G.W+2200,y2:G.groundY,class:'ground'},svg);
  E('rect',{x:G.PAD,y:G.ROOF,width:G.BW,height:nF*G.FH+G.LH,class:'h-body'},svg);
  const pw=P?360:440, px=G.PAD+(G.BW-pw)/2;
  E('rect',{x:px,y:G.ROOF-70,width:pw,height:70,class:'h-pent'},svg);
  T(svg,G.PAD+G.BW/2,G.ROOF-26,'MEDSTUDY','h-sign','middle');
  E('path',{d:`M${G.PAD-12} ${G.ROOF}H${G.PAD+G.BW+12}`,class:'h-roof'},svg);
  const ax=G.PAD+G.BW-60; E('line',{x1:ax,y1:G.ROOF,x2:ax,y2:G.ROOF-118,class:'h-ant'},svg);
  E('circle',{cx:ax,cy:G.ROOF-124,r:6,class:'h-beacon'},svg);
  G.floors=[];
  FL.forEach((f,i) => {
    const y=G.ROOF+(nF-1-i)*G.FH; G.floors[i]={y};
    const g=E('g',{'data-ix':i},svg);
    const ct=conteos(f.temas);
    hot(g,`Piso ${f.num}, ${f.name}: ${f.temas.length} consultorios, ${ct.rep} fichas por repasar y ${ct.nue} nuevas`,() => goFloor(i));
    const n=f.temas.length, per=Math.max(1,Math.ceil(n/2)); let wx,wy,ww,wh;
    // conteos sin tope: lo que vence (cian) y lo que falta por empezar (ámbar)
    const cuenta=(s) => { const c=E('tspan',{class:'c'},s); c.textContent=`${ct.rep} por repasar`;
      s.append(' · '); const a=E('tspan',{class:'a'},s); a.textContent=`${ct.nue} nuevas`; };
    const sub=(sy,sx,dos) => {
      if (!dos){ const s=T(g,sx,sy,`${n} consultorios · `,'p-sub'); cuenta(s); return; }
      T(g,sx,sy,`${n} consultorios`,'p-sub'); cuenta(T(g,sx,sy+24,'','p-sub p-sub2')); };
    if (P){ T(g,G.PAD+26,y+48,'P'+f.num,'p-num'); T(g,G.PAD+82,y+52,cut(f.name,20),'p-name'); sub(y+88,G.PAD+26,false);
      wx=G.PAD+26; wy=y+110; ww=G.BW-52; wh=G.FH-132; }
    else { T(g,G.PAD+30,y+50,'PISO '+f.num,'p-num'); const nl=wrap(f.name,13).slice(0,2); TL(g,G.PAD+30,y+96,nl,'p-name',42);
      sub(y+(nl.length>1?160:132),G.PAD+30,true); wx=G.PAD+330; wy=y+30; ww=G.BW-360; wh=G.FH-60; }
    if (!n) T(g,wx+ww/2,wy+wh/2+6,'sin consultorios todavía','p-empty','middle');
    // una ventana por consultorio, en dos filas como los dos lados del pasillo
    const cols=Math.max(per,4), gap=P?8:10, cw=(ww-gap*(cols-1))/cols, ch=(wh-gap)/2, off=(cols-per)*(cw+gap)/2;
    f.temas.forEach((t,j) => { const r=j<per?0:1, c=j<per?j:j-per;
      const wxx=wx+off+c*(cw+gap), wyy=wy+r*(ch+gap), st=estado(t);
      E('rect',{x:wxx,y:wyy,width:cw,height:ch,rx:2,
        class:'win'+(st==='hoy'?' hoy':st==='pend'?' pend':'')+(t.is_new||t.is_updated?' nuevo':'')},g);
      E('line',{x1:wxx+cw/2,y1:wyy,x2:wxx+cw/2,y2:wyy+ch,class:'mull'},g); });
    E('rect',{x:G.PAD+4,y:y+4,width:G.BW-8,height:G.FH-8,rx:6,class:'hl'},g);
    E('line',{x1:G.PAD,y1:y+G.FH,x2:G.PAD+G.BW,y2:y+G.FH,class:'slab'},svg);
  });
  // planta baja: recepción, la entrada y el tablero de ingresos
  const ly=G.lobbyY;
  T(svg,G.PAD+(P?26:30),ly+(P?42:46),'PB · RECEPCIÓN','lob-lbl');
  const recientes=recientesLista();
  const bw=P?G.BW-52:370, bh=P?150:152, bx=G.PAD+(P?26:30), by=ly+(P?60:66);
  const bg=E('g',{'data-board':'1'},svg);
  hot(bg,`Tablero de ingresos recientes: ${recientes.length} temas nuevos o actualizados`,openIngresos);
  E('rect',{x:bx,y:by,width:bw,height:bh,rx:8,class:'board-s'},bg);
  T(bg,bx+18,by+30,'INGRESOS RECIENTES','board-t');
  if (!recientes.length) T(bg,bx+18,by+64,'Nada nuevo por ahora','board-n');
  recientes.slice(0,4).forEach(({t},k) => { const yy=by+60+k*(P?22:26);
    E('circle',{cx:bx+24,cy:yy-5,r:4,class:'board-dot'+(t.is_new?'':' act')},bg);
    T(bg,bx+38,yy,cut(t.title,P?30:24),'board-i');
    if (!P) T(bg,bx+bw-18,yy,t.num,'board-n','end'); });
  E('rect',{x:bx-4,y:by-4,width:bw+8,height:bh+8,rx:10,class:'hl'},bg);
  const dw=P?190:230, dx=P?G.PAD+(G.BW-dw)/2:G.PAD+G.BW-dw-230, dh=P?96:150, dy=G.groundY-dh;
  E('rect',{x:dx-26,y:dy-24,width:dw+52,height:18,rx:3,class:'canopy'},svg);
  E('rect',{x:dx,y:dy,width:dw/2-3,height:dh,class:'door'},svg); E('rect',{x:dx+dw/2+3,y:dy,width:dw/2-3,height:dh,class:'door'},svg);
  E('rect',{x:dx,y:dy,width:dw,height:dh,fill:'url(#gDoor)'},svg);
  T(svg,dx+dw/2,dy-34,'ENTRADA','door-t','middle');
  if (!P){ const qx=G.PAD+G.BW-200, qy=ly+66; E('rect',{x:qx,y:qy,width:170,height:96,rx:6,class:'plaque'},svg);
    T(svg,qx+16,qy+28,'EN ESTE HOSPITAL','plaque-t'); T(svg,qx+16,qy+60,`${S.topics.length} temas`,'plaque-b');
    T(svg,qx+16,qy+84,`${S.folders.length} rotaciones`,'plaque-t'); }
  const ti=H('div','title',host); H('div','k',ti,'Medicina interna · residencia');
  const h1=H('h1','',ti); h1.append('Elige tu', document.createElement('br'), 'rotación.');
  H('p','',ti, S.topics.length
    ? 'Cada piso es una rotación. Cada ventana encendida, un consultorio con fichas para hoy.'
    : 'Todavía no hay material en el hospital.' + (S.admin ? ' Agrégalo en contenido/ o re-escanea desde el menú.' : ''));
  HG=G; HC=cam(svg);
}
const floorBox = i => [HG.PAD-14, HG.floors[i].y-14, HG.BW+28, HG.FH+28];
const hospitalBox = () => HG.P ? [0,-200,HG.W,HG.H+200] : [0,40,HG.W,HG.H+30];
function recientesLista(){
  const out=[]; S.FL.forEach((f,i) => f.temas.forEach((t,j) => { if (t.is_new || t.is_updated) out.push({i,j,t,f}); }));
  return out;
}

/* ---------------------------- 2. planta del piso: un consultorio por tema */
function buildPlan(i){
  const f=S.FL[i], P=portrait(), n=f.temas.length, per=Math.max(1,Math.ceil(n/2));
  const host=$('#vPlanta'); host.textContent='';
  const svg=E('svg',{preserveAspectRatio:'xMidYMid meet',role:'group',
    'aria-label':`Piso ${f.num}, ${f.name}: pasillo con ${n} consultorios`},host);
  const defs=E('defs',{},svg);
  grad(defs,'gPool','radialGradient',{cx:.5,cy:.5,r:.5},[[0,'#9be9f3',.22],[1,'#66d9ea',0]]);
  grad(defs,'gRoom','radialGradient',{cx:.5,cy:.15,r:.9},[[0,'#173049',1],[1,'#0d162b',1]]);
  const G={P,rooms:[]}; const M=40; let compact=false;
  if (!P){
    const RW=172, RH=206, CO=84, CORE=180;
    G.W=2*M+CORE+per*RW; G.H=2*M+2*RH+CO;
    E('rect',{x:M,y:M,width:G.W-2*M,height:G.H-2*M,class:'pl-foot'},svg);
    E('rect',{x:M+CORE,y:M+RH,width:per*RW,height:CO,class:'pl-corr'},svg);
    for (let c=0;c<per;c++) E('ellipse',{cx:M+CORE+c*RW+RW/2,cy:M+RH+CO/2,rx:RW*.55,ry:CO*.75,fill:'url(#gPool)'},svg);
    E('line',{x1:M+60,y1:M+RH+CO/2,x2:G.W-M-20,y2:M+RH+CO/2,class:'pl-line'},svg);
    E('rect',{x:M+20,y:M+20,width:CORE-40,height:RH-40,rx:4,class:'pl-core'},svg);
    T(svg,M+CORE/2,M+RH/2+5,'ELEVADOR','pl-core-t','middle');
    const sy=M+RH+CO+20; E('rect',{x:M+20,y:sy,width:CORE-40,height:RH-40,rx:4,class:'pl-core'},svg);
    for (let k=1;k<8;k++) E('line',{x1:M+20,y1:sy+k*(RH-40)/8,x2:M+CORE-20,y2:sy+k*(RH-40)/8,class:'pl-stair'},svg);
    T(svg,M+CORE/2,sy+(RH-40)/2+5,'ESCALERA','pl-core-t','middle');
    E('circle',{cx:M+36,cy:M+RH+CO/2,r:9,class:'pl-you'},svg); T(svg,M+54,M+RH+CO/2+5,'ESTÁS AQUÍ','pl-you-t');
    f.temas.forEach((t,j) => { const top=j<per, c=top?j:j-per;
      G.rooms[j]={x:M+CORE+c*RW,y:top?M:M+RH+CO,w:RW,h:RH,door:top?'b':'t'}; });
  } else {
    // en el teléfono todo el pasillo cabe en la pantalla: se navega solo con clics
    const RW=170, CO=76, CORE=96; G.W=2*M+2*RW+CO;
    const {w,h}=vsize(), avail=h*(G.W/Math.max(w,1));
    const RH=Math.max(64,Math.min(150,Math.floor((avail-2*M-CORE)/per))); compact=RH<110;
    G.H=2*M+CORE+per*RH;
    E('rect',{x:M,y:M,width:G.W-2*M,height:G.H-2*M,class:'pl-foot'},svg);
    E('rect',{x:M+RW,y:M+CORE,width:CO,height:per*RH,class:'pl-corr'},svg);
    for (let r=0;r<per;r++) E('ellipse',{cx:M+RW+CO/2,cy:M+CORE+r*RH+RH/2,rx:CO*.8,ry:RH*.5,fill:'url(#gPool)'},svg);
    E('line',{x1:M+RW+CO/2,y1:M+60,x2:M+RW+CO/2,y2:G.H-M-14,class:'pl-line'},svg);
    E('rect',{x:M+14,y:M+14,width:RW-28,height:CORE-28,rx:4,class:'pl-core'},svg);
    T(svg,M+RW/2,M+CORE/2+5,'ELEVADOR','pl-core-t','middle');
    const sx=M+RW+CO+14; E('rect',{x:sx,y:M+14,width:RW-28,height:CORE-28,rx:4,class:'pl-core'},svg);
    for (let k=1;k<7;k++) E('line',{x1:sx+k*(RW-28)/7,y1:M+14,x2:sx+k*(RW-28)/7,y2:M+CORE-14,class:'pl-stair'},svg);
    T(svg,sx+(RW-28)/2,M+CORE/2+5,'ESCALERA','pl-core-t','middle');
    E('circle',{cx:M+RW+CO/2,cy:M+30,r:8,class:'pl-you'},svg); T(svg,M+RW+CO/2,M+56,'AQUÍ','pl-you-t','middle');
    f.temas.forEach((t,j) => { const left=j<per, r=left?j:j-per;
      G.rooms[j]={x:left?M:M+RW+CO,y:M+CORE+r*RH,w:RW,h:RH,door:left?'r':'l'}; });
  }
  if (!n) T(svg,G.W/2,G.H/2+(P?40:-60),'Este piso todavía no tiene consultorios.','pl-empty','middle');
  f.temas.forEach((t,j) => {
    const R=G.rooms[j], g=E('g',{'data-ix':j,'data-topic':t.id},svg), st=estado(t);
    const tag = t.is_new ? ', nuevo' : t.is_updated ? ', actualizado' : '';
    hot(g,`Consultorio ${t.num}, ${t.title}: ${estadoTexto(t)}${tag}`,() => goRoom(j));
    E('rect',{x:R.x,y:R.y,width:R.w,height:R.h,class:'rm'+(st==='hoy'?' lit':st==='pend'?' lit-n':'')},g);
    const dW=P?Math.min(30,R.h/3):44; let d,arc;
    if (R.door==='b'){ const dx=R.x+R.w-24-dW; d=`M${R.x} ${R.y+R.h}H${dx}M${dx+dW} ${R.y+R.h}H${R.x+R.w}`; arc=`M${dx} ${R.y+R.h}A${dW} ${dW} 0 0 1 ${dx+dW} ${R.y+R.h-dW}`; }
    else if (R.door==='t'){ const dx=R.x+R.w-24-dW; d=`M${R.x} ${R.y}H${dx}M${dx+dW} ${R.y}H${R.x+R.w}`; arc=`M${dx} ${R.y}A${dW} ${dW} 0 0 0 ${dx+dW} ${R.y+dW}`; }
    else if (R.door==='r'){ const dy=R.y+R.h-10-dW; d=`M${R.x+R.w} ${R.y}V${dy}M${R.x+R.w} ${dy+dW}V${R.y+R.h}`; arc=`M${R.x+R.w} ${dy}A${dW} ${dW} 0 0 1 ${R.x+R.w+dW} ${dy+dW}`; }
    else { const dy=R.y+R.h-10-dW; d=`M${R.x} ${R.y}V${dy}M${R.x} ${dy+dW}V${R.y+R.h}`; arc=`M${R.x} ${dy}A${dW} ${dW} 0 0 0 ${R.x-dW} ${dy+dW}`; }
    E('rect',{x:R.x,y:R.y,width:R.w,height:R.h,class:'rm-wall',style:'stroke-opacity:.35'},g);
    E('path',{d,class:'rm-wall'},g); E('path',{d:arc,class:'rm-door'},g);
    if (st!=='aldia') E('circle',{cx:R.x+R.w-18,cy:R.y+(compact?18:27),r:5,class:'rm-lamp'+(st==='pend'?' n':'')},g);
    if (compact){
      if (t.is_new || t.is_updated) E('rect',{x:R.x+3,y:R.y+6,width:5,height:R.h-12,rx:2,class:'rm-bar'},g);
      // 14 caracteres: la lámpara de estado ocupa la esquina derecha
      const ln=wrap(t.title,14); const lines=ln.length>2?[ln[0],cut(ln[1],14)]:ln;
      TL(g,R.x+16,R.y+(lines.length>1?R.h/2-4:R.h/2+6),lines,'rm-name',19);
    } else {
      const tx=R.x+16, ty=R.y+32; T(g,tx,ty,t.num,'rm-num');
      const ln=wrap(t.title,15); const lines=ln.length>3?[ln[0],ln[1],cut(ln[2],14)]:ln;
      TL(g,tx,ty+28,lines,'rm-name',21);
      const sy=ty+28+lines.length*21+6;
      T(g,tx,sy,estadoTexto(t),'rm-st '+st);
      if (t.is_new || t.is_updated){ const lbl=t.is_new?'NUEVO':'ACTUAL.';
        E('rect',{x:tx,y:sy+12,width:76,height:20,rx:4,class:'rm-new'+(t.is_new?'':' act')},g);
        T(g,tx+38,sy+26,lbl,'rm-new-t','middle'); }
    }
    E('rect',{x:R.x+5,y:R.y+5,width:R.w-10,height:R.h-10,rx:4,class:'hl'},g);
  });
  if (!P){ const ti=H('div','title',host);
    H('div','k',ti,`Piso ${f.num} · ${n} consultorios · ${conteosTexto(f.temas)}`); H('h1','',ti,f.name+'.'); }
  PG=G; PC=cam(svg);
}
function planFull(){ if (PG.P) return [0,0,PG.W,PG.H]; const w=Math.max(PG.W,1150); return [(PG.W-w)/2,-150,w,PG.H+210]; }
function roomBox(j){ const R=PG.rooms[j]; return R ? [R.x-10,R.y-10,R.w+20,R.h+20] : planFull(); }
function roomIndex(){ const t=ctxTopic(); if (!t || S.floor==null || !S.FL[S.floor]) return -1;
  return S.FL[S.floor].temas.findIndex(x => x.id === t.id); }

/* ---------------------------------------- 3. el consultorio, en 3D (CSS) */
/* Cuarto cuadrado: el ancho de la pantalla es el ancho y el fondo; la cámara
   está al centro (perspective = W/2) y gira sobre sí misma. Pared del fondo:
   escritorio con el resumen, expediente y librero. Pared izquierda: el
   pizarrón. Pared derecha: el negatoscopio. */
function face(sc, cls, w, h, tf){ const d=H('div','face '+cls,sc); d.style.width=w+'px'; d.style.height=h+'px';
  d.style.transform=`translate(-50%,-50%) ${tf}`; return d; }
function buildRoom(){
  const Pt=portrait();
  const host=$('#vConsultorio'); host.textContent='';
  const {w,h}=vsize(); const W=Math.round(w), Hh=Math.round(h), P=W/2;
  const box=H('div','room3d',host); box.style.perspective=P+'px';
  box.addEventListener('scroll',() => { box.scrollTop=0; box.scrollLeft=0; });
  const sc=H('div','scene',box); sc.style.transformOrigin=`50% 50% ${P}px`;
  face(sc,'w-floor',W,2*P,`translateY(${Hh/2}px) translateZ(${P}px) rotateX(90deg)`);
  face(sc,'w-ceil',W,2*P,`translateY(${-Hh/2}px) translateZ(${P}px) rotateX(-90deg)`);
  const back=face(sc,'wall w-back',W,Hh,'');
  const left=face(sc,'wall w-left',2*P,Hh,`translateX(${-W/2}px) translateZ(${P}px) rotateY(90deg)`);
  const right=face(sc,'wall w-right',2*P,Hh,`translateX(${W/2}px) translateZ(${P}px) rotateY(-90deg)`);
  face(sc,'w-beam',W*.6,Hh,`translateZ(${P*.35}px)`);
  back.setAttribute('role','group'); back.setAttribute('aria-label','Escritorio');
  left.setAttribute('role','group'); left.setAttribute('aria-label','Pizarrón');
  right.inert=true; right.setAttribute('aria-hidden','true');
  const t=ctxTopic();
  if (t) deskWall(back, t, Pt); else salaWall(back, Pt);
  boardWall(left); sideWall(right, Pt);
  R3={box,sc,back,left,W,H:Hh,P,Pt};
}
function roomCam(wall, ms, from){
  if (!R3) return Promise.resolve();
  const {sc,box,P,Pt,back,left}=R3;
  const ry = wall==='board' ? -90 : 0;
  const dz = wall==='board' ? -(Pt?.04:.1)*P : wall==='door' ? -.95*P
           : wall==='read' ? -(Pt?.02:.3)*P : -(Pt?.22:.56)*P;
  const anim = ms && !reduce();
  sc.style.transition = anim ? `transform ${ms}ms cubic-bezier(.65,0,.35,1)` : 'none';
  sc.style.transform = `translateZ(${dz.toFixed(1)}px) rotateY(${ry}deg)`;
  if (wall !== 'door'){
    const onBoard = wall === 'board';
    back.inert = onBoard; left.inert = !onBoard;
    back.classList.toggle('dof', onBoard); left.classList.toggle('dof', !onBoard);
  }
  if (anim && from && from !== wall && from !== 'door'){ box.classList.remove('blur'); void box.offsetWidth; box.classList.add('blur'); }
  return anim ? wait(ms) : Promise.resolve();
}
function deskLayout(P, Hh){
  return P ? {shelf:[262,236,140,170],desk:[16,Hh-90,388],lect:[30,Hh-228],box:[214,Hh-180],tx:24,ty:96,tw:14,ts:32}
           : {shelf:[790,96,170,300],desk:[90,Hh-92,620],lect:[150,Hh-222],box:[440,Hh-250],tx:60,ty:120,tw:20,ts:50};
}
function roomTitle(svg, L, P, kicker, name, sub){
  T(svg,L.tx,L.ty-(P?38:50),kicker,'r-kick');
  const all=wrap(String(name).toUpperCase(),L.tw), max=P?3:2;
  const tl=all.length>max ? all.slice(0,max-1).concat([cut(all.slice(max-1).join(' '),L.tw)]) : all;
  tl.forEach((l,k) => { const e=T(svg,L.tx,L.ty+k*(L.ts+8),l+(k===tl.length-1?'.':''),'r-title'); e.style.fontSize=L.ts+'px'; });
  T(svg,L.tx,L.ty+tl.length*(L.ts+8)+(P?2:6),sub,'r-rot');
}
function deskFurniture(svg, L, Hh){
  const dy=L.desk[1];
  E('rect',{x:L.desk[0],y:dy,width:L.desk[2],height:16,rx:3,class:'desk'},svg);
  E('line',{x1:L.desk[0]+24,y1:dy+16,x2:L.desk[0]+24,y2:Hh,class:'desk-leg'},svg);
  E('line',{x1:L.desk[0]+L.desk[2]-24,y1:dy+16,x2:L.desk[0]+L.desk[2]-24,y2:Hh,class:'desk-leg'},svg);
  return dy;
}
function expedienteSvg(svg, L, P, dy, total, hoy, label, fn){
  const [bx,by]=L.box, bw=P?170:220, bh=dy-by; const eg=E('g',{},svg);
  hot(eg,label,fn);
  const cards=Math.max(1,Math.min(9,Math.round(total/9))), lit=Math.min(cards,Math.ceil(cards*hoy/Math.max(1,total)*3));
  for (let k=0;k<cards;k++){ const cxk=bx+16+k*((bw-44)/Math.max(1,cards-1)), hk=bh*.62+(hash(k+label)%16);
    E('rect',{x:cxk,y:dy-hk,width:22,height:hk,rx:2,class:'idx'+(k<lit?' hoy':'')},eg);
    E('rect',{x:cxk+2+(k%3)*5,y:dy-hk-6,width:10,height:7,rx:1,class:'tab'},eg); }
  const bxh=Math.max(60,bh*.42);
  E('rect',{x:bx,y:dy-bxh,width:bw,height:bxh,rx:4,class:'box'},eg);
  T(eg,bx+bw/2,dy-bxh+26,`${total} FICHAS`,'o-lbl','middle');
  const hs=T(eg,bx+bw/2,dy-bxh+46,hoy?`${hoy} para hoy`:'al día','o-sub','middle'); if (hoy) hs.classList.add('c');
  T(eg,bx+bw/2,dy+44,'EXPEDIENTE','o-lbl','middle');
  E('rect',{x:bx-8,y:by-14,width:bw+16,height:bh+72,rx:8,class:'hl'},eg);
}
function deskWall(host, t, P){
  const f=S.FL[S.floor] || {name:''};
  const W=P?420:1000, Hh=P?670:500, L=deskLayout(P,Hh);
  const svg=E('svg',{viewBox:`0 0 ${W} ${Hh}`,preserveAspectRatio:'xMidYMax meet',role:'group',
    'aria-label':`Consultorio ${t.num}, ${t.title}`},host);
  roomTitle(svg,L,P,`CONSULTORIO ${t.num} · ${cut(String(f.name).toUpperCase(),P?14:28)}`,t.title,
    t.is_new ? 'Ingreso reciente' : t.is_updated ? 'Material actualizado' : 'Rotación de '+f.name);
  const dy=deskFurniture(svg,L,Hh);
  // el resumen, sobre el atril
  const resumen=(t.assets.resumen||[])[0];
  const [lx,ly]=L.lect, lw=P?160:200, lh=dy-ly; const rg=E('g',{},svg);
  hot(rg, resumen ? `Resumen de ${t.title}: abrirlo para leer y anotar` : `${t.title} no tiene resumen todavía`,
      () => resumen ? openLector('postit') : sinResumen(t));
  E('path',{d:`M${lx+10} ${dy}L${lx+lw/2} ${ly+lh*.55}L${lx+lw-10} ${dy}Z`,class:'lect'},rg);
  const shw=lw*.8, shh=lh*.86, shx=lx+(lw-shw)/2;
  E('rect',{x:shx,y:ly,width:shw,height:shh,rx:2,class:'paper'+(resumen?'':' none')},rg);
  if (resumen){
    E('rect',{x:shx,y:ly,width:shw,height:24,class:'paper-h'},rg);
    T(rg,shx+10,ly+16,cut(t.title.toUpperCase(),16),'paper-t');
    for (let k=0;k<6;k++){ const yy=ly+42+k*((shh-56)/6); E('line',{x1:shx+12,y1:yy,x2:shx+shw-12-(k%3)*14,y2:yy,class:'paper-l'},rg); }
  } else T(rg,shx+shw/2,ly+shh/2,'sin resumen','o-sub','middle');
  T(rg,lx+lw/2,dy+44,'RESUMEN','o-lbl','middle');
  E('rect',{x:lx-6,y:ly-8,width:lw+12,height:lh+66,rx:8,class:'hl'},rg);
  // el expediente: la caja de fichas
  const hoy=t.vencidas+Math.min(t.nuevas,nuevasQuedan());
  expedienteSvg(svg,L,P,dy,t.total,hoy,`Expediente: ${t.total} fichas, ${estadoTexto(t)}`,() => openExpediente(t));
  // el librero: los artículos
  const arts=t.assets.articulo||[];
  const [sx,sy,sw,sh]=L.shelf; const sg=E('g',{},svg);
  hot(sg, arts.length ? `Librero: ${arts.length} artículos` : 'Librero: aún no hay artículos', () => openLibrero(t));
  E('rect',{x:sx,y:sy,width:sw,height:sh,rx:4,class:'shelf'},sg);
  const nS=4; for (let k=1;k<nS;k++) E('line',{x1:sx,y1:sy+k*sh/nS,x2:sx+sw,y2:sy+k*sh/nS,class:'shelf-b'},sg);
  let restantes=arts.length;
  for (let k=0;k<nS;k++){ let x=sx+12; const base=sy+(k+1)*sh/nS-4; let q=0;
    while (x < sx+sw-24 && q < 6){ const w=14+hash(t.title+k+q)%10, hh=sh/nS-18-(hash(q+'h'+k)%14);
      const real=restantes>0; if (real) restantes--;
      E('rect',{x,y:base-hh,width:w,height:hh,rx:1,class:real?'book c'+(hash(k+':'+q)%3):'book-ghost'},sg); x+=w+5; q++; } }
  T(sg,sx+sw/2,sy+sh+28,'ARTÍCULOS','o-lbl','middle');
  const as=T(sg,sx+sw/2,sy+sh+48,arts.length?`${arts.length} en el librero`:'aún no hay','o-sub','middle');
  if (arts.length) as.classList.add('c');
  E('rect',{x:sx-10,y:sy-10,width:sw+20,height:sh+70,rx:8,class:'hl'},sg);
}
// La sala de repaso: para «Repasar hoy» y «Repasar el piso». Mezcla temas,
// así que no tiene resumen ni librero propios: solo el expediente.
function salaWall(host, P){
  const o=S.ctx.opts, W=P?420:1000, Hh=P?670:500, L=deskLayout(P,Hh);
  const svg=E('svg',{viewBox:`0 0 ${W} ${Hh}`,preserveAspectRatio:'xMidYMax meet',role:'group',
    'aria-label':`Sala de repaso: ${o.name}`},host);
  roomTitle(svg,L,P,'SALA DE REPASO',o.name,'Fichas de varios consultorios, en un solo pizarrón');
  const dy=deskFurniture(svg,L,Hh);
  const temas=o.folder_id ? S.topics.filter(t => t.folder_id === o.folder_id) : S.topics;
  const total=temas.reduce((a,t) => a+t.total,0), hoy=paraHoy(temas);
  expedienteSvg(svg,L,P,dy,total,hoy,`Expediente de la sala: ${total} fichas, ${hoy} para hoy. Ir al pizarrón`,() => toBoard());
}
function sideWall(host, P){
  const W=P?420:1000, Hh=P?670:500;
  const svg=E('svg',{viewBox:`0 0 ${W} ${Hh}`,preserveAspectRatio:'xMidYMax meet'},host);
  const defs=E('defs',{},svg); grad(defs,'gNeg','radialGradient',{cx:.5,cy:.45,r:.7},[[0,'#bff3fa',.6],[1,'#66d9ea',.08]]);
  const nw=P?300:380, nh=P?190:220, nx=(W-nw)/2, ny=P?200:90;
  E('rect',{x:nx,y:ny,width:nw,height:nh,rx:6,class:'neg'},svg);
  E('rect',{x:nx+10,y:ny+10,width:nw/2-15,height:nh-20,rx:2,class:'neg-p'},svg);
  E('rect',{x:nx+nw/2+5,y:ny+10,width:nw/2-15,height:nh-20,rx:2,class:'neg-p'},svg);
  const cx1=nx+nw/4+2, cy=ny+nh/2;
  for (let k=-2;k<=2;k++) E('path',{d:`M${cx1-34} ${cy+k*16}q34 -9 68 0`,class:'neg-l'},svg);
  E('line',{x1:cx1,y1:ny+18,x2:cx1,y2:ny+nh-18,class:'neg-l'},svg);
  E('ellipse',{cx:nx+3*nw/4-2,cy,rx:nw/8,ry:nh/3.2,class:'neg-l'},svg);
}

/* ------------------------------------------------ el pizarrón (repaso) */
function boardWall(host){
  const b=H('div','chalk',host); b.id='chalk';
  const top=H('div','top-r',b); H('span','',top,''); H('span','',top,'');
  const body=H('div','body',b); H('div','q',body); H('div','rule',body);
  const a=H('div','a',body); a.hidden=true;
  H('div','row',b);
  H('div','note',b,'');
  renderChalk(false);
}
const fmtIv = d => d===0 ? 'hoy' : d<30 ? d+' d' : d<365 ? Math.round(d/30)+' mes' : (d/365).toFixed(1)+' a';
function renderChalk(anim){
  const b=$('#chalk'); if (!b) return;
  const body=b.querySelector('.body'), row=b.querySelector('.row'), [tl,tr]=b.querySelectorAll('.top-r span');
  const q=body.querySelector('.q'), a=body.querySelector('.a'), note=b.querySelector('.note');
  if (anim && !reduce()){ body.classList.remove('wipe'); void body.offsetWidth; body.classList.add('wipe'); }
  const libre=S.mode==='libre';
  const pinta = () => {
    row.textContent=''; b.classList.remove('shown'); a.hidden=true; a.textContent='';
    note.textContent = libre ? 'Repaso libre: nada de esto cambia tus fechas ni gasta el cupo de mañana.'
                             : 'Espacio muestra la respuesta · 1 a 4 califica.';
    if (!S.study){                             // todavía no se pide la cola
      tl.textContent='Pizarrón'; tr.textContent=''; q.textContent='Las preguntas aparecen aquí.';
      const r=H('button','show',row,'Empezar preguntas'); r.type='button'; r.onclick=() => startStudy().catch(avisar);
      return;
    }
    if (S.study.loading){ tl.textContent='Pizarrón'; tr.textContent=''; q.textContent='Cargando las fichas…'; return; }
    if (!S.card){
      const n=(S.scope && S.scope.total) || 0;
      tl.textContent = libre ? 'Fin de la vuelta' : 'Repaso terminado'; tr.textContent=''; note.textContent='';
      q.textContent=''; q.append(libre ? 'Fin de la vuelta.' : 'Por hoy terminaste.');
      H('span','muted',q, libre ? 'Nada de esto cambió tus fechas de repaso.'
        : 'Puedes volver a pasar las fichas cuantas veces quieras: el repaso libre no adelanta ninguna fecha ni gasta el cupo de mañana.');
      if (n){ const r=H('button','show',row,`🔁 Repasar de nuevo (${n})`); r.type='button';
        r.onclick=() => startStudy({mode:'libre'}).catch(avisar); }
      const s=H('button','',row, S.ctx && S.ctx.kind==='sala' ? 'Salir de la sala' : 'Girar al escritorio'); s.type='button';
      s.onclick=() => (S.ctx && S.ctx.kind==='sala' ? leaveRoom() : toDesk()).catch(avisar);
      return;
    }
    const tema=topicById(S.card.topic_id);
    tl.textContent = tema ? `${tema.num ? tema.num + ' · ' : ''}${tema.title}` : (S.study.name || 'Repaso');
    tr.textContent = libre ? 'Libre' : `${S.queue.length + 1} en la cola`;
    q.innerHTML = S.card.front;            // HTML de Anki, saneado en el servidor
    if (S.revealed){
      a.innerHTML = S.card.back; a.hidden=false; void a.offsetWidth; b.classList.add('shown');
      const iv=S.card.intervals || {};
      [['Otra vez',iv.again],['Difícil',iv.hard],['Bien',iv.good],['Fácil',iv.easy]].forEach(([lab,d],k) => {
        const r=H('button','r'+(k+1),row); r.type='button'; r.dataset.g=k+1; r.append(lab);
        const sm=H('small','',r, libre ? '' : fmtIv(d)); H('span','key',sm,(libre?'':' · ')+(k+1));
        r.setAttribute('aria-label', libre ? lab : `${lab}, vuelve en ${fmtIv(d)}`);
        r.onclick=() => grade(k+1);
      });
    } else {
      const r=H('button','show',row); r.type='button'; r.append('Mostrar respuesta');
      H('small','key',r,'espacio'); r.onclick=reveal;
    }
  };
  if (anim && !reduce()) setTimeout(() => { pinta(); focusBoard(); }, 200); else pinta();
}
function focusBoard(){ const btn=$('#chalk .row button'); if (btn && S.wall==='board' && !S.panel) btn.focus({preventScroll:true}); }
function reveal(){
  if (S.revealed || !S.card) return;
  S.revealed=true; renderChalk(false); focusBoard();
  say('Respuesta: ' + textoDe(S.card.back));
}
// Pide la cola del consultorio (o de la sala) y la pinta en el pizarrón.
// Cada petición lleva un número: si mientras tanto salió otra (o se salió del
// cuarto), la respuesta vieja se descarta y no pisa la cola ni el modo.
async function startStudy(cambio){
  if (!S.ctx) return;
  const t=ctxTopic();
  const base = t ? {topic_id:t.id, name:t.title} : S.ctx.opts;
  const opts = Object.assign({}, base, cambio || {});
  const antes = {mode:S.mode, study:S.study, queue:S.queue, card:S.card, revealed:S.revealed};
  const seq = ++S.studySeq;
  S.mode = opts.mode || 'normal';
  S.study = {loading:true, name:opts.name, opts}; S.card=null; S.revealed=false;
  renderChalk(false);
  if (S.level==='consultorio' && !S.panel) ui();
  const qs=new URLSearchParams();
  if (opts.topic_id)  qs.set('topic_id', opts.topic_id);
  if (opts.folder_id) qs.set('folder_id', opts.folder_id);
  if (S.mode !== 'normal') qs.set('mode', S.mode);
  try {
    const d=await api('/api/queue' + (qs.toString() ? '?' + qs : ''));
    if (seq !== S.studySeq) return;
    S.queue=d.cards; S.stats=d.stats; S.scope=d.scope; paintStats();
    S.study.loading=false; nextCard(true); if (S.level==='consultorio' && !S.panel) ui();
  } catch(e){
    if (seq !== S.studySeq) return;
    Object.assign(S, antes);               // vuelve el modo y la cola que había
    renderChalk(false); if (S.level==='consultorio' && !S.panel) ui();
    throw e;
  }
}
function nextCard(anim){
  S.card=S.queue.shift() || null; S.revealed=false;
  renderChalk(anim);
  if (S.card) say('Pregunta: ' + textoDe(S.card.front));
  else say('Repaso terminado');
}
async function grade(rating){
  if (!S.card || !S.revealed || S.grading) return;
  S.grading=true; $$('#chalk .row button').forEach(b => { b.disabled=true; });
  const card=S.card;
  try {
    const r=await post(`/api/cards/${card.id}/grade`, {rating, mode:S.mode});
    S.stats=r.stats; paintStats();
    if (S.mode !== 'libre') S.dirty=true;
    // 'Otra vez' vuelve al final de la cola: se repasa otra vez en esta sesión.
    if (rating === 1) S.queue.push(Object.assign({}, card, r.result || {}));
    nextCard(true);
  } catch(e){
    $$('#chalk .row button').forEach(b => { b.disabled=false; });
    avisar(e);
  } finally {
    S.grading=false;
    // si ya se salió del cuarto mientras se guardaba, el hospital se pone al día
    if (S.dirty && S.level!=='consultorio' && !S.busy) refreshData().catch(avisar);
  }
}

/* ----------------------------------------- barra: migas, pista y botones */
function ui(){
  const c=$('#crumbs'); c.textContent='';
  const f = S.floor!=null ? S.FL[S.floor] : null, t=ctxTopic();
  const items=[['Hospital', () => toHospital()]];
  if (f && S.level!=='hospital') items.push([f.name, S.level==='consultorio' ? () => leaveRoom() : null]);
  if (S.level==='consultorio'){
    const nm = t ? t.title : 'Sala de repaso';
    items.push([nm, (S.wall==='board' || S.panel==='lector') ? () => (S.panel ? closePanel() : toDesk()) : null]);
    if (S.panel==='lector') items.push(['Resumen', null]);
    else if (S.wall==='board') items.push(['Preguntas', null]);
  }
  if (S.panel==='ajustes') items.push(['Ajustes', null]);
  items.forEach(([lab,fn],k) => {
    if (k){ const sp=H('span','sep',c,'›'); sp.setAttribute('aria-hidden','true'); }
    const actual = k===items.length-1;
    if (actual || !fn){
      const sp=H('span','crumb'+(actual?' cur':''),c,lab);
      if (actual) sp.setAttribute('aria-current','location');
    } else {
      const b=H('button','',c,lab); b.type='button';
      b.onclick=() => Promise.resolve().then(fn).catch(avisar);
    }
  });
  $('#back').classList.toggle('on', S.level!=='hospital' || !!S.panel);

  const h=$('#hint'); h.textContent='';
  const line=(b,rest) => { H('b','',h,b); h.append(rest); };
  const go=$('#go'), go2=$('#go2'); go.textContent=''; go2.textContent=''; go2.hidden=true; go2.disabled=false;
  const n = s => H('span','n',null,String(s));
  if (S.level==='hospital'){
    if (!S.topics.length){ line('Hospital vacío',' · todavía no hay temas.'); }
    else {
      line('Elige un piso',' · cada piso es una rotación y cada ventana un consultorio');
      const lg=H('div','legend',h); lg.setAttribute('aria-hidden','true');
      for (const [cls,txt] of [['hoy','por repasar'],['pend','nuevas por empezar'],['','al día'],['nuevo','tema nuevo']]){
        const s=H('span','',lg); H('i',cls,s); s.append(txt); }
    }
    go.append('Repasar hoy · ', n(paraHoy(S.topics)));
    go2.hidden=false; go2.append('🔁 Repaso libre');
  } else if (S.level==='planta'){
    line(f.name,' · toca un consultorio para entrar');
    if (f.virtual){ go.append('Repasar hoy · ', n(paraHoy(S.topics))); }
    else { go.append(`Repasar ${cut(f.name,22)} · `, n(f.hoy)); go2.hidden=false; go2.append('🔁 Repaso libre'); }
  } else if (S.wall==='desk'){
    if (t){ line(t.title,' · el resumen, el expediente o el librero'); go.append('Empezar preguntas → pizarrón');
      go2.hidden=false; go2.append('📄 Resumen'); }
    else { line('Sala de repaso',' · el pizarrón está a la izquierda'); go.append('Ir al pizarrón'); }
  } else {
    line('Pizarrón',' · Espacio muestra la respuesta, 1 a 4 califica');
    go.append('↺ Girar al escritorio');
    if (S.study){ go2.hidden=false; go2.disabled=!!S.study.loading;
      go2.append(S.mode==='libre' ? 'Volver al repaso normal' : '🔁 Repaso libre'); }
  }
}
$('#go').onclick = () => Promise.resolve().then(() => {
  if (S.level==='hospital') return openSala({name:'Todos los temas'});
  if (S.level==='planta'){ const f=S.FL[S.floor];
    return f.virtual ? openSala({name:'Todos los temas'}) : openSala({folder_id:f.id, name:f.name}); }
  return S.wall==='desk' ? toBoard() : toDesk();
}).catch(avisar);
$('#go2').onclick = () => Promise.resolve().then(() => {
  if (S.level==='hospital') return openSala({name:'Todos los temas'}, 'libre');
  if (S.level==='planta'){ const f=S.FL[S.floor]; return openSala({folder_id:f.id, name:f.name}, 'libre'); }
  if (S.wall==='desk') return openLector('postit');
  return startStudy({mode: S.mode==='libre' ? 'normal' : 'libre'});
}).catch(avisar);

/* --------------------------------------------------------- la tarjeta */
let lastFocus=null;
// acts: [{label, sub, fn}] o [{label, sub, href}] (un enlace que abre en otra pestaña)
function card(k, title, text, acts, note){
  lastFocus=document.activeElement;
  $('#cardK').textContent=k; $('#cardT').textContent=title; $('#cardP').textContent=text;
  const a=$('#cardA'); a.textContent='';
  for (const it of acts){
    let el;
    if (it.href){ el=H('a','',a); el.href=it.href; el.target='_blank'; el.rel='noopener noreferrer'; }
    else { el=H('button','',a); el.type='button';
      el.onclick=() => { closeCard(); Promise.resolve().then(it.fn).catch(avisar); }; }
    H('span','',el,it.label); if (it.sub) H('small','',el,it.sub);
  }
  const nt=$('#cardN'); nt.hidden=!note; nt.textContent=note || '';
  $('#card').hidden=false; (a.querySelector('button,a') || $('#cardX')).focus({preventScroll:true});
}
function closeCard(refocus){ if ($('#card').hidden) return; $('#card').hidden=true;
  if (refocus && lastFocus && lastFocus.isConnected) lastFocus.focus({preventScroll:true}); }
$('#cardX').onclick = () => closeCard(true);
viewsEl.addEventListener('click', e => { if (!$('#card').hidden && !$('#card').contains(e.target)) closeCard(); });

function openIngresos(){
  const rec=recientesLista();
  card('Ingresos recientes', rec.length ? 'Temas nuevos y actualizados' : 'Nada nuevo por ahora',
    rec.length ? 'Lo que llegó en las últimas semanas. Toca uno para ir directo a su consultorio.'
               : 'Cuando se agregue un tema o material nuevo, aparecerá aquí durante tres semanas.',
    rec.map(({i,j,t,f}) => ({label:t.title, sub:(t.is_new?'nuevo':'actualizado')+' · '+f.name, fn:() => jump(i,j)})));
}
function openExpediente(t){
  const acts=[];
  if (t.total){
    acts.push({label:'Empezar preguntas', sub:'repaso normal', fn:() => toBoard()});
    acts.push({label:'🔁 Repaso libre', sub:'no cambia fechas', fn:() => toBoard('libre')});
    acts.push({label:'Ver todas las fichas', sub:String(t.total), fn:() => openLector('fichas')});
  }
  acts.push({label:'Notas del tema', sub:'', fn:() => openLector('general')});
  card('Expediente', t.title,
    t.total ? `${t.total} fichas · ${t.vencidas} pendientes · ${t.nuevas} sin empezar.`
            : 'Este tema todavía no tiene fichas.', acts);
}
function openLibrero(t){
  const arts=t.assets.articulo||[];
  card('Librero', 'Artículos · ' + t.title,
    arts.length ? 'Se abren en otra pestaña.'
                : 'Aún no hay artículos para este tema. Cuando se agregue alguno a la carpeta Articulos de su rotación, aparecerá aquí.',
    arts.map(a => ({label:String(a.filename).replace(/\.pdf$/i,''), sub:'PDF',
                    href:'/api/assets/' + encodeURIComponent(a.id) + '/file'})));
}
function sinResumen(t){
  card('Resumen', t.title, 'Este tema todavía no tiene resumen en PDF.' + (S.admin
    ? (S.subidas ? ' Cárgalo desde Ajustes → Cargar material.'
                 : ' Agrégalo con git en contenido/ y aparece en el siguiente despliegue.') : ''),
    [{label:'Notas del tema', sub:'', fn:() => openLector('general')}]);
}

/* ----------------------------------------------- el recorrido (clics) */
async function goFloor(i, fast){
  if (S.busy || S.level!=='hospital') return; S.busy=true; closeCard();
  try {
    S.floor=i; S.floorId=S.FL[i].id; S.ctx=null;
    await camera(HC, floorBox(i), fast?650:1050);
    buildPlan(i); camera(PC, planFull(), 0);
    S.level='planta'; show('planta', true); ui();
    say(`Piso ${S.FL[i].num}, ${S.FL[i].name}. ${S.FL[i].temas.length} consultorios.`);
  } finally { S.busy=false; }
  if (!fast){ const r=PC.svg.querySelector('.hot'); if (r) r.focus({preventScroll:true}); }
}
// Si falla, se avisa una sola vez y se olvida la promesa: abrir el escritorio
// lo vuelve a intentar.
function loadTopic(id){
  S.topicData=null;
  const p = api('/api/topics/' + id).then(t => {
    if (S.ctx && S.ctx.kind==='tema' && S.ctx.id===id) S.topicData=t;
    return t;
  });
  S.topicP = p;
  p.catch(e => { if (S.topicP === p) S.topicP=null; avisar(e); });
  return p;
}
function resetStudy(){ S.studySeq++; S.study=null; S.queue=[]; S.card=null; S.revealed=false; S.mode='normal'; }
async function goRoom(j, fast){
  if (S.busy || S.level!=='planta') return; S.busy=true; closeCard();
  try {
    const t=S.FL[S.floor].temas[j];
    S.ctx={kind:'tema', id:t.id}; S.wall='desk'; resetStudy();
    loadTopic(t.id);
    await camera(PC, roomBox(j), fast?600:900);
    buildRoom(); roomCam('door',0); S.level='consultorio'; show('consultorio'); ui();
    await frames();
    await roomCam('desk', fast?700:1000);   // la cámara entra caminando al cuarto
    say(`Consultorio ${t.num}, ${t.title}.`);
  } finally { S.busy=false; }
  const h=R3 && R3.back.querySelector('.hot'); if (h) h.focus({preventScroll:true});
}
// La sala de repaso: se entra directo al pizarrón, sin recorrer pisos.
async function openSala(opts, mode){
  if (S.busy || S.level==='consultorio') return; S.busy=true; closeCard();
  try {
    S.ctx={kind:'sala', opts, from:S.level}; S.wall='board'; resetStudy();
    buildRoom(); roomCam('door',0); S.level='consultorio'; show('consultorio', true); ui();
    const carga=startStudy(mode ? {mode} : null);
    carga.catch(() => {});                  // se espera (y se reporta) abajo
    await frames();
    await roomCam('board', 1200);
    ui(); await carga;
  } finally { S.busy=false; }
  focusBoard();
}
async function toBoard(mode){
  if (S.busy || S.level!=='consultorio') return;
  if (S.panel) await closePanel(true);
  if (S.wall==='board'){ if (mode && mode !== S.mode) await startStudy({mode}); return; }
  S.busy=true; closeCard();
  try {
    const from=S.wall; S.wall='board'; ui();
    const carga = (!S.study || (mode && mode !== S.mode)) ? startStudy(mode ? {mode} : null) : null;
    if (carga) carga.catch(() => {});       // se espera (y se reporta) abajo
    await roomCam('board', 1100, from);
    if (carga) await carga;
  } finally { S.busy=false; }
  focusBoard();
}
async function toDesk(){
  if (S.busy || S.level!=='consultorio' || S.wall==='desk') return; S.busy=true; closeCard();
  try { S.wall='desk'; ui(); await roomCam('desk', 1100, 'board'); }
  finally { S.busy=false; }
  const e=R3 && R3.back.querySelector('.hot'); if (e) e.focus({preventScroll:true});
}
async function jump(i, j){
  closeCard();
  if (S.level==='consultorio') await leaveRoom();
  if (S.level==='planta' && S.floor!==i) await toHospital();
  if (S.level==='hospital') await goFloor(i, true);
  await goRoom(j, true);
}
// Sale del consultorio (o de la sala) al nivel de donde se entró.
async function leaveRoom(){
  if (S.busy || S.level!=='consultorio') return;
  if (S.panel) await closePanel(true);
  // una calificación en camino se espera (máx. 5 s): así S.dirty ya es cierto
  for (let k=0; S.grading && k<100; k++) await wait(50);
  S.busy=true; closeCard();
  const sala = S.ctx && S.ctx.kind==='sala', j=roomIndex();
  try {
    if (S.wall==='board'){ S.wall='desk'; if (!sala) await roomCam('desk', reduce()?0:700, 'board'); }
    await roomCam('door', reduce()?0:500);
    const dest = sala ? S.ctx.from : 'planta';
    S.ctx=null; resetStudy(); S.topicData=null;
    S.level=dest; show(dest, sala); ui();
    if (dest==='planta' && !sala) await camera(PC, planFull(), 800);
  } finally { S.busy=false; }
  if (S.dirty) await refreshData();
  const sel = S.level==='planta' && PC ? PC.svg.querySelector(`.hot[data-ix="${j}"]`) || PC.svg.querySelector('.hot')
            : HC && HC.svg.querySelector('.hot');
  if (sel) sel.focus({preventScroll:true});
}
async function toHospital(){
  if (S.busy) return;
  if (S.panel==='ajustes') await closePanel(true);
  if (S.level==='consultorio') await leaveRoom();
  if (S.level!=='planta') return;
  S.busy=true; closeCard();
  const i=S.floor;
  try {
    S.level='hospital'; show('hospital'); ui();
    await camera(HC, hospitalBox(), 1000);
    S.floor=null; S.floorId=null; ui();
  } finally { S.busy=false; }
  const g=HC.svg.querySelector(`.hot[data-ix="${i}"]`); if (g) g.focus({preventScroll:true});
}
async function goBack(){
  if (!$('#menu').hidden){ closeMenu(true); return; }
  if (!$('#card').hidden){ closeCard(true); return; }
  if (S.panel){ await closePanel(); return; }
  if (S.level==='consultorio'){ if (S.wall==='board' && !(S.ctx && S.ctx.kind==='sala')) await toDesk(); else await leaveRoom(); }
  else if (S.level==='planta') await toHospital();
}
$('#back').onclick = () => goBack().catch(avisar);

document.addEventListener('keydown', e => {
  if (document.querySelector('.dlg-back')) return;          // un diálogo de ui.js manda
  const enCampo = ['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName);
  if (e.key === 'Escape'){
    if (S.placing){ setPlacing(false); return; }
    goBack().catch(avisar); return;
  }
  if (enCampo || S.panel || !$('#card').hidden) return;
  if (S.level==='consultorio' && S.wall==='board' && !S.busy && S.card){
    // Espacio y Enter solo si el foco está en la página o en el pizarrón: nunca
    // le roban la tecla a un enlace, a un botón de la barra o al menú.
    const act=document.activeElement, chalk=$('#chalk');
    const fuera = act && act!==document.body && !(chalk && chalk.contains(act));
    const ajeno = act && act.closest && act.closest('a,[role=button],#menu');
    const enBoton = act instanceof HTMLButtonElement;
    if (!S.revealed && !fuera && !ajeno && (e.key===' ' || (e.key==='Enter' && !enBoton))){ e.preventDefault(); reveal(); }
    else if (S.revealed && /^[1-4]$/.test(e.key)){ e.preventDefault(); grade(+e.key); }
  }
});

/* ---------------------------------- paneles: el escritorio y los ajustes */
async function openLector(tab){
  if (S.level!=='consultorio' || !ctxTopic()) return;
  closeCard();
  const t=ctxTopic();
  if (!S.topicData){
    toast('Abriendo el escritorio…');
    try { await (S.topicP || loadTopic(t.id)); } catch(e){ return; }   // loadTopic ya avisó
  }
  if (!S.topicData || S.topicData.id !== t.id) return;
  if (S.wall==='board'){ S.wall='desk'; await roomCam('desk', reduce()?0:600, 'board'); }
  await roomCam('read', reduce()?0:450);
  S.panel='lector'; S.placing=false; S.railTab=tab || 'postit';
  const d=S.topicData, resumen=(d.assets.resumen||[])[0];
  $('#lTitle').textContent=d.title;
  $('#lSub').textContent=`${d.cards.length} fichas · ${resumen ? (d.pages || 0) + ' págs. de resumen' : 'sin resumen'}`;
  $('#lPin').hidden = !(resumen && S.pdf_ok); $('#lPin').textContent='📌 Pegar post-it'; $('#lPin').classList.remove('on');
  $('#lStudy').hidden = !d.cards.length;
  $('#lectorBody').classList.toggle('rail-on', !!(tab && tab !== 'postit'));
  $('#lRail').setAttribute('aria-expanded', String($('#lectorBody').classList.contains('rail-on')));
  $('#lector').hidden=false;
  $('#views').inert=true; $('#hud').inert=true;
  renderPages(); renderRail(); ui();
  $('#lClose').focus({preventScroll:true});
}
async function closePanel(rapido){
  const era=S.panel; if (!era) return;
  S.panel=null; S.placing=false;
  $('#lector').hidden=true; $('#ajustes').hidden=true;
  $('#views').inert=false; $('#hud').inert=false;
  ui();
  if (era==='lector'){
    await roomCam('desk', rapido||reduce() ? 0 : 450);
    const e=R3 && R3.back.querySelector('.hot'); if (e && !rapido) e.focus({preventScroll:true});
  } else if (era==='ajustes'){
    if (!rapido) $('#menuBtn').focus({preventScroll:true});
    if (S.dataChanged){
      S.dataChanged=false;
      if (S.level==='consultorio') S.dirty=true;            // se rehace al salir del cuarto
      else await refreshData();
    }
  }
}
$('#lClose').onclick = () => closePanel().catch(avisar);
$('#lStudy').onclick = () => toBoard().catch(avisar);
$('#lRail').onclick = () => {
  const on=!$('#lectorBody').classList.contains('rail-on');
  $('#lectorBody').classList.toggle('rail-on', on); $('#lRail').setAttribute('aria-expanded', String(on));
};
function setPlacing(on){
  S.placing=on;
  $('#lPin').textContent = on ? '✕ Cancelar' : '📌 Pegar post-it'; $('#lPin').classList.toggle('on', on);
  $$('.page').forEach(p => p.classList.toggle('placing', on));
  if (on){ toast('Toca el resumen donde quieras pegar la nota'); $('#lectorBody').classList.remove('rail-on'); }
}
$('#lPin').onclick = () => setPlacing(!S.placing);

function renderPages(){
  const t=S.topicData, resumen=(t.assets.resumen || [])[0], host=$('#lPages');
  host.textContent='';
  const arts=t.assets.articulo || [];
  if (arts.length){
    const box=H('div','arts',host); H('span','lbl',box,'Artículos del librero');
    arts.forEach(a => { const l=H('a','btn',box,String(a.filename).replace(/\.pdf$/i,''));
      l.href='/api/assets/' + encodeURIComponent(a.id) + '/file'; l.target='_blank'; l.rel='noopener noreferrer'; });
  }
  if (!resumen){
    const em=H('div','empty',host,'Este tema no tiene resumen en PDF.');
    if (S.admin){ em.append(document.createElement('br'), S.subidas
      ? 'Cárgalo desde Ajustes → Cargar material.' : 'Agrégalo con git en contenido/ y aparece en el siguiente despliegue.'); }
    return;
  }
  if (!S.pdf_ok){
    const wrapEl=H('div','pages',host);
    const em=H('div','empty',wrapEl,'Para pegar post-it sobre el resumen falta poppler: ');
    H('code','',em,'brew install poppler'); em.append('. Mientras, el PDF se ve así:'); em.style.marginBottom='14px';
    const fr=H('iframe','',wrapEl); fr.src='/api/assets/' + encodeURIComponent(resumen.id) + '/file';
    fr.title='Resumen en PDF'; fr.style.cssText='width:100%;height:78vh;border:1px solid var(--line-2);border-radius:10px;background:#fff';
    return;
  }
  const pages=H('div','pages',host), n=t.pages || 1;
  for (let i=1;i<=n;i++){
    const p=H('div','page'+(S.placing?' placing':''),pages); p.dataset.page=i;
    const img=H('img','',p); img.src=`/api/assets/${encodeURIComponent(resumen.id)}/page/${i}`;
    img.alt=`Página ${i}`; img.loading='lazy';
    p.onclick=e => onPageClick(e, p, resumen.id).catch(avisar);
    paintStickies(p);
  }
}
function paintStickies(pageEl){
  const page=+pageEl.dataset.page;
  pageEl.querySelectorAll('.sticky').forEach(el => el.remove());
  S.topicData.notes.filter(nt => nt.kind==='sticky' && nt.page===page)
    .forEach(nt => pageEl.appendChild(stickyEl(nt, pageEl)));
}
function repaintAllStickies(){ $$('#lPages .page').forEach(paintStickies); }
// Guarda el texto de una nota un poco después de dejar de escribir.
// El texto local es la verdad mientras se escribe: se copia a la nota en cada
// tecla, así repintar (otra página, el riel) nunca muestra un texto viejo.
function guardarTexto(nt, ta, despues){
  let save;
  ta.oninput = () => { nt.text=ta.value; clearTimeout(save);
    save=setTimeout(() => post('/api/notes/' + nt.id, {text: ta.value})
      .then(() => { if (despues) despues(); })
      .catch(avisar), 600); };
}
// El servidor devuelve la lista completa al crear una nota: se conservan los
// objetos que ya teníamos (con lo escrito y lo arrastrado) y se agregan los nuevos.
function mezclarNotas(lista){
  const prev=new Map(S.topicData.notes.map(n => [n.id, n]));
  S.topicData.notes = lista.map(n => prev.get(n.id) || n);
  return lista.filter(n => !prev.has(n.id)).map(n => n.id);
}
function stickyEl(nt, pageEl){
  const el=document.createElement('div'); el.className='sticky'; el.dataset.note=nt.id;
  // en una página angosta (el teléfono) la nota no se sale por la derecha y su × sigue a la vista
  const coloca=() => { el.style.left=`min(${nt.x*100}%, calc(100% - 172px))`; el.style.top=(nt.y*100)+'%'; };
  coloca();
  const x=H('button','x',el,'×'); x.type='button'; x.setAttribute('aria-label','Borrar la nota');
  const ta=H('textarea','',el); ta.placeholder='Escribe aquí…'; ta.value=nt.text || '';
  ta.setAttribute('aria-label',`Post-it de la página ${nt.page}`);
  el.onclick=e => e.stopPropagation();      // no dispara la colocación de otra nota
  // la pestaña «Post-its» del panel muestra el mismo texto
  guardarTexto(nt, ta, () => { if (S.railTab==='postit') renderRail(); });
  x.onclick=async e => { e.stopPropagation();
    try { await del('/api/notes/' + nt.id); }
    catch(err){ avisar(err); return; }
    S.topicData.notes=S.topicData.notes.filter(n => n.id !== nt.id);
    el.remove(); renderRail(); toast('Nota borrada'); };
  // Arrastrar: la posición se guarda en porcentaje, así que la nota se queda
  // en su sitio aunque cambie el tamaño de la ventana o la escala del PDF.
  el.onpointerdown=e => {
    if (e.target.tagName==='TEXTAREA' || e.target.classList.contains('x')) return;
    e.preventDefault();
    const rect=pageEl.getBoundingClientRect();
    const move=ev => {
      nt.x=Math.min(Math.max((ev.clientX-rect.left)/rect.width,0),1);
      nt.y=Math.min(Math.max((ev.clientY-rect.top)/rect.height,0),1);
      coloca();
    };
    const up=() => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      document.removeEventListener('pointercancel', up);
      post('/api/notes/' + nt.id, {x:nt.x, y:nt.y}).catch(avisar);
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
    document.addEventListener('pointercancel', up);
  };
  return el;
}
async function onPageClick(e, pageEl, assetId){
  if (!S.placing) return;
  const rect=pageEl.getBoundingClientRect();
  const x=(e.clientX-rect.left)/rect.width, y=(e.clientY-rect.top)/rect.height;
  const r=await post(`/api/topics/${S.topicData.id}/notes`,
    {kind:'sticky', asset_id:assetId, page:+pageEl.dataset.page, x, y, text:''});
  const nuevos=mezclarNotas(r.notes);
  setPlacing(false);
  repaintAllStickies(); renderRail();
  const ult=nuevos.length && $(`#lPages .sticky[data-note="${nuevos[nuevos.length-1]}"] textarea`);
  if (ult) ult.focus();
}

function renderRail(){
  if (S.panel!=='lector' || !S.topicData) return;
  const tabs=[['postit','📌 Post-its'],['general','📝 Notas'],['fichas','🎴 Fichas']];
  const bar=$('#railtabs'); bar.textContent='';
  bar.setAttribute('role','tablist');
  tabs.forEach(([k,l]) => { const b=H('button',S.railTab===k?'on':'',bar,l); b.type='button';
    b.setAttribute('role','tab'); b.setAttribute('aria-selected', String(S.railTab===k));
    b.onclick=() => { S.railTab=k; renderRail(); }; });
  const body=$('#railbody'); body.textContent='';
  const t=S.topicData;
  if (S.railTab==='fichas'){
    const box=H('div','fichas',body);
    if (!t.cards.length){ H('div','empty',box,'Sin fichas todavía.'); return; }
    t.cards.forEach(c => { const f=H('div','f',box);
      H('div','q',f).innerHTML=c.front; H('div','a',f).innerHTML=c.back; });   // HTML de Anki, saneado en el servidor
    return;
  }
  const kind = S.railTab==='postit' ? 'sticky' : 'general';
  if (kind==='general'){
    const add=H('button','btn wide',body,'＋ Nota nueva'); add.type='button';
    add.onclick=async () => {
      try {
        const r=await post(`/api/topics/${t.id}/notes`, {kind:'general', text:''});
        const nuevos=mezclarNotas(r.notes); renderRail(); repaintAllStickies();
        const ta=nuevos.length && $(`#railbody [data-note="${nuevos[nuevos.length-1]}"] textarea`);
        if (ta) ta.focus();
      } catch(e){ avisar(e); }
    };
  } else {
    H('div','tip',body, S.pdf_ok && (t.assets.resumen||[]).length
      ? 'Pulsa «📌 Pegar post-it» y toca el resumen.' : 'Los post-it van sobre el resumen en PDF.');
  }
  const notes=t.notes.filter(n => n.kind===kind);
  if (!notes.length){ H('div','empty',body,'Sin notas todavía.'); return; }
  notes.forEach(n => {
    const c=H('div','notecard',body); c.dataset.note=n.id;
    const x=H('button','x',c,'×'); x.type='button'; x.setAttribute('aria-label','Borrar la nota');
    if (kind==='sticky') H('div','meta',c,`Página ${n.page}`);
    const ta=H('textarea','',c); ta.placeholder='Escribe aquí…'; ta.value=n.text || '';
    ta.setAttribute('aria-label', kind==='sticky' ? `Post-it de la página ${n.page}` : 'Nota del tema');
    guardarTexto(n, ta, () => { if (kind==='sticky') repaintAllStickies(); });
    x.onclick=async () => {
      try { await del('/api/notes/' + n.id); } catch(e){ avisar(e); return; }
      t.notes=t.notes.filter(z => z.id !== n.id);
      renderRail(); repaintAllStickies();
    };
  });
}

/* ------------------------------------------------------------- ajustes */
function field(p, id, label, attrs){
  const d=H('div','field',p); const l=H('label','',d,label); l.htmlFor=id;
  const i=H('input','',d); i.id=id; for (const k in attrs) i.setAttribute(k, attrs[k]); return i;
}
async function openAjustes(){
  closeMenu(); closeCard();
  if (S.panel==='lector') await closePanel(true);
  S.panel='ajustes';
  const host=$('#ajustesBody'); host.textContent='';
  const s=S.settings;

  const p1=H('div','panel',host); H('h3','',p1,'Ritmo diario');
  H('p','',p1,'Es tuyo: no cambia el de nadie más. El límite de fichas nuevas es el freno importante: si entran más de las que se pueden repasar, el atraso crece solo.');
  const iNew=field(p1,'s-new','Fichas nuevas al día',{type:'number',min:0,max:200,inputmode:'numeric'}); iNew.value=s.new_limit;
  const iRev=field(p1,'s-rev','Repasos máximos al día',{type:'number',min:0,max:2000,inputmode:'numeric'}); iRev.value=s.review_limit;
  const iRet=field(p1,'s-ret','Retención objetivo',{type:'number',min:'0.70',max:'0.97',step:'0.01',inputmode:'decimal'}); iRet.value=s.retention;
  const bS=H('button','btn primary',p1,'Guardar'); bS.type='button';
  bS.onclick=async () => {
    try {
      S.settings=await post('/api/settings', {new_limit:+iNew.value, review_limit:+iRev.value, retention:+iRet.value});
      toast('Guardado'); S.dataChanged=true;
    } catch(e){ avisar(e); }
  };

  const p2=H('div','panel',host); H('h3','',p2,'Tu cuenta');
  const pp=H('p','',p2); pp.append('Entras como '); H('b','',pp,S.user.username || '');
  if (S.user.email) pp.append(' · ' + S.user.email);
  pp.append('. Al cambiar la contraseña se cierran tus otras sesiones.');
  const iCur=field(p2,'s-cur','Contraseña actual',{type:'password',autocomplete:'current-password',maxlength:256});
  const iPw=field(p2,'s-pw','Contraseña nueva',{type:'password',autocomplete:'new-password',minlength:10,maxlength:256});
  const bP=H('button','btn',p2,'Cambiar contraseña'); bP.type='button';
  bP.onclick=async () => {
    bP.disabled=true;
    try {
      const r=await post('/api/auth/password', {current:iCur.value, new:iPw.value});
      iCur.value=''; iPw.value=''; toast(r.mensaje);
    } catch(e){ avisar(e); }
    finally { bP.disabled=false; }
  };

  if (S.admin){
    const p3=H('div','panel',host); H('h3','',p3,'Pisos del hospital (carpetas)');
    H('p','',p3,'Cada carpeta es un piso. Renombrar o borrar una carpeta no borra sus temas: quedan en el piso «Sin carpeta».');
    const list=H('div','folders',p3);
    const pintaCarpetas = () => {
      list.textContent='';
      S.folders.forEach(f => {
        const r=H('div','fr',list); H('span','nm',r,f.name);
        H('span','ct',r,`${S.topics.filter(t => t.folder_id===f.id).length} temas`);
        const b=H('button','btn',r,'Renombrar'); b.type='button';
        b.onclick=async () => {
          const name=await uiPrompt('', f.name, {title:'Nombre de la carpeta'});
          if (name===null || !name.trim()) return;
          try { const x=await post('/api/folders/' + f.id, {name:name.trim()});
            S.folders=x.folders; S.dataChanged=true; pintaCarpetas(); toast('Carpeta renombrada'); }
          catch(e){ avisar(e); }
        };
        const d=H('button','btn danger',r,'Borrar'); d.type='button';
        d.onclick=async () => {
          const si=await uiConfirm(`Los temas de «${f.name}» quedarán sin carpeta.`,
                                   {title:'¿Borrar la carpeta?', ok:'Borrar', danger:true});
          if (!si) return;
          try { const x=await del('/api/folders/' + f.id);
            S.folders=x.folders; S.topics=x.topics; S.dataChanged=true; pintaCarpetas(); toast('Carpeta borrada'); }
          catch(e){ avisar(e); }
        };
      });
      if (!S.folders.length) H('div','empty',list,'Sin carpetas todavía.');
    };
    pintaCarpetas();
    const bN=H('button','btn',p3,'＋ Carpeta nueva'); bN.type='button'; bN.style.marginTop='10px';
    bN.onclick=async () => {
      const name=await uiPrompt('', '', {title:'Carpeta nueva', ok:'Crear'});
      if (!name || !name.trim()) return;
      try { const x=await post('/api/folders', {name:name.trim()});
        S.folders=x.folders; S.dataChanged=true; pintaCarpetas(); toast('Carpeta creada'); }
      catch(e){ avisar(e); }
    };

    const p4=H('div','panel',host); H('h3','',p4,'Material');
    H('p','',p4,'Se vuelve a escanear la carpeta de material del servidor. Solo los administradores ven esto.');
    const bR=H('button','btn',p4,'Re-escanear'); bR.type='button'; bR.onclick=() => rescan().catch(avisar);
  }
  $('#ajustes').hidden=false; $('#views').inert=true; $('#hud').inert=true;
  ui(); $('#aClose').focus({preventScroll:true});
}
$('#aClose').onclick = () => closePanel().catch(avisar);

/* ---------------------------------------------------------------- menú */
function closeMenu(refocus){ if ($('#menu').hidden) return; $('#menu').hidden=true;
  $('#menuBtn').setAttribute('aria-expanded','false'); if (refocus) $('#menuBtn').focus(); }
$('#menuBtn').onclick = e => { e.stopPropagation();
  const abrir=$('#menu').hidden; $('#menu').hidden=!abrir; $('#menuBtn').setAttribute('aria-expanded', String(abrir));
  if (abrir){ const p=$('#menu button, #menu a:not([hidden])'); if (p) p.focus(); } };
document.addEventListener('click', e => { if (!$('#menu').hidden && !$('#menu').contains(e.target)) closeMenu(); });
$('#mAjustes').onclick = () => openAjustes().catch(avisar);
$('#mRescan').onclick = () => { closeMenu(); rescan().catch(avisar); };
$('#salir').onclick = async () => {
  try { await post('/api/auth/logout'); } catch(e){ /* se sale igual: la cookie la borra el servidor o caduca */ }
  location.href = '/entrar';
};

/* ------------------------------------------- arranque y datos del servidor */
function paintStats(){
  const s=S.stats;
  $('#streakNum').textContent = s.racha || 0;
  $('#streak').classList.toggle('on', !!s.racha);
  if (S.level!=='consultorio' && !S.panel) ui();
}
function render(){
  S.FL=floors();
  // El piso se sigue por su id: borrar o crear una carpeta cambia los índices.
  if (S.floorId!=null){ const k=S.FL.findIndex(f => f.id===S.floorId); S.floor = k>=0 ? k : null; }
  if (S.level==='consultorio' && S.ctx && S.ctx.kind==='tema'){
    const t=ctxTopic(), k=t ? S.FL.findIndex(f => f.temas.some(x => x.id===t.id)) : -1;
    if (k>=0){ S.floor=k; S.floorId=S.FL[k].id; }
    else {                                    // el tema ya no existe
      if (S.panel) { S.panel=null; $('#lector').hidden=true; $('#ajustes').hidden=true;
        $('#views').inert=false; $('#hud').inert=false; }
      S.ctx=null; resetStudy(); S.topicData=null; S.wall='desk';
      S.level='hospital'; S.floor=null; S.floorId=null;
      toast('Ese tema ya no está en el hospital.');
    }
  }
  if (S.level==='consultorio' && S.ctx && S.ctx.kind==='sala' && S.ctx.from==='planta' && S.floor==null) S.ctx.from='hospital';
  if (S.level==='planta' && S.floor==null){ S.level='hospital'; S.floorId=null; }
  buildHospital();
  if (S.level==='hospital' || S.floor==null){
    camera(HC, hospitalBox(), 0);
    if (S.level==='planta'){ S.level='hospital'; }
  } else {
    camera(HC, floorBox(S.floor), 0); buildPlan(S.floor);
    if (S.level==='planta') camera(PC, planFull(), 0);
  }
  if (S.level==='consultorio'){
    if (S.floor!=null && PC){ const j=roomIndex(); camera(PC, j>=0 ? roomBox(j) : planFull(), 0); }
    buildRoom(); roomCam(S.panel==='lector' ? 'read' : S.wall, 0);
  }
  show(S.level); ui();
}
async function refreshData(){
  const d=await api('/api/bootstrap');
  Object.assign(S, {folders:d.folders, topics:d.topics, orphans:d.orphans, stats:d.stats,
                    actualizando:d.actualizando, subidas:d.subidas, pdf_ok:d.pdf_ok});
  S.dirty=false;
  $('#uploadBox').hidden = !(S.admin && S.subidas);
  paintStats();
  if (S.level!=='consultorio' && !S.busy) render();
  else S.dirty=true;
}
async function boot(){
  const d = await api('/api/bootstrap');
  Object.assign(S, d);
  S.admin = S.user.role === 'admin';
  // Lo que cambia el material compartido solo se le muestra al admin. El
  // servidor lo exige igual (403): esto es solo para no ofrecer botones inútiles.
  $$('.solo-admin').forEach(el => { el.hidden = !S.admin; });
  // En producción el material se agrega con git: la carga no existe allí.
  $('#uploadBox').hidden = !(S.admin && S.subidas);
  const nombre = S.user.nombre || S.user.username || '';
  $('#quien').textContent = nombre; $('#menuNombre').textContent = nombre;
  $('#menuCorreo').textContent = S.user.email || S.user.username || '';
  render(); paintStats();
  vigilarIngesta();
}

/* Justo después de un despliegue (o de que el servidor despierte) la ingesta
   del material corre unos segundos en segundo plano y la biblioteca puede
   llegar vacía o a medias. Se avisa y se vuelve a pedir, sin tocar lo que se
   esté estudiando: si hay un consultorio abierto, se rehace al salir. */
let vigilando = null;
function vigilarIngesta(){
  if (!S.actualizando || vigilando) return;
  toast('Actualizando el material…');
  vigilando = setTimeout(async () => {
    vigilando = null;
    try { await refreshData(); }
    catch(e){ avisar(e); return; }
    vigilarIngesta();
  }, 3000);
}

async function rescan(){
  toast('Escaneando…');
  const r=await post('/api/ingest');
  toast(`${r.temas} temas · ${r.tarjetas_nuevas} fichas nuevas · ${r.materiales} materiales`);
  if (S.panel==='ajustes') S.dataChanged=true; else await refreshData();
}

/* -------------------------------------------------------------- cargar */
$('#uploadBtn').onclick = () => $('#fileInput').click();
$('#fileInput').onchange = async e => {
  const file = e.target.files[0]; if (!file) return;
  e.target.value = '';
  const esPdf = file.name.toLowerCase().endsWith('.pdf');
  let url = '/api/upload';
  if (esPdf){
    const t=ctxTopic();
    const destino = (t && t.id) || await uiElegir('El PDF se guarda como resumen del tema que elijas.',
      S.topics.map(x => ({value: x.id, label: x.title})), {title:'¿De qué tema es este PDF?', ok:'Cargar'});
    if (!destino) return;
    url += '?topic_id=' + parseInt(destino, 10);
  }
  const fd = new FormData(); fd.append('file', file);
  toast('Cargando…');
  try {
    const r = await api(url, {method:'POST', body: fd});
    S.dataChanged = true;
    toast(r.fichas_nuevas !== undefined
      ? `${r.titulo}: ${r.fichas_nuevas} fichas nuevas, ${r.fichas_actualizadas} actualizadas`
      : `${r.archivo} cargado`);
  } catch(err){ avisar(err); }
};

// Al cambiar el tamaño se rehace la escena; si hay una animación en curso, se
// espera a que termine.
let rz=0;
function renderLuego(){
  clearTimeout(rz);
  rz=setTimeout(() => { if (S.busy){ renderLuego(); return; } try { render(); } catch(e){ avisar(e); } }, 150);
}
addEventListener('resize', renderLuego);

boot().catch(e => {
  const host=$('#vHospital'); host.textContent='';
  const ti=H('div','title',host); H('div','k',ti,'MedStudy'); H('h1','',ti,'Sin conexión.');
  H('p','',ti,'No se pudo cargar el hospital: ' + ((e && e.message) || 'error desconocido') + '. Recarga la página para intentarlo de nuevo.');
  avisar(e);
});
