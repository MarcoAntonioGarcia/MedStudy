"""
core.py — Lógica de negocio de la App de Estudio, SIN framework de UI.

No importa FastAPI ni ninguna GUI: solo persistencia, ingesta, programación
de repasos e intercambio con Anki. Reutilizable por cualquier front.

Piezas:
    - normalize()/match_topic()  -> emparejar archivos con temas (nombres inconsistentes)
    - FSRS                       -> programación de repasos (reemplaza a SM-2)
    - AnkiImporter               -> .apkg -> tarjetas (Basic y Cloze)
    - AnkiExporter               -> tarjetas -> .apkg (genanki)
    - DatabaseManager            -> SQLite (temas, materiales, tarjetas, repasos)
    - ingest()                   -> escanea las carpetas y arma los temas

La unidad atómica del diseño es el TEMA. Ver ARCHITECTURE.md / SCHEMA.sql.
"""

from __future__ import annotations

import datetime
import glob
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import unicodedata
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "SCHEMA.sql")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")

# Los PDF que se cargan desde la app se copian aquí: son ligeros (~170 KB) y
# así la app es autocontenida. Las presentaciones NO se copian —pesan hasta
# 91 MB— y se consultan donde ya viven.
LIBRARY_DIR = os.path.join(BASE_DIR, "biblioteca")

# Páginas del resumen ya renderizadas a PNG. Se generan una vez y se reusan;
# se borran solas si el PDF cambia de fecha.
PAGE_CACHE_DIR = os.path.join(BASE_DIR, "assets", "paginas")
PAGE_DPI = 130

# Carpeta con el material (resúmenes, mazos y artículos). Por defecto, contenido/.
# Se puede sobreescribir con la variable de entorno o desde los ajustes.
DEFAULT_CONTENT_DIR = os.path.join(BASE_DIR, "contenido")
CONTENT_DIR = os.environ.get("ESTUDIO_CONTENT_DIR", DEFAULT_CONTENT_DIR)

PRESENTATION_EXTS = (".pptx", ".ppt", ".pdf", ".key")
SUMMARY_EXTS = (".pdf",)
DECK_EXTS = (".apkg",)

# Días que un tema o material recién llegado se muestra como «nuevo».
NEW_DAYS = 21

# Cuántas tarjetas nuevas por día por defecto. Con ~745 tarjetas, 20/día
# cubre todo el material en unas cinco semanas sin saturar.
DEFAULT_NEW_LIMIT = 20
DEFAULT_REVIEW_LIMIT = 200
DEFAULT_RETENTION = 0.90


# --------------------------------------------------------------------------- #
# Normalización y emparejamiento de nombres
#
# Los tres juegos de archivos NO usan los mismos nombres:
#     'Enfermedad de Alzheimer CORREGIDA.pptx'
#     'Alzheimer - Resumen.pdf'
#     'Neurología::Enfermedad de Alzheimer'   (dentro del .apkg)
# El nombre canónico sale del mazo Anki; los otros dos se emparejan por tokens.
# --------------------------------------------------------------------------- #

# Palabras sin valor discriminante: aparecen en muchos archivos o son ruido
# del proceso de edición ('CORREGIDA', 'EXPO', el nombre de quien la hizo).
_STOPWORDS = {
    "de", "del", "la", "las", "el", "los", "y", "e", "o", "u", "en", "con",
    "resumen", "expo", "exposicion", "presentacion", "corregida", "corregido",
    "final", "copia", "diana", "otras", "otro", "otra", "sx",
}

# Siglas y sinónimos del dominio. Sin esto, 'EVENTO CEREBRAL VASCULAR
# ISQUEMICO.pptx' nunca empata con el tema 'EVC Isquémico'.
_ALIASES = {
    "evc": {"evento", "cerebral", "vascular"},
    "cidp": {"polineuropatia", "desmielinizante", "cronica"},
    "ait": {"ataque", "isquemico", "transitorio"},
    "ft": {"frontotemporal", "frontotemporales"},
    "sindromes": {"sindrome"},
    "epilepticos": {"epileptico"},
    "primarias": {"primaria"},
    "cefaleas": {"cefalea"},
    "demencias": {"demencia"},
    "mielopatias": {"mielopatia"},
    "neurologica": {"neurologia", "neurologico"},
    "fisica": {"fisico"},
    "miasteniformes": {"miasteniforme"},
    "glucemico": {"glucosa", "glucemia"},
    "glucemia": {"glucosa", "glucemico"},
    "glucosa": {"glucemico", "glucemia"},
    "hospitalizado": {"hospitalizados", "hospitalario"},
    "hospitalizados": {"hospitalizado", "hospitalario"},
    "paciente": {"pacientes"},
    "pacientes": {"paciente"},
    "fenotipos": {"subfenotipos", "fenotipo"},
    "subfenotipos": {"fenotipos", "fenotipo"},
    "lra": {"lesion", "renal", "aguda"},
    # El archivo original está mecanografiado como «TOMBOPROFILAXIS».
    "tomboprofilaxis": {"tromboprofilaxis"},
    "tromboprofilaxis": {"tomboprofilaxis"},
}

# Ruido de nombres de archivo que no aporta y que además ensucia el conteo de
# tokens: sufijos de herramientas de compresión y de versiones.
_STOPWORDS_ARCHIVO = {"compressed", "compresed", "final", "v1", "v2", "v3", "copia"}


def strip_accents(text: str) -> str:
    """'Vértigo' -> 'Vertigo'. Necesario porque los nombres mezclan acentos."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def slugify(text: str) -> str:
    """Identificador estable de un tema: 'Enfermedad de Alzheimer' -> 'enfermedad-de-alzheimer'."""
    base = strip_accents(text).lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return base or "tema"


def tokenize(text: str) -> set[str]:
    """Tokens significativos de un nombre, ya expandidos por sinónimos."""
    base = strip_accents(text).lower()
    raw = [t for t in re.split(r"[^a-z0-9]+", base) if t]
    tokens: set[str] = set()
    for tok in raw:
        # Números sueltos ('Cefaleas Primarias 3') y palabras de una letra: ruido.
        if tok.isdigit() or len(tok) < 2:
            continue
        tokens.add(tok)
        tokens |= _ALIASES.get(tok, set())
    return tokens - _STOPWORDS - _STOPWORDS_ARCHIVO


def match_score(file_name: str, topic_title: str) -> float:
    """
    Confianza (0..1) de que un archivo pertenece a un tema.

    Usa CONTENCIÓN, no Jaccard: 'Alzheimer - Resumen.pdf' aporta un solo token
    útil frente a los dos de 'Enfermedad de Alzheimer'. Jaccard lo castigaría
    (1/2 = 0.5) aunque el empate sea perfecto. La contención sobre el conjunto
    más pequeño da 1.0, que es lo correcto.

    La extensión se quita antes de tokenizar: 'pdf' y 'pptx' aparecen en todos
    los archivos, así que no discriminan y solo diluyen el puntaje.
    """
    a, b = tokenize(os.path.splitext(file_name)[0]), tokenize(topic_title)
    if not a or not b:
        return 0.0
    shared = a & b
    if not shared:
        return 0.0
    containment = len(shared) / min(len(a), len(b))
    # Desempate: a igual contención, gana el tema que comparte más tokens en
    # absoluto. Distingue 'Exploración Neurológica' de 'Exploración Física
    # Neurológica', que si no quedarían empatados en 1.0.
    coverage = len(shared) / max(len(a), len(b))
    return round(containment * 0.8 + coverage * 0.2, 4)


def match_topic(file_name: str, topics: list[dict], threshold: float = 0.5):
    """Devuelve (topic|None, score) del mejor tema para un archivo."""
    best, best_score = None, 0.0
    for topic in topics:
        score = match_score(file_name, topic["title"])
        if score > best_score:
            best, best_score = topic, score
    if best_score < threshold:
        return None, best_score
    return best, best_score


# --------------------------------------------------------------------------- #
# FSRS — Free Spaced Repetition Scheduler (v5)
#
# Reemplaza al SM-2 clásico de Anki. Con material médico voluminoso reduce
# bastante la carga diaria a igual retención, porque modela por separado la
# ESTABILIDAD (cuánto dura la memoria) y la DIFICULTAD (qué tan dura es la
# tarjeta), en vez de un solo 'factor de facilidad'.
#
# Los pesos son los valores por defecto publicados de FSRS-5. Se pueden
# re-optimizar con el historial de la tabla `reviews`, que guarda todo lo
# necesario para hacerlo (por eso se registra cada repaso, no solo el último).
# --------------------------------------------------------------------------- #
FSRS_W = (
    0.40255, 1.18385, 3.173, 15.69105, 7.1949, 0.5345, 1.4604, 0.0046,
    1.54575, 0.1192, 1.01925, 1.9395, 0.11, 0.29605, 2.2698, 0.2315,
    2.9898, 0.51655, 0.6621,
)
FSRS_DECAY = -0.5
FSRS_FACTOR = 19.0 / 81.0
MAX_INTERVAL = 36500

RATING_AGAIN, RATING_HARD, RATING_GOOD, RATING_EASY = 1, 2, 3, 4


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def retrievability(elapsed_days: float, stability: float) -> float:
    """Probabilidad de recordar la tarjeta tras `elapsed_days` sin verla."""
    if stability <= 0:
        return 0.0
    return (1 + FSRS_FACTOR * max(elapsed_days, 0) / stability) ** FSRS_DECAY


def interval_for(stability: float, retention: float = DEFAULT_RETENTION) -> int:
    """Días hasta que la retención caiga al objetivo. Mínimo 1 día."""
    if stability <= 0:
        return 1
    days = (stability / FSRS_FACTOR) * (retention ** (1 / FSRS_DECAY) - 1)
    return int(_clamp(round(days), 1, MAX_INTERVAL))


def _init_stability(rating: int) -> float:
    return max(FSRS_W[rating - 1], 0.1)


def _init_difficulty(rating: int) -> float:
    return _clamp(FSRS_W[4] - math.exp(FSRS_W[5] * (rating - 1)) + 1, 1.0, 10.0)


def _next_difficulty(difficulty: float, rating: int) -> float:
    delta = -FSRS_W[6] * (rating - 3)
    updated = difficulty + delta * (10 - difficulty) / 9
    # Reversión a la media: evita que una tarjeta quede marcada como imposible
    # para siempre por un par de fallos puntuales.
    reverted = FSRS_W[7] * _init_difficulty(RATING_EASY) + (1 - FSRS_W[7]) * updated
    return _clamp(reverted, 1.0, 10.0)


def _stability_on_recall(difficulty: float, stability: float, r: float, rating: int) -> float:
    hard_penalty = FSRS_W[15] if rating == RATING_HARD else 1.0
    easy_bonus = FSRS_W[16] if rating == RATING_EASY else 1.0
    growth = (
        math.exp(FSRS_W[8])
        * (11 - difficulty)
        * (stability ** -FSRS_W[9])
        * (math.exp((1 - r) * FSRS_W[10]) - 1)
        * hard_penalty
        * easy_bonus
    )
    return stability * (1 + growth)


def _stability_on_lapse(difficulty: float, stability: float, r: float) -> float:
    long_term = (
        FSRS_W[11]
        * (difficulty ** -FSRS_W[12])
        * (((stability + 1) ** FSRS_W[13]) - 1)
        * math.exp((1 - r) * FSRS_W[14])
    )
    # FSRS-5: tras un fallo la estabilidad nunca sube. El tope evita que una
    # tarjeta olvidada quede programada más lejos que antes de olvidarla.
    ceiling = stability / math.exp(FSRS_W[17] * FSRS_W[18])
    return max(min(long_term, ceiling), 0.01)


def schedule(card: dict, rating: int, retention: float = DEFAULT_RETENTION,
             today: datetime.date | None = None) -> dict:
    """
    Calcula el nuevo estado FSRS de una tarjeta tras calificarla.

    Devuelve los campos a persistir: state, due, stability, difficulty,
    reps, lapses, last_review, y el `interval` en días (informativo).
    Trabaja con granularidad de DÍA: el repaso intradía de las tarjetas
    falladas lo resuelve la cola de estudio, no la fecha en la base.
    """
    today = today or datetime.date.today()
    state = card.get("state") or "new"
    stability = float(card.get("stability") or 0)
    difficulty = float(card.get("difficulty") or 0)
    reps = int(card.get("reps") or 0)
    lapses = int(card.get("lapses") or 0)

    last_review = card.get("last_review")
    if last_review:
        try:
            prev = datetime.date.fromisoformat(str(last_review)[:10])
            elapsed = max((today - prev).days, 0)
        except ValueError:
            elapsed = 0
    else:
        elapsed = 0

    if state == "new" or stability <= 0:
        stability = _init_stability(rating)
        difficulty = _init_difficulty(rating)
    else:
        r = retrievability(elapsed, stability)
        if rating == RATING_AGAIN:
            stability = _stability_on_lapse(difficulty, stability, r)
            lapses += 1
        else:
            stability = _stability_on_recall(difficulty, stability, r, rating)
        difficulty = _next_difficulty(difficulty, rating)

    if rating == RATING_AGAIN:
        # Se vuelve a ver hoy mismo, dentro de la sesión.
        new_state = "relearning" if state in ("review", "relearning") else "learning"
        interval = 0
    else:
        new_state = "review"
        interval = interval_for(stability, retention)

    reps += 1
    due = today + datetime.timedelta(days=interval)

    return {
        "state": new_state,
        "due": due.isoformat(),
        "stability": round(stability, 4),
        "difficulty": round(difficulty, 4),
        "reps": reps,
        "lapses": lapses,
        "last_review": datetime.datetime.now().isoformat(timespec="seconds"),
        "interval": interval,
        "elapsed_days": elapsed,
    }


def preview_intervals(card: dict, retention: float = DEFAULT_RETENTION) -> dict:
    """Los cuatro intervalos posibles, para pintarlos en los botones."""
    out = {}
    for rating, name in ((1, "again"), (2, "hard"), (3, "good"), (4, "easy")):
        out[name] = schedule(dict(card), rating, retention)["interval"]
    return out


# --------------------------------------------------------------------------- #
# Anki — import
# --------------------------------------------------------------------------- #
_FIELD_SEP = "\x1f"
_CLOZE_RE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.DOTALL)


class AnkiFormatError(RuntimeError):
    """El .apkg no se pudo leer (formato nuevo comprimido, o archivo corrupto)."""


def _cloze_indices(text: str) -> list[int]:
    return sorted({int(m.group(1)) for m in _CLOZE_RE.finditer(text or "")})


def render_cloze(text: str, index: int) -> tuple[str, str]:
    """
    Convierte un texto cloze en (frente, reverso) para un hueco concreto.

    El hueco `index` se tapa con [...] y los demás se revelan, que es como
    Anki lo presenta. El reverso muestra el texto completo.
    """
    def front_sub(m: re.Match) -> str:
        if int(m.group(1)) == index:
            hint = m.group(3)
            return f"[{hint}]" if hint else "[…]"
        return m.group(2)

    def back_sub(m: re.Match) -> str:
        if int(m.group(1)) == index:
            return f"<b>{m.group(2)}</b>"
        return m.group(2)

    return _CLOZE_RE.sub(front_sub, text), _CLOZE_RE.sub(back_sub, text)


class AnkiImporter:
    """Lee un .apkg y devuelve sus notas ya convertidas a tarjetas."""

    @staticmethod
    def _open_collection(apkg_path: str, workdir: str) -> str:
        with zipfile.ZipFile(apkg_path) as zf:
            names = zf.namelist()
            for candidate in ("collection.anki2", "collection.anki21"):
                if candidate in names:
                    zf.extract(candidate, workdir)
                    return os.path.join(workdir, candidate)
            if "collection.anki21b" in names:
                raise AnkiFormatError(
                    f"{os.path.basename(apkg_path)} usa el formato nuevo comprimido "
                    "(collection.anki21b). Vuelve a exportarlo desde Anki marcando "
                    "«Soporte para versiones anteriores de Anki»."
                )
            raise AnkiFormatError(
                f"{os.path.basename(apkg_path)} no contiene una colección Anki reconocible."
            )

    @classmethod
    def read(cls, apkg_path: str) -> dict:
        """
        Devuelve {'deck': str, 'topic': str, 'cards': [...]}.

        'topic' sale del nombre del mazo ('Neurología::Vértigo' -> 'Vértigo'):
        es el nombre canónico del tema, más confiable que el del archivo.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as workdir:
            col_path = cls._open_collection(apkg_path, workdir)
            conn = sqlite3.connect(col_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT models, decks FROM col").fetchone()
                models = json.loads(row["models"])
                decks = json.loads(row["decks"])

                deck_names = [d["name"] for d in decks.values() if d["name"] != "Default"]
                deck_name = deck_names[0] if deck_names else os.path.splitext(
                    os.path.basename(apkg_path))[0]
                topic = deck_name.split("::")[-1].strip()

                # type 1 = cloze en el esquema de Anki.
                is_cloze = {int(mid): (m.get("type") == 1) for mid, m in models.items()}

                cards: list[dict] = []
                for note in conn.execute("SELECT guid, mid, tags, flds FROM notes"):
                    fields = (note["flds"] or "").split(_FIELD_SEP)
                    tags = " ".join((note["tags"] or "").split())
                    if is_cloze.get(note["mid"]):
                        text = fields[0] if fields else ""
                        for idx in _cloze_indices(text) or [1]:
                            front, back = render_cloze(text, idx)
                            cards.append({
                                "kind": "cloze", "front": front, "back": back,
                                "cloze_index": idx, "tags": tags, "anki_guid": note["guid"],
                            })
                    else:
                        cards.append({
                            "kind": "basic",
                            "front": fields[0] if len(fields) > 0 else "",
                            "back": fields[1] if len(fields) > 1 else "",
                            "cloze_index": 0, "tags": tags, "anki_guid": note["guid"],
                        })
                return {"deck": deck_name, "topic": topic, "cards": cards}
            finally:
                conn.close()


# --------------------------------------------------------------------------- #
# Anki — export
# --------------------------------------------------------------------------- #
# IDs fijos: si cambiaran entre exportaciones, Anki trataría cada export como
# un tipo de nota distinto y duplicaría todo en la colección de destino.
_EXPORT_MODEL_ID = 1607392319001
_EXPORT_DECK_BASE = 2059400110000


class AnkiExporter:
    """Genera un .apkg a partir de las tarjetas de un tema."""

    @staticmethod
    def export(topic_title: str, cards: list[dict], out_path: str) -> str:
        try:
            import genanki
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise RuntimeError(
                "Falta genanki. Instálalo con: pip install genanki"
            ) from exc

        model = genanki.Model(
            _EXPORT_MODEL_ID,
            "Neuro Basico",
            fields=[{"name": "Pregunta"}, {"name": "Respuesta"}, {"name": "Tema"}],
            templates=[{
                "name": "Tarjeta",
                "qfmt": "{{Pregunta}}",
                "afmt": '{{FrontSide}}<hr id="answer">{{Respuesta}}'
                        '<div class="tema">{{Tema}}</div>',
            }],
            css=(
                ".card{font-family:-apple-system,Helvetica,Arial;font-size:19px;"
                "text-align:left;color:#1c1c1e;background:#fff;padding:18px;line-height:1.55}"
                ".tema{margin-top:14px;font-size:13px;color:#8e8e93}"
            ),
        )

        deck_id = _EXPORT_DECK_BASE + (abs(hash(topic_title)) % 100000)
        deck = genanki.Deck(deck_id, f"Neurología::{topic_title}")
        for card in cards:
            deck.add_note(genanki.Note(
                model=model,
                fields=[card.get("front", ""), card.get("back", ""), topic_title],
                tags=[t for t in (card.get("tags") or "").split() if t] or None,
            ))

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        genanki.Package(deck).write_to_file(out_path)
        return out_path


# --------------------------------------------------------------------------- #
# DatabaseManager
# --------------------------------------------------------------------------- #
class DatabaseManager:
    """Persistencia SQLite, segura entre hilos (uvicorn corre los endpoints
    sync en un threadpool). Conexión compartida + Lock, igual que EnglishApp."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._lock = threading.Lock()
        self._apply_schema()

    def _apply_schema(self) -> None:
        if not os.path.exists(SCHEMA_PATH):
            raise FileNotFoundError(f"No se encontró el esquema: {SCHEMA_PATH}")
        with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
            script = fh.read()
        # La migración va ANTES del script: el esquema crea un índice sobre
        # topics.folder_id, y sobre una base antigua esa columna todavía no
        # existe. Al revés, el CREATE INDEX falla y la app no arranca.
        self._migrate()
        with self._lock:
            self.conn.executescript(script)
            self.conn.commit()

    def _migrate(self) -> None:
        """
        Columnas nuevas sobre bases ya creadas.

        SQLite no tiene ADD COLUMN IF NOT EXISTS, así que se comprueba a mano.
        Sobre una base recién creada no hay nada que migrar: las tablas aún no
        existen y el esquema las creará ya completas.
        """
        with self._lock:
            tables = {r["name"] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "topics" in tables:
                cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(topics)")}
                if "folder_id" not in cols:
                    self.conn.execute("ALTER TABLE topics ADD COLUMN folder_id INTEGER")
            self.conn.commit()

    # --------------------------------------------------------------- carpetas
    def list_folders(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT f.*, COUNT(t.id) AS temas
                     FROM folders f LEFT JOIN topics t ON t.folder_id = f.id
                    GROUP BY f.id ORDER BY f.sort_order, f.name""").fetchall()
        return [dict(r) for r in rows]

    def ensure_folder(self, name: str, color: str = "#c9a86a") -> int:
        """Crea la carpeta si no existe (por nombre). Devuelve su id."""
        with self._lock:
            row = self.conn.execute(
                "SELECT id FROM folders WHERE name = ?", (name,)).fetchone()
            if row:
                return row["id"]
            cur = self.conn.execute(
                "INSERT INTO folders (name, color) VALUES (?, ?)", (name, color))
            self.conn.commit()
            return cur.lastrowid

    def rename_folder(self, folder_id: int, name: str, color: str | None = None) -> bool:
        with self._lock:
            if color:
                cur = self.conn.execute(
                    "UPDATE folders SET name = ?, color = ? WHERE id = ?",
                    (name, color, folder_id))
            else:
                cur = self.conn.execute(
                    "UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
            self.conn.commit()
            return cur.rowcount > 0

    def delete_folder(self, folder_id: int) -> bool:
        """Borra la carpeta. Los temas NO se borran: quedan sin carpeta."""
        with self._lock:
            cur = self.conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def move_topic(self, topic_id: int, folder_id: int | None) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE topics SET folder_id = ? WHERE id = ?", (folder_id, topic_id))
            self.conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ notas
    def list_notes(self, topic_id: int, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM notes WHERE topic_id = ?"
        params: list = [topic_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY page, created_at"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def add_note(self, topic_id: int, kind: str = "sticky", asset_id: int | None = None,
                 page: int = 1, x: float = 0.5, y: float = 0.5,
                 text: str = "", color: str = "amarillo") -> int:
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO notes (topic_id, asset_id, kind, page, x, y, text, color)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (topic_id, asset_id, kind, page,
                 _clamp(x, 0.0, 1.0), _clamp(y, 0.0, 1.0), text, color))
            self.conn.commit()
            return cur.lastrowid

    def update_note(self, note_id: int, **fields) -> bool:
        allowed = {"text", "color", "x", "y", "page"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return False
        if "x" in sets:
            sets["x"] = _clamp(float(sets["x"]), 0.0, 1.0)
        if "y" in sets:
            sets["y"] = _clamp(float(sets["y"]), 0.0, 1.0)
        clause = ", ".join(f"{k} = ?" for k in sets)
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE notes SET {clause}, updated_at = datetime('now') WHERE id = ?",
                (*sets.values(), note_id))
            self.conn.commit()
            return cur.rowcount > 0

    def delete_note(self, note_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            self.conn.commit()
            return cur.rowcount > 0

    # ---------------------------------------------------------------- ajustes
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
            self.conn.commit()

    def get_int_setting(self, key: str, default: int) -> int:
        try:
            return int(self.get_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    def retention(self) -> float:
        try:
            return float(self.get_setting("retention", str(DEFAULT_RETENTION)))
        except (TypeError, ValueError):
            return DEFAULT_RETENTION

    # ----------------------------------------------------------------- temas
    def ensure_topic(self, title: str, sort_order: int = 0,
                     folder_id: int | None = None) -> int:
        """
        Devuelve el id del tema, creándolo si hace falta.

        `folder_id` se aplica al crear el tema y también a un tema existente que
        haya quedado SIN carpeta. Un tema que ya está en una carpeta no se toca:
        re-escanear el material no debe deshacer la organización que hizo
        Mariela.
        """
        slug = slugify(title)
        with self._lock:
            row = self.conn.execute(
                "SELECT id, folder_id FROM topics WHERE slug = ?", (slug,)).fetchone()
            if row:
                if folder_id and row["folder_id"] is None:
                    self.conn.execute("UPDATE topics SET folder_id = ? WHERE id = ?",
                                      (folder_id, row["id"]))
                    self.conn.commit()
                return row["id"]
            cur = self.conn.execute(
                "INSERT INTO topics (slug, title, sort_order, folder_id) VALUES (?, ?, ?, ?)",
                (slug, title, sort_order, folder_id))
            self.conn.commit()
            return cur.lastrowid

    def now_sql(self) -> str:
        """La hora actual con el mismo formato que los DEFAULT datetime('now')."""
        with self._lock:
            return self.conn.execute("SELECT datetime('now') AS n").fetchone()["n"]

    def list_topics(self) -> list[dict]:
        """Temas con el conteo de tarjetas, cuántas tocan hoy y si son nuevos."""
        today = datetime.date.today().isoformat()
        baseline = self.get_setting("baseline_at")
        with self._lock:
            limite = self.conn.execute(
                "SELECT datetime('now', ?) AS d", (f"-{NEW_DAYS} days",)).fetchone()["d"]
            rows = self.conn.execute(
                """
                SELECT t.id, t.slug, t.title, t.area, t.sort_order, t.folder_id,
                       t.created_at,
                       (SELECT MAX(a.created_at) FROM assets a WHERE a.topic_id = t.id) AS material_at,
                       COUNT(c.id)                                        AS total,
                       SUM(c.state = 'new')                               AS nuevas,
                       SUM(c.state != 'new' AND c.due <= ? AND NOT c.suspended) AS vencidas
                  FROM topics t
                  LEFT JOIN cards c ON c.topic_id = t.id
                 GROUP BY t.id
                 ORDER BY t.sort_order, t.title
                """, (today,)).fetchall()
        topics = []
        for row in rows:
            topic = dict(row)
            topic["total"] = topic["total"] or 0
            topic["nuevas"] = topic["nuevas"] or 0
            topic["vencidas"] = topic["vencidas"] or 0
            # 'nuevo': el tema llegó después de la biblioteca de partida;
            # 'actualizado': el tema ya existía y le llegó material nuevo
            # (otro resumen, un artículo). Ambos caducan a los NEW_DAYS días.
            reciente = lambda ts: bool(baseline and ts and ts > baseline and ts >= limite)
            topic["is_new"] = reciente(topic["created_at"])
            topic["is_updated"] = (not topic["is_new"]) and reciente(topic["material_at"])
            topic["assets"] = self.topic_assets(topic["id"])
            topics.append(topic)
        return topics

    def get_topic(self, topic_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        if not row:
            return None
        topic = dict(row)
        topic["assets"] = self.topic_assets(topic_id)
        return topic

    # ------------------------------------------------------------ materiales
    def topic_assets(self, topic_id: int) -> dict:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, kind, path, filename, ext, bytes, score, matched_by "
                "FROM assets WHERE topic_id = ? ORDER BY kind", (topic_id,)).fetchall()
        assets: dict[str, list[dict]] = {}
        for row in rows:
            assets.setdefault(row["kind"], []).append(dict(row))
        return assets

    def add_asset(self, topic_id: int, kind: str, path: str, score: float = 1.0,
                  matched_by: str = "auto") -> None:
        stat = os.stat(path) if os.path.exists(path) else None
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO assets (topic_id, kind, path, filename, ext, bytes, score, matched_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    topic_id = excluded.topic_id, kind = excluded.kind,
                    score = excluded.score, matched_by = excluded.matched_by
                """,
                (topic_id, kind, path, os.path.basename(path),
                 os.path.splitext(path)[1].lstrip(".").lower(),
                 stat.st_size if stat else 0, score, matched_by))
            self.conn.commit()

    def get_asset(self, asset_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return dict(row) if row else None

    def add_orphan(self, path: str, kind: str, guess_id: int | None, score: float) -> None:
        stat = os.stat(path) if os.path.exists(path) else None
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO orphan_files (path, filename, kind, ext, bytes,
                                          best_guess_topic_id, best_score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    best_guess_topic_id = excluded.best_guess_topic_id,
                    best_score = excluded.best_score
                """,
                (path, os.path.basename(path), kind,
                 os.path.splitext(path)[1].lstrip(".").lower(),
                 stat.st_size if stat else 0, guess_id, score))
            self.conn.commit()

    def list_orphans(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT o.*, t.title AS guess_title FROM orphan_files o "
                "LEFT JOIN topics t ON t.id = o.best_guess_topic_id "
                "ORDER BY o.filename").fetchall()
        return [dict(r) for r in rows]

    def assign_orphan(self, orphan_id: int, topic_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM orphan_files WHERE id = ?", (orphan_id,)).fetchone()
        if not row:
            return False
        self.add_asset(topic_id, row["kind"], row["path"], score=1.0, matched_by="manual")
        with self._lock:
            self.conn.execute("DELETE FROM orphan_files WHERE id = ?", (orphan_id,))
            self.conn.commit()
        return True

    # -------------------------------------------------------------- tarjetas
    def upsert_cards(self, topic_id: int, cards: list[dict], source: str = "anki") -> dict:
        """
        Inserta tarjetas nuevas y respeta las que ya existen.

        Reimportar un .apkg actualizado NO reinicia el progreso: se emparejan
        por (anki_guid, cloze_index) y solo se actualiza el texto.
        """
        added = updated = 0
        today = datetime.date.today().isoformat()
        with self._lock:
            for card in cards:
                guid = card.get("anki_guid")
                existing = None
                if guid:
                    existing = self.conn.execute(
                        "SELECT id FROM cards WHERE anki_guid = ? AND cloze_index = ?",
                        (guid, card.get("cloze_index", 0))).fetchone()
                if existing:
                    self.conn.execute(
                        "UPDATE cards SET front = ?, back = ?, tags = ? WHERE id = ?",
                        (card.get("front", ""), card.get("back", ""),
                         card.get("tags", ""), existing["id"]))
                    updated += 1
                else:
                    self.conn.execute(
                        """
                        INSERT INTO cards (topic_id, kind, front, back, cloze_index,
                                           tags, source, anki_guid, state, due)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
                        """,
                        (topic_id, card.get("kind", "basic"), card.get("front", ""),
                         card.get("back", ""), card.get("cloze_index", 0),
                         card.get("tags", ""), source, guid, today))
                    added += 1
            self.conn.commit()
        return {"added": added, "updated": updated}

    def topic_cards(self, topic_id: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM cards WHERE topic_id = ? ORDER BY id", (topic_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_card(self, card_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return dict(row) if row else None

    def new_today(self) -> int:
        """Tarjetas nuevas ya introducidas hoy (primer repaso de una tarjeta)."""
        today = datetime.date.today().isoformat()
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM reviews "
                "WHERE day = ? AND state_before = 'new'", (today,)).fetchone()
        return row["n"] if row else 0

    def reviews_today(self) -> int:
        today = datetime.date.today().isoformat()
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM reviews "
                "WHERE day = ? AND state_before != 'new'", (today,)).fetchone()
        return row["n"] if row else 0

    def _scope(self, topic_id: int | None, folder_id: int | None) -> tuple[str, tuple]:
        """
        Cláusula SQL que acota la cola a un tema o a una carpeta.

        Va DENTRO de la consulta, no filtrando después: filtrar el resultado ya
        recortado por LIMIT descarta fichas que sí tocaban y deja la carpeta a
        medio repasar.
        """
        if topic_id:
            return "AND c.topic_id = ?", (topic_id,)
        if folder_id:
            return ("AND c.topic_id IN (SELECT id FROM topics WHERE folder_id = ?)",
                    (folder_id,))
        return "", ()

    def build_queue(self, topic_id: int | None = None, limit: int = 60,
                    folder_id: int | None = None, mode: str = "normal") -> list[dict]:
        """
        Cola de estudio.

        modo 'normal' — lo de hoy. Orden: fallado en la sesión → vencido de más
            atrás → nuevas, hasta el tope diario. Las vencidas van antes que las
            nuevas a propósito: introducir material nuevo mientras se acumula
            atraso es la forma más rápida de ahogarse.

        modo 'libre' — repaso extra. Devuelve fichas sin mirar la fecha ni los
            topes, barajadas. Sirve para volver a pasar un tema el mismo día;
            NO altera la programación (ver `grade`).
        """
        where, params = self._scope(topic_id, folder_id)

        if mode == "libre":
            with self._lock:
                rows = self.conn.execute(
                    f"""SELECT c.* FROM cards c
                         WHERE NOT c.suspended {where}
                         ORDER BY RANDOM() LIMIT ?""", (*params, limit)).fetchall()
            return [dict(r) for r in rows]

        today = datetime.date.today().isoformat()
        new_left = max(self.get_int_setting("new_limit", DEFAULT_NEW_LIMIT) - self.new_today(), 0)
        rev_left = max(self.get_int_setting("review_limit", DEFAULT_REVIEW_LIMIT)
                       - self.reviews_today(), 0)

        with self._lock:
            learning = self.conn.execute(
                f"""SELECT c.* FROM cards c
                     WHERE c.state IN ('learning', 'relearning') AND NOT c.suspended
                       AND c.due <= ? {where}
                     ORDER BY c.last_review LIMIT ?""",
                (today, *params, limit)).fetchall()

            due = self.conn.execute(
                f"""SELECT c.* FROM cards c
                     WHERE c.state = 'review' AND NOT c.suspended
                       AND c.due <= ? {where}
                     ORDER BY c.due, c.id LIMIT ?""",
                (today, *params, rev_left)).fetchall()

            new = self.conn.execute(
                f"""SELECT c.* FROM cards c
                     WHERE c.state = 'new' AND NOT c.suspended {where}
                     ORDER BY c.id LIMIT ?""",
                (*params, new_left)).fetchall()

        queue = [dict(r) for r in learning] + [dict(r) for r in due] + [dict(r) for r in new]
        return queue[:limit]

    def scope_counts(self, topic_id: int | None = None,
                     folder_id: int | None = None) -> dict:
        """Cuántas fichas hay en total y cuántas tocan hoy, para un tema o carpeta."""
        today = datetime.date.today().isoformat()
        where, params = self._scope(topic_id, folder_id)
        with self._lock:
            row = self.conn.execute(
                f"""SELECT COUNT(*) AS total,
                           SUM(c.state = 'new') AS nuevas,
                           SUM(c.state != 'new' AND c.due <= ? AND NOT c.suspended) AS vencidas
                      FROM cards c WHERE 1 {where}""", (today, *params)).fetchone()
        return {"total": row["total"] or 0, "nuevas": row["nuevas"] or 0,
                "vencidas": row["vencidas"] or 0}

    def grade(self, card_id: int, rating: int) -> dict | None:
        """Califica una tarjeta, persiste el nuevo estado y registra el repaso."""
        card = self.get_card(card_id)
        if not card:
            return None
        rating = int(_clamp(int(rating), 1, 4))
        result = schedule(card, rating, self.retention())

        with self._lock:
            self.conn.execute(
                """UPDATE cards SET state = ?, due = ?, stability = ?, difficulty = ?,
                                    reps = ?, lapses = ?, last_review = ?
                    WHERE id = ?""",
                (result["state"], result["due"], result["stability"], result["difficulty"],
                 result["reps"], result["lapses"], result["last_review"], card_id))
            self.conn.execute(
                """INSERT INTO reviews (card_id, rating, state_before, elapsed_days,
                                        scheduled_days, stability, difficulty)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (card_id, rating, card.get("state") or "new", result["elapsed_days"],
                 result["interval"], result["stability"], result["difficulty"]))
            self.conn.commit()
        return result

    # ------------------------------------------------------------- métricas
    def stats(self) -> dict:
        today = datetime.date.today().isoformat()
        with self._lock:
            totals = self.conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(state = 'new')    AS nuevas,
                          SUM(state = 'review') AS maduras,
                          SUM(state IN ('learning','relearning')) AS aprendiendo,
                          SUM(state != 'new' AND due <= ? AND NOT suspended) AS vencidas
                     FROM cards""", (today,)).fetchone()
            topics = self.conn.execute("SELECT COUNT(*) AS n FROM topics").fetchone()
            streak_rows = self.conn.execute(
                "SELECT DISTINCT day FROM reviews ORDER BY day DESC LIMIT 400").fetchall()

        # Racha: días consecutivos con al menos un repaso, contando hacia atrás
        # desde hoy (o desde ayer, si hoy todavía no se estudia).
        days = [r["day"] for r in streak_rows]
        streak = 0
        cursor = datetime.date.today()
        if days and days[0] != cursor.isoformat():
            cursor -= datetime.timedelta(days=1)
        day_set = set(days)
        while cursor.isoformat() in day_set:
            streak += 1
            cursor -= datetime.timedelta(days=1)

        out = {k: (totals[k] or 0) for k in
               ("total", "nuevas", "maduras", "aprendiendo", "vencidas")}
        out["temas"] = topics["n"] or 0
        out["repasos_hoy"] = self.reviews_today()
        out["nuevas_hoy"] = self.new_today()
        out["nuevas_limite"] = self.get_int_setting("new_limit", DEFAULT_NEW_LIMIT)
        out["racha"] = streak
        return out


# --------------------------------------------------------------------------- #
# Ingesta
# --------------------------------------------------------------------------- #
def _classify(path: str, content_dir: str) -> tuple[str, str] | None:
    """
    Decide si un archivo es presentación, resumen o mazo, y a qué sección
    pertenece. Devuelve (sección, tipo) o None.

    El material está organizado por SECCIONES —'Neuro', 'Generalidades de
    MI'—, cada una con sus presentaciones sueltas y sus subcarpetas 'Anki' y
    'Resumenes PDF'. Una sección se convierte en una carpeta de la app.

    Se mira la subcarpeta RELATIVA, no el nombre del directorio padre: la raíz
    se llama 'PresentacionesResumen' y contiene la subcadena 'resumen', así
    que buscarla ahí clasificaría mal las presentaciones.
    """
    ext = os.path.splitext(path)[1].lower()
    rel = os.path.relpath(path, content_dir)
    carpetas = rel.split(os.sep)[:-1]        # directorios entre la raíz y el archivo

    def es_subcarpeta_conocida(nombre: str) -> bool:
        n = strip_accents(nombre).lower()
        return n.startswith("resumen") or n.startswith("anki") or n.startswith("articulo")

    # Dos disposiciones posibles y hay que distinguirlas por el NOMBRE del
    # primer directorio, no por cuántos haya: 'Neuro/Vertigo.pdf' tiene un
    # solo directorio y sí es una sección, mientras que 'Anki/Vertigo.apkg'
    # también tiene uno y no lo es.
    if not carpetas:
        seccion, subdir = "", ""
    elif es_subcarpeta_conocida(carpetas[0]):
        seccion, subdir = "", strip_accents(carpetas[0]).lower()
    else:
        seccion = carpetas[0]
        subdir = strip_accents(carpetas[1]).lower() if len(carpetas) > 1 else ""

    if ext in DECK_EXTS:
        return (seccion, "anki")
    if subdir.startswith("resumen"):
        return (seccion, "resumen") if ext in SUMMARY_EXTS else None
    if subdir.startswith("anki"):
        return None                      # ya cubierto por la extensión .apkg
    if subdir.startswith("articulo"):
        # artículos relacionados que Mariela comparte: PDF de lectura, no fichas
        return (seccion, "articulo") if ext == ".pdf" else None
    if subdir:
        return None                      # subcarpeta desconocida: se ignora
    if ext == ".pdf" and not _pdf_apaisado(path):
        return None                      # apunte o tesis, no una presentación
    if ext in PRESENTATION_EXTS:
        return (seccion, "presentacion")
    return None


def _pdf_apaisado(path: str) -> bool:
    """
    True si la página del PDF es más ancha que alta.

    Las presentaciones exportadas a PDF son apaisadas; los apuntes y las tesis
    vienen en vertical (carta o A4). Sin esta comprobación, la tesis que vive
    junto al material acabaría convertida en un tema de estudio.
    """
    try:
        info = subprocess.run(["pdfinfo", path], capture_output=True,
                              text=True, timeout=20, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return True                      # sin poppler, no se descarta nada
    for linea in info.splitlines():
        if linea.startswith("Page size:"):
            nums = re.findall(r"[\d.]+", linea)
            if len(nums) >= 2:
                return float(nums[0]) > float(nums[1])
    return True


def scan_content(content_dir: str = CONTENT_DIR) -> dict[str, dict[str, list[str]]]:
    """
    Recorre la carpeta de material y agrupa por SECCIÓN y tipo.

    Devuelve {sección: {'presentacion': [...], 'resumen': [...], 'anki': [...],
    'articulo': [...]}}.
    Cada sección se convertirá en una carpeta de la app.
    """
    found: dict[str, dict[str, list[str]]] = {}
    if not os.path.isdir(content_dir):
        return found
    for path in sorted(glob.glob(os.path.join(content_dir, "**", "*"), recursive=True)):
        if not os.path.isfile(path) or os.path.basename(path).startswith("."):
            continue
        clasificado = _classify(path, content_dir)
        if not clasificado:
            continue
        seccion, kind = clasificado
        found.setdefault(seccion, {"presentacion": [], "resumen": [], "anki": [], "articulo": []})
        found[seccion][kind].append(path)
    return found


def ingest(db: DatabaseManager, content_dir: str = CONTENT_DIR) -> dict:
    """
    Escanea el material y arma los temas. Idempotente: se puede correr las
    veces que haga falta sin duplicar ni perder progreso.

    Orden deliberado:
      1. Los .apkg primero — su nombre de mazo ('Neurología::Vértigo') es el
         nombre canónico del tema. Sin ellos habría que adivinarlo del archivo.
      2. Presentaciones y resúmenes después, emparejados por tokens contra los
         temas ya creados. Lo que no llegue al umbral queda como huérfano
         visible en la UI, nunca descartado en silencio.
    """
    report = {"temas": 0, "tarjetas_nuevas": 0, "tarjetas_actualizadas": 0,
              "materiales": 0, "huerfanos": 0, "errores": []}

    secciones = scan_content(content_dir)
    if not secciones:
        report["errores"].append(f"No se encontró material en {content_dir}")
        return report

    # Paletas para que cada carpeta se distinga de un vistazo en la barra
    # lateral. Se reparten por orden de aparición.
    colores = ["#2f6b5e", "#b4453b", "#4a7fb5", "#c9a86a", "#7c5aa8", "#3d8a5f"]

    for n, (seccion, files) in enumerate(sorted(secciones.items())):
        # Cada sección del material es una carpeta de la app. Mariela puede
        # crear las suyas y mover los temas; esta solo evita que arranquen
        # sueltos.
        nombre = seccion or "Sin sección"
        folder_id = db.ensure_folder(nombre, colores[n % len(colores)])
        report.setdefault("carpetas", []).append(nombre)

        # --- 1. Archivos de fichas: definen los temas ---
        for order, apkg in enumerate(files["anki"]):
            try:
                deck = AnkiImporter.read(apkg)
            except (AnkiFormatError, sqlite3.Error, zipfile.BadZipFile) as exc:
                report["errores"].append(str(exc))
                continue
            topic_id = db.ensure_topic(deck["topic"], sort_order=order,
                                       folder_id=folder_id)
            db.add_asset(topic_id, "anki", apkg, score=1.0, matched_by="deck")
            counts = db.upsert_cards(topic_id, deck["cards"])
            report["temas"] += 1
            report["materiales"] += 1
            report["tarjetas_nuevas"] += counts["added"]
            report["tarjetas_actualizadas"] += counts["updated"]

        # --- 2. Presentaciones y resúmenes ---
        # Se emparejan SOLO contra los temas de su propia sección: si se
        # compararan contra todos, «Exploración Neurológica» de Neuro podría
        # robarle un archivo a un tema homónimo de otra sección.
        propios = [t for t in db.list_topics() if t["folder_id"] == folder_id]
        for kind in ("presentacion", "resumen", "articulo"):
            for path in files.get(kind, []):
                topic, score = match_topic(os.path.basename(path), propios)
                if topic:
                    db.add_asset(topic["id"], kind, path, score=score)
                    report["materiales"] += 1
                else:
                    guess, gs = match_topic(os.path.basename(path), propios, threshold=0.0)
                    db.add_orphan(path, kind, guess["id"] if guess else None, gs)
                    report["huerfanos"] += 1

    # Lo que encuentra el PRIMER escaneo es la biblioteca de partida, no una
    # novedad. A partir de aquí, un tema o material que aparece después (un
    # resumen nuevo que se despliega) se marca como nuevo durante NEW_DAYS.
    if not db.get_setting("baseline_at"):
        db.set_setting("baseline_at", db.now_sql())
    return report


# --------------------------------------------------------------------------- #
# Resúmenes en PDF: se renderizan a imagen en el servidor
#
# El visor de PDF del navegador es una caja opaca: no deja dibujar encima, así
# que los post-it no se podrían pegar sobre el papel. Renderizando cada página
# a PNG el navegador solo muestra un <img>, y sobre él va una capa normal donde
# las notas se posicionan en porcentaje. Sin librerías de JS ni CDN.
# --------------------------------------------------------------------------- #
class PdfRenderer:
    """Convierte páginas de PDF a PNG con poppler (pdftoppm), con caché."""

    @staticmethod
    def available() -> bool:
        from shutil import which
        return bool(which("pdftoppm"))

    @staticmethod
    def page_count(pdf_path: str) -> int:
        from shutil import which
        if not which("pdfinfo"):
            return 1
        try:
            out = subprocess.run(["pdfinfo", pdf_path], capture_output=True,
                                 text=True, timeout=20, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return 1
        for line in out.splitlines():
            if line.startswith("Pages:"):
                try:
                    return max(int(line.split(":", 1)[1].strip()), 1)
                except ValueError:
                    return 1
        return 1

    @staticmethod
    def _cache_key(pdf_path: str, page: int) -> str:
        # La fecha de modificación entra en la clave: si el PDF se reemplaza,
        # la clave cambia sola y no hay que invalidar nada a mano.
        try:
            mtime = int(os.path.getmtime(pdf_path))
        except OSError:
            mtime = 0
        raw = f"{os.path.abspath(pdf_path)}|{mtime}|{page}|{PAGE_DPI}"
        import hashlib
        return hashlib.sha1(raw.encode()).hexdigest()

    @classmethod
    def render(cls, pdf_path: str, page: int) -> str | None:
        """Devuelve la ruta del PNG de esa página, o None si no se pudo."""
        if not os.path.exists(pdf_path) or not cls.available():
            return None
        page = max(int(page), 1)
        os.makedirs(PAGE_CACHE_DIR, exist_ok=True)
        out_png = os.path.join(PAGE_CACHE_DIR, cls._cache_key(pdf_path, page) + ".png")
        if os.path.exists(out_png):
            return out_png

        # pdftoppm añade el sufijo '-N' al prefijo que se le pasa.
        prefix = out_png[:-4] + "-tmp"
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(PAGE_DPI),
                 "-f", str(page), "-l", str(page), pdf_path, prefix],
                capture_output=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError):
            return None

        for candidate in glob.glob(prefix + "*.png"):
            os.replace(candidate, out_png)
            return out_png
        return None


# --------------------------------------------------------------------------- #
# Carga de archivos desde la app
# --------------------------------------------------------------------------- #
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ áéíóúÁÉÍÓÚñÑüÜ()-]")


def save_upload(filename: str, data: bytes) -> str:
    """
    Guarda un archivo subido en la biblioteca de la app y devuelve su ruta.

    Solo se copian los PDF y los archivos de fichas: son ligeros. Las
    presentaciones se consultan donde ya viven y no pasan por aquí.
    """
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    clean = _SAFE_NAME.sub("_", os.path.basename(filename or "archivo")).strip() or "archivo"
    dest = os.path.join(LIBRARY_DIR, clean)
    stem, ext = os.path.splitext(dest)
    n = 2
    while os.path.exists(dest):
        dest = f"{stem} ({n}){ext}"
        n += 1
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest


def open_in_system_app(path: str) -> bool:
    """
    Abre un archivo con la app nativa (Keynote, PowerPoint, Vista Previa).

    Los .pptx de hasta 91 MB no se renderizan en el navegador; forzarlo sería
    lento y peor que la app que ya sabe abrirlos. La app es local, así que
    delegar al sistema es lo correcto aquí.
    """
    if not os.path.exists(path):
        return False
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=True)
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", path], check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False
