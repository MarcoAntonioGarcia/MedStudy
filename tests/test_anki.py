"""Fichas que vienen de archivos de Anki: el HTML se limpia al importar (L1) y
la base de dentro del .apkg se abre con desconfianza (L2)."""
import os
import sqlite3
import tempfile
import unittest
import zipfile

import genanki

from _comun import CONTENIDO, TMP, cliente, core, usuario, webapp

MALICIOSO = [
    ('<img src=x onerror="alert(1)">Pregunta <script>alert(2)</script>',
     '<a href="javascript:alert(3)">mira</a> <div style="color:red" onclick="x()">r</div>'),
    ('<style>body{display:none}</style><b onmouseover="y()">Negrita</b>',
     '<img src="https://espia.ejemplo/p.gif"><img src="//espia.ejemplo/q.gif">'
     '<iframe src="https://x"></iframe><table><tr><td colspan="2" bgcolor="red">ok</td></tr></table>'),
]


def _mazo(ruta: str, notas, titulo: str = "Mazo Malicioso") -> str:
    modelo = genanki.Model(1607392319111, "Basico prueba",
                           fields=[{"name": "F"}, {"name": "B"}],
                           templates=[{"name": "T", "qfmt": "{{F}}", "afmt": "{{B}}"}])
    mazo = genanki.Deck(2059400119999, f"Pruebas::{titulo}")
    for i, (f, b) in enumerate(notas):
        mazo.add_note(genanki.Note(model=modelo, fields=[f, b], guid=f"malo-{titulo}-{i}"))
    genanki.Package(mazo).write_to_file(ruta)
    return ruta


def _sin_peligro(texto: str) -> None:
    bajo = texto.lower()
    for peligro in ("<script", "onerror", "onclick", "onmouseover", "javascript:", "<a ",
                    "<style", "style=", "<iframe", "https://", "//espia", "bgcolor"):
        assert peligro not in bajo, (peligro, texto)


class LimpiezaHTML(unittest.TestCase):
    def test_lista_blanca(self):
        self.assertEqual(core.limpiar_html('<b>a</b><i>b</i><br><sub>2</sub>'),
                         '<b>a</b><i>b</i><br><sub>2</sub>')
        self.assertEqual(core.limpiar_html('<img src="data:image/png;base64,AAAA">'),
                         '<img src="data:image/png;base64,AAAA">')
        self.assertEqual(core.limpiar_html('<img src="media/a.png">'), '<img src="media/a.png">')
        self.assertEqual(core.limpiar_html('1 &lt; 2 &amp; 3'), '1 &lt; 2 &amp; 3')
        self.assertEqual(core.limpiar_html('<div><b>sin cerrar'), '<div><b>sin cerrar</b></div>')
        for malo in ('<img src="javascript:alert(1)">', '<IMG SRC=JaVaScRiPt:alert(1)>',
                     '<img src="data:text/html;base64,PHNjcmlwdD4=">', '<img src="\\\\x\\y.png">',
                     '<svg onload=alert(1)><script>1</script></svg>'):
            self.assertEqual(core.limpiar_html(malo), "", malo)
        self.assertEqual(core.limpiar_html('<a href="javascript:x">texto</a>'), "texto")

    def test_el_cloze_sigue_funcionando(self):
        _, reverso = core.render_cloze("La triada es {{c1::temblor}}", 1)
        self.assertEqual(core.limpiar_html(reverso), "La triada es <b>temblor</b>")


class MazoMalicioso(unittest.TestCase):
    def setUp(self):
        webapp._intentos.clear()
        usuario("admin.anki", "admin")

    def _fichas(self, titulo):
        tid = webapp.db.ensure_topic(titulo)
        return webapp.db.topic_cards(tid)

    def test_carga_desde_la_app(self):
        ruta = _mazo(os.path.join(tempfile.mkdtemp(dir=TMP), "malo.apkg"), MALICIOSO)
        with open(ruta, "rb") as fh:
            r = cliente("admin.anki").post("/api/upload", files={"file": ("malo.apkg", fh.read())})
        self.assertEqual(r.status_code, 200, r.text)
        fichas = self._fichas("Mazo Malicioso")
        self.assertEqual(len(fichas), 2)
        for f in fichas:
            _sin_peligro(f["front"])
            _sin_peligro(f["back"])
        self.assertIn("<b>Negrita</b>", fichas[1]["front"])
        self.assertIn('<td colspan="2">ok</td>', fichas[1]["back"])

    def test_la_ingesta_limpia_tambien_lo_que_ya_estaba(self):
        """Una ficha que ya estaba sucia en la base se limpia al reimportar."""
        tid = webapp.db.ensure_topic("Mazo Ingesta")
        with webapp.db._lock:
            webapp.db.conn.execute(
                "INSERT INTO cards (topic_id, front, back, anki_guid) VALUES (?, ?, ?, ?)",
                (tid, '<img src=x onerror=alert(1)>vieja', 'r', "malo-Mazo Ingesta-0"))
            webapp.db.conn.commit()
        carpeta = os.path.join(CONTENIDO, "Pruebas", "Anki")
        os.makedirs(carpeta, exist_ok=True)
        _mazo(os.path.join(carpeta, "Mazo Ingesta.apkg"), MALICIOSO, "Mazo Ingesta")
        r = cliente("admin.anki").post("/api/ingest")
        self.assertEqual(r.status_code, 200, r.text)
        fichas = self._fichas("Mazo Ingesta")
        self.assertEqual(len(fichas), 2)
        for f in fichas:
            _sin_peligro(f["front"])
            _sin_peligro(f["back"])


class ApkgDesconfiado(unittest.TestCase):
    def _apkg(self, preparar) -> str:
        dir_ = tempfile.mkdtemp(dir=TMP)
        base = os.path.join(dir_, "collection.anki2")
        con = sqlite3.connect(base)
        preparar(con)
        con.commit()
        con.close()
        ruta = os.path.join(dir_, "prueba.apkg")
        with zipfile.ZipFile(ruta, "w") as zf:
            zf.write(base, "collection.anki2")
        return ruta

    def test_col_o_notes_como_vista_se_rechaza(self):
        def vista(con):
            con.execute("CREATE TABLE col (models TEXT, decks TEXT)")
            con.execute("INSERT INTO col VALUES ('{}', '{}')")
            con.execute("CREATE VIEW notes AS SELECT 'g' AS guid, 1 AS mid, '' AS tags, 'a' AS flds")
        with self.assertRaises(core.AnkiFormatError):
            core.AnkiImporter.read(self._apkg(vista))

    def test_se_abre_en_solo_lectura(self):
        ruta = _mazo(os.path.join(tempfile.mkdtemp(dir=TMP), "ro.apkg"), [("a", "b")], "Solo Lectura")
        original = core.sqlite3.connect
        vistos = []

        def espia(destino, *a, **k):
            vistos.append((destino, k.get("uri")))
            return original(destino, *a, **k)
        core.sqlite3.connect = espia
        try:
            core.AnkiImporter.read(ruta)
        finally:
            core.sqlite3.connect = original
        self.assertEqual(len(vistos), 1)
        self.assertTrue(vistos[0][1])
        self.assertIn("mode=ro", vistos[0][0])
        self.assertIn("immutable=1", vistos[0][0])

    def test_lectura_demasiado_lenta_se_corta(self):
        def grande(con):
            con.execute("CREATE TABLE col (models TEXT, decks TEXT)")
            con.execute("""INSERT INTO col VALUES ('{"1": {"type": 0}}', '{"1": {"name": "Lento"}}')""")
            con.execute("CREATE TABLE notes (guid TEXT, mid INTEGER, tags TEXT, flds TEXT)")
            con.executemany("INSERT INTO notes VALUES (?, 1, '', 'a')",
                            ((f"g{i}",) for i in range(200_000)))
        ruta = self._apkg(grande)
        viejo = core.SEGUNDOS_LECTURA_ANKI
        core.SEGUNDOS_LECTURA_ANKI = 0
        try:
            with self.assertRaises(core.AnkiFormatError):
                core.AnkiImporter.read(ruta)
        finally:
            core.SEGUNDOS_LECTURA_ANKI = viejo


if __name__ == "__main__":
    unittest.main()
