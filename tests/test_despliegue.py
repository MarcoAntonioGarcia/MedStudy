"""Etapa 2: lo que hace falta para correr en Render (gratis) + Neon.

  * Rutas de los materiales RELATIVAS a la carpeta de material: la misma base
    la leen contenedores distintos en cada despliegue.
  * Ingesta en cada arranque, en segundo plano: lo publicado con git aparece
    solo, con su marca «Nuevo»/«Actualizado», y la comprobación de salud no
    espera a la ingesta.
  * En producción no se cargan archivos desde la app (el disco se borra).
  * El primer admin sale de ADMIN_EMAIL, sin terminal y sin contraseña en el
    entorno.
  * Dockerfile, .dockerignore, render.yaml y requirements-web.txt coherentes
    con la app (la imagen no se construye aquí: hace falta Docker).
"""
import ast
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import genanki

from _comun import (CLAVE, PDF_MINIMO, RAIZ, TMP, TestClient, cliente, core,
                    correo, correo_sincrono, tema_con_fichas, usuario, webapp)


def _mazo(ruta, titulo, n=2):
    modelo = genanki.Model(1607392319333, "Basico despliegue",
                           fields=[{"name": "F"}, {"name": "B"}],
                           templates=[{"name": "T", "qfmt": "{{F}}", "afmt": "{{B}}"}])
    mazo = genanki.Deck(2059400117777, f"Neurología::{titulo}")
    for i in range(n):
        mazo.add_note(genanki.Note(model=modelo, fields=[f"{titulo} P{i}", "R"],
                                   guid=f"desp-{titulo}-{i}"))
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    genanki.Package(mazo).write_to_file(ruta)


def _pdf(ruta):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "wb") as fh:
        fh.write(PDF_MINIMO)


def _material(raiz):
    _mazo(os.path.join(raiz, "Neuro", "Anki", "Vertigo.apkg"), "Vértigo")
    _pdf(os.path.join(raiz, "Neuro", "Resumenes PDF", "Vertigo - Resumen.pdf"))


class BaseAparte(unittest.TestCase):
    """Una base y una carpeta de material propias, puestas en webapp mientras
    dura la prueba (las del resto de la batería no se tocan)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.raiz = os.path.join(self.dir, "contenido")
        os.makedirs(self.raiz)
        self._db, self._contenido = webapp.db, core.CONTENT_DIR
        self.db = webapp.db = core.DatabaseManager(os.path.join(self.dir, "aparte.db"))
        core.CONTENT_DIR = self.raiz
        self._lanzar = correo_sincrono()
        webapp._intentos.clear()

    def tearDown(self):
        webapp.db.conn.close()
        webapp.db, core.CONTENT_DIR = self._db, self._contenido
        webapp._lanzar = self._lanzar
        webapp._intentos.clear()
        shutil.rmtree(self.dir, True)

    def crudas(self, tabla="assets"):
        with self.db._lock:
            return [r["path"] for r in self.db.conn.execute(f"SELECT path FROM {tabla}")]


# --------------------------------------------------------------------------- #
class RutasRelativas(BaseAparte):
    def test_se_guardan_relativas_y_se_resuelven_aqui(self):
        _material(self.raiz)
        core.ingest(self.db)
        self.assertEqual(sorted(self.crudas()), ["Neuro/Anki/Vertigo.apkg",
                                                 "Neuro/Resumenes PDF/Vertigo - Resumen.pdf"])
        tema = self.db.get_topic(self.db.topics_basic()[0]["id"])
        ruta = tema["assets"]["resumen"][0]["path"]
        self.assertEqual(ruta, os.path.join(self.raiz, "Neuro", "Resumenes PDF",
                                            "Vertigo - Resumen.pdf"))
        self.assertTrue(core.ruta_permitida(ruta))

    def test_otro_contenedor_otra_raiz(self):
        """La misma base leída desde otra carpeta (el Mac y /app en la imagen):
        los materiales siguen valiendo y re-escanear no duplica nada."""
        _material(self.raiz)
        core.ingest(self.db)
        antes = (self.db.count_topics(), sorted(self.crudas()))
        otra = os.path.join(self.dir, "imagen", "app", "contenido")
        shutil.copytree(self.raiz, otra)
        core.CONTENT_DIR = otra
        core.ingest(self.db)
        self.assertEqual((self.db.count_topics(), sorted(self.crudas())), antes)
        for lista in self.db.get_topic(self.db.topics_basic()[0]["id"])["assets"].values():
            for a in lista:
                self.assertTrue(a["path"].startswith(otra + os.sep), a["path"])
                self.assertTrue(os.path.isfile(a["path"]))

    def test_biblioteca_con_prefijo(self):
        dentro = os.path.join(core.LIBRARY_DIR, "Vertigo (2).pdf")
        self.assertEqual(core.ruta_guardada(dentro), "biblioteca:Vertigo (2).pdf")
        self.assertEqual(core.ruta_real("biblioteca:Vertigo (2).pdf"), dentro)
        self.assertEqual(core.ruta_guardada("biblioteca:x.pdf"), "biblioteca:x.pdf")
        self.assertEqual(core.ruta_guardada("Neuro/x.pdf"), "Neuro/x.pdf")

    def test_lo_de_fuera_se_guarda_tal_cual_y_no_se_sirve(self):
        for fuera in ("/etc/hosts", os.path.join(self.raiz, "..", "secreto.pdf")):
            self.assertTrue(os.path.isabs(core.ruta_guardada(fuera)), fuera)
            self.assertFalse(core.ruta_permitida(core.ruta_real(core.ruta_guardada(fuera))))
        # una ruta guardada con '..' tampoco saca nada de la carpeta
        self.assertFalse(core.ruta_permitida(core.ruta_real("../secreto.pdf")))

    def test_carga_local_va_a_la_biblioteca(self):
        usuario("jefa", "admin")
        tid = self.db.ensure_topic("Vértigo")
        r = cliente("jefa").post(f"/api/upload?topic_id={tid}",
                                 files={"file": ("resumen.pdf", PDF_MINIMO, "application/pdf")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(any(p.startswith(core.PREFIJO_BIBLIOTECA) for p in self.crudas()))

    def test_migra_una_base_con_rutas_absolutas(self):
        _material(self.raiz)
        ruta_db = os.path.join(self.dir, "vieja.db")
        db = core.DatabaseManager(ruta_db)
        tid = db.ensure_topic("Vértigo")
        resumen = os.path.join(self.raiz, "Neuro", "Resumenes PDF", "Vertigo - Resumen.pdf")
        uid = db.create_user("ana", CLAVE)
        with db._lock:
            db.conn.executemany(
                "INSERT INTO assets (topic_id, kind, path, filename, ext) VALUES (?, ?, ?, 'f', 'pdf')",
                [(tid, "resumen", resumen),                                   # de esta máquina
                 (tid, "anki", "/Users/otra/AppEstudio/contenido/Neuro/Anki/Vertigo.apkg"),
                 (tid, "articulo", "/Users/otra/AppEstudio/contenido/Neuro/No existe.pdf"),
                 (tid, "articulo", "/srv/ajeno/a.pdf")])                       # no se reubica
            db.conn.commit()
        aid = db.get_topic(tid)["assets"]["resumen"][0]["id"]
        db.add_note(uid, tid, asset_id=aid, text="pegada")
        db.conn.close()
        for _ in range(2):                                    # dos arranques: idempotente
            db = core.DatabaseManager(ruta_db)
            with db._lock:
                rutas = sorted(r["path"] for r in db.conn.execute("SELECT path FROM assets"))
            db.conn.close()
            self.assertEqual(rutas, ["/Users/otra/AppEstudio/contenido/Neuro/No existe.pdf",
                                     "/srv/ajeno/a.pdf", "Neuro/Anki/Vertigo.apkg",
                                     "Neuro/Resumenes PDF/Vertigo - Resumen.pdf"])
        # la ingesta reconoce los migrados (no crea otra fila) y la nota sigue
        db = core.DatabaseManager(ruta_db)
        core.ingest(db)
        with db._lock:
            n = db.conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"]
        self.assertEqual(n, 4)
        self.assertEqual([x["asset_id"] for x in db.list_notes(uid, tid)], [aid])
        db.conn.close()


# --------------------------------------------------------------------------- #
class Arranque(BaseAparte):
    def test_al_arrancar_se_ingiere_el_material(self):
        _material(self.raiz)
        webapp.arrancar(configurar_logs=False)
        self.assertEqual(self.db.count_topics(), 1)
        self.assertEqual(len(self.db.topic_cards(self.db.topics_basic()[0]["id"])), 2)
        webapp.arrancar(configurar_logs=False)                 # cada arranque: idempotente
        self.assertEqual(self.db.count_topics(), 1)
        self.assertFalse(webapp._ingesta["en_curso"])

    def test_la_hora_de_partida_y_la_de_la_base_son_comparables(self):
        """baseline_at la pone Python y created_at la pone la base: tienen que
        estar en la misma zona (UTC) o las marcas «Nuevo» saldrían mal."""
        _material(self.raiz)
        webapp.arrancar(configurar_logs=False)
        partida = self.db.get_setting("baseline_at")
        with self.db._lock:
            creado = self.db.conn.execute("SELECT MAX(created_at) AS m FROM assets").fetchone()["m"]
        import datetime
        a, b = (datetime.datetime.fromisoformat(x) for x in (partida, creado))
        self.assertLess(abs((a - b).total_seconds()), 5, (partida, creado))

    def test_lo_publicado_despues_se_marca(self):
        _material(self.raiz)
        _mazo(os.path.join(self.raiz, "Neuro", "Anki", "Cefalea.apkg"), "Cefalea")
        webapp.arrancar(configurar_logs=False)
        # La biblioteca de partida ya estaba de antes del despliegue
        with self.db._lock:
            for tabla in ("topics", "assets"):
                self.db.conn.execute(f"UPDATE {tabla} SET created_at = '2001-01-01 00:00:00'")
            self.db.conn.commit()
        self.db.set_setting("baseline_at", "2001-01-02 00:00:00")
        # Llega con git: un tema nuevo y un artículo para un tema que ya existía
        _mazo(os.path.join(self.raiz, "Neuro", "Anki", "Mareo.apkg"), "Mareo")
        _pdf(os.path.join(self.raiz, "Neuro", "Articulos", "Vertigo - articulo.pdf"))
        webapp.arrancar(configurar_logs=False)
        uid = usuario("ana")
        temas = {t["title"]: t for t in self.db.list_topics(uid)}
        self.assertTrue(temas["Mareo"]["is_new"])
        self.assertTrue(temas["Vértigo"]["is_updated"])
        self.assertFalse(temas["Cefalea"]["is_new"] or temas["Cefalea"]["is_updated"])

    def test_la_salud_no_espera_a_la_ingesta(self):
        _material(self.raiz)
        usuario("jefa", "admin")
        webapp._lanzar = self._lanzar                  # el hilo de verdad
        puede_seguir, empezo = threading.Event(), threading.Event()
        original = core.ingest

        def lenta(db, *a, **k):
            empezo.set()
            puede_seguir.wait(10)
            return original(db, *a, **k)
        core.ingest = lenta
        raiz_log = logging.getLogger()
        nulo = logging.NullHandler()
        raiz_log.addHandler(nulo)                      # que el arranque no configure logs
        try:
            with TestClient(webapp.app) as c:          # 'with' corre el arranque (lifespan)
                self.assertTrue(empezo.wait(5))
                inicio = time.perf_counter()
                self.assertEqual(c.get("/entrar").status_code, 200)
                self.assertLess(time.perf_counter() - inicio, 1.0)
                jefa = cliente("jefa")
                self.assertTrue(jefa.get("/api/bootstrap").json()["actualizando"])
                r = jefa.post("/api/ingest")
                self.assertEqual(r.status_code, 409)
                self.assertIn("actualizando", r.json()["detail"])
                puede_seguir.set()
                limite = time.monotonic() + 10
                while webapp._ingesta["en_curso"] and time.monotonic() < limite:
                    time.sleep(0.05)
                self.assertFalse(jefa.get("/api/bootstrap").json()["actualizando"])
                self.assertEqual(self.db.count_topics(), 1)
        finally:
            puede_seguir.set()
            core.ingest = original
            raiz_log.removeHandler(nulo)

    def test_si_la_ingesta_falla_queda_en_el_log_y_la_app_sigue(self):
        original = core.ingest

        def rota(*a, **k):
            raise RuntimeError("archivo roto")
        core.ingest = rota
        try:
            with self.assertLogs("estudio.web", level="ERROR") as log:
                webapp.arrancar(configurar_logs=False)
        finally:
            core.ingest = original
        self.assertTrue(any("falló" in l and "archivo roto" in l for l in log.output))
        self.assertFalse(webapp._ingesta["en_curso"])
        self.assertTrue(webapp._candado_ingesta.acquire(blocking=False))   # quedó libre
        webapp._candado_ingesta.release()


# --------------------------------------------------------------------------- #
class SubidasEnProduccion(unittest.TestCase):
    def setUp(self):
        webapp._intentos.clear()
        usuario("admin.prueba", "admin")
        usuario("ana")
        self.tid, _ = tema_con_fichas()
        webapp.SUBIDAS_APAGADAS = True

    def tearDown(self):
        webapp.SUBIDAS_APAGADAS = False
        webapp._intentos.clear()

    def test_la_carga_se_niega_con_un_mensaje_claro(self):
        pdf = {"file": ("r.pdf", PDF_MINIMO, "application/pdf")}
        r = cliente("admin.prueba").post(f"/api/upload?topic_id={self.tid}", files=pdf)
        self.assertEqual(r.status_code, 409)
        self.assertIn("En producción el material se agrega con git", r.json()["detail"])
        self.assertEqual(cliente("ana").post("/api/upload", files=pdf).status_code, 403)
        self.assertEqual(cliente().post("/api/upload", files=pdf).status_code, 401)

    def test_se_niega_sin_leer_el_archivo(self):
        grande = b"%PDF-" + b"0" * (26 * 1024 * 1024)        # por encima de los 25 MB
        r = cliente("admin.prueba").post(f"/api/upload?topic_id={self.tid}",
                                         files={"file": ("g.pdf", grande, "application/pdf")})
        self.assertEqual(r.status_code, 409)                  # no 413: ni lo miró

    def test_la_interfaz_no_ofrece_el_boton(self):
        self.assertFalse(cliente("admin.prueba").get("/api/bootstrap").json()["subidas"])
        webapp.SUBIDAS_APAGADAS = False
        self.assertTrue(cliente("admin.prueba").get("/api/bootstrap").json()["subidas"])
        html = open(os.path.join(RAIZ, "static", "index.html"), encoding="utf-8").read()
        js = open(os.path.join(RAIZ, "static", "app.js"), encoding="utf-8").read()
        self.assertIn('id="uploadBox"', html)
        self.assertIn("$('#uploadBox').hidden = !(S.admin && S.subidas)", js)

    def test_desplegado_se_apaga_solo(self):
        entorno = {k: v for k, v in os.environ.items()
                   if k not in ("DATABASE_URL", "APP_PG_SCHEMA")}
        entorno.update({"RENDER": "true", "COOKIE_SEGURA": "1", "IP_DE_CABECERA": "true-client-ip",
                        "SECRETO_APP": "s" * 32, "DATABASE_URL": "postgresql://nadie@127.0.0.1:1/x"})
        codigo = ("import core\n"
                  "core.DatabaseManager = lambda *a, **k: None\n"
                  "import webapp\n"
                  "print(webapp.SUBIDAS_APAGADAS, webapp.EN_PRODUCCION)\n")
        r = subprocess.run([sys.executable, "-c", codigo], cwd=RAIZ, env=entorno,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["True", "True"])


# --------------------------------------------------------------------------- #
class AdminDesdeElEntorno(BaseAparte):
    def setUp(self):
        super().setUp()
        correo.BANDEJA_PRUEBAS.clear()
        os.environ["ADMIN_EMAIL"] = "Jefa@Ejemplo.com"

    def tearDown(self):
        os.environ.pop("ADMIN_EMAIL", None)
        super().tearDown()

    def alta(self, email, usuario_nuevo):
        c = TestClient(webapp.app)
        self.assertEqual(c.post("/api/auth/register", json={"email": email}).status_code, 200)
        m = re.search(r"/create-account#t=([A-Za-z0-9_\-]+)", correo.BANDEJA_PRUEBAS[-1]["text"])
        r = c.post("/api/auth/register/complete",
                   json={"token": m.group(1), "username": usuario_nuevo, "password": CLAVE})
        self.assertEqual(r.status_code, 200, r.text)
        return c.get("/api/auth/me").json()["role"]

    def test_la_cuenta_con_ese_correo_es_el_primer_admin(self):
        self.assertEqual(self.alta("otra@ejemplo.com", "otra"), "user")
        self.assertEqual(self.alta("jefa@ejemplo.com", "jefa"), "admin")     # sin mayúsculas
        self.assertEqual(self.db.count_admins(), 1)

    def test_si_ya_hay_admin_no_cambia_nada(self):
        self.db.create_user("vieja", CLAVE, role="admin")
        self.assertEqual(self.alta("jefa@ejemplo.com", "jefa"), "user")

    def test_solo_el_correo_exacto(self):
        self.assertEqual(self.alta("jefa@ejemplo.com.mx", "casi"), "user")
        self.assertEqual(self.alta("xjefa@ejemplo.com", "casi2"), "user")
        self.assertFalse(self.db.hay_admin())

    def test_sin_la_variable_nadie_asciende(self):
        os.environ.pop("ADMIN_EMAIL")
        self.assertEqual(self.alta("jefa@ejemplo.com", "jefa"), "user")
        webapp.arrancar(configurar_logs=False)
        self.assertFalse(self.db.hay_admin())

    def test_al_arrancar_si_la_cuenta_ya_existe(self):
        bloqueada = self.db.create_user("jefa", CLAVE, email="jefa@ejemplo.com", status="blocked")
        webapp.arrancar(configurar_logs=False)
        self.assertFalse(self.db.hay_admin())                  # bloqueada: no
        self.db.set_user_status(bloqueada, "active")
        with self.assertLogs("estudio.web", level="WARNING") as log:
            webapp.arrancar(configurar_logs=False)
        self.assertEqual(self.db.get_user(bloqueada)["role"], "admin")
        self.assertFalse(any("@" in l for l in log.output))   # el correo no va al log

    def _crear_admin(self, extra):
        entorno = {k: v for k, v in os.environ.items()
                   if k not in ("DATABASE_URL", "APP_PG_SCHEMA", "RENDER", "ADMIN_EMAIL")}
        entorno.update({"APP_DB_PATH": os.path.join(self.dir, "crear.db"),
                        "ADMIN_USERNAME": "jefa", "ADMIN_PASSWORD": CLAVE, **extra})
        return subprocess.run([sys.executable, "crear_admin.py"], cwd=RAIZ, env=entorno,
                              capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)

    def test_en_produccion_no_se_acepta_una_clave_del_entorno(self):
        for extra in ({"RENDER": "true"}, {"APP_URL": "https://medstudy.ejemplo"}):
            r = self._crear_admin(extra)
            self.assertEqual(r.returncode, 1)
            self.assertIn("no se acepta ADMIN_PASSWORD", r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.dir, "crear.db")))  # ni la abrió
        r = self._crear_admin({})                               # en local sí
        self.assertEqual(r.returncode, 0, r.stderr)
        db = core.DatabaseManager(os.path.join(self.dir, "crear.db"))
        self.assertEqual(db.find_user("jefa")["role"], "admin")
        db.conn.close()


# --------------------------------------------------------------------------- #
def _leer(nombre):
    with open(os.path.join(RAIZ, nombre), encoding="utf-8") as fh:
        return fh.read()


class ArchivosDeDespliegue(unittest.TestCase):
    def test_dockerfile(self):
        d = _leer("Dockerfile")
        self.assertRegex(d, r"(?m)^FROM python:3\.11-slim$")
        self.assertRegex(d, r"apt-get install -y --no-install-recommends poppler-utils")
        self.assertIn("rm -rf /var/lib/apt/lists/*", d)
        self.assertIn("pip install --no-cache-dir -r requirements-web.txt", d)
        self.assertRegex(d, r"(?m)^USER medstudy$")
        self.assertRegex(d, r"(?m)^EXPOSE \d+$")
        self.assertIn("uvicorn webapp:app --host 0.0.0.0 --port ${PORT:-10000} --no-proxy-headers", d)
        # lo que se copia existe, y están todos los módulos propios que se importan
        copiados = set()
        for linea in re.findall(r"(?m)^COPY (?:--\S+ )*(.+)$", d):
            *origenes, _destino = linea.split()
            for o in origenes:
                self.assertTrue(os.path.exists(os.path.join(RAIZ, o)), o)
                copiados.add(o)
        propios = {"core", "correo", "pg_schema"}
        self.assertTrue({m + ".py" for m in propios} | {"webapp.py", "SCHEMA.sql", "static",
                                                         "contenido"} <= copiados, copiados)
        # USER va después de instalar y copiar: la app no puede reescribirse
        self.assertGreater(d.index("USER medstudy"), d.rindex("COPY "))

    def test_dockerignore(self):
        ignorado = set(_leer(".dockerignore").split())
        for patron in ("database.db", ".git/", "**/.env", "**/.env.*", "tests/", "assets/paginas/",
                       "biblioteca/", "exports/", "herramientas/", "**/__pycache__/"):
            self.assertIn(patron, ignorado)

    def test_requirements_web_cubre_lo_que_se_importa(self):
        paquetes = {re.split(r"[\[=<>]", l.strip())[0].lower()
                    for l in _leer("requirements-web.txt").splitlines()
                    if l.strip() and not l.startswith("#")}
        self.assertIn("psycopg", paquetes)
        de_pip = {"fastapi": "fastapi", "starlette": "starlette", "uvicorn": "uvicorn",
                  "genanki": "genanki", "psycopg": "psycopg", "multipart": "python-multipart"}
        importados = set()
        for archivo in ("core.py", "webapp.py", "correo.py", "pg_schema.py", "crear_admin.py"):
            for nodo in ast.walk(ast.parse(_leer(archivo))):
                if isinstance(nodo, ast.Import):
                    importados |= {a.name.split(".")[0] for a in nodo.names}
                elif isinstance(nodo, ast.ImportFrom) and nodo.module and not nodo.level:
                    importados.add(nodo.module.split(".")[0])
        terceros = importados - set(sys.stdlib_module_names) - {"core", "correo", "pg_schema",
                                                                 "webapp", "__future__"}
        self.assertTrue(terceros <= set(de_pip), terceros)
        faltan = {de_pip[m] for m in terceros} - paquetes
        self.assertEqual(faltan, set())
        self.assertIn("python-multipart", paquetes)            # la carga de archivos (local)

    def test_render_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("falta PyYAML para leer render.yaml")
        servicio, = yaml.safe_load(_leer("render.yaml"))["services"]
        self.assertEqual({k: servicio[k] for k in ("type", "runtime", "plan", "region",
                                                   "healthCheckPath", "autoDeploy", "branch")},
                         {"type": "web", "runtime": "docker", "plan": "free", "region": "ohio",
                          "healthCheckPath": "/entrar", "autoDeploy": True, "branch": "main"})
        variables = {v["key"]: v for v in servicio["envVars"]}
        self.assertEqual(set(variables), {"DATABASE_URL", "COOKIE_SEGURA", "IP_DE_CABECERA",
                                          "SECRETO_APP", "BREVO_API_KEY", "CORREO_REMITENTE",
                                          "APP_URL", "APP_NOMBRE", "ADMIN_EMAIL", "HOST"})
        secretos = {"DATABASE_URL", "BREVO_API_KEY", "CORREO_REMITENTE", "APP_URL", "ADMIN_EMAIL"}
        for clave in secretos:
            self.assertIs(variables[clave].get("sync"), False, clave)
            self.assertNotIn("value", variables[clave], clave)
        self.assertIs(variables["SECRETO_APP"].get("generateValue"), True)
        # Con lo que fija el blueprint (más lo que Render genera y lo que se
        # pega en el panel), la comprobación de arranque pasa; sin la base, no.
        entorno = {"RENDER": "true", "SECRETO_APP": "generado-por-render",
                   "DATABASE_URL": "postgresql://u:p@ep-x.us-east-2.aws.neon.tech/neondb"}
        entorno.update({k: v["value"] for k, v in variables.items() if "value" in v})
        self.assertEqual(webapp.problema_de_produccion(entorno), "")
        entorno.pop("DATABASE_URL")
        self.assertIn("DATABASE_URL", webapp.problema_de_produccion(entorno))
        self.assertTrue(core.es_produccion(entorno))           # RENDER: la carga se apaga


if __name__ == "__main__":
    unittest.main()
