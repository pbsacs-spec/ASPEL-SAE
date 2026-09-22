"""
Etiquetas de embarque generadas a partir de facturas de Aspel SAE.

Solo lee de Aspel (FACTF01, CLIE01, PARAM_DATOSEMP01, PARAM_DOMFISCAL01); los datos
propios del embarque (destinatario editado, chofer/unidad o paqueteria/guia, y los
datos del Emisor) se guardan localmente: ver embarques_db.py y embarque_config.ini.
"""
import configparser
import datetime
import io
import os
import threading

import qrcode
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, g, send_file
from fpdf import FPDF
from fpdf.enums import XPos, YPos, Align

from db import query, load_empresas
from auth import require_roles
import embarques_db

embarques_bp = Blueprint("embarques", __name__)
require_embarques = require_roles("administradores", "admin", realm="Aspel Inventario")
require_admin_embarques = require_roles("admin", realm="Aspel Inventario")

_CFG_FILE = os.path.join(os.path.dirname(__file__), "embarque_config.ini")
_LOCK = threading.Lock()
_CAMPOS_EMISOR = [
    "nombre_empresa", "rfc", "calle", "numext", "numint", "colonia",
    "cp", "municipio", "estado", "pais", "telefono",
]


# ── Configuracion del Emisor (por empresa) ──────────────────────────────────

def _leer_emisor_cfg():
    cfg = configparser.ConfigParser()
    if os.path.exists(_CFG_FILE):
        cfg.read(_CFG_FILE, encoding="utf-8")
    return cfg


def _guardar_emisor_cfg(cfg):
    with _LOCK:
        tmp = _CFG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            cfg.write(f)
        os.replace(tmp, _CFG_FILE)


def get_emisor(empresa_id):
    cfg = _leer_emisor_cfg()
    if cfg.has_section(empresa_id):
        return {c: cfg.get(empresa_id, c, fallback="") for c in _CAMPOS_EMISOR}
    return {c: "" for c in _CAMPOS_EMISOR}


def set_emisor(empresa_id, datos):
    cfg = _leer_emisor_cfg()
    cfg[empresa_id] = {c: (datos.get(c) or "").strip() for c in _CAMPOS_EMISOR}
    _guardar_emisor_cfg(cfg)


def _sugerido_emisor(empresa_id):
    """Prellenado desde Aspel (RFC y domicilio fiscal) para la primera vez que se
    configura el Emisor. El nombre comercial no se puede leer (Aspel lo encripta)."""
    datos = {c: "" for c in _CAMPOS_EMISOR}
    try:
        _, rows = query("SELECT RFC FROM PARAM_DATOSEMP01", empresa_id=empresa_id)
        if rows:
            datos["rfc"] = (rows[0][0] or "").strip()
    except Exception:
        pass
    try:
        _, rows = query("""
            SELECT CALLE, NUMERO_EXT, NUMERO_INT, COLONIA, MUNICIPIO, ESTADO, PAIS, CP
            FROM PARAM_DOMFISCAL01
        """, empresa_id=empresa_id)
        if rows:
            calle, numext, numint, colonia, municipio, estado, pais, cp = rows[0]
            datos.update({
                "calle": (calle or "").strip(), "numext": (numext or "").strip(),
                "numint": (numint or "").strip(), "colonia": (colonia or "").strip(),
                "municipio": (municipio or "").strip(), "estado": (estado or "").strip(),
                "pais": (pais or "").strip(), "cp": (cp or "").strip(),
            })
    except Exception:
        pass
    return datos


def _logo_empresa(empresa_id):
    """Logo de la empresa, tal cual lo tiene Aspel SAE (PARAM_DATOSEMP01.LOGO_EMPRESA
    es una imagen sin encriptar, a diferencia de NOMBRE_EMPRESA). None si no hay logo
    o si la tabla no tiene datos."""
    try:
        _, rows = query("SELECT LOGO_EMPRESA FROM PARAM_DATOSEMP01", empresa_id=empresa_id)
        blob = rows[0][0] if rows else None
        return bytes(blob) if blob else None
    except Exception:
        return None


# ── Busqueda de facturas y destinatario sugerido ────────────────────────────

def _empresa_actual():
    empresa = request.args.get("empresa", "").strip() or None
    if empresa:
        empresas, _ = load_empresas()
        if empresa not in empresas:
            empresa = None
    if not empresa:
        _, settings = load_empresas()
        empresa = settings["default"]
    return empresa


def _fallback(envio, general):
    envio = (envio or "").strip()
    general = (general or "").strip()
    return envio or general


@embarques_bp.route("/api/facturas/buscar")
@require_embarques
def facturas_buscar():
    empresa = _empresa_actual()
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": True, "data": []})

    try:
        _, rows = query("""
            SELECT FIRST 20 f.CVE_DOC, f.SERIE, f.FOLIO, f.FECHA_DOC, c.CLAVE, c.NOMBRE
            FROM FACTF01 f
            JOIN CLIE01 c ON c.CLAVE = f.CVE_CLPV
            WHERE f.STATUS = 'E'
              AND (CAST(f.FOLIO AS VARCHAR(20)) CONTAINING ? OR UPPER(c.NOMBRE) CONTAINING UPPER(?))
            ORDER BY f.FECHA_DOC DESC
        """, [q, q], empresa_id=empresa)

        data = [{
            "cve_doc": cve_doc, "serie": (serie or "").strip(), "folio": folio,
            "fecha": fecha_doc.strftime("%Y-%m-%d") if fecha_doc else None,
            "cliente_clave": clave.strip(), "cliente_nombre": nombre or "",
        } for cve_doc, serie, folio, fecha_doc, clave, nombre in rows]
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@embarques_bp.route("/api/facturas/pendientes")
@require_embarques
def facturas_pendientes():
    """Facturas del rango de fechas que todavia NO tienen ninguna etiqueta creada."""
    empresa = _empresa_actual()

    hasta_txt = request.args.get("hasta", "").strip()
    desde_txt = request.args.get("desde", "").strip()
    hoy = datetime.date.today()
    try:
        hasta_d = datetime.datetime.strptime(hasta_txt, "%Y-%m-%d").date() if hasta_txt else hoy
        desde_d = datetime.datetime.strptime(desde_txt, "%Y-%m-%d").date() if desde_txt else hoy - datetime.timedelta(days=15)
    except ValueError:
        return jsonify({"ok": False, "error": "Fecha invalida."}), 400

    desde_dt = datetime.datetime.combine(desde_d, datetime.time.min)
    hasta_dt = datetime.datetime.combine(hasta_d + datetime.timedelta(days=1), datetime.time.min)

    try:
        _, rows = query("""
            SELECT FIRST 200 f.CVE_DOC, f.SERIE, f.FOLIO, f.FECHA_DOC, c.CLAVE, c.NOMBRE
            FROM FACTF01 f
            JOIN CLIE01 c ON c.CLAVE = f.CVE_CLPV
            WHERE f.STATUS = 'E'
              AND f.FECHA_DOC >= ? AND f.FECHA_DOC < ?
            ORDER BY f.FECHA_DOC DESC
        """, [desde_dt, hasta_dt], empresa_id=empresa)

        ya_etiquetadas = embarques_db.cve_docs_con_etiqueta(empresa)
        data = [{
            "cve_doc": cve_doc, "serie": (serie or "").strip(), "folio": folio,
            "fecha": fecha_doc.strftime("%Y-%m-%d") if fecha_doc else None,
            "cliente_clave": clave.strip(), "cliente_nombre": nombre or "",
        } for cve_doc, serie, folio, fecha_doc, clave, nombre in rows if cve_doc not in ya_etiquetadas]
        return jsonify({"ok": True, "data": data, "desde": desde_d.isoformat(), "hasta": hasta_d.isoformat()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@embarques_bp.route("/api/facturas/<cve_doc>/sugerido")
@require_embarques
def factura_sugerido(cve_doc):
    empresa = _empresa_actual()
    try:
        _, rows = query("""
            SELECT f.SERIE, f.FOLIO, f.FECHA_DOC, c.CLAVE, c.NOMBRE, c.TELEFONO,
                   c.CALLE, c.NUMEXT, c.NUMINT, c.COLONIA, c.CODIGO, c.MUNICIPIO, c.ESTADO, c.PAIS,
                   c.CALLE_ENVIO, c.NUMEXT_ENVIO, c.NUMINT_ENVIO, c.COLONIA_ENVIO, c.CODIGO_ENVIO,
                   c.MUNICIPIO_ENVIO, c.ESTADO_ENVIO, c.PAIS_ENVIO,
                   c.REFERDIR, c.REFERENCIA_ENVIO
            FROM FACTF01 f
            JOIN CLIE01 c ON c.CLAVE = f.CVE_CLPV
            WHERE f.CVE_DOC = ? AND f.STATUS = 'E'
        """, [cve_doc], empresa_id=empresa)
        if not rows:
            return jsonify({"ok": False, "error": "Factura no encontrada."}), 404

        (serie, folio, fecha_doc, clave, nombre, telefono,
         calle, numext, numint, colonia, cp, municipio, estado, pais,
         calle_e, numext_e, numint_e, colonia_e, cp_e, municipio_e, estado_e, pais_e,
         referdir, referencia_e) = rows[0]

        data = {
            "factura": {
                "cve_doc": cve_doc, "serie": (serie or "").strip(), "folio": folio,
                "fecha": fecha_doc.strftime("%Y-%m-%d") if fecha_doc else None,
                "cliente_clave": clave.strip(), "cliente_nombre": nombre or "",
            },
            "destinatario": {
                "nombre": nombre or "",
                "calle": _fallback(calle_e, calle),
                "numext": _fallback(numext_e, numext),
                "numint": _fallback(numint_e, numint),
                "colonia": _fallback(colonia_e, colonia),
                "cp": _fallback(cp_e, cp),
                "municipio": _fallback(municipio_e, municipio),
                "estado": _fallback(estado_e, estado),
                "pais": _fallback(pais_e, pais),
                "telefono": (telefono or "").strip(),
                "referencia": _fallback(referencia_e, referdir),
            },
        }
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Paginas ──────────────────────────────────────────────────────────────

@embarques_bp.route("/embarques")
@require_embarques
def pagina():
    return render_template("embarques.html", es_admin=(g.role == "admin"))


@embarques_bp.route("/embarques/nueva")
@require_embarques
def pagina_nueva():
    return render_template("embarques_nueva.html", es_admin=(g.role == "admin"))


@embarques_bp.route("/embarques/configuracion", methods=["GET", "POST"])
@require_embarques
def pagina_configuracion():
    empresas, settings = load_empresas()
    empresa_id = request.values.get("empresa", "").strip() or settings["default"]
    if empresa_id not in empresas:
        empresa_id = settings["default"]

    if request.method == "POST":
        set_emisor(empresa_id, request.form)
        flash("Datos del emisor guardados.", "ok")
        return redirect(url_for("embarques.pagina_configuracion", empresa=empresa_id))

    datos = get_emisor(empresa_id)
    if not any(datos.values()):
        datos = _sugerido_emisor(empresa_id)

    return render_template(
        "embarques_config.html", es_admin=(g.role == "admin"),
        empresas=empresas, empresa_id=empresa_id, datos=datos,
    )


# ── Confirmacion de entrega (publica, sin login -- el chofer no tiene cuenta) ─

@embarques_bp.route("/entrega/<token>")
def entrega_pagina(token):
    e = embarques_db.obtener_por_token(token)
    if not e:
        return render_template("entrega_confirmar.html", encontrada=False), 404
    folio_txt = f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}".strip() or e["factura_cve_doc"]
    return render_template(
        "entrega_confirmar.html", encontrada=True, e=e, folio_txt=folio_txt,
        ya_entregada=(e["estatus"] == "entregado"),
    )


@embarques_bp.route("/entrega/<token>/confirmar", methods=["POST"])
def entrega_confirmar(token):
    e = embarques_db.obtener_por_token(token)
    if not e:
        return jsonify({"ok": False, "error": "Liga invalida."}), 404
    ok = embarques_db.marcar_entregado(e["id"], "qr", request.remote_addr)
    return jsonify({"ok": True, "nuevo": ok})


# ── API de embarques ─────────────────────────────────────────────────────

@embarques_bp.route("/api/embarques")
@require_embarques
def api_listar():
    empresa = _empresa_actual()
    estatus = request.args.get("estatus", "").strip() or None
    desde = request.args.get("desde", "").strip() or None
    hasta = request.args.get("hasta", "").strip() or None
    try:
        data = embarques_db.listar_embarques(empresa, estatus=estatus, desde=desde, hasta=hasta)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _leer_datos_embarque(body, requerido):
    """Valida tipo_embarque/chofer/unidad/paqueteria/guia/num_bultos de un body JSON.
    Si requerido es False y no viene tipo_embarque, regresa (None, None): al crear
    una etiqueta estos datos son opcionales (se pueden completar despues)."""
    tipo = body.get("tipo_embarque")
    if not tipo:
        if requerido:
            return None, "Tipo de embarque invalido."
        return None, None
    if tipo not in ("propio", "paqueteria"):
        return None, "Tipo de embarque invalido."
    if tipo == "propio" and not (body.get("chofer") or "").strip():
        return None, "Indica el chofer."
    if tipo == "paqueteria" and not (body.get("paqueteria") or "").strip():
        return None, "Indica la paqueteria."
    try:
        num_bultos = int(body.get("num_bultos") or 1)
        if not (1 <= num_bultos <= 200):
            raise ValueError
    except (TypeError, ValueError):
        return None, "Numero de bultos invalido (1-200)."

    return {
        "tipo_embarque": tipo,
        "chofer": (body.get("chofer") or "").strip(),
        "unidad": (body.get("unidad") or "").strip(),
        "paqueteria": (body.get("paqueteria") or "").strip(),
        "guia": (body.get("guia") or "").strip(),
        "num_bultos": num_bultos,
    }, None


@embarques_bp.route("/api/catalogo/<tipo>")
@require_embarques
def api_catalogo(tipo):
    if tipo not in ("chofer", "unidad", "paqueteria"):
        return jsonify({"ok": False, "error": "Catalogo invalido."}), 400
    return jsonify({"ok": True, "data": embarques_db.catalogo_listar(tipo)})


@embarques_bp.route("/api/embarques", methods=["POST"])
@require_embarques
def api_crear():
    empresa = _empresa_actual()
    body = request.get_json(silent=True) or {}
    factura = body.get("factura") or {}
    destinatario = body.get("destinatario") or {}

    if not factura.get("cve_doc"):
        return jsonify({"ok": False, "error": "Falta seleccionar una factura."}), 400
    if not (destinatario.get("nombre") or "").strip():
        return jsonify({"ok": False, "error": "El nombre del destinatario es obligatorio."}), 400

    embarque_info, error = _leer_datos_embarque(body.get("embarque") or {}, requerido=False)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    try:
        existentes = embarques_db.buscar_por_factura(empresa, factura["cve_doc"])
        embarque_id = embarques_db.crear_embarque(empresa, factura, destinatario, g.username, embarque_info)
        return jsonify({"ok": True, "id": embarque_id, "duplicado": bool(existentes)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@embarques_bp.route("/api/embarques/<int:embarque_id>/embarcar", methods=["POST"])
@require_embarques
def api_embarcar(embarque_id):
    empresa = _empresa_actual()
    body = request.get_json(silent=True) or {}

    embarque_info, error = _leer_datos_embarque(body, requerido=True)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    embarque = embarques_db.obtener_embarque(embarque_id, empresa_id=empresa)
    if not embarque:
        return jsonify({"ok": False, "error": "Etiqueta no encontrada."}), 404
    if embarque["estatus"] == "entregado":
        return jsonify({"ok": False, "error": "Esta etiqueta ya fue entregada. Un admin debe reactivarla primero."}), 409

    try:
        embarques_db.marcar_embarcado(embarque_id, embarque_info["tipo_embarque"], embarque_info, g.username)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@embarques_bp.route("/api/embarques/<int:embarque_id>/reactivar", methods=["POST"])
@require_admin_embarques
def api_reactivar(embarque_id):
    empresa = _empresa_actual()
    body = request.get_json(silent=True) or {}
    motivo = (body.get("motivo") or "").strip()
    if not motivo:
        return jsonify({"ok": False, "error": "Indica el motivo de la reactivacion."}), 400

    embarque = embarques_db.obtener_embarque(embarque_id, empresa_id=empresa)
    if not embarque:
        return jsonify({"ok": False, "error": "Etiqueta no encontrada."}), 404

    try:
        ok = embarques_db.reactivar(embarque_id, g.username, motivo)
        if not ok:
            return jsonify({"ok": False, "error": "Esta etiqueta no esta entregada, no hay nada que reactivar."}), 409
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── PDF de la etiqueta (media carta, hasta 2 por hoja, 1 etiqueta por bulto) ─

_PAD = 3       # mm, margen interno de cada etiqueta
_ROW_H = 5     # mm, alto de cada fila de producto
_CANT_W = 18   # mm, ancho de la columna Cantidad


def _partidas_factura(empresa_id, cve_doc):
    _, rows = query("""
        SELECT p.CANT, COALESCE(i.DESCR, p.DESCR_ART) AS DESCR
        FROM PAR_FACTF01 p
        LEFT JOIN INVE01 i ON i.CVE_ART = p.CVE_ART
        WHERE p.CVE_DOC = ?
        ORDER BY p.NUM_PAR
    """, [cve_doc], empresa_id=empresa_id)
    return [{"cantidad": float(c or 0), "descripcion": (d or "").strip()} for c, d in rows]


def _lineas_compactas(nombre, calle, numext, numint, colonia, cp, municipio, estado, extra=""):
    lineas = [nombre or "(sin nombre)"]
    calle_txt = calle or ""
    if numext:
        calle_txt += f" {numext}"
    if numint:
        calle_txt += f" Int.{numint}"
    if calle_txt.strip():
        lineas.append(calle_txt.strip())
    l2 = ", ".join(x for x in [colonia, f"CP {cp}" if cp else ""] if x)
    if l2:
        lineas.append(l2)
    l3 = ", ".join(x for x in [municipio, estado] if x)
    if l3:
        lineas.append(l3)
    if extra:
        lineas.append(extra)
    return lineas[:5]


def _pdf_safe(texto):
    """Los datos de Aspel (nombres, direcciones, descripciones de producto) pueden
    traer caracteres fuera de Latin-1 (comillas curvas, guiones largos, etc.) que
    la fuente Helvetica basica de fpdf2 no soporta y hacen fallar la generacion
    del PDF. Se reemplazan por '?' en vez de tronar la etiqueta completa."""
    return (texto or "").encode("latin-1", "replace").decode("latin-1")


def _truncar(pdf, texto, ancho_mm):
    texto = _pdf_safe(texto).strip()
    if pdf.get_string_width(texto) <= ancho_mm:
        return texto
    while texto and pdf.get_string_width(texto + "...") > ancho_mm:
        texto = texto[:-1]
    return (texto + "...") if texto else ""


def _dibujar_encabezado(pdf, slot_x, slot_y, slot_w, completo, bulto, num_bultos,
                         folio_txt, fecha, emisor, destinatario, logo):
    """Dibuja el encabezado de una etiqueta (completo, con Emisor/Destinatario, o
    resumido para una hoja de continuacion). Regresa el y donde debe empezar la
    tabla de productos."""
    cx, y = slot_x + _PAD, slot_y + _PAD
    cw = slot_w - 2 * _PAD

    if not completo:
        pdf.set_xy(cx, y)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(cw, 5, f"ETIQUETA DE EMBARQUE (continuacion) - Bulto {bulto} de {num_bultos} - Factura {folio_txt}",
                 new_x=XPos.LEFT, new_y=YPos.TOP)
        y += 7
        pdf.dashed_line(cx, y, cx + cw, y, 1, 1)
        y += 2
        return _dibujar_fila_productos_header(pdf, cx, y, cw)

    logo_w = 0
    if logo:
        try:
            pdf.image(logo, x=cx, y=y, w=16)
            logo_w = 19
        except Exception:
            pass
    pdf.set_xy(cx + logo_w, y)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(cw - logo_w, 4.5, "ETIQUETA DE EMBARQUE", new_x=XPos.LEFT, new_y=YPos.TOP)
    pdf.set_xy(cx + logo_w, y + 4.5)
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(cw - logo_w, 4, f"Bulto {bulto} de {num_bultos}  ·  Factura {folio_txt}  ·  {fecha or ''}",
              new_x=XPos.LEFT, new_y=YPos.TOP)
    y += 10
    pdf.dashed_line(cx, y, cx + cw, y, 1, 1)
    y += 2

    col_w = (cw - 4) / 2
    col2_x = cx + col_w + 4

    pdf.set_xy(cx, y)
    pdf.set_font("Helvetica", "B", 7)
    pdf.cell(col_w, 3.5, "EMISOR (remite)", new_x=XPos.LEFT, new_y=YPos.TOP)
    pdf.set_xy(col2_x, y)
    pdf.cell(col_w, 3.5, "DESTINATARIO (recibe)", new_x=XPos.LEFT, new_y=YPos.TOP)
    y_txt = y + 3.5

    extra_emisor = " · ".join(x for x in [
        f"Tel: {emisor['telefono']}" if emisor["telefono"] else "",
        f"RFC: {emisor['rfc']}" if emisor["rfc"] else "",
    ] if x)
    lineas_emisor = _lineas_compactas(
        emisor["nombre_empresa"] or "(configura el emisor en /embarques/configuracion)",
        emisor["calle"], emisor["numext"], emisor["numint"], emisor["colonia"], emisor["cp"],
        emisor["municipio"], emisor["estado"], extra_emisor,
    )
    lineas_dest = _lineas_compactas(
        destinatario["dest_nombre"], destinatario["dest_calle"], destinatario["dest_numext"],
        destinatario["dest_numint"], destinatario["dest_colonia"], destinatario["dest_cp"],
        destinatario["dest_municipio"], destinatario["dest_estado"],
        f"Tel: {destinatario['dest_telefono']}" if destinatario["dest_telefono"] else "",
    )
    for i, linea in enumerate(lineas_emisor):
        pdf.set_xy(cx, y_txt + i * 3.6)
        pdf.set_font("Helvetica", "B" if i == 0 else "", 7.5 if i == 0 else 7)
        pdf.cell(col_w, 3.6, _truncar(pdf, linea, col_w), new_x=XPos.LEFT, new_y=YPos.TOP)
    for i, linea in enumerate(lineas_dest):
        pdf.set_xy(col2_x, y_txt + i * 3.6)
        pdf.set_font("Helvetica", "B" if i == 0 else "", 7.5 if i == 0 else 7)
        pdf.cell(col_w, 3.6, _truncar(pdf, linea, col_w), new_x=XPos.LEFT, new_y=YPos.TOP)

    y = y_txt + 5 * 3.6 + 2
    pdf.dashed_line(cx, y, cx + cw, y, 1, 1)
    y += 2
    return _dibujar_fila_productos_header(pdf, cx, y, cw)


def _dibujar_fila_productos_header(pdf, cx, y, cw):
    pdf.set_xy(cx, y)
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(_CANT_W, _ROW_H, "Cant.", new_x=XPos.LEFT, new_y=YPos.TOP)
    pdf.set_xy(cx + _CANT_W, y)
    pdf.cell(cw - _CANT_W, _ROW_H, "Descripcion", new_x=XPos.LEFT, new_y=YPos.TOP)
    y += _ROW_H
    pdf.line(cx, y, cx + cw, y)
    return y + 1


def _dibujar_fila_producto(pdf, cx, y, cw, producto):
    pdf.set_xy(cx, y)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.cell(_CANT_W, _ROW_H, f"{producto['cantidad']:g}", new_x=XPos.LEFT, new_y=YPos.TOP)
    pdf.set_xy(cx + _CANT_W, y)
    pdf.cell(cw - _CANT_W, _ROW_H, _truncar(pdf, producto["descripcion"], cw - _CANT_W - 1),
              new_x=XPos.LEFT, new_y=YPos.TOP)


def _dibujar_total(pdf, cx, y, cw, total):
    pdf.line(cx, y, cx + cw, y)
    y += 1
    pdf.set_xy(cx, y)
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(_CANT_W, _ROW_H, f"{total:g}", new_x=XPos.LEFT, new_y=YPos.TOP)
    pdf.set_xy(cx + _CANT_W, y)
    pdf.cell(cw - _CANT_W, _ROW_H, "TOTAL", new_x=XPos.LEFT, new_y=YPos.TOP)


_FOOTER_H = 24  # mm, franja al pie de cada etiqueta: dato de embarque + QR de entrega
_QR_SIZE = 18   # mm


def _qr_png(data):
    img = qrcode.make(data, box_size=6, border=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _dibujar_pie(pdf, slot_x, slot_y, slot_w, slot_h, e, qr_png):
    """Pie de pagina: a la derecha el QR para que el chofer confirme la entrega desde
    su celular; a la izquierda, si ya esta embarcada, el chofer/unidad o paqueteria/guia."""
    cx = slot_x + _PAD
    cw = slot_w - 2 * _PAD
    y_top = slot_y + slot_h - _PAD - _FOOTER_H + 1
    pdf.line(cx, y_top, cx + cw, y_top)

    if qr_png:
        qr_x = cx + cw - _QR_SIZE
        qr_y = y_top + 1.5
        try:
            pdf.image(qr_png, x=qr_x, y=qr_y, w=_QR_SIZE, h=_QR_SIZE)
            pdf.set_xy(qr_x, qr_y + _QR_SIZE + 0.5)
            pdf.set_font("Helvetica", "", 6)
            pdf.cell(_QR_SIZE, 3, "Confirmar entrega", align="C", new_x=XPos.LEFT, new_y=YPos.TOP)
        except Exception:
            pass

    texto_w = cw - _QR_SIZE - 3
    if e["estatus"] == "embarcado":
        if e["tipo_embarque"] == "propio":
            lineas = ["Envio propio", f"Chofer: {e['chofer'] or ''}", f"Unidad: {e['unidad'] or ''}"]
        else:
            lineas = [f"Paqueteria: {e['paqueteria'] or ''}", f"Guia: {e['guia'] or ''}"]
        fecha_emb = (e["fecha_embarque"] or "")[:10]
        if fecha_emb:
            lineas.append(f"Embarcado: {fecha_emb}")
        pdf.set_font("Helvetica", "", 7.5)
        yy = y_top + 1.5
        for linea in lineas:
            pdf.set_xy(cx, yy)
            pdf.cell(texto_w, 3.8, _truncar(pdf, linea, texto_w), new_x=XPos.LEFT, new_y=YPos.TOP)
            yy += 3.8


@embarques_bp.route("/embarques/<int:embarque_id>/etiqueta.pdf")
@require_embarques
def etiqueta_pdf(embarque_id):
    empresa = _empresa_actual()
    e = embarques_db.obtener_embarque(embarque_id, empresa_id=empresa)
    if not e:
        return jsonify({"ok": False, "error": "Etiqueta no encontrada."}), 404
    if e["estatus"] == "entregado":
        return jsonify({
            "ok": False,
            "error": "Esta etiqueta ya fue entregada y esta bloqueada para reimprimir. "
                     "Si se perdio la etiqueta impresa, pide a un administrador que la reactive.",
        }), 409

    emisor = get_emisor(empresa)
    logo = _logo_empresa(empresa)
    folio_txt = _pdf_safe(f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}".strip() or e["factura_cve_doc"])
    fecha = e["fecha_creacion"][:10]
    num_bultos = max(1, min(200, int(e.get("num_bultos") or 1)))
    url_entrega = request.url_root.rstrip("/") + "/entrega/" + e["token_entrega"]
    qr_png = _qr_png(url_entrega)

    try:
        productos = _partidas_factura(empresa, e["factura_cve_doc"])
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    total_cant = sum(p["cantidad"] for p in productos)

    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(False)

    margen, gap = 10, 6
    slot_w = pdf.w - 2 * margen
    slot_h = (pdf.h - 2 * margen - gap) / 2
    slots_y = [margen, margen + slot_h + gap]

    estado_pagina = {"slot": 2, "total": 0}

    def nueva_etiqueta():
        estado_pagina["total"] += 1
        if estado_pagina["total"] > 1000:
            # Salvaguarda: nunca deberiamos llegar aqui con la geometria actual;
            # evita generar un PDF descontrolado si algun calculo de espacio fallara.
            raise RuntimeError("La etiqueta genero demasiadas paginas; revisa el numero de bultos o los productos de la factura.")
        if estado_pagina["slot"] >= 2:
            pdf.add_page()
            estado_pagina["slot"] = 0
        y0 = slots_y[estado_pagina["slot"]]
        estado_pagina["slot"] += 1
        pdf.set_draw_color(180, 180, 180)
        pdf.rect(margen, y0, slot_w, slot_h)
        pdf.set_draw_color(0, 0, 0)
        return y0

    for bulto in range(1, num_bultos + 1):
        y0 = nueva_etiqueta()
        y_limite = y0 + slot_h - _PAD - _FOOTER_H
        y = _dibujar_encabezado(pdf, margen, y0, slot_w, True, bulto, num_bultos,
                                 folio_txt, fecha, emisor, e, logo)
        _dibujar_pie(pdf, margen, y0, slot_w, slot_h, e, qr_png)

        idx = 0
        primero = True
        cw = slot_w - 2 * _PAD
        cx = margen + _PAD
        while True:
            if not primero:
                y0 = nueva_etiqueta()
                y_limite = y0 + slot_h - _PAD - _FOOTER_H
                y = _dibujar_encabezado(pdf, margen, y0, slot_w, False, bulto, num_bultos,
                                         folio_txt, fecha, emisor, e, logo)
                _dibujar_pie(pdf, margen, y0, slot_w, slot_h, e, qr_png)
            primero = False

            while idx < len(productos) and y + _ROW_H <= y_limite:
                _dibujar_fila_producto(pdf, cx, y, cw, productos[idx])
                y += _ROW_H
                idx += 1

            if idx >= len(productos):
                if y + _ROW_H + 1 > y_limite:
                    y0 = nueva_etiqueta()
                    y_limite = y0 + slot_h - _PAD - _FOOTER_H
                    y = _dibujar_encabezado(pdf, margen, y0, slot_w, False, bulto, num_bultos,
                                             folio_txt, fecha, emisor, e, logo)
                    _dibujar_pie(pdf, margen, y0, slot_w, slot_h, e, qr_png)
                _dibujar_total(pdf, cx, y, cw, total_cant)
                break
            # quedan productos pero no cupieron mas filas en esta etiqueta: continuar

    pdf_bytes = bytes(pdf.output())
    return send_file(
        io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False,
        download_name=f"etiqueta_{folio_txt}.pdf",
    )
