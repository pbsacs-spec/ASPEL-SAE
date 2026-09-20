from flask import Blueprint, request, jsonify, render_template, g

from db import query
from auth import require_roles

ventas_bp = Blueprint("ventas", __name__)
require_ventas = require_roles("administradores", "admin", realm="Aspel Inventario")

_TIPOS_VALIDOS = {"P": "Producto", "S": "Servicio", "K": "Kit"}


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
        anio = int(request.args.get("anio", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Indica un anio valido."}), 400

    mes = request.args.get("mes", "").strip()
    mes_num = None
    if mes:
        try:
            mes_num = int(mes)
            if not (1 <= mes_num <= 12):
                raise ValueError
        except ValueError:
            return jsonify({"ok": False, "error": "Mes invalido."}), 400

    tipo = request.args.get("tipo", "").strip().upper()
    if tipo and tipo not in _TIPOS_VALIDOS:
        tipo = ""

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
