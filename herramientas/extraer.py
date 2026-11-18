"""
extraer.py — Saca el texto de las presentaciones para poder trabajarlas.

    python herramientas/extraer.py "/ruta/a/la/seccion"          # todas
    python herramientas/extraer.py "/ruta/archivo.pptx"          # una

Escribe un .txt por presentación en herramientas/extraido/.

Soporta dos formatos, porque el material mezcla ambos:
  .pptx  — es un zip con XML dentro; se lee con la librería estándar.
  .pdf   — presentaciones exportadas; se leen con pdftotext (poppler).

Un PDF solo se trata como presentación si su página es APAISADA. Los apuntes
y las tesis vienen en vertical (carta/A4) y no son material de estudio.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extraido")

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml$")


_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_IMG_EXT = (".png", ".jpeg", ".jpg", ".gif")


def texto_pptx(path: str) -> list[str]:
    """Devuelve el texto de cada diapositiva, en orden."""
    salida = []
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            (n for n in z.namelist() if _SLIDE_RE.match(n)),
            key=lambda n: int(_SLIDE_RE.match(n).group(1)))
        for n in slides:
            root = ET.fromstring(z.read(n))
            # Cada <a:p> es un párrafo; se juntan sus <a:t> para no partir
            # una frase que venga troceada en varios tramos con formato.
            lineas = []
            for p in root.iter(f"{_A}p"):
                trozos = [t.text for t in p.iter(f"{_A}t") if t.text]
                linea = "".join(trozos).strip()
                if linea:
                    lineas.append(linea)
            salida.append("\n".join(lineas))
    return salida


def imagenes_por_lamina(path: str) -> dict[int, list[str]]:
    """
    Qué imágenes lleva cada lámina.

    Buena parte del contenido (etiologías, algoritmos, tablas de tratamiento)
    vive solo en imágenes: la lámina trae el título y nada más. Sin este mapa
    habría que inventarse lo que dicen, que es justo lo que no se puede hacer.

    La relación lámina→imagen está en ppt/slides/_rels/slideN.xml.rels.
    """
    mapa: dict[int, list[str]] = {}
    with zipfile.ZipFile(path) as z:
        nombres = set(z.namelist())
        for n in nombres:
            m = _SLIDE_RE.match(n)
            if not m:
                continue
            num = int(m.group(1))
            rels = f"ppt/slides/_rels/slide{num}.xml.rels"
            if rels not in nombres:
                continue
            usadas = []
            for rel in ET.fromstring(z.read(rels)).iter(f"{_REL_NS}Relationship"):
                destino = rel.get("Target", "")
                if any(destino.lower().endswith(e) for e in _IMG_EXT):
                    usadas.append(os.path.normpath(
                        os.path.join("ppt/slides", destino)).replace("\\", "/"))
            if usadas:
                mapa[num] = usadas
    return mapa


def volcar_imagenes(path: str, laminas: list[int], destino: str) -> list[str]:
    """Saca a disco las imágenes de las láminas indicadas. Devuelve las rutas."""
    os.makedirs(destino, exist_ok=True)
    mapa = imagenes_por_lamina(path)
    escritas = []
    with zipfile.ZipFile(path) as z:
        for num in laminas:
            for i, interna in enumerate(mapa.get(num, []), 1):
                try:
                    datos = z.read(interna)
                except KeyError:
                    continue
                ext = os.path.splitext(interna)[1]
                fuera = os.path.join(destino, f"lamina{num:02d}_{i}{ext}")
                with open(fuera, "wb") as fh:
                    fh.write(datos)
                escritas.append(fuera)
    return escritas


def volcar_paginas_pdf(path: str, paginas: list[int], destino: str) -> list[str]:
    """
    Renderiza a PNG las páginas indicadas de una presentación en PDF.

    Equivale a volcar_imagenes() para los .pptx: da algo que mirar cuando la
    lámina no tiene texto extraíble.
    """
    os.makedirs(destino, exist_ok=True)
    escritas = []
    for num in paginas:
        prefijo = os.path.join(destino, f"lamina{num:02d}")
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", "110", "-f", str(num), "-l", str(num),
                 path, prefijo],
                capture_output=True, timeout=120, check=True)
        except (OSError, subprocess.SubprocessError):
            continue
        import glob as _glob
        for f in _glob.glob(prefijo + "-*.png"):
            destino_final = prefijo + ".png"
            os.replace(f, destino_final)
            escritas.append(destino_final)
    return escritas


def laminas_sin_texto(laminas: list[str], minimo: int = 2) -> list[int]:
    """Índices (1-based) de las láminas cuyo texto no llega a `minimo` líneas útiles."""
    flojas = []
    for i, texto in enumerate(laminas, 1):
        utiles = [l for l in texto.split("\n")
                  if len(l.strip()) > 3 and "http" not in l and not l.startswith("de Sequera")]
        if len(utiles) <= minimo:
            flojas.append(i)
    return flojas


def es_apaisado(path: str) -> bool:
    """True si el PDF es una presentación (página más ancha que alta)."""
    try:
        info = subprocess.run(["pdfinfo", path], capture_output=True,
                              text=True, timeout=20, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for linea in info.splitlines():
        if linea.startswith("Page size:"):
            nums = re.findall(r"[\d.]+", linea)
            if len(nums) >= 2:
                return float(nums[0]) > float(nums[1])
    return False


def texto_pdf(path: str) -> list[str]:
    """Texto de cada página del PDF, en orden."""
    try:
        crudo = subprocess.run(["pdftotext", "-layout", path, "-"],
                               capture_output=True, text=True,
                               timeout=120, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [p.strip() for p in crudo.split("\f")]


def extraer(path: str) -> dict | None:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pptx":
        laminas = texto_pptx(path)
    elif ext == ".pdf":
        if not es_apaisado(path):
            return None                      # apunte o tesis, no presentación
        laminas = texto_pdf(path)
    else:
        return None
    laminas = [l for l in laminas if l.strip()]
    return {"archivo": os.path.basename(path), "laminas": laminas,
            "palabras": sum(len(l.split()) for l in laminas)}


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    destino = sys.argv[1]
    rutas = ([destino] if os.path.isfile(destino) else
             sorted(os.path.join(destino, f) for f in os.listdir(destino)
                    if not f.startswith(".")))

    os.makedirs(OUT_DIR, exist_ok=True)
    total = 0
    for ruta in rutas:
        if not os.path.isfile(ruta):
            continue
        datos = extraer(ruta)
        if not datos:
            print(f"  omitido  {os.path.basename(ruta)}")
            continue
        nombre = os.path.splitext(datos["archivo"])[0] + ".txt"
        with open(os.path.join(OUT_DIR, nombre), "w", encoding="utf-8") as fh:
            for i, lamina in enumerate(datos["laminas"], 1):
                fh.write(f"\n--- lámina {i} ---\n{lamina}\n")
        print(f"  {len(datos['laminas']):3} láminas · {datos['palabras']:5} palabras  "
              f"{datos['archivo']}")
        total += 1
    print(f"\n{total} presentaciones extraídas en {OUT_DIR}")


if __name__ == "__main__":
    main()
