"""
Pruebas de core.py. No necesitan servidor ni red.

    python tests/test_core.py
    (también corre con: python -m unittest discover -s tests)

Cubren las dos partes donde un error sería silencioso y caro: el
emparejamiento de archivos con temas (un archivo mal asignado se nota tarde)
y FSRS (un intervalo mal calculado degrada el estudio sin dar señales).
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

fails: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  OK   " if condition else "  FALLA") + "  " + label + (f"  ({detail})" if detail else ""))
    if not condition:
        fails.append(label)


def ejecutar() -> None:
    # --------------------------------------------------------------------------- #
    print("\nNormalización y emparejamiento")
    # --------------------------------------------------------------------------- #
    check("quita acentos", core.strip_accents("Vértigo Epiléptico") == "Vertigo Epileptico")
    check("slug estable", core.slugify("Enfermedad de Alzheimer") == "enfermedad-de-alzheimer")
    # La extensión se recorta en match_score(), no en tokenize(): sin eso,
    # 'Vertigo.pdf' contra 'Vértigo' bajaría de 1.00 a 0.50 por el token 'pdf'.
    check("la extensión no diluye el puntaje",
          core.match_score("Vertigo.pdf", "Vértigo") == 1.0,
          f"{core.match_score('Vertigo.pdf', 'Vértigo'):.2f}")
    check("expande siglas", {"evento", "cerebral", "vascular"} <= core.tokenize("EVC Isquémico"))

    # Los casos reales que rompieron la primera versión del emparejador.
    CASOS = [
        ("Enfermedad de Alzheimer CORREGIDA.pptx", "Enfermedad de Alzheimer"),
        ("Alzheimer - Resumen.pdf", "Enfermedad de Alzheimer"),
        ("EVENTO CEREBRAL VASCULAR ISQUEMICO.pptx", "EVC Isquémico"),
        ("Sx Epilepticos.pdf", "Síndromes Epilépticos"),
        ("OTRAS DEMENCIAS FRONTOTEMPORALES.pptx", "Demencias FT, Lewy y Vascular"),
        ("esclerosis .pptx", "Esclerosis Múltiple"),
        ("EXPLORACIÓN NEUROLÓGICA.pptx", "Exploración Neurológica"),
        ("Exploración física neurología.pdf", "Exploración Física Neurológica"),
        ("Estatus epiléptico DIANA.pptx", "Estatus Epiléptico"),
        ("CIDP - Resumen.pdf", "Polineuropatía Desmielinizante Crónica (CIDP)"),
    ]
    TEMAS = [{"id": i, "title": t} for i, t in enumerate(sorted({c[1] for c in CASOS}))]

    for archivo, esperado in CASOS:
        topic, score = core.match_topic(archivo, TEMAS)
        got = topic["title"] if topic else "— ninguno —"
        check(f"{archivo[:44]:46} → {esperado[:34]}", got == esperado, f"dio {got}, {score:.2f}")

    huerfano, score = core.match_topic("Tesis capitulo 3.pdf", TEMAS)
    check("lo que no empata queda sin asignar", huerfano is None, f"score {score:.2f}")

    # --------------------------------------------------------------------------- #
    print("\nFSRS")
    # --------------------------------------------------------------------------- #
    nueva = {"state": "new"}
    por_nota = {r: core.schedule(dict(nueva), r) for r in (1, 2, 3, 4)}
    check("mejor nota → más estabilidad",
          por_nota[1]["stability"] < por_nota[2]["stability"]
          < por_nota[3]["stability"] < por_nota[4]["stability"])
    check("'Otra vez' se repite hoy mismo", por_nota[1]["interval"] == 0)
    check("'Otra vez' pasa a aprendizaje", por_nota[1]["state"] == "learning")

    # Propiedad que define FSRS: a t = S, la retención es exactamente la objetivo.
    r_en_s = core.retrievability(10.0, 10.0)
    check("R(t=S) = retención objetivo", abs(r_en_s - 0.90) < 0.005, f"R={r_en_s:.4f}")
    check("R decae con el tiempo",
          core.retrievability(1, 10) > core.retrievability(10, 10) > core.retrievability(30, 10))

    card, dia, previo = dict(nueva), datetime.date(2026, 1, 1), 0
    crece = True
    for _ in range(8):
        out = core.schedule(card, 3, today=dia)
        crece = crece and out["interval"] > previo
        previo = out["interval"]
        card = dict(card, **out)
        card["last_review"] = dia.isoformat()
        dia += datetime.timedelta(days=out["interval"])
    check("acertar siempre → intervalos crecientes", crece, f"último {previo} d")

    antes = card["stability"]
    fallo = core.schedule(card, 1, today=dia)
    check("fallar nunca sube la estabilidad", fallo["stability"] < antes,
          f"{antes:.1f} → {fallo['stability']:.1f}")
    check("fallar cuenta un lapse", fallo["lapses"] == 1)

    madura = {"state": "review", "stability": 10.0, "difficulty": 5.0,
              "reps": 5, "last_review": "2026-01-01"}
    iv = core.preview_intervals(madura)
    check("orden de intervalos: otra vez < difícil < bien < fácil",
          iv["again"] < iv["hard"] < iv["good"] < iv["easy"], str(iv))

    dura = dict(nueva)
    for _ in range(20):
        dura = dict(dura, **core.schedule(dura, 1, today=datetime.date(2026, 6, 1)))
    facil = dict(nueva)
    for _ in range(20):
        facil = dict(facil, **core.schedule(facil, 4, today=datetime.date(2026, 6, 1)))
    check("dificultad acotada a 1..10",
          1.0 <= facil["difficulty"] and dura["difficulty"] <= 10.0,
          f"fácil {facil['difficulty']:.2f}, difícil {dura['difficulty']:.2f}")

    # --------------------------------------------------------------------------- #
    print("\nCloze")
    # --------------------------------------------------------------------------- #
    frente, reverso = core.render_cloze(
        "La triada es {{c1::temblor}}, {{c2::rigidez}} y bradicinesia", 1)
    check("tapa solo el hueco pedido", "[…]" in frente and "rigidez" in frente)
    check("el reverso revela la respuesta", "<b>temblor</b>" in reverso)
    frente2, _ = core.render_cloze("Dosis: {{c1::10 mg::cantidad}}", 1)
    check("usa la pista cuando existe", "[cantidad]" in frente2, frente2)

    # --------------------------------------------------------------------------- #
    print("\nClasificación de archivos")
    # --------------------------------------------------------------------------- #
    raiz = "/x/PresentacionesResumen"

    # Material organizado por secciones: <raíz>/<sección>/…
    check("un .pptx de una sección es presentación",
          core._classify(f"{raiz}/Neuro/DELIRIUM.pptx", raiz) == ("Neuro", "presentacion"))
    check("la carpeta raíz no se confunde con 'Resumen'",
          core._classify(f"{raiz}/Neuro/Vertigo.pdf", raiz) == ("Neuro", "presentacion"))
    check("un PDF de la subcarpeta es resumen",
          core._classify(f"{raiz}/Neuro/Resumenes PDF/Vertigo - Resumen.pdf", raiz)
          == ("Neuro", "resumen"))
    check("un .apkg es mazo",
          core._classify(f"{raiz}/Neuro/Anki/Vertigo.apkg", raiz) == ("Neuro", "anki"))
    check("la sección se detecta correctamente",
          core._classify(f"{raiz}/Generalidades de MI/sepsis.pptx", raiz)
          == ("Generalidades de MI", "presentacion"))

    # Material plano en la raíz, sin secciones: sigue funcionando.
    check("sin secciones, el material plano sigue clasificándose",
          core._classify(f"{raiz}/DELIRIUM.pptx", raiz) == ("", "presentacion"))
    check("sin secciones, el resumen sigue clasificándose",
          core._classify(f"{raiz}/Resumenes PDF/Vertigo - Resumen.pdf", raiz)
          == ("", "resumen"))
    check("una subcarpeta desconocida se ignora",
          core._classify(f"{raiz}/Neuro/Borradores/algo.pptx", raiz) is None)


class PruebasCore(unittest.TestCase):
    """Envoltura para unittest discover: las comprobaciones son las mismas."""

    def test_core(self):
        fails.clear()
        ejecutar()
        self.assertEqual(fails, [], "; ".join(fails))


if __name__ == "__main__":
    ejecutar()
    print()
    if fails:
        print(f"{len(fails)} FALLAS: " + "; ".join(fails))
        sys.exit(1)
    print("Todo correcto.")
