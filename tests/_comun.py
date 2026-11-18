"""Preparación común de las pruebas: base SQLite TEMPORAL, carpetas temporales
y sin red (adaptado de EnglishApp).

Se importa ANTES que core y webapp: fija el entorno para que nada toque
database.db, contenido/, biblioteca/ ni la caché de páginas, y para que los
correos queden en una bandeja en memoria en vez de mandarse.
"""
import atexit
import os
import shutil
import sys
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="medstudy-pruebas-")
atexit.register(shutil.rmtree, TMP, True)

CONTENIDO = os.path.join(TMP, "contenido")
BIBLIOTECA = os.path.join(TMP, "biblioteca")
os.makedirs(CONTENIDO)

os.environ["APP_DB_PATH"] = os.path.join(TMP, "pruebas.db")
os.environ["ESTUDIO_CONTENT_DIR"] = CONTENIDO
os.environ.pop("DATABASE_URL", None)          # jamás una base de red en las pruebas
os.environ.pop("IP_DE_CABECERA", None)
os.environ.pop("BREVO_API_KEY", None)
os.environ.pop("COOKIE_SEGURA", None)
os.environ.pop("DOCS", None)
os.environ.pop("RENDER", None)
os.environ.pop("SECRETO_APP", None)
os.environ["CORREO_MODO"] = "memoria"
# http y no https: con https la app se cree desplegada y exige la
# configuración de producción (ver webapp.problema_de_produccion).
os.environ["APP_URL"] = "http://app.ejemplo.test"
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import core  # noqa: E402

# Por si core ya se hubiera importado antes con otro entorno.
core.CONTENT_DIR = CONTENIDO
core.LIBRARY_DIR = BIBLIOTECA
core.PAGE_CACHE_DIR = os.path.join(TMP, "paginas")
core.EXPORT_DIR = os.path.join(TMP, "exports")

from fastapi.testclient import TestClient  # noqa: E402,F401

import correo  # noqa: E402,F401
import webapp  # noqa: E402,F401

assert os.path.realpath(webapp.db.conn.execute("PRAGMA database_list").fetchone()["file"]) \
    == os.path.realpath(os.environ["APP_DB_PATH"]), "las pruebas no usan la base temporal"

CLAVE = "clave-de-prueba-123"

# Un PDF de una página, válido y mínimo (para servirlo y renderizarlo).
PDF_MINIMO = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n")


def usuario(nombre: str, rol: str = "user") -> int:
    if not webapp.db.find_user(nombre):
        webapp.db.create_user(username=nombre, password=CLAVE, role=rol, status="active")
    fila = webapp.db.find_user(nombre)
    if fila["status"] != "active":
        webapp.db.set_user_status(fila["id"], "active")
    return fila["id"]


def cliente(nombre: str | None = None, ip: str | None = None) -> TestClient:
    c = TestClient(webapp.app)
    if nombre:
        cab = {"true-client-ip": ip} if ip else {}
        r = c.post("/api/auth/login", json={"login": nombre, "password": CLAVE}, headers=cab)
        assert r.status_code == 200, r.text
    return c


def tema_con_fichas(titulo: str = "Vértigo", n: int = 5) -> tuple[int, list[int]]:
    """Un tema con n fichas básicas y un resumen PDF dentro de la carpeta de
    material. Idempotente: devuelve siempre los mismos ids para el mismo título."""
    tid = webapp.db.ensure_topic(titulo)
    slug = core.slugify(titulo)
    webapp.db.upsert_cards(tid, [
        {"kind": "basic", "front": f"Pregunta {i} de {titulo}", "back": f"Respuesta {i}",
         "anki_guid": f"{slug}-{i}", "cloze_index": 0} for i in range(n)])
    carpeta = os.path.join(CONTENIDO, "Neuro", "Resumenes PDF")
    os.makedirs(carpeta, exist_ok=True)
    pdf = os.path.join(carpeta, f"{slug} - Resumen.pdf")
    if not os.path.exists(pdf):
        with open(pdf, "wb") as fh:
            fh.write(PDF_MINIMO)
    webapp.db.add_asset(tid, "resumen", pdf)
    ids = [c["id"] for c in webapp.db.topic_cards(tid)]
    return tid, ids


def reiniciar_topes() -> None:
    webapp._intentos.clear()


def correo_sincrono():
    """Hace que los correos se manden en el mismo hilo (para no esperar).
    Devuelve la función original, para restaurarla."""
    original = webapp._lanzar
    webapp._lanzar = lambda funcion, *args: funcion(*args)
    return original
