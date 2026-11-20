"""Datos extra del XML de un CFDI, independientes de `cfdi.py`: RFC del receptor, forma de pago, método de pago y combustible.

Lee CFDI 3.3 y 4.0 (y la forma de pago de un complemento de pagos 2.0). Lógica pura, sin Streamlit.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

RFC_RECEPTOR_ESPERADO = "BDS2302216Q2"
MSG_RFC_INCORRECTO = "RFC DEL RECEPTOR INCORRECTO, CONTACTE AL PROVEEDOR PARA CAMBIAR LA FACTURA"

# Catálogo SAT c_FormaPago
FORMAS_PAGO = {
    "01": "Efectivo", "02": "Cheque nominativo", "03": "Transferencia electrónica", "04": "Tarjeta de crédito",
    "05": "Monedero electrónico", "06": "Dinero electrónico", "08": "Vales de despensa", "12": "Dación en pago",
    "13": "Subrogación", "14": "Pago por consignación", "15": "Condonación", "17": "Compensación", "23": "Novación",
    "24": "Confusión", "25": "Remisión de deuda", "26": "Prescripción o caducidad", "27": "A satisfacción del acreedor",
    "28": "Tarjeta de débito", "29": "Tarjeta de servicios", "30": "Aplicación de anticipos",
    "31": "Intermediario pagos", "99": "Por definir",
}
EFECTIVO = "01"
# Clave de producto o servicio (SAT) de los combustibles que se vigilan. Agregar aquí otras claves si hace falta.
COMBUSTIBLES = {"15101514": "Magna / Regular", "15101515": "Premium", "15101505": "Diésel"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def leer_extra(contenido: bytes) -> dict:
    """{'rfc_receptor', 'forma_pago' (código '01'…), 'metodo_pago', 'claves_prod' (texto), 'combustible' (nombre o None)}.
    Lanza ValueError si el XML no se puede leer."""
    try:
        raiz = ET.fromstring(contenido)
    except ET.ParseError as e:
        raise ValueError(f"XML inválido: {e}") from e
    rfc, forma, metodo, claves = None, None, None, []
    if _local(raiz.tag) == "Comprobante":
        forma = (raiz.get("FormaPago") or "").strip() or None
        metodo = (raiz.get("MetodoPago") or "").strip() or None
    for el in raiz.iter():
        t = _local(el.tag)
        if t == "Receptor" and rfc is None:
            rfc = (el.get("Rfc") or "").strip().upper() or None
        elif t == "Concepto":
            c = (el.get("ClaveProdServ") or "").strip()
            if c and c not in claves:
                claves.append(c)
        elif t == "Pago" and forma is None:        # complemento de pagos: forma de pago de cada pago
            forma = (el.get("FormaDePagoP") or "").strip() or None
    comb = next((COMBUSTIBLES[c] for c in claves if c in COMBUSTIBLES), None)
    return {"rfc_receptor": rfc, "forma_pago": forma, "metodo_pago": metodo, "claves_prod": ", ".join(claves) or None, "combustible": comb}


def validar_receptor(extra: dict, esperado: str = RFC_RECEPTOR_ESPERADO) -> str | None:
    """None si el RFC del receptor es el esperado; si no, el mensaje de error para el usuario."""
    rfc = (extra.get("rfc_receptor") or "").strip().upper()
    if rfc == esperado.strip().upper():
        return None
    return f"{MSG_RFC_INCORRECTO}. (El XML trae «{rfc or 'sin RFC'}»; debe ser {esperado}.)"


def etiqueta_forma_pago(codigo) -> str:
    c = str(codigo or "").strip()
    return f"{c} · {FORMAS_PAGO.get(c, 'Otra')}" if c else ""


def combustible_en_efectivo(f: dict) -> bool:
    """Factura de gasolina / diésel pagada en efectivo (alerta para el revisor)."""
    return bool(f.get("combustible")) and str(f.get("forma_pago") or "").strip() == EFECTIVO
