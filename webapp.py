"""
webapp.py — App de Estudio (Neurología). Servidor local.

    python webapp.py        ->  http://localhost:8090

Es una capa fina sobre core.py: aquí solo van rutas HTTP y validación de
entrada. Toda la lógica (ingesta, repaso espaciado, fichas) vive en core.py,
para que un front distinto —o la línea de comandos— pueda usar lo mismo.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

import core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
INDEX_PATH = os.path.join(STATIC_DIR, "index.html")

# 200 MB: la presentación más grande del material ronda los 91 MB.
MAX_UPLOAD = 200 * 1024 * 1024

app = FastAPI(title="App de Estudio — Neurología")
db = core.DatabaseManager()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(INDEX_PATH, "r", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


# --------------------------------------------------------------------------- #
# Estado general
# --------------------------------------------------------------------------- #
@app.get("/api/stats")
def get_stats() -> dict:
    return db.stats()


@app.get("/api/bootstrap")
def bootstrap() -> dict:
    """Todo lo que la UI necesita para pintarse. Una llamada en vez de cuatro."""
    return {
        "folders": db.list_folders(),
        "topics": db.list_topics(),
        "orphans": db.list_orphans(),
        "stats": db.stats(),
        "settings": _settings_payload(),
        "pdf_ok": core.PdfRenderer.available(),
    }


# --------------------------------------------------------------------------- #
# Carpetas
# --------------------------------------------------------------------------- #
@app.get("/api/folders")
def list_folders() -> list[dict]:
    return db.list_folders()


@app.post("/api/folders")
def create_folder(payload: dict = Body(...)) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "La carpeta necesita un nombre")
    fid = db.ensure_folder(name, payload.get("color") or "#c9a86a")
    return {"id": fid, "folders": db.list_folders()}


@app.post("/api/folders/{folder_id}")
def update_folder(folder_id: int, payload: dict = Body(...)) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "La carpeta necesita un nombre")
    if not db.rename_folder(folder_id, name, payload.get("color")):
        raise HTTPException(404, "Carpeta no encontrada")
    return {"folders": db.list_folders()}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int) -> dict:
    """Borra la carpeta. Los temas que contenía NO se borran, quedan sueltos."""
    if not db.delete_folder(folder_id):
        raise HTTPException(404, "Carpeta no encontrada")
    return {"folders": db.list_folders(), "topics": db.list_topics()}


@app.post("/api/topics/{topic_id}/folder")
def move_topic(topic_id: int, payload: dict = Body(...)) -> dict:
    folder_id = payload.get("folder_id")
    folder_id = int(folder_id) if folder_id not in (None, "", 0) else None
    if not db.move_topic(topic_id, folder_id):
        raise HTTPException(404, "Tema no encontrado")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Temas
# --------------------------------------------------------------------------- #
@app.get("/api/topics")
def list_topics() -> list[dict]:
    return db.list_topics()


@app.get("/api/topics/{topic_id}")
def get_topic(topic_id: int) -> dict:
    topic = db.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Tema no encontrado")
    topic["cards"] = db.topic_cards(topic_id)
    topic["notes"] = db.list_notes(topic_id)
    resumen = (topic["assets"].get("resumen") or [None])[0]
    topic["pages"] = core.PdfRenderer.page_count(resumen["path"]) if resumen else 0
    return topic


@app.post("/api/ingest")
def run_ingest(payload: dict = Body(default={})) -> dict:
    content_dir = (payload or {}).get("content_dir") or db.get_setting(
        "content_dir", core.CONTENT_DIR)
    if not os.path.isdir(content_dir):
        raise HTTPException(400, f"No existe la carpeta: {content_dir}")
    db.set_setting("content_dir", content_dir)
    return core.ingest(db, content_dir)


@app.get("/api/orphans")
def list_orphans() -> list[dict]:
    return db.list_orphans()


@app.post("/api/orphans/{orphan_id}/assign")
def assign_orphan(orphan_id: int, payload: dict = Body(...)) -> dict:
    topic_id = int(payload.get("topic_id") or 0)
    if not db.get_topic(topic_id):
        raise HTTPException(400, "Tema inválido")
    if not db.assign_orphan(orphan_id, topic_id):
        raise HTTPException(404, "Archivo no encontrado")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Carga de archivos
# --------------------------------------------------------------------------- #
@app.post("/api/upload")
async def upload(topic_id: int | None = None, kind: str = "auto",
                 file: UploadFile = File(...)) -> dict:
    """
    Carga un archivo desde la app.

    Los PDF se copian a la biblioteca (son ligeros). Los archivos de fichas se
    importan a la base y el archivo en sí ya no hace falta. Las presentaciones
    NO se aceptan por aquí: pesan demasiado para duplicarlas, se consultan
    desde la carpeta de material.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "El archivo supera los 200 MB")
    name = file.filename or "archivo"
    ext = os.path.splitext(name)[1].lower()

    if ext == ".apkg":
        tmp = core.save_upload(name, data)
        try:
            deck = core.AnkiImporter.read(tmp)
        except core.AnkiFormatError as exc:
            os.remove(tmp)
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            os.remove(tmp)
            raise HTTPException(400, f"No se pudo leer el archivo: {exc}") from exc
        tid = topic_id or db.ensure_topic(deck["topic"])
        counts = db.upsert_cards(tid, deck["cards"])
        db.add_asset(tid, "anki", tmp, score=1.0, matched_by="manual")
        return {"ok": True, "topic_id": tid, "titulo": deck["topic"],
                "fichas_nuevas": counts["added"], "fichas_actualizadas": counts["updated"]}

    if ext == ".pdf":
        if not topic_id or not db.get_topic(topic_id):
            raise HTTPException(400, "Elige a qué tema pertenece el PDF")
        path = core.save_upload(name, data)
        db.add_asset(topic_id, "resumen", path, score=1.0, matched_by="manual")
        return {"ok": True, "topic_id": topic_id, "archivo": os.path.basename(path)}

    raise HTTPException(
        400, "Solo se pueden cargar PDF y archivos de fichas (.apkg). "
             "Las presentaciones se consultan desde la carpeta de material.")


# --------------------------------------------------------------------------- #
# Archivos y páginas del resumen
# --------------------------------------------------------------------------- #
_INLINE_TYPES = {"pdf": "application/pdf"}


@app.get("/api/assets/{asset_id}/file")
def asset_file(asset_id: int) -> FileResponse:
    asset = db.get_asset(asset_id)
    if not asset:
        raise HTTPException(404, "Material no encontrado")
    if not os.path.exists(asset["path"]):
        raise HTTPException(410, f"El archivo ya no está en {asset['path']}")
    media = _INLINE_TYPES.get(asset["ext"], "application/octet-stream")
    disposition = "inline" if asset["ext"] in _INLINE_TYPES else "attachment"
    return FileResponse(
        asset["path"], media_type=media, filename=asset["filename"],
        headers={"Content-Disposition": f'{disposition}; filename="{asset["filename"]}"'},
    )


@app.get("/api/assets/{asset_id}/page/{page}")
def asset_page(asset_id: int, page: int) -> FileResponse:
    """Una página del resumen, ya renderizada a PNG. Sobre ella van los post-it."""
    asset = db.get_asset(asset_id)
    if not asset:
        raise HTTPException(404, "Material no encontrado")
    if asset["ext"] != "pdf":
        raise HTTPException(400, "Solo se pueden renderizar páginas de un PDF")
    png = core.PdfRenderer.render(asset["path"], page)
    if not png:
        raise HTTPException(
            503, "No se pudo renderizar la página. Falta poppler: brew install poppler")
    # Inmutable: la clave de caché incluye la fecha del PDF, así que si el
    # archivo cambia la URL apunta a otro PNG y el navegador no reusa el viejo.
    return FileResponse(png, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.post("/api/assets/{asset_id}/open")
def asset_open(asset_id: int) -> dict:
    """Abre el material con la app nativa (Keynote/PowerPoint para presentaciones)."""
    asset = db.get_asset(asset_id)
    if not asset:
        raise HTTPException(404, "Material no encontrado")
    if not core.open_in_system_app(asset["path"]):
        raise HTTPException(500, "No se pudo abrir el archivo")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Notas
# --------------------------------------------------------------------------- #
@app.get("/api/topics/{topic_id}/notes")
def list_notes(topic_id: int, kind: str | None = None) -> list[dict]:
    return db.list_notes(topic_id, kind)


@app.post("/api/topics/{topic_id}/notes")
def create_note(topic_id: int, payload: dict = Body(...)) -> dict:
    if not db.get_topic(topic_id):
        raise HTTPException(404, "Tema no encontrado")
    kind = payload.get("kind") or "sticky"
    if kind not in ("sticky", "general"):
        raise HTTPException(400, "La nota debe ser 'sticky' o 'general'")
    nid = db.add_note(
        topic_id, kind=kind, asset_id=payload.get("asset_id"),
        page=int(payload.get("page") or 1),
        x=float(payload.get("x") or 0.5), y=float(payload.get("y") or 0.5),
        text=payload.get("text") or "", color=payload.get("color") or "amarillo")
    return {"id": nid, "notes": db.list_notes(topic_id)}


@app.post("/api/notes/{note_id}")
def update_note(note_id: int, payload: dict = Body(...)) -> dict:
    if not db.update_note(note_id, **payload):
        raise HTTPException(404, "Nota no encontrada o sin cambios válidos")
    return {"ok": True}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int) -> dict:
    if not db.delete_note(note_id):
        raise HTTPException(404, "Nota no encontrada")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Repaso
# --------------------------------------------------------------------------- #
@app.get("/api/queue")
def get_queue(topic_id: int | None = None, folder_id: int | None = None,
              limit: int = 60, mode: str = "normal") -> dict:
    """
    modo 'normal' — lo que toca hoy, respetando los topes diarios.
    modo 'libre'  — repaso extra: cualquier ficha, barajada, sin tocar la
                    programación. Para volver a pasar algo el mismo día.
    """
    if mode not in ("normal", "libre"):
        raise HTTPException(400, "El modo debe ser 'normal' o 'libre'")
    cards = db.build_queue(topic_id, limit=max(1, min(limit, 200)),
                           folder_id=folder_id, mode=mode)
    retention = db.retention()
    for card in cards:
        card["intervals"] = core.preview_intervals(card, retention)
    return {"cards": cards, "stats": db.stats(), "mode": mode,
            "scope": db.scope_counts(topic_id, folder_id)}


@app.post("/api/cards/{card_id}/grade")
def grade_card(card_id: int, payload: dict = Body(...)) -> dict:
    rating = int(payload.get("rating") or 0)
    if rating not in (1, 2, 3, 4):
        raise HTTPException(400, "La calificación debe ser 1, 2, 3 o 4")

    # En repaso libre no se guarda nada: ni el estado de la ficha ni el
    # registro del repaso. Si se guardara, una pasada extra consumiría el cupo
    # diario y adelantaría fechas que ya estaban bien calculadas.
    if payload.get("mode") == "libre":
        card = db.get_card(card_id)
        if not card:
            raise HTTPException(404, "Ficha no encontrada")
        return {"ok": True, "result": None, "stats": db.stats(), "mode": "libre"}

    result = db.grade(card_id, rating)
    if not result:
        raise HTTPException(404, "Ficha no encontrada")
    return {"ok": True, "result": result, "stats": db.stats()}


@app.post("/api/topics/{topic_id}/export")
def export_topic(topic_id: int) -> dict:
    """Genera un .apkg para llevarse las fichas a otra app si hace falta."""
    topic = db.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Tema no encontrado")
    cards = [c for c in db.topic_cards(topic_id) if c["kind"] == "basic"]
    if not cards:
        raise HTTPException(400, "El tema no tiene fichas básicas que exportar")
    out = os.path.join(core.EXPORT_DIR, f"{core.slugify(topic['title'])}.apkg")
    try:
        core.AnkiExporter.export(topic["title"], cards, out)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True, "path": out, "cards": len(cards)}


# --------------------------------------------------------------------------- #
# Ajustes
# --------------------------------------------------------------------------- #
def _settings_payload() -> dict:
    return {
        "content_dir": db.get_setting("content_dir", core.CONTENT_DIR),
        "new_limit": db.get_int_setting("new_limit", core.DEFAULT_NEW_LIMIT),
        "review_limit": db.get_int_setting("review_limit", core.DEFAULT_REVIEW_LIMIT),
        "retention": db.retention(),
        "theme": db.get_setting("theme", "auto"),
    }


@app.get("/api/settings")
def get_settings() -> dict:
    return _settings_payload()


@app.post("/api/settings")
def set_settings(payload: dict = Body(...)) -> dict:
    if "new_limit" in payload:
        db.set_setting("new_limit", max(0, int(payload["new_limit"])))
    if "review_limit" in payload:
        db.set_setting("review_limit", max(0, int(payload["review_limit"])))
    if "retention" in payload:
        # Fuera de 0.70–0.97 el algoritmo se vuelve inútil: por debajo se olvida
        # todo, por encima los intervalos se hacen absurdamente cortos.
        db.set_setting("retention", round(core._clamp(float(payload["retention"]), 0.70, 0.97), 2))
    if "theme" in payload and payload["theme"] in ("auto", "claro", "oscuro"):
        db.set_setting("theme", payload["theme"])
    return _settings_payload()


def main() -> None:
    os.makedirs(STATIC_DIR, exist_ok=True)
    if not db.list_topics():
        print("Primera vez: escaneando el material…", flush=True)
        report = core.ingest(db)
        print(f"  {report['temas']} temas · {report['tarjetas_nuevas']} fichas "
              f"· {report['materiales']} materiales · {report['huerfanos']} sin emparejar",
              flush=True)
        for err in report["errores"]:
            print(f"  [aviso] {err}", flush=True)
    if not core.PdfRenderer.available():
        print("  [aviso] Falta poppler: los resúmenes no se podrán anotar.\n"
              "          Instálalo con: brew install poppler", flush=True)
    print("\n  App de Estudio →  http://localhost:8090\n", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8090, log_level="warning")


if __name__ == "__main__":
    main()
