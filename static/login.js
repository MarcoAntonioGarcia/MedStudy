// login.js — pantalla de entrada. Va en un archivo aparte (no en el HTML)
// porque la política de contenido no deja ejecutar scripts en línea.
const $ = s => document.querySelector(s);
const aviso = $("#aviso");

function decir(texto, tipo){
  aviso.textContent = texto;
  aviso.className = "aviso " + tipo;
}

async function enviar(url, cuerpo, boton){
  if (boton) boton.disabled = true;
  try{
    const r = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(cuerpo),
      credentials: "same-origin",
    });
    const datos = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof datos.detail === "string" ? datos.detail
                               : "Algo salió mal. Inténtalo de nuevo.");
    return datos;
  } catch(e){
    if (e instanceof TypeError) throw new Error("No se puede contactar con el servidor. Revisa tu conexión.");
    throw e;
  } finally {
    if (boton) boton.disabled = false;
  }
}

$("#formEntrar").onsubmit = async e => {
  e.preventDefault();
  try{
    await enviar("/api/auth/login",
      {login: $("#login").value.trim(), password: $("#clave").value}, e.submitter);
    location.href = "/";
  }catch(err){ decir(err.message, "error"); }
};
