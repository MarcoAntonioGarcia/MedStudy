"""
core.py — Lógica de negocio de la App de Estudio, SIN framework de UI.

No importa FastAPI ni ninguna GUI: solo persistencia, ingesta, programación
de repasos e intercambio con Anki. Reutilizable por cualquier front.

Piezas:
    - normalize()/match_topic()  -> emparejar archivos con temas (nombres inconsistentes)
    - FSRS                       -> programación de repasos (reemplaza a SM-2)
    - AnkiImporter               -> .apkg -> tarjetas (Basic y Cloze)
    - AnkiExporter               -> tarjetas -> .apkg (genanki)
    - hash_password()/ruta_permitida() -> cuentas (scrypt) y archivos servibles
    - DatabaseManager            -> SQLite (contenido compartido; cuentas, progreso,
                                    notas y ajustes de cada persona)
    - ingest()                   -> escanea las carpetas y arma los temas

La unidad atómica del diseño es el TEMA. Ver ARCHITECTURE.md / SCHEMA.sql.
"""

from __future__ import annotations

import datetime
import glob
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.request
import zipfile
from html.parser import HTMLParser
import html

_log = logging.getLogger("estudio")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# APP_DB_PATH solo lo usan las pruebas, para no tocar nunca la base de verdad.
# Se lee al abrir la base, no al importar: así da igual qué módulo importe
# core primero.
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "database.db")
DB_PATH = os.environ.get("APP_DB_PATH") or DEFAULT_DB_PATH
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
# Cuántos pdftoppm pueden correr a la vez (ver PdfRenderer.render).
_RENDERS = threading.BoundedSemaphore(2)

# Carpeta con el material (resúmenes, mazos y artículos). Por defecto, contenido/.
# Solo se cambia con la variable de entorno, nunca desde la app: si un ajuste
# o una petición pudiera elegir qué carpeta se escanea, cualquiera con acceso
# a la API podría hacer que el servidor leyera y sirviera otra parte del disco.
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

# Ajustes que son de cada persona (su ritmo, su tema visual). Los demás, como
# baseline_at, son de la app entera y siguen en `settings`.
AJUSTES_DE_USUARIO = ("new_limit", "review_limit", "retention", "theme")

# Una sola sesión abierta por persona, como en EnglishApp: entrar cierra las
# demás. Compartir una cuenta se vuelve incómodo y se nota.
UNA_SESION_POR_USUARIO = True

# Tope de lo que puede ocupar la colección DESCOMPRIMIDA de un .apkg. Un zip
# de 1 MB puede inflarse a gigas; sin tope llenaría el disco del servidor.
MAX_COLECCION_ANKI = 300 * 1024 * 1024


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


# --------------------------------------------------------------------------- #
# HTML de las fichas: se limpia al importar
#
# Las fichas vienen de archivos de Anki (de cualquiera que comparta un mazo) y
# la app las pinta como HTML: sin limpiar, un mazo con <img onerror=…> o un
# enlace javascript: sería código ejecutándose en la sesión de quien estudia.
# La política de contenido ya lo frena; esto es la segunda barrera, en el
# servidor. Lista BLANCA: lo que no está aquí no pasa.
# --------------------------------------------------------------------------- #
_ETIQUETAS_PERMITIDAS = {
    "b", "strong", "i", "em", "u", "sub", "sup", "br", "p", "div", "span",
    "ul", "ol", "li", "table", "thead", "tbody", "tr", "td", "th", "img",
}
_ETIQUETAS_VACIAS = {"br", "img"}
# Su CONTENIDO también se tira, no solo la etiqueta.
_ETIQUETAS_CON_CONTENIDO_PROHIBIDO = {"script", "style", "iframe", "object", "embed",
                                      "template", "noscript", "svg", "math", "title"}
_RE_DATA_IMAGEN = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=\s]*$", re.I)
_RE_NUMERO = re.compile(r"^\d{1,3}$")


def _src_seguro(src: str) -> bool:
    """data:image/… o una ruta relativa del mismo sitio. Nada de http:,
    javascript:, '//otro.sitio' ni barras invertidas."""
    src = (src or "").strip()
    if not src or len(src) > 200_000:
        return False
    if _RE_DATA_IMAGEN.match(src):
        return True
    if "\\" in src or src.startswith("//") or any(ord(c) < 32 for c in src):
        return False
    # sin esquema: nada de 'algo:' antes de la primera barra, ? o #
    return ":" not in re.split(r"[/?#]", src, maxsplit=1)[0]


class _LimpiadorHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.salida: list[str] = []
        self.abiertas: list[str] = []
        self.saltando = 0               # dentro de <script>, <style>…

    def handle_starttag(self, tag, attrs):
        if tag in _ETIQUETAS_CON_CONTENIDO_PROHIBIDO:
            self.saltando += 1
            return
        if self.saltando or tag not in _ETIQUETAS_PERMITIDAS:
            return                      # <a>, <font>…: fuera la etiqueta, queda el texto
        limpios = []
        for nombre, valor in attrs:
            valor = valor or ""
            if tag == "img" and nombre == "src" and _src_seguro(valor):
                limpios.append((nombre, valor.strip()))
            elif tag == "img" and nombre == "alt":
                limpios.append((nombre, valor[:300]))
            elif tag in ("td", "th") and nombre in ("colspan", "rowspan") and _RE_NUMERO.match(valor):
                limpios.append((nombre, valor))
        if tag == "img" and not any(n == "src" for n, _ in limpios):
            return                      # una imagen sin origen seguro no se pinta
        attrs_html = "".join(f' {n}="{html.escape(v, quote=True)}"' for n, v in limpios)
        self.salida.append(f"<{tag}{attrs_html}>")
        if tag not in _ETIQUETAS_VACIAS:
            self.abiertas.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _ETIQUETAS_VACIAS and self.abiertas and self.abiertas[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _ETIQUETAS_CON_CONTENIDO_PROHIBIDO:
            self.saltando = max(self.saltando - 1, 0)
            return
        if self.saltando or tag not in self.abiertas:
            return
        # cierra también lo que quedó abierto dentro: el HTML sale bien anidado
        while self.abiertas:
            abierta = self.abiertas.pop()
            self.salida.append(f"</{abierta}>")
            if abierta == tag:
                break

    def handle_data(self, data):
        if not self.saltando:
            self.salida.append(html.escape(data, quote=False))

    def resultado(self) -> str:
        self.close()
        return "".join(self.salida) + "".join(f"</{t}>" for t in reversed(self.abiertas))


def limpiar_html(texto: str) -> str:
    """HTML de una ficha con solo etiquetas y atributos seguros (ver arriba)."""
    if not texto:
        return ""
    limpiador = _LimpiadorHTML()
    limpiador.feed(texto)
    return limpiador.resultado()


# Tiempo máximo para leer la base de un .apkg. Una consulta trampa (vistas o
# disparadores diseñados para tardar) no debe dejar un hilo del servidor colgado.
SEGUNDOS_LECTURA_ANKI = 20


class AnkiImporter:
    """Lee un .apkg y devuelve sus notas ya convertidas a tarjetas."""

    @staticmethod
    def _open_collection(apkg_path: str, workdir: str) -> str:
        with zipfile.ZipFile(apkg_path) as zf:
            names = zf.namelist()
            for candidate in ("collection.anki2", "collection.anki21"):
                if candidate in names:
                    if zf.getinfo(candidate).file_size > MAX_COLECCION_ANKI:
                        raise AnkiFormatError(
                            f"{os.path.basename(apkg_path)} es demasiado grande para importarlo.")
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
            # Solo lectura e inmutable: la base viene de fuera y no debe poder
            # escribir nada (ni un diario junto al archivo) ni cambiar al leerla.
            uri = "file:" + urllib.request.pathname2url(col_path) + "?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            limite = time.monotonic() + SEGUNDOS_LECTURA_ANKI
            # devolver algo verdadero aborta la consulta en curso
            conn.set_progress_handler(lambda: time.monotonic() > limite, 10_000)
            try:
                # 'col' y 'notes' tienen que ser TABLAS de verdad: una vista con
                # el mismo nombre podría esconder una consulta cualquiera.
                tablas = {r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('col', 'notes')")}
                if tablas != {"col", "notes"}:
                    raise AnkiFormatError(
                        f"{os.path.basename(apkg_path)} no contiene una colección Anki reconocible.")
                row = conn.execute("SELECT models, decks FROM col").fetchone()
                if row is None:
                    raise AnkiFormatError(
                        f"{os.path.basename(apkg_path)} no contiene una colección Anki reconocible.")
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
            except sqlite3.OperationalError as exc:
                if time.monotonic() > limite:
                    raise AnkiFormatError(
                        f"{os.path.basename(apkg_path)} tardó demasiado en leerse.") from exc
                raise
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
# Cuentas: contraseñas, nombres y correos
#
# Portado de EnglishApp, que ya corre en producción. Reglas de todo el bloque:
#   * la contraseña en claro no se guarda, no se registra y no se devuelve;
#   * de las sesiones y de los enlaces por correo se guarda el SHA-256 del
#     token, nunca el token: quien lea la base no puede suplantar a nadie;
#   * bloquear a alguien o cambiarle la contraseña le cierra las sesiones.
# --------------------------------------------------------------------------- #
# scrypt viene en la librería estándar: no hace falta bcrypt ni argon2. Los
# parámetros van GUARDADOS en el propio hash, así que subirlos más adelante no
# invalida las contraseñas que ya existen.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
LONGITUD_MINIMA_CLAVE = 10
# Sin tope, scrypt tendría que procesar una "contraseña" de megas en cada intento.
LONGITUD_MAXIMA_CLAVE = 256
LONGITUD_MINIMA_USUARIO = 3
LARGO_MAXIMO_NOMBRE = 60

_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Letras, números, punto, guion y guion bajo. Sin espacios ni acentos: un
# usuario con tilde se teclea mal y deja fuera a quien no sabe si la llevaba.
_RE_USUARIO = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def hash_password(plain: str) -> str:
    """Devuelve 'scrypt$N$r$p$salt$hash'. Nunca se guarda la contraseña en claro."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, dklen=32, maxmem=_SCRYPT_MAXMEM)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """Compara en tiempo constante: con == se filtraría cuántos bytes coinciden."""
    try:
        algo, n, r, p, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(plain.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(dk_hex) // 2,
                            maxmem=_SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False                      # hash vacío o con formato raro
    return hmac.compare_digest(dk.hex(), dk_hex)


def problema_con_la_clave(plain: str) -> str:
    """Mensaje de error si la contraseña no vale, o cadena vacía si vale."""
    if not isinstance(plain, str) or len(plain) < LONGITUD_MINIMA_CLAVE:
        return f"La contraseña debe tener al menos {LONGITUD_MINIMA_CLAVE} caracteres."
    if len(plain) > LONGITUD_MAXIMA_CLAVE:
        return f"La contraseña puede tener como mucho {LONGITUD_MAXIMA_CLAVE} caracteres."
    if plain.strip() != plain:
        return "La contraseña no puede empezar ni terminar con un espacio."
    return ""


def problema_con_el_usuario(nombre: str) -> str:
    nombre = (nombre or "").strip()
    if not nombre:
        return "Elige un nombre de usuario."
    if len(nombre) < LONGITUD_MINIMA_USUARIO:
        return f"El usuario debe tener al menos {LONGITUD_MINIMA_USUARIO} caracteres."
    if len(nombre) > 32:
        return "El usuario puede tener como mucho 32 caracteres."
    if not _RE_USUARIO.match(nombre):
        return ("Usa solo letras sin acentos, números, punto (.), guion (-) "
                "y guion bajo (_). Sin espacios.")
    return ""


def problema_con_el_nombre(nombre: str) -> str:
    """El nombre visible es texto libre, pero sin signos de HTML: aunque la
    pantalla ya escapa al pintar, si nunca entran en la base, un descuido
    futuro en otra pantalla no se convierte en un agujero."""
    nombre = (nombre or "").strip()
    if len(nombre) > LARGO_MAXIMO_NOMBRE:
        return f"El nombre puede tener como mucho {LARGO_MAXIMO_NOMBRE} caracteres."
    if any(ch in nombre for ch in "<>"):
        return "El nombre no puede llevar < ni >."
    return ""


def email_valido(email: str) -> bool:
    email = (email or "").strip()
    return len(email) <= 254 and bool(_RE_EMAIL.match(email))


def _ahora(desplazamiento_s: int = 0) -> str:
    """Fecha y hora del servidor como texto 'AAAA-MM-DD HH:MM:SS'.

    La pone Python y no SQLite (datetime('now')): así el formato es el mismo
    en SQLite y en Postgres, y las comparaciones de texto siguen valiendo.
    """
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() + desplazamiento_s))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Rutas que se pueden servir
# --------------------------------------------------------------------------- #
def ruta_permitida(path: str) -> bool:
    """¿Vive este archivo DENTRO de la carpeta de material o de la biblioteca?

    Las rutas de los materiales salen de la base, no de la petición; aun así se
    comprueba al servirlas (defensa en profundidad): una fila mal puesta, un
    enlace simbólico o un '..' no deben poder sacar del servidor un archivo
    cualquiera del disco. Se compara la ruta REAL, con los enlaces resueltos.
    """
    if not path:
        return False
    try:
        real = os.path.realpath(path)
        for base in (CONTENT_DIR, LIBRARY_DIR):
            raiz = os.path.realpath(base)
            if real != raiz and os.path.commonpath([real, raiz]) == raiz:
                return True
    except (OSError, ValueError):
        return False
    return False


# --------------------------------------------------------------------------- #
# DatabaseManager
# --------------------------------------------------------------------------- #
# Columnas de una ficha tal y como la ve UNA persona: el contenido es de todos
# (cards) y el estado FSRS es suyo (card_progress). Sin fila de progreso, la
# ficha es nueva para esa persona.
_CARD_COLS = """
    c.id, c.topic_id, c.kind, c.front, c.back, c.cloze_index, c.tags,
    c.source, c.anki_guid, c.created_at,
    COALESCE(p.state, 'new')    AS state,
    p.due                       AS due,
    COALESCE(p.stability, 0)    AS stability,
    COALESCE(p.difficulty, 0)   AS difficulty,
    COALESCE(p.reps, 0)         AS reps,
    COALESCE(p.lapses, 0)       AS lapses,
    p.last_review               AS last_review,
    COALESCE(p.suspended, 0)    AS suspended"""
# El user_id va en el ON del LEFT JOIN, no en el WHERE: en el WHERE dejaría
# fuera las fichas que esa persona todavía no ha visto.
_CARD_JOIN = "cards c LEFT JOIN card_progress p ON p.card_id = c.id AND p.user_id = ?"

# Columnas FSRS que antes vivían en `cards` (progreso global, de una sola
# persona). Se quitan al migrar una base vieja.
_COLUMNAS_FSRS_VIEJAS = ("state", "due", "stability", "difficulty", "reps",
                         "lapses", "last_review", "suspended")


class DatabaseManager:
    """Persistencia SQLite, segura entre hilos (uvicorn corre los endpoints
    sync en un threadpool). Conexión compartida + Lock, igual que EnglishApp."""

    CAMPOS_PRIVADOS = ("password_hash",)

    def __init__(self, db_path: str | None = None) -> None:
        db_path = db_path or os.environ.get("APP_DB_PATH") or DEFAULT_DB_PATH
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
        # La migración va ANTES del script: el esquema crea índices sobre
        # columnas nuevas (topics.folder_id, notes.user_id…) y sobre una base
        # antigua todavía no existen. Al revés, el CREATE INDEX falla y la app
        # no arranca.
        self._migrate()
        with self._lock:
            self.conn.executescript(script)
            self.conn.commit()

    def _migrate(self) -> None:
        """
        Pone al día una base ya creada. Idempotente: se corre en cada arranque.

        SQLite no tiene ADD COLUMN IF NOT EXISTS, así que se comprueba a mano.
        Sobre una base recién creada no hay nada que migrar: las tablas aún no
        existen y el esquema las creará ya completas.

        Etapa 1 (cuentas): el progreso pasa de `cards` (global) a
        `card_progress` (por persona); `reviews` y `notes` ganan user_id; los
        ajustes personales pasan a `user_settings`. El progreso global viejo
        no tiene dueño y se descarta (la base local no tenía repasos).
        """
        with self._lock:
            tables = {r["name"] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}

            def columnas(tabla: str) -> set[str]:
                return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({tabla})")}

            if "topics" in tables and "folder_id" not in columnas("topics"):
                self.conn.execute("ALTER TABLE topics ADD COLUMN folder_id INTEGER")

            if "cards" in tables:
                viejas = [c for c in _COLUMNAS_FSRS_VIEJAS if c in columnas("cards")]
                if viejas:
                    if "state" in viejas:
                        n = self.conn.execute(
                            "SELECT COUNT(*) AS n FROM cards WHERE state != 'new'").fetchone()["n"]
                        if n:
                            _log.warning("Migración: se descarta el progreso global de %d "
                                         "fichas (no tiene dueño).", n)
                    # Los índices viejos usan esas columnas: hay que quitarlos
                    # antes, o el DROP COLUMN falla.
                    self.conn.execute("DROP INDEX IF EXISTS idx_cards_due")
                    self.conn.execute("DROP INDEX IF EXISTS idx_cards_topic")
                    for col in viejas:
                        self.conn.execute(f"ALTER TABLE cards DROP COLUMN {col}")

            for tabla in ("reviews", "notes"):
                if tabla in tables and "user_id" not in columnas(tabla):
                    self.conn.execute(
                        f"ALTER TABLE {tabla} ADD COLUMN user_id INTEGER "
                        "REFERENCES users(id) ON DELETE CASCADE")

            if "email_tokens" in tables and "ip_grupo" not in columnas("email_tokens"):
                self.conn.execute(
                    "ALTER TABLE email_tokens ADD COLUMN ip_grupo TEXT NOT NULL DEFAULT ''")

            # Notas y repasos de antes de las cuentas (user_id NULL): son de
            # quien usaba la app entonces, que es quien la administra. Se le
            # asignan al primer admin en cuanto exista; hasta entonces se
            # quedan sin dueño (invisibles) y solo se anota cuántos hay.
            if "users" in tables:
                admin = self.conn.execute(
                    "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
                for tabla in ("notes", "reviews"):
                    if tabla not in tables:
                        continue
                    if admin:
                        cur = self.conn.execute(
                            f"UPDATE {tabla} SET user_id = ? WHERE user_id IS NULL", (admin["id"],))
                        if cur.rowcount:
                            _log.info("Migración: %d filas de %s sin dueño pasan al primer admin.",
                                      cur.rowcount, tabla)
                    else:
                        n = self.conn.execute(
                            f"SELECT COUNT(*) AS n FROM {tabla} WHERE user_id IS NULL").fetchone()["n"]
                        if n:
                            _log.warning("Migración: %d filas de %s sin dueño; se asignarán al "
                                         "primer admin cuando exista.", n, tabla)

            if "settings" in tables:
                # content_dir ya no es un ajuste (solo la variable de entorno), y
                # los personales viven ahora en user_settings.
                self.conn.execute(
                    "DELETE FROM settings WHERE key IN (?, ?, ?, ?, ?)",
                    ("content_dir", *AJUSTES_DE_USUARIO))
            self.conn.commit()

    # ============================================================ cuentas
    @classmethod
    def _publico(cls, row) -> dict:
        """La ficha de un usuario tal y como puede salir hacia el navegador."""
        d = dict(row)
        for campo in cls.CAMPOS_PRIVADOS:
            d.pop(campo, None)
        return d

    def get_user(self, user_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._publico(row) if row else None

    def find_user(self, login: str) -> dict | None:
        """Busca por usuario O por correo, sin distinguir mayúsculas.

        Devuelve la fila CRUDA (con el hash): solo la usan el login y el cambio
        de contraseña.
        """
        login = (login or "").strip()
        if not login:
            return None
        with self._lock:
            # lower() en los dos lados: en Postgres '=' distingue mayúsculas
            row = self.conn.execute(
                "SELECT * FROM users WHERE lower(username) = lower(?) "
                "OR lower(email) = lower(?)", (login, login)).fetchone()
        return dict(row) if row else None

    def find_user_by_email(self, email: str) -> dict | None:
        email = (email or "").strip().lower()
        if not email:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
        return dict(row) if row else None

    def create_user(self, username: str, password: str, email: str = "",
                    role: str = "user", status: str = "active",
                    nombre: str = "") -> int:
        """Alta de usuario. Devuelve el id nuevo.

        Lanza ValueError si el usuario, la contraseña, el nombre o el correo no
        valen, o si el usuario o el correo ya existen.
        """
        username = (username or "").strip()
        problema = (problema_con_el_usuario(username)
                    or problema_con_la_clave(password)
                    or problema_con_el_nombre(nombre))
        if problema:
            raise ValueError(problema)
        if role not in ("admin", "user"):
            raise ValueError("Rol desconocido.")
        if status not in ("pending", "active", "blocked"):
            raise ValueError("Estado desconocido.")
        email = (email or "").strip().lower()
        if email and not email_valido(email):
            raise ValueError("Ese correo no parece válido.")
        with self._lock:
            if self.conn.execute("SELECT 1 FROM users WHERE lower(username) = lower(?)",
                                 (username,)).fetchone():
                raise ValueError(f"El usuario «{username}» ya está ocupado.")
            if email and self.conn.execute(
                    "SELECT 1 FROM users WHERE lower(email) = lower(?)", (email,)).fetchone():
                raise ValueError("Ya hay una cuenta con ese correo.")
            cur = self.conn.execute(
                "INSERT INTO users (username, email, password_hash, role, status, nombre, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (username, email or None, hash_password(password), role, status,
                 (nombre or "").strip(), _ahora()))
            self.conn.commit()
            return cur.lastrowid

    def set_password(self, user_id: int, password: str) -> None:
        """Cambia la contraseña y cierra TODAS sus sesiones: si se cambia porque
        pudo filtrarse, dejar sesiones vivas no arreglaría nada."""
        problema = problema_con_la_clave(password)
        if problema:
            raise ValueError(problema)
        with self._lock:
            self.conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                              (hash_password(password), user_id))
            self._revocar_sesiones_locked(user_id)
            self.conn.commit()

    def set_user_status(self, user_id: int, status: str) -> bool:
        """'pending' | 'active' | 'blocked'. Cualquier estado que no sea activo
        cierra sus sesiones."""
        if status not in ("pending", "active", "blocked"):
            raise ValueError("Estado desconocido.")
        with self._lock:
            cur = self.conn.execute("UPDATE users SET status = ? WHERE id = ?",
                                    (status, user_id))
            if status != "active":
                self._revocar_sesiones_locked(user_id)
            self.conn.commit()
            return cur.rowcount > 0

    def list_users(self) -> list[dict]:
        """Las cuentas, con cuántas sesiones tiene abiertas cada una."""
        ahora = _ahora()
        with self._lock:
            rows = self.conn.execute(
                "SELECT u.*, "
                "(SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id "
                " AND s.expires_at > ?) AS sesiones, "
                "(SELECT MAX(s.created_at) FROM sessions s WHERE s.user_id = u.id "
                " AND s.expires_at > ?) AS sesion_desde "
                "FROM users u ORDER BY "
                "CASE u.status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, "
                "u.created_at, u.id", (ahora, ahora)).fetchall()
        return [self._publico(r) for r in rows]

    def count_admins(self) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND status = 'active'"
            ).fetchone()["n"]

    # ----------------------------------------------------------- sesiones
    def create_session(self, user_id: int, dias: int = 30) -> str:
        """Abre sesión y devuelve el token, que solo viajará en la cookie.
        En la tabla queda su SHA-256."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            if UNA_SESION_POR_USUARIO:
                self._revocar_sesiones_locked(user_id)
            self.conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (_hash_token(token), user_id, _ahora(), _ahora(int(dias) * 86400)))
            self.conn.commit()
        return token

    def user_for_token(self, token: str) -> dict | None:
        """El usuario de una sesión, o None si no vale. Exige además que siga
        activo: bloquear a alguien tiene que echarlo aunque su cookie valga."""
        if not token or len(token) > 200:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires_at > ? AND u.status = 'active'",
                (_hash_token(token), _ahora())).fetchone()
        return self._publico(row) if row else None

    def delete_session(self, token: str) -> None:
        if not token or len(token) > 200:
            return
        with self._lock:
            self.conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                              (_hash_token(token),))
            self.conn.commit()

    def _revocar_sesiones_locked(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def cerrar_sesiones(self, user_id: int) -> int:
        """Echa a esa persona de todos sus dispositivos. Devuelve cuántas cerró."""
        with self._lock:
            n = self.conn.execute("SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?",
                                  (user_id,)).fetchone()["n"]
            self._revocar_sesiones_locked(user_id)
            self.conn.commit()
            return n

    def purge_expired_sessions(self) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_ahora(),))
            self.conn.commit()
            return cur.rowcount

    # ------------------------------- enlaces por correo (alta y restablecer)
    def crear_token_correo(self, email: str, proposito: str, minutos: int,
                           ip_grupo: str = "") -> str:
        """Token de un solo uso. Devuelve el token en claro (va SOLO en el
        correo); en la base queda su SHA-256.

        `ip_grupo` es un resumen (hash) de la IP —o de su /64— que lo pidió:
        sirve para contar los topes por (correo, IP) sin guardar la IP."""
        if proposito not in ("signup", "reset"):
            raise ValueError("propósito desconocido")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.conn.execute(
                "INSERT INTO email_tokens (token_hash, email, purpose, created_at, "
                "expires_at, ip_grupo) VALUES (?, ?, ?, ?, ?, ?)",
                (_hash_token(token), email.strip().lower(), proposito,
                 _ahora(), _ahora(minutos * 60), ip_grupo or ""))
            self.conn.commit()
        return token

    def contar_correos(self, segundos: int, email: str | None = None,
                       proposito: str | None = None, ip_grupo: str | None = None) -> int:
        """Cuántos enlaces se mandaron en los últimos `segundos` (a ese correo,
        de ese tipo y/o pedidos desde esa IP, o en total). Es el contador de
        los topes."""
        sql = "SELECT COUNT(*) AS n FROM email_tokens WHERE created_at > ?"
        args: list = [_ahora(-segundos)]
        if email is not None:
            sql += " AND email = ?"
            args.append(email.strip().lower())
        if proposito is not None:
            sql += " AND purpose = ?"
            args.append(proposito)
        if ip_grupo is not None:
            sql += " AND ip_grupo = ?"
            args.append(ip_grupo)
        with self._lock:
            return int(self.conn.execute(sql, tuple(args)).fetchone()["n"])

    def ver_token_correo(self, token: str, proposito: str) -> str | None:
        """El correo del token si sigue valiendo (sin usar y sin caducar). No lo gasta."""
        if not token or not isinstance(token, str) or len(token) > 200:
            return None
        with self._lock:
            fila = self.conn.execute(
                "SELECT email FROM email_tokens WHERE token_hash = ? AND purpose = ? "
                "AND used_at IS NULL AND expires_at > ?",
                (_hash_token(token), proposito, _ahora())).fetchone()
        return fila["email"] if fila else None

    def gastar_token_correo(self, token: str, proposito: str) -> bool:
        """Lo marca como usado con un solo UPDATE condicionado: si dos
        peticiones llegan a la vez con el mismo enlace, solo una lo consigue."""
        if not token or not isinstance(token, str) or len(token) > 200:
            return False
        with self._lock:
            cur = self.conn.execute(
                "UPDATE email_tokens SET used_at = ? WHERE token_hash = ? AND purpose = ? "
                "AND used_at IS NULL AND expires_at > ?",
                (_ahora(), _hash_token(token), proposito, _ahora()))
            self.conn.commit()
            return cur.rowcount == 1

    def anular_tokens_correo(self, email: str, proposito: str) -> None:
        """Deja sin valor los enlaces pendientes de ese correo."""
        if not email:
            return
        with self._lock:
            self.conn.execute(
                "UPDATE email_tokens SET used_at = ? WHERE email = ? AND purpose = ? "
                "AND used_at IS NULL", (_ahora(), email.strip().lower(), proposito))
            self.conn.commit()

    def purgar_tokens_correo(self, dias: int = 2) -> None:
        """Borra los enlaces viejos. Se guardan dos días: lo justo para que los
        topes por día cuenten bien."""
        with self._lock:
            self.conn.execute("DELETE FROM email_tokens WHERE created_at < ?",
                              (_ahora(-dias * 86400),))
            self.conn.commit()

    # =========================================================== carpetas
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

    def folder_exists(self, folder_id: int) -> bool:
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM folders WHERE id = ?", (folder_id,)).fetchone() is not None

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

    # ============================================================== notas
    # Las notas son PRIVADAS: cada consulta va acotada por user_id. Una nota de
    # otra persona no existe para ti (404), ni para leerla ni para tocarla.
    def list_notes(self, user_id: int, topic_id: int, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM notes WHERE user_id = ? AND topic_id = ?"
        params: list = [user_id, topic_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY page, created_at, id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def add_note(self, user_id: int, topic_id: int, kind: str = "sticky",
                 asset_id: int | None = None, page: int = 1, x: float = 0.5,
                 y: float = 0.5, text: str = "", color: str = "amarillo") -> int:
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO notes (user_id, topic_id, asset_id, kind, page, x, y, text, color)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, topic_id, asset_id, kind, page,
                 _clamp(x, 0.0, 1.0), _clamp(y, 0.0, 1.0), text, color))
            self.conn.commit()
            return cur.lastrowid

    def update_note(self, user_id: int, note_id: int, **fields) -> bool:
        allowed = {"text", "color", "x", "y", "page"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return False
        if "x" in sets:
            sets["x"] = _clamp(float(sets["x"]), 0.0, 1.0)
        if "y" in sets:
            sets["y"] = _clamp(float(sets["y"]), 0.0, 1.0)
        clause = ", ".join(f"{k} = ?" for k in sets)       # claves de la lista fija
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE notes SET {clause}, updated_at = datetime('now') "
                "WHERE id = ? AND user_id = ?",
                (*sets.values(), note_id, user_id))
            self.conn.commit()
            return cur.rowcount > 0

    def delete_note(self, user_id: int, note_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM notes WHERE id = ? AND user_id = ?",
                                    (note_id, user_id))
            self.conn.commit()
            return cur.rowcount > 0

    # ======================================================= ajustes globales
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

    # ================================================= ajustes de cada persona
    def get_user_setting(self, user_id: int, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                (user_id, key)).fetchone()
        return row["value"] if row else default

    def set_user_setting(self, user_id: int, key: str, value) -> None:
        if key not in AJUSTES_DE_USUARIO:
            raise ValueError("Ajuste desconocido.")
        with self._lock:
            self.conn.execute(
                "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (user_id, key, str(value)))
            self.conn.commit()

    def get_int_user_setting(self, user_id: int, key: str, default: int) -> int:
        try:
            return int(self.get_user_setting(user_id, key, str(default)))
        except (TypeError, ValueError):
            return default

    def retention(self, user_id: int) -> float:
        try:
            return float(self.get_user_setting(user_id, "retention", str(DEFAULT_RETENTION)))
        except (TypeError, ValueError):
            return DEFAULT_RETENTION

    # ============================================================== temas
    def ensure_topic(self, title: str, sort_order: int = 0,
                     folder_id: int | None = None) -> int:
        """
        Devuelve el id del tema, creándolo si hace falta.

        `folder_id` se aplica al crear el tema y también a un tema existente que
        haya quedado SIN carpeta. Un tema que ya está en una carpeta no se toca:
        re-escanear el material no debe deshacer la organización que se hizo
        a mano.
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

    def count_topics(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) AS n FROM topics").fetchone()["n"]

    def topics_basic(self) -> list[dict]:
        """id, título y carpeta de cada tema. Para emparejar archivos en la
        ingesta, que no necesita el progreso de nadie."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, title, folder_id FROM topics ORDER BY sort_order, title").fetchall()
        return [dict(r) for r in rows]

    def list_topics(self, user_id: int) -> list[dict]:
        """Temas con el conteo de fichas, cuántas le tocan hoy A ESA PERSONA y si
        son nuevos."""
        today = datetime.date.today().isoformat()
        baseline = self.get_setting("baseline_at")
        with self._lock:
            limite = self.conn.execute(
                "SELECT datetime('now', ?) AS d", (f"-{NEW_DAYS} days",)).fetchone()["d"]
            # c.id IS NOT NULL: un tema sin fichas trae una fila con c.id NULL,
            # y COALESCE(p.state, 'new') la contaría como una ficha nueva.
            rows = self.conn.execute(
                """
                SELECT t.id, t.slug, t.title, t.area, t.sort_order, t.folder_id,
                       t.created_at,
                       (SELECT MAX(a.created_at) FROM assets a WHERE a.topic_id = t.id) AS material_at,
                       COUNT(c.id) AS total,
                       SUM(c.id IS NOT NULL AND COALESCE(p.state, 'new') = 'new') AS nuevas,
                       SUM(COALESCE(p.state, 'new') != 'new' AND p.due <= ?
                           AND NOT COALESCE(p.suspended, 0)) AS vencidas
                  FROM topics t
                  LEFT JOIN cards c ON c.topic_id = t.id
                  LEFT JOIN card_progress p ON p.card_id = c.id AND p.user_id = ?
                 GROUP BY t.id
                 ORDER BY t.sort_order, t.title
                """, (today, user_id)).fetchall()
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

    # ========================================================= materiales
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

    # ============================================================ tarjetas
    def upsert_cards(self, topic_id: int, cards: list[dict], source: str = "anki") -> dict:
        """
        Inserta tarjetas nuevas y respeta las que ya existen.

        Reimportar un .apkg actualizado NO reinicia el progreso de nadie: se
        emparejan por (anki_guid, cloze_index) y solo se actualiza el texto;
        el progreso vive en card_progress, atado al id de la ficha.

        El HTML se limpia AQUÍ (limpiar_html), por donde pasa toda ficha que
        entra: la ingesta, la carga desde la app y la reimportación, que así
        vuelve a limpiar también las fichas que ya estaban en la base.
        """
        added = updated = 0
        with self._lock:
            for card in cards:
                card = dict(card, front=limpiar_html(card.get("front", "")),
                            back=limpiar_html(card.get("back", "")))
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
                                           tags, source, anki_guid)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (topic_id, card.get("kind", "basic"), card.get("front", ""),
                         card.get("back", ""), card.get("cloze_index", 0),
                         card.get("tags", ""), source, guid))
                    added += 1
            self.conn.commit()
        return {"added": added, "updated": updated}

    def topic_cards(self, topic_id: int) -> list[dict]:
        """El CONTENIDO de las fichas de un tema (igual para todos), sin progreso."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, topic_id, kind, front, back, cloze_index, tags "
                "FROM cards WHERE topic_id = ? ORDER BY id", (topic_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_card(self, card_id: int, user_id: int) -> dict | None:
        """La ficha con el progreso de ESA persona (nueva si nunca la vio)."""
        with self._lock:
            row = self.conn.execute(
                f"SELECT {_CARD_COLS} FROM {_CARD_JOIN} WHERE c.id = ?",
                (user_id, card_id)).fetchone()
        return dict(row) if row else None

    def new_today(self, user_id: int) -> int:
        """Fichas nuevas que esa persona ya introdujo hoy (primer repaso)."""
        today = datetime.date.today().isoformat()
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM reviews "
                "WHERE user_id = ? AND day = ? AND state_before = 'new'",
                (user_id, today)).fetchone()
        return row["n"] if row else 0

    def reviews_today(self, user_id: int) -> int:
        today = datetime.date.today().isoformat()
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM reviews "
                "WHERE user_id = ? AND day = ? AND state_before != 'new'",
                (user_id, today)).fetchone()
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

    def build_queue(self, user_id: int, topic_id: int | None = None, limit: int = 60,
                    folder_id: int | None = None, mode: str = "normal") -> list[dict]:
        """
        Cola de estudio de UNA persona.

        modo 'normal' — lo de hoy. Orden: fallado en la sesión → vencido de más
            atrás → nuevas, hasta el tope diario. Las vencidas van antes que las
            nuevas a propósito: introducir material nuevo mientras se acumula
            atraso es la forma más rápida de ahogarse.

        modo 'libre' — repaso extra. Devuelve fichas sin mirar la fecha ni los
            topes, barajadas. Sirve para volver a pasar un tema el mismo día;
            NO altera la programación (ver `grade`).
        """
        where, params = self._scope(topic_id, folder_id)
        activa = "COALESCE(p.suspended, 0) = 0"

        if mode == "libre":
            with self._lock:
                rows = self.conn.execute(
                    f"""SELECT {_CARD_COLS} FROM {_CARD_JOIN}
                         WHERE {activa} {where}
                         ORDER BY RANDOM() LIMIT ?""",
                    (user_id, *params, limit)).fetchall()
            return [dict(r) for r in rows]

        today = datetime.date.today().isoformat()
        new_left = max(self.get_int_user_setting(user_id, "new_limit", DEFAULT_NEW_LIMIT)
                       - self.new_today(user_id), 0)
        rev_left = max(self.get_int_user_setting(user_id, "review_limit", DEFAULT_REVIEW_LIMIT)
                       - self.reviews_today(user_id), 0)

        with self._lock:
            learning = self.conn.execute(
                f"""SELECT {_CARD_COLS} FROM {_CARD_JOIN}
                     WHERE p.state IN ('learning', 'relearning') AND {activa}
                       AND p.due <= ? {where}
                     ORDER BY p.last_review LIMIT ?""",
                (user_id, today, *params, limit)).fetchall()

            due = self.conn.execute(
                f"""SELECT {_CARD_COLS} FROM {_CARD_JOIN}
                     WHERE p.state = 'review' AND {activa}
                       AND p.due <= ? {where}
                     ORDER BY p.due, c.id LIMIT ?""",
                (user_id, today, *params, rev_left)).fetchall()

            new = self.conn.execute(
                f"""SELECT {_CARD_COLS} FROM {_CARD_JOIN}
                     WHERE COALESCE(p.state, 'new') = 'new' AND {activa} {where}
                     ORDER BY c.id LIMIT ?""",
                (user_id, *params, new_left)).fetchall()

        queue = [dict(r) for r in learning] + [dict(r) for r in due] + [dict(r) for r in new]
        return queue[:limit]

    def scope_counts(self, user_id: int, topic_id: int | None = None,
                     folder_id: int | None = None) -> dict:
        """Cuántas fichas hay y cuántas le tocan hoy a esa persona, para un tema o carpeta."""
        today = datetime.date.today().isoformat()
        where, params = self._scope(topic_id, folder_id)
        with self._lock:
            # el orden de los parámetros sigue al de los '?' en el texto:
            # primero el del SELECT (hoy), luego el del JOIN (user_id)
            row = self.conn.execute(
                f"""SELECT COUNT(*) AS total,
                           SUM(COALESCE(p.state, 'new') = 'new') AS nuevas,
                           SUM(COALESCE(p.state, 'new') != 'new' AND p.due <= ?
                               AND NOT COALESCE(p.suspended, 0)) AS vencidas
                      FROM {_CARD_JOIN} WHERE 1 {where}""",
                (today, user_id, *params)).fetchone()
        return {"total": row["total"] or 0, "nuevas": row["nuevas"] or 0,
                "vencidas": row["vencidas"] or 0}

    def grade(self, user_id: int, card_id: int, rating: int) -> dict | None:
        """Califica una ficha para ESA persona: guarda su nuevo estado y
        registra el repaso. El progreso de los demás no se toca."""
        card = self.get_card(card_id, user_id)
        if not card:
            return None
        rating = int(_clamp(int(rating), 1, 4))
        result = schedule(card, rating, self.retention(user_id))
        hoy = datetime.date.today().isoformat()

        with self._lock:
            self.conn.execute(
                """INSERT INTO card_progress (user_id, card_id, state, due, stability,
                                              difficulty, reps, lapses, last_review)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, card_id) DO UPDATE SET
                       state = excluded.state, due = excluded.due,
                       stability = excluded.stability, difficulty = excluded.difficulty,
                       reps = excluded.reps, lapses = excluded.lapses,
                       last_review = excluded.last_review""",
                (user_id, card_id, result["state"], result["due"], result["stability"],
                 result["difficulty"], result["reps"], result["lapses"],
                 result["last_review"]))
            # El día lo pone Python, en hora local: con el DEFAULT date('now')
            # (UTC) un repaso de las 11 de la noche contaba para mañana.
            self.conn.execute(
                """INSERT INTO reviews (user_id, card_id, rating, state_before, elapsed_days,
                                        scheduled_days, stability, difficulty,
                                        reviewed_at, day)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, card_id, rating, card.get("state") or "new",
                 result["elapsed_days"], result["interval"], result["stability"],
                 result["difficulty"], _ahora(), hoy))
            self.conn.commit()
        return result

    # ============================================================ métricas
    def stats(self, user_id: int) -> dict:
        today = datetime.date.today().isoformat()
        with self._lock:
            totals = self.conn.execute(
                f"""SELECT COUNT(*) AS total,
                          SUM(COALESCE(p.state, 'new') = 'new')    AS nuevas,
                          SUM(p.state = 'review')                  AS maduras,
                          SUM(p.state IN ('learning','relearning')) AS aprendiendo,
                          SUM(COALESCE(p.state, 'new') != 'new' AND p.due <= ?
                              AND NOT COALESCE(p.suspended, 0)) AS vencidas
                     FROM {_CARD_JOIN}""", (today, user_id)).fetchone()
            topics = self.conn.execute("SELECT COUNT(*) AS n FROM topics").fetchone()
            streak_rows = self.conn.execute(
                "SELECT DISTINCT day FROM reviews WHERE user_id = ? "
                "ORDER BY day DESC LIMIT 400", (user_id,)).fetchall()

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
        out["repasos_hoy"] = self.reviews_today(user_id)
        out["nuevas_hoy"] = self.new_today(user_id)
        out["nuevas_limite"] = self.get_int_user_setting(user_id, "new_limit", DEFAULT_NEW_LIMIT)
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


def scan_content(content_dir: str | None = None) -> dict[str, dict[str, list[str]]]:
    """
    Recorre la carpeta de material y agrupa por SECCIÓN y tipo.

    Devuelve {sección: {'presentacion': [...], 'resumen': [...], 'anki': [...],
    'articulo': [...]}}.
    Cada sección se convertirá en una carpeta de la app.
    """
    content_dir = content_dir or CONTENT_DIR
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


def ingest(db: DatabaseManager, content_dir: str | None = None) -> dict:
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
    content_dir = content_dir or CONTENT_DIR
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
        propios = [t for t in db.topics_basic() if t["folder_id"] == folder_id]
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
        # Pocos renders a la vez: cada pdftoppm es un proceso que come CPU y
        # memoria, y una ráfaga de páginas no debe tumbar el servidor.
        if not _RENDERS.acquire(timeout=30):
            _log.warning("Render de página descartado: demasiados a la vez.")
            return None
        try:
            if os.path.exists(out_png):      # otro hilo la hizo mientras esperábamos
                return out_png
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(PAGE_DPI),
                 "-f", str(page), "-l", str(page), pdf_path, prefix],
                capture_output=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning("pdftoppm falló: %s", type(exc).__name__)
            return None
        finally:
            _RENDERS.release()

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

