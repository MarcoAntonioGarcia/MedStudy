// cuenta.js — los cuatro pasos de cuenta (portado de EnglishApp):
// registro, crear la cuenta desde el enlace, olvidé mi contraseña y
// restablecerla desde el enlace. Cada uno se elige por la ruta.
const $ = s => document.querySelector(s);
const aviso = $("#aviso");
function decir(texto, tipo){ aviso.textContent = texto; aviso.className = "aviso " + tipo; }

// El token viaja en el FRAGMENTO de la URL (#t=...), que el navegador nunca
// manda al servidor. Se lee una vez y se borra de la barra y del historial.
const token = new URLSearchParams(location.hash.slice(1)).get("t") || "";
if (location.hash) history.replaceState(null, "", location.pathname);

async function post(url, cuerpo, boton){
  if (boton) boton.disabled = true;
  try{
    const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
                                body: JSON.stringify(cuerpo), credentials: "same-origin"});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Algo salió mal. Inténtalo de nuevo.");
    return d;
  } catch(e){
    if (e instanceof TypeError) throw new Error("No se puede contactar con el servidor. Revisa tu conexión.");
    throw e;
  } finally { if (boton) boton.disabled = false; }
}

function mostrar(id, titulo, sub){
  $("#" + id).hidden = false;
  $("#titulo").textContent = titulo;
  $("#subtitulo").textContent = sub || "";
  document.title = titulo + " · MedStudy";
}

function caducado(texto, proposito){
  $("#subtitulo").textContent = "";
  aviso.className = "aviso error";
  aviso.textContent = texto + " ";
  const a = document.createElement("a");
  a.href = proposito === "reset" ? "/forgot-password" : "/signup";
  a.textContent = "Pide un enlace nuevo";
  aviso.appendChild(a);
}

async function comprobarEnlace(proposito, destino, form){
  if (!token){ caducado("Este enlace está incompleto. Ábrelo de nuevo desde tu correo, o", proposito); return false; }
  try{
    const d = await post("/api/auth/link", {token, purpose: proposito});
    if (!d.valid){ caducado("Este enlace caducó o ya se usó.", proposito); return false; }
    $(destino).textContent = d.email;
    if (d.username){
      $("#rUser").textContent = "Usuario: " + d.username;
      $("#rUserField").value = d.username;
    }
    $(form).hidden = false;
    return true;
  }catch(e){ decir(e.message, "error"); return false; }
}

// Un enlace nuevo abierto en una pestaña que ya estaba aquí solo cambia el #t=:
// se recarga para leerlo.
window.addEventListener("hashchange", () => location.reload());

const ruta = location.pathname;
if (ruta === "/signup"){
  mostrar("fSignup", "Crea tu cuenta", "Repaso espaciado, resúmenes y notas para estudiar medicina interna.");
  $("#fSignup").onsubmit = async e => {
    e.preventDefault();
    try{
      const d = await post("/api/auth/register", {email: $("#sEmail").value.trim()}, e.submitter);
      $("#fSignup").hidden = true;
      decir(d.message, "ok");
    }catch(err){ decir(err.message, "error"); }
  };
} else if (ruta === "/create-account"){
  mostrar("fCreate", "Elige tu usuario", "Último paso: tu correo ya está confirmado.");
  $("#fCreate").hidden = true;
  comprobarEnlace("signup", "#cEmail", "#fCreate");
  $("#fCreate").onsubmit = async e => {
    e.preventDefault();
    try{
      await post("/api/auth/register/complete", {
        token, username: $("#cUser").value.trim(), nombre: $("#cName").value.trim(),
        password: $("#cPass").value}, e.submitter);
      location.href = "/";
    }catch(err){ decir(err.message, "error"); }
  };
} else if (ruta === "/forgot-password"){
  mostrar("fForgot", "¿Olvidaste tu contraseña?", "");
  $("#fForgot").onsubmit = async e => {
    e.preventDefault();
    try{
      const d = await post("/api/auth/password/forgot", {email: $("#oEmail").value.trim()}, e.submitter);
      $("#fForgot").hidden = true;
      decir(d.message, "ok");
    }catch(err){ decir(err.message, "error"); }
  };
} else if (ruta === "/reset-password"){
  mostrar("fReset", "Pon una contraseña nueva", "");
  $("#fReset").hidden = true;
  comprobarEnlace("reset", "#rEmail", "#fReset");
  $("#fReset").onsubmit = async e => {
    e.preventDefault();
    try{
      await post("/api/auth/password/reset", {token, password: $("#rPass").value}, e.submitter);
      location.href = "/";
    }catch(err){ decir(err.message, "error"); }
  };
}
