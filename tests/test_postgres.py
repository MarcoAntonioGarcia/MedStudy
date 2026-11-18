"""Postgres (producción) sin tener un Postgres: la traducción de dialecto.

La app escribe cada consulta UNA vez, en dialecto SQLite, y core.a_postgres la
traduce cuando la base es Postgres. Un descuido ahí no se ve en local (allí la
consulta va tal cual a SQLite): se ve en producción. Por eso:

  * TodasLasConsultas ejecuta un recorrido por TODOS los métodos de la base
    contra SQLite, graba cada consulta con sus parámetros, la traduce y busca
    restos de SQLite, marcas que no cuadran con los parámetros y GROUP BY que
    Postgres rechazaría. Además comprueba, leyendo core.py, que el recorrido
    pasó por todas las llamadas a self.conn.execute: una consulta nueva que
    nadie ejercita hace fallar la prueba.
  * PostgresReal corre ese mismo recorrido contra un Postgres de verdad, solo
    si se define TEST_PG_URL (en un esquema propio que se borra al terminar).
    Sin la variable, se salta. Con ella, además, TODA la batería corre contra
    Postgres (ver _comun.py):

        TEST_PG_URL=postgresql://… python -m unittest discover -s tests
"""
import ast
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

import genanki

from _comun import CLAVE, PDF_MINIMO, PG_URL, TMP, core, esquema_pg_temporal

import pg_schema

RUTA_CORE = os.path.abspath(core.__file__)

# Métodos que solo corren con un motor. Los de SQLite son las migraciones de
# bases de antes de las cuentas (PRAGMA, sqlite_master): con Postgres no se
# llaman. Los de Postgres se comprueban aparte (PorEscrito).
SOLO_SQLITE = {"__init__", "_migrate", "_apply_schema"}
SOLO_POSTGRES = {"_esquema_postgres", "_alinear_columnas_postgres", "candado_global"}


# --------------------------------------------------------------------------- #
# Recorrido por toda la base
# --------------------------------------------------------------------------- #
class Grabadora:
    """Envuelve la conexión y apunta cada consulta, con la línea de core.py
    desde la que se lanzó."""

    def __init__(self, con):
        self._con = con
        self.vistas: list[tuple[int, str, tuple]] = []

    def execute(self, sql, params=()):
        marco = sys._getframe(1)
        if os.path.abspath(marco.f_code.co_filename) == RUTA_CORE:
            self.vistas.append((marco.f_lineno, sql, tuple(params)))
        return self._con.execute(sql, params)

    def __getattr__(self, nombre):
        return getattr(self._con, nombre)


def _mazo(ruta, titulo, n=3, cambio=""):
    modelo = genanki.Model(1607392319222, "Basico recorrido",
                           fields=[{"name": "F"}, {"name": "B"}],
                           templates=[{"name": "T", "qfmt": "{{F}}", "afmt": "{{B}}"}])
    mazo = genanki.Deck(2059400118888, f"Neurología::{titulo}")
    for i in range(n):
        mazo.add_note(genanki.Note(model=modelo, fields=[f"P{i}{cambio}", f"R{i}"],
                                   guid=f"rec-{titulo}-{i}"))
    genanki.Package(mazo).write_to_file(ruta)


def ejercitar(db: core.DatabaseManager, raiz: str) -> None:
    """Pasa por todos los métodos de DatabaseManager (y por la ingesta) con
    datos que hacen que cada rama lance su consulta. `raiz` hace de carpeta de
    material: core.CONTENT_DIR tiene que apuntar a ella."""
    t = unittest.TestCase()
    for sub in ("Anki", "Resumenes PDF", "Articulos"):
        os.makedirs(os.path.join(raiz, "Neuro", sub), exist_ok=True)
    _mazo(os.path.join(raiz, "Neuro", "Anki", "Vertigo.apkg"), "Vértigo")
    with open(os.path.join(raiz, "Neuro", "Resumenes PDF", "Vertigo - Resumen.pdf"), "wb") as fh:
        fh.write(PDF_MINIMO)
    with open(os.path.join(raiz, "Neuro", "Articulos", "Algo sin tema.pdf"), "wb") as fh:
        fh.write(PDF_MINIMO)

    # --- ingesta, dos veces: la segunda encuentra todo ya creado
    r = core.ingest(db, raiz)
    t.assertEqual((r["temas"], r["tarjetas_nuevas"], r["huerfanos"]), (1, 3, 1), r)
    r = core.ingest(db, raiz)
    t.assertEqual((r["tarjetas_nuevas"], r["tarjetas_actualizadas"]), (0, 3))
    _mazo(os.path.join(raiz, "Neuro", "Anki", "Vertigo.apkg"), "Vértigo", n=4, cambio="!")
    r = core.ingest(db, raiz)                      # una nueva y tres con texto cambiado
    t.assertEqual((r["tarjetas_nuevas"], r["tarjetas_actualizadas"]), (1, 3))

    # --- cuentas
    ana = db.create_user("ana", CLAVE, email="Ana@Ejemplo.com", nombre="Ana")
    with t.assertRaises(ValueError):
        db.create_user("ANA", CLAVE)                             # usuario ocupado
    with t.assertRaises(ValueError):
        db.create_user("otra", CLAVE, email="ana@ejemplo.com")    # correo ocupado
    t.assertIsNone(db.find_user(""))
    t.assertEqual(db.find_user("ANA@ejemplo.com")["id"], ana)
    t.assertEqual(db.find_user_by_email("ana@ejemplo.com")["id"], ana)
    t.assertEqual(db.get_user(ana)["username"], "ana")
    t.assertFalse(db.hay_admin())
    t.assertIsNone(db.promover_admin_inicial("nadie@ejemplo.com"))
    eva = db.create_user("eva", CLAVE, email="eva@ejemplo.com")
    t.assertEqual(db.promover_admin_inicial("EVA@ejemplo.com"), eva)
    t.assertIsNone(db.promover_admin_inicial("ana@ejemplo.com"))  # ya hay admin
    t.assertTrue(db.hay_admin())
    t.assertEqual(db.count_admins(), 1)
    token = db.create_session(ana)
    t.assertEqual(db.user_for_token(token)["id"], ana)
    t.assertIn("sesiones", db.list_users()[0])
    t.assertEqual(db.cerrar_sesiones(ana), 1)
    db.delete_session(db.create_session(ana))
    db.purge_expired_sessions()
    db.set_password(ana, CLAVE + "x")
    t.assertTrue(db.set_user_status(ana, "blocked"))
    t.assertTrue(db.set_user_status(ana, "active"))
    tok = db.crear_token_correo("ana@ejemplo.com", "reset", 30, "grupo")
    t.assertEqual(db.contar_correos(3600), 1)
    t.assertEqual(db.contar_correos(3600, "ana@ejemplo.com", "reset", "grupo"), 1)
    t.assertEqual(db.ver_token_correo(tok, "reset"), "ana@ejemplo.com")
    t.assertTrue(db.gastar_token_correo(tok, "reset"))
    t.assertFalse(db.gastar_token_correo(tok, "reset"))
    db.anular_tokens_correo("ana@ejemplo.com", "reset")
    db.purgar_tokens_correo()

    # --- carpetas y temas
    fid = db.ensure_folder("Extra")
    t.assertEqual(db.ensure_folder("Extra"), fid)
    t.assertTrue(db.folder_exists(fid))
    t.assertTrue(db.rename_folder(fid, "Extra 2"))
    t.assertTrue(db.rename_folder(fid, "Extra 3", "#123456"))
    t.assertTrue(any(f["temas"] == 1 for f in db.list_folders()))
    tid = db.ensure_topic("Vértigo")
    suelto = db.ensure_topic("Tema suelto")
    t.assertEqual(db.ensure_topic("Tema suelto", folder_id=fid), suelto)   # se le pone carpeta
    t.assertTrue(db.move_topic(suelto, None))
    t.assertTrue(db.move_topic(suelto, fid))
    t.assertTrue(db.delete_folder(fid))
    t.assertEqual(db.count_topics(), 2)
    t.assertEqual(len(db.topics_basic()), 2)
    t.assertTrue(db.now_sql())

    # --- materiales
    tema = db.get_topic(tid)
    resumen = tema["assets"]["resumen"][0]
    t.assertTrue(os.path.isfile(resumen["path"]))
    t.assertEqual(db.get_asset(resumen["id"])["path"], resumen["path"])
    t.assertIn("resumen", db.topic_assets(tid))
    huerfano = db.list_orphans()[0]
    t.assertTrue(db.assign_orphan(huerfano["id"], tid))
    t.assertFalse(db.assign_orphan(huerfano["id"], tid))
    t.assertEqual(db.list_orphans(), [])

    # --- notas y ajustes
    nid = db.add_note(ana, tid, asset_id=resumen["id"], page=1, x=0.3, y=0.4, text="hola")
    t.assertEqual(len(db.list_notes(ana, tid)), 1)
    t.assertEqual(len(db.list_notes(ana, tid, "sticky")), 1)
    t.assertTrue(db.update_note(ana, nid, text="adiós", x=0.5))
    t.assertFalse(db.update_note(eva, nid, text="ajena"))
    db.set_setting("prueba", "1")
    t.assertEqual(db.get_setting("prueba"), "1")
    db.set_setting("prueba", "2")
    db.set_user_setting(ana, "new_limit", 50)
    db.set_user_setting(ana, "new_limit", 2)
    t.assertEqual(db.get_int_user_setting(ana, "new_limit", 20), 2)
    t.assertEqual(db.get_user_setting(ana, "theme", "auto"), "auto")
    t.assertEqual(db.retention(ana), core.DEFAULT_RETENTION)

    # --- repaso
    fichas = db.topic_cards(tid)
    t.assertEqual(len(fichas), 4)
    carpeta = db.get_topic(tid)["folder_id"]
    t.assertEqual(len(db.build_queue(ana)), 2)                 # tope de 2 nuevas
    t.assertEqual(len(db.build_queue(ana, topic_id=tid)), 2)
    t.assertEqual(len(db.build_queue(ana, folder_id=carpeta)), 2)
    t.assertEqual(len(db.build_queue(ana, mode="libre")), 4)
    t.assertEqual(db.get_card(fichas[0]["id"], ana)["state"], "new")
    t.assertEqual(db.grade(ana, fichas[0]["id"], 1)["state"], "learning")
    t.assertEqual(db.grade(ana, fichas[1]["id"], 3)["state"], "review")
    t.assertEqual(db.new_today(ana), 2)
    t.assertEqual(db.reviews_today(ana), 0)
    t.assertEqual(db.grade(ana, fichas[0]["id"], 3)["state"], "review")
    t.assertEqual(db.reviews_today(ana), 1)
    t.assertEqual(db.scope_counts(ana)["nuevas"], 2)
    t.assertEqual(db.scope_counts(ana, tid)["total"], 4)
    s = db.stats(ana)
    t.assertEqual((s["total"], s["nuevas"], s["maduras"], s["racha"]), (4, 2, 2, 1))
    temas = {x["title"]: x for x in db.list_topics(ana)}
    t.assertEqual((temas["Vértigo"]["total"], temas["Vértigo"]["nuevas"]), (4, 2))
    t.assertEqual(temas["Tema suelto"]["total"], 0)
    t.assertEqual(temas["Tema suelto"]["nuevas"], 0)
    t.assertTrue(db.delete_note(ana, nid))

    # --- rutas viejas (absolutas) a relativas, con un duplicado que tiene notas
    nid = db.add_note(ana, tid, asset_id=resumen["id"], text="sigue")
    with db._lock:
        db.conn.execute("UPDATE assets SET path = ? WHERE id = ?",
                        ("/Users/otra/AppEstudio/" + os.path.basename(raiz)
                         + "/Neuro/Resumenes PDF/Vertigo - Resumen.pdf", resumen["id"]))
        db.conn.execute("INSERT INTO assets (topic_id, kind, path, filename, ext) "
                        "VALUES (?, 'resumen', ?, 'x.pdf', 'pdf')",
                        (tid, "Neuro/Resumenes PDF/Vertigo - Resumen.pdf"))
        db.conn.execute("INSERT INTO orphan_files (path, filename, kind, ext) "
                        "VALUES (?, 'y.pdf', 'articulo', 'pdf')",
                        (os.path.join(raiz, "Neuro", "Articulos", "Otro.pdf"),))
        db.conn.commit()
    db._migrar_rutas()
    t.assertEqual([n["asset_id"] for n in db.list_notes(ana, tid)],
                  [db.get_topic(tid)["assets"]["resumen"][0]["id"]])
    with db._lock:
        rutas = [r["path"] for r in db.conn.execute(
            "SELECT path FROM assets UNION ALL SELECT path FROM orphan_files")]
    t.assertFalse([p for p in rutas if os.path.isabs(p)], rutas)
    db._migrar_rutas()                                          # idempotente


def recorrido_sqlite() -> tuple[list, int]:
    """Corre ejercitar() contra un SQLite nuevo y devuelve lo grabado."""
    carpeta = tempfile.mkdtemp(dir=TMP)
    raiz = os.path.join(carpeta, "contenido")
    db = core.DatabaseManager(os.path.join(carpeta, "recorrido.db"))
    grabadora = Grabadora(db.conn)
    db.conn = grabadora
    antes = core.CONTENT_DIR
    core.CONTENT_DIR = raiz
    try:
        ejercitar(db, raiz)
    finally:
        core.CONTENT_DIR = antes
        db.conn = grabadora._con
        db.conn.close()
        shutil.rmtree(carpeta, True)
    return grabadora.vistas


def llamadas_en_core() -> dict[int, str]:
    """{línea: método} de cada self.conn.execute(...) dentro de DatabaseManager."""
    arbol = ast.parse(open(RUTA_CORE, encoding="utf-8").read())
    fuera = {}
    for clase in arbol.body:
        if isinstance(clase, ast.ClassDef) and clase.name == "DatabaseManager":
            for metodo in clase.body:
                if not isinstance(metodo, ast.FunctionDef):
                    continue
                for nodo in ast.walk(metodo):
                    if (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
                            and nodo.func.attr == "execute"
                            and ast.unparse(nodo.func.value) == "self.conn"):
                        fuera[nodo.lineno] = metodo.name
    return fuera


# --------------------------------------------------------------------------- #
# Comprobaciones sobre una consulta ya traducida
# --------------------------------------------------------------------------- #
def _sin_parentesis(texto: str) -> str:
    """Quita todo lo que va entre paréntesis (agregados, subconsultas)."""
    anterior = None
    while anterior != texto:
        anterior, texto = texto, re.sub(r"\([^()]*\)", "", texto)
    return texto


def problemas_group_by(sql_pg: str) -> list[str]:
    """Postgres exige que toda columna no agregada del SELECT esté en el GROUP
    BY. Heurística: las columnas 'alias.col' (y 'alias.*') del SELECT exterior,
    fuera de paréntesis, tienen que aparecer en el GROUP BY."""
    plano = " ".join(_sin_parentesis(sql_pg).split())
    m = re.search(r"\bGROUP BY\s+(.*?)(?:\bORDER BY\b|\bLIMIT\b|\bHAVING\b|$)", plano, re.I)
    if not m:
        return []
    agrupadas = {g.strip() for g in m.group(1).split(",")}
    lista = re.search(r"\bSELECT\s+(.*?)\s+FROM\b", plano, re.I | re.S)
    columnas = re.findall(r"\b\w+\.(?:\w+|\*)", lista.group(1)) if lista else []
    return [f"{c} no está en el GROUP BY" for c in columnas if c not in agrupadas]


def problemas_de(sql: str, params: tuple) -> list[str]:
    pg = core.a_postgres(sql)
    problemas = core.problemas_postgres(pg) + problemas_group_by(pg)
    marcas = pg.replace("%%", "").count("%s")
    if marcas != len(params):
        problemas.append(f"{marcas} marcas para {len(params)} parámetros")
    if pg.replace("%%", "").replace("%s", "").count("%"):
        problemas.append("'%' suelto (psycopg lo tomaría por una marca)")
    return problemas


class Traduccion(unittest.TestCase):
    def test_marcas_y_porcentajes(self):
        self.assertEqual(core.a_postgres("SELECT * FROM t WHERE a = ? AND b LIKE 'x%'"),
                         "SELECT * FROM t WHERE a = %s AND b LIKE 'x%%'")

    def test_fechas_como_texto_y_en_utc(self):
        pg = core.a_postgres("UPDATE notes SET updated_at = datetime('now') WHERE id = ?")
        self.assertIn("to_char(timezone('UTC', now()), 'YYYY-MM-DD HH24:MI:SS')", pg)
        self.assertIn("to_char(timezone('UTC', now()), 'YYYY-MM-DD')",
                      core.a_postgres("DEFAULT (date('now'))"))
        self.assertIn("LOCALTIMESTAMP", core.a_postgres("DEFAULT (datetime('now', 'localtime'))"))
        self.assertEqual(core.problemas_postgres(pg), [])

    def test_or_ignore_y_or_replace(self):
        self.assertEqual(core.a_postgres("INSERT OR IGNORE INTO t (a) VALUES (?);"),
                         "INSERT INTO t (a) VALUES (%s) ON CONFLICT DO NOTHING")
        with self.assertRaises(NotImplementedError):
            core.a_postgres("INSERT OR REPLACE INTO t (a) VALUES (?)")

    def test_detecta_restos_de_sqlite(self):
        for sql in ("SELECT SUM(state = 'new') FROM c", "SELECT 1 FROM c WHERE NOT COALESCE(s, 0)",
                    # las de la etapa 1, tal cual estaban: paréntesis anidados
                    "SELECT SUM(COALESCE(p.state, 'new') = 'new') AS n FROM c",
                    "SELECT SUM(p.state IN ('learning','relearning')) AS n FROM c",
                    "SELECT SUM(c.id IS NOT NULL AND COALESCE(p.state, 'new') = 'new') FROM c",
                    "SELECT 1 FROM c WHERE 1 AND x = %s", "PRAGMA table_info(c)",
                    "SELECT name FROM sqlite_master", "SELECT strftime('%Y', d) FROM c",
                    "SELECT IFNULL(a, 0) FROM c"):
            self.assertTrue(core.problemas_postgres(sql), sql)
        for sql in ("SELECT SUM(CASE WHEN state = 'new' THEN 1 ELSE 0 END) FROM c",
                    "SELECT 1 FROM c WHERE 1 = 1 AND COALESCE(s, 0) = 0", "SELECT COUNT(*) FROM c"):
            self.assertEqual(core.problemas_postgres(sql), [], sql)

    def test_group_by_estricto(self):
        self.assertTrue(problemas_group_by(
            "SELECT f.*, COUNT(t.id) AS n FROM folders f LEFT JOIN topics t ON 1 = 1 GROUP BY f.id"))
        self.assertEqual(problemas_group_by(
            "SELECT f.id, f.name, COUNT(t.id) AS n, (SELECT MAX(a.x) FROM a WHERE a.f = f.id) "
            "FROM folders f LEFT JOIN topics t ON 1 = 1 GROUP BY f.id, f.name ORDER BY f.name"), [])


class Esquema(unittest.TestCase):
    def test_sin_restos_de_sqlite(self):
        self.assertEqual(pg_schema.comprobar(), [])

    def test_mismas_tablas_e_indices_que_sqlite(self):
        con = sqlite3.connect(":memory:")
        con.executescript(open(core.SCHEMA_PATH, encoding="utf-8").read())
        tablas = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        indices = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")}
        texto = "\n".join(pg_schema.sentencias())
        self.assertEqual(set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", texto)), tablas)
        self.assertEqual(set(re.findall(r"INDEX IF NOT EXISTS (\w+)", texto))
                         - {"idx_users_username_lower", "idx_users_email_lower"}, indices)
        # el índice único parcial que impide duplicar fichas al reimportar
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_guid ON cards "
                      "(anki_guid, cloze_index) WHERE anki_guid IS NOT NULL", texto)

    def test_columnas_para_alinear(self):
        columnas = dict(pg_schema.columnas_por_tabla())
        self.assertIn(("ip_grupo", "TEXT NOT NULL DEFAULT ''"), columnas["email_tokens"])
        self.assertTrue(all(not re.match(r"(UNIQUE|CHECK|PRIMARY)", n) for n, _ in columnas["users"]))
        for definiciones in columnas.values():
            for nombre, definicion in definiciones:
                self.assertEqual(core.problemas_postgres(definicion), [], (nombre, definicion))

    def test_tablas_con_id(self):
        self.assertIn("assets", core._tablas_con_id())
        self.assertNotIn("card_progress", core._tablas_con_id())
        self.assertNotIn("settings", core._tablas_con_id())


class TodasLasConsultas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vistas = recorrido_sqlite()

    def test_cada_consulta_se_traduce_bien(self):
        malas = {}
        for linea, sql, params in self.vistas:
            problemas = problemas_de(sql, params)
            if problemas:
                malas[linea] = (problemas, " ".join(sql.split())[:120])
        self.assertEqual(malas, {})

    def test_el_recorrido_pasa_por_todas_las_consultas(self):
        llamadas = llamadas_en_core()
        portables = {l: m for l, m in llamadas.items()
                     if m not in SOLO_SQLITE | SOLO_POSTGRES}
        vistas = {linea for linea, _, _ in self.vistas}
        sin_probar = {l: m for l, m in portables.items() if l not in vistas}
        self.assertEqual(sin_probar, {}, "consultas que el recorrido no ejecutó")
        self.assertGreater(len(portables), 60)

    def test_las_de_solo_postgres_por_escrito(self):
        """Las consultas que solo corren con Postgres no se pueden grabar en
        SQLite: se comprueban sobre el texto de core.py."""
        arbol = ast.parse(open(RUTA_CORE, encoding="utf-8").read())
        vistas = 0
        for nodo in ast.walk(arbol):
            if (isinstance(nodo, ast.FunctionDef) and nodo.name in SOLO_POSTGRES):
                for llamada in ast.walk(nodo):
                    if (isinstance(llamada, ast.Call) and isinstance(llamada.func, ast.Attribute)
                            and llamada.func.attr == "execute"
                            and ast.unparse(llamada.func.value) == "self.conn"
                            and isinstance(llamada.args[0], ast.Constant)):
                        sql = llamada.args[0].value
                        params = llamada.args[1].elts if len(llamada.args) > 1 else []
                        self.assertEqual(problemas_de(sql, tuple(params)), [], sql)
                        vistas += 1
        self.assertGreaterEqual(vistas, 3)


# --------------------------------------------------------------------------- #
# La conexión a Postgres, con un servidor de mentira
# --------------------------------------------------------------------------- #
class _CursorFalso:
    rowcount = 1

    def fetchone(self):
        return {"id": 7, "ok": True}

    def fetchall(self):
        return []

    def __iter__(self):
        return iter([])


class _ServidorFalso:
    """Apunta lo que llega a cada conexión y la «mata» cuando se le pide,
    como hace Neon al suspenderse. Las excepciones son las de psycopg."""

    def __init__(self):
        self.enviado: list[tuple[int, str]] = []     # (n.º de conexión, sql)
        self.abiertas = 0
        self.matar_en: str | None = None             # al recibir esto, la conexión muere
        self.error_en: str | None = None             # al recibir esto, error de la consulta

    def conexion(self):
        self.abiertas += 1
        servidor, numero = self, self.abiertas

        class Con:
            closed = broken = False

            def execute(self, sql, params=None):
                import psycopg
                if self.closed:
                    raise psycopg.OperationalError("the connection is closed")
                if servidor.matar_en and servidor.matar_en in sql:
                    servidor.matar_en = None
                    self.closed = self.broken = True
                    raise psycopg.OperationalError("server closed the connection unexpectedly")
                if servidor.error_en and servidor.error_en in sql:
                    servidor.error_en = None
                    raise psycopg.errors.UniqueViolation("duplicate key")
                servidor.enviado.append((numero, sql))
                return _CursorFalso()

            def close(self):
                self.closed = True
        return Con()


class ConexionSimulada(unittest.TestCase):
    def setUp(self):
        self.s = _ServidorFalso()
        servidor = self.s

        class Probada(core.ConexionPostgres):
            def _abrir(self):
                return servidor.conexion()
        self.c = Probada("postgresql://u@ep-x.us-east-2.aws.neon.tech/db")

    def sql(self):
        return [s for _, s in self.s.enviado]

    def test_lectura_suelta_sin_transaccion(self):
        self.c.execute("SELECT * FROM users WHERE id = ?", (1,))
        self.c.commit()
        self.assertEqual(self.sql(), ["SELECT * FROM users WHERE id = %s"])

    def test_escrituras_juntas_y_lastrowid(self):
        cur = self.c.execute("INSERT INTO folders (name) VALUES (?)", ("x",))
        self.assertEqual(cur.lastrowid, 7)
        self.c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("a", "b"))
        self.c.commit()
        self.assertEqual(self.sql(), ["BEGIN", "INSERT INTO folders (name) VALUES (%s) RETURNING id",
                                      "INSERT INTO settings (key, value) VALUES (%s, %s)", "COMMIT"])

    def test_lectura_tras_neon_dormido_reconecta(self):
        self.s.matar_en = "SELECT"
        self.c.execute("SELECT 1 AS n")
        self.assertEqual(self.s.enviado, [(2, "SELECT 1 AS n")])   # en una conexión nueva

    def test_escritura_tras_neon_dormido_reconecta_en_el_begin(self):
        self.s.matar_en = "BEGIN"
        self.c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (1,))
        self.c.commit()
        self.assertEqual(self.s.enviado, [(2, "BEGIN"), (2, "UPDATE users SET role = 'admin' "
                                          "WHERE id = %s"), (2, "COMMIT")])

    def test_si_muere_a_mitad_de_transaccion_no_se_reintenta(self):
        import psycopg
        self.c.execute("DELETE FROM sessions WHERE user_id = ?", (1,))
        self.s.matar_en = "UPDATE"
        with self.assertRaises(psycopg.OperationalError):
            self.c.execute("UPDATE users SET password_hash = ? WHERE id = ?", ("h", 1))
        self.c.commit()                                       # no hay nada que confirmar
        self.assertNotIn("COMMIT", self.sql())
        self.assertEqual(sum(1 for s in self.sql() if s.startswith("UPDATE")), 0)
        self.c.execute("SELECT 1 AS n")                       # y la conexión nueva sirve
        self.assertEqual(self.s.enviado[-1], (2, "SELECT 1 AS n"))

    def test_error_de_consulta_deshace_y_sube(self):
        import psycopg
        self.c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("a", "1"))
        self.s.error_en = "INSERT INTO users"
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.c.execute("INSERT INTO users (username) VALUES (?)", ("ana",))
        self.assertEqual(self.sql()[-1], "ROLLBACK")
        self.c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("b", "2"))
        self.assertEqual(self.sql()[-2], "BEGIN")             # transacción nueva, limpia
        self.assertEqual(self.s.abiertas, 1)                  # sin reconectar

    def test_commit_que_no_llega_no_se_da_por_bueno(self):
        import psycopg
        self.c.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("a", "1"))
        self.s.matar_en = "COMMIT"
        with self.assertRaises(psycopg.OperationalError):
            self.c.commit()
        self.c.execute("SELECT 1 AS n")
        self.assertEqual(self.s.abiertas, 2)

    def test_pooler_sin_candados_de_sesion(self):
        self.assertFalse(self.c.con_pooler)
        servidor = self.s

        class Probada(core.ConexionPostgres):
            def _abrir(self):
                return servidor.conexion()
        self.assertTrue(Probada("postgresql://u@ep-x-pooler.us-east-2.aws.neon.tech/db").con_pooler)
        with self.assertRaises(ValueError):
            Probada("postgresql://u@h/db", esquema="x; DROP TABLE users")


@unittest.skipUnless(PG_URL, "define TEST_PG_URL para probar contra un Postgres real")
class PostgresReal(unittest.TestCase):
    """El recorrido completo contra Postgres, en un esquema propio."""

    def setUp(self):
        self._esquema_antes = os.environ.get("APP_PG_SCHEMA")
        os.environ["APP_PG_SCHEMA"] = esquema_pg_temporal()
        self.carpeta = tempfile.mkdtemp(dir=TMP)

    def tearDown(self):
        if self._esquema_antes is None:
            os.environ.pop("APP_PG_SCHEMA", None)
        else:
            os.environ["APP_PG_SCHEMA"] = self._esquema_antes
        shutil.rmtree(self.carpeta, True)

    def _db(self):
        return core.DatabaseManager(url_pg=PG_URL)

    def test_recorrido_completo(self):
        db = self._db()
        self.assertTrue(db.es_postgres)
        raiz = os.path.join(self.carpeta, "contenido")
        antes = core.CONTENT_DIR
        core.CONTENT_DIR = raiz
        try:
            ejercitar(db, raiz)
        finally:
            core.CONTENT_DIR = antes
        db.conn.close()
        self._db().conn.close()                     # abrir otra vez: idempotente

    def test_unicidad_sin_mayusculas(self):
        import psycopg
        db = self._db()
        db.create_user("ana", CLAVE, email="ana@ejemplo.com")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with db._lock:
                db.conn.execute("INSERT INTO users (username, email, password_hash) "
                                "VALUES (?, ?, 'x')", ("ANA", "otra@ejemplo.com"))
        # tras el error la conexión sigue sirviendo (se hizo rollback)
        self.assertEqual(db.find_user("ANA")["username"], "ana")
        db.conn.close()

    def test_reconecta_si_neon_cerro_la_conexion(self):
        db = self._db()
        db.conn._con.close()                           # como cuando Neon se suspende
        self.assertEqual(db.count_topics(), 0)         # lectura: reconecta y sigue
        db.conn._con.close()
        self.assertTrue(db.ensure_folder("Tras dormir"))   # escritura: el BEGIN reconecta
        with db._lock:
            db.conn.execute("INSERT INTO settings (key, value) VALUES ('a', '1')")
            db.conn._con.close()                       # muere A MITAD de la transacción
            with self.assertRaises(Exception):
                db.conn.execute("INSERT INTO settings (key, value) VALUES ('b', '2')")
        self.assertEqual(db.get_setting("a"), "")      # nada a medias, y se dijo
        db.conn.close()

    def test_columna_nueva_llega_a_una_base_creada(self):
        db = self._db()
        with db._lock:
            db.conn.execute("ALTER TABLE email_tokens DROP COLUMN ip_grupo")
            db.conn.commit()
        db.conn.close()
        db = self._db()
        db.crear_token_correo("a@ejemplo.com", "reset", 5, "g")
        self.assertEqual(db.contar_correos(60, ip_grupo="g"), 1)
        db.conn.close()

    def test_candado_entre_instancias(self):
        a, b = self._db(), self._db()
        with a.candado_global("prueba") as primero:
            self.assertTrue(primero)
            with b.candado_global("prueba", espera=1) as segundo:
                self.assertFalse(segundo)
        with b.candado_global("prueba", espera=1) as despues:
            self.assertTrue(despues)
        a.conn.close()
        b.conn.close()


if __name__ == "__main__":
    unittest.main()
