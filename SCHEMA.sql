-- SCHEMA.sql — MedStudy (App de Estudio)
--
-- La unidad atómica del diseño es el TEMA. Ver ARCHITECTURE.md.
-- Un tema agrupa sus materiales (presentación, resumen, mazo) y sus tarjetas.
--
-- Tipos estándar (INTEGER/TEXT/REAL) para poder migrar a Postgres sin dolor.
-- Idempotente: se aplica en cada arranque con CREATE TABLE IF NOT EXISTS.

PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------------------
-- Cuentas (portado de EnglishApp). El contenido —carpetas, temas, materiales y
-- fichas— es de todos; el progreso, las notas y los ajustes de estudio son de
-- cada persona.
-- La contraseña nunca se guarda en claro: solo su hash scrypt.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL COLLATE NOCASE,
    email         TEXT    COLLATE NOCASE,
    password_hash TEXT    NOT NULL DEFAULT '',
    role          TEXT    NOT NULL DEFAULT 'user',     -- 'admin' | 'user'
    status        TEXT    NOT NULL DEFAULT 'active',   -- 'pending' | 'active' | 'blocked'
    nombre        TEXT    NOT NULL DEFAULT '',         -- nombre visible, no el de entrar
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (username),
    UNIQUE (email),
    CHECK (role   IN ('admin', 'user')),
    CHECK (status IN ('pending', 'active', 'blocked'))
);

-- Una sesión abierta = una fila. Se guarda el SHA-256 del token, no el token:
-- el original solo existe en la cookie del navegador. Tenerlas en la base (y
-- no en una cookie firmada) permite revocarlas al bloquear o cambiar la clave.
-- Las fechas las pone Python ('AAAA-MM-DD HH:MM:SS', hora del servidor).
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);

-- Enlaces de un solo uso que se mandan por correo: crear la cuenta ('signup')
-- y restablecer la contraseña ('reset'). Se guarda el SHA-256 del token.
-- Sirve también de registro de envíos: los topes por hora y por día se
-- cuentan aquí.
CREATE TABLE IF NOT EXISTS email_tokens (
    token_hash TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    purpose    TEXT NOT NULL,               -- 'signup' | 'reset'
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    -- Hash de la IP (o de su /64) que lo pidió: los topes por correo se
    -- cuentan por (correo, IP) sin guardar la IP en claro.
    ip_grupo   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_email_tokens_email   ON email_tokens (email);
CREATE INDEX IF NOT EXISTS idx_email_tokens_created ON email_tokens (created_at);

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
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cards_por_tema ON cards (topic_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_guid
    ON cards (anki_guid, cloze_index) WHERE anki_guid IS NOT NULL;

-- --------------------------------------------------------------------------
-- Progreso FSRS de CADA persona en cada ficha (ver core.py: FSRS_W y
-- schedule()). Sin fila, la ficha es nueva para esa persona: no hace falta
-- crear 1 900 filas al dar de alta a alguien.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_progress (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    card_id     INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    state       TEXT    NOT NULL DEFAULT 'new',     -- new | learning | review | relearning
    due         TEXT,                               -- fecha ISO 'YYYY-MM-DD'
    stability   REAL    NOT NULL DEFAULT 0,
    difficulty  REAL    NOT NULL DEFAULT 0,
    reps        INTEGER NOT NULL DEFAULT 0,
    lapses      INTEGER NOT NULL DEFAULT 0,
    last_review TEXT,
    suspended   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, card_id)
);
CREATE INDEX IF NOT EXISTS idx_progress_due ON card_progress (user_id, state, due);

-- --------------------------------------------------------------------------
-- Bitácora de repasos. Es la fuente de verdad histórica: permite re-optimizar
-- los pesos de FSRS más adelante sin haber perdido nada. Una fila por repaso
-- de una persona (user_id).
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
    day            TEXT    NOT NULL DEFAULT (date('now')),
    -- Va al final porque en una base vieja se añade con ALTER TABLE.
    user_id        INTEGER REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reviews_day  ON reviews (day);
CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews (card_id, reviewed_at);
CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews (user_id, day);

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
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    -- Dueña de la nota: las notas son privadas. Al final porque en una base
    -- vieja se añade con ALTER TABLE; la app nunca escribe una nota sin dueño.
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notes_topic ON notes (topic_id, kind);
CREATE INDEX IF NOT EXISTS idx_notes_page  ON notes (asset_id, page);
CREATE INDEX IF NOT EXISTS idx_notes_user  ON notes (user_id, topic_id);

-- --------------------------------------------------------------------------
-- Ajustes de la app entera (baseline_at). La carpeta de material NO es un
-- ajuste: solo se fija con la variable de entorno ESTUDIO_CONTENT_DIR.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Ajustes de estudio de cada persona: new_limit, review_limit, retention, theme.
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, key)
);
