"""
Analisis de cartera de clientes (cuentas por cobrar): facturacion, dias de
credito y puntualidad de pago, para identificar clientes sanos vs morosos.

Modelo de datos verificado contra CLIE01.SALDO (reconciliacion >98%):
  - CUEN_M01  (TIPO_MOV='C') = cargos, una fila por factura con su fecha
    de vencimiento (FECHA_VENC).
  - CUEN_DET01 (TIPO_MOV='A') = pagos aplicados, agrupados por
    (CVE_CLIE, NO_FACTURA) para saber cuanto y cuando se pago cada factura.
"""
import datetime
from collections import defaultdict

from flask import Blueprint, request, jsonify, render_template, g

from db import query
from auth import require_roles

cartera_bp = Blueprint("cartera", __name__)
require_cartera = require_roles("administradores", "admin", realm="Aspel Inventario")


def _as_date(v):
    """Normaliza a datetime.date: fdb devuelve datetime.datetime para columnas
    TIMESTAMP pero datetime.date para columnas DATE, y solo el primero tiene
    .date()."""
    if v is None:
        return None
    return v.date() if isinstance(v, datetime.datetime) else v


def _cargos_y_pagos(empresa_id, cve_clie=None, meses=None):
    """Si cve_clie se da, filtra en SQL a un solo cliente (usado por el detalle,
    siempre historial completo). Si no, trae todos los clientes (resumen de
    cartera); ahi se puede acotar a los ultimos `meses` para que empresas con
    mucho historial (cientos de miles de filas en CUEN_M/CUEN_DET) no tarden
    minutos en cargar -- CUEN_DET no tiene indice por (CVE_CLIE, NO_FACTURA),
    asi que ese agrupado es lento sin importar si se hace en SQL o en Python."""
    filtro = " AND TRIM(CVE_CLIE) = ?" if cve_clie else ""
    params = [cve_clie] if cve_clie else []
    filtro_fecha = f" AND FECHA_APLI >= DATEADD(-{int(meses)} MONTH TO CURRENT_DATE)" if meses else ""

    _, cargos = query(f"""
        SELECT CVE_CLIE, NO_FACTURA, IMPORTE, FECHA_APLI, FECHA_VENC
        FROM __CUEN_M__
        WHERE TIPO_MOV = 'C'{filtro}{filtro_fecha}
    """, params, empresa_id=empresa_id)

    _, abonos = query(f"""
        SELECT CVE_CLIE, NO_FACTURA, SUM(IMPORTE), MAX(FECHA_APLI)
        FROM __CUEN_DET__
        WHERE TIPO_MOV = 'A'{filtro}{filtro_fecha}
        GROUP BY CVE_CLIE, NO_FACTURA
    """, params, empresa_id=empresa_id)
    pagos = {(c, f): (float(p or 0), u) for c, f, p, u in abonos}

    return cargos, pagos


def _clasificar(dias_vencido_max, pct_a_tiempo, tiene_historial):
    if dias_vencido_max is not None:
        if dias_vencido_max > 60:
            return "moroso"
        if dias_vencido_max > 30:
            return "atrasado"
        return "atencion"
    if tiene_historial and pct_a_tiempo < 80:
        return "atencion"
    return "saludable"


_MESES_RESUMEN = 24  # ver nota en _cargos_y_pagos sobre por que se acota el resumen


def _analizar_clientes(empresa_id, hoy=None):
    hoy = hoy or datetime.date.today()
    cargos, pagos = _cargos_y_pagos(empresa_id, meses=_MESES_RESUMEN)

    por_cliente = defaultdict(lambda: {
        "importe_total": 0.0, "num_facturas": 0,
        "a_tiempo": 0, "tarde": 0, "suma_dias_atraso": 0.0,
        "vencido_monto": 0.0, "vencido_num": 0, "vencido_dias_max": 0,
        "saldo_calc": 0.0,
    })

    for cve_clie, no_factura, importe, fecha_apli, fecha_venc in cargos:
        importe = float(importe)
        pagado, ult_pago = pagos.get((cve_clie, no_factura), (0.0, None))
        saldo = importe - pagado
        c = por_cliente[cve_clie]
        c["importe_total"] += importe
        c["num_facturas"] += 1
        c["saldo_calc"] += saldo

        if saldo <= 1:
            if ult_pago and fecha_venc:
                dias = (_as_date(ult_pago) - _as_date(fecha_venc)).days
                if dias <= 0:
                    c["a_tiempo"] += 1
                else:
                    c["tarde"] += 1
                    c["suma_dias_atraso"] += dias
        elif fecha_venc and _as_date(fecha_venc) < hoy:
            dias_venc = (hoy - _as_date(fecha_venc)).days
            c["vencido_monto"] += saldo
            c["vencido_num"] += 1
            c["vencido_dias_max"] = max(c["vencido_dias_max"], dias_venc)

    return por_cliente


@cartera_bp.route("/cartera")
@require_cartera
def pagina():
    return render_template("cartera.html", es_admin=(g.role == "admin"))


@cartera_bp.route("/api/cartera")
@require_cartera
def resumen():
    empresa = request.args.get("empresa", "").strip() or None

    try:
        por_cliente = _analizar_clientes(empresa)

        _, clientes = query("""
            SELECT CLAVE, NOMBRE, SALDO, DIASCRED, LIMCRED, CON_CREDITO, FCH_ULTCOM
            FROM __CLIE__
            WHERE STATUS = 'A'
        """, empresa_id=empresa)

        data = []
        for clave, nombre, saldo, diascred, limcred, con_credito, fch_ultcom in clientes:
            c = por_cliente.get(clave)
            saldo = float(saldo or 0)
            if not c and abs(saldo) < 1:
                continue  # sin movimientos ni saldo, no aporta a la cartera

            hist_facturas = (c["a_tiempo"] + c["tarde"]) if c else 0
            pct_a_tiempo = round(100 * c["a_tiempo"] / hist_facturas, 1) if c and hist_facturas else None
            dias_venc_max = c["vencido_dias_max"] if c and c["vencido_num"] else None

            data.append({
                "clave": clave.strip(),
                "nombre": nombre or "",
                "saldo": saldo,
                "dias_credito": int(diascred or 0),
                "limite_credito": float(limcred or 0),
                "con_credito": (con_credito or "").strip().upper() == "S",
                "fch_ultcom": fch_ultcom.strftime("%Y-%m-%d") if fch_ultcom else None,
                "num_facturas_historicas": c["num_facturas"] if c else 0,
                "pagadas_a_tiempo": c["a_tiempo"] if c else 0,
                "pagadas_tarde": c["tarde"] if c else 0,
                "pct_a_tiempo": pct_a_tiempo,
                "promedio_dias_atraso": round(c["suma_dias_atraso"] / c["tarde"], 1) if c and c["tarde"] else None,
                "vencido_monto": round(c["vencido_monto"], 2) if c else 0.0,
                "vencido_num": c["vencido_num"] if c else 0,
                "vencido_dias_max": dias_venc_max,
                "clasificacion": _clasificar(dias_venc_max, pct_a_tiempo, hist_facturas > 0),
            })

        data.sort(key=lambda d: (d["vencido_dias_max"] or 0, d["vencido_monto"]), reverse=True)

        resumen = {
            "num_clientes": len(data),
            "cartera_total": round(sum(d["saldo"] for d in data), 2),
            "cartera_vencida": round(sum(d["vencido_monto"] for d in data), 2),
            "num_morosos": sum(1 for d in data if d["clasificacion"] == "moroso"),
            "num_atrasados": sum(1 for d in data if d["clasificacion"] == "atrasado"),
            "num_atencion": sum(1 for d in data if d["clasificacion"] == "atencion"),
            "num_saludables": sum(1 for d in data if d["clasificacion"] == "saludable"),
        }

        nota = (
            f"La clasificacion (moroso/atrasado/etc.) y el detalle de vencidas solo "
            f"consideran facturas de los ultimos {_MESES_RESUMEN} meses. El saldo total "
            f"de cada cliente es el saldo real de Aspel, sin importar la antiguedad."
        )
        return jsonify({"ok": True, "data": data, "resumen": resumen, "nota": nota})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@cartera_bp.route("/api/cartera/<clave>")
@require_cartera
def detalle(clave):
    empresa = request.args.get("empresa", "").strip() or None
    clave = clave.strip()
    hoy = datetime.date.today()

    try:
        cargos, pagos = _cargos_y_pagos(empresa, cve_clie=clave)
        facturas = []
        for cve_clie, no_factura, importe, fecha_apli, fecha_venc in cargos:
            importe = float(importe)
            pagado, ult_pago = pagos.get((cve_clie, no_factura), (0.0, None))
            saldo = round(importe - pagado, 2)

            if saldo <= 1:
                estado = "pagada"
                if ult_pago and fecha_venc:
                    dias = (_as_date(ult_pago) - _as_date(fecha_venc)).days
                    estado = "pagada_tarde" if dias > 0 else "pagada_a_tiempo"
            elif fecha_venc and _as_date(fecha_venc) < hoy:
                estado = "vencida"
            else:
                estado = "vigente"

            facturas.append({
                "no_factura": no_factura,
                "importe": importe,
                "pagado": round(pagado, 2),
                "saldo": saldo,
                "fecha_emision": fecha_apli.strftime("%Y-%m-%d") if fecha_apli else None,
                "fecha_vencimiento": fecha_venc.strftime("%Y-%m-%d") if fecha_venc else None,
                "fecha_pago": ult_pago.strftime("%Y-%m-%d") if ult_pago else None,
                "estado": estado,
            })

        facturas.sort(key=lambda f: f["fecha_emision"] or "", reverse=True)
        return jsonify({"ok": True, "data": facturas})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
