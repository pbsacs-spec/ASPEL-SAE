"""
Catalogo de codigos postales de Mexico (SEPOMEX, abril 2016) para autocompletar
colonia/municipio/estado/pais al capturar una direccion y evitar errores de
captura -- codigos_postales.db se genera una sola vez a partir del CSV publico
de SEPOMEX y se distribuye junto con el codigo (es informacion de referencia,
no cambia con el uso de la app).
"""
import os
import sqlite3

_DB_PATH = os.path.join(os.path.dirname(__file__), "codigos_postales.db")


def buscar_cp(cp):
    """Retorna una lista de colonias para ese CP:
    [{"colonia":..., "tipo":..., "municipio":..., "estado":..., "ciudad":...}, ...]
    (puede haber varias colonias por CP), o [] si el CP no existe o es invalido."""
    cp = "".join(c for c in (cp or "") if c.isdigit()).zfill(5)
    if len(cp) != 5:
        return []
    con = sqlite3.connect(_DB_PATH)
    try:
        rows = con.execute(
            "SELECT colonia, tipo, municipio, estado, ciudad FROM cp WHERE cp = ? ORDER BY colonia",
            (cp,),
        ).fetchall()
        return [
            {"colonia": r[0], "tipo": r[1] or "", "municipio": r[2], "estado": r[3], "ciudad": r[4] or ""}
            for r in rows
        ]
    finally:
        con.close()
