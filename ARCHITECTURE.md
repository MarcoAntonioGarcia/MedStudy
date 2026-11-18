# Arquitectura — MedStudy (App de Estudio)

Un proceso Python (FastAPI), una base SQLite (`database.db`) y unas pocas
páginas sin frameworks ni CDN. Con **cuentas** (etapa 1): el material es
compartido y el progreso es de cada persona. El único servicio externo es el
envío de correos de alta y de restablecer (Brevo), y es opcional.

## Cuentas y progreso por persona (etapa 1)

Portado de EnglishApp, que ya corre en producción:

- **Contraseñas** con scrypt de la librería estándar (`core.hash_password`).
- **Sesiones** opacas: token aleatorio en una cookie `HttpOnly`, `SameSite=Lax`
  (y `Secure` con `COOKIE_SEGURA=1`); en la base, solo su SHA-256. Una sesión
  por persona: entrar cierra las demás. Bloquear o cambiar la contraseña las
  cierra todas.
- **Enlaces por correo** (`email_tokens`): SHA-256, un solo uso, caducan. El
  registro pide solo el correo; usuario y contraseña se eligen al abrir el
  enlace (contra el pre-secuestro de cuentas). Respuestas iguales exista o no
  la cuenta; topes por IP, por correo y globales.
- **Topes de intentos** en memoria: por IP (`IP_DE_CABECERA` detrás de un
  proxy), por cuenta y por «dispositivo conocido» (cookie firmada).
- **Permisos**: todo `/api/*` salvo `/api/auth/*` exige sesión (401). Lo que
  cambia el material compartido —re-escanear, cargar, carpetas, mover temas,
  huérfanos, `/admin`— exige ser admin (403).
- **Defensa en profundidad**: tope de cuerpo de 1 MB (25 MB para la carga del
  admin), CSP sin scripts en línea (el JavaScript vive en `static/*.js`, así el
  HTML de las fichas de Anki no puede ejecutar nada), `frame-ancestors 'none'`,
  `nosniff`, HSTS con `COOKIE_SEGURA=1`, `/docs` apagado salvo `DOCS=1`. Los
  archivos de los materiales solo se sirven si su ruta real está dentro de la
  carpeta de material o de `biblioteca/`. La carpeta de material solo se fija
  con `ESTUDIO_CONTENT_DIR`; la API no puede elegir otra.

Qué es de quién:

| Tabla | De quién |
|---|---|
| `folders`, `topics`, `assets`, `cards`, `orphan_files` | De todos (el contenido) |
| `card_progress (user_id, card_id)` | De cada persona: estado FSRS. Sin fila = ficha nueva |
| `reviews` (con `user_id`) | De cada persona: bitácora de repasos |
| `notes` (con `user_id`) | Privadas: otra persona recibe 404 |
| `user_settings (user_id, key)` | De cada persona: `new_limit`, `review_limit`, `retention`, `theme` |
| `settings` | De la app: `baseline_at` |
| `users`, `sessions`, `email_tokens` | Cuentas |

`_migrate()` pone al día una base vieja en cada arranque, de forma idempotente:
quita las columnas FSRS de `cards` (el progreso global no tenía dueño y se
descarta), añade `user_id` a `reviews` y `notes`, y borra de `settings` los
ajustes que ya son personales y `content_dir`. Las fechas nuevas las pone
Python como texto, igual en SQLite que en un futuro Postgres.

## Principio rector

> **La unidad atómica de la app es el TEMA.**

Un tema no es un archivo: es *Vértigo* = presentación + resumen + mazo +
progreso. Los tres materiales de un mismo tema viven en carpetas distintas y
con nombres distintos, pero en la app son una sola cosa.

Por eso la app **no arranca vacía pidiendo que subas archivos**: escanea el
material que ya existe y presenta los 20 temas armados. Subir un archivo es
cómo agregas el tema 21, no el primer obstáculo para empezar a estudiar.

---

## De dónde sale cada cosa

```
Mariela/
├── PresentacionesResumen/
│   ├── Vertigo.pdf                    ─┐
│   ├── Resumenes PDF/                  │
│   │   └── Vertigo - Resumen.pdf      ─┼─→  un TEMA: «Vértigo»
│   └── Anki/                           │
│       └── Vertigo.apkg               ─┘
└── AppEstudio/                              ← esta app
```

La app **no copia nada**. Guarda las rutas y lee los archivos donde están:
las presentaciones llegan a pesar 91 MB y duplicarlas no aporta nada.

---

## El problema real: los nombres no coinciden

Los tres juegos de archivos se nombraron en momentos distintos:

| Presentación | Resumen | Mazo Anki |
|---|---|---|
| `Enfermedad de Alzheimer CORREGIDA.pptx` | `Alzheimer - Resumen.pdf` | `Neurología::Enfermedad de Alzheimer` |
| `EVENTO CEREBRAL VASCULAR ISQUEMICO.pptx` | `EVC Isquemico - Resumen.pdf` | `Neurología::EVC Isquémico` |
| `Sx Epilepticos.pdf` | `Sindromes Epilepticos - Resumen.pdf` | `Neurología::Síndromes Epilépticos` |

La ingesta lo resuelve en dos pasos, en este orden y no en otro:

1. **Los `.apkg` primero.** El nombre del mazo (`Neurología::Vértigo`) es el
   nombre **canónico** del tema. Está escrito a mano, sin abreviaturas ni
   sufijos de edición. Sin él habría que adivinar el nombre del tema a partir
   de un archivo llamado `esclerosis .pptx`.
2. **Presentaciones y resúmenes después**, emparejados por tokens contra los
   temas ya creados.

### Cómo se emparejan

`match_score()` compara conjuntos de palabras, no cadenas. Tres decisiones:

- **Se quitan acentos y extensión.** `pdf` aparece en todos los archivos: no
  discrimina, solo diluye el puntaje.
- **Se expanden siglas** (`_ALIASES`). Sin `evc → {evento, cerebral, vascular}`,
  `EVENTO CEREBRAL VASCULAR ISQUEMICO.pptx` jamás empataría con *EVC Isquémico*.
- **Se usa contención, no Jaccard.** `Alzheimer - Resumen` aporta un token útil
  frente a los dos de *Enfermedad de Alzheimer*; Jaccard daría 0.5 y lo dejaría
  al borde del umbral, aunque el empate sea perfecto. La contención da 1.0.

Un 20 % del puntaje viene de la cobertura, como desempate: es lo que separa
*Exploración Neurológica* de *Exploración Física Neurológica*, que si no
quedarían empatadas.

**Lo que no llega al umbral no se descarta**: cae en `orphan_files` y aparece
en la UI para asignarlo a mano. Un archivo perdido en silencio es peor que uno
mal clasificado a la vista.

---

## Programación de repasos: FSRS, no SM-2

Anki clásico usa SM-2, que resume el estado de una tarjeta en un solo número
(el *factor de facilidad*). FSRS separa dos cosas que no son lo mismo:

- **Estabilidad (S)** — cuántos días aguanta la memoria antes de caer al
  umbral de retención.
- **Dificultad (D)** — qué tan costosa es *esa* tarjeta, en escala 1–10.

Con eso calcula la **recuperabilidad** `R(t,S) = (1 + 19/81 · t/S)^-0.5`, que
es la probabilidad de acertar tras `t` días. El intervalo es simplemente el
`t` donde `R` cae a la retención objetivo (0.90 por defecto).

Los pesos (`FSRS_W`) son los valores por defecto publicados de FSRS-5. La
tabla `reviews` guarda **cada** repaso —no solo el último— justamente para
poder re-optimizarlos con el historial real más adelante.

Granularidad de **día**. El repaso intradía de lo fallado lo resuelve la cola
en memoria, no la fecha en la base: calificar *Otra vez* devuelve la tarjeta
al final de la cola de la sesión.

### El tope diario es el freno

`build_queue()` sirve en este orden: **fallado → vencido → nuevo**. Las
vencidas van antes que las nuevas a propósito. Introducir material nuevo
mientras se acumula atraso es la forma más rápida de ahogarse, y es la razón
más común por la que alguien abandona el repaso espaciado.

---

## Ida y vuelta con Anki

Bidireccional a propósito: la app no obliga a abandonar Anki.

- **Importar** no necesita dependencias. Un `.apkg` es un zip con una base
  SQLite dentro; `zipfile` y `sqlite3` son de la librería estándar. Se leen
  las notas *Basic* y *Cloze* (`{{c1::...}}` se expande a una tarjeta por hueco).
- **Exportar** usa `genanki`.

Reimportar un `.apkg` actualizado **no duplica ni reinicia el progreso**: las
tarjetas se emparejan por `(anki_guid, cloze_index)`, así que solo se
actualiza el texto y el estado FSRS queda intacto.

---

## Los resúmenes se renderizan en el servidor

Para pegar post-it **encima** del papel hay un obstáculo: el visor de PDF del
navegador es una caja opaca. No deja dibujar sobre él, ni saber dónde se hizo
clic dentro del documento.

Dos salidas posibles: incrustar pdf.js (~1 MB de JavaScript) o renderizar cada
página a imagen en el servidor. Se eligió lo segundo:

- El navegador solo recibe un `<img>`, y sobre él va una capa normal donde las
  notas se posicionan con CSS. Sin librerías, sin CDN.
- `pdftoppm` (poppler) genera el PNG a 130 dpi en ~350 ms por página.
- La caché lo hace irrelevante: la clave incluye la **fecha del PDF**, así que
  si el archivo se reemplaza la clave cambia sola y no hay que invalidar nada.
  Segunda petición: 2 ms.

Si falta poppler la app no se rompe: cae al visor del navegador dentro de un
iframe y avisa de que no se puede anotar.

### Por qué las coordenadas van en porcentaje

Un post-it guarda `(página, x, y)` con `x` e `y` entre 0 y 1, no en píxeles.
La página se renderiza a un ancho fijo pero se muestra escalada al ancho de la
ventana; con píxeles, la nota se despegaría de su sitio en cuanto cambiara el
tamaño de la ventana o el dpi del render.

## Qué se copia y qué no

| | Se copia | Por qué |
|---|---|---|
| Resúmenes PDF | Sí, a `biblioteca/` | ~170 KB. La app queda autocontenida. |
| Archivos de fichas | Se importan a la base | El contenido pasa a SQLite; el archivo ya no hace falta. |
| Presentaciones | **No** | Hasta 91 MB. De ellas se sacan el resumen y las fichas. |

## Piezas

| Archivo | Responsabilidad |
|---|---|
| `core.py` | Toda la lógica. No importa FastAPI ni ninguna UI. |
| `webapp.py` | HTTP: rutas, sesiones, permisos, topes y validación de entrada. |
| `correo.py` | Envío de correos (Brevo); `memoria` en pruebas, `consola` en local. |
| `crear_admin.py` | Crea la cuenta de administrador sin escribir la contraseña en ningún sitio. |
| `static/` | `index.html` + `app.js` (la app), `login`, `cuenta`, `admin`, `ui.js` (diálogos). |
| `SCHEMA.sql` | Esquema SQLite, idempotente. |

Dentro de `core.py`:

- `slugify` / `tokenize` / `match_score` / `match_topic` — emparejamiento
- `schedule` / `retrievability` / `interval_for` — FSRS
- `AnkiImporter` / `AnkiExporter` — intercambio con Anki
- `hash_password` / `verify_password` / `ruta_permitida` — cuentas y archivos
- `DatabaseManager` — única dueña de la conexión SQLite (cuentas, progreso, notas)
- `ingest` — escanea y arma los temas (idempotente)

El acceso a datos está aislado en `DatabaseManager`: migrar a Postgres
significaría cambiar esa clase, no el resto de la app.

---

## Estructura de archivos

```
AppEstudio/
├── core.py                 # lógica: ingesta, FSRS, Anki, cuentas, persistencia
├── webapp.py               # servidor (FastAPI) — entrypoint
├── correo.py               # correos de alta y restablecer
├── crear_admin.py          # primera cuenta de administrador
├── SCHEMA.sql              # esquema de la base
├── static/                 # páginas, scripts y estilos
├── tests/                  # unittest: núcleo, registro, seguridad, aislamiento
├── database.db             # (generado) contenido, cuentas, progreso y notas
├── ARCHITECTURE.md         # este documento
└── README.md
```

---

## Lo que sigue (fuera de este entregable)

Decidido pero no construido todavía, en orden de valor:

1. **Casos clínicos.** Viñetas generadas desde los resúmenes. Recordar una
   definición no es lo que se pregunta en una guardia ni en el ENARM.
2. **Oclusión de imágenes.** Los `.pptx` están llenos de TACs, RMs y
   campimetrías que hoy no se aprovechan; los mazos actuales no tienen ni una
   imagen. Neurología es una especialidad visual.
3. **Modo diferencial.** Intercalar a propósito los temas confundibles
   (EVC isquémico / hemorrágico / AIT, las demencias, las cefaleas). Estudiar
   tema por tema crea la ilusión de dominio; el intercalado la rompe.
