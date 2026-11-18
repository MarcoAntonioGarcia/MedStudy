"""Pruebas de seguridad: cabeceras, topes, sesiones, permisos, rutas quitadas
y contención de rutas de archivos.

Corren contra una base SQLite TEMPORAL (APP_DB_PATH) y carpetas temporales:
nunca tocan database.db ni contenido/. Desde la raíz del repo:

    python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from _comun import (CLAVE, CONTENIDO, PDF_MINIMO, RAIZ, TMP, TestClient, cliente, core,
                    tema_con_fichas, usuario, webapp)

# Rutas de /api/* que se pueden usar sin sesión. Todas las demás exigen una.
PUBLICAS = {"/api/auth/register", "/api/auth/link", "/api/auth/register/complete",
            "/api/auth/password/forgot", "/api/auth/password/reset",
            "/api/auth/login", "/api/auth/logout"}


class Base(unittest.TestCase):
    def setUp(self):
        webapp._intentos.clear()
        self._ip_cab = webapp.IP_DE_CABECERA
        self.admin = usuario("admin.prueba", "admin")
        self.ana = usuario("ana")
        self.beto = usuario("beto")

    def tearDown(self):
        webapp.IP_DE_CABECERA = self._ip_cab
        webapp._intentos.clear()


class Cabeceras(Base):
    def test_cabeceras_de_seguridad(self):
        for ruta, c in (("/entrar", cliente()), ("/", cliente("ana")),
                        ("/api/stats", cliente("ana"))):
            r = c.get(ruta)
            self.assertEqual(r.status_code, 200, ruta)
            csp = r.headers["content-security-policy"]
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertIn("default-src 'self'", csp)
            self.assertNotIn("unsafe-inline", csp.split("script-src")[1].split(";")[0])
            self.assertEqual(r.headers["x-frame-options"], "DENY")
            self.assertEqual(r.headers["x-content-type-options"], "nosniff")
            self.assertEqual(r.headers["referrer-policy"], "same-origin")
            self.assertNotIn("strict-transport-security", r.headers)

    def test_hsts_solo_con_cookie_segura(self):
        webapp.COOKIE_SEGURA = True
        try:
            r = cliente().get("/entrar")
            self.assertIn("max-age=", r.headers["strict-transport-security"])
        finally:
            webapp.COOKIE_SEGURA = False

    def test_api_no_se_cachea(self):
        r = cliente("ana").get("/api/bootstrap")
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_documentacion_apagada(self):
        c = cliente("admin.prueba")
        for ruta in ("/docs", "/openapi.json", "/redoc"):
            self.assertEqual(c.get(ruta).status_code, 404, ruta)

    def test_las_paginas_no_llevan_scripts_en_linea(self):
        """Con script-src 'self' un script en línea no correría: todos van en archivos."""
        for nombre in ("index.html", "login.html", "cuenta.html", "admin.html"):
            with open(os.path.join(RAIZ, "static", nombre), encoding="utf-8") as fh:
                html = fh.read()
            for etiqueta in re.findall(r"<script\b[^>]*>", html):
                self.assertIn("src=", etiqueta, nombre)
            self.assertNotRegex(html, r"\son[a-z]+\s*=", nombre)

    def test_cookie_de_sesion(self):
        c = TestClient(webapp.app)
        r = c.post("/api/auth/login", json={"login": "ana", "password": CLAVE})
        cookie = r.headers.get("set-cookie", "").lower()
        self.assertIn("sesion=", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)


class Topes(Base):
    def test_cuerpo_grande_por_content_length(self):
        r = cliente("ana").post("/api/settings", content=b"x" * (webapp.LIMITE_CUERPO + 1),
                                headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_cuerpo_grande_sin_content_length(self):
        def trozos():
            for _ in range(20):
                yield b"x" * 100_000
        r = cliente("ana").post("/api/settings", content=trozos(),
                                headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_sin_sesion_o_sin_ser_admin_el_tope_es_1mb(self):
        dos_mb = b"%PDF-" + b"0" * (2 * 1024 * 1024)
        for c in (cliente(), cliente("ana")):
            r = c.post("/api/upload?topic_id=1", files={"file": ("a.pdf", dos_mb)})
            self.assertEqual(r.status_code, 413)

    def test_el_admin_tiene_25mb(self):
        self.assertEqual(webapp.LIMITES_POR_RUTA["/api/upload"][0], 25 * 1024 * 1024)
        tid, _ = tema_con_fichas("Subidas")
        c = cliente("admin.prueba")
        dos_mb = PDF_MINIMO + b"%" * (2 * 1024 * 1024)
        r = c.post(f"/api/upload?topic_id={tid}", files={"file": ("grande.pdf", dos_mb)})
        self.assertEqual(r.status_code, 200, r.text)
        grande = b"%PDF-" + b"0" * (25 * 1024 * 1024 + 1)
        r = c.post(f"/api/upload?topic_id={tid}", files={"file": ("enorme.pdf", grande)})
        self.assertEqual(r.status_code, 413)

    def test_subida_valida_el_tipo_de_archivo(self):
        tid, _ = tema_con_fichas("Subidas")
        c = cliente("admin.prueba")
        self.assertEqual(c.post(f"/api/upload?topic_id={tid}",
                                files={"file": ("falso.pdf", b"<html>")}).status_code, 400)
        self.assertEqual(c.post(f"/api/upload?topic_id={tid}",
                                files={"file": ("x.pptx", b"PK..")}).status_code, 400)
        self.assertEqual(c.post("/api/upload", files={"file": ("x.apkg", b"no-zip")}).status_code, 400)

    def test_validacion_de_entrada(self):
        tid, ids = tema_con_fichas()
        c = cliente("ana")
        malos = [
            ("/api/settings", {"new_limit": "muchas"}),
            ("/api/settings", {"new_limit": -1}),
            ("/api/settings", {"review_limit": 10 ** 9}),
            ("/api/settings", {"retention": "nan"}),
            ("/api/settings", {"theme": "<script>"}),
            (f"/api/cards/{ids[0]}/grade", {"rating": 7}),
            (f"/api/cards/{ids[0]}/grade", {"rating": "abc"}),
            (f"/api/cards/{ids[0]}/grade", {"rating": 3, "mode": "raro"}),
            (f"/api/topics/{tid}/notes", {"kind": "otra"}),
            (f"/api/topics/{tid}/notes", {"kind": "general", "text": "x" * 20001}),
            (f"/api/topics/{tid}/notes", {"kind": "sticky", "x": "izq"}),
            (f"/api/topics/{tid}/notes", {"kind": "sticky", "page": 0}),
            (f"/api/topics/{tid}/notes", {"kind": "general", "text": 5}),
            (f"/api/topics/{tid}/notes", {"kind": "sticky", "color": "red;}"}),
            (f"/api/topics/{tid}/notes", {"kind": "sticky", "asset_id": 999999}),
        ]
        for ruta, cuerpo in malos:
            self.assertEqual(c.post(ruta, json=cuerpo).status_code, 400, (ruta, cuerpo))
        a = cliente("admin.prueba")
        self.assertEqual(a.post("/api/folders", json={"name": "x" * 81}).status_code, 400)
        self.assertEqual(a.post("/api/folders", json={"name": "Ok", "color": "red;x"}).status_code, 400)
        self.assertEqual(a.post(f"/api/topics/{tid}/folder", json={"folder_id": 999999}).status_code, 400)
        self.assertEqual(c.get("/api/queue?mode=raro").status_code, 400)
        self.assertEqual(c.get("/api/assets/1/page/0").status_code, 400)


class Login(Base):
    def _falla(self, c, login, ip):
        return c.post("/api/auth/login", json={"login": login, "password": "mala-clave-000"},
                      headers={"true-client-ip": ip}).status_code

    def test_login_bien_y_mal(self):
        self.assertEqual(self._falla(cliente(), "ana", "10.0.0.1"), 401)
        self.assertEqual(self._falla(cliente(), "nadie-existe", "10.0.0.1"), 401)
        r = cliente().post("/api/auth/login", json={"login": "ANA", "password": CLAVE})
        self.assertEqual(r.status_code, 200)

    def test_tope_por_ip(self):
        c = cliente()
        for i in range(webapp.TOPES["login"][0]):
            self.assertEqual(self._falla(c, f"x{i}", "1.1.1.1"), 401)
        self.assertEqual(self._falla(c, "y", "1.1.1.1"), 429)

    def test_tope_por_cuenta_aunque_cambie_la_ip(self):
        webapp.IP_DE_CABECERA = "true-client-ip"
        c = cliente()
        for i in range(webapp.TOPES["login_cuenta"][0]):
            self.assertEqual(self._falla(c, "ana", f"10.0.{i}.1"), 401)
        r = c.post("/api/auth/login", json={"login": "ana", "password": CLAVE},
                   headers={"true-client-ip": "10.9.9.9"})
        self.assertEqual(r.status_code, 429)

    def test_entrar_bien_no_borra_los_fallos_de_la_ip(self):
        webapp.IP_DE_CABECERA = "true-client-ip"
        c = cliente()
        tope = webapp.TOPES["login"][0]
        for i in range(tope - 1):
            self._falla(c, f"nadie{i}", "203.0.113.7")
        cliente("ana", ip="203.0.113.7")
        self.assertEqual(self._falla(c, "otro", "203.0.113.7"), 401)
        self.assertEqual(self._falla(c, "otro2", "203.0.113.7"), 429)

    def test_sin_cabecera_configurada_no_se_fia_de_ninguna(self):
        webapp.IP_DE_CABECERA = ""
        c = cliente()
        for i in range(webapp.TOPES["login"][0]):
            self._falla(c, f"x{i}", f"198.51.100.{i}")   # IPs inventadas: se ignoran
        self.assertEqual(self._falla(c, "y", "198.51.100.250"), 429)

    def test_dispositivo_conocido_no_queda_bloqueado(self):
        webapp.IP_DE_CABECERA = "true-client-ip"
        dueno = cliente("ana", ip="192.0.2.10")
        self.assertIn(webapp.COOKIE_DISPOSITIVO, dueno.cookies)
        atacante = cliente()
        for i in range(webapp.TOPES["login_cuenta"][0]):
            self._falla(atacante, "ana", f"10.2.{i}.1")
        self.assertEqual(self._falla(atacante, "ana", "10.2.250.1"), 429)
        r = dueno.post("/api/auth/login", json={"login": "ana", "password": CLAVE},
                       headers={"true-client-ip": "192.0.2.10"})
        self.assertEqual(r.status_code, 200)

    def test_cookie_de_dispositivo_falsa_no_sirve(self):
        webapp.IP_DE_CABECERA = "true-client-ip"
        uid = webapp.db.find_user("beto")["id"]
        c = cliente()
        for i in range(webapp.TOPES["login_cuenta"][0]):
            self._falla(c, "beto", f"10.3.{i}.1")
        c.cookies.set(webapp.COOKIE_DISPOSITIVO, f"{uid}.{int(time.time())}.{'0' * 64}",
                      path="/api/auth")
        r = c.post("/api/auth/login", json={"login": "beto", "password": CLAVE},
                   headers={"true-client-ip": "10.3.250.1"})
        self.assertEqual(r.status_code, 429)

    def test_ipv6_se_agrupa_por_64(self):
        webapp.IP_DE_CABECERA = "true-client-ip"
        c = cliente()
        for i in range(webapp.TOPES["login"][0]):
            self._falla(c, f"v6-{i}", f"2001:db8:1:2::{i + 1:x}")
        self.assertEqual(self._falla(c, "v6-x", "2001:db8:1:2:ffff::9"), 429)
        self.assertEqual(self._falla(c, "v6-y", "2001:db8:1:3::1"), 401)   # otra /64

    def test_en_local_una_cabecera_basura_cae_a_la_ip_del_socket(self):
        webapp.IP_DE_CABECERA = "true-client-ip"
        self.assertFalse(webapp.EN_PRODUCCION)
        self.assertEqual(self._falla(cliente(), "x", "no-es-una-ip"), 401)
        r = cliente().post("/api/auth/login", json={"login": "x", "password": "y" * 12})
        self.assertEqual(r.status_code, 401)

    def test_contrasena_demasiado_larga(self):
        with self.assertRaises(ValueError):
            webapp.db.create_user(username="largo", password="a" * 300)
        r = cliente().post("/api/auth/login", json={"login": "ana", "password": "a" * 5000})
        self.assertEqual(r.status_code, 401)

    def test_una_sesion_por_usuario_y_salir(self):
        primero = cliente("beto")
        segundo = cliente("beto")
        self.assertEqual(primero.get("/api/auth/me").status_code, 401)
        self.assertEqual(segundo.get("/api/auth/me").status_code, 200)
        segundo.post("/api/auth/logout")
        self.assertEqual(segundo.get("/api/auth/me").status_code, 401)

    def test_bloquear_echa_al_momento(self):
        c = cliente("beto")
        a = cliente("admin.prueba")
        try:
            self.assertEqual(a.post(f"/api/admin/users/{self.beto}/status",
                                    json={"status": "blocked"}).status_code, 200)
            self.assertEqual(c.get("/api/auth/me").status_code, 401)
            r = cliente().post("/api/auth/login", json={"login": "beto", "password": CLAVE})
            self.assertEqual(r.status_code, 403)
        finally:
            webapp.db.set_user_status(self.beto, "active")

    def test_cambiar_mi_contrasena(self):
        usuario("carla")
        c = cliente("carla")
        uid = webapp.db.find_user("carla")["id"]
        try:
            self.assertEqual(c.post("/api/auth/password",
                                    json={"current": "mala-0000000", "new": "otra-clave-larga"}).status_code, 403)
            r = c.post("/api/auth/password", json={"current": CLAVE, "new": "otra-clave-larga"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(c.get("/api/auth/me").status_code, 200)        # sigue dentro
        finally:
            webapp.db.set_password(uid, CLAVE)

    def test_el_hash_es_scrypt_y_no_sale(self):
        fila = webapp.db.find_user("ana")
        self.assertTrue(fila["password_hash"].startswith("scrypt$16384$8$1$"))
        self.assertNotIn(CLAVE, fila["password_hash"])
        for u in cliente("admin.prueba").get("/api/admin/users").json():
            self.assertNotIn("password_hash", u)


class Produccion(Base):
    """M1: en un despliegue la app falla cerrado si falta configuración."""

    COMPLETO = {"COOKIE_SEGURA": "1", "IP_DE_CABECERA": "true-client-ip",
                "SECRETO_APP": "s" * 32}

    def test_en_local_no_exige_nada(self):
        self.assertEqual(webapp.problema_de_produccion({}), "")
        self.assertEqual(webapp.problema_de_produccion({"APP_URL": "http://localhost:8090"}), "")

    def test_desplegado_exige_las_tres(self):
        for base in ({"RENDER": "true"}, {"APP_URL": "https://medstudy.ejemplo"},
                     {"APP_URL": "HTTPS://medstudy.ejemplo"}):
            self.assertIn("COOKIE_SEGURA=1", webapp.problema_de_produccion(base))
            self.assertEqual(webapp.problema_de_produccion({**base, **self.COMPLETO}), "")
            for falta in self.COMPLETO:
                entorno = {**base, **self.COMPLETO}
                entorno.pop(falta)
                self.assertIn(falta, webapp.problema_de_produccion(entorno), (base, falta))
            self.assertIn("COOKIE_SEGURA", webapp.problema_de_produccion(
                {**base, **self.COMPLETO, "COOKIE_SEGURA": "0"}))

    def _importar(self, extra):
        entorno = {k: v for k, v in os.environ.items()
                   if k not in ("RENDER", "COOKIE_SEGURA", "IP_DE_CABECERA", "SECRETO_APP")}
        entorno.update({"APP_DB_PATH": os.path.join(TMP, "produccion.db"), **extra})
        return subprocess.run([sys.executable, "-c", "import webapp"], cwd=RAIZ, env=entorno,
                              capture_output=True, text=True, timeout=60)

    def test_no_arranca_sin_configurar(self):
        r = self._importar({"RENDER": "true"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MedStudy no arranca", r.stderr)
        self.assertEqual(self._importar({"RENDER": "true", **self.COMPLETO}).returncode, 0)

    def test_sin_la_cabecera_de_ip_da_400(self):
        webapp.IP_DE_CABECERA = "true-client-ip"
        webapp.EN_PRODUCCION = True
        try:
            c = cliente()
            cuerpo = {"login": "ana", "password": CLAVE}
            self.assertEqual(c.post("/api/auth/login", json=cuerpo).status_code, 400)
            self.assertEqual(c.post("/api/auth/login", json=cuerpo,
                                    headers={"true-client-ip": "basura"}).status_code, 400)
            self.assertEqual(c.post("/api/auth/password/forgot", json={"email": "a@b.co"}).status_code, 400)
            self.assertEqual(c.post("/api/auth/login", json=cuerpo,
                                    headers={"true-client-ip": "192.0.2.1"}).status_code, 200)
        finally:
            webapp.EN_PRODUCCION = False


class Permisos(Base):
    def _rutas_api(self):
        for ruta in webapp.app.routes:
            camino = getattr(ruta, "path", "")
            if camino.startswith("/api/") and camino not in PUBLICAS:
                for metodo in sorted(getattr(ruta, "methods", None) or []):
                    yield metodo, re.sub(r"\{[^}]+\}", "1", camino)

    def test_todo_api_exige_sesion(self):
        c = cliente()
        vistas = 0
        for metodo, camino in self._rutas_api():
            r = c.request(metodo, camino, json={} if metodo in ("POST", "PUT") else None)
            self.assertEqual(r.status_code, 401, (metodo, camino, r.text))
            vistas += 1
        self.assertGreater(vistas, 25)

    def test_la_pagina_principal_manda_a_entrar(self):
        r = cliente().get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303, 307))
        self.assertEqual(r.headers["location"], "/entrar")
        self.assertEqual(cliente().get("/admin", follow_redirects=False).headers["location"], "/entrar")
        self.assertEqual(cliente("ana").get("/").status_code, 200)

    def test_operaciones_de_admin_dan_403_a_usuarios(self):
        tid, _ = tema_con_fichas()
        c = cliente("ana")
        llamadas = [
            ("POST", "/api/ingest", None),
            ("POST", "/api/upload?topic_id=1", "archivo"),
            ("GET", "/api/orphans", None),
            ("POST", "/api/orphans/1/assign", {"topic_id": tid}),
            ("POST", "/api/folders", {"name": "Mía"}),
            ("POST", "/api/folders/1", {"name": "Renombrada"}),
            ("DELETE", "/api/folders/1", None),
            ("POST", f"/api/topics/{tid}/folder", {"folder_id": None}),
            ("GET", "/api/admin/users", None),
            ("POST", "/api/admin/users", {"username": "colado", "password": CLAVE}),
            ("POST", f"/api/admin/users/{self.beto}/status", {"status": "blocked"}),
            ("POST", f"/api/admin/users/{self.beto}/sesiones/cerrar", {}),
            ("POST", f"/api/admin/users/{self.beto}/password", {"password": "x" * 12}),
            ("GET", "/admin", None),
        ]
        for metodo, ruta, cuerpo in llamadas:
            if cuerpo == "archivo":
                r = c.post(ruta, files={"file": ("a.pdf", PDF_MINIMO)})
            else:
                r = c.request(metodo, ruta, json=cuerpo)
            self.assertEqual(r.status_code, 403, (metodo, ruta, r.text))
        self.assertIsNone(webapp.db.find_user("colado"))
        self.assertEqual(webapp.db.get_user(self.beto)["status"], "active")
        self.assertEqual([o for o in c.get("/api/bootstrap").json()["orphans"]], [])

    def test_el_admin_si_puede(self):
        a = cliente("admin.prueba")
        r = a.post("/api/folders", json={"name": "Carpeta del admin", "color": "#2f6b5e"})
        self.assertEqual(r.status_code, 200)
        fid = r.json()["id"]
        tid, _ = tema_con_fichas()
        self.assertEqual(a.post(f"/api/topics/{tid}/folder", json={"folder_id": fid}).status_code, 200)
        self.assertEqual(a.post(f"/api/folders/{fid}", json={"name": "Otra"}).status_code, 200)
        self.assertEqual(a.delete(f"/api/folders/{fid}").status_code, 200)
        self.assertEqual(a.get("/api/admin/users").status_code, 200)
        self.assertEqual(a.get("/admin").status_code, 200)

    def test_el_ultimo_admin_no_se_bloquea(self):
        a = cliente("admin.prueba")
        self.assertEqual(a.post(f"/api/admin/users/{self.admin}/status",
                                json={"status": "blocked"}).status_code, 400)

    def test_rutas_quitadas(self):
        tid, _ = tema_con_fichas()
        aid = webapp.db.get_topic(tid)["assets"]["resumen"][0]["id"]
        a = cliente("admin.prueba")
        self.assertIn(a.post(f"/api/assets/{aid}/open").status_code, (404, 405))
        self.assertFalse(hasattr(core, "open_in_system_app"))

    def test_ingest_no_acepta_otra_carpeta(self):
        otra = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(otra, "Ajena", "Anki"))
        shutil.copy(__file__, os.path.join(otra, "Ajena", "Anki", "Ajena.apkg"))
        antes = webapp.db.count_topics()
        r = cliente("admin.prueba").post("/api/ingest", json={"content_dir": otra})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(webapp.db.count_topics(), antes)
        self.assertFalse(any(otra in e for e in r.json()["errores"]))
        s = cliente("ana").post("/api/settings", json={"content_dir": otra}).json()
        self.assertNotIn("content_dir", s)
        self.assertEqual(webapp.db.get_setting("content_dir"), "")

    def test_las_rutas_del_disco_no_salen(self):
        tema_con_fichas()
        for c in (cliente("ana"), cliente("admin.prueba")):
            texto = c.get("/api/bootstrap").text
            self.assertNotIn(TMP, texto)
            self.assertNotIn('"path"', texto)


class Archivos(Base):
    def setUp(self):
        super().setUp()
        self.tid, _ = tema_con_fichas()
        self.aid = webapp.db.get_topic(self.tid)["assets"]["resumen"][0]["id"]

    def _material_en(self, ruta, kind="articulo"):
        webapp.db.add_asset(self.tid, kind, ruta)
        with webapp.db._lock:
            return webapp.db.conn.execute("SELECT id FROM assets WHERE path = ?",
                                          (ruta,)).fetchone()["id"]

    def test_pdf_se_sirve_con_sesion(self):
        self.assertEqual(cliente().get(f"/api/assets/{self.aid}/file").status_code, 401)
        r = cliente("ana").get(f"/api/assets/{self.aid}/file")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/pdf")
        self.assertTrue(r.headers["content-disposition"].startswith("inline"))
        self.assertTrue(r.content.startswith(b"%PDF-"))
        self.assertEqual(r.headers["cache-control"], "private, no-store")
        # se puede enmarcar en la propia app (visor sin poppler), en ninguna otra
        self.assertEqual(r.headers["x-frame-options"], "SAMEORIGIN")
        self.assertEqual(r.headers["content-security-policy"], "frame-ancestors 'self'")

    def test_pagina_renderizada(self):
        if not core.PdfRenderer.available():
            self.skipTest("poppler no está instalado")
        self.assertEqual(cliente().get(f"/api/assets/{self.aid}/page/1").status_code, 401)
        r = cliente("ana").get(f"/api/assets/{self.aid}/page/1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertEqual(r.headers["cache-control"], "private, no-store")

    def test_pagina_que_no_existe(self):
        # el PDF de prueba tiene 1 página
        self.assertEqual(cliente("ana").get(f"/api/assets/{self.aid}/page/2").status_code, 404)

    def test_renders_limitados_a_la_vez(self):
        if not core.PdfRenderer.available():
            self.skipTest("poppler no está instalado")
        pdf = webapp.db.get_asset(self.aid)["path"]
        activos, maximo, candado = [0], [0], threading.Lock()
        original = core.subprocess.run

        def lento(*a, **k):
            with candado:
                activos[0] += 1
                maximo[0] = max(maximo[0], activos[0])
            time.sleep(0.2)
            with candado:
                activos[0] -= 1
            return None                       # no deja PNG: render devuelve None
        core.subprocess.run = lento
        try:
            hilos = [threading.Thread(target=core.PdfRenderer.render, args=(pdf, 100 + i))
                     for i in range(6)]
            for h in hilos:
                h.start()
            for h in hilos:
                h.join()
        finally:
            core.subprocess.run = original
        self.assertEqual(maximo[0], 2)

    def test_fuera_de_las_carpetas_no_se_sirve(self):
        fuera = os.path.join(TMP, "secreto.pdf")
        with open(fuera, "wb") as fh:
            fh.write(PDF_MINIMO)
        intentos = [
            fuera,
            os.path.join(CONTENIDO, "..", "secreto.pdf"),          # con '..'
            "/etc/hosts",
        ]
        enlace = os.path.join(CONTENIDO, "Neuro", "enlace.pdf")     # enlace simbólico hacia fuera
        if not os.path.lexists(enlace):
            os.symlink(fuera, enlace)
        intentos.append(enlace)
        c = cliente("ana")
        for ruta in intentos:
            aid = self._material_en(ruta, "resumen" if ruta.endswith(".pdf") else "articulo")
            self.assertEqual(c.get(f"/api/assets/{aid}/file").status_code, 404, ruta)
            self.assertEqual(c.get(f"/api/assets/{aid}/page/1").status_code, 404, ruta)

    def test_ruta_permitida(self):
        self.assertTrue(core.ruta_permitida(os.path.join(CONTENIDO, "Neuro", "a.pdf")))
        self.assertTrue(core.ruta_permitida(os.path.join(core.LIBRARY_DIR, "a.pdf")))
        self.assertFalse(core.ruta_permitida(CONTENIDO))
        self.assertFalse(core.ruta_permitida(CONTENIDO + "-otra/a.pdf"))   # prefijo, no carpeta
        self.assertFalse(core.ruta_permitida(""))
        self.assertFalse(core.ruta_permitida("/etc/passwd"))

    def test_material_que_ya_no_existe(self):
        aid = self._material_en(os.path.join(CONTENIDO, "Neuro", "borrado.pdf"))
        self.assertEqual(cliente("ana").get(f"/api/assets/{aid}/file").status_code, 410)

    def test_exportar_descarga_un_apkg(self):
        r = cliente("ana").post(f"/api/topics/{self.tid}/export")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.content.startswith(b"PK"))
        self.assertIn("attachment", r.headers["content-disposition"])


class Sesion(Base):
    def test_static_no_consulta_la_sesion(self):
        """L6: los estilos y scripts no tocan la base; el resto sí."""
        c = cliente("ana")
        original = webapp.db.user_for_token
        llamadas = []

        def contando(token):
            llamadas.append(1)
            return original(token)
        webapp.db.user_for_token = contando
        try:
            self.assertEqual(c.get("/static/app.js").status_code, 200)
            self.assertEqual(llamadas, [])
            self.assertEqual(c.get("/api/stats").status_code, 200)
            self.assertEqual(len(llamadas), 1)
        finally:
            webapp.db.user_for_token = original


class Navegador(unittest.TestCase):
    """esc() del front, ejecutado con node si está instalado."""

    def test_esc_escapa_comillas_simples(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node no está instalado")
        with open(os.path.join(RAIZ, "static", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        ini = js.index("const esc = ")
        fin = js.index(";\n", ini) + 2
        prueba = js[ini:fin] + """
console.log(JSON.stringify({
  a: esc(`"><img src=x onerror=alert(1)> ' &`), n: esc(null), c: esc(0)}));"""
        r = subprocess.run([node, "-e", prueba], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        for peligro in ('"', "'", "<", ">"):
            self.assertNotIn(peligro, out["a"])
        self.assertIn("&#39;", out["a"])
        self.assertEqual(out["n"], "")
        self.assertEqual(out["c"], "0")


if __name__ == "__main__":
    unittest.main()
