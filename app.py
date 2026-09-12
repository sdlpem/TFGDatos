from flask import Flask, request, jsonify, send_from_directory, session, redirect
import os
import json
import requests
from datetime import datetime, timezone, timedelta

import psycopg
from psycopg.rows import dict_row

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "clave_secreta_cambiar")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "mi_token_secreto_123")
WA_TOKEN = os.environ.get("WA_TOKEN", "")
WA_PHONE_ID = os.environ.get("WA_PHONE_ID", "")
CECO_USER = os.environ.get("CECO_USER", "ceco")
CECO_PASS = os.environ.get("CECO_PASS", "ceco1234")
RESULTADOS_URL = os.environ.get(
    "RESULTADOS_URL",
    "https://tfgdatos.onrender.com/resultados"
)

DATA_DIR = os.environ.get("DATA_DIR", "/tmp")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ============================================================
# BASE DE DATOS
# ============================================================

def db_conn():
    """Abre una conexión PostgreSQL."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no está configurada en Render")
    # No usamos row_factory aquí: algunas consultas necesitan tuplas.
    # Las consultas que necesitan diccionarios usan cursor(row_factory=dict_row).
    return psycopg.connect(DATABASE_URL)


def init_db():
    """Crea las tablas necesarias si todavía no existen."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS encuestas (
                    id SERIAL PRIMARY KEY,
                    texto TEXT NOT NULL DEFAULT '',
                    tipo VARCHAR(30) NOT NULL DEFAULT 'sino',
                    minimo DOUBLE PRECISION,
                    maximo DOUBLE PRECISION,
                    cierre TIMESTAMP NULL,
                    activa BOOLEAN NOT NULL DEFAULT FALSE,
                    estado VARCHAR(30) NOT NULL DEFAULT 'BORRADOR',
                    fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    fecha_lanzamiento TIMESTAMP NULL,
                    fecha_cierre_real TIMESTAMP NULL,
                    destinatarios INTEGER NOT NULL DEFAULT 0
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS votos (
                    id SERIAL PRIMARY KEY,
                    encuesta_id INTEGER NOT NULL
                        REFERENCES encuestas(id) ON DELETE CASCADE,
                    telefono VARCHAR(30) NOT NULL,
                    respuesta TEXT NOT NULL,
                    fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(encuesta_id, telefono)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS estados_participantes (
                    id SERIAL PRIMARY KEY,
                    encuesta_id INTEGER NOT NULL
                        REFERENCES encuestas(id) ON DELETE CASCADE,
                    telefono VARCHAR(30) NOT NULL,
                    estado VARCHAR(40) NOT NULL,
                    fecha_actualizacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(encuesta_id, telefono)
                )
            """)

        conn.commit()


def _fecha_db(valor):
    """Convierte una fecha ISO recibida del frontend a datetime."""
    if not valor:
        return None

    try:
        s = str(valor).strip()

        # JavaScript puede mandar una Z.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"

        if len(s) == 16:
            s += ":00"

        dt = datetime.fromisoformat(s)

        # Las columnas son TIMESTAMP sin zona.
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

        return dt
    except Exception:
        return None


def _fecha_iso(valor):
    if not valor:
        return None
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%dT%H:%M:%S")
    return str(valor)


def _ahora_local():
    """
    Hora local usada por la aplicación.
    TZ_OFFSET permite mantener el mismo funcionamiento que la versión anterior.
    """
    offset = int(os.environ.get("TZ_OFFSET", "2"))
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=offset)


def _encuesta_dict(row):
    if not row:
        return {
            "id": None,
            "texto": "",
            "tipo": "sino",
            "min": None,
            "max": None,
            "cierre": None,
            "activa": False,
            "estado": "SIN_CONFIGURAR",
            "fecha_creacion": None,
            "fecha_lanzamiento": None,
            "fecha_cierre_real": None,
            "destinatarios": 0,
            "plantilla_sino": os.environ.get(
                "PLANTILLA_SINO", "plantilla_dinamica"
            ),
            "plantilla_abierta": os.environ.get(
                "PLANTILLA_ABIERTA", "plantilla_dinamica"
            )
        }

    return {
        "id": row["id"],
        "texto": row["texto"],
        "tipo": row["tipo"],
        "min": row["minimo"],
        "max": row["maximo"],
        "cierre": _fecha_iso(row["cierre"]),
        "activa": bool(row["activa"]),
        "estado": row["estado"],
        "fecha_creacion": _fecha_iso(row["fecha_creacion"]),
        "fecha_lanzamiento": _fecha_iso(row["fecha_lanzamiento"]),
        "fecha_cierre_real": _fecha_iso(row["fecha_cierre_real"]),
        "destinatarios": row["destinatarios"],
        "plantilla_sino": os.environ.get(
            "PLANTILLA_SINO", "plantilla_dinamica"
        ),
        "plantilla_abierta": os.environ.get(
            "PLANTILLA_ABIERTA", "plantilla_dinamica"
        )
    }


def obtener_ultima_encuesta():
    """Última encuesta creada; es la encuesta de trabajo del panel."""
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM encuestas ORDER BY id DESC LIMIT 1")
            return cur.fetchone()


def obtener_encuesta_activa():
    """Única encuesta que puede recibir respuestas por WhatsApp."""
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT * FROM encuestas
                WHERE activa=TRUE AND estado='ACTIVA'
                ORDER BY id DESC LIMIT 1
            """)
            return cur.fetchone()


def cargar_encuesta_activa():
    return _encuesta_dict(obtener_encuesta_activa())


def obtener_encuesta(id_encuesta):
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM encuestas WHERE id=%s",
                (id_encuesta,)
            )
            return cur.fetchone()


def cargar_encuesta():
    return _encuesta_dict(obtener_ultima_encuesta())


def guardar_nueva_encuesta(d):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO encuestas
                    (texto, tipo, minimo, maximo, cierre, activa, estado)
                VALUES (%s, %s, %s, %s, %s, FALSE, 'BORRADOR')
                RETURNING id
            """, (
                d.get("texto", ""),
                d.get("tipo", "sino"),
                d.get("min"),
                d.get("max"),
                _fecha_db(d.get("cierre"))
            ))
            encuesta_id = cur.fetchone()[0]

        conn.commit()

    return encuesta_id


def actualizar_encuesta(id_encuesta, d):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE encuestas
                SET texto=%s,
                    tipo=%s,
                    minimo=%s,
                    maximo=%s,
                    cierre=%s
                WHERE id=%s
            """, (
                d.get("texto", ""),
                d.get("tipo", "sino"),
                d.get("min"),
                d.get("max"),
                _fecha_db(d.get("cierre")),
                id_encuesta
            ))

        conn.commit()


def estado_encuesta():
    """
    Estado real de la encuesta actual:
    SIN_CONFIGURAR / BORRADOR / PENDIENTE / ACTIVA / CERRADA
    """
    e = cargar_encuesta()

    if not e.get("id") or not e.get("texto"):
        return "SIN_CONFIGURAR"

    if e.get("activa"):
        if e.get("cierre"):
            cierre = _fecha_db(e["cierre"])
            if cierre and _ahora_local() > cierre:
                return "CERRADA"

        return "ACTIVA"

    if e.get("estado") == "BORRADOR":
        if e.get("cierre"):
            cierre = _fecha_db(e["cierre"])
            if cierre and _ahora_local() <= cierre:
                return "PENDIENTE"

        return "BORRADOR"

    return e.get("estado", "BORRADOR")


def encuesta_abierta():
    return estado_encuesta() == "ACTIVA"


# ============================================================
# VOTOS Y ESTADOS
# ============================================================

def guardar_voto(numero, respuesta):
    e = cargar_encuesta_activa()

    if not e.get("id"):
        return

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO votos
                    (encuesta_id, telefono, respuesta)
                VALUES (%s, %s, %s)
                ON CONFLICT (encuesta_id, telefono)
                DO UPDATE SET
                    respuesta=EXCLUDED.respuesta,
                    fecha=CURRENT_TIMESTAMP
            """, (e["id"], numero, respuesta))

            cur.execute("""
                INSERT INTO estados_participantes
                    (encuesta_id, telefono, estado)
                VALUES (%s, %s, 'confirmado')
                ON CONFLICT (encuesta_id, telefono)
                DO UPDATE SET
                    estado='confirmado',
                    fecha_actualizacion=CURRENT_TIMESTAMP
            """, (e["id"], numero))

        conn.commit()

    print(f"✅ Voto: {numero} → {respuesta}")


def guardar_estado_participante(numero, nuevo_estado):
    e = cargar_encuesta_activa()

    if not e.get("id"):
        return

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO estados_participantes
                    (encuesta_id, telefono, estado)
                VALUES (%s, %s, %s)
                ON CONFLICT (encuesta_id, telefono)
                DO UPDATE SET
                    estado=EXCLUDED.estado,
                    fecha_actualizacion=CURRENT_TIMESTAMP
            """, (e["id"], numero, nuevo_estado))

        conn.commit()


def cargar_estado_participante(numero):
    e = cargar_encuesta_activa()

    if not e.get("id"):
        return "esperando_respuesta"

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT estado
                FROM estados_participantes
                WHERE encuesta_id=%s
                  AND telefono=%s
            """, (e["id"], numero))
            row = cur.fetchone()

    return row[0] if row else "esperando_respuesta"


def resumen_votos(id_encuesta=None):
    if id_encuesta is None:
        e = cargar_encuesta()
        id_encuesta = e.get("id")

    if not id_encuesta:
        return {"total": 0, "conteo": {}}

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT respuesta, COUNT(*)
                FROM votos
                WHERE encuesta_id=%s
                GROUP BY respuesta
            """, (id_encuesta,))
            rows = cur.fetchall()

    conteo = {respuesta: cantidad for respuesta, cantidad in rows}

    return {
        "total": sum(conteo.values()),
        "conteo": conteo
    }


def listar_encuestas():
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT
                    e.*,
                    COUNT(v.id)::INTEGER AS respuestas
                FROM encuestas e
                LEFT JOIN votos v
                    ON v.encuesta_id=e.id
                GROUP BY e.id
                ORDER BY e.id DESC
            """)
            return cur.fetchall()


# ============================================================
# MIGRACIÓN ÚNICA DESDE LOS JSON ANTIGUOS
# ============================================================

def migrar_json_si_es_necesario():
    """
    Si la base de datos está vacía y todavía existen los JSON antiguos
    en DATA_DIR, los copia a PostgreSQL una sola vez.
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM encuestas")
            total = cur.fetchone()[0]

    if total > 0:
        return

    encuesta_path = os.path.join(DATA_DIR, "encuesta.json")
    votos_path = os.path.join(DATA_DIR, "votos.json")
    estados_path = os.path.join(DATA_DIR, "estados.json")

    try:
        with open(encuesta_path, encoding="utf-8") as f:
            old_e = json.load(f)
    except Exception:
        return

    encuesta_id = guardar_nueva_encuesta(old_e)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE encuestas
                SET activa=%s,
                    estado=%s,
                    fecha_lanzamiento=%s,
                    destinatarios=%s
                WHERE id=%s
            """, (
                bool(old_e.get("activa")),
                "ACTIVA" if old_e.get("activa") else "BORRADOR",
                datetime.now() if old_e.get("activa") else None,
                int(old_e.get("destinatarios", 0) or 0),
                encuesta_id
            ))

        conn.commit()

    # Migrar votos.
    try:
        with open(votos_path, encoding="utf-8") as f:
            old_votos = json.load(f)

        for numero, voto in old_votos.items():
            guardar_voto(numero, voto.get("respuesta", ""))
    except Exception:
        pass

    # Restaurar estados.
    try:
        with open(estados_path, encoding="utf-8") as f:
            old_estados = json.load(f)

        for numero, estado in old_estados.items():
            guardar_estado_participante(numero, estado)
    except Exception:
        pass

    print("✅ Migración inicial de JSON → PostgreSQL completada")


# ============================================================
# ARRANQUE DE BASE DE DATOS
# ============================================================

try:
    init_db()
    migrar_json_si_es_necesario()
    print("🗄️ PostgreSQL conectado correctamente")
except Exception as ex:
    print(f"❌ Error conectando con PostgreSQL: {ex}")
    raise


# ============================================================
# WHATSAPP — ENVÍO
# ============================================================

def enviar_texto(numero, texto):
    if not WA_TOKEN or not WA_PHONE_ID:
        print(f"⚠️ Sin credenciales WhatsApp: {texto}")
        return

    r = requests.post(
        f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages",
        headers={
            "Authorization": f"Bearer {WA_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "text",
            "text": {"body": texto}
        }
    )

    print(f"📤 Texto {numero}: {r.status_code} {r.text}")


def enviar_plantilla(numero, encuesta):
    if not WA_TOKEN or not WA_PHONE_ID:
        print("⚠️ Sin credenciales WhatsApp para plantilla")
        return

    tipo = encuesta.get("tipo", "sino")

    if tipo == "sino":
        nombre = encuesta.get(
            "plantilla_sino",
            os.environ.get("PLANTILLA_SINO", "plantilla_dinamica")
        )
        formato = "Responda con SÍ o NO"
    else:
        nombre = encuesta.get(
            "plantilla_abierta",
            os.environ.get("PLANTILLA_ABIERTA", "plantilla_dinamica")
        )
        formato = formato_instrucciones(encuesta)

    if tipo == "sino":
        componentes = [{
            "type": "body",
            "parameters": [
                {
                    "type": "text",
                    "parameter_name": "pregunta",
                    "text": encuesta.get("texto", "")
                }
            ]
        }]
    else:
        componentes = [{
            "type": "body",
            "parameters": [
                {
                    "type": "text",
                    "parameter_name": "pregunta",
                    "text": encuesta.get("texto", "")
                },
                {
                    "type": "text",
                    "parameter_name": "formato_respuesta",
                    "text": formato
                }
            ]
        }]

    r = requests.post(
        f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages",
        headers={
            "Authorization": f"Bearer {WA_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "template",
            "template": {
                "name": nombre,
                "language": {"code": "es"},
                "components": componentes
            }
        }
    )

    print(
        f"📤 Plantilla '{nombre}' → {numero}: "
        f"{r.status_code} {r.text}"
    )


def enviar_confirmacion(numero, valor):
    """Mensaje enviado después de registrar un voto."""
    if not WA_TOKEN or not WA_PHONE_ID:
        return

    texto = (
        f"✅ Tu respuesta *{valor}* ha sido registrada.\n\n"
        f"Escribe *CAMBIAR* en cualquier momento para modificarla.\n\n"
        f"📊 Ve cómo están votando los demás:\n{RESULTADOS_URL}"
    )

    enviar_texto(numero, texto)


# ============================================================
# VALIDACIÓN
# ============================================================

def validar(texto, encuesta):
    t = (
        texto.strip()
        .upper()
        .replace("Í", "I")
        .replace("É", "E")
        .replace("Á", "A")
        .replace("Ó", "O")
        .replace("Ú", "U")
    )

    tipo = encuesta.get("tipo", "sino")

    if tipo == "sino":
        if t in ["SI", "S"]:
            return True, "SÍ"

        if t in ["NO", "N"]:
            return True, "NO"

        return False, None

    if tipo == "porcentaje":
        try:
            v = float(
                texto.strip()
                .replace("%", "")
                .replace(",", ".")
            )

            mn = encuesta.get("min")
            mx = encuesta.get("max")

            if mn is None:
                mn = 0

            if mx is None:
                mx = 100

            if mn <= v <= mx:
                return True, f"{v}%"

            return False, None

        except Exception:
            return False, None

    if tipo == "numero":
        try:
            v = float(
                texto.strip().replace(",", ".")
            )

            mn = encuesta.get("min")
            mx = encuesta.get("max")

            if mn is not None and v < mn:
                return False, None

            if mx is not None and v > mx:
                return False, None

            return True, str(v)

        except Exception:
            return False, None

    return False, None


def formato_instrucciones(encuesta):
    tipo = encuesta.get("tipo")

    if tipo == "porcentaje":
        mn = encuesta.get("min", 0)
        mx = encuesta.get("max", 100)

        return (
            f"con un porcentaje entre {mn}% y {mx}% "
            f"(ejemplo: 3.5)"
        )

    if tipo == "numero":
        mn = encuesta.get("min")
        mx = encuesta.get("max")

        if mn is not None and mx is not None:
            return f"con un número entre {mn} y {mx}"

        return "con un número"

    return ""


# ============================================================
# CONVERSACIÓN WHATSAPP
# ============================================================

def procesar(numero, texto=None, button_id=None):
    estado = cargar_estado_participante(numero)
    encuesta = cargar_encuesta_activa()

    print(
        f"📊 {numero} | estado: {estado} | "
        f"texto: {texto} | btn: {button_id}"
    )

    # --------------------------------------------------------
    # Botón CAMBIAR -> SÍ
    # --------------------------------------------------------
    if button_id == "cambiar_si":
        if not encuesta_abierta():
            enviar_texto(
                numero,
                "⏰ La encuesta ya está cerrada. "
                "No es posible cambiar la respuesta."
            )
            return

        guardar_estado_participante(
            numero,
            "esperando_cambio"
        )

        enviar_plantilla(numero, encuesta)
        return

    # --------------------------------------------------------
    # Botón CAMBIAR -> NO
    # --------------------------------------------------------
    if button_id == "cambiar_no":
        enviar_texto(
            numero,
            "👍 Tu voto se mantiene. ¡Gracias por participar!"
        )
        return

    # --------------------------------------------------------
    # Botones SÍ / NO de la plantilla
    # --------------------------------------------------------
    if button_id is not None:
        t = (
            button_id
            .strip()
            .upper()
            .replace("Í", "I")
        )

        es_si = t in ["SI", "S"]
        es_no = t in ["NO", "N"]

        if (
            (es_si or es_no)
            and estado in [
                "esperando_respuesta",
                "esperando_cambio"
            ]
        ):
            if not encuesta_abierta():
                enviar_texto(
                    numero,
                    "⏰ La encuesta ya está cerrada."
                )
                return

            valor = "SÍ" if es_si else "NO"

            guardar_voto(numero, valor)
            guardar_estado_participante(
                numero,
                "confirmado"
            )
            enviar_confirmacion(numero, valor)
            return

    if texto is None:
        return

    # --------------------------------------------------------
    # Primera respuesta por texto
    # --------------------------------------------------------
    if estado == "esperando_respuesta":
        if not encuesta_abierta():
            enviar_texto(
                numero,
                "⏰ La encuesta ya está cerrada."
            )
            return

        ok, valor = validar(texto, encuesta)

        if not ok:
            enviar_texto(
                numero,
                f"❌ Respuesta no válida.\n\n"
                f"{encuesta['texto']}\n\n"
                f"Responde "
                f"{formato_instrucciones(encuesta) or 'con SÍ o NO'}"
            )
            return

        guardar_voto(numero, valor)
        guardar_estado_participante(
            numero,
            "confirmado"
        )
        enviar_confirmacion(numero, valor)

    # --------------------------------------------------------
    # Usuario que ya votó
    # --------------------------------------------------------
    elif estado == "confirmado":
        if texto.strip().upper() == "CAMBIAR":
            if not encuesta_abierta():
                enviar_texto(
                    numero,
                    "⏰ La encuesta ya está cerrada. "
                    "No puedes cambiar tu respuesta."
                )
                return

            guardar_estado_participante(
                numero,
                "esperando_cambio"
            )

            enviar_plantilla(numero, encuesta)

        else:
            enviar_texto(
                numero,
                "Tu voto ya está registrado. "
                "Escribe *CAMBIAR* para modificarlo.\n\n"
                f"📊 {RESULTADOS_URL}"
            )

    # --------------------------------------------------------
    # Nueva respuesta después de CAMBIAR
    # --------------------------------------------------------
    elif estado == "esperando_cambio":
        if not encuesta_abierta():
            enviar_texto(
                numero,
                "⏰ La encuesta ya está cerrada."
            )
            return

        ok, valor = validar(texto, encuesta)

        if not ok:
            enviar_texto(
                numero,
                "❌ Respuesta no válida. "
                f"Responde "
                f"{formato_instrucciones(encuesta) or 'con SÍ o NO'}"
            )
            return

        guardar_voto(numero, valor)
        guardar_estado_participante(
            numero,
            "confirmado"
        )
        enviar_confirmacion(numero, valor)


# ============================================================
# WEBHOOK
# ============================================================

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200

        return "Token inválido", 403

    data = request.json or {}

    print(
        f"\n📩 {json.dumps(data, indent=2, ensure_ascii=False)}"
    )

    try:
        value = data["entry"][0]["changes"][0]["value"]

        if "messages" not in value:
            return "OK", 200

        msg = value["messages"][0]
        numero = msg["from"]
        tipo = msg["type"]

        if tipo == "text":
            procesar(
                numero,
                texto=msg["text"]["body"]
            )

        elif tipo == "button":
            procesar(
                numero,
                button_id=msg["button"].get("text", "")
            )

        elif tipo == "interactive":
            inter = msg["interactive"]

            if "button_reply" in inter:
                procesar(
                    numero,
                    button_id=inter["button_reply"].get(
                        "id", ""
                    )
                )

    except Exception as e:
        print(f"⚠️ Error procesando webhook: {e}")

    return "OK", 200


# ============================================================
# AUTENTICACIÓN / ADMIN
# ============================================================

def autenticado():
    return (
        session.get("admin") is True
        or session.get("ceco") is True
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        data = request.json or {}

        if (
            data.get("user") == CECO_USER
            and data.get("password") == CECO_PASS
        ):
            session["admin"] = True
            return jsonify({"ok": True})

        return jsonify({
            "ok": False,
            "error": "Credenciales incorrectas"
        }), 401

    return send_from_directory(
        "static",
        "login.html"
    )


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")


@app.route("/admin")
def admin():
    if not autenticado():
        return redirect("/admin/login")

    return send_from_directory(
        "static",
        "admin.html"
    )


# Compatibilidad temporal con las rutas antiguas /ceco.

@app.route("/ceco/login", methods=["GET", "POST"])
def ceco_login_legacy():
    if request.method == "POST":
        data = request.json or {}

        if (
            data.get("user") == CECO_USER
            and data.get("password") == CECO_PASS
        ):
            session["admin"] = True
            return jsonify({"ok": True})

        return jsonify({
            "ok": False,
            "error": "Credenciales incorrectas"
        }), 401

    return redirect("/admin/login")


@app.route("/ceco/logout")
def ceco_logout_legacy():
    return redirect("/admin/logout")


@app.route("/ceco")
def ceco_legacy():
    return redirect("/admin")


@app.route("/resultados")
def resultados():
    return send_from_directory(
        "static",
        "resultados.html"
    )


# ============================================================
# API DE ADMINISTRACIÓN
# ============================================================

@app.route("/api/encuesta", methods=["GET"])
def api_get_encuesta():
    if not autenticado():
        return jsonify({"error": "No autorizado"}), 401

    return jsonify(cargar_encuesta())


@app.route("/api/encuesta", methods=["POST"])
def api_set_encuesta():
    if not autenticado():
        return jsonify({"error": "No autorizado"}), 401

    d=request.json or {}
    modo=d.get("modo","editar")
    id_encuesta=d.get("id")

    if modo=="nueva":
        encuesta_id=guardar_nueva_encuesta(d)
        return jsonify({"ok":True,"id":encuesta_id,"nueva":True})

    if id_encuesta:
        try: id_encuesta=int(id_encuesta)
        except Exception: return jsonify({"ok":False,"error":"ID inválido"}),400
        e=obtener_encuesta(id_encuesta)
        if not e: return jsonify({"ok":False,"error":"Encuesta no encontrada"}),404
        if e["activa"]:
            return jsonify({"ok":False,"error":"No puedes editar una encuesta activa. Ciérrala primero."}),400
        actualizar_encuesta(id_encuesta,d)
        return jsonify({"ok":True,"id":id_encuesta,"nueva":False})

    e=obtener_ultima_encuesta()
    if not e:
        encuesta_id=guardar_nueva_encuesta(d)
    else:
        if e["activa"]:
            return jsonify({"ok":False,"error":"La encuesta está activa. Ciérrala primero."}),400
        encuesta_id=e["id"]
        actualizar_encuesta(encuesta_id,d)
    return jsonify({"ok":True,"id":encuesta_id,"nueva":False})


@app.route("/api/lanzar", methods=["POST"])
def api_lanzar():
    if not autenticado():
        return jsonify({"error":"No autorizado"}),401

    d=request.json or {}
    numeros=d.get("numeros",[])
    id_solicitada=d.get("id")
    encuesta=obtener_encuesta(int(id_solicitada)) if id_solicitada else obtener_ultima_encuesta()

    if not encuesta or not encuesta["texto"]:
        return jsonify({"ok":False,"error":"No hay una encuesta configurada"}),400

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT id FROM encuestas
                WHERE activa=TRUE AND id<>%s
                LIMIT 1
            """,(encuesta["id"],))
            otra=cur.fetchone()

    if otra:
        return jsonify({"ok":False,"error":f"Ya hay una encuesta activa (#{otra['id']}). Ciérrala antes de lanzar la nueva."}),409

    numeros_limpios=[]; vistos=set()
    for n in numeros:
        n=str(n).strip().replace(" ","").replace("+","")
        if n and n not in vistos:
            vistos.add(n); numeros_limpios.append(n)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE encuestas
                SET activa=TRUE, estado='ACTIVA',
                    fecha_lanzamiento=CURRENT_TIMESTAMP,
                    fecha_cierre_real=NULL, destinatarios=%s
                WHERE id=%s
            """,(len(numeros_limpios),encuesta["id"]))
            cur.execute("DELETE FROM estados_participantes WHERE encuesta_id=%s",(encuesta["id"],))
            cur.execute("DELETE FROM votos WHERE encuesta_id=%s",(encuesta["id"],))
        conn.commit()

    encuesta=cargar_encuesta_activa()
    enviados=0; errores=[]
    for n in numeros_limpios:
        try:
            enviar_plantilla(n,encuesta)
            guardar_estado_participante(n,"esperando_respuesta")
            enviados+=1
        except Exception as ex:
            errores.append({"numero":n,"error":str(ex)})

    return jsonify({"ok":True,"encuesta_id":encuesta["id"],"enviados":enviados,"errores":errores})


@app.route("/api/cerrar", methods=["POST"])
def api_cerrar():
    if not autenticado():
        return jsonify({"error":"No autorizado"}),401
    d=request.json or {}
    id_encuesta=d.get("id")
    e=obtener_encuesta(int(id_encuesta)) if id_encuesta else obtener_encuesta_activa()
    if not e:
        return jsonify({"ok":False,"error":"No hay encuesta activa"}),400
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE encuestas
                SET activa=FALSE, estado='CERRADA',
                    fecha_cierre_real=CURRENT_TIMESTAMP
                WHERE id=%s
            """,(e["id"],))
        conn.commit()
    return jsonify({"ok":True,"id":e["id"]})


@app.route("/api/encuestas", methods=["GET"])
def api_encuestas():
    if not autenticado():
        return jsonify({"error":"No autorizado"}),401
    rows=listar_encuestas()
    salida=[]
    for row in rows:
        x=dict(row)
        for k in ["fecha_creacion","fecha_lanzamiento","fecha_cierre_real","cierre"]:
            if x.get(k): x[k]=_fecha_iso(x[k])
        x["activa"]=bool(x["activa"])
        if x["activa"]: st="ACTIVA"
        elif x.get("estado")=="CERRADA": st="CERRADA"
        elif x.get("estado")=="BORRADOR":
            dt=_fecha_db(x.get("cierre"))
            st="PROGRAMADA" if dt and _ahora_local()<=dt else "BORRADOR"
        else: st=x.get("estado","BORRADOR")
        x["estado_calculado"]=st
        salida.append(x)
    return jsonify({"encuestas":salida,"activa_id":next((x["id"] for x in salida if x["activa"]),None)})


@app.route("/api/encuestas/<int:id_encuesta>", methods=["GET"])
def api_encuesta_detalle(id_encuesta):
    if not autenticado():
        return jsonify({"error":"No autorizado"}),401
    e=obtener_encuesta(id_encuesta)
    if not e: return jsonify({"error":"Encuesta no encontrada"}),404
    x=_encuesta_dict(e); r=resumen_votos(id_encuesta)
    x["respuestas"]=r["total"]; x["conteo"]=r["conteo"]
    return jsonify(x)


# API LEGACY /api/votos
# ============================================================

@app.route("/api/votos", methods=["GET"])
def api_votos_legacy():
    if not autenticado():
        return jsonify({"error":"No autorizado"}),401
    id_encuesta=request.args.get("id",type=int)
    e=obtener_encuesta(id_encuesta) if id_encuesta else (obtener_encuesta_activa() or obtener_ultima_encuesta())
    if not e: return jsonify({"total":0,"votos":{}})
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT telefono,respuesta,fecha FROM votos WHERE encuesta_id=%s ORDER BY fecha ASC",(e["id"],))
            rows=cur.fetchall()
    votos={x["telefono"]:{
        "respuesta":x["respuesta"],
        "hora":x["fecha"].strftime("%H:%M %d/%m/%Y") if x["fecha"] else ""
    } for x in rows}
    return jsonify({"encuesta_id":e["id"],"total":len(votos),"votos":votos})


@app.route("/api/resetear", methods=["POST"])
def api_resetear_legacy():
    if not autenticado(): return jsonify({"error":"No autorizado"}),401
    d=request.json or {}; id_encuesta=d.get("id")
    e=obtener_encuesta(int(id_encuesta)) if id_encuesta else (obtener_encuesta_activa() or obtener_ultima_encuesta())
    if e:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM votos WHERE encuesta_id=%s",(e["id"],))
                cur.execute("DELETE FROM estados_participantes WHERE encuesta_id=%s",(e["id"],))
            conn.commit()
    return jsonify({"ok":True,"id":e["id"] if e else None})


@app.route("/api/pregunta", methods=["GET"])
def api_pregunta_legacy():
    if not autenticado():
        return jsonify({"error": "No autorizado"}), 401

    e = cargar_encuesta()

    return jsonify({
        "texto": e.get("texto", ""),
        "tipo": e.get("tipo", "sino"),
        "min": e.get("min"),
        "max": e.get("max"),
        "plantilla": e.get(
            "plantilla_sino",
            "plantilla_dinamica"
        )
    })


# ============================================================
# API RESULTADOS PÚBLICA
# ============================================================

@app.route("/api/resultados")
def api_resultados():
    id_encuesta=request.args.get("id",type=int)
    if id_encuesta:
        encuesta=_encuesta_dict(obtener_encuesta(id_encuesta))
    else:
        encuesta=cargar_encuesta_activa()
        if not encuesta.get("id"): encuesta=cargar_encuesta()

    if not encuesta.get("id"):
        return jsonify({"pregunta":"","tipo":"sino","cierre":None,"activa":False,"estado":"SIN_CONFIGURAR","total":0,"conteo":{},"destinatarios":0,"encuesta_id":None})

    r=resumen_votos(encuesta["id"])
    return jsonify({
        "pregunta":encuesta.get("texto",""),
        "tipo":encuesta.get("tipo","sino"),
        "cierre":encuesta.get("cierre"),
        "activa":bool(encuesta.get("activa")),
        "estado":"ACTIVA" if encuesta.get("activa") else encuesta.get("estado","BORRADOR"),
        "total":r["total"],"conteo":r["conteo"],
        "destinatarios":encuesta.get("destinatarios",0),
        "encuesta_id":encuesta["id"]
    })


@app.route("/health")
def health():
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return jsonify({"ok":True,"database":"postgresql"})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500


# ============================================================
# INICIO
# ============================================================

if __name__ == "__main__":
    port = int(
        os.environ.get("PORT", 5000)
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
