from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, g
from db import load_empresas, save_empresas, config_lock, _detectar_sufijo
from auth import require_admin, list_users, set_user, delete_user, ROLES

db_admin_bp = Blueprint("db_admin", __name__)
db_admin_bp.before_request(require_admin(lambda: None))


def _siguiente_id(empresas):
    nums = []
    for k in empresas:
        if k.startswith("empresa_"):
            try:
                nums.append(int(k.split("_", 1)[1]))
            except ValueError:
                pass
    n = max(nums) + 1 if nums else 1
    while f"empresa_{n}" in empresas:
        n += 1
    return f"empresa_{n}"


@db_admin_bp.route("/admin/database", methods=["GET"])
def config_pagina():
    empresas, settings = load_empresas()
    return render_template("db_config.html", empresas=empresas, settings=settings)


@db_admin_bp.route("/admin/database/guardar", methods=["POST"])
def config_guardar():
    empresa_id = request.form.get("empresa_id", "").strip()
    nombre     = request.form.get("nombre",     "").strip()
    db_path    = request.form.get("db_path",    "").strip()

    if not nombre or not db_path:
        flash("El nombre y la ruta de BD son obligatorios.", "err")
        return redirect(url_for("db_admin.config_pagina"))

    with config_lock():
        empresas, settings = load_empresas()

        if not empresa_id or empresa_id not in empresas:
            empresa_id = _siguiente_id(empresas)

        empresas[empresa_id] = {"nombre": nombre, "db_path": db_path}
        save_empresas(empresas, settings)
    flash(f"Empresa \"{nombre}\" guardada.", "ok")
    return redirect(url_for("db_admin.config_pagina"))


@db_admin_bp.route("/admin/database/eliminar", methods=["POST"])
def config_eliminar():
    empresa_id = request.form.get("empresa_id", "").strip()
    with config_lock():
        empresas, settings = load_empresas()

        if empresa_id not in empresas:
            flash("Empresa no encontrada.", "err")
            return redirect(url_for("db_admin.config_pagina"))

        if len(empresas) <= 1:
            flash("Debe haber al menos una empresa registrada.", "err")
            return redirect(url_for("db_admin.config_pagina"))

        nombre = empresas[empresa_id]["nombre"]
        del empresas[empresa_id]

        if settings["default"] == empresa_id:
            settings["default"] = next(iter(empresas))

        save_empresas(empresas, settings)
    flash(f"Empresa \"{nombre}\" eliminada.", "ok")
    return redirect(url_for("db_admin.config_pagina"))


@db_admin_bp.route("/admin/database/predeterminar", methods=["POST"])
def config_predeterminar():
    empresa_id = request.form.get("empresa_id", "").strip()
    with config_lock():
        empresas, settings = load_empresas()

        if empresa_id not in empresas:
            flash("Empresa no encontrada.", "err")
            return redirect(url_for("db_admin.config_pagina"))

        settings["default"] = empresa_id
        save_empresas(empresas, settings)
        nombre = empresas[empresa_id]["nombre"]
    flash(f"\"{nombre}\" establecida como empresa predeterminada.", "ok")
    return redirect(url_for("db_admin.config_pagina"))


@db_admin_bp.route("/admin/database/fb_lib", methods=["POST"])
def config_fb_lib():
    fb_lib = request.form.get("fb_lib", "").strip()
    if not fb_lib:
        flash("Indica la ruta de fbclient.dll.", "err")
        return redirect(url_for("db_admin.config_pagina"))
    with config_lock():
        empresas, settings = load_empresas()
        settings["fb_lib"] = fb_lib
        save_empresas(empresas, settings)
    flash("Ruta de Firebird guardada. Reinicia los servicios para que tome efecto.", "ok")
    return redirect(url_for("db_admin.config_pagina"))


@db_admin_bp.route("/admin/database/test", methods=["POST"])
def config_test():
    data    = request.json or {}
    db_path = data.get("db_path", "").strip()
    try:
        resultado = _probar_conexion(db_path)
        if resultado["ok"]:
            return jsonify({"ok": True, "mensaje": resultado["mensaje"]})
        return jsonify({"ok": False, "error": resultado["error"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@db_admin_bp.route("/admin/usuarios", methods=["GET"])
def usuarios_pagina():
    return render_template("usuarios.html", usuarios=list_users(), roles=ROLES)


@db_admin_bp.route("/admin/usuarios/guardar", methods=["POST"])
def usuarios_guardar():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role     = request.form.get("role", "").strip()

    ok, error = set_user(username, password, role)
    if ok:
        flash(f"Cuenta \"{username}\" guardada.", "ok")
    else:
        flash(error, "err")
    return redirect(url_for("db_admin.usuarios_pagina"))


@db_admin_bp.route("/admin/usuarios/eliminar", methods=["POST"])
def usuarios_eliminar():
    username = request.form.get("username", "").strip()
    if username == g.username:
        flash("No puedes eliminar la cuenta con la que iniciaste sesion.", "err")
        return redirect(url_for("db_admin.usuarios_pagina"))

    ok, error = delete_user(username)
    if ok:
        flash(f"Cuenta \"{username}\" eliminada.", "ok")
    else:
        flash(error, "err")
    return redirect(url_for("db_admin.usuarios_pagina"))


def _probar_conexion(db_path):
    try:
        import fdb
        con = fdb.connect(
            database=db_path,
            user="SYSDBA",
            password="masterkey",
            charset="WIN1252",
        )
        sufijo = _detectar_sufijo(con)
        cur = con.cursor()
        cur.execute(f"SELECT COUNT(*) FROM INVE{sufijo}")
        total = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM ALMACENES{sufijo} WHERE STATUS = 'A'")
        alm = cur.fetchone()[0]
        cur.close()
        con.close()
        return {"ok": True, "mensaje": f"{total:,} productos  |  {alm} almacenes activos"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
