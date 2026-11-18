"""
generar.py — Convierte la ficha de un tema en su resumen PDF y su mazo.

    python herramientas/generar.py tema.json

Produce, dentro de la sección que indique el JSON:
    <sección>/Resumenes PDF/<Tema> - Resumen.pdf
    <sección>/Anki/<Tema>.apkg

El PDF se genera con Chrome sin interfaz, que es como se hicieron los
resúmenes de Neuro (lo dice su metadato: HeadlessChrome + Skia/PDF). El
estilo replica el de aquellos: granate #7c2231, tablas de cabecera rosada y
recuadros azules [Extra] para el complemento externo.

Formato del JSON:
{
  "seccion":  "Generalidades de MI",
  "tema":     "Trastornos del Sodio",
  "autor":    "R1 Medicina Interna",
  "fuentes":  "Guía X 2024, artículo Y",
  "resumen_html": "<h2>Definición</h2><ul><li>…</li></ul>",
  "fichas":   [{"p": "¿…?", "r": "…"}],
  "cloze":    ["La natremia normal es {{c1::135–145 mEq/L}}"]
}

Clases disponibles en resumen_html:
  <div class="cols">…</div>      dos columnas
  <div class="extra">…</div>     recuadro azul de complemento externo
  <table>…</table>               tabla con cabecera rosada
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
import tempfile
import zlib

CHROME = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

# Mismos IDs que los mazos de Neuro: si cambiaran, Anki trataría estos como
# tipos de nota distintos y duplicaría todo al importarlos junto a los otros.
MODELO_BASICO = 1607392319001
MODELO_CLOZE = 1607392319002

CSS = """
@page { size: letter; margin: 12mm 12mm; }
* { box-sizing: border-box; }
body { font: 8.8pt/1.36 -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
       color: #111; margin: 0; }
h1 { color: #7c2231; font-size: 18pt; margin: 0 0 2px; letter-spacing: -.01em; }
.sub { color: #8f8f8f; font-size: 7.3pt; line-height: 1.32; margin: 0 0 11px; }
h2 { color: #7c2231; font-size: 10pt; margin: 11px 0 4px; padding-bottom: 2px;
     border-bottom: 1px solid #e6d3d7; }
h3 { font-size: 8.9pt; margin: 7px 0 2px; }
ul { margin: 2px 0 0; padding-left: 15px; }
li { margin-bottom: 2.6px; }
p { margin: 2px 0; }
b, strong { font-weight: 650; }
table { width: 100%; border-collapse: collapse; margin: 5px 0 2px; font-size: 8pt; }
th { background: #f3e2e6; text-align: left; font-weight: 650; }
th, td { border: 1px solid #bbb; padding: 3px 5px; vertical-align: top; }
/* Una fila partida entre dos páginas es ilegible; el navegador prefiere
   empujar la fila entera a la página siguiente. */
tr { break-inside: avoid; }
h2, h3 { break-after: avoid; }
.cols { display: flex; gap: 20px; }
.cols > div { flex: 1; min-width: 0; }
.extra { background: #eef4fb; border-left: 3px solid #3a6ea5; padding: 6px 9px;
         margin: 7px 0 2px; font-size: 8.2pt; break-inside: avoid; }
.extra::before { content: "[Extra] "; color: #3a6ea5; font-weight: 700; }
"""

PLANTILLA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>{titulo} — Resumen</title><style>{css}</style></head><body>
<h1>{tema}</h1>
<div class="sub">Resumen de la presentación{autor} · {fuentes} · Recuadros azules
[Extra] = complemento externo.</div>
{cuerpo}
</body></html>"""


def hacer_pdf(spec: dict, destino: str) -> str:
    # resumen_html admite una lista de bloques: un HTML de dos páginas en una
    # sola línea de JSON es imposible de revisar o corregir.
    cuerpo = spec.get("resumen_html", "")
    if isinstance(cuerpo, list):
        cuerpo = "\n".join(cuerpo)
    autor = f" ({spec['autor']})" if spec.get("autor") else ""
    doc = PLANTILLA.format(
        titulo=html.escape(spec["tema"]), tema=html.escape(spec["tema"]),
        css=CSS, autor=html.escape(autor),
        fuentes=spec.get("fuentes", "—"), cuerpo=cuerpo)

    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        origen = os.path.join(tmp, "resumen.html")
        with open(origen, "w", encoding="utf-8") as fh:
            fh.write(doc)
        subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={destino}", origen],
            capture_output=True, timeout=120, check=True)
    return destino


def hacer_mazo(spec: dict, destino: str) -> str:
    import genanki

    tema = spec["tema"]
    seccion = spec["seccion"]

    basico = genanki.Model(
        MODELO_BASICO, "Neuro Basico",
        fields=[{"name": "Pregunta"}, {"name": "Respuesta"}, {"name": "Tema"}],
        templates=[{"name": "Tarjeta", "qfmt": "{{Pregunta}}",
                    "afmt": '{{FrontSide}}<hr id="answer">{{Respuesta}}'
                            '<div class="tema">{{Tema}}</div>'}],
        css=(".card{font-family:-apple-system,Helvetica,Arial;font-size:19px;"
             "text-align:left;color:#1c1c1e;background:#fff;padding:18px;line-height:1.55}"
             ".tema{margin-top:14px;font-size:13px;color:#8e8e93}"))

    cloze = genanki.Model(
        MODELO_CLOZE, "Neuro Cloze",
        fields=[{"name": "Texto"}, {"name": "Tema"}],
        templates=[{"name": "Cloze", "qfmt": "{{cloze:Texto}}",
                    "afmt": '{{cloze:Texto}}<div class="tema">{{Tema}}</div>'}],
        css=(".card{font-family:-apple-system,Helvetica,Arial;font-size:19px;"
             "text-align:left;color:#1c1c1e;background:#fff;padding:18px;line-height:1.55}"
             ".cloze{font-weight:700;color:#7c2231}"
             ".tema{margin-top:14px;font-size:13px;color:#8e8e93}"),
        model_type=genanki.Model.CLOZE)

    # ID del mazo derivado del nombre: estable entre ejecuciones, así que
    # reexportar el mismo tema actualiza en vez de duplicar.
    deck_id = 2059400110000 + (zlib.crc32(f"{seccion}::{tema}".encode()) % 100000)
    mazo = genanki.Deck(deck_id, f"{seccion}::{tema}")

    for f in spec.get("fichas", []):
        mazo.add_note(genanki.Note(model=basico, fields=[f["p"], f["r"], tema]))
    for texto in spec.get("cloze", []):
        mazo.add_note(genanki.Note(model=cloze, fields=[texto, tema]))

    os.makedirs(os.path.dirname(destino), exist_ok=True)
    genanki.Package(mazo).write_to_file(destino)
    return destino


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        spec = json.load(fh)

    for campo in ("seccion", "tema"):
        if not spec.get(campo):
            print(f"Falta '{campo}' en el JSON"); sys.exit(1)

    raiz = spec.get("raiz") or os.path.join(
        os.path.expanduser("~"), "Desktop", "Mariela", "PresentacionesResumen")
    base = os.path.join(raiz, spec["seccion"])
    if not os.path.isdir(base):
        print(f"No existe la sección: {base}"); sys.exit(1)

    pdf = hacer_pdf(spec, os.path.join(base, "Resumenes PDF",
                                       f"{spec['tema']} - Resumen.pdf"))
    apkg = hacer_mazo(spec, os.path.join(base, "Anki", f"{spec['tema']}.apkg"))

    n_fichas = len(spec.get("fichas", []))
    n_cloze = len(spec.get("cloze", []))
    paginas = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout
    paginas = next((l.split(":")[1].strip() for l in paginas.splitlines()
                    if l.startswith("Pages:")), "?")

    print(f"  {spec['tema']}")
    print(f"    resumen  {paginas} págs.  {os.path.relpath(pdf, raiz)}")
    print(f"    mazo     {n_fichas} fichas + {n_cloze} cloze  {os.path.relpath(apkg, raiz)}")


if __name__ == "__main__":
    main()
