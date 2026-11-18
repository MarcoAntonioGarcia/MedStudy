-- SCHEMA.sql — App de Estudio (Neurología)
--
-- La unidad atómica del diseño es el TEMA. Ver ARCHITECTURE.md.
-- Un tema agrupa sus materiales (presentación, resumen, mazo) y sus tarjetas.
--
-- Tipos estándar (INTEGER/TEXT/REAL) para poder migrar a Postgres sin dolor.
-- Idempotente: se aplica en cada arranque con CREATE TABLE IF NOT EXISTS.

PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------------------
-- Carpetas — las crea Mariela, agrupan temas. Una carpeta por materia,
-- por rotación, por examen: la app no impone ninguna estructura.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    color      TEXT    NOT NULL DEFAULT '#c9a86a',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------------------------
-- Temas
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    NOT NULL UNIQUE,       -- 'vertigo' (sin acentos, minúsculas)
    title       TEXT    NOT NULL,              -- 'Vértigo' (canónico, del archivo de fichas)
    area        TEXT    NOT NULL DEFAULT 'Neurología',
    folder_id   INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_topics_folder ON topics (folder_id, sort_order);

-- --------------------------------------------------------------------------
-- Materiales de cada tema (presentación / resumen / mazo origen)
--
-- Se guarda la RUTA, no el archivo: los .pptx pesan hasta 91 MB y ya viven en
-- las carpetas hermanas. La app los lee de ahí; no duplica nada en disco.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id   INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,               -- 'presentacion' | 'resumen' | 'anki'
    path       TEXT    NOT NULL,
    filename   TEXT    NOT NULL,
    ext        TEXT    NOT NULL,               -- 'pptx' | 'pdf' | 'apkg'
    bytes      INTEGER NOT NULL DEFAULT 0,
    matched_by TEXT    NOT NULL DEFAULT 'auto',-- 'auto' | 'manual' | 'deck'
    score      REAL    NOT NULL DEFAULT 0,     -- confianza del emparejamiento (0..1)
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (path)
);
CREATE INDEX IF NOT EXISTS idx_assets_topic ON assets (topic_id, kind);

-- Archivos que la ingesta NO logró emparejar con ningún tema.
-- No se pierden: se listan en la UI para asignarlos a mano.
CREATE TABLE IF NOT EXISTS orphan_files (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT NOT NULL UNIQUE,
    filename  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    ext       TEXT NOT NULL,
    bytes     INTEGER NOT NULL DEFAULT 0,
    best_guess_topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    best_score REAL NOT NULL DEFAULT 0
);

-- --------------------------------------------------------------------------
-- Tarjetas
--
-- 'anki_guid' es el GUID de la nota original: permite reimportar un .apkg
-- actualizado sin duplicar tarjetas ni perder el progreso FSRS.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL DEFAULT 'basic',   -- 'basic' | 'cloze'
    front       TEXT    NOT NULL,
    back        TEXT    NOT NULL DEFAULT '',
    cloze_index INTEGER NOT NULL DEFAULT 0,         -- c1, c2… dentro de una nota cloze
    tags        TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL DEFAULT 'anki',    -- 'anki' | 'manual' | 'ia'
    anki_guid   TEXT,
    suspended   INTEGER NOT NULL DEFAULT 0,

    -- Estado FSRS (ver core.py: FSRS_W y schedule()).
    state       TEXT    NOT NULL DEFAULT 'new',     -- new | learning | review | relearning
    due         TEXT,                               -- fecha ISO 'YYYY-MM-DD'
    stability   REAL    NOT NULL DEFAULT 0,
    difficulty  REAL    NOT NULL DEFAULT 0,
    reps        INTEGER NOT NULL DEFAULT 0,
    lapses      INTEGER NOT NULL DEFAULT 0,
    last_review TEXT,

    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cards_due   ON cards (due, suspended);
CREATE INDEX IF NOT EXISTS idx_cards_topic ON cards (topic_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_guid
    ON cards (anki_guid, cloze_index) WHERE anki_guid IS NOT NULL;

-- --------------------------------------------------------------------------
-- Bitácora de repasos. Es la fuente de verdad histórica: permite re-optimizar
-- los pesos de FSRS más adelante sin haber perdido nada.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id        INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    rating         INTEGER NOT NULL,            -- 1=Otra vez 2=Difícil 3=Bien 4=Fácil
    state_before   TEXT    NOT NULL DEFAULT '',
    elapsed_days   REAL    NOT NULL DEFAULT 0,
    scheduled_days REAL    NOT NULL DEFAULT 0,
    stability      REAL    NOT NULL DEFAULT 0,
    difficulty     REAL    NOT NULL DEFAULT 0,
    reviewed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    day            TEXT    NOT NULL DEFAULT (date('now'))
);
CREATE INDEX IF NOT EXISTS idx_reviews_day  ON reviews (day);
CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews (card_id, reviewed_at);

-- --------------------------------------------------------------------------
-- Notas
--
-- Dos clases, deliberadamente distintas:
--   'sticky'  — post-it pegado a una coordenada del resumen. Guarda página y
--               posición en PORCENTAJE (0..1), no en píxeles: así la nota
--               sigue en su sitio aunque el PDF se renderice a otra escala o
--               la ventana cambie de tamaño.
--   'general' — nota del tema completo, sin anclaje. Para lo que no pertenece
--               a un punto concreto del papel.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id   INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    asset_id   INTEGER REFERENCES assets(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL DEFAULT 'sticky',   -- 'sticky' | 'general'
    page       INTEGER NOT NULL DEFAULT 1,
    x          REAL    NOT NULL DEFAULT 0.5,        -- 0..1 del ancho de la página
    y          REAL    NOT NULL DEFAULT 0.5,        -- 0..1 del alto
    color      TEXT    NOT NULL DEFAULT 'amarillo',
    text       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notes_topic ON notes (topic_id, kind);
CREATE INDEX IF NOT EXISTS idx_notes_page  ON notes (asset_id, page);

-- --------------------------------------------------------------------------
-- Ajustes clave/valor (límites diarios, retención objetivo, rutas…)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
