# Arquitectura — MedStudy (App de Estudio)

Un proceso Python (FastAPI), una base (SQLite en local, Postgres en
producción) y unas pocas páginas sin frameworks ni CDN. Con **cuentas** (etapa 1): el material es
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

## Despliegue: Render gratis + Neon (etapa 2)

Copiado de EnglishApp, que ya corre así. La app va en una imagen de Docker en
Render (plan gratuito) y la base en Neon (Postgres gratuito). El disco de
Render se borra en cada reinicio y no hay terminal: todo lo que sigue sale de
esas dos restricciones.

### Dos motores, el SQL escrito una vez

`DatabaseManager` abre SQLite (local, pruebas) o Postgres (si hay
`DATABASE_URL`). Las consultas se escriben **una vez, en dialecto SQLite**, y
`core.a_postgres()` las traduce al vuelo solo cuando la base es Postgres: `?`
→ `%s`, `%` → `%%`, `datetime('now')` → texto UTC con el mismo formato que
SQLite (las fechas son TEXT y se comparan como cadenas), `INSERT OR IGNORE` →
`ON CONFLICT DO NOTHING`. `lastrowid` se resuelve añadiendo `RETURNING id` en
las tablas con `id` (deducidas de SCHEMA.sql).

Lo que no se traduce se escribió para valer en los dos: los booleanos dentro de
`SUM()` van como `CASE WHEN … THEN 1 ELSE 0 END`, `NOT COALESCE(x, 0)` como
`COALESCE(x, 0) = 0`, `WHERE 1` como `WHERE 1 = 1`, y los `GROUP BY` agrupan
todas las columnas no agregadas (Postgres lo exige). `baseline_at` sale del
reloj de la base (`SELECT datetime('now')`, traducido), el mismo que pone los
`created_at` con los que se compara; el límite de 21 días de «Nuevo» lo pone
Python en UTC.

### El día de quien estudia, no el del servidor

El contenedor corre en UTC: con su reloj, el día cambiaría a las 18:00 en
México y un repaso de las 7 de la tarde contaría para mañana (tope de nuevas,
vencidas, racha, `reviews.day`). Todo «hoy» y todo «ahora» salen de
`core.hoy()` / `core._ahora()`, en una zona **explícita** (`APP_TZ`, por
defecto `America/Mexico_City`, con `zoneinfo`), sin depender de la zona del
sistema. La imagen instala `tzdata`; si la zona no existiera, la app no
arranca. `render.yaml` pone además `TZ` para el reloj del sistema y los logs.

El esquema sigue teniendo una sola fuente, `SCHEMA.sql`; `pg_schema.py` lo
traduce (`AUTOINCREMENT` → `IDENTITY`, `REAL` → `DOUBLE PRECISION`, `COLLATE
NOCASE` → índice único sobre `lower()`) y la app lo aplica en cada arranque:
tablas, columnas que falten (`ADD COLUMN IF NOT EXISTS`) e índices, todo
idempotente. Las migraciones de SQLite (`_migrate`, con `PRAGMA`) no corren en
Postgres: la base nace con el esquema de hoy.

**La conexión.** Una sola, compartida con un candado, como en SQLite. Va en
autocommit y la transacción se abre sola con la primera escritura (`BEGIN`) y
se cierra con `commit()`: las lecturas no dejan la conexión «idle in
transaction» y las escrituras de un método siguen yendo juntas. Neon suspende
la base sin uso y mata las conexiones; si la conexión está muerta, se abre otra
y se reintenta **una** vez, solo cuando es seguro (una lectura fuera de
transacción, o el `BEGIN`). Si muere a mitad de una transacción, el error sube:
lo escrito se perdió con la conexión y no se finge lo contrario. Un error de
consulta (p. ej. unicidad) deshace la transacción para que la conexión siga
sirviendo. Sin sentencias preparadas en el servidor (el pooler de Neon no
siempre las conserva). Keepalives de TCP y `tcp_user_timeout` dan por muerta en
segundos una conexión cortada sin aviso, y cada conexión empieza con
`SET statement_timeout = '20s'`: ninguna consulta retiene la conexión (y el
candado de todas las peticiones) más de eso.

Las escrituras de varias sentencias van en `DatabaseManager._transaccion()`:
se confirman al final y, si algo falla en medio (también un error de Python),
se deshacen. Nada queda a medias para que un `commit()` posterior lo confirme.

**Cómo se prueba sin Postgres.** `tests/test_postgres.py` recorre todos los
métodos de la base contra SQLite, graba cada consulta con sus parámetros, la
traduce y busca restos de SQLite, marcas que no cuadran con los parámetros y
`GROUP BY` incompletos. Además lee `core.py` y exige que el recorrido haya
pasado por **cada** llamada a `self.conn.execute`: una consulta nueva sin
ejercitar hace fallar la batería. La lógica de la conexión (transacciones,
reconexión) se prueba con un servidor de mentira. Con `TEST_PG_URL`, todo eso
corre además contra un Postgres real.

### Rutas relativas

`assets.path` y `orphan_files.path` se guardan **relativas** a la carpeta de
material (`Neuro/Resumenes PDF/Vertigo - Resumen.pdf`), o con el prefijo
`biblioteca:` para lo cargado desde la app. La misma base la leen contenedores
distintos en cada despliegue, y el proyecto no vive en el mismo sitio en el Mac
que en la imagen (`/app`): con rutas absolutas, el primer despliegue habría
dejado todos los materiales apuntando a la nada, y cada ingesta habría creado
filas duplicadas. Hacia fuera de `DatabaseManager` las rutas salen ya resueltas
en el disco de este servidor, y `ruta_permitida` se sigue comprobando al
servirlas. Al arrancar, `_migrar_rutas` pasa a relativas las filas viejas
(también las de otra máquina, si el archivo existe aquí con la misma ruta
relativa); si la forma relativa ya existía, las notas pegadas al duplicado
pasan a la buena antes de borrarlo.

### El material va en la imagen y se ingiere al arrancar

`contenido/` está en el repositorio y el Dockerfile lo copia. En cada arranque
la app corre la ingesta, idempotente, en un hilo aparte: la comprobación de
salud (`/entrar`, que no toca la base) responde en cuanto el servidor escucha.
Medido con el material actual (38 temas, 1940 fichas, 76 materiales): 0,16 s
la primera vez y 0,11 s las siguientes en SQLite; en consultas, 2141 la primera
y 159 las siguientes (`upsert_cards` busca las fichas existentes de una vez y
no reescribe las que no cambiaron). Contra Neon cada consulta es un viaje de
red, por eso el hilo. Mientras corre, `/api/bootstrap` dice `actualizando` y la
interfaz avisa y vuelve a pedir la biblioteca cada 3 s, sin tocar lo que se esté
repasando. Dentro del proceso, un candado impide dos ingestas a la vez (el
re-escaneo del admin responde 409 si choca). Entre instancias (durante un
despliegue conviven dos) hay un candado consultivo, pero solo con la conexión
directa de Neon, y la ingesta **no depende de él**: nada es «leer y luego
insertar» a secas. Carpetas (nombre único), temas (slug), fichas (guid +
hueco), materiales y huérfanos (ruta) se crean con `INSERT … ON CONFLICT`, así
que dos ingestas a la vez ni fallan ni duplican; una ficha que la otra insertó
entre medias se actualiza como existente. Y cada carpeta, mazo o archivo va por
separado: si uno falla, queda en el informe y en el log y se sigue con el
resto. Las carpetas repetidas de antes del índice único se unifican al
arrancar. Lo publicado después de la primera ingesta lleva la marca «Nuevo» o
«Actualizado».

### En producción no se carga nada desde la app

Un archivo cargado viviría en un disco que se borra. En producción
(`MEDSTUDY_PRODUCCION`, `RENDER` o `APP_URL` con https) `/api/upload` responde 409 —«En producción el
material se agrega con git»— **sin leer el cuerpo**, y la interfaz no muestra
el botón. En local funciona como siempre. La caché de páginas PNG sí se queda
en el disco: es una caché y se rehace sola.

### El primer admin sin terminal

`ADMIN_EMAIL`: cuando la cuenta con exactamente ese correo (sin distinguir
mayúsculas) termina el alta por el enlace del correo —o ya existe y está activa
al arrancar— y **no hay ningún admin**, pasa a serlo. Es un solo `UPDATE`
condicionado (`… AND NOT EXISTS (SELECT 1 FROM users WHERE role = 'admin')`).
Es seguro porque el correo de una cuenta solo se fija verificándolo, y la
variable solo la pone quien despliega. Nunca se acepta una contraseña del
entorno en producción: `crear_admin.py` (para local) se niega a leer
`ADMIN_PASSWORD` si detecta un despliegue. Los correos solo valen en ASCII:
así `lower()` es el mismo en Python, SQLite y Postgres.

### Falla cerrado

La comprobación de arranque de la etapa 1 (`COOKIE_SEGURA`, `IP_DE_CABECERA`,
`SECRETO_APP`) exige ahora también un `DATABASE_URL` de Postgres, y corre
**antes** de abrir la base: un despliegue mal configurado no llega a crear un
`database.db` en un disco que se va a borrar. Producción se reconoce por
cualquiera de tres señales: `MEDSTUDY_PRODUCCION=1` (fija en `render.yaml`),
`RENDER` o un `APP_URL` https; con la primera, la app nunca cae en silencio a
SQLite aunque la plataforma dejara de poner la segunda.

### La imagen

`python:3.11.11-slim-bookworm` (etiqueta fija) con `poppler-utils`
(pdftoppm, pdfinfo) y `tzdata` por apt, las
dependencias de `requirements-web.txt` fijadas a las versiones probadas, un
usuario sin privilegios y el código de root (la app no puede reescribirse; solo
escribe en `assets/paginas`). `.dockerignore` deja fuera la base, `.git`, los
`.env`, las pruebas, las herramientas y las cachés. Arranca con
`uvicorn webapp:app --no-proxy-headers` en `$PORT`: la IP del cliente la decide
la app con `IP_DE_CABECERA`, nunca `X-Forwarded-For`.

---

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

La app **no copia nada**. Guarda las rutas (relativas a la carpeta de material,
ver «Rutas relativas») y lee los archivos donde están: las presentaciones
llegan a pesar 91 MB y duplicarlas no aporta nada.

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
| Resúmenes PDF | Sí, a `biblioteca/` (solo en local) | ~170 KB. La app queda autocontenida. En producción, con git. |
| Archivos de fichas | Se importan a la base | El contenido pasa a la base; el archivo ya no hace falta. |
| Presentaciones | **No** | Hasta 91 MB. De ellas se sacan el resumen y las fichas. |

## Piezas

| Archivo | Responsabilidad |
|---|---|
| `core.py` | Toda la lógica. No importa FastAPI ni ninguna UI. |
| `webapp.py` | HTTP: rutas, sesiones, permisos, topes y validación de entrada. |
| `correo.py` | Envío de correos (Brevo); `memoria` en pruebas, `consola` en local. |
| `crear_admin.py` | Crea la cuenta de administrador en local, sin escribir la contraseña en ningún sitio. |
| `static/` | `index.html` + `app.js` (la app), `login`, `cuenta`, `admin`, `ui.js` (diálogos). |
| `SCHEMA.sql` | Esquema, idempotente. La única fuente, también para Postgres. |
| `pg_schema.py` | Traduce `SCHEMA.sql` a Postgres. |
| `Dockerfile`, `render.yaml`, `requirements-web.txt` | La imagen y el despliegue en Render. |

Dentro de `core.py`:

- `slugify` / `tokenize` / `match_score` / `match_topic` — emparejamiento
- `schedule` / `retrievability` / `interval_for` — FSRS
- `AnkiImporter` / `AnkiExporter` — intercambio con Anki
- `hash_password` / `verify_password` / `ruta_permitida` — cuentas y archivos
- `ruta_guardada` / `ruta_real` — rutas relativas en la base, absolutas en el disco
- `a_postgres` / `ConexionPostgres` — dialecto y conexión de Postgres
- `DatabaseManager` — única dueña de la conexión (SQLite o Postgres): cuentas, progreso, notas
- `ingest` — escanea y arma los temas (idempotente)

El acceso a datos está aislado en `DatabaseManager`: el paso a Postgres cambió
esa clase y la conexión, no el resto de la app.

---

## Estructura de archivos

```
AppEstudio/
├── core.py                 # lógica: ingesta, FSRS, Anki, cuentas, persistencia
├── webapp.py               # servidor (FastAPI) — entrypoint
├── correo.py               # correos de alta y restablecer
├── crear_admin.py          # primera cuenta de administrador (en local)
├── SCHEMA.sql              # esquema de la base (única fuente)
├── pg_schema.py            # SCHEMA.sql traducido a Postgres
├── contenido/              # el material (va en la imagen)
├── static/                 # páginas, scripts y estilos
├── tests/                  # unittest: núcleo, registro, seguridad, aislamiento, postgres, despliegue
├── Dockerfile              # imagen de producción (con poppler)
├── render.yaml             # Blueprint de Render
├── requirements-web.txt    # dependencias de producción, fijadas
├── database.db             # (generado, local) contenido, cuentas, progreso y notas
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
