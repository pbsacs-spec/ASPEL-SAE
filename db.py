import configparser
import os
import threading
import fdb

_CFG_FILE        = os.path.join(os.path.dirname(__file__), "db_config.ini")
_DEFAULT_DB_PATH = r"D:\2-Dacaspel\Sistemas Aspel\SAE9.00\Empresa01\Datos\SAE90EMPRE01.FDB"
_DEFAULT_FB_LIB  = r"C:\Program Files\Firebird\Firebird_2_5\bin\fbclient.dll"

# Protege el ciclo leer->modificar->escribir de db_config.ini: sin esto, dos
# requests casi simultaneas (p.ej. dos admins guardando empresas distintas)
# pueden pisarse el cambio una a la otra.
_LOCK = threading.RLock()


def config_lock():
    """Lock a usar por quien haga load_empresas() + ... + save_empresas()
    fuera de este modulo, para que todo el ciclo quede serializado."""
    return _LOCK


def load_empresas():
    """Returns (empresas_dict, settings_dict).
    empresas_dict: { 'empresa_1': {'nombre': ..., 'db_path': ...}, ... }
    settings_dict: { 'fb_lib': ..., 'default': 'empresa_1' }
    """
    cfg = configparser.ConfigParser()
    cfg.read(_CFG_FILE, encoding="utf-8")

    # Migrate old [database] single-company format transparently
    if cfg.has_section("database") and not any(s.startswith("empresa_") for s in cfg.sections()):
        db_path = cfg.get("database", "db_path", fallback=_DEFAULT_DB_PATH)
        fb_lib  = cfg.get("database", "fb_lib",  fallback=_DEFAULT_FB_LIB)
        return (
            {"empresa_1": {"nombre": "Empresa 1", "db_path": db_path}},
            {"fb_lib": fb_lib, "default": "empresa_1"},
        )

    empresas = {
        sec: {
            "nombre":  cfg.get(sec, "nombre",  fallback=sec),
            "db_path": cfg.get(sec, "db_path", fallback=""),
        }
        for sec in cfg.sections()
        if sec.startswith("empresa_")
    }

    if not empresas:
        empresas = {"empresa_1": {"nombre": "Empresa 1", "db_path": _DEFAULT_DB_PATH}}

    settings = {
        "fb_lib":  cfg.get("settings", "fb_lib",  fallback=_DEFAULT_FB_LIB),
        "default": cfg.get("settings", "default", fallback=next(iter(empresas))),
    }
    if settings["default"] not in empresas:
        settings["default"] = next(iter(empresas))

    return empresas, settings


def save_empresas(empresas, settings):
    cfg = configparser.ConfigParser()
    cfg["settings"] = {
        "fb_lib":  settings.get("fb_lib",  _DEFAULT_FB_LIB),
        "default": settings.get("default", next(iter(empresas), "empresa_1")),
    }
    for eid, edata in empresas.items():
        cfg[eid] = {
            "nombre":  edata.get("nombre",  eid),
            "db_path": edata.get("db_path", ""),
        }
    with _LOCK:
        tmp = _CFG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            cfg.write(f)
        os.replace(tmp, _CFG_FILE)


def load_config():
    """Config del default empresa (compatibilidad con código existente)."""
    empresas, settings = load_empresas()
    emp = empresas.get(settings["default"], next(iter(empresas.values())))
    return {"db_path": emp["db_path"], "fb_lib": settings["fb_lib"]}


def save_config(db_path, fb_lib):
    """Actualiza db_path del default y fb_lib global (compatibilidad)."""
    empresas, settings = load_empresas()
    def_id = settings["default"]
    if def_id in empresas:
        empresas[def_id]["db_path"] = db_path
    settings["fb_lib"] = fb_lib
    save_empresas(empresas, settings)


# Cargar fbclient.dll al arrancar (fdb solo lo carga una vez)
fdb.load_api(load_config()["fb_lib"])


def get_connection(empresa_id=None):
    empresas, settings = load_empresas()
    if empresa_id not in empresas:
        empresa_id = settings["default"]
    emp = empresas[empresa_id]
    return fdb.connect(
        database=emp["db_path"],
        user="SYSDBA",
        password="masterkey",
        charset="WIN1252",
    )


def query(sql, params=(), empresa_id=None):
    con = get_connection(empresa_id)
    cur = None
    try:
        cur = con.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass


def get_almacenes(empresa_id=None):
    _, rows = query(
        "SELECT CVE_ALM, DESCR FROM ALMACENES01 WHERE STATUS = 'A' ORDER BY CVE_ALM",
        empresa_id=empresa_id,
    )
    return [{"cve": r[0], "descr": r[1]} for r in rows]


def existencias_producto(clave, empresa_id=None):
    if not clave or len(clave) > 16:
        return None
    cols, rows = query("""
        SELECT i.CVE_ART, i.DESCR, i.LIN_PROD, i.UNI_MED,
               i.EXIST AS EXIST_TOTAL, i.STOCK_MIN, i.COSTO_PROM, i.ULT_COSTO,
               MAX(CASE WHEN p.CVE_PRECIO=1 THEN p.PRECIO END) AS PREC1,
               MAX(CASE WHEN p.CVE_PRECIO=2 THEN p.PRECIO END) AS PREC2,
               MAX(CASE WHEN p.CVE_PRECIO=3 THEN p.PRECIO END) AS PREC3,
               i.STATUS
        FROM INVE01 i
        LEFT JOIN PRECIO_X_PROD01 p ON p.CVE_ART = i.CVE_ART
        WHERE i.CVE_ART = ?
        GROUP BY i.CVE_ART, i.DESCR, i.LIN_PROD, i.UNI_MED,
                 i.EXIST, i.STOCK_MIN, i.COSTO_PROM, i.ULT_COSTO, i.STATUS
    """, [clave], empresa_id=empresa_id)

    if not rows:
        return None

    prod = dict(zip(cols, rows[0]))

    _, alm_rows = query("""
        SELECT a.CVE_ALM, a.DESCR, COALESCE(m.EXISTENCIA, 0)
        FROM ALMACENES01 a
        LEFT JOIN MINVE01 m ON m.ALMACEN = a.CVE_ALM
            AND m.CVE_ART = ?
            AND m.NUM_MOV = (
                SELECT MAX(m2.NUM_MOV) FROM MINVE01 m2
                WHERE m2.CVE_ART = m.CVE_ART AND m2.ALMACEN = m.ALMACEN
            )
        WHERE a.STATUS = 'A'
        ORDER BY a.CVE_ALM
    """, [clave], empresa_id=empresa_id)

    prod["almacenes"] = [
        {"cve": r[0], "descr": r[1], "existencia": float(r[2] or 0)}
        for r in alm_rows
    ]
    return prod


def buscar_productos(texto, limite=8, empresa_id=None):
    cols, rows = query(f"""
        SELECT FIRST {int(limite)} i.CVE_ART, i.DESCR, i.EXIST
        FROM INVE01 i
        WHERE UPPER(i.CVE_ART) CONTAINING UPPER(?)
           OR UPPER(i.DESCR) CONTAINING UPPER(?)
        ORDER BY i.CVE_ART
    """, [texto, texto], empresa_id=empresa_id)
    return [dict(zip(cols, r)) for r in rows]
