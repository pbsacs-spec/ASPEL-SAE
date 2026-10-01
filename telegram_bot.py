"""
Integracion Telegram (PRUEBA) para Aspel SAE -- uso interno.

Reutiliza el procesador de comandos de whatsapp.py (_procesar y las
funciones de generacion de archivos) para no duplicar logica de negocio;
solo cambia el transporte. A diferencia del bot de WhatsApp (que necesita
un navegador Chromium controlado por Node.js, ver wa_service/), Telegram
tiene una API HTTP oficial: se usa long polling (getUpdates), que solo
necesita salida a internet -- no requiere IP publica ni certificado, por
eso es mas simple de correr en esta LAN.

Tambien evita el bug que tuvimos con WhatsApp (whatsapp-web.js no puede
mandar archivos a contactos "@lid"): aqui pdf/excel se mandan como
documento real via sendDocument, sin necesidad del workaround de link de
descarga.

── Como quitar esta integracion por completo ──────────────────────────
  1. Borrar este archivo y templates/telegram_config.html
  2. En app.py: quitar el import, el register_blueprint(telegram_bp) y la
     llamada a iniciar_telegram_en_hilo()
  3. Borrar telegram_config.ini (tiene el token, no esta en git)
  4. Quitar la linea "telegram_config.ini" de .gitignore
No toca whatsapp.py ni ninguna otra parte del sistema -- es un modulo
aparte que solo LEE funciones de whatsapp.py, nunca lo modifica.
También existe un tag de git "pre-telegram-integration" con el estado
exacto de antes de esto, por si se prefiere revertir con git en vez de
borrar archivos a mano.
"""
import base64
import configparser
import os
import threading
import time

import requests
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import embarques_db
from auth import require_admin
from db import load_empresas
from whatsapp import _generar_archivo_busqueda, _generar_archivo_producto, _procesar

telegram_bp = Blueprint("telegram", __name__)
CFG_FILE = os.path.join(os.path.dirname(__file__), "telegram_config.ini")
API_URL = "https://api.telegram.org/bot{token}/{method}"

_ESTADO = {"activo": False, "error": None, "bot_username": None}


# ── Config ────────────────────────────────────────────────────────

def _load():
    cfg = configparser.ConfigParser()
    cfg.read(CFG_FILE, encoding="utf-8")
    return {"bot_token": cfg.get("telegram", "bot_token", fallback="")}


def _save(bot_token):
    cfg = configparser.ConfigParser()
    cfg["telegram"] = {"bot_token": bot_token}
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


# ── Llamadas al Bot API ─────────────────────────────────────────────

def _api(token, method, **params):
    r = requests.post(API_URL.format(token=token, method=method), json=params, timeout=20)
    return r.json()


def _enviar_documento(token, chat_id, contenido, filename, mimetype):
    url = API_URL.format(token=token, method="sendDocument")
    files = {"document": (filename, contenido, mimetype)}
    r = requests.post(url, data={"chat_id": chat_id}, files=files, timeout=30)
    return r.json()


def _descargar_foto(token, file_id):
    """Telegram no manda el archivo directo en el mensaje, solo un file_id: hay
    que pedir su ruta (getFile) y luego descargarlo de un dominio aparte
    (api.telegram.org/file/...), ambos pasos usando el mismo token."""
    info = _api(token, "getFile", file_id=file_id)
    if not info.get("ok"):
        return None
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    r = requests.get(url, timeout=30)
    return r.content if r.ok else None


# ── Fotos y ubicacion de evidencia (sin firma: Telegram no tiene lienzo
# para dibujar) -- alternativa a lo que captura el QR. No depende de haber
# escrito antes "entregado": se puede mandar esta evidencia antes o
# despues de confirmar la entrega.

def _buscar_por_folio_default(folio_digits):
    _, settings = load_empresas()
    return embarques_db.buscar_por_folio(settings["default"], int(folio_digits))


# chat_id -> (embarque_id, folio_txt, timestamp). En memoria: recuerda a que
# factura se refirio el ultimo "entregado FOLIO"/"ubicacion FOLIO"/foto con
# folio de ese chat, para poder asociar un mensaje de ubicacion -- Telegram
# no permite mandar un "caption" junto con la ubicacion como si fuera foto.
_PENDIENTE_UBICACION = {}
_VENTANA_UBICACION_SEG = 600  # 10 minutos


def _marcar_pendiente_ubicacion(chat_id, embarque_id, folio_txt):
    _PENDIENTE_UBICACION[chat_id] = (embarque_id, folio_txt, time.time())


def _tomar_pendiente_ubicacion(chat_id):
    """La consume (la borra) al leerla: cada ubicacion que se comparte requiere
    haber indicado el folio justo antes (via 'ubicacion FOLIO', 'entregado FOLIO'
    o una foto con folio), para no asociar por error una ubicacion mas nueva a
    una factura ya vieja de ese chat."""
    info = _PENDIENTE_UBICACION.pop(chat_id, None)
    if not info:
        return None
    embarque_id, folio_txt, ts = info
    if time.time() - ts > _VENTANA_UBICACION_SEG:
        return None
    return embarque_id, folio_txt


def _procesar_foto(token, chat_id, msg):
    caption = (msg.get("caption") or "").strip()
    folio_digits = "".join(c for c in caption if c.isdigit())
    if not folio_digits:
        _api(token, "sendMessage", chat_id=chat_id,
             text="Para guardar la foto como evidencia de entrega, mandala otra vez "
                  "con el folio de la factura como descripcion (ej. 2259).")
        return

    e = _buscar_por_folio_default(folio_digits)
    if not e:
        _api(token, "sendMessage", chat_id=chat_id,
             text=f"No encontre ninguna etiqueta de embarque con folio {folio_digits}.")
        return

    file_id = msg["photo"][-1]["file_id"]
    contenido = _descargar_foto(token, file_id)
    if contenido is None:
        _api(token, "sendMessage", chat_id=chat_id, text="No pude descargar la foto, intenta de nuevo.")
        return

    guardadas, total = embarques_db.agregar_fotos(e["id"], [contenido])
    folio_txt = f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}"
    _marcar_pendiente_ubicacion(chat_id, e["id"], folio_txt)
    if guardadas == 0:
        _api(token, "sendMessage", chat_id=chat_id,
             text=f"Ya hay {total} fotos guardadas para la factura {folio_txt} (maximo 3), no se agrego esta.")
        return

    aviso_pendiente = ""
    if e["estatus"] != "entregado":
        aviso_pendiente = f"\nRecuerda confirmar la entrega con: entregado {folio_digits}"
    _api(token, "sendMessage", chat_id=chat_id,
         text=f"Foto guardada como evidencia de la factura {folio_txt} ({total}/3).{aviso_pendiente}")


def _procesar_ubicacion_pedida(token, chat_id, arg):
    """Comando 'ubicacion FOLIO' / 'gps FOLIO': solo fija a que factura se
    referira la siguiente ubicacion que comparta, sin marcar nada entregado."""
    folio_digits = "".join(c for c in (arg or "").strip() if c.isdigit())
    if not folio_digits:
        _api(token, "sendMessage", chat_id=chat_id,
             text="Indica el folio de la factura.\nEjemplo: ubicacion 2259")
        return
    e = _buscar_por_folio_default(folio_digits)
    if not e:
        _api(token, "sendMessage", chat_id=chat_id,
             text=f"No encontre ninguna etiqueta de embarque con folio {folio_digits}.")
        return
    folio_txt = f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}"
    _marcar_pendiente_ubicacion(chat_id, e["id"], folio_txt)
    _api(token, "sendMessage", chat_id=chat_id,
         text=f"Listo. Ahora comparte tu ubicacion (icono de clip -> Ubicacion) "
              f"y se guardara para la factura {folio_txt}.")


def _procesar_ubicacion(token, chat_id, msg):
    loc = msg["location"]
    pendiente = _tomar_pendiente_ubicacion(chat_id)
    if not pendiente:
        _api(token, "sendMessage", chat_id=chat_id,
             text="Antes de compartir tu ubicacion, dime a que factura corresponde: "
                  "escribe 'ubicacion FOLIO' (o 'entregado FOLIO') y despues comparte tu ubicacion.")
        return
    embarque_id, folio_txt = pendiente
    ok = embarques_db.guardar_gps(embarque_id, loc["latitude"], loc["longitude"], loc.get("horizontal_accuracy"))
    if ok:
        _api(token, "sendMessage", chat_id=chat_id, text=f"Ubicacion guardada para la factura {folio_txt}.")
    else:
        _api(token, "sendMessage", chat_id=chat_id, text="No pude guardar la ubicacion, intenta de nuevo.")


# ── Procesar un mensaje entrante ────────────────────────────────────

def _procesar_y_responder(token, chat_id, texto, remitente=None):
    resultado = _procesar(texto, remitente=remitente, canal="telegram")

    if isinstance(resultado, dict) and resultado.get("_archivo"):
        fmt        = resultado["fmt"]
        tipo       = resultado["tipo"]
        arg        = resultado["arg"]
        empresa_id = resultado.get("empresa_id")
        con_costo  = resultado.get("con_costo", False)
        if tipo == "buscar":
            b64, filename, err = _generar_archivo_busqueda(fmt, arg, empresa_id)
        else:
            b64, filename, err = _generar_archivo_producto(fmt, arg, empresa_id, con_costo)
        if err:
            _api(token, "sendMessage", chat_id=chat_id, text=err)
            return
        mimetype = (
            "application/pdf" if fmt == "pdf"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        _enviar_documento(token, chat_id, base64.b64decode(b64), filename, mimetype)
        return

    # Telegram usa Markdown distinto a WhatsApp (_*texto*_ vs *texto*); se
    # manda como texto plano para no arriesgar errores de parseo con
    # descripciones de productos que traigan caracteres especiales.
    _api(token, "sendMessage", chat_id=chat_id, text=resultado or "Sin respuesta.")


# ── Polling (long polling, sin necesidad de webhook/IP publica) ────

def _polling_loop(token):
    offset = None
    _ESTADO["activo"] = True
    _ESTADO["error"] = None
    try:
        me = requests.get(API_URL.format(token=token, method="getMe"), timeout=10).json()
        if me.get("ok"):
            _ESTADO["bot_username"] = me["result"].get("username")
        else:
            _ESTADO["activo"] = False
            _ESTADO["error"] = me.get("description", "Token invalido")
            return
    except Exception as e:
        _ESTADO["activo"] = False
        _ESTADO["error"] = str(e)
        return

    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(API_URL.format(token=token, method="getUpdates"), params=params, timeout=35)
            data = r.json()
            if not data.get("ok"):
                _ESTADO["error"] = data.get("description", "Error desconocido")
                time.sleep(5)
                continue
            _ESTADO["error"] = None
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg or not any(k in msg for k in ("text", "photo", "location")):
                    continue
                chat_id = msg["chat"]["id"]
                from_user = msg.get("from") or {}
                username = from_user.get("username")
                nombre = from_user.get("first_name") or ""
                remitente = f"@{username}" if username else (nombre or str(chat_id))
                try:
                    if "photo" in msg:
                        _procesar_foto(token, chat_id, msg)
                    elif "location" in msg:
                        _procesar_ubicacion(token, chat_id, msg)
                    else:
                        texto = msg["text"].strip()
                        partes = texto.split(None, 1)
                        cmd = partes[0].lower() if partes else ""
                        arg = partes[1] if len(partes) > 1 else ""
                        if cmd in ("ubicacion", "ubicación", "gps"):
                            _procesar_ubicacion_pedida(token, chat_id, arg)
                        else:
                            _procesar_y_responder(token, chat_id, texto, remitente)
                            if cmd in ("entregado", "entrega", "entregue"):
                                folio_digits = "".join(c for c in arg if c.isdigit())
                                e = _buscar_por_folio_default(folio_digits) if folio_digits else None
                                if e:
                                    folio_txt = f"{e['factura_serie'] or ''}{e['factura_folio'] or ''}"
                                    _marcar_pendiente_ubicacion(chat_id, e["id"], folio_txt)
                except Exception as e:
                    try:
                        _api(token, "sendMessage", chat_id=chat_id, text=f"Error al consultar: {e}")
                    except Exception:
                        pass
        except requests.exceptions.RequestException as e:
            _ESTADO["error"] = str(e)
            time.sleep(5)
        except Exception as e:
            _ESTADO["error"] = str(e)
            time.sleep(5)


def iniciar_telegram_en_hilo():
    """Se llama una vez al arrancar la app (ver app.py). No hace nada si no
    hay token configurado todavia -- entonces solo queda pendiente hasta
    que se configure en /admin/telegram y se reinicie el servicio."""
    token = _load()["bot_token"].strip()
    if not token:
        return
    threading.Thread(target=_polling_loop, args=(token,), daemon=True, name="telegram-polling").start()
    print("Bot de Telegram iniciado (polling)", flush=True)


# ── Administracion ───────────────────────────────────────────────────

@telegram_bp.route("/admin/telegram", methods=["GET"])
@require_admin
def config_pagina():
    cfg = _load()
    return render_template("telegram_config.html", cfg=cfg, configurado=bool(cfg["bot_token"]))


@telegram_bp.route("/admin/telegram", methods=["POST"])
@require_admin
def config_guardar():
    _save(request.form.get("bot_token", "").strip())
    flash("Token guardado. Reinicia el servicio (AspelInventario-Flask) para que tome efecto.", "ok")
    return redirect(url_for("telegram.config_pagina"))


@telegram_bp.route("/admin/telegram/status")
@require_admin
def status():
    return jsonify(_ESTADO)
