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

        # Catalogo formal de choferes y unidades (antes solo texto libre en `catalogo`).
        chofer_tabla_nueva = not con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='choferes'"
        ).fetchone()
        con.execute("""
            CREATE TABLE IF NOT EXISTS choferes (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre             TEXT NOT NULL,
                estatus            TEXT NOT NULL DEFAULT 'activo',
                telefono           TEXT,
                licencia_numero    TEXT,
                licencia_vigencia  TEXT,
                unidad_default_id  INTEGER
            )
        """)
        unidad_tabla_nueva = not con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='unidades'"
        ).fetchone()
        con.execute("""
            CREATE TABLE IF NOT EXISTS unidades (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                descripcion            TEXT NOT NULL,
                placas                 TEXT,
                estatus                TEXT NOT NULL DEFAULT 'activo',
                tipo                   TEXT,
                capacidad              TEXT,
                verificacion_vigencia  TEXT,
                seguro_vigencia        TEXT
            )
        """)

        cols_emb = [r["name"] for r in con.execute("PRAGMA table_info(embarques)").fetchall()]
        for columna in ("chofer_id", "unidad_id"):
            if columna not in cols_emb:
                con.execute(f"ALTER TABLE embarques ADD COLUMN {columna} INTEGER")

        # Migracion: eleccion del Emisor por etiqueta (cliente facturado, para
        # logistica a nombre de terceros, vs datos fijos de la empresa) -- se
        # decide al crear cada etiqueta, no es una configuracion global.
        if "emisor_cliente" not in cols_emb:
            con.execute("ALTER TABLE embarques ADD COLUMN emisor_cliente INTEGER NOT NULL DEFAULT 0")

        # Migracion: saber si una etiqueta ya se imprimio (bloquea editar el
        # destino) y si esta archivada (oculta de la vista normal, solo para
        # consulta -- ver marcar_impreso/actualizar_destinatario/archivar).
        if "fecha_impresion" not in cols_emb:
            con.execute("ALTER TABLE embarques ADD COLUMN fecha_impresion TEXT")
        if "archivado" not in cols_emb:
            con.execute("ALTER TABLE embarques ADD COLUMN archivado INTEGER NOT NULL DEFAULT 0")

        if chofer_tabla_nueva or unidad_tabla_nueva:
            # Siembra los catalogos con lo que ya se habia aprendido como texto libre,
            # y liga los embarques existentes por coincidencia de nombre (TRIM) para
            # que los reportes tambien sirvan sobre datos historicos.
            for nombre, in con.execute(
                "SELECT DISTINCT valor FROM catalogo WHERE tipo = 'chofer'"
            ).fetchall():
                con.execute("INSERT INTO choferes (nombre) VALUES (?)", (nombre,))
            for descripcion, in con.execute(
                "SELECT DISTINCT valor FROM catalogo WHERE tipo = 'unidad'"
            ).fetchall():
                con.execute("INSERT INTO unidades (descripcion) VALUES (?)", (descripcion,))

            con.execute("""
                UPDATE embarques SET chofer_id = (
                    SELECT c.id FROM choferes c WHERE TRIM(c.nombre) = TRIM(embarques.chofer)
                ) WHERE chofer_id IS NULL AND chofer IS NOT NULL
            """)
            con.execute("""
                UPDATE embarques SET unidad_id = (
                    SELECT u.id FROM unidades u WHERE TRIM(u.descripcion) = TRIM(embarques.unidad)
                ) WHERE unidad_id IS NULL AND unidad IS NOT NULL
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
    # Chofer/unidad ahora se administran en su propio catalogo (choferes_listar()/
    # unidades_listar(), tablas `choferes`/`unidades`); solo paqueteria sigue con
    # autocompletado de texto libre.
    if tipo_embarque == "paqueteria":
        catalogo_agregar("paqueteria", datos.get("paqueteria"))


# ── Catalogo de choferes y unidades ─────────────────────────────────────────
# No se borran registros (igual que Aspel usa STATUS en vez de eliminar filas):
# solo se activan/desactivan, porque etiquetas historicas los siguen referenciando.

def choferes_listar(solo_activos=False):
    with _conn() as con:
        sql = "SELECT * FROM choferes"
        if solo_activos:
            sql += " WHERE estatus = 'activo'"
        sql += " ORDER BY nombre"
        return [dict(r) for r in con.execute(sql).fetchall()]


def chofer_obtener(chofer_id):
    with _conn() as con:
        row = con.execute("SELECT * FROM choferes WHERE id = ?", (chofer_id,)).fetchone()
        return dict(row) if row else None


def chofer_guardar(chofer_id, datos):
    """chofer_id None/0 -> crea uno nuevo; si no, actualiza. Regresa el id."""
    campos = ("nombre", "telefono", "licencia_numero", "licencia_vigencia", "unidad_default_id")
    valores = {c: (datos.get(c) or None) for c in campos}
    valores["unidad_default_id"] = int(valores["unidad_default_id"]) if valores["unidad_default_id"] else None
    with _LOCK, _conn() as con:
        if chofer_id:
            con.execute("""
                UPDATE choferes SET nombre=?, telefono=?, licencia_numero=?,
                    licencia_vigencia=?, unidad_default_id=? WHERE id=?
            """, (*valores.values(), chofer_id))
            return chofer_id
        cur = con.execute("""
            INSERT INTO choferes (nombre, telefono, licencia_numero, licencia_vigencia, unidad_default_id)
            VALUES (?, ?, ?, ?, ?)
        """, tuple(valores.values()))
        return cur.lastrowid


def chofer_cambiar_estatus(chofer_id, estatus):
    with _LOCK, _conn() as con:
        con.execute("UPDATE choferes SET estatus=? WHERE id=?", (estatus, chofer_id))


def chofer_obtener_o_crear(nombre):
    """Para la opcion "+ Nuevo chofer..." en el formulario de embarque: si ya existe
    uno activo con ese nombre lo reutiliza, si no lo crea. Regresa (id, nombre)."""
    nombre = (nombre or "").strip()
    if not nombre:
        return None, None
    with _LOCK, _conn() as con:
        row = con.execute(
            "SELECT id FROM choferes WHERE TRIM(nombre) = ?", (nombre,)
        ).fetchone()
        if row:
            return row["id"], nombre
        cur = con.execute("INSERT INTO choferes (nombre) VALUES (?)", (nombre,))
        return cur.lastrowid, nombre


def unidades_listar(solo_activos=False):
    with _conn() as con:
        sql = "SELECT * FROM unidades"
        if solo_activos:
            sql += " WHERE estatus = 'activo'"
        sql += " ORDER BY descripcion"
        return [dict(r) for r in con.execute(sql).fetchall()]


def unidad_obtener(unidad_id):
    with _conn() as con:
        row = con.execute("SELECT * FROM unidades WHERE id = ?", (unidad_id,)).fetchone()
        return dict(row) if row else None


def unidad_guardar(unidad_id, datos):
    campos = ("descripcion", "placas", "tipo", "capacidad", "verificacion_vigencia", "seguro_vigencia")
    valores = {c: (datos.get(c) or None) for c in campos}
    with _LOCK, _conn() as con:
        if unidad_id:
            con.execute("""
                UPDATE unidades SET descripcion=?, placas=?, tipo=?, capacidad=?,
                    verificacion_vigencia=?, seguro_vigencia=? WHERE id=?
            """, (*valores.values(), unidad_id))
            return unidad_id
        cur = con.execute("""
            INSERT INTO unidades (descripcion, placas, tipo, capacidad, verificacion_vigencia, seguro_vigencia)
            VALUES (?, ?, ?, ?, ?, ?)
        """, tuple(valores.values()))
        return cur.lastrowid


def unidad_cambiar_estatus(unidad_id, estatus):
    with _LOCK, _conn() as con:
        con.execute("UPDATE unidades SET estatus=? WHERE id=?", (estatus, unidad_id))


def unidad_obtener_o_crear(descripcion):
    descripcion = (descripcion or "").strip()
    if not descripcion:
        return None, None
    with _LOCK, _conn() as con:
        row = con.execute(
            "SELECT id FROM unidades WHERE TRIM(descripcion) = ?", (descripcion,)
        ).fetchone()
        if row:
            return row["id"], descripcion
        cur = con.execute("INSERT INTO unidades (descripcion) VALUES (?)", (descripcion,))
        return cur.lastrowid, descripcion


def crear_embarque(empresa_id, factura, destinatario, creado_por, embarque_info=None, emisor_cliente=False):
    """factura: dict con cve_doc, serie, folio, cliente_clave, cliente_nombre.
    destinatario: dict con las claves en _CAMPOS_DEST (sin el prefijo dest_).
    embarque_info: opcional, dict con tipo_embarque/chofer/unidad/paqueteria/guia/
    num_bultos -- si se da, la etiqueta se crea ya como 'embarcado'; si no, queda
    'pendiente' (se puede marcar despues).
    emisor_cliente: si True, la etiqueta/comprobante de esta entrega mostraran
    como Emisor los datos del cliente facturado (logistica a nombre de
    terceros) en vez de los datos de la empresa -- se decide por etiqueta, no
    depende de si ya se lleno tipo_embarque/chofer."""
    info = embarque_info or {}
    tipo = info.get("tipo_embarque")
    chofer_id, unidad_id = info.get("chofer_id"), info.get("unidad_id")
    if tipo == "propio":
        estatus, chofer, unidad, paqueteria, guia = "embarcado", info.get("chofer", ""), info.get("unidad", ""), None, None
        fecha_embarque, embarcado_por = _ahora(), creado_por
    elif tipo == "paqueteria":
        estatus, chofer, unidad, paqueteria, guia = "embarcado", None, None, info.get("paqueteria", ""), info.get("guia", "")
        fecha_embarque, embarcado_por = _ahora(), creado_por
        chofer_id = unidad_id = None
    else:
        estatus, chofer, unidad, paqueteria, guia = "pendiente", None, None, None, None
        fecha_embarque, embarcado_por = None, None
        chofer_id = unidad_id = None
    num_bultos = max(1, min(200, int(info.get("num_bultos") or 1)))
    emisor_cliente = 1 if emisor_cliente else 0
    token_entrega = secrets.token_urlsafe(24)

    with _LOCK, _conn() as con:
        cur = con.execute(f"""
            INSERT INTO embarques (
                empresa_id, factura_cve_doc, factura_serie, factura_folio,
                cliente_clave, cliente_nombre,
                {", ".join(_CAMPOS_DEST)},
                estatus, tipo_embarque, chofer, unidad, chofer_id, unidad_id,
                paqueteria, guia, num_bultos, emisor_cliente,
                fecha_creacion, creado_por, fecha_embarque, embarcado_por, token_entrega
            ) VALUES (?, ?, ?, ?, ?, ?, {", ".join("?" for _ in _CAMPOS_DEST)},
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            empresa_id, factura["cve_doc"], factura.get("serie"), factura.get("folio"),
            factura.get("cliente_clave"), factura.get("cliente_nombre"),
            *[destinatario.get(c[len("dest_"):], "") for c in _CAMPOS_DEST],
            estatus, tipo, chofer, unidad, chofer_id, unidad_id, paqueteria, guia, num_bultos, emisor_cliente,
            _ahora(), creado_por, fecha_embarque, embarcado_por, token_entrega,
        ))
        embarque_id = cur.lastrowid

    if tipo:
        _aprender_catalogo(tipo, info)
    return embarque_id


def listar_embarques(empresa_id, estatus=None, desde=None, hasta=None, archivadas=False, q=None):
    where = ["empresa_id = ?", "archivado = ?"]
    params = [empresa_id, 1 if archivadas else 0]
    if estatus:
        where.append("estatus = ?")
        params.append(estatus)
    if desde:
        where.append("date(fecha_creacion) >= date(?)")
        params.append(desde)
    if hasta:
        where.append("date(fecha_creacion) <= date(?)")
        params.append(hasta)
    if q:
        # Busca por factura (como se ve en pantalla: serie+folio, ej. "FAC2259",
        # o la clave completa de Aspel, ej. "FAC02259") o por nombre de
        # cliente/destinatario -- util sobre todo en Archivadas, para encontrar
        # una entrega vieja sin tener que acotar primero el rango de fechas.
        where.append("(factura_serie || CAST(factura_folio AS TEXT) LIKE ? "
                      "OR factura_cve_doc LIKE ? OR cliente_nombre LIKE ? OR dest_nombre LIKE ?)")
        comodin = f"%{q}%"
        params += [comodin, comodin, comodin, comodin]
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
                    chofer=?, unidad=?, chofer_id=?, unidad_id=?, paqueteria=NULL, guia=NULL, num_bultos=?,
                    fecha_embarque=COALESCE(fecha_embarque, ?),
                    embarcado_por=COALESCE(embarcado_por, ?)
                WHERE id = ? AND estatus != 'entregado'
            """, (datos.get("chofer", ""), datos.get("unidad", ""), datos.get("chofer_id"), datos.get("unidad_id"),
                  num_bultos, _ahora(), usuario, embarque_id))
        else:
            con.execute("""
                UPDATE embarques SET estatus='embarcado', tipo_embarque='paqueteria',
                    chofer=NULL, unidad=NULL, chofer_id=NULL, unidad_id=NULL, paqueteria=?, guia=?, num_bultos=?,
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


def marcar_impreso(embarque_id):
    """Se llama la primera vez que se genera de verdad el PDF de la etiqueta
    (ver embarques.py: etiqueta_pdf). No pisa la fecha si ya se habia
    impreso antes -- queda la fecha de la primera impresion, que es la que
    bloquea poder editar el destino."""
    with _LOCK, _conn() as con:
        con.execute(
            "UPDATE embarques SET fecha_impresion = ? WHERE id = ? AND fecha_impresion IS NULL",
            (_ahora(), embarque_id),
        )


def actualizar_destinatario(embarque_id, destinatario):
    """Sobreescribe los datos de destino. Quien llama (embarques.py) ya debe
    haber verificado que la etiqueta no se ha impreso ni entregado -- aqui
    solo se escribe, sin repetir esa validacion."""
    with _LOCK, _conn() as con:
        con.execute(f"""
            UPDATE embarques SET {", ".join(f"{c} = ?" for c in _CAMPOS_DEST)}
            WHERE id = ?
        """, (*[destinatario.get(c[len("dest_"):], "") for c in _CAMPOS_DEST], embarque_id))


def eliminar_embarque(embarque_id):
    """Borra la etiqueta por completo (fotos de entrega incluidas, si las
    tuviera). Quien llama ya debe haber verificado que no esta entregada --
    una vez entregada no se puede eliminar nunca, solo archivar."""
    with _LOCK, _conn() as con:
        row = con.execute("SELECT fotos_entrega FROM embarques WHERE id = ?", (embarque_id,)).fetchone()
        if row:
            _borrar_fotos(row["fotos_entrega"])
        con.execute("DELETE FROM embarques WHERE id = ?", (embarque_id,))


def archivar(embarque_id, archivado=True):
    """Oculta (o vuelve a mostrar) una etiqueta entregada de la vista normal,
    sin borrarla -- queda disponible indefinidamente en la vista de
    archivadas para consulta (ej. evidencia de entrega ante dudas de un
    cliente)."""
    with _LOCK, _conn() as con:
        con.execute("UPDATE embarques SET archivado = ? WHERE id = ?", (1 if archivado else 0, embarque_id))


def buscar_por_folio(empresa_id, folio):
    """La etiqueta mas reciente de esta empresa con ese folio de factura (para el
    comando de WhatsApp 'entregado <folio>')."""
    with _conn() as con:
        row = con.execute("""
            SELECT * FROM embarques WHERE empresa_id = ? AND factura_folio = ?
            ORDER BY id DESC LIMIT 1
        """, (empresa_id, folio)).fetchone()
        return dict(row) if row else None


# ── Reportes de rutas por chofer ─────────────────────────────────────────────

def reporte_entregas(empresa_id, desde, hasta, chofer_id=None):
    """Manifiesto de entregas confirmadas en el periodo (sobre fecha_entrega).
    chofer_id opcional filtra a un solo chofer."""
    where = [
        "empresa_id = ?", "estatus = 'entregado'", "fecha_entrega IS NOT NULL",
        "date(fecha_entrega) >= date(?)", "date(fecha_entrega) <= date(?)",
    ]
    params = [empresa_id, desde, hasta]
    if chofer_id:
        where.append("chofer_id = ?")
        params.append(chofer_id)
    with _conn() as con:
        rows = con.execute(f"""
            SELECT id, factura_serie, factura_folio, dest_nombre, chofer, unidad,
                   paqueteria, guia, tipo_embarque, fecha_embarque, fecha_entrega,
                   num_bultos, lat_entrega, lon_entrega,
                   (julianday(fecha_entrega) - julianday(fecha_embarque)) * 24.0 AS horas_transcurridas
            FROM embarques
            WHERE {" AND ".join(where)}
            ORDER BY fecha_entrega DESC
        """, params).fetchall()
        return [dict(r) for r in rows]


def reporte_totales_por_chofer(empresa_id, desde, hasta):
    """Entregas de reparto propio del periodo, agrupadas por chofer (usa chofer_id
    cuando existe; si no, cae al texto crudo para etiquetas viejas sin backfill)."""
    with _conn() as con:
        rows = con.execute("""
            SELECT COALESCE(c.nombre, e.chofer, '(sin chofer registrado)') AS chofer,
                   e.chofer_id AS chofer_id,
                   COUNT(*) AS num_entregas,
                   SUM(e.num_bultos) AS total_bultos,
                   AVG((julianday(e.fecha_entrega) - julianday(e.fecha_embarque)) * 24.0) AS horas_promedio
            FROM embarques e
            LEFT JOIN choferes c ON c.id = e.chofer_id
            WHERE e.empresa_id = ? AND e.estatus = 'entregado' AND e.fecha_entrega IS NOT NULL
              AND e.tipo_embarque = 'propio'
              AND date(e.fecha_entrega) >= date(?) AND date(e.fecha_entrega) <= date(?)
            GROUP BY COALESCE(c.id, e.chofer)
            ORDER BY num_entregas DESC
        """, (empresa_id, desde, hasta)).fetchall()
        return [dict(r) for r in rows]
