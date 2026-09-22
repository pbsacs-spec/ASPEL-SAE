"""
Etiquetas de embarque generadas a partir de facturas de Aspel SAE.

Solo lee de Aspel (FACTF01, CLIE01, PARAM_DATOSEMP01, PARAM_DOMFISCAL01); los datos
propios del embarque (destinatario editado, chofer/unidad o paqueteria/guia, y los
datos del Emisor) se guardan localmente: ver embarques_db.py y embarque_config.ini.
"""
import configparser
import io
import os
import threading

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, g, send_file
from fpdf import FPDF
from fpdf.enums import XPos, YPos, Align

from db import query, load_empresas
from auth import require_roles
import embarques_db

embarques_bp = Blueprint("embarques", __name__)
require_embarques = require_roles("administradores", "admin", realm="Aspel Inventario")

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

    try:
        existentes = embarques_db.buscar_por_factura(empresa, factura["cve_doc"])
        embarque_id = embarques_db.crear_embarque(empresa, factura, destinatario, g.username)
        return jsonify({"ok": True, "id": embarque_id, "duplicado": bool(existentes)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@embarques_bp.route("/api/embarques/<int:embarque_id>/embarcar", methods=["POST"])
@require_embarques
def api_embarcar(embarque_id):
    empresa = _empresa_actual()
    body = request.get_json(silent=True) or {}
    tipo = body.get("tipo_embarque")

    if tipo not in ("propio", "paqueteria"):
        return jsonify({"ok": False, "error": "Tipo de embarque invalido."}), 400
    if tipo == "propio" and not (body.get("chofer") or "").strip():
        return jsonify({"ok": False, "error": "Indica el chofer."}), 400
    if tipo == "paqueteria" and not (body.get("paqueteria") or "").strip():
        return jsonify({"ok": False, "error": "Indica la paqueteria."}), 400

    embarque = embarques_db.obtener_embarque(embarque_id, empresa_id=empresa)
    if not embarque:
        return jsonify({"ok": False, "error": "Etiqueta no encontrada."}), 404

    try:
        embarques_db.marcar_embarcado(embarque_id, tipo, body, g.username)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── PDF de la etiqueta ───────────────────────────────────────────────────

def _linea_direccion(nombre, calle, numext, numint, colonia, cp, municipio, estado, pais, telefono, referencia):
    lineas = [nombre or "(sin nombre)"]
    calle_txt = calle or ""
    if numext:
        calle_txt += f" {numext}"
    if numint:
        calle_txt += f" Int. {numint}"
    if calle_txt.strip():
        lineas.append(calle_txt.strip())
    if colonia:
        lineas.append(f"Col. {colonia}")
    cp_mun = " ".join(x for x in [f"CP {cp}" if cp else "", municipio or ""] if x)
    if cp_mun:
        lineas.append(cp_mun)
    edo_pais = ", ".join(x for x in [estado or "", pais or ""] if x)
    if edo_pais:
        lineas.append(edo_pais)
    if telefono:
        lineas.append(f"Tel: {telefono}")
    if referencia:
        lineas.append(f"Ref: {referencia}")
    return lineas


@embarques_bp.route("/embarques/<int:embarque_id>/etiqueta.pdf")
@require_embarques
def etiqueta_pdf(embarque_id):
    empresa = _empresa_actual()
    e = embarques_db.obtener_embarque(embarque_id, empresa_id=empresa)
    if not e:
        return jsonify({"ok": False, "error": "Etiqueta no encontrada."}), 404

    emisor = get_emisor(empresa)

    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_margin(15)

    logo = _logo_empresa(empresa)
    if logo:
        try:
            info = pdf.image(logo, x=Align.C, y=10, w=32)
            pdf.set_y(10 + info.rendered_height + 4)
        except Exception:
            logo = None
    if not logo:
        pdf.set_y(12)

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "ETIQUETA DE EMBARQUE", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "", 11)
    folio_txt = f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}".strip() or e["factura_cve_doc"]
    pdf.cell(0, 8, f"Factura: {folio_txt}    Fecha de creacion: {e['fecha_creacion'][:10]}",
             align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(6)

    def bloque(titulo, lineas):
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_fill_color(230, 236, 245)
        pdf.cell(0, 9, f"  {titulo}", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 12)
        pdf.ln(2)
        for i, linea in enumerate(lineas):
            pdf.set_font("Helvetica", "B" if i == 0 else "", 12 if i == 0 else 11)
            pdf.multi_cell(0, 7, linea, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(6)

    lineas_emisor = _linea_direccion(
        emisor["nombre_empresa"] or "(configura el emisor en /embarques/configuracion)",
        emisor["calle"], emisor["numext"], emisor["numint"], emisor["colonia"], emisor["cp"],
        emisor["municipio"], emisor["estado"], emisor["pais"], emisor["telefono"], "",
    )
    if emisor["rfc"]:
        lineas_emisor.append(f"RFC: {emisor['rfc']}")
    bloque("EMISOR (remite)", lineas_emisor)

    bloque("DESTINATARIO (recibe)", _linea_direccion(
        e["dest_nombre"], e["dest_calle"], e["dest_numext"], e["dest_numint"], e["dest_colonia"],
        e["dest_cp"], e["dest_municipio"], e["dest_estado"], e["dest_pais"], e["dest_telefono"],
        e["dest_referencia"],
    ))

    if e["estatus"] == "embarcado":
        if e["tipo_embarque"] == "propio":
            lineas = [f"Envio propio", f"Chofer: {e['chofer'] or ''}", f"Unidad: {e['unidad'] or ''}"]
        else:
            lineas = [f"Paqueteria", f"Empresa: {e['paqueteria'] or ''}", f"Guia: {e['guia'] or ''}"]
        lineas.append(f"Fecha de embarque: {(e['fecha_embarque'] or '')[:10]}")
        bloque("DATOS DE EMBARQUE", lineas)

    pdf_bytes = bytes(pdf.output())
    return send_file(
        io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False,
        download_name=f"etiqueta_{folio_txt}.pdf",
    )
