"""
Integracion WhatsApp para Aspel SAE.
Soporta dos modos:
  1. Meta Cloud API  — webhook con token (requiere cuenta Meta Business)
  2. QR (whatsapp-web.js) — escaneo con telefono via Node.js en puerto 5001

Comandos:
  exist CLAVE        — existencias por almacen
  info CLAVE         — existencias + precios + costo
  buscar TEXTO       — busqueda por descripcion
  pdf CLAVE          — ficha del producto en PDF
  pdf buscar TEXTO   — resultados de busqueda en PDF
  excel CLAVE        — ficha del producto en Excel
  excel buscar TEXTO — resultados de busqueda en Excel
  entregado FOLIO    — registra la entrega de una etiqueta de embarque
  ayuda              — lista de comandos
"""
import base64
import configparser
import hashlib
import hmac
import io
import os
import requests
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, abort

from db import existencias_producto, buscar_productos, load_empresas
from auth import require_admin
import embarques_db

wa_bp = Blueprint("whatsapp", __name__)
CFG_FILE = os.path.join(os.path.dirname(__file__), "wa_config.ini")


# ── Config ────────────────────────────────────────────────────────

def _load():
    cfg = configparser.ConfigParser()
    cfg.read(CFG_FILE, encoding="utf-8")
    return {
        "verify_token":     cfg.get("whatsapp", "verify_token",     fallback=""),
        "access_token":     cfg.get("whatsapp", "access_token",     fallback=""),
        "phone_number_id":  cfg.get("whatsapp", "phone_number_id",  fallback=""),
        "app_secret":       cfg.get("whatsapp", "app_secret",       fallback=""),
        "numeros_con_costo": cfg.get("whatsapp", "numeros_con_costo", fallback=""),
    }


def _save(verify_token, access_token, phone_number_id, app_secret="", numeros_con_costo=""):
    cfg = configparser.ConfigParser()
    cfg["whatsapp"] = {
        "verify_token":      verify_token,
        "access_token":      access_token,
        "phone_number_id":   phone_number_id,
        "app_secret":        app_secret,
        "numeros_con_costo": numeros_con_costo,
    }
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


def _solo_digitos(s):
    return "".join(c for c in (s or "") if c.isdigit())


def _puede_ver_costo(remitente):
    """Solo los numeros listados en numeros_con_costo ven el costo por WhatsApp.
    Si la lista esta vacia, nadie ve costo (seguro por defecto)."""
    if not remitente:
        return False
    cfg = _load()
    permitidos = {_solo_digitos(n) for n in cfg["numeros_con_costo"].split(",") if n.strip()}
    return _solo_digitos(remitente) in permitidos


# ── Webhook Meta Cloud API ────────────────────────────────────────

@wa_bp.route("/whatsapp/webhook", methods=["GET"])
def verificar():
    cfg = _load()
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == cfg["verify_token"]):
        return request.args.get("hub.challenge", ""), 200
    return "Token invalido", 403


def _firma_valida(cfg):
    """Verifica X-Hub-Signature-256 contra app_secret (si esta configurado)."""
    if not cfg["app_secret"]:
        return True
    firma = request.headers.get("X-Hub-Signature-256", "")
    if not firma.startswith("sha256="):
        return False
    esperada = hmac.new(
        cfg["app_secret"].encode("utf-8"), request.get_data(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(firma[len("sha256="):], esperada)


@wa_bp.route("/whatsapp/webhook", methods=["POST"])
def recibir():
    cfg = _load()
    if not _firma_valida(cfg):
        abort(403)
    data = request.json or {}
    try:
        value = data["entry"][0]["changes"][0]["value"]
        msgs  = value.get("messages", [])
        if not msgs:
            return jsonify({"ok": True})

        msg  = msgs[0]
        tipo = msg.get("type", "")
        de   = msg["from"]

        if tipo == "text":
            texto    = msg["text"]["body"].strip()
            resultado = _procesar(texto, remitente=de)
            respuesta = (resultado if isinstance(resultado, str)
                         else "Para descargar archivos usa el bot de WhatsApp QR.")
        else:
            respuesta = "Solo proceso mensajes de texto. Escribe *ayuda* para ver los comandos."

        _enviar(de, respuesta)
    except (KeyError, IndexError, TypeError):
        pass
    return jsonify({"ok": True})


# ── Procesamiento de mensajes ─────────────────────────────────────

def _resolver_empresa(token):
    """Si token es un numero (1, 2...), devuelve el empresa_id correspondiente; si no, None."""
    if not token.isdigit():
        return None
    empresas, _ = load_empresas()
    keys = sorted(empresas.keys())
    idx = int(token) - 1
    return keys[idx] if 0 <= idx < len(keys) else None


def _procesar(texto, empresa_id=None, remitente=None):
    """Retorna str (texto) o dict con _archivo=True (para generar archivo)."""
    partes = texto.strip().split(None, 1)
    cmd = partes[0].lower() if partes else ""
    arg = partes[1].strip() if len(partes) > 1 else ""

    # Numero al inicio → seleccionar empresa y reprocesar el resto
    eid = _resolver_empresa(cmd)
    if eid is not None:
        return _procesar(arg, empresa_id=eid, remitente=remitente)

    if cmd in ("empresas", "empresa", "lista"):
        return _cmd_empresas()

    if cmd in ("ayuda", "help", "menu", "?", "hola"):
        return _ayuda()

    if cmd in ("exist", "existencias", "ex", "e"):
        return _cmd_exist(arg.upper() if arg else "", empresa_id)

    if cmd in ("info", "producto", "p"):
        return _cmd_info(arg.upper() if arg else "", empresa_id, remitente)

    if cmd in ("buscar", "busca", "b"):
        return _cmd_buscar(arg, empresa_id)

    if cmd in ("pdf", "excel"):
        return _cmd_archivo(cmd, arg, empresa_id, remitente)

    if cmd in ("entregado", "entrega", "entregue"):
        return _cmd_entregado(arg, empresa_id, remitente)

    return _cmd_exist(texto.upper(), empresa_id)


def _cmd_empresas():
    empresas, settings = load_empresas()
    keys = sorted(empresas.keys())
    lineas = ["*Empresas registradas:*\n"]
    for i, eid in enumerate(keys, 1):
        nombre = empresas[eid]["nombre"]
        marca  = " _(predeterminada)_" if eid == settings["default"] else ""
        lineas.append(f"• *{i}. {nombre}*{marca}")
    lineas += [
        "",
        "_Prefija el numero para consultar una empresa:_",
        "  `2 exist CLAVE`",
        "  `2 buscar TEXTO`",
    ]
    return "\n".join(lineas)


def _ayuda():
    empresas, _ = load_empresas()
    multi = len(empresas) > 1
    lines = [
        "*Consultas Aspel SAE*\n",
        "• *exist CLAVE* — existencias por almacen",
        "• *info CLAVE* — existencias + precios + ultimo costo",
        "• *buscar TEXTO* — buscar por descripcion",
        "• *pdf CLAVE* — ficha del producto en PDF",
        "• *pdf buscar TEXTO* — resultados en PDF",
        "• *excel CLAVE* — ficha del producto en Excel",
        "• *excel buscar TEXTO* — resultados en Excel",
        "• *entregado FOLIO* — registrar la entrega de un embarque",
    ]
    if multi:
        lines += [
            "• *empresas* — ver empresas registradas",
            "",
            "_Para otra empresa escribe el numero antes del comando:_",
            "  `2 exist CLAVE`",
        ]
    lines += ["", "_Ejemplo:_ exist 12AC", "_Ejemplo:_ excel buscar cartucho"]
    return "\n".join(lines)


def _empresa_tag(empresa_id):
    """Devuelve etiqueta '(Empresa X)' cuando empresa_id no es None."""
    if empresa_id is None:
        return ""
    empresas, settings = load_empresas()
    if empresa_id == settings.get("default"):
        return ""
    nombre = empresas.get(empresa_id, {}).get("nombre", empresa_id)
    return f" _({nombre})_"


def _cmd_exist(clave, empresa_id=None):
    if not clave:
        return "Indica la clave del producto.\n_Ejemplo: exist 12AC_"
    prod = existencias_producto(clave, empresa_id)
    if not prod:
        return f"No se encontro *{clave}*.\nEscribe *buscar TEXTO* para buscarlo."
    tag    = _empresa_tag(empresa_id)
    lineas = [f"*{prod['CVE_ART']}*{tag}\n{prod['DESCR'] or ''}", "", "Existencias:"]
    for a in prod.get("almacenes", []):
        lineas.append(f"  {a['descr']}: {a['existencia']:,.2f}")
    lineas.append(f"  *Total: {float(prod.get('EXIST_TOTAL') or 0):,.2f}*")
    return "\n".join(lineas)


def _cmd_info(clave, empresa_id=None, remitente=None):
    if not clave:
        return "Indica la clave del producto.\n_Ejemplo: info 12AC_"
    prod = existencias_producto(clave, empresa_id)
    if not prod:
        return f"No se encontro *{clave}*.\nEscribe *buscar TEXTO* para buscarlo."
    tag = _empresa_tag(empresa_id)
    p = lambda v: f"${float(v or 0):,.2f}"
    lineas = [
        f"*{prod['CVE_ART']}*{tag}",
        f"{prod['DESCR'] or ''}",
        f"Linea: {prod.get('LIN_PROD') or '-'}  |  Unidad: {prod.get('UNI_MED') or '-'}",
        "",
        "Existencias:",
    ]
    for a in prod.get("almacenes", []):
        lineas.append(f"  {a['descr']}: {a['existencia']:,.2f}")
    lineas.append(f"  *Total: {float(prod.get('EXIST_TOTAL') or 0):,.2f}*")
    lineas.append("")
    if _puede_ver_costo(remitente):
        lineas.append(f"Ult. costo:  {p(prod.get('ULT_COSTO'))}")
    lineas += [
        f"Precio 1:    {p(prod.get('PREC1'))}",
        f"Precio 2:    {p(prod.get('PREC2'))}",
        f"Precio 3:    {p(prod.get('PREC3'))}",
    ]
    return "\n".join(lineas)


def _cmd_entregado(arg, empresa_id=None, remitente=None):
    """Permite al chofer (o quien reciba el pedido) reportar una entrega por WhatsApp,
    igual que escanear el QR de la etiqueta: registra fecha/hora y el numero que avisa."""
    folio_digits = "".join(c for c in (arg or "").strip() if c.isdigit())
    if not folio_digits:
        return "Indica el folio de la factura.\n_Ejemplo: entregado 2259_"
    folio = int(folio_digits)

    eid = empresa_id
    if eid is None:
        _, settings = load_empresas()
        eid = settings["default"]

    e = embarques_db.buscar_por_folio(eid, folio)
    if not e:
        return f"No encontre ninguna etiqueta de embarque con folio *{folio}*."
    if e["estatus"] == "entregado":
        fecha = (e["fecha_entrega"] or "")[:16].replace("T", " ")
        return f"Esta entrega ya estaba registrada el {fecha}."

    embarques_db.marcar_entregado(e["id"], "whatsapp", remitente)
    actualizado = embarques_db.obtener_embarque(e["id"])
    fecha      = (actualizado["fecha_entrega"] or "")[:16].replace("T", " ")
    folio_txt  = f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}"
    return (
        f"*Entrega registrada*\n"
        f"Factura: {folio_txt}\n"
        f"Cliente: {e['dest_nombre'] or ''}\n"
        f"Fecha: {fecha}"
    )


def _cmd_buscar(texto, empresa_id=None):
    if not texto:
        return "Indica el texto a buscar.\n_Ejemplo: buscar cartucho_"
    prods = buscar_productos(texto, limite=8, empresa_id=empresa_id)
    if not prods:
        return f"Sin resultados para \"{texto}\"."
    tag    = _empresa_tag(empresa_id)
    lineas = [f"Resultados para \"{texto}\"{tag}:"]
    for p in prods:
        ex   = float(p.get("EXIST") or 0)
        desc = (p.get("DESCR") or "")[:40]
        lineas.append(f"• *{p['CVE_ART']}* — {desc}  ({ex:,.2f})")
    lineas.append("\n_Escribe *info CLAVE* para ver detalle_")
    return "\n".join(lineas)


def _cmd_archivo(fmt, arg, empresa_id=None, remitente=None):
    """Retorna sentinel dict para generacion de archivo, o str de error."""
    if not arg:
        return f"Indica la clave o usa: {fmt} buscar TEXTO"
    partes  = arg.split(None, 1)
    sub_cmd = partes[0].lower()
    sub_arg = partes[1].strip() if len(partes) > 1 else ""
    con_costo = _puede_ver_costo(remitente)
    if sub_cmd in ("buscar", "busca", "b"):
        if not sub_arg:
            return f"Indica el texto a buscar.\n_Ejemplo: {fmt} buscar cartucho_"
        return {"_archivo": True, "fmt": fmt, "tipo": "buscar", "arg": sub_arg,
                "empresa_id": empresa_id, "con_costo": con_costo}
    return {"_archivo": True, "fmt": fmt, "tipo": "producto", "arg": arg.upper(),
            "empresa_id": empresa_id, "con_costo": con_costo}


# ── Generacion de archivos ────────────────────────────────────────

def _st(s, max_len=None):
    """String seguro; trunca si max_len dado."""
    v = str(s) if s is not None else ""
    if max_len and len(v) > max_len:
        v = v[:max_len - 3] + "..."
    return v


def _safe_filename(texto, ext):
    safe = "".join(c for c in texto[:20] if c.isalnum() or c in " _-").strip().replace(" ", "_")
    return f"busqueda_{safe or 'resultados'}.{ext}"


def _generar_archivo_producto(fmt, clave, empresa_id=None, con_costo=True):
    """Retorna (base64_str, filename, error_str)."""
    if not clave or len(clave) > 16:
        return None, None, "Clave invalida."
    prod = existencias_producto(clave, empresa_id)
    if not prod:
        return None, None, f"No se encontro *{clave}*.\nEscribe *buscar TEXTO* para buscarlo."
    return (_pdf_producto(prod, con_costo) if fmt == "pdf"
            else _excel_producto(prod, con_costo))


def _generar_archivo_busqueda(fmt, texto, empresa_id=None):
    """Retorna (base64_str, filename, error_str)."""
    prods = buscar_productos(texto, limite=50, empresa_id=empresa_id)
    if not prods:
        return None, None, f"Sin resultados para \"{texto}\"."
    return _pdf_busqueda(texto, prods) if fmt == "pdf" else _excel_busqueda(texto, prods)


def _pdf_producto(prod, con_costo=True):
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(180, 12, _st(prod["CVE_ART"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(180, 8, _st(prod["DESCR"], 80), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(180, 6,
             f"Linea: {_st(prod.get('LIN_PROD')) or '-'}   Unidad: {_st(prod.get('UNI_MED')) or '-'}",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(180, 8, "Existencias por Almacen", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_fill_color(220, 220, 220)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(130, 7, "Almacen", border=1, fill=True)
    pdf.cell(50, 7, "Existencia", border=1, fill=True, align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "", 10)
    for a in prod.get("almacenes", []):
        pdf.cell(130, 6, _st(a["descr"], 55), border=1)
        pdf.cell(50, 6, f"{a['existencia']:,.2f}", border=1, align="R",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(130, 7, "TOTAL", border=1)
    pdf.cell(50, 7, f"{float(prod.get('EXIST_TOTAL') or 0):,.2f}", border=1, align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(180, 8, "Precios y Costos" if con_costo else "Precios", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    fmt_p = lambda v: f"${float(v or 0):,.2f}"
    filas_precio = [("Precio 1", "PREC1"), ("Precio 2", "PREC2"), ("Precio 3", "PREC3")]
    if con_costo:
        filas_precio = [("Ultimo Costo", "ULT_COSTO")] + filas_precio
    for label, key in filas_precio:
        pdf.cell(130, 6, label, border=1)
        pdf.cell(50, 6, fmt_p(prod.get(key)), border=1, align="R",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return base64.b64encode(pdf.output()).decode(), f"{prod['CVE_ART']}.pdf", None


def _excel_producto(prod, con_costo=True):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Producto"

    ws["A1"] = prod["CVE_ART"]
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = _st(prod["DESCR"])
    ws["A3"] = f"Linea: {prod.get('LIN_PROD') or '-'}   Unidad: {prod.get('UNI_MED') or '-'}"

    row = 5
    ws.cell(row, 1, "Almacen").font = Font(bold=True)
    ws.cell(row, 2, "Existencia").font = Font(bold=True)
    ws.cell(row, 2).alignment = Alignment(horizontal="right")
    row += 1

    for a in prod.get("almacenes", []):
        ws.cell(row, 1, _st(a["descr"]))
        c = ws.cell(row, 2, a["existencia"])
        c.number_format = "#,##0.00"
        c.alignment = Alignment(horizontal="right")
        row += 1

    ws.cell(row, 1, "TOTAL").font = Font(bold=True)
    c = ws.cell(row, 2, float(prod.get("EXIST_TOTAL") or 0))
    c.font = Font(bold=True)
    c.number_format = "#,##0.00"
    c.alignment = Alignment(horizontal="right")
    row += 2

    ws.cell(row, 1, "Precios y Costos" if con_costo else "Precios").font = Font(bold=True, size=12)
    row += 1
    filas_precio = [("Precio 1", "PREC1"), ("Precio 2", "PREC2"), ("Precio 3", "PREC3")]
    if con_costo:
        filas_precio = [("Ultimo Costo", "ULT_COSTO")] + filas_precio
    for label, key in filas_precio:
        ws.cell(row, 1, label)
        c = ws.cell(row, 2, float(prod.get(key) or 0))
        c.number_format = '"$"#,##0.00'
        c.alignment = Alignment(horizontal="right")
        row += 1

    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 18

    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue()).decode(), f"{prod['CVE_ART']}.xlsx", None


def _pdf_busqueda(texto, prods):
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(180, 10, f"Busqueda: {_st(texto, 40)}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(180, 6, f"{len(prods)} resultado(s)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    pdf.set_fill_color(220, 220, 220)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(35, 7, "Clave", border=1, fill=True)
    pdf.cell(115, 7, "Descripcion", border=1, fill=True)
    pdf.cell(30, 7, "Existencia", border=1, fill=True, align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "", 9)
    for p in prods:
        pdf.cell(35, 6, _st(p["CVE_ART"], 16), border=1)
        pdf.cell(115, 6, _st(p["DESCR"], 50), border=1)
        pdf.cell(30, 6, f"{float(p.get('EXIST') or 0):,.2f}", border=1, align="R",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return base64.b64encode(pdf.output()).decode(), _safe_filename(texto, "pdf"), None


def _excel_busqueda(texto, prods):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Resultados"

    ws["A1"] = f"Busqueda: {texto}"
    ws["A1"].font = Font(bold=True, size=12)

    for col, h in enumerate(["Clave", "Descripcion", "Existencia"], 1):
        ws.cell(3, col, h).font = Font(bold=True)

    for i, p in enumerate(prods, 4):
        ws.cell(i, 1, _st(p["CVE_ART"]))
        ws.cell(i, 2, _st(p["DESCR"]))
        c = ws.cell(i, 3, float(p.get("EXIST") or 0))
        c.number_format = "#,##0.00"
        c.alignment = Alignment(horizontal="right")

    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 45
    ws.column_dimensions["C"].width = 16

    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue()).decode(), _safe_filename(texto, "xlsx"), None


# ── Envio de mensaje ──────────────────────────────────────────────

def _enviar(to, body):
    cfg = _load()
    if not cfg["access_token"] or not cfg["phone_number_id"]:
        return False
    url = f"https://graph.facebook.com/v19.0/{cfg['phone_number_id']}/messages"
    try:
        r = requests.post(
            url,
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body, "preview_url": False},
            },
            headers={
                "Authorization": f"Bearer {cfg['access_token']}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


# ── Pagina de configuracion ───────────────────────────────────────

@wa_bp.route("/admin/whatsapp", methods=["GET"])
@require_admin
def config_pagina():
    cfg = _load()
    configurado = bool(cfg["access_token"] and cfg["phone_number_id"])
    return render_template("wa_config.html", cfg=cfg, configurado=configurado)


@wa_bp.route("/admin/whatsapp", methods=["POST"])
@require_admin
def config_guardar():
    _save(
        verify_token      = request.form.get("verify_token",      "").strip(),
        access_token      = request.form.get("access_token",      "").strip(),
        phone_number_id   = request.form.get("phone_number_id",   "").strip(),
        app_secret        = request.form.get("app_secret",        "").strip(),
        numeros_con_costo = request.form.get("numeros_con_costo", "").strip(),
    )
    flash("Configuracion guardada.", "ok")
    return redirect(url_for("whatsapp.config_pagina"))


@wa_bp.route("/admin/whatsapp/test", methods=["POST"])
@require_admin
def config_probar():
    numero = request.form.get("numero", "").strip().replace("+", "").replace(" ", "")
    if numero:
        ok = _enviar(numero, "Prueba desde Aspel SAE. Escribe *ayuda* para ver los comandos.")
        flash("Mensaje de prueba enviado." if ok else
              "Error al enviar. Verifica el token y el ID del numero.", "ok" if ok else "err")
    else:
        flash("Ingresa un numero de telefono.", "err")
    return redirect(url_for("whatsapp.config_pagina"))


@wa_bp.route("/admin/whatsapp/simular", methods=["POST"])
@require_admin
def simular():
    texto     = request.form.get("mensaje", "").strip()
    remitente = request.form.get("numero", "").strip() or None
    if texto:
        resultado = _procesar(texto, remitente=remitente)
        if isinstance(resultado, dict):
            flash("Simulacion de archivos no disponible aqui. Usa el bot QR.", "sim")
        else:
            flash(f"RESPUESTA:\n{resultado}", "sim")
    return redirect(url_for("whatsapp.config_pagina"))


# ── Endpoints para modo QR (whatsapp-web.js) ──────────────────────

WA_SERVICE_URL = "http://localhost:5001"


@wa_bp.route("/api/wa/procesar", methods=["POST"])
def api_procesar():
    """El servicio Node.js llama aqui cuando llega un mensaje. Solo local."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    data      = request.json or {}
    texto     = data.get("texto", "").strip()
    remitente = data.get("de") or None
    if not texto:
        return jsonify({"respuesta": ""}), 400

    resultado = _procesar(texto, remitente=remitente)

    if isinstance(resultado, dict) and resultado.get("_archivo"):
        fmt        = resultado["fmt"]
        tipo       = resultado["tipo"]
        arg        = resultado["arg"]
        empresa_id = resultado.get("empresa_id")
        con_costo  = resultado.get("con_costo", False)
        if tipo == "buscar":
            b64, filename, err = _generar_archivo_busqueda(fmt, arg, empresa_id)
        else:
            b64, filename, err = _generar_archivo_producto(fmt, arg, empresa_id, con_costo)
        if err:
            return jsonify({"respuesta": err})
        mimetype = (
            "application/pdf"
            if fmt == "pdf"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        return jsonify({"archivo": {"base64": b64, "mimetype": mimetype, "filename": filename}})

    return jsonify({"respuesta": resultado})


@wa_bp.route("/admin/whatsapp/qr-status")
@require_admin
def qr_status():
    try:
        r = requests.get(f"{WA_SERVICE_URL}/status", timeout=2)
        return jsonify(r.json())
    except Exception:
        return jsonify({"status": "offline"})


@wa_bp.route("/admin/whatsapp/qr-image")
@require_admin
def qr_image():
    try:
        r = requests.get(f"{WA_SERVICE_URL}/qr.png", timeout=3)
        if r.status_code == 200:
            return r.content, 200, {
                "Content-Type": "image/png",
                "Cache-Control": "no-store",
            }
        return "", 204
    except Exception:
        return "", 503
