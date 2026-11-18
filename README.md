# MedStudy — App de Estudio

App web para estudiar medicina interna: los materiales de cada tema en un
solo sitio, repaso espaciado sobre las fichas, y post-it pegados sobre los
resúmenes. **No usa IA.**

Tiene **cuentas**: el material (temas, fichas, resúmenes) es de todos, y el
progreso, las notas y los ajustes de estudio son de cada persona y privados.

---

## Instalación

Requiere **Python 3.10+** y **poppler** (para poder anotar los resúmenes):

```bash
pip install -r requirements.txt
brew install poppler
```

Sin poppler la app funciona igual, pero los resúmenes se ven en el visor del
navegador y no se les pueden pegar post-it.

## Ejecutar

```bash
python crear_admin.py      # solo la primera vez: crea tu cuenta de administrador
python webapp.py
```

Abre **http://localhost:8090** y entra con esa cuenta. `crear_admin.py` pide la
contraseña sin mostrarla (o la lee de `ADMIN_PASSWORD`); nunca la guardes en un
archivo del repositorio.

Cada vez que arranca escanea `contenido/` (en segundo plano, ~0,2 s) y arma los
temas solo, una carpeta por materia. Es idempotente: no duplica nada ni toca el
progreso de nadie.

## Cuentas

- **Registro público con correo verificado** (`/signup`): se pide solo el correo;
  el usuario y la contraseña se eligen al abrir el enlace que llega. Hace falta
  `APP_URL` y un modo de correo: en local, `CORREO_MODO=consola` escribe el
  enlace en la terminal. Sin correo, el registro se apaga y las cuentas se
  crean desde **/admin**.
- **Entrar** con usuario o correo (`/entrar`), **¿olvidaste tu contraseña?**
  (`/forgot-password`) y cambio de contraseña en Ajustes.
- **Admin** (`/admin`): crear cuentas, bloquear y activar, cerrar sesiones,
  poner una contraseña temporal. Solo el admin re-escanea, carga archivos y
  organiza carpetas; los demás estudian. La carga de archivos existe solo en
  local: en producción el material se agrega con git (ver «Desplegar»).

## Variables de entorno

Los nombres están en `.env.example`. Ninguna es obligatoria en local.

| Variable | Para qué |
|---|---|
| `PORT`, `HOST` | Puerto (8090) y dirección (127.0.0.1). |
| `DATABASE_URL` | Postgres (Neon). Sin ella, SQLite (`database.db`). **Obligatoria en producción.** Nunca en un archivo del repo ni en `.env`: la app no lee `.env`. |
| `ESTUDIO_CONTENT_DIR` | Carpeta de material (por defecto `contenido/`). |
| `APP_URL`, `APP_NOMBRE` | Dirección pública para los enlaces de los correos; nombre de la app. |
| `CORREO_MODO` | `consola` en local, `memoria` en las pruebas. Los dos se niegan con `COOKIE_SEGURA=1`. |
| `BREVO_API_KEY`, `CORREO_REMITENTE` | Envío real de correos (Brevo). |
| `COOKIE_SEGURA` | `1` al servir por HTTPS: cookie `Secure` y HSTS. |
| `SECRETO_APP` | Firma de la cookie de «dispositivo conocido». |
| `IP_DE_CABECERA` | Cabecera con la IP real detrás de un proxy (p. ej. `true-client-ip`). |
| `MAX_CORREOS_DIA`, `MAX_ALTAS_DIA` | Topes diarios de correos. |
| `DOCS` | `1` para servir `/docs`. |
| `ADMIN_EMAIL` | El primer admin sin terminal: la cuenta que se registre con ese correo, mientras no haya ningún admin. |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Solo para `crear_admin.py` en local sin terminal. En producción se niega a leer la contraseña. |
| `APP_DB_PATH` | Otra base SQLite (la usan las pruebas). |
| `TEST_PG_URL`, `APP_PG_SCHEMA` | Solo pruebas: correrlas contra un Postgres real (ver «Pruebas»). |

En producción (Render define `RENDER`, o `APP_URL` empieza por `https://`) la
app **no arranca** si falta `COOKIE_SEGURA=1`, `IP_DE_CABECERA`, `SECRETO_APP`
o un `DATABASE_URL` de Postgres: es mejor no arrancar que arrancar mal (con
SQLite en un disco que se borra, se perderían las cuentas y el progreso).

## Pruebas

```bash
python -m unittest discover -s tests
```

Corren contra una base y unas carpetas temporales: nunca tocan `database.db`
ni `contenido/`, y los correos quedan en memoria. `DATABASE_URL` se ignora
siempre: las pruebas jamás tocan la base de producción.

`tests/test_postgres.py` comprueba la traducción a Postgres **sin** Postgres:
recorre todos los métodos de la base, graba cada consulta y la traduce, y
falla si alguna llamada a la base de `core.py` quedó sin ejercitar. Para correr
además toda la batería contra un Postgres de verdad (por ejemplo, una rama de
pruebas en Neon; nunca la de producción):

```bash
TEST_PG_URL="postgresql://…" python -m unittest discover -s tests
```

Trabajan en un esquema `medstudy_prueba_…` propio que se borra al terminar.

---

## Desplegar (Render gratis + Neon)

Igual que EnglishApp: la app en **Render** (plan gratuito, con Docker para
tener poppler) y la base en **Neon** (Postgres gratuito). El disco de Render
se borra en cada reinicio, así que nada que importe vive en él.

**1. La base en Neon.** Crea un proyecto en [neon.tech](https://neon.tech) en la
región **AWS us-east-2 (Ohio)**, la misma que la app (cada consulta viaja entre
las dos). En *Connect* copia la cadena de conexión **directa** (con *Connection
pooling* apagado; funciona también con el pooler, pero la directa es la
recomendada): `postgresql://usuario:clave@ep-….us-east-2.aws.neon.tech/neondb?sslmode=require`.
No la pegues en ningún archivo: va solo en el panel de Render.

**2. El servicio en Render.** *New → Blueprint*, elige este repositorio y Render
lee `render.yaml` (un servicio web, Docker, plan gratuito, Ohio, se despliega
solo desde `main`). Te pedirá las variables que no van en el archivo:

| Variable | Qué poner |
|---|---|
| `DATABASE_URL` | La cadena de Neon del paso 1. |
| `APP_URL` | La dirección pública, p. ej. `https://medstudy.onrender.com` (la ves al crear el servicio; si la cambias, vuelve a desplegar). |
| `BREVO_API_KEY`, `CORREO_REMITENTE` | Los de Brevo (pueden ser los de EnglishApp). Sin ellos no hay registro ni «olvidé mi contraseña». |
| `ADMIN_EMAIL` | **Tu** correo: con él saldrá el primer administrador. |

`COOKIE_SEGURA`, `IP_DE_CABECERA`, `APP_NOMBRE` y `HOST` ya vienen en el
archivo, y `SECRETO_APP` lo genera Render.

**3. El primer arranque.** Render construye la imagen (con poppler y con todo
`contenido/` dentro), la app crea las tablas en Neon y escanea el material.
La comprobación de salud es `/entrar`, que responde enseguida aunque el escaneo
siga en segundo plano.

**4. Tu cuenta de administrador.** Render gratis no tiene terminal, así que
no se usa `crear_admin.py`: abre `APP_URL/signup`, regístrate con el correo de
`ADMIN_EMAIL`, abre el enlace que te llega y elige usuario y contraseña. Esa
cuenta es la administradora. Solo funciona mientras no exista ningún admin:
después la variable ya no hace nada (puedes borrarla). Ninguna contraseña va
nunca en el entorno.

**5. Publicar material nuevo.** En producción no hay botón de cargar archivos
(se borrarían en el siguiente reinicio). El material se publica con git:

```bash
# deja los archivos en su materia, con la estructura de siempre:
#   contenido/<Materia>/Resumenes PDF/…pdf · Anki/…apkg · Articulos/…pdf
git add "contenido/Neuro/Resumenes PDF/Mareo - Resumen.pdf"
git commit -m "Material: resumen de Mareo"
git push            # a main (o fusiona tu rama en main)
```

Render vuelve a construir y desplegar solo. Al arrancar, la app incorpora lo
nuevo y lo muestra arriba, en «Recién agregado», con la marca **Nuevo** (tema
nuevo) o **Actualizado** (material nuevo de un tema que ya existía) durante 21
días. El progreso de nadie se toca.

**Bueno saber:**

- El plan gratuito **se duerme** tras 15 minutos sin visitas: la primera visita
  tarda en despertarlo, y durante unos segundos la barra lateral puede decir
  «Actualizando el material…» mientras corre el escaneo.
- Neon también suspende la base sin uso; la app reconecta sola.
- La caché de páginas renderizadas vive en el disco de Render y se pierde al
  reiniciar: se rehace sola la primera vez que se abre cada página.
- La base local (`database.db`) **no** se copia a producción: allí las cuentas
  y el progreso empiezan de cero.

---

## Cómo está organizado

```
Carpeta          →  Tema          →  Fichas + resumen + presentación + notas
«Neurología»        «Vértigo»        35 fichas · 2 págs. · post-it
```

- **Carpetas**: las creas tú desde la barra lateral (＋). Una por materia, por
  rotación, por examen. Arrastra temas entre ellas.
- **Tema**: la unidad de la app. Reúne todo lo de un asunto.
- **Fichas**: las preguntas. Se estudian con repetición espaciada dentro de la
  app — no hace falta abrir nada más.

---

## Qué hace

| Función | Cómo |
|---|---|
| **Arranque sin fricción** | Escanea el material y arma los 20 temas ya enlazados. |
| **Repasar** | Repetición espaciada (FSRS). Todo, una carpeta o un solo tema. |
| **Teclado** | <kbd>espacio</kbd> mostrar · <kbd>1</kbd> otra vez · <kbd>2</kbd> difícil · <kbd>3</kbd> bien · <kbd>4</kbd> fácil |
| **Resumen anotable** | Las páginas se ven dentro de la app y admiten post-it. |
| **Post-it** | «📌 Pegar post-it» y clic donde quieras. Se arrastran y se editan. |
| **Notas generales** | En el panel derecho, para lo que no va en un punto concreto. |
| **Panel derecho** | Al repasar un tema, el resumen queda a la vista para consultarlo. |
| **Cargar archivos** | PDF y archivos de fichas `.apkg` (hasta 25 MB), desde la barra lateral. Solo admin y solo en local: en producción, con git. |
| **Modo oscuro** | Tres estados: automático, claro y oscuro (abajo a la izquierda). |
| **Sin emparejar** | Lo que la ingesta no pudo asignar se ve en la UI para hacerlo a mano. |

### El material actual

20 temas · 745 fichas (617 básicas + 39 cloze) · 61 materiales enlazados.

---

## Ritmo diario

En **Ajustes** (cada persona el suyo):

- **Fichas nuevas al día** (20 por defecto). Es el freno importante: con 745
  fichas, 20/día cubre todo en unas cinco semanas. Subirlo mucho hace que el
  atraso crezca solo.
- **Repasos máximos al día** (200).
- **Retención objetivo** (0.90). Más alta = intervalos más cortos y más
  repasos; más baja = menos repasos y más olvidos.

La cola sirve primero lo fallado, luego lo vencido y al final lo nuevo. Ese
orden es deliberado: meter material nuevo con el atraso creciendo es la forma
más rápida de abandonar.

---

## Sobre los archivos de fichas

La app lee los `.apkg` que ya tienes y puede volver a generarlos. Reimportar un
archivo actualizado **no duplica fichas ni reinicia el progreso** —se emparejan
por el identificador de la nota original—, así que puedes re-escanear las veces
que quieras.

Si un archivo viene en el formato nuevo comprimido y no se puede leer, la app
lo dice y te pide reexportarlo marcando *«Soporte para versiones anteriores»*.

---

## Dónde vive cada cosa

```
AppEstudio/
├── core.py             # lógica: ingesta, repaso, cuentas, persistencia
├── webapp.py           # servidor — ejecuta esto
├── correo.py           # correos de alta y de restablecer (Brevo)
├── crear_admin.py      # crea la cuenta de administrador (en local)
├── SCHEMA.sql          # esquema de la base (la única fuente, también para Postgres)
├── pg_schema.py        # traduce SCHEMA.sql a Postgres
├── contenido/          # el material: va en el repo y en la imagen
├── static/             # interfaz: index.html + app.js, entrar, cuenta, admin
├── tests/              # pruebas (unittest)
├── Dockerfile          # la imagen de producción (con poppler)
├── render.yaml         # el despliegue en Render (Blueprint)
├── requirements-web.txt# dependencias de producción, fijadas
├── database.db         # (se genera, solo en local) temas, fichas, cuentas, progreso y notas
├── biblioteca/         # (se genera, solo en local) los PDF que cargas desde la app
├── assets/paginas/     # (se genera) caché de páginas renderizadas
```

En local, `database.db` guarda las cuentas, el progreso y las notas de todos:
es el único archivo que vale la pena respaldar. En producción eso vive en Neon.
El material vive en `contenido/`.

Diseño y decisiones técnicas: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Cambiar la carpeta de material

Por defecto es `contenido/`, dentro del proyecto. Solo se cambia al arrancar
(no desde la app: la API no puede elegir qué carpeta se lee):

```bash
ESTUDIO_CONTENT_DIR="/ruta/a/otro/material" python webapp.py
```

La estructura que espera:

```
material/
└── <Materia>/             ← Neuro, Gastro, Generalidades de MI… (una carpeta de la app)
    ├── Resumenes PDF/*.pdf  ← resúmenes (subcarpeta que empiece con «resumen»)
    ├── Anki/*.apkg          ← archivos de fichas: definen los temas
    └── Articulos/*.pdf      ← artículos relacionados (se enlazan al tema por su nombre)

Las presentaciones NO van al repo: pesan demasiado. De ellas se sacan el
resumen y las fichas, que sí van.

Lo que se agrega después del primer escaneo (un tema nuevo, o un resumen o
artículo nuevo para un tema que ya existía) aparece primero, en «Recién
agregado», con la marca «Nuevo» o «Actualizado» durante 21 días.
```
