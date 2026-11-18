"""
verificar.py — Segunda pasada: ¿el resumen cubre lo que dice la presentación?

    python herramientas/verificar.py herramientas/temas/<tema>.json

Recorre la presentación lámina por lámina y comprueba qué proporción de sus
términos distintivos aparece en el resumen o en las fichas. No juzga si el
contenido es correcto —eso es criterio médico— sino si algo se quedó fuera.

Dos avisos distintos, y conviene no confundirlos:

  FALTA    la lámina tiene texto y ese texto no está recogido. Hay que
           añadirlo o justificar por qué se omite.
  REVISAR  la lámina no tiene texto: su contenido está en imágenes. La
           herramienta no puede comprobarla; hay que mirarla a ojo.

El segundo aviso es el importante: es justo donde vive la mitad del contenido
de estas presentaciones y donde es fácil dejarse algo, como pasó con la
clasificación de gravedad de la hiperpotasemia.
"""

from __future__ import annotations

import collections
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extraer  # noqa: E402

# Palabras que aparecen en cualquier lámina y no distinguen nada.
RUIDO = {
    "para", "por", "con", "las", "los", "del", "que", "una", "uno", "como",
    "más", "mas", "sus", "este", "esta", "estos", "estas", "puede", "pueden",
    "ser", "son", "está", "estan", "están", "entre", "sobre", "cuando", "sin",
    "the", "and", "disponible", "recuperado", "eds", "issn", "http", "https",
    "www", "pdf", "figura", "tabla", "gracias", "introduccion", "introducción",
    # Palabras de las citas en inglés. Los resúmenes se escriben en
    # castellano, así que ninguna de estas puede ser contenido propio.
    "kidney", "disease", "improving", "global", "outcomes", "acute", "injury",
    "work", "group", "clinical", "practice", "guideline", "guidelines", "public",
    "review", "draft", "brussels", "retrieved", "available", "journal", "volume",
    "medicine", "published", "university", "press", "official", "society", "american",
    "european", "international", "statement", "article", "chapter", "editors",
}


def normalizar(texto: str) -> str:
    base = unicodedata.normalize("NFKD", texto or "").lower()
    return "".join(c for c in base if not unicodedata.combining(c))


# Las láminas usan la nomenclatura anglosajona (kalemia) y el resumen la
# castellana (potasemia). Sin esto, cada lámina titulada «HIPERKALEMIA» se
# marcaría como no cubierta aunque el tema esté entero.
SINONIMOS = {
    "hiperkalemia": "hiperpotasemia", "hipokalemia": "hipopotasemia",
    "kalemia": "potasemia", "hipernatremia": "hipernatremia",
    "hiponatremia": "hiponatremia", "manejo": "tratamiento",
    "clinica": "manifestaciones", "definicion": "define",
}


def terminos(texto: str) -> set[str]:
    """Términos distintivos: palabras largas y cifras con unidad."""
    plano = normalizar(texto)
    palabras = {w for w in re.findall(r"[a-z]{5,}", plano) if w not in RUIDO}
    cifras = set(re.findall(r"\d+[.,]?\d*", plano))
    # Cada término se acompaña de su sinónimo, en ambos sentidos, para que
    # coincida escriba como escriba cada lado.
    equivalentes = set()
    for p in palabras:
        if p in SINONIMOS:
            equivalentes.add(SINONIMOS[p])
        equivalentes.update(k for k, v in SINONIMOS.items() if v == p)
    return palabras | cifras | equivalentes


# Portadas, cierres y separadores: no llevan contenido que verificar.
_SIN_CONTENIDO = re.compile(
    r"^\s*(gracias|muchas gracias|indice|índice|contenido|bibliografia|"
    r"bibliografía|referencias)\s*$", re.IGNORECASE)

# Casi todas las láminas llevan su cita al pie. Sus apellidos y años se
# marcarían siempre como «no recogidos» —el resumen no cita autor por autor—
# y ahogarían los avisos de verdad.
_CITA = re.compile(
    r"\(\s*(19|20)\d{2}\s*\)"          # (2022)
    r"|\bet al\b|\bdoi\b|https?://"
    r"|\(Eds?\.\)|\bEd\.\)|\d+\.[ªa] ed\."
    r"|\b(?:pp?|vol)\.\s*\d"
    r"|^[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+,\s+[A-ZÁÉÍÓÚ]"      # «Loscalzo, J.» y «Bellani, Giacomo»
    r"|,\s*(19|20)\d{2}\.?\s*$"                    # línea que acaba en el año
    r"|\b(Springer|Elsevier|McGraw|Wolters|Lippincott|StatPearls|Publishing)\b"
    r"|\bPMID\b|\bPMCID\b|\bdoi:|10\.\d{4}/"          # DOI y códigos de PubMed
    r"|\b\d{1,4}\(\d+\):\s*\d"                          # volumen(número):página
    r"|\b(Crit|Care|Med|Nephrol|Nephrology|Lancet|BMJ|JAMA|Kidney|Int|Chest|CHEST|"
    r"Intensive|Diagnostics|Reviews|Revews|Trial|Randomized|Meta-analysis)\b.*\d{4}",
    re.IGNORECASE | re.MULTILINE)


def es_cita(linea: str) -> bool:
    return bool(_CITA.search(linea))


def texto_del_tema(spec: dict) -> str:
    partes = spec.get("resumen_html", "")
    if isinstance(partes, list):
        partes = " ".join(partes)
    for ficha in spec.get("fichas", []):
        partes += " " + ficha.get("p", "") + " " + ficha.get("r", "")
    partes += " " + " ".join(spec.get("cloze", []))
    return re.sub(r"<[^>]+>", " ", partes)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        spec = json.load(fh)

    origen = spec.get("origen")
    if not origen or not os.path.exists(origen):
        print("El JSON necesita 'origen' con la ruta de la presentación."); sys.exit(1)

    laminas = (extraer.texto_pptx(origen) if origen.lower().endswith(".pptx")
               else extraer.texto_pdf(origen))
    cubierto = terminos(texto_del_tema(spec))
    es_pdf = not origen.lower().endswith(".pptx")
    mapa_img = ({} if es_pdf else extraer.imagenes_por_lamina(origen))

    print(f"\n{spec['tema']}")
    print(f"  {len(laminas)} láminas · {len(spec.get('fichas', []))} fichas "
          f"· {len(spec.get('cloze', []))} cloze\n")

    # Al añadir contenido en varias pasadas es fácil repetir una pregunta;
    # en el mazo aparecerían como dos fichas distintas que se repasan por
    # separado, y no hay nada en la app que lo detecte.
    repes = [(p, n) for p, n in
             collections.Counter(f.get("p", "") for f in spec.get("fichas", [])).items()
             if n > 1]
    for pregunta, n in repes:
        print(f"  REPETIDA x{n}  {pregunta[:70]}")
    if repes:
        print()

    # Una línea que se repite en varias láminas es plantilla: el pie con la
    # cita, el logo, el nombre del módulo. No es contenido de esa lámina, y si
    # se contara, cada lámina aparecería como «no cubierta» por culpa del pie.
    frecuencia = collections.Counter(
        l.strip() for t in laminas for l in t.split("\n") if len(l.strip()) > 12)
    umbral = max(3, len(laminas) // 6)
    plantilla = {l for l, n in frecuencia.items() if n >= umbral}

    faltan, revisar, ok = [], [], 0
    for i, texto in enumerate(laminas, 1):
        utiles = [l for l in texto.split("\n")
                  if len(l.strip()) > 3 and not es_cita(l)
                  and l.strip() not in plantilla]
        # La portada (lámina 1) es el título, el autor y el hospital: nada que
        # verificar, y sus nombres propios se marcarían siempre como perdidos.
        if i == 1 or (utiles and all(_SIN_CONTENIDO.match(l) for l in utiles)):
            continue
        # Al separar un PDF por páginas salen huecos vacíos: no son láminas.
        if not utiles and not mapa_img.get(i):
            if es_pdf:
                revisar.append((i, "(página sin texto — hay que renderizarla)", 0))
            continue
        propios = terminos("\n".join(utiles))
        # Se descartan los términos que solo aparecen en la cita bibliográfica.
        propios = {t for t in propios if len(t) > 2}

        if len(propios) < 4:
            if mapa_img.get(i):
                revisar.append((i, utiles[0][:58] if utiles else "(sin título)",
                                len(mapa_img.get(i, []))))
            continue

        hallados = propios & cubierto
        pct = len(hallados) / len(propios)
        if pct < 0.55:
            faltan.append((i, utiles[0][:58] if utiles else "?", pct,
                           sorted(propios - hallados)[:9]))
        else:
            ok += 1

    for i, titulo, pct, perdidos in faltan:
        print(f"  FALTA    lámina {i:>2}  {titulo}")
        print(f"           cubierto {pct:.0%} · sin recoger: {', '.join(perdidos)}")
    for i, titulo, n in revisar:
        print(f"  REVISAR  lámina {i:>2}  {titulo}  ({n} imagen/es, sin texto)")

    print(f"\n  {ok} láminas cubiertas · {len(faltan)} con texto sin recoger "
          f"· {len(revisar)} que exigen mirar la imagen")
    if faltan:
        print("\n  Revisa las marcadas FALTA antes de dar el tema por bueno.")


if __name__ == "__main__":
    main()
