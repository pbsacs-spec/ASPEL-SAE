import os
import secrets

from flask import Flask, render_template, request, jsonify, g

from db import query, get_almacenes, existencias_por_almacen, load_empresas
from whatsapp import wa_bp
from db_admin import db_admin_bp
from ventas import ventas_bp
from cartera import cartera_bp
from embarques import embarques_bp
from auth import require_dashboard


def _cargar_secret_key():
    env_key = os.environ.get("ASPEL_SECRET_KEY")
    if env_key:
        return env_key
    ruta = os.path.join(os.path.dirname(__file__), ".secret_key")
    if os.path.exists(ruta):
        return open(ruta, "r", encoding="utf-8").read().strip()
    clave = secrets.token_hex(32)
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(clave)
    return clave


app = Flask(__name__)
app.secret_key = _cargar_secret_key()
app.register_blueprint(wa_bp)
app.register_blueprint(db_admin_bp)
app.register_blueprint(ventas_bp)
app.register_blueprint(cartera_bp)
app.register_blueprint(embarques_bp)


@app.after_request
def _sin_cache(resp):
    """Evita que el navegador reuse una version cacheada entre roles/permisos.
    Los archivos estaticos (librerias JS vendorizadas) si se pueden cachear."""
    if not request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
@require_dashboard
def index():
    return render_template(
        "index.html",
        sin_costo=(g.role == "vendedores"),
        es_admin=(g.role == "admin"),
        ve_ventas=(g.role in ("administradores", "admin")),
    )


@app.route("/api/empresas")
@require_dashboard
def empresas_lista():
    try:
        empresas, settings = load_empresas()
        data = [
            {"id": eid, "nombre": emp["nombre"], "default": eid == settings["default"]}
            for eid, emp in empresas.items()
        ]
        return jsonify({"ok": True, "data": data, "default": settings["default"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/productos")
@require_dashboard
def productos():
    return _consultar_productos(incluir_costo=(g.role != "vendedores"))


def _consultar_productos(incluir_costo):
    buscar    = request.args.get("q", "").strip()
    linea     = request.args.get("linea", "").strip()
    almacen   = request.args.get("almacen", "").strip()
    cve_desde = request.args.get("cve_desde", "").strip().upper()
    cve_hasta = request.args.get("cve_hasta", "").strip().upper()
    solo_con  = request.args.get("solo_con", "0")
    empresa   = request.args.get("empresa", "").strip() or None

    # Validar empresa_id
    if empresa:
        empresas, _ = load_empresas()
        if empresa not in empresas:
            empresa = None

    alm_filtro = None
    if almacen:
        try:
            alm_filtro = int(almacen)
        except ValueError:
            return jsonify({"ok": False, "error": "Almacen invalido"}), 400

    where_parts = ["1=1"]
    params = []

    if buscar:
        where_parts.append(
            "(UPPER(i.CVE_ART) CONTAINING UPPER(?) OR UPPER(i.DESCR) CONTAINING UPPER(?))"
        )
        params += [buscar, buscar]
    if linea:
        where_parts.append("i.LIN_PROD = ?")
        params.append(linea)
    if cve_desde:
        where_parts.append("i.CVE_ART >= ?")
        params.append(cve_desde)
    if cve_hasta:
        where_parts.append("i.CVE_ART <= ?")
        params.append(cve_hasta)
    if alm_filtro is not None:
        where_parts.append(f"""
            COALESCE((
                SELECT m.EXISTENCIA FROM __MINVE__ m
                WHERE m.CVE_ART = i.CVE_ART AND m.ALMACEN = {alm_filtro}
                  AND m.NUM_MOV = (
                      SELECT MAX(m2.NUM_MOV) FROM __MINVE__ m2
                      WHERE m2.CVE_ART = m.CVE_ART AND m2.ALMACEN = m.ALMACEN
                  )
            ), 0) > 0
        """)
    elif solo_con == "1":
        where_parts.append("i.EXIST > 0")

    where = " AND ".join(where_parts)

    sql = f"""
        SELECT FIRST 500
            i.CVE_ART,
            i.DESCR,
            i.LIN_PROD,
            i.UNI_MED,
            i.EXIST     AS EXIST_TOTAL,
            i.STOCK_MIN,
            i.COSTO_PROM,
            i.ULT_COSTO,
            MAX(CASE WHEN p.CVE_PRECIO = 1 THEN p.PRECIO END) AS PREC1,
            MAX(CASE WHEN p.CVE_PRECIO = 2 THEN p.PRECIO END) AS PREC2,
            MAX(CASE WHEN p.CVE_PRECIO = 3 THEN p.PRECIO END) AS PREC3,
            i.STATUS
        FROM __INVE__ i
        LEFT JOIN __PRECIO_X_PROD__ p ON p.CVE_ART = i.CVE_ART
        WHERE {where}
        GROUP BY
            i.CVE_ART, i.DESCR, i.LIN_PROD, i.UNI_MED,
            i.EXIST, i.STOCK_MIN, i.COSTO_PROM, i.ULT_COSTO, i.STATUS
        ORDER BY i.CVE_ART
    """

    try:
        almacenes = get_almacenes(empresa_id=empresa)
        cols, rows = query(sql, params, empresa_id=empresa)
        data = [dict(zip(cols, r)) for r in rows]

        if data:
            claves = [r["CVE_ART"] for r in data]
            exist_map = existencias_por_almacen(claves, almacenes, empresa_id=empresa)

            for row in data:
                cve = row["CVE_ART"]
                for alm in almacenes:
                    row[f"ALM_{alm['cve']}"] = exist_map.get(cve, {}).get(alm["cve"], 0.0)
                exist     = float(row.get("EXIST_TOTAL") or 0)
                stock_min = float(row.get("STOCK_MIN") or 0)
                row["BAJO_STOCK"] = stock_min > 0 and exist <= stock_min

        if not incluir_costo:
            for row in data:
                row.pop("COSTO_PROM", None)
                row.pop("ULT_COSTO", None)

        return jsonify({"ok": True, "data": data, "almacenes": almacenes})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lineas")
@require_dashboard
def lineas():
    empresa = request.args.get("empresa", "").strip() or None
    try:
        _, rows = query("""
            SELECT DISTINCT LIN_PROD FROM __INVE__
            WHERE LIN_PROD IS NOT NULL AND LIN_PROD <> ''
            ORDER BY LIN_PROD
        """, empresa_id=empresa)
        return jsonify({"ok": True, "data": [r[0] for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/almacenes")
@require_dashboard
def almacenes_route():
    empresa = request.args.get("empresa", "").strip() or None
    try:
        return jsonify({"ok": True, "data": get_almacenes(empresa_id=empresa)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _iniciar_https_en_hilo():
    """Segundo servidor, en HTTPS, ademas del HTTP de siempre en el puerto 5000.
    Necesario porque el navegador del celular bloquea navigator.geolocation (GPS)
    en paginas servidas por HTTP -- exige un "contexto seguro" (HTTPS). Usa un
    certificado autofirmado (ver https_cert/README.txt); si no existe, se omite
    y la app sigue funcionando igual, solo por HTTP como antes."""
    cert = os.path.join(os.path.dirname(__file__), "https_cert", "cert.pem")
    key = os.path.join(os.path.dirname(__file__), "https_cert", "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        return
    import threading
    from werkzeug.serving import run_simple

    def _servir():
        run_simple("0.0.0.0", 5443, app, ssl_context=(cert, key), threaded=True)

    threading.Thread(target=_servir, daemon=True, name="https-5443").start()
    print("HTTPS (autofirmado) tambien disponible en el puerto 5443", flush=True)


if __name__ == "__main__":
    _iniciar_https_en_hilo()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
