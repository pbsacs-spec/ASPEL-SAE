"""
Almacenamiento local (SQLite) de las etiquetas de embarque generadas a partir de
facturas de Aspel SAE. No toca ninguna tabla de Aspel: es dato propio de esta app.
"""
import contextlib
import datetime
import os
import sqlite3
import threading

_DB_FILE = os.path.join(os.path.dirname(__file__), "embarques.db")

# Serializa las escrituras (SQLite ya serializa a nivel de archivo, pero esto evita
# condiciones de carrera al leer-antes-de-escribir, ej. validar duplicados).
_LOCK = threading.RLock()

_CAMPOS_DEST = [
    "dest_nombre", "dest_calle", "dest_numext", "dest_numint", "dest_colonia",
    "dest_cp", "dest_municipio", "dest_estado", "dest_pais", "dest_telefono",
    "dest_referencia",
]


@contextlib.contextmanager
def _conn():
    """A diferencia de usar sqlite3.Connection directamente como context manager
    (que solo hace commit/rollback y NUNCA cierra la conexion), esto tambien
    cierra el archivo al salir del bloque `with`."""
    con = sqlite3.connect(_DB_FILE)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with _LOCK, _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS embarques (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                empresa_id       TEXT NOT NULL,
                factura_cve_doc  TEXT NOT NULL,
                factura_serie    TEXT,
                factura_folio    INTEGER,
                cliente_clave    TEXT,
                cliente_nombre   TEXT,
                dest_nombre      TEXT,
                dest_calle       TEXT,
                dest_numext      TEXT,
                dest_numint      TEXT,
                dest_colonia     TEXT,
                dest_cp          TEXT,
                dest_municipio   TEXT,
                dest_estado      TEXT,
                dest_pais        TEXT,
                dest_telefono    TEXT,
                dest_referencia  TEXT,
                estatus          TEXT NOT NULL DEFAULT 'pendiente',
                tipo_embarque    TEXT,
                chofer           TEXT,
                unidad           TEXT,
                paqueteria       TEXT,
                guia             TEXT,
                notas            TEXT,
                fecha_creacion   TEXT NOT NULL,
                creado_por       TEXT NOT NULL,
                fecha_embarque   TEXT,
                embarcado_por    TEXT
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_embarques_factura
            ON embarques (empresa_id, factura_cve_doc)
        """)


init_db()


def _ahora():
    return datetime.datetime.now().isoformat(timespec="seconds")


def buscar_por_factura(empresa_id, factura_cve_doc):
    """Etiquetas ya creadas para esta factura (para avisar de posibles duplicados)."""
    with _conn() as con:
        rows = con.execute("""
            SELECT id, estatus, fecha_creacion FROM embarques
            WHERE empresa_id = ? AND factura_cve_doc = ?
            ORDER BY id DESC
        """, (empresa_id, factura_cve_doc)).fetchall()
        return [dict(r) for r in rows]


def crear_embarque(empresa_id, factura, destinatario, creado_por):
    """factura: dict con cve_doc, serie, folio, cliente_clave, cliente_nombre.
    destinatario: dict con las claves en _CAMPOS_DEST (sin el prefijo dest_)."""
    with _LOCK, _conn() as con:
        cur = con.execute(f"""
            INSERT INTO embarques (
                empresa_id, factura_cve_doc, factura_serie, factura_folio,
                cliente_clave, cliente_nombre,
                {", ".join(_CAMPOS_DEST)},
                estatus, fecha_creacion, creado_por
            ) VALUES (?, ?, ?, ?, ?, ?, {", ".join("?" for _ in _CAMPOS_DEST)}, 'pendiente', ?, ?)
        """, (
            empresa_id, factura["cve_doc"], factura.get("serie"), factura.get("folio"),
            factura.get("cliente_clave"), factura.get("cliente_nombre"),
            *[destinatario.get(c[len("dest_"):], "") for c in _CAMPOS_DEST],
            _ahora(), creado_por,
        ))
        return cur.lastrowid


def listar_embarques(empresa_id, estatus=None, desde=None, hasta=None):
    where = ["empresa_id = ?"]
    params = [empresa_id]
    if estatus:
        where.append("estatus = ?")
        params.append(estatus)
    if desde:
        where.append("date(fecha_creacion) >= date(?)")
        params.append(desde)
    if hasta:
        where.append("date(fecha_creacion) <= date(?)")
        params.append(hasta)
    with _conn() as con:
        rows = con.execute(f"""
            SELECT * FROM embarques WHERE {" AND ".join(where)}
            ORDER BY fecha_creacion DESC
        """, params).fetchall()
        return [dict(r) for r in rows]


def obtener_embarque(embarque_id, empresa_id=None):
    with _conn() as con:
        sql = "SELECT * FROM embarques WHERE id = ?"
        params = [embarque_id]
        if empresa_id:
            sql += " AND empresa_id = ?"
            params.append(empresa_id)
        row = con.execute(sql, params).fetchone()
        return dict(row) if row else None


def marcar_embarcado(embarque_id, tipo_embarque, datos, usuario):
    """tipo_embarque: 'propio' (datos: chofer, unidad) o 'paqueteria' (datos: paqueteria, guia)."""
    with _LOCK, _conn() as con:
        if tipo_embarque == "propio":
            con.execute("""
                UPDATE embarques SET estatus='embarcado', tipo_embarque='propio',
                    chofer=?, unidad=?, paqueteria=NULL, guia=NULL,
                    fecha_embarque=?, embarcado_por=?
                WHERE id = ?
            """, (datos.get("chofer", ""), datos.get("unidad", ""), _ahora(), usuario, embarque_id))
        else:
            con.execute("""
                UPDATE embarques SET estatus='embarcado', tipo_embarque='paqueteria',
                    chofer=NULL, unidad=NULL, paqueteria=?, guia=?,
                    fecha_embarque=?, embarcado_por=?
                WHERE id = ?
            """, (datos.get("paqueteria", ""), datos.get("guia", ""), _ahora(), usuario, embarque_id))
        return con.total_changes > 0
