"""Compresión de comprobantes (PDF / foto) antes de subirlos a la nube.

Regla del proyecto: solo se guarda la versión comprimida, que se consulta y se
descarga como un PDF normal. Por eso TODO se normaliza a PDF:

* Foto o imagen (JPG/PNG)  -> se reduce a ~1800 px del lado largo, se recodifica
  a JPEG (calidad 80) y se envuelve en un PDF de una página.
* PDF                       -> se re-muestrean sus imágenes internas a ~150 dpi
  (solo si superan 200 dpi) y se optimiza su estructura. PDFs digitales (los que
  manda el proveedor) casi no cambian. Si el resultado no queda más pequeño que el
  original, se conserva el original (nunca se empeora).

Sin Streamlit ni Supabase: se puede probar con pytest.
"""
from __future__ import annotations

import hashlib
import io
from typing import Any

import pymupdf as fitz  # PyMuPDF
from PIL import Image, ImageOps

MAX_BYTES_ENTRADA = 10 * 1024 * 1024
MAX_LADO_IMAGEN = 1800
CALIDAD_JPEG = 80
DPI_UMBRAL = 200
DPI_OBJETIVO = 150
CALIDAD_IMG_PDF = 75
MAX_PAGINAS = 40


class AdjuntoError(Exception):
    """El archivo no se puede aceptar (tipo, tamaño, dañado o protegido)."""


def _tipo(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    raise AdjuntoError("Formato no admitido. Sube un PDF, JPG o PNG.")


def _imagen_a_pdf(data: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except Exception as e:  # imagen dañada
        raise AdjuntoError(f"No se pudo leer la imagen: {e}") from e
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        fondo = Image.new("RGB", img.size, (255, 255, 255))
        fondo.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = fondo
    else:
        img = img.convert("RGB")
    img.thumbnail((MAX_LADO_IMAGEN, MAX_LADO_IMAGEN), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=CALIDAD_JPEG, optimize=True)
    ancho, alto = img.size
    # Página con la misma proporción de la imagen, 595 pt de ancho (A4 vertical/horizontal).
    escala = 595.0 / max(ancho, 1) if ancho >= alto else 842.0 / max(alto, 1)
    w, h = ancho * escala, alto * escala
    doc = fitz.open()
    pagina = doc.new_page(width=w, height=h)
    pagina.insert_image(fitz.Rect(0, 0, w, h), stream=buf.getvalue())
    out = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return out


def _optimizar_pdf(data: bytes) -> bytes:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise AdjuntoError(f"No se pudo abrir el PDF: {e}") from e
    try:
        if doc.needs_pass:
            raise AdjuntoError("El PDF está protegido con contraseña.")
        if doc.page_count > MAX_PAGINAS:
            raise AdjuntoError(f"El PDF tiene {doc.page_count} páginas (máximo {MAX_PAGINAS}).")
        try:
            doc.rewrite_images(
                dpi_threshold=DPI_UMBRAL, dpi_target=DPI_OBJETIVO, quality=CALIDAD_IMG_PDF,
            )
        except Exception:
            pass  # versiones sin rewrite_images: solo se optimiza la estructura
        return doc.tobytes(garbage=4, deflate=True, clean=True)
    finally:
        doc.close()


def comprimir_a_pdf(data: bytes, nombre: str = "") -> tuple[bytes, dict[str, Any]]:
    """Regresa (pdf_comprimido, info). `info` trae bytes_original, bytes_final, sha256
    del archivo final y el nombre original."""
    if not data:
        raise AdjuntoError("El archivo está vacío.")
    if len(data) > MAX_BYTES_ENTRADA:
        raise AdjuntoError(f"El archivo pesa más de {MAX_BYTES_ENTRADA // (1024 * 1024)} MB.")
    tipo = _tipo(data)
    if tipo == "pdf":
        optimizado = _optimizar_pdf(data)
        final = optimizado if len(optimizado) < len(data) else data
    else:
        final = _imagen_a_pdf(data)
    return final, {
        "nombre_original": nombre,
        "bytes_original": len(data),
        "bytes_final": len(final),
        "sha256": hashlib.sha256(final).hexdigest(),
    }
