"""El progreso, las notas y los ajustes son de cada persona.

Dos personas estudian el mismo material: lo que una califica, anota o ajusta
no cambia nada de lo que ve la otra, y ninguna puede leer ni tocar las notas
de la otra (404, como si no existieran). También: la migración de una base
vieja (progreso global en `cards`) es idempotente.
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

from _comun import RAIZ, TMP, cliente, core, tema_con_fichas, usuario, webapp


class Aislamiento(unittest.TestCase):
    def setUp(self):
        webapp._intentos.clear()
        self.a_id = usuario("alicia")
        self.b_id = usuario("bruno")
        self.tid, self.ids = tema_con_fichas("Cefaleas Primarias", n=6)
        # cada prueba parte de cero para estas dos personas
        with webapp.db._lock:
            for t in ("card_progress", "reviews", "notes", "user_settings"):
                webapp.db.conn.execute(f"DELETE FROM {t} WHERE user_id IN (?, ?)",
                                       (self.a_id, self.b_id))
            webapp.db.conn.commit()
        self.a = cliente("alicia")
        self.b = cliente("bruno")

    def _tema(self, c):
        return next(t for t in c.get("/api/topics").json() if t["id"] == self.tid)

    def test_calificar_no_cambia_el_progreso_del_otro(self):
        antes_b = (self._tema(self.b), self.b.get("/api/stats").json(),
                   self.b.get(f"/api/queue?topic_id={self.tid}").json()["scope"])
        r = self.a.post(f"/api/cards/{self.ids[0]}/grade", json={"rating": 3})
        self.assertEqual(r.status_code, 200, r.text)
        self.a.post(f"/api/cards/{self.ids[1]}/grade", json={"rating": 1})

        # alicia ve su progreso
        ta = self._tema(self.a)
        self.assertEqual(ta["nuevas"], 4)
        sa = self.a.get("/api/stats").json()
        self.assertEqual(sa["nuevas_hoy"], 2)
        self.assertEqual(sa["aprendiendo"], 1)
        self.assertEqual(sa["racha"], 1)

        # bruno sigue exactamente igual
        despues_b = (self._tema(self.b), self.b.get("/api/stats").json(),
                     self.b.get(f"/api/queue?topic_id={self.tid}").json()["scope"])
        self.assertEqual(antes_b, despues_b)
        self.assertEqual(despues_b[0]["nuevas"], 6)
        self.assertEqual(despues_b[1]["nuevas_hoy"], 0)
        self.assertEqual(despues_b[1]["racha"], 0)

    def test_la_cola_es_de_cada_uno(self):
        self.a.post(f"/api/cards/{self.ids[0]}/grade", json={"rating": 4})   # fácil: días
        cola_a = [c["id"] for c in self.a.get(f"/api/queue?topic_id={self.tid}").json()["cards"]]
        cola_b = self.b.get(f"/api/queue?topic_id={self.tid}").json()["cards"]
        self.assertNotIn(self.ids[0], cola_a)
        self.assertIn(self.ids[0], [c["id"] for c in cola_b])
        self.assertTrue(all(c["state"] == "new" for c in cola_b))

    def test_el_tope_diario_es_de_cada_uno(self):
        self.a.post("/api/settings", json={"new_limit": 2})
        for cid in self.ids[:2]:
            self.a.post(f"/api/cards/{cid}/grade", json={"rating": 3})
        self.assertEqual(self.a.get(f"/api/queue?topic_id={self.tid}").json()["cards"], [])
        self.assertEqual(len(self.b.get(f"/api/queue?topic_id={self.tid}").json()["cards"]), 6)

    def test_repaso_libre_no_guarda_nada(self):
        self.a.post(f"/api/cards/{self.ids[0]}/grade", json={"rating": 3, "mode": "libre"})
        self.assertEqual(self.a.get("/api/stats").json()["nuevas_hoy"], 0)
        with webapp.db._lock:
            n = webapp.db.conn.execute("SELECT COUNT(*) AS n FROM card_progress WHERE user_id = ?",
                                       (self.a_id,)).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_los_repasos_llevan_dueno(self):
        self.a.post(f"/api/cards/{self.ids[2]}/grade", json={"rating": 3})
        with webapp.db._lock:
            filas = webapp.db.conn.execute(
                "SELECT user_id FROM reviews WHERE card_id = ?", (self.ids[2],)).fetchall()
        self.assertEqual([f["user_id"] for f in filas], [self.a_id])

    def test_notas_privadas(self):
        r = self.a.post(f"/api/topics/{self.tid}/notes", json={"kind": "general", "text": "de alicia"})
        self.assertEqual(r.status_code, 200, r.text)
        nid = r.json()["id"]
        aid = webapp.db.get_topic(self.tid)["assets"]["resumen"][0]["id"]
        self.a.post(f"/api/topics/{self.tid}/notes",
                    json={"kind": "sticky", "asset_id": aid, "page": 1, "x": 0.2, "y": 0.3,
                          "text": "post-it de alicia"})

        # bruno no las ve: ni en el tema, ni en la lista de notas
        self.assertEqual(self.b.get(f"/api/topics/{self.tid}").json()["notes"], [])
        self.assertEqual(self.b.get(f"/api/topics/{self.tid}/notes").json(), [])
        # ni puede leerlas, editarlas o borrarlas por id
        self.assertEqual(self.b.post(f"/api/notes/{nid}", json={"text": "pisada"}).status_code, 404)
        self.assertEqual(self.b.post(f"/api/notes/{nid}", json={"x": 0.9}).status_code, 404)
        self.assertEqual(self.b.delete(f"/api/notes/{nid}").status_code, 404)

        notas_a = self.a.get(f"/api/topics/{self.tid}/notes").json()
        self.assertEqual(sorted(n["text"] for n in notas_a), ["de alicia", "post-it de alicia"])
        # la dueña sí puede
        self.assertEqual(self.a.post(f"/api/notes/{nid}", json={"text": "editada"}).status_code, 200)
        self.assertEqual(self.a.delete(f"/api/notes/{nid}").status_code, 200)
        self.assertEqual(len(self.a.get(f"/api/topics/{self.tid}/notes").json()), 1)

    def test_ajustes_de_cada_uno(self):
        self.a.post("/api/settings", json={"new_limit": 5, "review_limit": 50,
                                           "retention": 0.85, "theme": "oscuro"})
        sa = self.a.get("/api/settings").json()
        sb = self.b.get("/api/settings").json()
        self.assertEqual((sa["new_limit"], sa["review_limit"], sa["retention"], sa["theme"]),
                         (5, 50, 0.85, "oscuro"))
        self.assertEqual((sb["new_limit"], sb["review_limit"], sb["retention"], sb["theme"]),
                         (core.DEFAULT_NEW_LIMIT, core.DEFAULT_REVIEW_LIMIT,
                          core.DEFAULT_RETENTION, "auto"))
        self.assertEqual(self.b.get("/api/stats").json()["nuevas_limite"], core.DEFAULT_NEW_LIMIT)

    def test_el_contenido_es_compartido(self):
        """Las fichas en sí no son de nadie: las dos personas ven las mismas."""
        fa = [c["front"] for c in self.a.get(f"/api/topics/{self.tid}").json()["cards"]]
        fb = [c["front"] for c in self.b.get(f"/api/topics/{self.tid}").json()["cards"]]
        self.assertEqual(fa, fb)
        self.assertEqual(len(fa), 6)

    def test_reimportar_no_borra_el_progreso(self):
        self.a.post(f"/api/cards/{self.ids[0]}/grade", json={"rating": 4})
        tema_con_fichas("Cefaleas Primarias", n=6)          # mismo guid: solo se actualiza el texto
        self.assertEqual(webapp.db.get_card(self.ids[0], self.a_id)["state"], "review")


class Migracion(unittest.TestCase):
    """Una base de antes de las cuentas se pone al día sin romperse, y volver a
    abrirla no cambia nada."""

    def test_base_vieja(self):
        dir_ = tempfile.mkdtemp(dir=TMP)
        ruta = os.path.join(dir_, "vieja.db")
        con = sqlite3.connect(ruta)
        con.executescript("""
            CREATE TABLE folders (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '#c9a86a', sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE topics (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL, area TEXT NOT NULL DEFAULT 'Neurología',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                kind TEXT NOT NULL DEFAULT 'basic', front TEXT NOT NULL, back TEXT NOT NULL DEFAULT '',
                cloze_index INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'anki', anki_guid TEXT,
                suspended INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'new', due TEXT,
                stability REAL NOT NULL DEFAULT 0, difficulty REAL NOT NULL DEFAULT 0,
                reps INTEGER NOT NULL DEFAULT 0, lapses INTEGER NOT NULL DEFAULT 0, last_review TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE INDEX idx_cards_due ON cards (due, suspended);
            CREATE INDEX idx_cards_topic ON cards (topic_id, state);
            CREATE UNIQUE INDEX idx_cards_guid ON cards (anki_guid, cloze_index) WHERE anki_guid IS NOT NULL;
            CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                rating INTEGER NOT NULL, state_before TEXT NOT NULL DEFAULT '',
                elapsed_days REAL NOT NULL DEFAULT 0, scheduled_days REAL NOT NULL DEFAULT 0,
                stability REAL NOT NULL DEFAULT 0, difficulty REAL NOT NULL DEFAULT 0,
                reviewed_at TEXT NOT NULL DEFAULT (datetime('now')),
                day TEXT NOT NULL DEFAULT (date('now')));
            CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                asset_id INTEGER, kind TEXT NOT NULL DEFAULT 'sticky', page INTEGER NOT NULL DEFAULT 1,
                x REAL NOT NULL DEFAULT 0.5, y REAL NOT NULL DEFAULT 0.5,
                color TEXT NOT NULL DEFAULT 'amarillo', text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
            INSERT INTO topics (slug, title) VALUES ('vertigo', 'Vértigo');
            INSERT INTO cards (topic_id, front, back, anki_guid) VALUES (1, 'P', 'R', 'g1');
            INSERT INTO notes (topic_id, kind, text) VALUES (1, 'general', 'nota sin dueño');
            INSERT INTO settings VALUES ('content_dir', '/otra/parte'), ('new_limit', '50'),
                                        ('baseline_at', '2026-01-01 00:00:00');
        """)
        con.commit()
        con.close()

        for _ in range(2):                           # dos arranques: idempotente
            db = core.DatabaseManager(ruta)
            cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(cards)")}
            self.assertFalse(cols & set(core._COLUMNAS_FSRS_VIEJAS), cols)
            self.assertIn("user_id", {r["name"] for r in db.conn.execute("PRAGMA table_info(notes)")})
            self.assertIn("user_id", {r["name"] for r in db.conn.execute("PRAGMA table_info(reviews)")})
            tablas = {r["name"] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertTrue({"users", "sessions", "email_tokens", "card_progress",
                             "user_settings"} <= tablas)
            self.assertEqual(db.get_setting("content_dir"), "")
            self.assertEqual(db.get_setting("new_limit"), "")
            self.assertEqual(db.get_setting("baseline_at"), "2026-01-01 00:00:00")
            self.assertEqual(db.count_topics(), 1)
            db.conn.close()

        db = core.DatabaseManager(ruta)
        uid = db.create_user("migrada", "clave-larga-123")
        self.assertEqual(db.list_notes(uid, 1), [])          # la nota sin dueño no es de nadie
        self.assertEqual(db.build_queue(uid)[0]["front"], "P")
        self.assertEqual(db.grade(uid, 1, 3)["state"], "review")
        db.conn.close()
        shutil.rmtree(dir_, True)

    def test_la_base_real_no_se_toca(self):
        self.assertNotEqual(os.path.realpath(os.environ["APP_DB_PATH"]),
                            os.path.realpath(os.path.join(RAIZ, "database.db")))


if __name__ == "__main__":
    unittest.main()
