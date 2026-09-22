"""
Almacenamiento local (SQLite) de las etiquetas de embarque generadas a partir de
facturas de Aspel SAE. No toca ninguna tabla de Aspel: es dato propio de esta app.
"""
import contextlib
import datetime
import json
import os
import secrets
import sqlite3
import threading

_DB_FILE = os.path.join(os.path.dirname(__file__), "embarques.db")
_FOTOS_DIR = os.path.join(os.path.dirname(__file__), "embarque_fotos")
os.makedirs(_FOTOS_DIR, exist_ok=True)

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
                embarcado_por    TEXT,
                num_bultos       INTEGER NOT NULL DEFAULT 1
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_embarques_factura
            ON embarques (empresa_id, factura_cve_doc)
        """)
        # Migracion: bases creadas antes de agregar num_bultos.
        cols = [r["name"] for r in con.execute("PRAGMA table_info(embarques)").fetchall()]
        if "num_bultos" not in cols:
            con.execute("ALTER TABLE embarques ADD COLUMN num_bultos INTEGER NOT NULL DEFAULT 1")

        # Migracion: confirmacion de entrega (QR / WhatsApp).
        for columna, tipo in [
            ("token_entrega", "TEXT"), ("fecha_entrega", "TEXT"),
            ("entregado_via", "TEXT"), ("entregado_ref", "TEXT"),
            ("firma_entrega", "TEXT"), ("fotos_entrega", "TEXT"),
            ("lat_entrega", "REAL"), ("lon_entrega", "REAL"), ("precision_entrega", "REAL"),
        ]:
            if columna not in cols:
                con.execute(f"ALTER TABLE embarques ADD COLUMN {columna} {tipo}")
        con.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_embarques_token
            ON embarques (token_entrega)
        """)
        # Backfill: etiquetas creadas antes de este cambio no tienen token todavia.
        sin_token = con.execute(
            "SELECT id FROM embarques WHERE token_entrega IS NULL"
        ).fetchall()
        for row in sin_token:
            con.execute(
                "UPDATE embarques SET token_entrega = ? WHERE id = ?",
                (secrets.token_urlsafe(24), row["id"]),
            )

        con.execute("""
            CREATE TABLE IF NOT EXISTS catalogo (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo  TEXT NOT NULL,
                valor TEXT NOT NULL,
                UNIQUE(tipo, valor)
            )
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


def cve_docs_con_etiqueta(empresa_id):
    """Claves de documento (FACTF01.CVE_DOC) que ya tienen al menos una etiqueta
    creada en esta empresa, para poder excluirlas de 'facturas pendientes'."""
    with _conn() as con:
        rows = con.execute(
            "SELECT DISTINCT factura_cve_doc FROM embarques WHERE empresa_id = ?",
            (empresa_id,),
        ).fetchall()
        return {r["factura_cve_doc"] for r in rows}


def catalogo_agregar(tipo, valor):
    """Recuerda un chofer/unidad/paqueteria capturado, para sugerirlo despues."""
    valor = (valor or "").strip()
    if not valor:
        return
    with _LOCK, _conn() as con:
        con.execute("INSERT OR IGNORE INTO catalogo (tipo, valor) VALUES (?, ?)", (tipo, valor))


def catalogo_listar(tipo):
    with _conn() as con:
        rows = con.execute(
            "SELECT valor FROM catalogo WHERE tipo = ? ORDER BY valor", (tipo,)
        ).fetchall()
        return [r["valor"] for r in rows]


def _aprender_catalogo(tipo_embarque, datos):
    if tipo_embarque == "propio":
        catalogo_agregar("chofer", datos.get("chofer"))
        catalogo_agregar("unidad", datos.get("unidad"))
    elif tipo_embarque == "paqueteria":
        catalogo_agregar("paqueteria", datos.get("paqueteria"))


def crear_embarque(empresa_id, factura, destinatario, creado_por, embarque_info=None):
    """factura: dict con cve_doc, serie, folio, cliente_clave, cliente_nombre.
    destinatario: dict con las claves en _CAMPOS_DEST (sin el prefijo dest_).
    embarque_info: opcional, dict con tipo_embarque/chofer/unidad/paqueteria/guia/
    num_bultos -- si se da, la etiqueta se crea ya como 'embarcado'; si no, queda
    'pendiente' (se puede marcar despues)."""
    info = embarque_info or {}
    tipo = info.get("tipo_embarque")
    if tipo == "propio":
        estatus, chofer, unidad, paqueteria, guia = "embarcado", info.get("chofer", ""), info.get("unidad", ""), None, None
        fecha_embarque, embarcado_por = _ahora(), creado_por
    elif tipo == "paqueteria":
        estatus, chofer, unidad, paqueteria, guia = "embarcado", None, None, info.get("paqueteria", ""), info.get("guia", "")
        fecha_embarque, embarcado_por = _ahora(), creado_por
    else:
        estatus, chofer, unidad, paqueteria, guia = "pendiente", None, None, None, None
        fecha_embarque, embarcado_por = None, None
    num_bultos = max(1, min(200, int(info.get("num_bultos") or 1)))
    token_entrega = secrets.token_urlsafe(24)

    with _LOCK, _conn() as con:
        cur = con.execute(f"""
            INSERT INTO embarques (
                empresa_id, factura_cve_doc, factura_serie, factura_folio,
                cliente_clave, cliente_nombre,
                {", ".join(_CAMPOS_DEST)},
                estatus, tipo_embarque, chofer, unidad, paqueteria, guia, num_bultos,
                fecha_creacion, creado_por, fecha_embarque, embarcado_por, token_entrega
            ) VALUES (?, ?, ?, ?, ?, ?, {", ".join("?" for _ in _CAMPOS_DEST)},
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            empresa_id, factura["cve_doc"], factura.get("serie"), factura.get("folio"),
            factura.get("cliente_clave"), factura.get("cliente_nombre"),
            *[destinatario.get(c[len("dest_"):], "") for c in _CAMPOS_DEST],
            estatus, tipo, chofer, unidad, paqueteria, guia, num_bultos,
            _ahora(), creado_por, fecha_embarque, embarcado_por, token_entrega,
        ))
        embarque_id = cur.lastrowid

    if tipo:
        _aprender_catalogo(tipo, info)
    return embarque_id


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
    """tipo_embarque: 'propio' (datos: chofer, unidad) o 'paqueteria' (datos: paqueteria, guia).
    datos puede incluir num_bultos (cuantas etiquetas/cajas fisicas se imprimen).
    Sirve tanto para marcar por primera vez como para editar despues: fecha_embarque
    y embarcado_por solo se fijan la primera vez (COALESCE), no se pisan en ediciones.
    No aplica nada (regresa False) si la etiqueta ya fue entregada -- hay que
    reactivarla primero."""
    num_bultos = max(1, min(200, int(datos.get("num_bultos") or 1)))
    with _LOCK, _conn() as con:
        if tipo_embarque == "propio":
            con.execute("""
                UPDATE embarques SET estatus='embarcado', tipo_embarque='propio',
                    chofer=?, unidad=?, paqueteria=NULL, guia=NULL, num_bultos=?,
                    fecha_embarque=COALESCE(fecha_embarque, ?),
                    embarcado_por=COALESCE(embarcado_por, ?)
                WHERE id = ? AND estatus != 'entregado'
            """, (datos.get("chofer", ""), datos.get("unidad", ""), num_bultos, _ahora(), usuario, embarque_id))
        else:
            con.execute("""
                UPDATE embarques SET estatus='embarcado', tipo_embarque='paqueteria',
                    chofer=NULL, unidad=NULL, paqueteria=?, guia=?, num_bultos=?,
                    fecha_embarque=COALESCE(fecha_embarque, ?),
                    embarcado_por=COALESCE(embarcado_por, ?)
                WHERE id = ? AND estatus != 'entregado'
            """, (datos.get("paqueteria", ""), datos.get("guia", ""), num_bultos, _ahora(), usuario, embarque_id))
        cambios = con.total_changes > 0
    if cambios:
        _aprender_catalogo(tipo_embarque, datos)
    return cambios


def obtener_por_token(token):
    if not token:
        return None
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM embarques WHERE token_entrega = ?", (token,)
        ).fetchone()
        return dict(row) if row else None


def ruta_foto(nombre_archivo):
    return os.path.join(_FOTOS_DIR, nombre_archivo)


def guardar_fotos(embarque_id, fotos_bytes):
    """Escribe cada foto (bytes JPEG) a disco -- NO en la base, para no inflar
    embarques.db con binarios cada vez que se lista o consulta una etiqueta.
    Regresa la lista de nombres de archivo guardados."""
    nombres = []
    for i, contenido in enumerate(fotos_bytes, 1):
        nombre = f"{embarque_id}_{i}_{secrets.token_hex(4)}.jpg"
        with open(ruta_foto(nombre), "wb") as f:
            f.write(contenido)
        nombres.append(nombre)
    return nombres


def _borrar_fotos(nombres_json):
    if not nombres_json:
        return
    try:
        nombres = json.loads(nombres_json)
    except (TypeError, ValueError):
        return
    for nombre in nombres:
        try:
            os.remove(ruta_foto(nombre))
        except OSError:
            pass


def fotos_de(embarque):
    if not embarque or not embarque.get("fotos_entrega"):
        return []
    try:
        return json.loads(embarque["fotos_entrega"])
    except (TypeError, ValueError):
        return []


def marcar_entregado(embarque_id, via, ref, firma=None, fotos_bytes=None, lat=None, lon=None, precision=None):
    """via: 'qr' | 'whatsapp'. ref: IP (qr) o numero de telefono (whatsapp).
    firma: PNG en base64 (data URI), solo aplica para 'qr' -- por WhatsApp no hay forma
    de capturar firma, fotos ni GPS, solo texto.
    fotos_bytes: lista de hasta 3 fotos (bytes JPEG), opcional.
    lat/lon/precision: ubicacion del GPS del celular al confirmar, opcional (requiere
    que el navegador haya podido usar navigator.geolocation, lo cual exige HTTPS).
    Idempotente: si ya estaba entregada, no hace nada (ni guarda fotos) y regresa False."""
    nombres_fotos = guardar_fotos(embarque_id, fotos_bytes) if fotos_bytes else []
    with _LOCK, _conn() as con:
        con.execute("""
            UPDATE embarques SET estatus='entregado', fecha_entrega=?,
                entregado_via=?, entregado_ref=?, firma_entrega=?, fotos_entrega=?,
                lat_entrega=?, lon_entrega=?, precision_entrega=?
            WHERE id = ? AND estatus != 'entregado'
        """, (_ahora(), via, ref, firma, json.dumps(nombres_fotos) if nombres_fotos else None,
              lat, lon, precision, embarque_id))
        aplicado = con.total_changes > 0
    if not aplicado:
        # La etiqueta ya estaba entregada: no se debian guardar estas fotos, se descartan.
        for nombre in nombres_fotos:
            try:
                os.remove(ruta_foto(nombre))
            except OSError:
                pass
    return aplicado


def reactivar(embarque_id, admin_user, motivo):
    """Solo tiene efecto si la etiqueta esta 'entregado'. Regresa a 'embarcado'
    (conserva chofer/unidad o paqueteria/guia), limpia los datos de entrega y deja
    registro del motivo en `notas` (no se pisa lo que ya hubiera en notas)."""
    with _LOCK, _conn() as con:
        row = con.execute(
            "SELECT notas, fecha_entrega, fotos_entrega FROM embarques WHERE id = ? AND estatus = 'entregado'",
            (embarque_id,),
        ).fetchone()
        if not row:
            return False
        nota = (
            f"[{_ahora()[:16].replace('T', ' ')}] Reactivada por {admin_user} "
            f"(entrega original: {(row['fecha_entrega'] or '')[:16].replace('T', ' ')}) "
            f"-- motivo: {motivo}"
        )
        notas_nuevas = f"{row['notas']}\n{nota}" if row["notas"] else nota
        _borrar_fotos(row["fotos_entrega"])
        con.execute("""
            UPDATE embarques SET estatus='embarcado', fecha_entrega=NULL,
                entregado_via=NULL, entregado_ref=NULL, firma_entrega=NULL,
                fotos_entrega=NULL, lat_entrega=NULL, lon_entrega=NULL,
                precision_entrega=NULL, notas=?
            WHERE id = ?
        """, (notas_nuevas, embarque_id))
        return True


def buscar_por_folio(empresa_id, folio):
    """La etiqueta mas reciente de esta empresa con ese folio de factura (para el
    comando de WhatsApp 'entregado <folio>')."""
    with _conn() as con:
        row = con.execute("""
            SELECT * FROM embarques WHERE empresa_id = ? AND factura_folio = ?
            ORDER BY id DESC LIMIT 1
        """, (empresa_id, folio)).fetchone()
        return dict(row) if row else None
