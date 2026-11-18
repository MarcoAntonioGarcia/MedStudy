# Imagen de MedStudy para Render (plan gratuito, runtime: docker).
#
# Por qué Docker y no el runtime de Python de Render: la app necesita poppler
# (pdftoppm, pdfinfo) para pintar los resúmenes como imagen y pegarles post-it,
# y en el runtime nativo no se puede instalar con apt. Aquí sí.
#
# Qué va dentro: el código, las páginas y el MATERIAL (contenido/). El material
# viaja en la imagen: publicar un resumen nuevo es hacer commit en contenido/ y
# desplegar; al arrancar, la app lo incorpora sola (ingesta idempotente).
# Lo que NO va: la base (vive en Neon), las cachés y las pruebas (.dockerignore).
# Etiqueta fija (versión de Python y de Debian): la imagen no cambia sola
# entre despliegues. Para actualizar, se cambia aquí y se prueba.
FROM python:3.11.18-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# poppler-utils: pdftoppm y pdfinfo. tzdata: las zonas horarias del sistema,
# que usa zoneinfo para APP_TZ (sin ellas la app no arranca, a propósito).
# --no-install-recommends y borrar las listas de apt dejan la imagen sin lo que
# no hace falta para correr.
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tzdata \
    && rm -rf /var/lib/apt/lists/*

# Un usuario sin privilegios. El código queda de root (de solo lectura para la
# app): si alguien lograra ejecutar algo dentro, no podría reescribir la app.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin medstudy

WORKDIR /app

# Las dependencias primero: esta capa se reaprovecha mientras no cambien.
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY core.py webapp.py correo.py pg_schema.py crear_admin.py SCHEMA.sql ./
COPY static ./static
COPY contenido ./contenido

# Lo único en que escribe la app: la caché de páginas renderizadas (se pierde
# al reiniciar y se rehace sola) y la biblioteca de cargas (apagada en
# producción, pero la carpeta existe para que nada falle).
RUN mkdir -p assets/paginas biblioteca exports \
    && chown -R medstudy:medstudy assets biblioteca exports

USER medstudy

# Render pasa el puerto en $PORT (10000 por defecto en Docker).
EXPOSE 10000

# --no-proxy-headers: la IP del cliente la decide la app (IP_DE_CABECERA),
# nunca X-Forwarded-For, que cualquiera puede inventar.
CMD ["sh", "-c", "exec uvicorn webapp:app --host 0.0.0.0 --port ${PORT:-10000} --no-proxy-headers"]
