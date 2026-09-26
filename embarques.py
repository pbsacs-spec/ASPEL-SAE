"""
Etiquetas de embarque generadas a partir de facturas de Aspel SAE.

Solo lee de Aspel (FACTF01, CLIE01, PARAM_DATOSEMP01, PARAM_DOMFISCAL01); los datos
propios del embarque (destinatario editado, chofer/unidad o paqueteria/guia, y los
datos del Emisor) se guardan localmente: ver embarques_db.py y embarque_config.ini.
"""
import base64
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
    """datos: request.form. Solo actualiza los campos que de verdad llegaron
    en el POST -- si el campo no viene (ej. estaba disabled porque modo_cliente
    esta activo en el formulario) se deja el valor ya guardado sin tocar, en
    vez de borrarlo."""
    cfg = _leer_emisor_cfg()
    seccion = dict(cfg[empresa_id]) if cfg.has_section(empresa_id) else {}
    for c in _CAMPOS_EMISOR:
        if c in datos:
            seccion[c] = (datos.get(c) or "").strip()
    cfg[empresa_id] = seccion
    _guardar_emisor_cfg(cfg)


def emisor_modo_cliente(empresa_id):
    """True si esta empresa muestra en la etiqueta/comprobante los datos del
    cliente facturado como Emisor, en vez de un Emisor fijo -- para negocios
    que hacen logistica a nombre de un tercero (entregan a los clientes de su
    cliente) y no deben aparecer ellos mismos como remitente."""
    cfg = _leer_emisor_cfg()
    return cfg.has_section(empresa_id) and cfg.getboolean(empresa_id, "modo_cliente", fallback=False)


def set_emisor_modo_cliente(empresa_id, activo):
    cfg = _leer_emisor_cfg()
    seccion = dict(cfg[empresa_id]) if cfg.has_section(empresa_id) else {}
    seccion["modo_cliente"] = "true" if activo else "false"
    cfg[empresa_id] = seccion
    _guardar_emisor_cfg(cfg)


def _emisor_desde_cliente(empresa_id, cliente_clave):
    """Arma un Emisor con la forma de _CAMPOS_EMISOR a partir de los datos
    generales (no los de envio) del cliente facturado."""
    vacio = {c: "" for c in _CAMPOS_EMISOR}
    if not cliente_clave:
        return vacio
    try:
        _, rows = query("""
            SELECT NOMBRE, RFC, CALLE, NUMEXT, NUMINT, COLONIA, CODIGO,
                   MUNICIPIO, ESTADO, PAIS, TELEFONO
            FROM __CLIE__ WHERE TRIM(CLAVE) = ?
        """, [cliente_clave], empresa_id=empresa_id)
        if not rows:
            return vacio
        nombre, rfc, calle, numext, numint, colonia, cp, municipio, estado, pais, telefono = rows[0]
        return {
            "nombre_empresa": (nombre or "").strip(), "rfc": (rfc or "").strip(),
            "calle": (calle or "").strip(), "numext": (numext or "").strip(),
            "numint": (numint or "").strip(), "colonia": (colonia or "").strip(),
            "cp": (cp or "").strip(), "municipio": (municipio or "").strip(),
            "estado": (estado or "").strip(), "pais": (pais or "").strip(),
            "telefono": (telefono or "").strip(),
        }
    except Exception:
        return vacio


def _resolver_emisor(empresa_id, cliente_clave):
    if emisor_modo_cliente(empresa_id):
        return _emisor_desde_cliente(empresa_id, cliente_clave)
    return get_emisor(empresa_id)


def _resolver_logo(empresa_id):
    # En modo "cliente" no se muestra el logo de la propia empresa: la
    # etiqueta/comprobante no debe llevar ninguna marca de quien hace la
    # logistica, solo del cliente facturado.
    return None if emisor_modo_cliente(empresa_id) else _logo_empresa(empresa_id)


def _sugerido_emisor(empresa_id):
    """Prellenado desde Aspel (RFC y domicilio fiscal) para la primera vez que se
    configura el Emisor. El nombre comercial no se puede leer (Aspel lo encripta)."""
    datos = {c: "" for c in _CAMPOS_EMISOR}
    try:
        _, rows = query("SELECT RFC FROM __PARAM_DATOSEMP__", empresa_id=empresa_id)
        if rows:
            datos["rfc"] = (rows[0][0] or "").strip()
    except Exception:
        pass
    try:
        _, rows = query("""
            SELECT CALLE, NUMERO_EXT, NUMERO_INT, COLONIA, MUNICIPIO, ESTADO, PAIS, CP
            FROM __PARAM_DOMFISCAL__
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
        _, rows = query("SELECT LOGO_EMPRESA FROM __PARAM_DATOSEMP__", empresa_id=empresa_id)
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


_HTTPS_CERT = os.path.join(os.path.dirname(__file__), "https_cert", "cert.pem")


def _url_entrega(token):
    """URL publica de confirmacion de entrega para el QR. Si hay certificado HTTPS
    configurado (ver app.py: _iniciar_https_en_hilo), siempre apunta al puerto 5443
    -- el navegador del celular exige HTTPS para permitir el GPS (navigator.geolocation),
    sin importar si la etiqueta se genero desde el puerto 5000 normal. Sin certificado,
    cae al mismo host/puerto de siempre (solo se pierde la opcion de GPS)."""
    if os.path.exists(_HTTPS_CERT):
        host = request.host.split(":")[0]
        return f"https://{host}:5443/entrega/{token}"
    return request.url_root.rstrip("/") + "/entrega/" + token


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
            FROM __FACTF__ f
            JOIN __CLIE__ c ON c.CLAVE = f.CVE_CLPV
            WHERE f.STATUS = 'E'
              AND (CAST(f.FOLIO AS VARCHAR(20)) CONTAINING ? OR UPPER(c.NOMBRE) CONTAINING UPPER(?))
            ORDER BY f.FECHA_DOC DESC
        """, [q[:20], q], empresa_id=empresa)

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
            FROM __FACTF__ f
            JOIN __CLIE__ c ON c.CLAVE = f.CVE_CLPV
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
            FROM __FACTF__ f
            JOIN __CLIE__ c ON c.CLAVE = f.CVE_CLPV
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
        set_emisor_modo_cliente(empresa_id, request.form.get("modo_cliente") == "1")
        flash("Datos del emisor guardados.", "ok")
        return redirect(url_for("embarques.pagina_configuracion", empresa=empresa_id))

    datos = get_emisor(empresa_id)
    if not any(datos.values()):
        datos = _sugerido_emisor(empresa_id)

    return render_template(
        "embarques_config.html", es_admin=(g.role == "admin"),
        empresas=empresas, empresa_id=empresa_id, datos=datos,
        modo_cliente=emisor_modo_cliente(empresa_id),
    )


@embarques_bp.route("/embarques/choferes", methods=["GET", "POST"])
@require_embarques
def pagina_choferes():
    if request.method == "POST":
        chofer_id = request.form.get("chofer_id", "").strip()
        nombre = request.form.get("nombre", "").strip()
        if not nombre:
            flash("El nombre es obligatorio.", "err")
        else:
            embarques_db.chofer_guardar(int(chofer_id) if chofer_id else None, request.form)
            flash(f'Chofer "{nombre}" guardado.', "ok")
        return redirect(url_for("embarques.pagina_choferes"))

    return render_template(
        "embarques_choferes.html", es_admin=(g.role == "admin"),
        choferes=embarques_db.choferes_listar(),
        unidades=embarques_db.unidades_listar(solo_activos=True),
    )


@embarques_bp.route("/embarques/choferes/estatus", methods=["POST"])
@require_embarques
def choferes_estatus():
    chofer_id = int(request.form.get("chofer_id", 0) or 0)
    nuevo = "inactivo" if request.form.get("estatus") == "activo" else "activo"
    if chofer_id:
        embarques_db.chofer_cambiar_estatus(chofer_id, nuevo)
        flash(f"Chofer marcado como {nuevo}.", "ok")
    return redirect(url_for("embarques.pagina_choferes"))


@embarques_bp.route("/embarques/unidades", methods=["GET", "POST"])
@require_embarques
def pagina_unidades():
    if request.method == "POST":
        unidad_id = request.form.get("unidad_id", "").strip()
        descripcion = request.form.get("descripcion", "").strip()
        if not descripcion:
            flash("La descripcion es obligatoria.", "err")
        else:
            embarques_db.unidad_guardar(int(unidad_id) if unidad_id else None, request.form)
            flash(f'Unidad "{descripcion}" guardada.', "ok")
        return redirect(url_for("embarques.pagina_unidades"))

    return render_template(
        "embarques_unidades.html", es_admin=(g.role == "admin"),
        unidades=embarques_db.unidades_listar(),
    )


@embarques_bp.route("/embarques/unidades/estatus", methods=["POST"])
@require_embarques
def unidades_estatus():
    unidad_id = int(request.form.get("unidad_id", 0) or 0)
    nuevo = "inactivo" if request.form.get("estatus") == "activo" else "activo"
    if unidad_id:
        embarques_db.unidad_cambiar_estatus(unidad_id, nuevo)
        flash(f"Unidad marcada como {nuevo}.", "ok")
    return redirect(url_for("embarques.pagina_unidades"))


@embarques_bp.route("/api/choferes")
@require_embarques
def api_choferes():
    return jsonify({"ok": True, "data": embarques_db.choferes_listar(solo_activos=True)})


@embarques_bp.route("/api/unidades")
@require_embarques
def api_unidades():
    return jsonify({"ok": True, "data": embarques_db.unidades_listar(solo_activos=True)})


# ── Reportes de rutas ────────────────────────────────────────────────────

@embarques_bp.route("/embarques/reportes")
@require_embarques
def pagina_reportes():
    return render_template("embarques_reportes.html", es_admin=(g.role == "admin"))


@embarques_bp.route("/api/reportes/entregas")
@require_embarques
def api_reporte_entregas():
    empresa = _empresa_actual()
    hoy = datetime.date.today()
    desde = request.args.get("desde", "").strip() or (hoy - datetime.timedelta(days=7)).isoformat()
    hasta = request.args.get("hasta", "").strip() or hoy.isoformat()
    chofer_id = request.args.get("chofer_id", "").strip()

    try:
        entregas = embarques_db.reporte_entregas(empresa, desde, hasta, int(chofer_id) if chofer_id else None)
        totales = embarques_db.reporte_totales_por_chofer(empresa, desde, hasta)
        puntos = [
            {"lat": e["lat_entrega"], "lon": e["lon_entrega"], "folio": f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}",
             "cliente": e["dest_nombre"], "chofer": e["chofer"] or e["paqueteria"], "fecha": e["fecha_entrega"]}
            for e in entregas if e["lat_entrega"] is not None and e["lon_entrega"] is not None
        ]
        return jsonify({"ok": True, "entregas": entregas, "totales_por_chofer": totales, "puntos_mapa": puntos,
                         "desde": desde, "hasta": hasta})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Confirmacion de entrega (publica, sin login -- el chofer no tiene cuenta) ─

@embarques_bp.route("/entrega/<token>")
def entrega_pagina(token):
    e = embarques_db.obtener_por_token(token)
    if not e:
        return render_template("entrega_confirmar.html", encontrada=False), 404
    folio_txt = f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}".strip() or e["factura_cve_doc"]
    return render_template(
        "entrega_confirmar.html", encontrada=True, e=e, folio_txt=folio_txt, token=token,
        ya_entregada=(e["estatus"] == "entregado"),
    )


@embarques_bp.route("/entrega/<token>/confirmar", methods=["POST"])
def entrega_confirmar(token):
    e = embarques_db.obtener_por_token(token)
    if not e:
        return jsonify({"ok": False, "error": "Liga invalida."}), 404

    body = request.get_json(silent=True) or {}
    firma = (body.get("firma") or "").strip()
    if not firma.startswith("data:image/png;base64,"):
        return jsonify({"ok": False, "error": "Debes firmar antes de confirmar."}), 400
    if len(firma) > 2_000_000:
        return jsonify({"ok": False, "error": "La firma es demasiado grande."}), 400

    fotos_in = body.get("fotos") or []
    if not isinstance(fotos_in, list) or len(fotos_in) > 3:
        return jsonify({"ok": False, "error": "Maximo 3 fotos."}), 400
    fotos_bytes = []
    for foto in fotos_in:
        foto = (foto or "").strip()
        if not foto.startswith("data:image/"):
            continue
        if len(foto) > 8_000_000:
            return jsonify({"ok": False, "error": "Una de las fotos es demasiado grande."}), 400
        try:
            fotos_bytes.append(base64.b64decode(foto.split(",", 1)[1]))
        except (IndexError, ValueError, base64.binascii.Error):
            return jsonify({"ok": False, "error": "Una de las fotos no es valida."}), 400

    lat, lon, precision = None, None, None
    ubicacion = body.get("ubicacion") or {}
    try:
        if ubicacion.get("lat") is not None and ubicacion.get("lon") is not None:
            lat = float(ubicacion["lat"])
            lon = float(ubicacion["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                lat = lon = None
            elif ubicacion.get("precision") is not None:
                precision = float(ubicacion["precision"])
    except (TypeError, ValueError):
        lat = lon = precision = None

    ok = embarques_db.marcar_entregado(e["id"], "qr", request.remote_addr, firma, fotos_bytes, lat, lon, precision)
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
    una etiqueta estos datos son opcionales (se pueden completar despues).
    chofer/unidad vienen del catalogo formal: chofer_id/unidad_id (seleccionado de
    un <select>) o chofer_nuevo/unidad_nuevo (texto, opcion "+ Nuevo...", se crea
    en el catalogo de una vez)."""
    tipo = body.get("tipo_embarque")
    if not tipo:
        if requerido:
            return None, "Tipo de embarque invalido."
        return None, None
    if tipo not in ("propio", "paqueteria"):
        return None, "Tipo de embarque invalido."

    chofer_id = unidad_id = None
    chofer_nombre = unidad_nombre = ""
    if tipo == "propio":
        if body.get("chofer_id"):
            try:
                chofer_id = int(body["chofer_id"])
            except (TypeError, ValueError):
                return None, "Chofer invalido."
            c = embarques_db.chofer_obtener(chofer_id)
            if not c:
                return None, "Chofer invalido."
            chofer_nombre = c["nombre"]
        elif (body.get("chofer_nuevo") or "").strip():
            chofer_id, chofer_nombre = embarques_db.chofer_obtener_o_crear(body["chofer_nuevo"])
        if not chofer_nombre:
            return None, "Indica el chofer."

        if body.get("unidad_id"):
            try:
                unidad_id = int(body["unidad_id"])
            except (TypeError, ValueError):
                return None, "Unidad invalida."
            u = embarques_db.unidad_obtener(unidad_id)
            if not u:
                return None, "Unidad invalida."
            unidad_nombre = u["descripcion"]
        elif (body.get("unidad_nuevo") or "").strip():
            unidad_id, unidad_nombre = embarques_db.unidad_obtener_o_crear(body["unidad_nuevo"])
    elif tipo == "paqueteria" and not (body.get("paqueteria") or "").strip():
        return None, "Indica la paqueteria."

    try:
        num_bultos = int(body.get("num_bultos") or 1)
        if not (1 <= num_bultos <= 200):
            raise ValueError
    except (TypeError, ValueError):
        return None, "Numero de bultos invalido (1-200)."

    return {
        "tipo_embarque": tipo,
        "chofer": chofer_nombre,
        "unidad": unidad_nombre,
        "chofer_id": chofer_id,
        "unidad_id": unidad_id,
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
        FROM __PAR_FACTF__ p
        LEFT JOIN __INVE__ i ON i.CVE_ART = p.CVE_ART
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


def _tam_imagen_px(fuente):
    """(ancho, alto) en pixeles de una imagen, dada como bytes o como ruta de archivo."""
    from PIL import Image as PILImage
    with PILImage.open(io.BytesIO(fuente) if isinstance(fuente, (bytes, bytearray)) else fuente) as img:
        return img.size


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

    emisor = _resolver_emisor(empresa, e["cliente_clave"])
    logo = _resolver_logo(empresa)
    folio_txt = _pdf_safe(f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}".strip() or e["factura_cve_doc"])
    fecha = e["fecha_creacion"][:10]
    num_bultos = max(1, min(200, int(e.get("num_bultos") or 1)))
    qr_png = _qr_png(_url_entrega(e["token_entrega"]))

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


# ── Comprobante de entrega (con firma) ────────────────────────────────────

def _comprobante_pdf(e, emisor, logo):
    folio_txt = _pdf_safe(f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}".strip() or e["factura_cve_doc"])

    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_margin(15)

    if logo:
        try:
            info = pdf.image(logo, x=Align.C, y=12, w=28)
            pdf.set_y(12 + info.rendered_height + 4)
        except Exception:
            pdf.set_y(14)
    else:
        pdf.set_y(14)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "COMPROBANTE DE ENTREGA", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, f"Factura {folio_txt}", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    def bloque(titulo, lineas):
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_fill_color(230, 236, 245)
        pdf.cell(0, 8, f"  {titulo}", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)
        for i, linea in enumerate(lineas):
            pdf.set_font("Helvetica", "B" if i == 0 else "", 10 if i == 0 else 9.5)
            pdf.multi_cell(0, 6, _pdf_safe(linea), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

    bloque("EMISOR", _lineas_compactas(
        emisor["nombre_empresa"] or "(sin configurar)", emisor["calle"], emisor["numext"],
        emisor["numint"], emisor["colonia"], emisor["cp"], emisor["municipio"], emisor["estado"],
        f"RFC: {emisor['rfc']}" if emisor["rfc"] else "",
    ))

    bloque("DESTINATARIO", _lineas_compactas(
        e["dest_nombre"], e["dest_calle"], e["dest_numext"], e["dest_numint"], e["dest_colonia"],
        e["dest_cp"], e["dest_municipio"], e["dest_estado"],
        f"Tel: {e['dest_telefono']}" if e["dest_telefono"] else "",
    ))

    lineas_envio = []
    if e["tipo_embarque"] == "propio":
        lineas_envio = ["Envio propio", f"Chofer: {e['chofer'] or ''}", f"Unidad: {e['unidad'] or ''}"]
    elif e["tipo_embarque"] == "paqueteria":
        lineas_envio = [f"Paqueteria: {e['paqueteria'] or ''}", f"Guia: {e['guia'] or ''}"]
    fecha_emb = (e["fecha_embarque"] or "")[:16].replace("T", " ")
    if fecha_emb:
        lineas_envio.append(f"Fecha de embarque: {fecha_emb}")
    if lineas_envio:
        bloque("DATOS DE ENVIO", lineas_envio)

    fecha_ent = (e["fecha_entrega"] or "")[:16].replace("T", " ")
    via_txt = {"qr": "Confirmado desde el QR de la etiqueta", "whatsapp": "Confirmado por WhatsApp"}.get(
        e["entregado_via"], e["entregado_via"] or "")
    lineas_entrega = [f"Fecha de entrega: {fecha_ent}", via_txt]
    tiene_ubicacion = e.get("lat_entrega") is not None and e.get("lon_entrega") is not None
    if tiene_ubicacion:
        prec_txt = f" (precision ~{e['precision_entrega']:.0f} m)" if e.get("precision_entrega") else ""
        lineas_entrega.append(f"Ubicacion GPS: {e['lat_entrega']:.6f}, {e['lon_entrega']:.6f}{prec_txt}")
    bloque("ENTREGA", lineas_entrega)

    if tiene_ubicacion:
        maps_url = f"https://www.google.com/maps?q={e['lat_entrega']},{e['lon_entrega']}"
        pdf.set_font("Helvetica", "U", 9.5)
        pdf.set_text_color(26, 58, 92)
        pdf.cell(0, 6, "Ver ubicacion en Google Maps", link=maps_url, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    # pdf.image() no respeta auto_page_break (solo cell()/multi_cell() lo hacen): una foto
    # de celular en vertical, alta, se dibujaba mas alla del borde de la pagina y esa parte
    # simplemente no se ve ("cortada"). Por eso aqui se mide el tamano real de cada imagen
    # ANTES de dibujarla y se decide si hace falta saltar de pagina o encoger el ancho.
    max_h_pagina = pdf.page_break_trigger - pdf.t_margin

    def espacio_disponible():
        return pdf.page_break_trigger - pdf.get_y()

    firma_bytes, w_firma, h_firma = None, 70, 0
    if e["firma_entrega"] and "," in e["firma_entrega"]:
        try:
            firma_bytes = base64.b64decode(e["firma_entrega"].split(",", 1)[1])
            aw, ah = _tam_imagen_px(firma_bytes)
            h_firma = w_firma * (ah / aw)
            if h_firma > max_h_pagina:
                w_firma *= max_h_pagina / h_firma
                h_firma = max_h_pagina
        except Exception:
            firma_bytes = None

    if 12 + h_firma > espacio_disponible():
        pdf.add_page()
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(230, 236, 245)
    pdf.cell(0, 8, "  FIRMA DE QUIEN RECIBE", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    if firma_bytes:
        y_firma = pdf.get_y()
        info = pdf.image(firma_bytes, x=pdf.l_margin, y=y_firma, w=w_firma)
        pdf.set_y(y_firma + info.rendered_height + 4)
    else:
        pdf.set_font("Helvetica", "", 9.5)
        pdf.cell(0, 6, "Sin firma (entrega confirmada por WhatsApp).", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    fotos = embarques_db.fotos_de(e)
    if fotos:
        ancho_disp = pdf.w - pdf.l_margin - pdf.r_margin
        gap = 4
        foto_w = (ancho_disp - gap * (len(fotos) - 1)) / len(fotos)

        rutas = [embarques_db.ruta_foto(n) for n in fotos]
        alto_fila = 0
        for ruta in rutas:
            if os.path.exists(ruta):
                try:
                    aw, ah = _tam_imagen_px(ruta)
                    alto_fila = max(alto_fila, foto_w * (ah / aw))
                except Exception:
                    pass
        if alto_fila > max_h_pagina:
            foto_w *= max_h_pagina / alto_fila
            alto_fila = max_h_pagina

        pdf.ln(6)
        if 12 + alto_fila > espacio_disponible():
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_fill_color(230, 236, 245)
        pdf.cell(0, 8, "  EVIDENCIA FOTOGRAFICA", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)
        y0, x = pdf.get_y(), pdf.l_margin
        for ruta in rutas:
            if os.path.exists(ruta):
                try:
                    pdf.image(ruta, x=x, y=y0, w=foto_w)
                except Exception:
                    pass
            x += foto_w + gap
        pdf.set_y(y0 + alto_fila + 4)

    return bytes(pdf.output())


def _comprobante_response(e):
    if e["estatus"] != "entregado":
        return jsonify({"ok": False, "error": "Esta etiqueta todavia no ha sido entregada."}), 409
    emisor = _resolver_emisor(e["empresa_id"], e["cliente_clave"])
    logo = _resolver_logo(e["empresa_id"])
    pdf_bytes = _comprobante_pdf(e, emisor, logo)
    folio_txt = f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}".strip() or e["factura_cve_doc"]
    return send_file(
        io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False,
        download_name=f"comprobante_{folio_txt}.pdf",
    )


@embarques_bp.route("/entrega/<token>/comprobante.pdf")
def entrega_comprobante(token):
    e = embarques_db.obtener_por_token(token)
    if not e:
        return jsonify({"ok": False, "error": "Liga invalida."}), 404
    return _comprobante_response(e)


@embarques_bp.route("/embarques/<int:embarque_id>/comprobante.pdf")
@require_embarques
def embarque_comprobante(embarque_id):
    empresa = _empresa_actual()
    e = embarques_db.obtener_embarque(embarque_id, empresa_id=empresa)
    if not e:
        return jsonify({"ok": False, "error": "Etiqueta no encontrada."}), 404
    return _comprobante_response(e)


def _servir_foto(e, nombre):
    if nombre not in embarques_db.fotos_de(e):
        return jsonify({"ok": False, "error": "Foto no encontrada."}), 404
    ruta = embarques_db.ruta_foto(nombre)
    if not os.path.exists(ruta):
        return jsonify({"ok": False, "error": "Foto no encontrada."}), 404
    return send_file(ruta, mimetype="image/jpeg")


@embarques_bp.route("/entrega/<token>/foto/<nombre>")
def entrega_foto(token, nombre):
    e = embarques_db.obtener_por_token(token)
    if not e:
        return jsonify({"ok": False, "error": "Liga invalida."}), 404
    return _servir_foto(e, nombre)


@embarques_bp.route("/embarques/<int:embarque_id>/foto/<nombre>")
@require_embarques
def embarque_foto(embarque_id, nombre):
    empresa = _empresa_actual()
    e = embarques_db.obtener_embarque(embarque_id, empresa_id=empresa)
    if not e:
        return jsonify({"ok": False, "error": "Etiqueta no encontrada."}), 404
    return _servir_foto(e, nombre)
