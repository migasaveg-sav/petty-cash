"""Lectura de comprobantes fiscales digitales (CFDI) 3.3 y 4.0.

Este módulo no depende de Streamlit: es lógica pura, fácil de probar por separado
de la interfaz (ver tests/test_cfdi.py).
"""
from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any

CFDI_NS_POR_VERSION = {
    "3": "http://www.sat.gob.mx/cfd/3",
    "4": "http://www.sat.gob.mx/cfd/4",
}
TFD_NS = "http://www.sat.gob.mx/TimbreFiscalDigital"

# Códigos de impuesto estándar del SAT
IMPUESTO_IVA = "002"
IMPUESTO_ISR = "001"

TIPOS_COMPROBANTE = {
    "I": "Ingreso",
    "E": "Egreso (nota de crédito)",
    "N": "Nómina",
    "P": "Pago",
    "T": "Traslado",
}


class CFDIParseError(Exception):
    """Se lanza cuando un XML no se puede leer como CFDI válido."""


def detectar_namespace_cfdi(root: ET.Element) -> str:
    """Detecta si el XML es CFDI 3.3 o 4.0 a partir del namespace del root."""
    tag = root.tag
    if tag.startswith("{"):
        uri = tag[1:tag.index("}")]
        if uri == CFDI_NS_POR_VERSION["3"]:
            return CFDI_NS_POR_VERSION["3"]
        if uri == CFDI_NS_POR_VERSION["4"]:
            return CFDI_NS_POR_VERSION["4"]
        return uri  # namespace no reconocido, se intenta igual
    return CFDI_NS_POR_VERSION["4"]  # fallback


def _parse_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_cfdi(xml_bytes: bytes, filename: str) -> dict[str, Any]:
    """Extrae los datos relevantes de un CFDI (3.3 o 4.0).

    Regresa un dict; nunca regresa None. Si el XML no es parseable en absoluto
    (no es XML válido), lanza CFDIParseError. Si es XML válido pero le faltan
    nodos esperados (timbre, emisor, etc.), regresa valores por defecto ("SIN-UUID",
    "N/A", 0.0) y agrega el motivo a la lista "Advertencias" en vez de tronar,
    para que una factura incompleta se pueda seguir viendo y corrigiendo a mano
    en vez de perderse silenciosamente.
    """
    try:
        tree = ET.parse(BytesIO(xml_bytes))
        root = tree.getroot()
    except ET.ParseError as e:
        raise CFDIParseError(f"El archivo no es un XML válido: {e}") from e

    cfdi_uri = detectar_namespace_cfdi(root)
    ns = {"cfdi": cfdi_uri, "tfd": TFD_NS}

    advertencias: list[str] = []

    tipo_comprobante = root.get("TipoDeComprobante", "I")
    es_nota_credito = tipo_comprobante == "E"

    total = _parse_float(root.get("Total"))
    if total <= 0:
        subtotal = _parse_float(root.get("SubTotal"))
        if subtotal > 0:
            total = subtotal
            advertencias.append("Sin atributo 'Total' válido; se usó 'SubTotal' como respaldo.")
        else:
            advertencias.append("El comprobante no trae un monto total mayor a cero.")

    monto_total = -total if es_nota_credito else total

    fecha_factura = root.get("Fecha", str(datetime.date.today()))[:10]

    timbre = root.find(".//tfd:TimbreFiscalDigital", ns)
    uuid = timbre.get("UUID") if timbre is not None else None
    if not uuid:
        uuid = "SIN-UUID"
        advertencias.append("No se encontró el Timbre Fiscal Digital (UUID).")

    emisor = root.find(".//cfdi:Emisor", ns)
    rfc_emisor = emisor.get("Rfc") if emisor is not None else None
    razon_social = emisor.get("Nombre") if emisor is not None else None
    if not rfc_emisor:
        rfc_emisor = "N/A"
        advertencias.append("No se encontró el RFC del emisor.")
    if not razon_social:
        razon_social = "N/A"

    valor_iva = 0.0
    iva_retenido = 0.0
    isr_retenido = 0.0
    impuestos_globales = root.find("./cfdi:Impuestos", ns)
    if impuestos_globales is not None:
        for traslado in impuestos_globales.findall(".//cfdi:Traslados/cfdi:Traslado", ns):
            if traslado.get("Impuesto") == IMPUESTO_IVA:
                valor_iva += _parse_float(traslado.get("Importe"))
        for retencion in impuestos_globales.findall(".//cfdi:Retenciones/cfdi:Retencion", ns):
            if retencion.get("Impuesto") == IMPUESTO_IVA:
                iva_retenido += _parse_float(retencion.get("Importe"))
            elif retencion.get("Impuesto") == IMPUESTO_ISR:
                isr_retenido += _parse_float(retencion.get("Importe"))
    if es_nota_credito:
        valor_iva = -valor_iva

    conceptos = root.findall(".//cfdi:Concepto", ns)
    concepto = "; ".join(c.get("Descripcion", "") for c in conceptos) if conceptos else "N/A"

    incompleta = bool(advertencias)

    return {
        "_id": None,  # se asigna al agregarlo a una lista de trabajo
        "Fuente": f"XML: {filename}",
        "Archivo": filename,
        "Fecha Factura": fecha_factura,
        "UUID": uuid,
        "Concepto": concepto,
        "RFC Emisor": rfc_emisor,
        "Razón Social": razon_social,
        "IVA": valor_iva,
        "IVA Retenido": iva_retenido,
        "ISR Retenido": isr_retenido,
        "Monto Total": monto_total,
        "Tipo Comprobante": TIPOS_COMPROBANTE.get(tipo_comprobante, tipo_comprobante),
        "Es Nota Credito": es_nota_credito,
        "Incompleta": incompleta,
        "Advertencias": advertencias,
    }
