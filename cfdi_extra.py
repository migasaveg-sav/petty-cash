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


def _num(v) -> float:
    try:
        return float(str(v or 0).replace(",", ""))
    except ValueError:
        return 0.0


def completar_datos(datos: dict, extra: dict) -> dict:
    """Agrega a lo que leyó `parse_cfdi` los datos extra. ISH y retenciones solo se completan si el lector principal los dejó en 0."""
    datos.update({k: extra[k] for k in ("rfc_receptor", "forma_pago", "metodo_pago", "claves_prod", "combustible")})
    for campo, clave in (("ISH", "ish"), ("ISR Retenido", "isr_retenido"), ("IVA Retenido", "iva_retenido")):
        if not float(datos.get(campo) or 0) and extra.get(clave):
            datos[campo] = extra[clave]
    return datos


def leer_extra(contenido: bytes) -> dict:
    """{'rfc_receptor', 'forma_pago' (código '01'…), 'metodo_pago', 'claves_prod' (texto), 'combustible' (nombre o None)}.
    También 'ish' (impuesto local de hospedaje del complemento «Impuestos locales», implocal), 'isr_retenido' e 'iva_retenido'
    (retenciones a nivel comprobante) para completar lo que no haya leído `parse_cfdi`.
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
    ish = 0.0
    for el in raiz.iter():
        if _local(el.tag) == "TrasladosLocales":     # implocal:ImpuestosLocales / TrasladosLocales ImpLocTrasladado="ISH"
            nombre = (el.get("ImpLocTrasladado") or "").strip().upper()
            if nombre == "ISH" or "HOSPEDAJE" in nombre:
                ish += _num(el.get("Importe"))
    isr = iva_ret = 0.0
    for hijo in raiz:                                 # retenciones del comprobante (no las de cada concepto: se contarían doble)
        if _local(hijo.tag) != "Impuestos":
            continue
        for ret in hijo.iter():
            if _local(ret.tag) == "Retencion":
                imp = (ret.get("Impuesto") or "").strip()
                if imp in ("001", "ISR"):
                    isr += _num(ret.get("Importe"))
                elif imp in ("002", "IVA"):
                    iva_ret += _num(ret.get("Importe"))
    comb = next((COMBUSTIBLES[c] for c in claves if c in COMBUSTIBLES), None)
    return {"rfc_receptor": rfc, "forma_pago": forma, "metodo_pago": metodo, "claves_prod": ", ".join(claves) or None, "combustible": comb,
            "ish": round(ish, 2), "isr_retenido": round(isr, 2), "iva_retenido": round(iva_ret, 2)}


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
