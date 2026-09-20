from flask import Blueprint, request, jsonify, render_template, g

from db import query
from auth import require_roles
import geo_mx

ventas_bp = Blueprint("ventas", __name__)
require_ventas = require_roles("administradores", "admin", realm="Aspel Inventario")

_TIPOS_VALIDOS = {"P": "Producto", "S": "Servicio", "K": "Kit"}


class _FiltroInvalido(Exception):
    def __init__(self, mensaje):
        self.mensaje = mensaje


def _parse_filtros(args):
    """Lee anio/mes/tipo de request.args. Lanza _FiltroInvalido si algo esta mal."""
    try:
        anio = int(args.get("anio", ""))
    except ValueError:
        raise _FiltroInvalido("Indica un anio valido.")

    mes = args.get("mes", "").strip()
    mes_num = None
    if mes:
        try:
            mes_num = int(mes)
            if not (1 <= mes_num <= 12):
                raise ValueError
        except ValueError:
            raise _FiltroInvalido("Mes invalido.")

    tipo = args.get("tipo", "").strip().upper()
    if tipo and tipo not in _TIPOS_VALIDOS:
        tipo = ""

    return anio, mes_num, tipo


@ventas_bp.route("/ventas")
@require_ventas
def pagina():
    return render_template("ventas.html", es_admin=(g.role == "admin"))


@ventas_bp.route("/api/ventas/anios")
@require_ventas
def anios():
    empresa = request.args.get("empresa", "").strip() or None
    try:
        _, rows = query("""
            SELECT DISTINCT EXTRACT(YEAR FROM FECHA_DOC)
            FROM FACTF01
            WHERE STATUS = 'E'
            ORDER BY 1 DESC
        """, empresa_id=empresa)
        return jsonify({"ok": True, "data": [int(r[0]) for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@ventas_bp.route("/api/ventas")
@require_ventas
def datos():
    empresa = request.args.get("empresa", "").strip() or None

    try:
        anio, mes_num, tipo = _parse_filtros(request.args)
    except _FiltroInvalido as e:
        return jsonify({"ok": False, "error": e.mensaje}), 400

    where = ["f.STATUS = 'E'", "EXTRACT(YEAR FROM f.FECHA_DOC) = ?"]
    params = [anio]
    if mes_num:
        where.append("EXTRACT(MONTH FROM f.FECHA_DOC) = ?")
        params.append(mes_num)
    if tipo:
        where.append("p.TIPO_PROD = ?")
        params.append(tipo)
    where_sql = " AND ".join(where)

    try:
        _, rows = query(f"""
            SELECT
                p.CVE_ART,
                COALESCE(i.DESCR, p.DESCR_ART) AS DESCR,
                i.LIN_PROD,
                p.TIPO_PROD,
                SUM(p.CANT) AS CANTIDAD,
                SUM(p.TOT_PARTIDA) AS IMPORTE,
                COUNT(DISTINCT p.CVE_DOC) AS NUM_VENTAS
            FROM PAR_FACTF01 p
            JOIN FACTF01 f ON f.CVE_DOC = p.CVE_DOC
            LEFT JOIN INVE01 i ON i.CVE_ART = p.CVE_ART
            WHERE {where_sql}
            GROUP BY p.CVE_ART, COALESCE(i.DESCR, p.DESCR_ART), i.LIN_PROD, p.TIPO_PROD
            ORDER BY IMPORTE DESC
        """, params, empresa_id=empresa)

        data = [{
            "cve_art":    r[0],
            "descr":      r[1] or "",
            "lin_prod":   r[2] or "",
            "tipo":       _TIPOS_VALIDOS.get(r[3], r[3] or "?"),
            "cantidad":   float(r[4] or 0),
            "importe":    float(r[5] or 0),
            "num_ventas": int(r[6] or 0),
        } for r in rows]

        resumen = {
            "importe_total":  sum(d["importe"] for d in data),
            "cantidad_total": sum(d["cantidad"] for d in data),
            "num_productos":  len(data),
        }

        por_mes = []
        if not mes_num:
            mes_where = ["f.STATUS = 'E'", "EXTRACT(YEAR FROM f.FECHA_DOC) = ?"]
            mes_params = [anio]
            if tipo:
                mes_where.append("p.TIPO_PROD = ?")
                mes_params.append(tipo)
            _, mes_rows = query(f"""
                SELECT EXTRACT(MONTH FROM f.FECHA_DOC) AS MES, SUM(p.TOT_PARTIDA)
                FROM PAR_FACTF01 p
                JOIN FACTF01 f ON f.CVE_DOC = p.CVE_DOC
                WHERE {" AND ".join(mes_where)}
                GROUP BY EXTRACT(MONTH FROM f.FECHA_DOC)
                ORDER BY 1
            """, mes_params, empresa_id=empresa)
            importes_por_mes = {int(m): float(imp or 0) for m, imp in mes_rows}
            por_mes = [importes_por_mes.get(m, 0.0) for m in range(1, 13)]

        return jsonify({"ok": True, "data": data, "resumen": resumen, "por_mes": por_mes})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@ventas_bp.route("/api/ventas/mapa")
@require_ventas
def mapa():
    empresa = request.args.get("empresa", "").strip() or None

    try:
        anio, mes_num, tipo = _parse_filtros(request.args)
    except _FiltroInvalido as e:
        return jsonify({"ok": False, "error": e.mensaje}), 400

    where = ["f.STATUS = 'E'", "EXTRACT(YEAR FROM f.FECHA_DOC) = ?"]
    params = [anio]
    if mes_num:
        where.append("EXTRACT(MONTH FROM f.FECHA_DOC) = ?")
        params.append(mes_num)
    if tipo:
        where.append("p.TIPO_PROD = ?")
        params.append(tipo)
    where_sql = " AND ".join(where)

    try:
        _, rows = query(f"""
            SELECT
                c.ESTADO,
                c.MUNICIPIO,
                p.CVE_ART,
                COALESCE(i.DESCR, p.DESCR_ART) AS DESCR,
                p.TIPO_PROD,
                SUM(p.CANT) AS CANTIDAD,
                SUM(p.TOT_PARTIDA) AS IMPORTE,
                COUNT(DISTINCT f.CVE_DOC) AS NUM_VENTAS,
                COUNT(DISTINCT c.CLAVE) AS NUM_CLIENTES
            FROM PAR_FACTF01 p
            JOIN FACTF01 f ON f.CVE_DOC = p.CVE_DOC
            JOIN CLIE01 c ON c.CLAVE = f.CVE_CLPV
            LEFT JOIN INVE01 i ON i.CVE_ART = p.CVE_ART
            WHERE {where_sql}
            GROUP BY c.ESTADO, c.MUNICIPIO, p.CVE_ART, COALESCE(i.DESCR, p.DESCR_ART), p.TIPO_PROD
        """, params, empresa_id=empresa)

        por_estado = {}
        por_municipio = {}
        sin_identificar = {"importe": 0.0, "num_ventas": 0, "num_clientes": 0}

        def _nuevo_bucket(nombre):
            return {"nombre": nombre, "importe": 0.0, "num_ventas": 0, "num_clientes": 0, "productos": {}}

        def _acumular(bucket, importe, num_ventas, num_clientes):
            bucket["importe"] += importe
            bucket["num_ventas"] += num_ventas
            bucket["num_clientes"] += num_clientes

        def _acumular_producto(bucket, cve_art, descr, tipo_prod, cantidad, importe, num_ventas):
            prod = bucket["productos"].setdefault(cve_art, {
                "cve_art": cve_art, "descr": descr or "",
                "tipo": _TIPOS_VALIDOS.get(tipo_prod, tipo_prod or "?"),
                "cantidad": 0.0, "importe": 0.0, "num_ventas": 0,
            })
            prod["cantidad"] += cantidad
            prod["importe"] += importe
            prod["num_ventas"] += num_ventas

        for estado_txt, municipio_txt, cve_art, descr, tipo_prod, cantidad, importe, num_ventas, num_clientes in rows:
            cantidad = float(cantidad or 0)
            importe = float(importe or 0)
            num_ventas = int(num_ventas or 0)
            num_clientes = int(num_clientes or 0)

            estado = geo_mx.normalizar_estado(estado_txt)
            if not estado:
                _acumular(sin_identificar, importe, num_ventas, num_clientes)
                continue

            eid = estado["id"]
            bucket_e = por_estado.setdefault(eid, {"id": eid, **_nuevo_bucket(estado["nombre"])})
            _acumular(bucket_e, importe, num_ventas, num_clientes)
            _acumular_producto(bucket_e, cve_art, descr, tipo_prod, cantidad, importe, num_ventas)

            municipio = geo_mx.normalizar_municipio(eid, municipio_txt) if municipio_txt else None
            nombre_mun = municipio or "No identificado"
            bucket_m = por_municipio.setdefault(eid, {}).setdefault(nombre_mun, _nuevo_bucket(nombre_mun))
            _acumular(bucket_m, importe, num_ventas, num_clientes)
            _acumular_producto(bucket_m, cve_art, descr, tipo_prod, cantidad, importe, num_ventas)

        def _finalizar(bucket, top=10):
            bucket["productos"] = sorted(
                bucket["productos"].values(), key=lambda p: p["importe"], reverse=True
            )[:top]
            return bucket

        for b in por_estado.values():
            _finalizar(b)
        for muns in por_municipio.values():
            for b in muns.values():
                _finalizar(b)

        return jsonify({
            "ok": True,
            "por_estado": list(por_estado.values()),
            "por_municipio": {eid: list(m.values()) for eid, m in por_municipio.items()},
            "sin_identificar": sin_identificar,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
