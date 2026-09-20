"""
Normalizacion de los campos de texto libre ESTADO/MUNICIPIO de CLIE01
contra el catalogo real de estados y municipios de Mexico (para el mapa
de ventas). Los datos capturados en Aspel varian en mayusculas, acentos
y errores de captura, asi que se hace coincidencia aproximada.
"""
import json
import os
import re
import unicodedata
from difflib import get_close_matches

_GEO_DIR = os.path.join(os.path.dirname(__file__), "static", "vendor", "geo")


def _sin_acentos(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm(s):
    s = _sin_acentos(s or "").upper().strip()
    return re.sub(r"\s+", " ", s)


with open(os.path.join(_GEO_DIR, "estados.json"), encoding="utf-8") as _f:
    _ESTADOS_GEOJSON = json.load(_f)

ESTADOS = [
    {"id": feat["properties"]["id"], "nombre": feat["properties"]["name"]}
    for feat in _ESTADOS_GEOJSON["features"]
]
_NOMBRE_POR_ID = {e["id"]: e["nombre"] for e in ESTADOS}
_ID_POR_NOMBRE_NORM = {_norm(e["nombre"]): e["id"] for e in ESTADOS}

# Alias para variantes/errores de captura frecuentes que no son un simple
# problema de acentos/mayusculas (agregar aqui si aparecen mas casos).
_ALIAS_ESTADO = {
    "CDMX": "MX-CMX",
    "DF": "MX-CMX",
    "D F": "MX-CMX",
    "DISTRITO FEDERAL": "MX-CMX",
    "EDOMEX": "MX-MEX",
    "ESTADO MEXICO": "MX-MEX",
    "HDALGO": "MX-HID",
    "GUADALAJARA": "MX-JAL",  # es municipio, no estado; error de captura comun
}


def normalizar_estado(texto):
    """Retorna {"id":..., "nombre":...} o None si no se reconoce."""
    if not texto:
        return None
    t = _norm(texto)

    eid = _ALIAS_ESTADO.get(t) or _ID_POR_NOMBRE_NORM.get(t)
    if not eid:
        sin_prefijo = re.sub(r"^ESTADO DE ", "", t)
        eid = _ID_POR_NOMBRE_NORM.get(sin_prefijo)
    if not eid:
        candidatos = get_close_matches(t, _ID_POR_NOMBRE_NORM.keys(), n=1, cutoff=0.82)
        if candidatos:
            eid = _ID_POR_NOMBRE_NORM[candidatos[0]]

    return {"id": eid, "nombre": _NOMBRE_POR_ID[eid]} if eid else None


_cache_municipios = {}


def _municipios_de(estado_id):
    if estado_id in _cache_municipios:
        return _cache_municipios[estado_id]
    nombres = []
    try:
        with open(os.path.join(_GEO_DIR, "municipios", f"{estado_id}.json"), encoding="utf-8") as f:
            data = json.load(f)
        vistos = set()
        for feat in data["features"]:
            nombre = feat["properties"].get("NAME_2")
            if nombre and nombre not in vistos:
                vistos.add(nombre)
                nombres.append(nombre)
    except (OSError, KeyError, json.JSONDecodeError):
        pass
    _cache_municipios[estado_id] = nombres
    return nombres


def normalizar_municipio(estado_id, texto):
    """Retorna el nombre oficial del municipio dentro de ese estado, o None."""
    if not texto or not estado_id:
        return None
    por_norm = {_norm(n): n for n in _municipios_de(estado_id)}
    if not por_norm:
        return None

    t = _norm(texto)
    if t in por_norm:
        return por_norm[t]

    candidatos = get_close_matches(t, por_norm.keys(), n=1, cutoff=0.78)
    if candidatos:
        return por_norm[candidatos[0]]

    # Nombres oficiales largos donde el usuario solo capturo la primera
    # parte (ej. "COACALCO" en vez de "COACALCO DE BERRIOZABAL").
    prefijos = [norm for norm in por_norm if norm.startswith(t) or t.startswith(norm)]
    if prefijos:
        mas_corto = min(prefijos, key=len)
        return por_norm[mas_corto]

    return None
