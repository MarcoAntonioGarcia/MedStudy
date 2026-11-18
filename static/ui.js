// ui.js — diálogos dentro de la página, compartidos por la app y el panel.
/* Sustituyen a prompt / alert / confirm (adaptado de EnglishApp).
   Los nativos bloquean la página, ignoran el tema y en el móvil se leen mal.
   Estos devuelven promesas; Esc o el fondo = cancelar. Mientras hay uno
   abierto, el resto de la página queda inerte. Un diálogo destructivo empieza
   en «Cancelar»: un Enter perdido nunca debe borrar nada. */
let _dlgSeq = 0;
function uiDialog({title, text, input, options, ok = "Aceptar", cancel = "Cancelar",
                   danger = false, type = "text"}){
  return new Promise(res => {
    const n = ++_dlgSeq;
    const back = document.createElement("div"); back.className = "dlg-back";
    const d = document.createElement("div"); d.className = "dlg";
    d.setAttribute("role", danger ? "alertdialog" : "dialog"); d.setAttribute("aria-modal", "true");
    const h = document.createElement("h2"); h.id = "dlgT" + n;
    h.textContent = title || (input !== undefined ? "Escribe un valor" : "Confirmar");
    d.setAttribute("aria-labelledby", h.id); d.appendChild(h);
    if (text){
      const p = document.createElement("p"); p.id = "dlgD" + n; p.textContent = text;
      d.setAttribute("aria-describedby", p.id); d.appendChild(p);
    }
    let campo = null;
    if (options){
      // lista de opciones: [{value, label}]
      campo = document.createElement("select");
      for (const o of options){
        const op = document.createElement("option");
        op.value = String(o.value); op.textContent = o.label;
        campo.appendChild(op);
      }
      if (input !== undefined && input !== null) campo.value = String(input);
    } else if (input !== undefined){
      campo = document.createElement("input"); campo.type = type;
      campo.value = input == null ? "" : String(input);
    }
    if (campo){
      campo.setAttribute("aria-labelledby", h.id);
      if (text) campo.setAttribute("aria-describedby", "dlgD" + n);
      d.appendChild(campo);
    }
    const acts = document.createElement("div"); acts.className = "acts";
    const bc = document.createElement("button"); bc.type = "button"; bc.className = "dlg-btn"; bc.textContent = cancel;
    const bo = document.createElement("button"); bo.type = "button";
    bo.className = "dlg-btn " + (danger ? "danger" : "primary"); bo.textContent = ok;
    acts.append(bc, bo); d.appendChild(acts); back.appendChild(d);

    const others = [...document.body.children].filter(el => !el.inert && el.tagName !== "SCRIPT");
    others.forEach(el => { el.inert = true; });
    document.body.appendChild(back);

    const prev = document.activeElement;
    const close = v => {
      document.removeEventListener("keydown", key, true);
      back.remove();
      others.forEach(el => { el.inert = false; });
      if (prev && prev.focus) prev.focus();
      res(v);
    };
    const cancelar = () => close(campo ? null : false);
    const aceptar = () => close(campo ? campo.value : true);
    const key = e => {
      if (e.key === "Escape"){ e.preventDefault(); e.stopImmediatePropagation(); cancelar(); }
      else if (e.key === "Enter" && campo && document.activeElement === campo){ e.preventDefault(); aceptar(); }
      else if (e.key === "Tab"){
        const f = [...d.querySelectorAll("input,select,button")]; const i = f.indexOf(document.activeElement);
        if (e.shiftKey && i <= 0){ e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && i === f.length - 1){ e.preventDefault(); f[0].focus(); }
      }
    };
    document.addEventListener("keydown", key, true);
    bc.onclick = cancelar;
    bo.onclick = aceptar;
    back.addEventListener("click", e => { if (e.target === back) cancelar(); });
    if (campo){ campo.focus(); if (campo.select) campo.select(); } else (danger ? bc : bo).focus();
  });
}
function uiConfirm(text, o = {}){
  return uiDialog({title: o.title || "¿Seguro?", text, ok: o.ok || "Continuar", danger: !!o.danger});
}
function uiPrompt(text, value, o = {}){
  return uiDialog({title: o.title || "", text, input: value, ok: o.ok || "Guardar", type: o.type || "text"});
}
function uiElegir(text, options, o = {}){
  return uiDialog({title: o.title || "", text, options, input: o.value, ok: o.ok || "Elegir"});
}
