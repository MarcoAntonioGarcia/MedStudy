# Neuro — App de Estudio

App web **local** para estudiar neurología: los materiales de cada tema en un
solo sitio, repaso espaciado sobre las fichas, y post-it pegados sobre los
resúmenes.

Corre en tu máquina y se abre en el navegador. Tu material, tu base de datos,
tu equipo. **No usa IA y no sube nada a ninguna parte.**

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
python webapp.py
```

Abre **http://localhost:8090**.

La primera vez escanea `../PresentacionesResumen/` y arma los temas solo.

## Pruebas

```bash
python tests/test_core.py
```

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
| **Cargar archivos** | PDF y archivos de fichas `.apkg`, desde la barra lateral. |
| **Presentaciones** | Se abren con Keynote/PowerPoint. No se copian: pesan hasta 91 MB. |
| **Modo oscuro** | Tres estados: automático, claro y oscuro (abajo a la izquierda). |
| **Sin emparejar** | Lo que la ingesta no pudo asignar se ve en la UI para hacerlo a mano. |

### El material actual

20 temas · 745 fichas (617 básicas + 39 cloze) · 61 materiales enlazados.

---

## Ritmo diario

En **Ajustes**:

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
├── core.py             # lógica: ingesta, repaso, fichas, notas, persistencia
├── webapp.py           # servidor local — ejecuta esto
├── SCHEMA.sql          # esquema de la base
├── static/index.html   # interfaz
├── tests/test_core.py  # pruebas
├── database.db         # (se genera) temas, fichas, notas y progreso
├── biblioteca/         # (se genera) los PDF que cargas desde la app
├── assets/paginas/     # (se genera) caché de páginas renderizadas
└── exports/            # (se genera) archivos de fichas exportados
```

`database.db` es todo tu progreso y todas tus notas. Es el único archivo que
vale la pena respaldar; el material sigue viviendo en
`../PresentacionesResumen/`.

Diseño y decisiones técnicas: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Cambiar la carpeta de material

Por defecto es la hermana `../PresentacionesResumen`. Se cambia desde
**Ajustes**, o al arrancar:

```bash
ESTUDIO_CONTENT_DIR="/ruta/a/otro/material" python webapp.py
```

La estructura que espera:

```
material/
├── *.pptx, *.pdf          ← presentaciones (nivel superior)
├── Resumenes PDF/*.pdf    ← resúmenes (subcarpeta que empiece con «resumen»)
└── Anki/*.apkg            ← archivos de fichas
```
