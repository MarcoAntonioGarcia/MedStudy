"""
webapp.py — MedStudy (App de Estudio). Servidor web.

    python webapp.py        ->  http://localhost:8090

Es una capa fina sobre core.py: aquí van las rutas HTTP, las cuentas, los
permisos y la validación de entrada. Toda la lógica de estudio (ingesta,
repaso espaciado, fichas) vive en core.py.

Cuentas, sesiones, topes de intentos, límites de tamaño y cabeceras de
seguridad están portados de EnglishApp, que ya corre en producción.

Quién puede qué:
    sin sesión   solo las rutas de /api/auth/* (entrar, alta, restablecer)
    usuario      estudiar: su progreso, sus notas y sus ajustes, privados
    admin        además, el material compartido: re-escanear, cargar
                 archivos, carpetas, mover temas y el panel /admin
"""

from __future__ import annotations

import hashlib
import hmac
import html as html_mod
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

import core
import correo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

_log = logging.getLogger("estudio.web")


def _es_produccion(entorno) -> bool:
    """Desplegado de verdad: en Render (que define RENDER) o servido por HTTPS."""
    return core.es_produccion(entorno)


def problema_de_produccion(entorno) -> str:
    """Qué falta para poder arrancar en producción, o cadena vacía si nada.

    Falla CERRADO: sin cookie segura, sin la cabecera de la IP real (detrás de
    un proxy todos compartirían la IP del proxy y un desconocido dejaría fuera
    a todo el mundo), sin secreto propio (las cookies de dispositivo se
    invalidarían en cada reinicio) o sin Postgres (el disco del plan gratuito
    se borra en cada reinicio: una base SQLite perdería las cuentas y el
    progreso de todos), es mejor no arrancar que arrancar mal.
    """
    if not _es_produccion(entorno):
        return ""
    faltan = []
    if entorno.get("COOKIE_SEGURA", "") != "1":
        faltan.append("COOKIE_SEGURA=1")
    if not entorno.get("IP_DE_CABECERA", "").strip():
        faltan.append("IP_DE_CABECERA")
    if not entorno.get("SECRETO_APP", "").strip():
        faltan.append("SECRETO_APP")
    if not re.match(r"postgres(ql)?://", entorno.get("DATABASE_URL", "").strip(), re.I):
        faltan.append("DATABASE_URL (Postgres)")
    if not faltan:
        return ""
    return ("MedStudy no arranca: parece un despliegue (MEDSTUDY_PRODUCCION, RENDER o "
            "APP_URL con https://) "
            "pero falta configurar: " + ", ".join(faltan) + ".")


# Se comprueba ANTES de abrir la base: en un despliegue mal configurado no se
# toca nada, ni siquiera se crea un database.db en un disco que se va a borrar.
EN_PRODUCCION = _es_produccion(os.environ)
_problema = problema_de_produccion(os.environ)
if _problema:
    raise RuntimeError(_problema)


@asynccontextmanager
async def _ciclo_de_vida(_app):
    """Al arrancar (también con `uvicorn webapp:app`, que es como corre en
    Render): avisos, el primer admin y la ingesta del material, que va en
    segundo plano para que la comprobación de salud responda enseguida."""
    await run_in_threadpool(arrancar)
    yield


# La documentación automática (/docs, /openapi.json) lista todos los endpoints,
# los del admin incluidos. Solo se sirve si se pide: DOCS=1.
_DOCS = os.environ.get("DOCS", "") == "1"
app = FastAPI(title="MedStudy",
              docs_url="/docs" if _DOCS else None,
              redoc_url=None,
              openapi_url="/openapi.json" if _DOCS else None,
              lifespan=_ciclo_de_vida)
db = core.DatabaseManager()


# --------------------------------------------------------------------------- #
# Sesión
# --------------------------------------------------------------------------- #
COOKIE = "sesion"
DIAS_DE_SESION = 30

# En producción la cookie tiene que ir SOLO por HTTPS. En local se sirve por
# HTTP plano, así que hay que activarlo al desplegar: COOKIE_SEGURA=1
COOKIE_SEGURA = os.environ.get("COOKIE_SEGURA", "") == "1"

# Topes de intentos, en memoria a propósito (como en EnglishApp): para unas
# decenas de usuarios no merece una tabla. (tope, ventana en segundos)
#   login: 10 fallos por cuarto de hora y por IP, contra la fuerza bruta.
#   login_cuenta: 20 fallos por cuarto de hora contra UNA cuenta, vengan de la
#     IP que vengan. Es el que de verdad frena: el de IP se esquiva cambiando
#     de IP; este no.
#   login_dispositivo: el mismo tope, aparte, para quien entra desde un aparato
#     donde ya entró bien (cookie 'dispositivo'). Así un desconocido que machaca
#     tu cuenta no te deja fuera a ti.
#   alta / olvido: 5 peticiones de correo por hora y por IP (cada una gasta un
#     envío del cupo gratuito de Brevo).
#   enlace: 30 usos de enlaces por cuarto de hora y por IP.
TOPES = {"login": (10, 15 * 60), "login_cuenta": (20, 15 * 60),
         "login_dispositivo": (20, 15 * 60), "alta": (5, 60 * 60),
         "olvido": (5, 60 * 60), "enlace": (30, 15 * 60)}
_intentos: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()
_MAX_CLAVES_INTENTOS = 20000
# Los endpoints sync corren en varios hilos: sin candado, una ráfaga simultánea
# pasaría la comprobación antes de que se anotara ningún fallo.
_candado_intentos = threading.Lock()

# De qué cabecera sale la IP real del cliente cuando hay un proxy delante (en
# local va vacía y se usa la del socket). Solo una cabecera que el proxy
# SOBRESCRIBE: X-Forwarded-For la puede inventar cualquiera.
IP_DE_CABECERA = os.environ.get("IP_DE_CABECERA", "").strip().lower()


def _agrupar_ip(valor: str) -> str | None:
    """La IP como clave del tope. Una IPv6 se agrupa por su /64: cada conexión
    doméstica recibe millones de direcciones y, si no, cada una contaría aparte."""
    try:
        ip = ipaddress.ip_address(valor.strip())
    except ValueError:
        return None
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _ip(peticion: Request) -> str:
    if IP_DE_CABECERA:
        agrupada = _agrupar_ip(peticion.headers.get(IP_DE_CABECERA) or "")
        if agrupada:
            return agrupada
        # En producción, una petición sin la cabecera (o con basura) no pasó por
        # el proxy como debía: caer a la IP del socket juntaría a todos en una.
        if EN_PRODUCCION:
            raise HTTPException(400, "Petición sin el origen esperado.")
    socket = peticion.client.host if peticion.client else ""
    return _agrupar_ip(socket) or socket or "?"


def _clave_intento(accion: str, quien: str) -> tuple[str, str]:
    # se guarda un hash: lo tecleado puede ser largo y no hace falta tenerlo
    return accion, hashlib.sha256(quien.encode("utf-8")).hexdigest()[:32]


def _reservar_intento(accion: str, quien: str) -> float | None:
    """Comprueba el tope y apunta el intento en un solo paso, bajo el candado.
    Devuelve la marca apuntada, o None si ya no quedan intentos."""
    tope, ventana = TOPES[accion]
    clave = _clave_intento(accion, quien)
    ahora = time.time()
    with _candado_intentos:
        recientes = [t for t in _intentos.get(clave, []) if ahora - t < ventana]
        if len(recientes) >= tope:
            _intentos[clave] = recientes
            return None
        recientes.append(ahora)
        _intentos[clave] = recientes
        _intentos.move_to_end(clave)
        while len(_intentos) > _MAX_CLAVES_INTENTOS:
            _intentos.popitem(last=False)
        return ahora


def _devolver_intento(accion: str, quien: str, marca: float) -> None:
    """Quita SOLO la marca de este intento (el que salió bien), no las demás."""
    clave = _clave_intento(accion, quien)
    with _candado_intentos:
        lista = _intentos.get(clave)
        if lista and marca in lista:
            lista.remove(marca)


def _olvidar_intentos(accion: str, quien: str) -> None:
    with _candado_intentos:
        _intentos.pop(_clave_intento(accion, quien), None)


# Cookie de "dispositivo conocido" (patrón de OWASP contra el bloqueo de
# cuentas). Va firmada con un secreto del servidor: no se puede fabricar. Sin
# SECRETO_APP se inventa uno al arrancar y las cookies valen hasta el reinicio.
COOKIE_DISPOSITIVO = "dispositivo"
DIAS_DISPOSITIVO = 180
_SECRETO = (os.environ.get("SECRETO_APP") or "").encode("utf-8") or secrets.token_bytes(32)


def _firma_dispositivo(uid: int, emitida: int) -> str:
    return hmac.new(_SECRETO, f"dispositivo:{uid}:{emitida}".encode(), hashlib.sha256).hexdigest()


def _cookie_dispositivo(uid: int) -> str:
    emitida = int(time.time())
    return f"{uid}.{emitida}.{_firma_dispositivo(uid, emitida)}"


def _dispositivo_conocido(valor: str, uid: int) -> bool:
    try:
        cuenta, emitida, firma = valor.split(".")
        cuenta, emitida = int(cuenta), int(emitida)
    except (ValueError, AttributeError):
        return False
    if cuenta != uid or time.time() - emitida > DIAS_DISPOSITIVO * 86400:
        return False
    return hmac.compare_digest(firma, _firma_dispositivo(uid, emitida))


# --------------------------------------------------------------------------- #
# Límites de tamaño y cabeceras de seguridad
# --------------------------------------------------------------------------- #
# Sin tope, una subida de 1 GB se carga entera en la memoria y tumba la app
# para todos. Cada ruta tiene el suyo; el resto, 1 MB, que sobra para
# cualquier JSON de la app.
LIMITE_CUERPO = 1 * 1024 * 1024
# (tope, quién puede usarlo). Se decide con la sesión ANTES de leer el cuerpo:
# FastAPI lee el formulario entero antes de comprobar quién llama, así que sin
# esto cualquiera sin cuenta podría hacer subir 25 MB al servidor para nada.
LIMITES_POR_RUTA = {
    "/api/upload": (25 * 1024 * 1024, "admin"),     # resúmenes PDF y archivos de fichas
}

# En producción el disco es efímero (plan gratuito de Render): un archivo
# cargado desde la app desaparecería en el siguiente reinicio y su fila en la
# base quedaría apuntando a la nada. Allí el material se agrega con git: va en
# la imagen y la ingesta del arranque lo incorpora. En local, la carga funciona.
SUBIDAS_APAGADAS = EN_PRODUCCION
MENSAJE_SUBIDAS_APAGADAS = ("En producción el material se agrega con git: ponlo en "
                            "contenido/ del repositorio y aparece en el siguiente despliegue.")


def _limite_de(scope) -> int:
    tope, quien = LIMITES_POR_RUTA.get(scope.get("path", ""), (LIMITE_CUERPO, "user"))
    usuario = (scope.get("state") or {}).get("usuario") or {}
    if not usuario or (quien == "admin" and usuario.get("role") != "admin"):
        return LIMITE_CUERPO
    return tope


# Política de contenido: la página solo carga lo suyo y no habla con otros
# dominios. El JavaScript vive en archivos (static/*.js), así que no hace
# falta 'unsafe-inline' para los scripts: el HTML de las fichas (viene de
# archivos de Anki) no puede ejecutar código aunque traiga un onerror=.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-src 'self'; "
       "frame-ancestors 'none'")
# El PDF de un material se muestra dentro de un iframe de la propia app cuando
# falta poppler: esa respuesta sí puede ir enmarcada, pero solo por nosotros.
# Una política más larga (object-src 'none') rompe el visor de PDF del navegador.
CSP_ARCHIVO = "frame-ancestors 'self'"
_RUTA_ARCHIVO = re.compile(r"^/api/assets/\d+/file$")


class _CuerpoDemasiadoGrande(Exception):
    pass


class SeguridadMiddleware:
    """Corta los cuerpos demasiado grandes y añade las cabeceras de seguridad.

    El tope se mira dos veces: por Content-Length (rechazo inmediato, sin leer
    nada) y contando lo que llega, porque una subida 'chunked' no lo declara.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limite = _limite_de(scope)
        ruta = scope.get("path", "")
        es_archivo = bool(_RUTA_ARCHIVO.match(ruta))
        csp = (CSP_ARCHIVO if es_archivo else CSP).encode()
        marcos = b"SAMEORIGIN" if es_archivo else b"DENY"
        empezada = False
        excedido = False        # el cuerpo pasó del tope mientras se leía
        tapada = False          # ya se mandó el 413 en lugar de la respuesta

        def seguridad() -> list[tuple[bytes, bytes]]:
            extra = [
                (b"content-security-policy", csp),
                (b"x-content-type-options", b"nosniff"),
                (b"x-frame-options", marcos),
                (b"referrer-policy", b"same-origin"),
                (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
            ]
            if COOKIE_SEGURA:
                extra.append((b"strict-transport-security",
                              b"max-age=31536000; includeSubDomains"))
            return extra

        async def enviar(mensaje):
            nonlocal empezada, tapada
            # FastAPI convierte cualquier fallo al leer el cuerpo en un 400
            # genérico. Si fue por el tope, se responde 413 con un mensaje claro.
            if excedido and not empezada and mensaje["type"] == "http.response.start":
                tapada = True
                await rechazar()
                return
            if tapada:
                return
            if mensaje["type"] == "http.response.start":
                empezada = True
                cabeceras = list(mensaje.get("headers", []))
                extra = seguridad()
                # Lo de /api/ es privado (progreso, notas): que ningún caché
                # intermedio ni el del navegador lo guarde, salvo que la ruta
                # diga otra cosa (las páginas renderizadas del resumen).
                if ruta.startswith("/api/") and not any(
                        k.lower() == b"cache-control" for k, _ in cabeceras):
                    extra.append((b"cache-control", b"no-store"))
                mensaje = {**mensaje, "headers": cabeceras + extra}
            await send(mensaje)

        async def rechazar(estado: int = 413,
                           detalle: str = "El archivo o el texto es demasiado grande."):
            nonlocal empezada
            empezada = True
            cuerpo = json.dumps({"detail": detalle}).encode()
            await send({"type": "http.response.start", "status": estado,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(cuerpo)).encode()),
                                    *seguridad()]})
            await send({"type": "http.response.body", "body": cuerpo})

        # Con la carga apagada (producción) se contesta sin leer el cuerpo: si
        # no, FastAPI leería el archivo entero antes de llegar a la ruta.
        # Mismo orden de respuestas que la ruta: sin sesión 401, sin ser admin 403.
        if SUBIDAS_APAGADAS and ruta == "/api/upload":
            quien = (scope.get("state") or {}).get("usuario") or {}
            if not quien:
                return await rechazar(401, "Inicia sesión para continuar.")
            if quien.get("role") != "admin":
                return await rechazar(403, "Solo un administrador puede hacer esto.")
            return await rechazar(409, MENSAJE_SUBIDAS_APAGADAS)

        for nombre, valor in scope.get("headers", []):
            if nombre == b"content-length":
                try:
                    if int(valor) > limite:
                        return await rechazar()
                except ValueError:
                    return await rechazar()

        recibido = 0

        async def recibir():
            nonlocal recibido, excedido
            mensaje = await receive()
            if mensaje["type"] == "http.request":
                recibido += len(mensaje.get("body", b""))
                if recibido > limite:
                    excedido = True
                    raise _CuerpoDemasiadoGrande()
            return mensaje

        try:
            await self.app(scope, recibir, enviar)
        except _CuerpoDemasiadoGrande:
            if not empezada:
                await rechazar()


class SesionMiddleware:
    """Resuelve la sesión UNA vez por petición, antes de nada, y la deja en
    scope['state']['usuario']. Es ASGI puro (como en EnglishApp) para que los
    topes de tamaño sepan quién llama antes de leer el cuerpo."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            usuario = None
            # Los estilos y scripts no necesitan saber quién llama: no se toca
            # la base por ellos. Para lo demás, la consulta (síncrona, con su
            # candado) va a un hilo y no bloquea el bucle de eventos.
            if not scope.get("path", "").startswith("/static/"):
                token = Request(scope).cookies.get(COOKIE, "")
                if token:
                    usuario = await run_in_threadpool(db.user_for_token, token)
            scope.setdefault("state", {})["usuario"] = usuario
        await self.app(scope, receive, send)


# La última que se añade es la más externa. La sesión va por fuera para que los
# topes de tamaño sepan quién llama antes de leer nada del cuerpo.
app.add_middleware(SeguridadMiddleware)
app.add_middleware(SesionMiddleware)


def usuario_de_la_peticion(peticion: Request) -> dict | None:
    """La ficha del usuario con sesión, o None. No lanza."""
    return peticion.scope.get("state", {}).get("usuario")


def current_user(peticion: Request) -> int:
    """El id de quien hace la petición. 401 si no hay sesión válida.

    De aquí sale el user_id de todos los endpoints: el progreso, las notas y
    los ajustes de cada consulta van acotados por él.
    """
    u = usuario_de_la_peticion(peticion)
    if not u:
        raise HTTPException(401, "Inicia sesión para continuar.")
    return u["id"]


def current_admin(peticion: Request) -> int:
    """Como current_user, pero además exige ser admin (403 si no lo es)."""
    u = usuario_de_la_peticion(peticion)
    if not u:
        raise HTTPException(401, "Inicia sesión para continuar.")
    if u.get("role") != "admin":
        raise HTTPException(403, "Solo un administrador puede hacer esto.")
    return u["id"]


def _poner_cookie(respuesta: Response, token: str) -> None:
    respuesta.set_cookie(
        COOKIE, token,
        max_age=DIAS_DE_SESION * 24 * 3600,
        httponly=True,        # que el JavaScript de la página no pueda leerla
        samesite="lax",       # no viaja en peticiones cross-site: frena el CSRF
        secure=COOKIE_SEGURA,
        path="/",
    )


# --------------------------------------------------------------------------- #
# Validación de entrada
# --------------------------------------------------------------------------- #
MAX_TEXTO_NOTA = 20000
MAX_NOMBRE_CARPETA = 80
_RE_COLOR_CARPETA = re.compile(r"^#[0-9A-Fa-f]{6}$")
_RE_COLOR_NOTA = re.compile(r"^[a-z]{1,20}$")


def _entero(valor, campo: str, minimo: int, maximo: int) -> int:
    if isinstance(valor, bool):
        valor = None
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    if isinstance(valor, str) and re.fullmatch(r"-?\d{1,9}", valor.strip()):
        valor = int(valor.strip())
    if not isinstance(valor, int):
        raise HTTPException(400, f"«{campo}» debe ser un número entero.")
    if not minimo <= valor <= maximo:
        raise HTTPException(400, f"«{campo}» debe estar entre {minimo} y {maximo}.")
    return valor


def _real(valor, campo: str, minimo: float, maximo: float) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        raise HTTPException(400, f"«{campo}» debe ser un número.")
    try:
        n = float(valor)
    except ValueError:
        raise HTTPException(400, f"«{campo}» debe ser un número.") from None
    if not math.isfinite(n) or not minimo <= n <= maximo:
        raise HTTPException(400, f"«{campo}» debe estar entre {minimo} y {maximo}.")
    return n


def _texto(valor, campo: str, maximo: int) -> str:
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise HTTPException(400, f"«{campo}» debe ser texto.")
    if len(valor) > maximo:
        raise HTTPException(400, f"«{campo}» es demasiado largo (máximo {maximo}).")
    return valor


def _sin_rutas(asset: dict) -> dict:
    """Un material tal y como sale hacia el navegador: sin su ruta en el disco
    del servidor, que no le sirve de nada a la página."""
    return {k: v for k, v in asset.items() if k != "path"}


def _tema_publico(topic: dict) -> dict:
    topic = dict(topic)
    topic["assets"] = {k: [_sin_rutas(a) for a in v] for k, v in topic["assets"].items()}
    return topic


def _temas(uid: int) -> list[dict]:
    return [_tema_publico(t) for t in db.list_topics(uid)]


def _ficha(uid: int) -> dict:
    u = db.get_user(uid) or {}
    return {k: u.get(k) for k in ("id", "username", "nombre", "email", "role")}


# --------------------------------------------------------------------------- #
# Alta, entrada y salida
# --------------------------------------------------------------------------- #
# Registro público con correo verificado. El orden importa: primero se pide
# SOLO el correo y se manda un enlace; el usuario y la contraseña se eligen
# DESPUÉS, al abrirlo. Si se pidiera la contraseña antes, alguien podría dar de
# alta el correo de otra persona con una contraseña suya y esperar a que la
# víctima pulse "confirmar" (pre-secuestro de cuentas).
#
# El token va en el FRAGMENTO del enlace (#t=...), que el navegador no manda al
# servidor: no queda en los logs ni en ningún proxy.
APP_NOMBRE = os.environ.get("APP_NOMBRE", "MedStudy").strip() or "MedStudy"
# La dirección pública de la app, para los enlaces de los correos. Se fija en la
# configuración, NUNCA se saca de la cabecera Host: si no, cualquiera podría
# pedir un enlace para tu correo con un Host suyo y el token iría a su servidor.
APP_URL = os.environ.get("APP_URL", "").strip().rstrip("/")
MINUTOS_ENLACE_ALTA = 60
MINUTOS_ENLACE_CLAVE = 30
# Topes por (correo, IP): 3 a la hora y 5 al día. Por IP y no solo por correo
# para que quien pida enlaces para TU correo desde su IP no te deje sin poder
# restablecer desde la tuya. Desde un aparato donde ya entraste bien (cookie de
# dispositivo de esa cuenta) este tope no aplica.
MAX_CORREOS_HORA_POR_CORREO = 3
MAX_CORREOS_DIA_POR_CORREO = 5
# Respaldo: nadie recibe más de 15 al día, vengan de las IPs que vengan.
MAX_CORREOS_DIA_POR_CORREO_TOTAL = 15
# Por debajo de los ~300 diarios del plan gratuito de Brevo. Las altas tienen su
# propio cupo, más bajo: un bot que pide altas de correos inventados no puede
# gastar los envíos que necesitan las cuentas que ya existen para restablecer.
MAX_CORREOS_DIA = int(os.environ.get("MAX_CORREOS_DIA", "250"))
MAX_ALTAS_DIA = int(os.environ.get("MAX_ALTAS_DIA", "150"))
RESPUESTA_GENERICA = ("Si esa dirección puede recibir correo, te enviamos un enlace. "
                      "Revisa tu bandeja de entrada (y la carpeta de spam).")


def _registro_disponible() -> bool:
    return bool(APP_URL) and correo.disponible()


def _plantilla(titulo: str, parrafo: str, boton: str, enlace: str, pie: str) -> tuple[str, str]:
    texto = f"{titulo}\n\n{parrafo}\n\n{boton}: {enlace}\n\n{pie}\n"
    e = html_mod.escape
    html = (
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:480px;'
        'margin:0 auto;padding:24px;color:#2a2723">'
        f'<h2 style="margin:0 0 12px">{e(titulo)}</h2>'
        f'<p style="line-height:1.5">{e(parrafo)}</p>'
        f'<p style="margin:24px 0"><a href="{e(enlace)}" style="background:#2f6b5e;color:#fff;'
        f'padding:12px 18px;border-radius:8px;text-decoration:none;font-weight:600">{e(boton)}</a></p>'
        f'<p style="color:#8a8378;font-size:13px;line-height:1.5">{e(pie)}</p></div>')
    return texto, html


def _grupo_ip(ip: str) -> str:
    """La IP (ya agrupada) resumida: para contar topes sin guardarla."""
    return hashlib.sha256(("ip:" + ip).encode("utf-8")).hexdigest()[:32]


def _mandar_enlace(email: str, proposito: str, ya_tiene_cuenta: bool = False,
                   usuario: str = "", ip: str = "", dispositivo_ok: bool = False) -> None:
    """Crea el token y manda el correo. 503 si hoy ya no se puede enviar más o
    si Brevo falla; nada (en silencio) si se pasó un tope por correo, porque la
    respuesta a quien lo pide es la misma en todos los casos."""
    if (db.contar_correos(86400) >= MAX_CORREOS_DIA
            or (proposito == "signup"
                and db.contar_correos(86400, proposito="signup") >= MAX_ALTAS_DIA)):
        raise HTTPException(503, "Hoy ya no podemos mandar más correos. Inténtalo mañana.")
    grupo = _grupo_ip(ip)
    if db.contar_correos(86400, email) >= MAX_CORREOS_DIA_POR_CORREO_TOTAL:
        return
    if not dispositivo_ok and (
            db.contar_correos(3600, email, ip_grupo=grupo) >= MAX_CORREOS_HORA_POR_CORREO
            or db.contar_correos(86400, email, ip_grupo=grupo) >= MAX_CORREOS_DIA_POR_CORREO):
        return
    nota_usuario = f"Tu usuario es {usuario}." if usuario else ""
    if proposito == "signup":
        token = db.crear_token_correo(email, "signup", MINUTOS_ENLACE_ALTA, grupo)
        enlace = f"{APP_URL}/create-account#t={token}"
        asunto = f"Confirma tu correo para {APP_NOMBRE}"
        texto, html = _plantilla(
            "Confirma tu correo",
            f"Abre este enlace para elegir tu usuario y tu contraseña en {APP_NOMBRE}. "
            f"Sirve una sola vez y caduca en {MINUTOS_ENLACE_ALTA} minutos.",
            "Crear mi cuenta", enlace,
            "Si no lo pediste, ignora este correo: no se creará nada.")
    else:
        token = db.crear_token_correo(email, "reset", MINUTOS_ENLACE_CLAVE, grupo)
        enlace = f"{APP_URL}/reset-password#t={token}"
        if ya_tiene_cuenta:
            asunto = f"Ya tienes una cuenta en {APP_NOMBRE}"
            texto, html = _plantilla(
                "Ya tienes una cuenta",
                "Alguien (esperamos que tú) intentó registrarse con este correo, pero ya "
                f"tiene una cuenta. {nota_usuario} Si olvidaste la contraseña, puedes poner "
                f"una nueva. El enlace caduca en {MINUTOS_ENLACE_CLAVE} minutos.",
                "Poner una contraseña nueva", enlace,
                "Si no fuiste tú, ignora este correo: tu cuenta sigue igual.")
        else:
            asunto = f"Restablece tu contraseña de {APP_NOMBRE}"
            texto, html = _plantilla(
                "Restablece tu contraseña",
                f"{nota_usuario} Abre este enlace para elegir una contraseña nueva. Sirve "
                f"una sola vez y caduca en {MINUTOS_ENLACE_CLAVE} minutos. Al cambiarla se "
                "cierran tus otras sesiones.",
                "Poner una contraseña nueva", enlace,
                "Si no lo pediste, ignora este correo: tu contraseña sigue igual.")
    try:
        correo.enviar(email, asunto, texto, html, APP_NOMBRE)
    except correo.CorreoNoDisponible:
        raise HTTPException(503, "No pudimos mandar el correo ahora. "
                                 "Inténtalo de nuevo en unos minutos.") from None


def _email_de(payload: dict) -> str:
    email = _texto(payload.get("email"), "email", 254).strip().lower()
    if not core.email_valido(email):
        raise HTTPException(400, "Ese correo no parece válido.")
    return email


def _entrar(respuesta: Response, uid: int) -> None:
    _poner_cookie(respuesta, db.create_session(uid, DIAS_DE_SESION))
    # Viaja a todo /api/auth/: el login la usa para sus topes y el registro y
    # el «olvidé mi contraseña» para no frenar a la dueña de la cuenta.
    respuesta.set_cookie(COOKIE_DISPOSITIVO, _cookie_dispositivo(uid),
                         max_age=DIAS_DISPOSITIVO * 86400, httponly=True,
                         samesite="lax", secure=COOKIE_SEGURA, path="/api/auth")


def _lanzar(funcion, *args) -> None:
    """Corre `funcion` en segundo plano. Las pruebas la sustituyen por una
    llamada directa para no tener que esperar al hilo."""
    threading.Thread(target=funcion, args=args, daemon=True).start()


def _mandar_en_segundo_plano(email: str, proposito: str, ya_tiene_cuenta: bool,
                             usuario: str, ip: str, dispositivo_ok: bool) -> None:
    try:
        _mandar_enlace(email, proposito, ya_tiene_cuenta=ya_tiene_cuenta, usuario=usuario,
                       ip=ip, dispositivo_ok=dispositivo_ok)
    except HTTPException as exc:
        _log.error("No se mandó un enlace por correo: %s", exc.detail)


def _cupo_global_lleno() -> bool:
    """El único tope que se contesta en la respuesta: es igual para todos, así
    que no dice nada de ninguna cuenta."""
    return db.contar_correos(86400) >= MAX_CORREOS_DIA


@app.post("/api/auth/register")
def registrarse(peticion: Request, payload: dict = Body(...)) -> dict:
    """Paso 1: solo el correo. La respuesta es la misma tenga cuenta o no."""
    if not _registro_disponible():
        raise HTTPException(503, "El registro no está disponible ahora.")
    ip = _ip(peticion)
    if _reservar_intento("alta", ip) is None:
        raise HTTPException(429, "Demasiadas peticiones. Inténtalo más tarde.")
    email = _email_de(payload)
    db.purgar_tokens_correo()
    if _cupo_global_lleno():
        raise HTTPException(503, "Hoy ya no podemos mandar más correos. Inténtalo mañana.")
    existente = db.find_user_by_email(email)
    # El envío va en segundo plano y la respuesta es siempre la misma: si se
    # esperara a Brevo solo en unos casos, el tiempo de respuesta diría qué
    # correos tienen cuenta (y cuáles están bloqueados o pendientes).
    if existente is None:
        _lanzar(_mandar_en_segundo_plano, email, "signup", False, "", ip, False)
    elif existente.get("status") == "active":
        _lanzar(_mandar_en_segundo_plano, email, "reset", True,
                existente.get("username") or "", ip, _dispositivo_de(peticion, existente))
    return {"ok": True, "message": RESPUESTA_GENERICA}


def _dispositivo_de(peticion: Request, fila: dict) -> bool:
    """¿Llega esta petición desde un aparato donde esa cuenta ya entró bien?"""
    return _dispositivo_conocido(peticion.cookies.get(COOKIE_DISPOSITIVO, ""), fila["id"])


@app.post("/api/auth/link")
def ver_enlace(peticion: Request, payload: dict = Body(...)) -> dict:
    """¿Sigue valiendo este enlace? Para avisar antes de rellenar el formulario.
    No lo gasta."""
    if _reservar_intento("enlace", _ip(peticion)) is None:
        raise HTTPException(429, "Demasiadas peticiones. Inténtalo más tarde.")
    proposito = payload.get("purpose")
    if proposito not in ("signup", "reset"):
        raise HTTPException(400, "Enlace no válido.")
    email = db.ver_token_correo(_texto(payload.get("token"), "token", 200), proposito)
    usuario = ""
    if email and proposito == "reset":     # quien tiene el enlace es el dueño del buzón
        usuario = (db.find_user_by_email(email) or {}).get("username") or ""
    return {"valid": bool(email), "email": email or "", "username": usuario}


@app.post("/api/auth/register/complete")
def completar_alta(peticion: Request, respuesta: Response, payload: dict = Body(...)) -> dict:
    """Paso 2: con el enlace del correo, elige usuario y contraseña. Entra directo."""
    if _reservar_intento("enlace", _ip(peticion)) is None:
        raise HTTPException(429, "Demasiadas peticiones. Inténtalo más tarde.")
    token = _texto(payload.get("token"), "token", 200)
    email = db.ver_token_correo(token, "signup")
    if not email:
        raise HTTPException(400, "Este enlace caducó o ya se usó. "
                                 "Pide uno nuevo desde la página de registro.")
    if db.find_user_by_email(email):
        raise HTTPException(400, "Ya hay una cuenta con este correo. "
                                 "Entra, o restablece tu contraseña.")
    try:
        # el rol NO sale de la petición: quien se registra es siempre 'user'
        uid = db.create_user(
            username=_texto(payload.get("username"), "username", 64),
            password=_texto(payload.get("password"), "password", core.LONGITUD_MAXIMA_CLAVE + 1),
            email=email, role="user", status="active",
            nombre=_texto(payload.get("nombre"), "nombre", 200))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    # gastar el enlace (y anular los demás de alta de ese correo) cierra la
    # puerta a reusarlo
    db.gastar_token_correo(token, "signup")
    db.anular_tokens_correo(email, "signup")
    _promover_si_es_admin_email(email)
    _entrar(respuesta, uid)
    return {"ok": True}


def _promover_si_es_admin_email(email: str) -> None:
    """El primer admin en producción, sin terminal: si la cuenta recién
    verificada tiene el correo de ADMIN_EMAIL y aún no hay admin, lo es."""
    objetivo = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    if objetivo and (email or "").strip().lower() == objetivo:
        uid = db.promover_admin_inicial(objetivo)
        if uid:
            _log.warning("ADMIN_EMAIL: la cuenta %d pasa a ser la administradora.", uid)


@app.post("/api/auth/password/forgot")
def olvide_mi_clave(peticion: Request, payload: dict = Body(...)) -> dict:
    """Manda el enlace de restablecer si hay una cuenta activa con ese correo.

    El envío va en segundo plano y la respuesta es siempre la misma: si se
    esperara a Brevo solo cuando la cuenta existe, el tiempo de respuesta
    diría qué correos están dados de alta.
    """
    if not _registro_disponible():
        raise HTTPException(503, "Restablecer la contraseña no está disponible ahora.")
    ip = _ip(peticion)
    if _reservar_intento("olvido", ip) is None:
        raise HTTPException(429, "Demasiadas peticiones. Inténtalo más tarde.")
    email = _email_de(payload)
    db.purgar_tokens_correo()
    if _cupo_global_lleno():
        raise HTTPException(503, "Hoy ya no podemos mandar más correos. Inténtalo mañana.")
    fila = db.find_user_by_email(email)
    if fila and fila.get("status") == "active":
        _lanzar(_mandar_en_segundo_plano, email, "reset", False,
                fila.get("username") or "", ip, _dispositivo_de(peticion, fila))
    return {"ok": True, "message": RESPUESTA_GENERICA}


@app.post("/api/auth/password/reset")
def restablecer_clave(peticion: Request, respuesta: Response, payload: dict = Body(...)) -> dict:
    """Con el enlace del correo, pone una contraseña nueva. Cierra todas las
    sesiones de esa cuenta y abre una nueva aquí."""
    if _reservar_intento("enlace", _ip(peticion)) is None:
        raise HTTPException(429, "Demasiadas peticiones. Inténtalo más tarde.")
    token = _texto(payload.get("token"), "token", 200)
    clave = _texto(payload.get("password"), "password", core.LONGITUD_MAXIMA_CLAVE + 1)
    caducado = "Este enlace caducó o ya se usó. Pide uno nuevo."
    email = db.ver_token_correo(token, "reset")
    fila = db.find_user_by_email(email) if email else None
    if not fila or fila.get("status") != "active":
        raise HTTPException(400, caducado)
    problema = core.problema_con_la_clave(clave)
    if problema:
        raise HTTPException(400, problema)
    if not db.gastar_token_correo(token, "reset"):
        raise HTTPException(400, caducado)
    db.set_password(fila["id"], clave)          # cierra todas sus sesiones
    db.anular_tokens_correo(email, "reset")
    _olvidar_intentos("login_cuenta", f"id:{fila['id']}")
    _entrar(respuesta, fila["id"])
    return {"ok": True}


@app.post("/api/auth/login")
def login(peticion: Request, respuesta: Response, payload: dict = Body(...)) -> dict:
    ip = _ip(peticion)
    login_pedido = payload.get("login")
    login_pedido = login_pedido.strip()[:200] if isinstance(login_pedido, str) else ""
    marca_ip = _reservar_intento("login", ip)
    if marca_ip is None:
        raise HTTPException(429, "Demasiados intentos. Inténtalo más tarde.")
    fila = db.find_user(login_pedido)
    # La cuenta se identifica por su id: alternar usuario y correo no duplica
    # los intentos. Desde un aparato donde ya entraste bien, tus intentos van
    # aparte y no los agota quien ataque tu cuenta.
    dispositivo = peticion.cookies.get(COOKIE_DISPOSITIVO, "")
    if fila and _dispositivo_conocido(dispositivo, fila["id"]):
        accion, quien = "login_dispositivo", dispositivo
    else:
        accion = "login_cuenta"
        quien = f"id:{fila['id']}" if fila else "login:" + login_pedido.lower()
    if _reservar_intento(accion, quien) is None:
        raise HTTPException(429, "Demasiados intentos. Inténtalo más tarde.")

    clave = payload.get("password")
    if not isinstance(clave, str) or len(clave) > core.LONGITUD_MAXIMA_CLAVE:
        clave = ""        # ninguna cuenta puede tenerla: se trata como incorrecta
    # se comprueba la contraseña AUNQUE el usuario no exista, contra un hash de
    # mentira: si no, el tiempo de respuesta delataría qué cuentas existen
    guardado = (fila or {}).get("password_hash") or "scrypt$16384$8$1$00$00"
    correcta = core.verify_password(clave, guardado)

    if not fila or not correcta:
        raise HTTPException(401, "Usuario, correo o contraseña incorrectos.")
    if fila["status"] == "pending":
        raise HTTPException(403, "Tu cuenta todavía está pendiente de aprobación.")
    if fila["status"] == "blocked":
        raise HTTPException(403, "Tu cuenta está bloqueada.")

    # Entró bien: no cuenta este intento de la IP, pero sí los fallos de antes.
    # Si se borraran, quien tenga una cuenta podría probar 9 claves ajenas,
    # entrar con la suya para limpiar el contador, y repetir sin fin.
    _devolver_intento("login", ip, marca_ip)
    _olvidar_intentos(accion, quien)
    _entrar(respuesta, fila["id"])
    db.purge_expired_sessions()
    return {"ok": True, "role": fila["role"]}


@app.post("/api/auth/logout")
def logout(peticion: Request, respuesta: Response) -> dict:
    db.delete_session(peticion.cookies.get(COOKIE, ""))
    respuesta.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def quien_soy(uid: int = Depends(current_user)) -> dict:
    return _ficha(uid)


@app.post("/api/auth/password")
def cambiar_mi_clave(respuesta: Response, payload: dict = Body(...),
                     uid: int = Depends(current_user)) -> dict:
    """Cambio de contraseña por el propio usuario. Pide la actual: una sesión
    robada no debe poder dejarte fuera de tu propia cuenta."""
    if _reservar_intento("login_cuenta", f"id:{uid}") is None:
        raise HTTPException(429, "Demasiados intentos. Inténtalo más tarde.")
    actual = _texto(payload.get("current"), "current", core.LONGITUD_MAXIMA_CLAVE + 1)
    nueva = _texto(payload.get("new"), "new", core.LONGITUD_MAXIMA_CLAVE + 1)
    fila = db.find_user((db.get_user(uid) or {}).get("username") or "")
    if not fila or not core.verify_password(actual, fila["password_hash"] or ""):
        raise HTTPException(403, "La contraseña actual no es correcta.")
    try:
        db.set_password(uid, nueva)            # cierra TODAS sus sesiones
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    # un enlace de restablecer pendiente ya no tiene sentido (y si lo pidió
    # otra persona, no debe poder usarse después del cambio)
    db.anular_tokens_correo(fila.get("email") or "", "reset")
    _olvidar_intentos("login_cuenta", f"id:{uid}")
    _poner_cookie(respuesta, db.create_session(uid, DIAS_DE_SESION))
    return {"ok": True, "mensaje": "Contraseña cambiada. Se cerraron tus otras sesiones."}


# --------------------------------------------------------------------------- #
# Panel de administración
# --------------------------------------------------------------------------- #
@app.get("/api/admin/users")
def admin_usuarios(uid: int = Depends(current_admin)) -> list[dict]:
    return db.list_users()


@app.post("/api/admin/users")
def admin_crear_usuario(payload: dict = Body(...), uid: int = Depends(current_admin)) -> dict:
    """Crea una cuenta, ya activa. La contraseña es temporal: se la pasas a esa
    persona y ella la cambia desde Ajustes."""
    try:
        nuevo = db.create_user(
            username=_texto(payload.get("username"), "username", 64),
            password=_texto(payload.get("password"), "password", core.LONGITUD_MAXIMA_CLAVE + 1),
            email=_texto(payload.get("email"), "email", 254),
            role="admin" if payload.get("role") == "admin" else "user",
            status="active",
            nombre=_texto(payload.get("nombre"), "nombre", 200))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True, "id": nuevo, "username": (db.get_user(nuevo) or {}).get("username")}


@app.post("/api/admin/users/{user_id}/status")
def admin_cambiar_estado(user_id: int, payload: dict = Body(...),
                         uid: int = Depends(current_admin)) -> dict:
    estado = payload.get("status")
    if estado not in ("active", "blocked"):
        raise HTTPException(400, "El estado debe ser 'active' o 'blocked'.")
    if user_id == uid and estado != "active":
        raise HTTPException(400, "No puedes bloquear tu propia cuenta.")
    objetivo = db.get_user(user_id)
    if not objetivo:
        raise HTTPException(404, "Ese usuario no existe.")
    # dejar la app sin ningún admin activo la volvería ingobernable
    if objetivo["role"] == "admin" and estado != "active" and db.count_admins() <= 1:
        raise HTTPException(400, "Es el único administrador activo.")
    db.set_user_status(user_id, estado)
    return {"ok": True, "status": estado}


@app.post("/api/admin/users/{user_id}/sesiones/cerrar")
def admin_cerrar_sesiones(user_id: int, uid: int = Depends(current_admin)) -> dict:
    """Echa a esa persona de todos sus dispositivos, sin tocar su contraseña."""
    if not db.get_user(user_id):
        raise HTTPException(404, "Ese usuario no existe.")
    return {"ok": True, "cerradas": db.cerrar_sesiones(user_id)}


@app.post("/api/admin/users/{user_id}/password")
def admin_resetear_clave(user_id: int, payload: dict = Body(...),
                         uid: int = Depends(current_admin)) -> dict:
    """Pone una contraseña temporal. Cierra las sesiones de esa persona."""
    if not db.get_user(user_id):
        raise HTTPException(404, "Ese usuario no existe.")
    try:
        db.set_password(user_id, _texto(payload.get("password"), "password",
                                        core.LONGITUD_MAXIMA_CLAVE + 1))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Páginas
# --------------------------------------------------------------------------- #
def _pagina(nombre: str) -> HTMLResponse:
    with open(os.path.join(STATIC_DIR, nombre), "r", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@app.get("/", response_class=HTMLResponse)
def index(peticion: Request):
    """La app si hay sesión; si no, a la pantalla de entrada."""
    if not usuario_de_la_peticion(peticion):
        return RedirectResponse("/entrar", status_code=303)
    return _pagina("index.html")


@app.get("/entrar", response_class=HTMLResponse)
def pagina_login() -> HTMLResponse:
    return _pagina("login.html")


@app.get("/signup", response_class=HTMLResponse)
@app.get("/create-account", response_class=HTMLResponse)
@app.get("/forgot-password", response_class=HTMLResponse)
@app.get("/reset-password", response_class=HTMLResponse)
def paginas_de_cuenta() -> HTMLResponse:
    """Una sola página para los cuatro pasos; cada uno se elige por la ruta."""
    return _pagina("cuenta.html")


@app.get("/admin", response_class=HTMLResponse)
def pagina_admin(peticion: Request):
    u = usuario_de_la_peticion(peticion)
    if not u:
        return RedirectResponse("/entrar", status_code=303)
    if u.get("role") != "admin":
        raise HTTPException(403, "Solo un administrador puede ver esta página.")
    return _pagina("admin.html")


# --------------------------------------------------------------------------- #
# Estado general
# --------------------------------------------------------------------------- #
@app.get("/api/stats")
def get_stats(uid: int = Depends(current_user)) -> dict:
    return db.stats(uid)


@app.get("/api/bootstrap")
def bootstrap(peticion: Request, uid: int = Depends(current_user)) -> dict:
    """Todo lo que la UI necesita para pintarse. Una llamada en vez de cinco."""
    es_admin = (usuario_de_la_peticion(peticion) or {}).get("role") == "admin"
    return {
        "user": _ficha(uid),
        "folders": db.list_folders(),
        "topics": _temas(uid),
        "orphans": [_sin_rutas(o) for o in db.list_orphans()] if es_admin else [],
        "stats": db.stats(uid),
        "settings": _settings_payload(uid),
        "pdf_ok": core.PdfRenderer.available(),
        # la carga de archivos solo existe en local (ver SUBIDAS_APAGADAS)
        "subidas": not SUBIDAS_APAGADAS,
        # la ingesta del arranque todavía corre: la biblioteca puede estar a medias
        "actualizando": _ingesta["en_curso"],
    }


# --------------------------------------------------------------------------- #
# Carpetas (organizan el material compartido: solo el admin las cambia)
# --------------------------------------------------------------------------- #
def _nombre_carpeta(payload: dict) -> str:
    name = _texto(payload.get("name"), "name", MAX_NOMBRE_CARPETA).strip()
    if not name:
        raise HTTPException(400, "La carpeta necesita un nombre.")
    return name


def _color_carpeta(payload: dict) -> str | None:
    color = payload.get("color")
    if color in (None, ""):
        return None
    if not isinstance(color, str) or not _RE_COLOR_CARPETA.match(color):
        raise HTTPException(400, "El color debe ser del tipo #2f6b5e.")
    return color


@app.get("/api/folders")
def list_folders(uid: int = Depends(current_user)) -> list[dict]:
    return db.list_folders()


@app.post("/api/folders")
def create_folder(payload: dict = Body(...), uid: int = Depends(current_admin)) -> dict:
    fid = db.ensure_folder(_nombre_carpeta(payload), _color_carpeta(payload) or "#c9a86a")
    return {"id": fid, "folders": db.list_folders()}


@app.post("/api/folders/{folder_id}")
def update_folder(folder_id: int, payload: dict = Body(...),
                  uid: int = Depends(current_admin)) -> dict:
    try:
        cambiada = db.rename_folder(folder_id, _nombre_carpeta(payload), _color_carpeta(payload))
    except ValueError as e:                     # ya hay otra con ese nombre
        raise HTTPException(400, str(e)) from None
    if not cambiada:
        raise HTTPException(404, "Carpeta no encontrada.")
    return {"folders": db.list_folders()}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int, uid: int = Depends(current_admin)) -> dict:
    """Borra la carpeta. Los temas que contenía NO se borran, quedan sueltos."""
    if not db.delete_folder(folder_id):
        raise HTTPException(404, "Carpeta no encontrada.")
    return {"folders": db.list_folders(), "topics": _temas(uid)}


@app.post("/api/topics/{topic_id}/folder")
def move_topic(topic_id: int, payload: dict = Body(...),
               uid: int = Depends(current_admin)) -> dict:
    folder_id = payload.get("folder_id")
    if folder_id in (None, "", 0):
        folder_id = None
    else:
        folder_id = _entero(folder_id, "folder_id", 1, 2 ** 31)
        if not db.folder_exists(folder_id):
            raise HTTPException(400, "Esa carpeta no existe.")
    if not db.move_topic(topic_id, folder_id):
        raise HTTPException(404, "Tema no encontrado.")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Temas
# --------------------------------------------------------------------------- #
@app.get("/api/topics")
def list_topics(uid: int = Depends(current_user)) -> list[dict]:
    return _temas(uid)


@app.get("/api/topics/{topic_id}")
def get_topic(topic_id: int, uid: int = Depends(current_user)) -> dict:
    topic = db.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Tema no encontrado.")
    resumen = (topic["assets"].get("resumen") or [None])[0]
    paginas = 0
    if resumen and core.ruta_permitida(resumen["path"]):
        paginas = core.PdfRenderer.page_count(resumen["path"])
    topic = _tema_publico(topic)
    topic["cards"] = db.topic_cards(topic_id)
    topic["notes"] = db.list_notes(uid, topic_id)
    topic["pages"] = paginas
    return topic


# --------------------------------------------------------------------------- #
# Ingesta del material
#
# Corre sola en cada arranque (el material va dentro de la imagen: lo que se
# publica con git aparece al desplegar) y cuando el admin re-escanea. Nunca
# dos a la vez: este candado es del proceso y, con Postgres, core toma además
# uno consultivo entre instancias (durante un despliegue conviven dos).
# --------------------------------------------------------------------------- #
_candado_ingesta = threading.Lock()
_ingesta = {"en_curso": False}


def _ingerir(origen: str) -> dict | None:
    """Corre la ingesta. None si ya había otra en curso en este proceso."""
    if not _candado_ingesta.acquire(blocking=False):
        return None
    _ingesta["en_curso"] = True
    try:
        with db.candado_global("ingesta", espera=30) as obtenido:
            if not obtenido:
                # Es idempotente: se sigue igual. Lo peor es un choque de
                # unicidad en una de las dos, que queda en el log.
                _log.warning("Ingesta (%s): otra instancia tiene el candado; se sigue igual.",
                             origen)
            inicio = time.perf_counter()
            reporte = core.ingest(db)
        reporte["segundos"] = round(time.perf_counter() - inicio, 2)
        _log.info("Ingesta (%s) en %.2f s: %d temas · %d fichas nuevas · %d materiales · "
                  "%d sin emparejar", origen, reporte["segundos"], reporte["temas"],
                  reporte["tarjetas_nuevas"], reporte["materiales"], reporte["huerfanos"])
        for err in reporte["errores"]:
            _log.warning("Ingesta: %s", err)
        return reporte
    finally:
        _ingesta["en_curso"] = False
        _candado_ingesta.release()


def _ingesta_de_arranque() -> None:
    try:
        _ingerir("arranque")
    except Exception as exc:
        # La app sigue con el material que ya tenía en la base; el error queda
        # en el log con su traza, no se traga.
        _log.exception("La ingesta del arranque falló: %s", exc)


@app.post("/api/ingest")
def run_ingest(uid: int = Depends(current_admin)) -> dict:
    """Re-escanea la carpeta de material. Siempre la misma: core.CONTENT_DIR
    (variable ESTUDIO_CONTENT_DIR). La petición no puede elegir otra."""
    reporte = _ingerir("admin")
    if reporte is None:
        raise HTTPException(409, "Ya se está actualizando el material. "
                                 "Espera unos segundos y vuelve a intentarlo.")
    return reporte


@app.get("/api/orphans")
def list_orphans(uid: int = Depends(current_admin)) -> list[dict]:
    return [_sin_rutas(o) for o in db.list_orphans()]


@app.post("/api/orphans/{orphan_id}/assign")
def assign_orphan(orphan_id: int, payload: dict = Body(...),
                  uid: int = Depends(current_admin)) -> dict:
    topic_id = _entero(payload.get("topic_id"), "topic_id", 1, 2 ** 31)
    if not db.get_topic(topic_id):
        raise HTTPException(400, "Tema inválido.")
    if not db.assign_orphan(orphan_id, topic_id):
        raise HTTPException(404, "Archivo no encontrado.")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Carga de archivos (solo admin; tope de 25 MB en LIMITES_POR_RUTA)
# --------------------------------------------------------------------------- #
@app.post("/api/upload")
async def upload(topic_id: int | None = None, file: UploadFile = File(...),
                 uid: int = Depends(current_admin)) -> dict:
    """
    Carga un archivo desde la app.

    Los PDF se copian a la biblioteca (son ligeros). Los archivos de fichas se
    importan a la base. Las presentaciones NO se aceptan por aquí: pesan
    demasiado; de ellas se sacan el resumen y las fichas.
    """
    if SUBIDAS_APAGADAS:               # el middleware ya lo corta; por si acaso
        raise HTTPException(409, MENSAJE_SUBIDAS_APAGADAS)
    data = await file.read()
    if len(data) > LIMITES_POR_RUTA["/api/upload"][0]:
        raise HTTPException(413, "El archivo supera los 25 MB.")
    name = file.filename or "archivo"
    ext = os.path.splitext(name)[1].lower()

    if ext == ".apkg":
        if not data.startswith(b"PK"):
            raise HTTPException(400, "Ese archivo no es un archivo de fichas válido.")
        if topic_id is not None and not db.get_topic(topic_id):
            raise HTTPException(400, "Ese tema no existe.")
        tmp = core.save_upload(name, data)
        try:
            deck = core.AnkiImporter.read(tmp)
        except core.AnkiFormatError as exc:
            os.remove(tmp)
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            os.remove(tmp)
            _log.warning("No se pudo leer un .apkg subido: %s", type(exc).__name__)
            raise HTTPException(400, "No se pudo leer el archivo de fichas.") from exc
        tid = topic_id or db.ensure_topic(deck["topic"])
        counts = db.upsert_cards(tid, deck["cards"])
        db.add_asset(tid, "anki", tmp, score=1.0, matched_by="manual")
        return {"ok": True, "topic_id": tid, "titulo": deck["topic"],
                "fichas_nuevas": counts["added"], "fichas_actualizadas": counts["updated"]}

    if ext == ".pdf":
        if not topic_id or not db.get_topic(topic_id):
            raise HTTPException(400, "Elige a qué tema pertenece el PDF.")
        if not data.startswith(b"%PDF-"):
            raise HTTPException(400, "Ese archivo no es un PDF válido.")
        path = core.save_upload(name, data)
        db.add_asset(topic_id, "resumen", path, score=1.0, matched_by="manual")
        return {"ok": True, "topic_id": topic_id, "archivo": os.path.basename(path)}

    raise HTTPException(
        400, "Solo se pueden cargar PDF y archivos de fichas (.apkg).")


# --------------------------------------------------------------------------- #
# Archivos y páginas del resumen
# --------------------------------------------------------------------------- #
_INLINE_TYPES = {"pdf": "application/pdf"}
MAX_PAGINA = 2000


def _asset_servible(asset_id: int) -> dict:
    """El material, solo si su archivo vive dentro de la carpeta de material o
    de la biblioteca. Si no, 404: no hay por qué decir dónde está."""
    asset = db.get_asset(asset_id)
    if not asset or not core.ruta_permitida(asset["path"]):
        raise HTTPException(404, "Material no encontrado.")
    if not os.path.isfile(asset["path"]):
        raise HTTPException(410, "El archivo de este material ya no está disponible.")
    return asset


@app.get("/api/assets/{asset_id}/file")
def asset_file(asset_id: int, uid: int = Depends(current_user)) -> FileResponse:
    asset = _asset_servible(asset_id)
    inline = asset["ext"] in _INLINE_TYPES
    return FileResponse(
        asset["path"], media_type=_INLINE_TYPES.get(asset["ext"], "application/octet-stream"),
        filename=asset["filename"],
        content_disposition_type="inline" if inline else "attachment",
        headers={"Cache-Control": "private, no-store"})


@app.get("/api/assets/{asset_id}/page/{page}")
def asset_page(asset_id: int, page: int, uid: int = Depends(current_user)) -> FileResponse:
    """Una página del resumen, ya renderizada a PNG. Sobre ella van los post-it."""
    if not 1 <= page <= MAX_PAGINA:
        raise HTTPException(400, "Página fuera de rango.")
    asset = _asset_servible(asset_id)
    if asset["ext"] != "pdf":
        raise HTTPException(400, "Solo se pueden renderizar páginas de un PDF.")
    # Solo las páginas que existen: pedir la 2000 de un PDF de 3 no debe
    # lanzar un pdftoppm para nada.
    if page > core.PdfRenderer.page_count(asset["path"]):
        raise HTTPException(404, "Esa página no existe.")
    png = core.PdfRenderer.render(asset["path"], page)
    if not png:
        raise HTTPException(503, "No se pudo renderizar la página.")
    # El material va detrás de sesión: que no quede en ninguna caché, ni en
    # la del navegador de un equipo compartido. El servidor sí guarda el PNG.
    return FileResponse(png, media_type="image/png",
                        headers={"Cache-Control": "private, no-store"})


# --------------------------------------------------------------------------- #
# Notas (privadas: cada persona ve y toca solo las suyas)
# --------------------------------------------------------------------------- #
@app.get("/api/topics/{topic_id}/notes")
def list_notes(topic_id: int, kind: str | None = None,
               uid: int = Depends(current_user)) -> list[dict]:
    if kind not in (None, "sticky", "general"):
        raise HTTPException(400, "La nota debe ser 'sticky' o 'general'.")
    return db.list_notes(uid, topic_id, kind)


@app.post("/api/topics/{topic_id}/notes")
def create_note(topic_id: int, payload: dict = Body(...),
                uid: int = Depends(current_user)) -> dict:
    topic = db.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Tema no encontrado.")
    kind = payload.get("kind") or "sticky"
    if kind not in ("sticky", "general"):
        raise HTTPException(400, "La nota debe ser 'sticky' o 'general'.")
    asset_id = payload.get("asset_id")
    if asset_id not in (None, ""):
        asset_id = _entero(asset_id, "asset_id", 1, 2 ** 31)
        # el post-it va pegado a un material DE ESTE tema, no a uno cualquiera
        if asset_id not in {a["id"] for lista in topic["assets"].values() for a in lista}:
            raise HTTPException(400, "Ese material no es de este tema.")
    else:
        asset_id = None
    color = payload.get("color") or "amarillo"
    if not isinstance(color, str) or not _RE_COLOR_NOTA.match(color):
        raise HTTPException(400, "Color de nota no válido.")
    nid = db.add_note(
        uid, topic_id, kind=kind, asset_id=asset_id,
        page=_entero(payload.get("page", 1), "page", 1, MAX_PAGINA),
        x=_real(payload.get("x", 0.5), "x", 0.0, 1.0),
        y=_real(payload.get("y", 0.5), "y", 0.0, 1.0),
        text=_texto(payload.get("text"), "text", MAX_TEXTO_NOTA), color=color)
    return {"id": nid, "notes": db.list_notes(uid, topic_id)}


@app.post("/api/notes/{note_id}")
def update_note(note_id: int, payload: dict = Body(...),
                uid: int = Depends(current_user)) -> dict:
    cambios: dict = {}
    if "text" in payload:
        cambios["text"] = _texto(payload["text"], "text", MAX_TEXTO_NOTA)
    if "x" in payload:
        cambios["x"] = _real(payload["x"], "x", 0.0, 1.0)
    if "y" in payload:
        cambios["y"] = _real(payload["y"], "y", 0.0, 1.0)
    if "page" in payload:
        cambios["page"] = _entero(payload["page"], "page", 1, MAX_PAGINA)
    if "color" in payload:
        if not isinstance(payload["color"], str) or not _RE_COLOR_NOTA.match(payload["color"]):
            raise HTTPException(400, "Color de nota no válido.")
        cambios["color"] = payload["color"]
    if not cambios:
        raise HTTPException(400, "No hay cambios válidos.")
    if not db.update_note(uid, note_id, **cambios):
        raise HTTPException(404, "Nota no encontrada.")
    return {"ok": True}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int, uid: int = Depends(current_user)) -> dict:
    if not db.delete_note(uid, note_id):
        raise HTTPException(404, "Nota no encontrada.")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Repaso (el progreso es de cada persona)
# --------------------------------------------------------------------------- #
@app.get("/api/queue")
def get_queue(topic_id: int | None = None, folder_id: int | None = None,
              limit: int = 60, mode: str = "normal",
              uid: int = Depends(current_user)) -> dict:
    """
    modo 'normal' — lo que toca hoy, respetando los topes diarios.
    modo 'libre'  — repaso extra: cualquier ficha, barajada, sin tocar la
                    programación. Para volver a pasar algo el mismo día.
    """
    if mode not in ("normal", "libre"):
        raise HTTPException(400, "El modo debe ser 'normal' o 'libre'.")
    cards = db.build_queue(uid, topic_id, limit=max(1, min(limit, 200)),
                           folder_id=folder_id, mode=mode)
    retention = db.retention(uid)
    for card in cards:
        card["intervals"] = core.preview_intervals(card, retention)
    return {"cards": cards, "stats": db.stats(uid), "mode": mode,
            "scope": db.scope_counts(uid, topic_id, folder_id)}


@app.post("/api/cards/{card_id}/grade")
def grade_card(card_id: int, payload: dict = Body(...),
               uid: int = Depends(current_user)) -> dict:
    rating = _entero(payload.get("rating"), "rating", 1, 4)
    mode = payload.get("mode") or "normal"
    if mode not in ("normal", "libre"):
        raise HTTPException(400, "El modo debe ser 'normal' o 'libre'.")

    # En repaso libre no se guarda nada: ni el estado de la ficha ni el
    # registro del repaso. Si se guardara, una pasada extra consumiría el cupo
    # diario y adelantaría fechas que ya estaban bien calculadas.
    if mode == "libre":
        if not db.get_card(card_id, uid):
            raise HTTPException(404, "Ficha no encontrada.")
        return {"ok": True, "result": None, "stats": db.stats(uid), "mode": "libre"}

    result = db.grade(uid, card_id, rating)
    if not result:
        raise HTTPException(404, "Ficha no encontrada.")
    return {"ok": True, "result": result, "stats": db.stats(uid)}


@app.post("/api/topics/{topic_id}/export")
def export_topic(topic_id: int, uid: int = Depends(current_user)) -> FileResponse:
    """Descarga un .apkg con las fichas del tema, para llevárselas a Anki.

    Un archivo propio por petición, que se borra al terminar de enviarlo: con
    una ruta fija, dos personas exportando a la vez podían pisarse.
    """
    topic = db.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Tema no encontrado.")
    cards = [c for c in db.topic_cards(topic_id) if c["kind"] == "basic"]
    if not cards:
        raise HTTPException(400, "El tema no tiene fichas básicas que exportar.")
    fd, out = tempfile.mkstemp(prefix="medstudy-", suffix=".apkg")
    os.close(fd)
    try:
        core.AnkiExporter.export(topic["title"], cards, out)
    except Exception:
        os.unlink(out)
        raise
    return FileResponse(out, media_type="application/octet-stream",
                        filename=f"{core.slugify(topic['title'])}.apkg",
                        background=BackgroundTask(os.unlink, out))


# --------------------------------------------------------------------------- #
# Ajustes de estudio (de cada persona)
# --------------------------------------------------------------------------- #
def _settings_payload(uid: int) -> dict:
    return {
        "new_limit": db.get_int_user_setting(uid, "new_limit", core.DEFAULT_NEW_LIMIT),
        "review_limit": db.get_int_user_setting(uid, "review_limit", core.DEFAULT_REVIEW_LIMIT),
        "retention": db.retention(uid),
        "theme": db.get_user_setting(uid, "theme", "auto"),
    }


@app.get("/api/settings")
def get_settings(uid: int = Depends(current_user)) -> dict:
    return _settings_payload(uid)


@app.post("/api/settings")
def set_settings(payload: dict = Body(...), uid: int = Depends(current_user)) -> dict:
    # Se valida todo ANTES de guardar nada: un campo malo no deja los demás a medias.
    cambios: dict = {}
    if "new_limit" in payload:
        cambios["new_limit"] = _entero(payload["new_limit"], "new_limit", 0, 1000)
    if "review_limit" in payload:
        cambios["review_limit"] = _entero(payload["review_limit"], "review_limit", 0, 10000)
    if "retention" in payload:
        # Fuera de 0.70–0.97 el algoritmo se vuelve inútil: por debajo se olvida
        # todo, por encima los intervalos se hacen absurdamente cortos.
        cambios["retention"] = round(core._clamp(
            _real(payload["retention"], "retention", 0.0, 1.0), 0.70, 0.97), 2)
    if "theme" in payload:
        if payload["theme"] not in ("auto", "claro", "oscuro"):
            raise HTTPException(400, "El tema debe ser 'auto', 'claro' u 'oscuro'.")
        cambios["theme"] = payload["theme"]
    for clave, valor in cambios.items():
        db.set_user_setting(uid, clave, valor)
    return _settings_payload(uid)


# Estilos y scripts de las páginas. No llevan datos de nadie: se sirven sin
# sesión porque la pantalla de entrada los necesita.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def arrancar(configurar_logs: bool = True) -> None:
    """Lo que se hace al arrancar el servidor (ver _ciclo_de_vida)."""
    if configurar_logs and not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Lo que pasó al abrir la base (al importar, antes de que hubiera logs).
    for aviso in db.avisos:
        _log.info("Base: %s", aviso)
    db.avisos.clear()
    _log.info("Zona horaria de la app: %s (hoy es %s).", core.APP_TZ, core.hoy().isoformat())
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    if admin_email:
        uid = db.promover_admin_inicial(admin_email)
        if uid:
            _log.warning("ADMIN_EMAIL: la cuenta %d pasa a ser la administradora.", uid)
    if not db.hay_admin():
        if admin_email:
            _log.warning("Todavía no hay administrador: lo será la cuenta que se registre "
                         "con el correo de ADMIN_EMAIL.")
        else:
            _log.warning("Todavía no hay ningún administrador. En local: python crear_admin.py. "
                         "Desplegado: define ADMIN_EMAIL y regístrate con ese correo.")
    if not _registro_disponible():
        _log.warning("El registro por correo está apagado (falta APP_URL o el envío de "
                     "correos). Las cuentas se crean desde /admin.")
    if not core.PdfRenderer.available():
        _log.warning("Falta poppler: los resúmenes no se podrán anotar "
                     "(brew install poppler, o poppler-utils en Linux).")
    # Medido: ~0,2 s con SQLite y el material actual (38 temas, 1940 fichas);
    # contra Postgres cada consulta es un viaje de red y la primera vez son
    # miles. En segundo plano, para que /entrar (la comprobación de salud de
    # Render) responda en cuanto el servidor escucha.
    _lanzar(_ingesta_de_arranque)


def main() -> None:
    # En local se escucha solo en el propio equipo (127.0.0.1), que es lo
    # seguro. HOST solo se cambia a propósito, al desplegar detrás de un proxy.
    puerto = int(os.environ.get("PORT", "8090"))
    host = os.environ.get("HOST") or "127.0.0.1"
    print(f"\n  {APP_NOMBRE} →  http://localhost:{puerto}\n", flush=True)
    # proxy_headers=False: la IP la decide _ip(), nunca X-Forwarded-For.
    # La ingesta y los avisos los hace el arranque (arrancar), también aquí.
    uvicorn.run(app, host=host, port=puerto, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
