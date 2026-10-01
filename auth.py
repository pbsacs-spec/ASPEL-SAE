"""
Autenticacion HTTP Basic con cuentas gestionables desde /admin/usuarios.
Cada usuario tiene un rol:
  - "vendedores"      -> solo consulta, sin costo
  - "administradores" -> consulta con costo
  - "admin"           -> acceso total, incluye /admin/database, /admin/whatsapp
                         y /admin/usuarios
Los endpoints usados por el bot de WhatsApp NO pasan por aqui (se filtran
por numero de telefono, ver whatsapp.py).
"""
import configparser
import functools
import os
import secrets
import threading
import time

from flask import request, Response, g
from werkzeug.security import generate_password_hash, check_password_hash

_CFG_FILE = os.path.join(os.path.dirname(__file__), "auth_config.ini")

# Protege el ciclo leer->modificar->escribir de auth_config.ini: sin esto,
# dos admins guardando/borrando cuentas casi al mismo tiempo pueden
# pisarse el cambio uno al otro.
_LOCK = threading.RLock()

ROLES = ["vendedores", "administradores", "admin"]
_SECTION_PREFIX = "user:"

_DEFAULTS_MIGRACION = {
    "admin":           "admin",
    "administradores": "administrador",
    "vendedores":      "vendedor",
}


def _leer_cfg():
    cfg = configparser.ConfigParser()
    if os.path.exists(_CFG_FILE):
        cfg.read(_CFG_FILE, encoding="utf-8")
    return cfg


def _guardar_cfg(cfg):
    tmp = _CFG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        cfg.write(f)
    os.replace(tmp, _CFG_FILE)


def _migrar_formato_viejo(cfg):
    """Convierte el formato anterior (una cuenta fija por rol) al nuevo
    (multiples cuentas con seccion 'user:<username>'), preservando las
    contrasenas ya emitidas."""
    cambiado = False
    for role, username_default in _DEFAULTS_MIGRACION.items():
        if cfg.has_section(role):
            username = cfg.get(role, "username", fallback=username_default)
            pwd_hash = cfg.get(role, "password_hash", fallback="")
            if pwd_hash and not cfg.has_section(_SECTION_PREFIX + username):
                cfg[_SECTION_PREFIX + username] = {"password_hash": pwd_hash, "role": role}
            cfg.remove_section(role)
            cambiado = True
    if cambiado:
        _guardar_cfg(cfg)


def _generar_admin_inicial(cfg):
    """No hay ninguna cuenta: crea un admin con contrasena aleatoria (se imprime UNA vez)."""
    password = secrets.token_urlsafe(12)
    username = "admin"
    cfg[_SECTION_PREFIX + username] = {
        "password_hash": generate_password_hash(password),
        "role":          "admin",
    }
    _guardar_cfg(cfg)
    aviso = (
        "\n"
        "==================================================================\n"
        "  Se genero la cuenta administradora inicial\n"
        f"    Usuario:    {username}\n"
        f"    Contrasena: {password}\n"
        "  Guardala: no se volvera a mostrar. Crea mas cuentas desde\n"
        "  /admin/usuarios una vez dentro.\n"
        "==================================================================\n"
    )
    print(aviso, flush=True)


def _cargar_usuarios():
    with _LOCK:
        cfg = _leer_cfg()
        _migrar_formato_viejo(cfg)
        usuarios = {
            sec[len(_SECTION_PREFIX):]: {
                "password_hash": cfg.get(sec, "password_hash", fallback=""),
                "role":          cfg.get(sec, "role", fallback=""),
            }
            for sec in cfg.sections()
            if sec.startswith(_SECTION_PREFIX)
        }
        if not usuarios:
            _generar_admin_inicial(cfg)
            return _cargar_usuarios()
        return usuarios


def list_users():
    """[(username, role), ...] ordenado por username, para la pagina de admin."""
    return sorted(_cargar_usuarios().items())


def set_user(username, password, role):
    """Crea o actualiza (upsert) un usuario. Retorna (ok, error)."""
    username = (username or "").strip()
    if not username:
        return False, "El usuario no puede estar vacio."
    if role not in ROLES:
        return False, "Rol invalido."
    if not password:
        return False, "La contrasena no puede estar vacia."
    if len(password) < 6:
        return False, "La contrasena debe tener al menos 6 caracteres."

    with _LOCK:
        cfg = _leer_cfg()
        _migrar_formato_viejo(cfg)
        cfg[_SECTION_PREFIX + username] = {
            "password_hash": generate_password_hash(password),
            "role":          role,
        }
        _guardar_cfg(cfg)
    return True, None


def delete_user(username):
    """Elimina un usuario. Retorna (ok, error). Evita quedarte sin ningun admin."""
    with _LOCK:
        usuarios = _cargar_usuarios()
        if username not in usuarios:
            return False, "Usuario no encontrado."

        otros_admin = [u for u, d in usuarios.items() if d["role"] == "admin" and u != username]
        if usuarios[username]["role"] == "admin" and not otros_admin:
            return False, "No puedes eliminar la unica cuenta con rol admin."

        cfg = _leer_cfg()
        _migrar_formato_viejo(cfg)
        cfg.remove_section(_SECTION_PREFIX + username)
        _guardar_cfg(cfg)
        return True, None


def _credenciales_validas(username, password):
    """Retorna el rol si las credenciales son correctas, o None."""
    usuarios = _cargar_usuarios()
    for u, datos in usuarios.items():
        if secrets.compare_digest(u, username or ""):
            if datos["password_hash"] and check_password_hash(datos["password_hash"], password or ""):
                return datos["role"]
            return None
    return None


def _no_autorizado(realm):
    return Response(
        "Acceso restringido.", 401,
        {"WWW-Authenticate": f'Basic realm="{realm}"'},
    )


# ── Limite de intentos fallidos (fuerza bruta) ──────────────────────────
# En memoria (no persiste a disco): alcanza para frenar un ataque
# automatizado contra el login, que es el riesgo real de exponer el sitio
# a internet -- no es un registro de auditoria, se reinicia si se reinicia
# el servicio.
_INTENTOS_LOCK = threading.Lock()
_INTENTOS_FALLIDOS = {}  # ip -> {"fallos": [timestamp, ...], "bloqueado_hasta": timestamp}
_MAX_INTENTOS      = 5
_VENTANA_SEGUNDOS  = 300   # 5 min para acumular los intentos
_BLOQUEO_SEGUNDOS  = 900   # 15 min de bloqueo al superar el limite


def _ip_bloqueada(ip):
    with _INTENTOS_LOCK:
        info = _INTENTOS_FALLIDOS.get(ip)
        return bool(info) and time.time() < info["bloqueado_hasta"]


def _registrar_fallo(ip):
    ahora = time.time()
    with _INTENTOS_LOCK:
        info = _INTENTOS_FALLIDOS.setdefault(ip, {"fallos": [], "bloqueado_hasta": 0})
        info["fallos"] = [t for t in info["fallos"] if ahora - t < _VENTANA_SEGUNDOS]
        info["fallos"].append(ahora)
        if len(info["fallos"]) >= _MAX_INTENTOS:
            info["bloqueado_hasta"] = ahora + _BLOQUEO_SEGUNDOS
            info["fallos"] = []


def _registrar_exito(ip):
    with _INTENTOS_LOCK:
        _INTENTOS_FALLIDOS.pop(ip, None)


def require_roles(*roles, realm="Aspel Inventario"):
    """Exige credenciales validas cuyo rol este entre los dados.
    En la vista, g.role y g.username quedan con los datos autenticados."""
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            ip = request.remote_addr or "desconocida"
            if _ip_bloqueada(ip):
                return Response(
                    "Demasiados intentos fallidos. Intenta de nuevo en unos minutos.", 429,
                )
            auth = request.authorization
            role = _credenciales_validas(auth.username, auth.password) if auth else None
            if role is None or role not in roles:
                if auth is not None:
                    _registrar_fallo(ip)
                return _no_autorizado(realm)
            _registrar_exito(ip)
            g.role = role
            g.username = auth.username
            return view(*args, **kwargs)
        return wrapped
    return decorator


require_admin      = require_roles("admin", realm="Aspel Inventario Admin")
require_dashboard  = require_roles(*ROLES, realm="Aspel Inventario")
