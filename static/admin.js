// admin.js — panel de usuarios (portado de EnglishApp): crear, bloquear y
// reactivar, cerrar sesiones y poner una contraseña temporal.
const $ = s => document.querySelector(s);
let yo = null;

// Todo lo que viene de la base y se mete como HTML pasa por aquí. El nombre
// de una persona es texto libre: sin esto, un nombre con <img onerror=…> se
// ejecutaría al pintar la tabla.
const esc = s => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

const ETIQUETA = {pending: "Pendiente", active: "Activa", blocked: "Bloqueada"};

function decir(texto, tipo){
  const a = $("#aviso");
  a.textContent = texto;
  a.className = "aviso " + tipo;
  if (tipo === "ok") setTimeout(() => { a.className = "aviso"; }, 3500);
}

async function llamar(url, cuerpo){
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
                              body: JSON.stringify(cuerpo), credentials: "same-origin"});
  if (r.status === 401){ location.href = "/entrar"; throw new Error("Tu sesión terminó."); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "No se pudo completar.");
  return d;
}

function acciones(u){
  const soyYo = yo && u.id === yo.id;
  const b = [];
  if (u.status !== "active")
    b.push(`<button class="ok" data-a="active" data-id="${u.id}">Activar</button>`);
  if (u.status === "active" && !soyYo)
    b.push(`<button class="mal" data-a="blocked" data-id="${u.id}">Bloquear</button>`);
  if (u.sesiones) b.push(`<button class="mal" data-a="cerrar" data-id="${u.id}">Cerrar sesiones</button>`);
  b.push(`<button data-a="clave" data-id="${u.id}">Poner contraseña</button>`);
  return b.join("");
}

function pintar(usuarios){
  $("#filas").innerHTML = usuarios.map(u => `
    <tr>
      <td>
        <div><b>${esc(u.nombre || u.username)}</b></div>
        <div class="sub">
          ${u.role === "admin" ? '<span class="chip admin">Admin</span> ' : ""}
          entra como <code>${esc(u.username)}</code>${u.email ? " · " + esc(u.email) : ""}
        </div>
      </td>
      <td><span class="chip ${esc(u.status)}">${esc(ETIQUETA[u.status] || u.status)}</span></td>
      <td class="sub">${u.sesiones ? "dentro desde " + esc((u.sesion_desde || "").slice(5, 16)) : "fuera"}</td>
      <td class="sub">${esc((u.created_at || "").slice(0, 10))}</td>
      <td><div class="acciones">${acciones(u)}</div></td>
    </tr>`).join("");
}

async function cargar(){
  const r = await fetch("/api/admin/users", {credentials: "same-origin"});
  if (r.status === 401){ location.href = "/entrar"; return; }
  if (!r.ok){ decir("No se pudo cargar la lista.", "error"); return; }
  pintar(await r.json());
}

$("#filas").addEventListener("click", async e => {
  const b = e.target.closest("button");
  if (!b) return;
  const id = Number(b.dataset.id);
  b.disabled = true;
  try{
    if (b.dataset.a === "cerrar"){
      const d = await llamar(`/api/admin/users/${id}/sesiones/cerrar`, {});
      decir("Sesiones cerradas: " + d.cerradas + ". Tendrá que volver a entrar.", "ok");
    } else if (b.dataset.a === "clave"){
      const clave = await uiPrompt("Al menos 10 caracteres. Se cerrarán sus sesiones abiertas.", "",
                                   {title: "Contraseña temporal", ok: "Poner contraseña"});
      if (clave === null) return;
      await llamar(`/api/admin/users/${id}/password`, {password: clave});
      decir("Contraseña cambiada y sesiones cerradas.", "ok");
    } else if (b.dataset.a === "blocked"){
      const si = await uiConfirm("No podrá entrar hasta que la actives de nuevo. Sus datos se conservan.",
                                 {title: "¿Bloquear esta cuenta?", ok: "Bloquear", danger: true});
      if (!si) return;
      await llamar(`/api/admin/users/${id}/status`, {status: "blocked"});
      decir("Cuenta bloqueada.", "ok");
    } else {
      await llamar(`/api/admin/users/${id}/status`, {status: "active"});
      decir("Cuenta activa.", "ok");
    }
    await cargar();
  }catch(err){
    decir(err.message, "error");
  }finally{
    b.disabled = false;
  }
});

// Una contraseña temporal decente, generada en el NAVEGADOR con el generador
// criptográfico: no pasa por el servidor hasta que se pulsa «Crear».
$("#btnSugerir").onclick = () => {
  const abc = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const n = new Uint32Array(14);
  crypto.getRandomValues(n);
  $("#nuevaClave").value = [...n].map(x => abc[x % abc.length]).join("");
};

$("#btnCrear").onclick = async () => {
  const b = $("#btnCrear"), m = $("#crearMsg"), ficha = $("#ficha");
  const clave = $("#nuevaClave").value;
  m.textContent = "";
  ficha.className = "ficha";
  b.disabled = true;
  try{
    const d = await llamar("/api/admin/users", {
      username: $("#nuevoUsuario").value.trim(), password: clave,
      nombre: $("#nuevoNombre").value.trim(), email: $("#nuevoCorreo").value.trim()});
    // se muestran los datos UNA vez, para poder copiarlos y pasarlos
    ficha.innerHTML = "Cuenta creada. Pásale estos datos:<br><br>" +
      "Usuario: <code>" + esc(d.username) + "</code><br>" +
      "Contraseña: <code>" + esc(clave) + "</code>" +
      "<br><br><span class=\"sub\">No se volverán a mostrar. Si se pierde la " +
      "contraseña, pon otra desde la tabla.</span>";
    ficha.className = "ficha on";
    ["nuevoNombre", "nuevoUsuario", "nuevaClave", "nuevoCorreo"].forEach(i => { $("#" + i).value = ""; });
    await cargar();
  }catch(err){
    m.textContent = err.message;
    m.style.color = "var(--danger)";
  }finally{
    b.disabled = false;
  }
};

fetch("/api/auth/me", {credentials: "same-origin"})
  .then(r => { if (r.status === 401){ location.href = "/entrar"; return null; } return r.json(); })
  .then(u => { if (u){ yo = u; cargar(); } })
  .catch(() => decir("No se puede contactar con el servidor.", "error"));
