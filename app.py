from flask import Flask, request, jsonify, send_from_directory, session, redirect, send_file
import os
import json
import csv
import re
import requests
from io import BytesIO
from copy import copy
from openpyxl import Workbook
from openpyxl.styles import Font
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
PLANTILLA_OPCIONES = os.environ.get("PLANTILLA_OPCIONES", "plantilla_opciones")
PLANTILLA_NUEVO_PARTICIPANTE = os.environ.get("PLANTILLA_NUEVO_PARTICIPANTE", "nuevo_participante")

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

            # Fecha/hora programada de lanzamiento.
            cur.execute("""
                ALTER TABLE encuestas
                ADD COLUMN IF NOT EXISTS fecha_programada TIMESTAMP NULL
            """)

            cur.execute("""
                ALTER TABLE encuestas
                ADD COLUMN IF NOT EXISTS opciones TEXT NULL
            """)

            # Teléfonos asociados a una encuesta, necesarios para poder
            # lanzar automáticamente una encuesta programada.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS destinatarios_encuesta (
                    id SERIAL PRIMARY KEY,
                    encuesta_id INTEGER NOT NULL
                        REFERENCES encuestas(id) ON DELETE CASCADE,
                    telefono VARCHAR(30) NOT NULL,
                    UNIQUE(encuesta_id, telefono)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS participantes (
                    id SERIAL PRIMARY KEY,
                    telefono VARCHAR(30) NOT NULL UNIQUE,
                    estado VARCHAR(20) NOT NULL DEFAULT 'NUEVO',
                    consentimiento BOOLEAN NOT NULL DEFAULT FALSE,
                    fecha_alta TIMESTAMP NULL,
                    fecha_baja TIMESTAMP NULL,
                    codigo_invitacion VARCHAR(20) UNIQUE,
                    invitado_por INTEGER NULL REFERENCES participantes(id) ON DELETE SET NULL
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
            ),
            "plantilla_opciones": os.environ.get("PLANTILLA_OPCIONES", "plantilla_opciones")
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
        "fecha_programada": _fecha_iso(row.get("fecha_programada")),
        "opciones": json.loads(row.get("opciones") or "[]") if row.get("opciones") else [],
        "fecha_cierre_real": _fecha_iso(row["fecha_cierre_real"]),
        "destinatarios": row["destinatarios"],
        "plantilla_sino": os.environ.get(
            "PLANTILLA_SINO", "plantilla_dinamica"
        ),
        "plantilla_abierta": os.environ.get(
            "PLANTILLA_ABIERTA", "plantilla_dinamica"
        ),
        "plantilla_opciones": os.environ.get("PLANTILLA_OPCIONES", "plantilla_opciones")
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
                    (texto, tipo, minimo, maximo, cierre, activa, estado, fecha_programada, opciones)
                VALUES (%s, %s, %s, %s, %s, FALSE, 'BORRADOR', %s, %s)
                RETURNING id
            """, (
                d.get("texto", ""),
                d.get("tipo", "sino"),
                d.get("min"),
                d.get("max"),
                _fecha_db(d.get("cierre")),
                _fecha_db(d.get("fecha_programada")),
                json.dumps(d.get("opciones") or [], ensure_ascii=False)
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
                    cierre=%s,
                    fecha_programada=%s,
                    opciones=%s
                WHERE id=%s
            """, (
                d.get("texto", ""),
                d.get("tipo", "sino"),
                d.get("min"),
                d.get("max"),
                _fecha_db(d.get("cierre")),
                _fecha_db(d.get("fecha_programada")),
                json.dumps(d.get("opciones") or [], ensure_ascii=False),
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
# PARTICIPANTES
# ============================================================

def generar_codigo_invitacion():
    import secrets
    return secrets.token_hex(4).upper()

def obtener_participante(numero):
    numero = normalizar_numero(numero)
    if not numero:
        return None
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM participantes WHERE telefono=%s", (numero,))
            return cur.fetchone()

def asegurar_participante(numero):
    numero = normalizar_numero(numero)
    if not numero:
        return None
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM participantes WHERE telefono=%s", (numero,))
            p = cur.fetchone()
            if p:
                return p
            cur.execute("""
                INSERT INTO participantes (telefono, estado, consentimiento, codigo_invitacion)
                VALUES (%s, 'NUEVO', FALSE, %s)
                RETURNING *
            """, (numero, generar_codigo_invitacion()))
            p = cur.fetchone()
        conn.commit()
    return p

def activar_participante(numero, invitado_por=None):
    numero = normalizar_numero(numero)
    if not numero:
        return None

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM participantes WHERE telefono=%s", (numero,))
            existente = cur.fetchone()

            if existente:
                cur.execute("""
                    UPDATE participantes
                    SET estado='ACTIVO',
                        consentimiento=TRUE,
                        fecha_alta=CURRENT_TIMESTAMP,
                        fecha_baja=NULL,
                        invitado_por=COALESCE(invitado_por, %s)
                    WHERE telefono=%s
                    RETURNING *
                """, (invitado_por, numero))
            else:
                cur.execute("""
                    INSERT INTO participantes
                        (telefono, estado, consentimiento, fecha_alta, codigo_invitacion, invitado_por)
                    VALUES (%s, 'ACTIVO', TRUE, CURRENT_TIMESTAMP, %s, %s)
                    RETURNING *
                """, (numero, generar_codigo_invitacion(), invitado_por))

            p = cur.fetchone()
        conn.commit()
    return p

def dar_de_baja_participante(numero):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE participantes SET estado='BAJA', consentimiento=FALSE, fecha_baja=CURRENT_TIMESTAMP WHERE telefono=%s""", (numero,))
        conn.commit()

def listar_participantes():
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT id, telefono, estado, consentimiento, fecha_alta, fecha_baja, codigo_invitacion, invitado_por FROM participantes ORDER BY id DESC""")
            return cur.fetchall()

def obtener_activos():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telefono FROM participantes WHERE estado='ACTIVO' AND consentimiento=TRUE ORDER BY id")
            return [r[0] for r in cur.fetchall()]


# ============================================================
# DESTINATARIOS / LANZAMIENTO PROGRAMADO
# ============================================================

def normalizar_numero(n):
    if n is None:
        return None

    s = str(n).strip()

    # Excel puede convertir un teléfono numérico a "34600123456.0".
    if re.fullmatch(r"\\d+\\.0", s):
        s = s[:-2]

    # Conservamos solo dígitos.
    s = re.sub(r"\\D", "", s)

    if 8 <= len(s) <= 15:
        return s

    return None


def normalizar_numeros(numeros):
    salida = []
    vistos = set()

    for n in numeros or []:
        numero = normalizar_numero(n)

        if numero and numero not in vistos:
            vistos.add(numero)
            salida.append(numero)

    return salida


def guardar_destinatarios(id_encuesta, numeros):
    numeros = normalizar_numeros(numeros)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM destinatarios_encuesta WHERE encuesta_id=%s",
                (id_encuesta,)
            )

            for numero in numeros:
                cur.execute("""
                    INSERT INTO destinatarios_encuesta (encuesta_id, telefono)
                    VALUES (%s, %s)
                    ON CONFLICT (encuesta_id, telefono) DO NOTHING
                """, (id_encuesta, numero))

        conn.commit()

    return numeros


def obtener_destinatarios(id_encuesta):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT telefono
                FROM destinatarios_encuesta
                WHERE encuesta_id=%s
                ORDER BY id ASC
            """, (id_encuesta,))
            return [row[0] for row in cur.fetchall()]


def guardar_estado_participante_encuesta(id_encuesta, numero, nuevo_estado):
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
            """, (id_encuesta, numero, nuevo_estado))

        conn.commit()


def activar_encuesta(id_encuesta):
    """
    Activa una encuesta previamente programada/configurada.
    Devuelve (ok, mensaje, encuesta).
    """
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT *
                FROM encuestas
                WHERE id=%s
                FOR UPDATE
            """, (id_encuesta,))
            encuesta = cur.fetchone()

            if not encuesta:
                return False, "Encuesta no encontrada", None

            cur.execute("""
                SELECT id
                FROM encuestas
                WHERE activa=TRUE AND id<>%s
                LIMIT 1
            """, (id_encuesta,))
            otra = cur.fetchone()

            if otra:
                return (
                    False,
                    f"Ya hay una encuesta activa (#{otra['id']})",
                    None
                )

            cur.execute("""
                UPDATE encuestas
                SET activa=TRUE,
                    estado='ACTIVA',
                    fecha_lanzamiento=CURRENT_TIMESTAMP,
                    fecha_programada=NULL,
                    fecha_cierre_real=NULL
                WHERE id=%s
            """, (id_encuesta,))

            # Una encuesta programada puede ser reutilizada antes de lanzarse.
            # Al activarla empezamos una nueva edición limpia de sus respuestas.
            cur.execute(
                "DELETE FROM estados_participantes WHERE encuesta_id=%s",
                (id_encuesta,)
            )
            cur.execute(
                "DELETE FROM votos WHERE encuesta_id=%s",
                (id_encuesta,)
            )

        conn.commit()

    encuesta = obtener_encuesta(id_encuesta)
    return True, "", encuesta


def enviar_encuesta_a_destinatarios(id_encuesta):
    encuesta = _encuesta_dict(obtener_encuesta(id_encuesta))
    numeros = obtener_destinatarios(id_encuesta)

    enviados = 0
    errores = []

    for numero in numeros:
        try:
            enviar_plantilla(numero, encuesta)
            guardar_estado_participante_encuesta(
                id_encuesta,
                numero,
                "esperando_respuesta"
            )
            enviados += 1
        except Exception as ex:
            errores.append({
                "numero": numero,
                "error": str(ex)
            })

    return enviados, errores


def procesar_programaciones():
    """
    Comprueba las encuestas programadas y las activa/cierra cuando toca.
    Se ejecuta en segundo plano cada minuto.
    """
    ahora = _ahora_local()

    # Cerrar encuestas activas cuya hora de cierre haya pasado.
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE encuestas
                SET activa=FALSE,
                    estado='CERRADA',
                    fecha_cierre_real=CURRENT_TIMESTAMP
                WHERE activa=TRUE
                  AND cierre IS NOT NULL
                  AND cierre <= %s
            """, (ahora,))
        conn.commit()

    # Obtener encuestas que ya deben lanzarse.
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT id
                FROM encuestas
                WHERE activa=FALSE
                  AND estado='PROGRAMADA'
                  AND fecha_programada IS NOT NULL
                  AND fecha_programada <= %s
                ORDER BY fecha_programada ASC, id ASC
            """, (ahora,))
            pendientes = cur.fetchall()

    for row in pendientes:
        id_encuesta = row["id"]

        ok, mensaje, _ = activar_encuesta(id_encuesta)

        if not ok:
            print(f"⏳ No se pudo activar #{id_encuesta}: {mensaje}")
            continue

        enviados, errores = enviar_encuesta_a_destinatarios(id_encuesta)
        print(
            f"🚀 Encuesta programada #{id_encuesta} activada. "
            f"Enviados: {enviados}. Errores: {len(errores)}"
        )

        if errores:
            print(f"⚠️ Errores de envío: {errores}")


_scheduler_iniciado = False


def iniciar_scheduler():
    """
    Hilo sencillo para Render/Gunicorn con WEB_CONCURRENCY=1.
    Evita iniciar dos hilos si el módulo se carga más de una vez.
    """
    global _scheduler_iniciado

    if _scheduler_iniciado:
        return

    _scheduler_iniciado = True

    import threading
    import time

    def worker():
        while True:
            try:
                procesar_programaciones()
            except Exception as ex:
                print(f"⚠️ Error en scheduler: {ex}")

            time.sleep(60)

    hilo = threading.Thread(
        target=worker,
        name="scheduler-encuestas",
        daemon=True
    )
    hilo.start()



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

# Arranque del programador de encuestas.
iniciar_scheduler()


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


def _respuesta_http_whatsapp(response, contexto):
    """Comprueba la respuesta de Meta y lanza error si el envío ha fallado."""
    if not response.ok:
        raise RuntimeError(
            f"Error WhatsApp ({response.status_code}) en {contexto}: {response.text}"
        )


def _enviar_template(nombre, numero, componentes=None):
    if not WA_TOKEN or not WA_PHONE_ID:
        raise RuntimeError("WA_TOKEN o WA_PHONE_ID no están configurados")

    payload_template = {
        "name": nombre,
        "language": {"code": "es"}
    }
    if componentes:
        payload_template["components"] = componentes

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
            "template": payload_template
        },
        timeout=20
    )

    print(f"📤 Plantilla '{nombre}' → {numero}: {r.status_code} {r.text}")
    _respuesta_http_whatsapp(r, f"plantilla '{nombre}'")
    return r.json() if r.content else {}


def enviar_plantilla_nuevo_participante(numero):
    """Envía la plantilla aprobada de consentimiento inicial."""
    return _enviar_template(PLANTILLA_NUEVO_PARTICIPANTE, numero)


def _opciones_con_letras(opciones):
    """Devuelve [(A, texto), (B, texto), ...] para las opciones de una encuesta."""
    salida = []
    for i, opcion in enumerate(opciones or []):
        if i >= 26:
            break
        letra = chr(65 + i)
        salida.append((letra, str(opcion).strip()))
    return salida


def _texto_opciones(encuesta):
    """Texto que se inyecta en {{2}} de plantilla_opciones."""
    pares = _opciones_con_letras(encuesta.get("opciones") or [])
    if not pares:
        return ""
    return "\n".join(f"{letra}. {texto}" for letra, texto in pares)


def enviar_plantilla(numero, encuesta):
    """Envía la plantilla correspondiente al tipo de encuesta."""
    tipo = encuesta.get("tipo", "sino")

    if tipo == "sino":
        nombre = encuesta.get(
            "plantilla_sino",
            os.environ.get("PLANTILLA_SINO", "plantilla_dinamica")
        )
        # Conservamos el formato de variables que ya usa la plantilla SÍ/NO actual.
        componentes = [{
            "type": "body",
            "parameters": [{
                "type": "text",
                "parameter_name": "pregunta",
                "text": encuesta.get("texto", "")
            }]
        }]

    elif tipo == "opciones":
        nombre = encuesta.get(
            "plantilla_opciones",
            os.environ.get("PLANTILLA_OPCIONES", "plantilla_opciones")
        )
        # Esta plantilla fue creada en Meta con variables numéricas {{1}} y {{2}}.
        componentes = [{
            "type": "body",
            "parameters": [
                {
                    "type": "text",
                    "text": encuesta.get("texto", "")
                },
                {
                    "type": "text",
                    "text": _texto_opciones(encuesta)
                }
            ]
        }]

    else:
        nombre = encuesta.get(
            "plantilla_abierta",
            os.environ.get("PLANTILLA_ABIERTA", "plantilla_dinamica")
        )
        formato = formato_instrucciones(encuesta)
        # Conservamos el formato de variables que ya usa la plantilla abierta actual.
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

    return _enviar_template(nombre, numero, componentes)

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
    texto = (texto or "").strip()
    t = (
        texto.upper()
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
            v = float(texto.replace("%", "").replace(",", "."))
            mn = encuesta.get("min") if encuesta.get("min") is not None else 0
            mx = encuesta.get("max") if encuesta.get("max") is not None else 100
            if mn <= v <= mx:
                return True, f"{v}%"
            return False, None
        except Exception:
            return False, None

    if tipo == "numero":
        try:
            v = float(texto.replace(",", "."))
            mn = encuesta.get("min")
            mx = encuesta.get("max")
            if mn is not None and v < mn:
                return False, None
            if mx is not None and v > mx:
                return False, None
            return True, str(v)
        except Exception:
            return False, None

    if tipo == "opciones":
        opciones = encuesta.get("opciones") or []
        pares = _opciones_con_letras(opciones)

        # Permite pulsar A/B/C/D o escribir cualquier letra hasta Z.
        for letra, opcion in pares:
            if t == letra or t == opcion.upper():
                return True, opcion

        return False, None

    return False, None


def formato_instrucciones(encuesta):
    tipo = encuesta.get("tipo")

    if tipo == "porcentaje":
        mn = encuesta.get("min", 0)
        mx = encuesta.get("max", 100)
        return f"con un porcentaje entre {mn}% y {mx}% (ejemplo: 65%)"

    if tipo == "numero":
        mn = encuesta.get("min")
        mx = encuesta.get("max")
        if mn is not None and mx is not None:
            return f"con un número entre {mn} y {mx}"
        return "con un número"

    if tipo == "opciones":
        pares = _opciones_con_letras(encuesta.get("opciones") or [])
        if not pares:
            return "con una de las opciones disponibles"
        return "con una de las opciones (" + ", ".join(letra for letra, _ in pares) + ")"

    return ""


# ============================================================
# CONVERSACIÓN WHATSAPP
# ============================================================

def enviar_ayuda(numero):
    enviar_texto(numero, "ℹ️ Opciones disponibles:\n\nENCUESTA — participar en la encuesta activa\nRESULTADOS — ver resultados\nINVITAR — obtener tu código de invitación\nCAMBIAR — modificar tu respuesta\nBAJA — dejar de recibir encuestas\nAYUDA — ver este menú")

def procesar(numero, texto=None, button_id=None, button_text=None):
    numero = normalizar_numero(numero)
    if not numero:
        return

    participante = asegurar_participante(numero)

    # Para botones de WhatsApp usamos el título visible como texto principal.
    # Si Meta no lo incluye, usamos el ID del botón como alternativa.
    texto_limpio = (texto or button_text or button_id or "").strip()
    comando = texto_limpio.upper()

    # Invitación: QUIERO PARTICIPAR ABC123
    if comando.startswith("QUIERO PARTICIPAR"):
        partes = comando.split()
        codigo = partes[-1] if len(partes) >= 3 else ""
        invitador = None

        if codigo:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM participantes WHERE codigo_invitacion=%s",
                        (codigo,)
                    )
                    row = cur.fetchone()
                    invitador = row[0] if row else None

        if invitador or not codigo:
            activar_participante(numero, invitador)
            enviar_texto(
                numero,
                "✅ ¡Listo! Ya estás registrado como participante.\n\n"
                "Cuando haya una encuesta activa recibirás la pregunta por aquí. "
                "Escribe AYUDA para ver las opciones."
            )
        else:
            enviar_texto(
                numero,
                "❌ El código de invitación no es válido. Escribe AYUDA si necesitas ayuda."
            )
        return

    # Consentimiento de una persona nueva.
    if participante["estado"] == "NUEVO":
        if comando in ["SI", "SÍ", "ACEPTO", "QUIERO PARTICIPAR"]:
            activar_participante(numero)
            enviar_texto(
                numero,
                "✅ ¡Gracias! Ya estás registrado como participante. Cuando haya una "
                "encuesta activa recibirás la pregunta por aquí.\n\n"
                "Escribe AYUDA para ver las opciones."
            )
        elif comando in ["NO", "NO GRACIAS"]:
            dar_de_baja_participante(numero)
            enviar_texto(
                numero,
                "De acuerdo. No recibirás encuestas. Si quieres participar en el futuro, "
                "escribe ALTA."
            )
        else:
            enviar_texto(
                numero,
                "👋 ¡Hola! Este es el canal de participación en las encuestas.\n\n"
                "¿Quieres participar? Responde *SÍ* o *NO*.\n\n"
                "Si has recibido un código de invitación, escribe *QUIERO PARTICIPAR CÓDIGO*."
            )
        return

    # BAJA / ALTA son comandos globales.
    if comando == "BAJA":
        dar_de_baja_participante(numero)
        enviar_texto(
            numero,
            "👋 Te has dado de baja correctamente. No recibirás nuevas encuestas. "
            "Puedes volver cuando quieras escribiendo ALTA."
        )
        return

    if comando == "ALTA":
        activar_participante(numero)
        enviar_texto(numero, "✅ Has vuelto a activar tu participación. Recibirás las próximas encuestas.")
        return

    if participante["estado"] == "BAJA":
        if comando in ["AYUDA", "MENU", "OPCIONES"]:
            enviar_texto(numero, "Estás dado de baja y no recibirás encuestas. Escribe ALTA para volver a participar.")
        else:
            enviar_texto(numero, "Estás dado de baja. Escribe ALTA si quieres volver a participar.")
        return

    if comando in ["AYUDA", "MENU", "OPCIONES"]:
        enviar_ayuda(numero)
        return

    if comando == "RESULTADOS":
        enviar_texto(numero, f"📊 Consulta los resultados aquí:\n{RESULTADOS_URL}")
        return

    if comando == "INVITAR":
        enviar_texto(
            numero,
            f"👥 Invita a otra persona a participar.\n\n"
            f"Tu código de invitación es: *{participante['codigo_invitacion']}*\n\n"
            f"La otra persona debe escribir en este chat: *QUIERO PARTICIPAR {participante['codigo_invitacion']}*"
        )
        return

    encuesta = cargar_encuesta_activa()
    if not encuesta.get("id"):
        enviar_ayuda(numero)
        return

    estado = cargar_estado_participante(numero)

    if comando == "ENCUESTA":
        guardar_estado_participante_encuesta(encuesta["id"], numero, "esperando_respuesta")
        enviar_plantilla(numero, encuesta)
        return

    if comando == "CAMBIAR":
        if estado != "confirmado":
            enviar_texto(numero, "Todavía no tienes una respuesta registrada. Escribe ENCUESTA para participar.")
            return
        guardar_estado_participante(numero, "esperando_cambio")
        enviar_plantilla(numero, encuesta)
        return

    if estado in ["esperando_respuesta", "esperando_cambio"] or comando == "ENCUESTA":
        ok, valor = validar(texto_limpio, encuesta)
        if not ok:
            enviar_texto(
                numero,
                f"❌ Respuesta no válida. {encuesta['texto']}\n\n"
                f"Responde {formato_instrucciones(encuesta) or 'con SÍ o NO'}"
            )
            return
        guardar_voto(numero, valor)
        enviar_confirmacion(numero, valor)
        return

    if estado == "confirmado":
        enviar_texto(
            numero,
            "Tu voto ya está registrado. Escribe CAMBIAR para modificarlo, "
            "RESULTADOS para ver los resultados o AYUDA para ver las opciones."
        )
    else:
        enviar_ayuda(numero)


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Token inválido", 403

    data = request.json or {}

    print(f"\n📩 {json.dumps(data, indent=2, ensure_ascii=False)}")

    try:
        value = data["entry"][0]["changes"][0]["value"]

        if "messages" not in value:
            return "OK", 200

        msg = value["messages"][0]
        numero = msg["from"]
        tipo = msg["type"]

        if tipo == "text":
            procesar(numero, texto=msg["text"].get("body", ""))

        elif tipo == "button":
            boton = msg.get("button", {})
            procesar(
                numero,
                button_id=boton.get("payload", ""),
                button_text=boton.get("text", "")
            )

        elif tipo == "interactive":
            inter = msg.get("interactive", {})

            if "button_reply" in inter:
                reply = inter["button_reply"]
                procesar(
                    numero,
                    button_id=reply.get("id", ""),
                    button_text=reply.get("title", "")
                )

            elif "list_reply" in inter:
                reply = inter["list_reply"]
                procesar(
                    numero,
                    button_id=reply.get("id", ""),
                    button_text=reply.get("title", "")
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



@app.route("/api/subir-numeros", methods=["POST"])
def api_subir_numeros():
    """Recibe CSV/XLSX y devuelve los teléfonos detectados."""
    if not autenticado():
        return jsonify({"error": "No autorizado"}), 401

    archivo = request.files.get("archivo")

    if not archivo or not archivo.filename:
        return jsonify({
            "ok": False,
            "error": "No se ha seleccionado ningún archivo"
        }), 400

    nombre = archivo.filename.lower()

    try:
        valores = []

        if nombre.endswith(".csv"):
            contenido = archivo.read().decode("utf-8-sig", errors="replace")
            muestra = contenido[:4096]

            try:
                dialecto = csv.Sniffer().sniff(muestra, delimiters=",;\\t")
            except Exception:
                dialecto = csv.excel

            reader = csv.reader(contenido.splitlines(), dialect=dialecto)

            for fila in reader:
                valores.extend(fila)

        elif nombre.endswith(".xlsx"):
            from openpyxl import load_workbook

            libro = load_workbook(
                archivo,
                read_only=True,
                data_only=True
            )
            hoja = libro.active

            for fila in hoja.iter_rows(values_only=True):
                valores.extend(fila)

            libro.close()

        elif nombre.endswith(".xls"):
            return jsonify({
                "ok": False,
                "error": "El formato .xls antiguo no está soportado. Guarda el archivo como .xlsx y vuelve a subirlo."
            }), 400

        else:
            return jsonify({
                "ok": False,
                "error": "Formato no soportado. Usa CSV o XLSX."
            }), 400

        numeros = normalizar_numeros(valores)

        if not numeros:
            return jsonify({
                "ok": False,
                "error": "No se encontraron teléfonos válidos. Deben tener entre 8 y 15 dígitos."
            }), 400

        return jsonify({
            "ok": True,
            "numeros": numeros,
            "total": len(numeros),
            "archivo": archivo.filename
        })

    except Exception as ex:
        print(f"⚠️ Error leyendo archivo de teléfonos: {ex}")
        return jsonify({
            "ok": False,
            "error": f"No se pudo leer el archivo: {ex}"
        }), 400


@app.route("/api/participantes", methods=["GET"])
def api_participantes():
    if not autenticado():
        return jsonify({"error":"No autorizado"}),401
    rows=[]
    for p in listar_participantes():
        x=dict(p)
        for k in ["fecha_alta","fecha_baja"]:
            if x.get(k): x[k]=_fecha_iso(x[k])
        rows.append(x)
    return jsonify({"participantes":rows,"total":len(rows),"activos":sum(1 for x in rows if x["estado"]=="ACTIVO"),"bajas":sum(1 for x in rows if x["estado"]=="BAJA")})

@app.route("/api/participantes", methods=["POST"])
def api_crear_participante():
    if not autenticado():
        return jsonify({"error": "No autorizado"}), 401

    d = request.json or {}
    numero = normalizar_numero(d.get("telefono"))
    if not numero:
        return jsonify({"ok": False, "error": "Teléfono no válido"}), 400

    existente = obtener_participante(numero)
    if existente:
        return jsonify({
            "ok": True,
            "participante": dict(existente),
            "nuevo": False,
            "bienvenida_enviada": False,
            "mensaje": "El participante ya estaba registrado."
        })

    participante = asegurar_participante(numero)

    try:
        enviar_plantilla_nuevo_participante(numero)
        return jsonify({
            "ok": True,
            "participante": dict(participante),
            "nuevo": True,
            "bienvenida_enviada": True
        })
    except Exception as ex:
        print(f"⚠️ Participante creado pero no se pudo enviar bienvenida a {numero}: {ex}")
        return jsonify({
            "ok": False,
            "participante": dict(participante),
            "nuevo": True,
            "bienvenida_enviada": False,
            "error": f"Participante creado, pero no se pudo enviar la plantilla de bienvenida: {ex}"
        }), 502

@app.route("/api/participantes/importar", methods=["POST"])
def api_importar_participantes():
    if not autenticado(): return jsonify({"error":"No autorizado"}),401
    archivo=request.files.get("archivo")
    if not archivo or not archivo.filename: return jsonify({"ok":False,"error":"No se ha seleccionado ningún archivo"}),400
    nombre=archivo.filename.lower(); valores=[]
    try:
        if nombre.endswith('.csv'):
            contenido=archivo.read().decode('utf-8-sig',errors='replace')
            try: dialecto=csv.Sniffer().sniff(contenido[:4096],delimiters=',;\t')
            except Exception: dialecto=csv.excel
            for fila in csv.reader(contenido.splitlines(),dialect=dialecto): valores.extend(fila)
        elif nombre.endswith('.xlsx'):
            from openpyxl import load_workbook
            libro=load_workbook(archivo,read_only=True,data_only=True)
            for fila in libro.active.iter_rows(values_only=True): valores.extend(fila)
            libro.close()
        else: return jsonify({"ok":False,"error":"Usa CSV o XLSX"}),400
        numeros=normalizar_numeros(valores)
        creados=0
        for n in numeros:
            if not obtener_participante(n): asegurar_participante(n); creados+=1
        return jsonify({"ok":True,"total":len(numeros),"nuevos":creados})
    except Exception as ex:
        return jsonify({"ok":False,"error":str(ex)}),400

@app.route("/api/lanzar", methods=["POST"])
def api_lanzar():
    if not autenticado():
        return jsonify({"error": "No autorizado"}), 401

    d = request.json or {}
    numeros = normalizar_numeros(d.get("numeros", []))
    if not numeros and d.get("destinatarios") == "todos_activos":
        numeros = obtener_activos()
    id_solicitada = d.get("id")
    programar = bool(d.get("programar", False))

    try:
        id_encuesta = int(id_solicitada) if id_solicitada else None
    except Exception:
        return jsonify({"ok": False, "error": "ID inválido"}), 400

    encuesta = (
        obtener_encuesta(id_encuesta)
        if id_encuesta
        else obtener_ultima_encuesta()
    )

    if not encuesta or not encuesta["texto"]:
        return jsonify({
            "ok": False,
            "error": "No hay una encuesta configurada"
        }), 400

    if encuesta["activa"]:
        return jsonify({
            "ok": False,
            "error": "La encuesta ya está activa"
        }), 400

    # Guardamos los teléfonos para que una encuesta programada
    # pueda lanzarse aunque el navegador esté cerrado.
    if numeros:
        numeros = guardar_destinatarios(encuesta["id"], numeros)
    else:
        numeros = obtener_destinatarios(encuesta["id"])

    if not numeros:
        return jsonify({
            "ok": False,
            "error": "No hay participantes activos seleccionados"
        }), 400

    fecha_programada = _fecha_db(d.get("fecha_programada"))

    if programar:
        if not fecha_programada:
            return jsonify({
                "ok": False,
                "error": "Indica una fecha y hora de lanzamiento"
            }), 400

        ahora = _ahora_local()

        if fecha_programada <= ahora:
            return jsonify({
                "ok": False,
                "error": "La fecha de lanzamiento debe ser futura"
            }), 400

        cierre = _fecha_db(encuesta["cierre"])

        if cierre and cierre <= fecha_programada:
            return jsonify({
                "ok": False,
                "error": "La fecha de cierre debe ser posterior al lanzamiento"
            }), 400

        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE encuestas
                    SET estado='PROGRAMADA',
                        fecha_programada=%s,
                        activa=FALSE,
                        fecha_lanzamiento=NULL,
                        fecha_cierre_real=NULL,
                        destinatarios=%s
                    WHERE id=%s
                """, (
                    fecha_programada,
                    len(numeros),
                    encuesta["id"]
                ))
            conn.commit()

        return jsonify({
            "ok": True,
            "programada": True,
            "encuesta_id": encuesta["id"],
            "destinatarios": len(numeros),
            "fecha_programada": _fecha_iso(fecha_programada)
        })

    # Lanzamiento inmediato.
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT id
                FROM encuestas
                WHERE activa=TRUE AND id<>%s
                LIMIT 1
            """, (encuesta["id"],))
            otra = cur.fetchone()

    if otra:
        return jsonify({
            "ok": False,
            "error": (
                f"Ya hay una encuesta activa (#{otra['id']}). "
                "Ciérrala antes de lanzar la nueva."
            )
        }), 409

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE encuestas
                SET activa=TRUE,
                    estado='ACTIVA',
                    fecha_lanzamiento=CURRENT_TIMESTAMP,
                    fecha_programada=NULL,
                    fecha_cierre_real=NULL,
                    destinatarios=%s
                WHERE id=%s
            """, (len(numeros), encuesta["id"]))

            cur.execute(
                "DELETE FROM estados_participantes WHERE encuesta_id=%s",
                (encuesta["id"],)
            )
            cur.execute(
                "DELETE FROM votos WHERE encuesta_id=%s",
                (encuesta["id"],)
            )

        conn.commit()

    encuesta_activa = _encuesta_dict(obtener_encuesta(encuesta["id"]))

    enviados = 0
    errores = []

    for numero in numeros:
        try:
            enviar_plantilla(numero, encuesta_activa)
            guardar_estado_participante_encuesta(
                encuesta["id"],
                numero,
                "esperando_respuesta"
            )
            enviados += 1
        except Exception as ex:
            errores.append({
                "numero": numero,
                "error": str(ex)
            })

    return jsonify({
        "ok": True,
        "encuesta_id": encuesta["id"],
        "enviados": enviados,
        "errores": errores,
        "programada": False
    })


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
        for k in ["fecha_creacion","fecha_lanzamiento","fecha_programada","fecha_cierre_real","cierre"]:
            if x.get(k): x[k]=_fecha_iso(x[k])
        x["activa"]=bool(x["activa"])
        if x["activa"]: st="ACTIVA"
        elif x.get("estado")=="CERRADA": st="CERRADA"
        elif x.get("estado")=="PROGRAMADA": st="PROGRAMADA"
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

@app.route("/api/exportar/<int:id_encuesta>")
def api_exportar_excel(id_encuesta):
    if not autenticado():
        return jsonify({"error": "No autorizado"}), 401

    encuesta = obtener_encuesta(id_encuesta)

    if not encuesta:
        return jsonify({
            "ok": False,
            "error": "Encuesta no encontrada"
        }), 404

    e = _encuesta_dict(encuesta)
    resumen = resumen_votos(id_encuesta)
    numeros = obtener_destinatarios(id_encuesta)

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT id, telefono, respuesta, fecha
                FROM votos
                WHERE encuesta_id=%s
                ORDER BY fecha ASC
            """, (id_encuesta,))
            votos = cur.fetchall()

            cur.execute("""
                SELECT telefono, estado, fecha_actualizacion
                FROM estados_participantes
                WHERE encuesta_id=%s
                ORDER BY telefono ASC
            """, (id_encuesta,))
            estados = cur.fetchall()

    wb = Workbook()

    # Hoja resumen
    ws = wb.active
    ws.title = "Resumen"
    ws.append(["ENCUESTA", f"#{id_encuesta}"])
    ws.append(["Pregunta", e.get("texto", "")])
    ws.append(["Tipo", e.get("tipo", "")])
    ws.append(["Estado", e.get("estado", "")])
    ws.append(["Fecha creación", e.get("fecha_creacion") or ""])
    ws.append(["Fecha programada", e.get("fecha_programada") or ""])
    ws.append(["Fecha lanzamiento", e.get("fecha_lanzamiento") or ""])
    ws.append(["Fecha cierre previsto", e.get("cierre") or ""])
    ws.append(["Fecha cierre real", e.get("fecha_cierre_real") or ""])
    ws.append(["Destinatarios", e.get("destinatarios", len(numeros)) or 0])
    ws.append(["Respuestas", resumen["total"]])

    destinatarios = e.get("destinatarios", len(numeros)) or 0
    participacion = (
        resumen["total"] / destinatarios
        if destinatarios else 0
    )
    ws.append(["Participación", participacion])
    ws.append([])
    ws.append(["Respuesta", "Cantidad"])

    for respuesta, cantidad in resumen["conteo"].items():
        ws.append([respuesta, cantidad])

    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 70

    # Hoja respuestas
    ws2 = wb.create_sheet("Respuestas")
    ws2.append(["ID", "Teléfono", "Respuesta", "Fecha"])

    for voto in votos:
        ws2.append([
            voto["id"],
            voto["telefono"],
            voto["respuesta"],
            voto["fecha"].strftime("%Y-%m-%d %H:%M:%S")
            if voto["fecha"] else ""
        ])

    ws2.column_dimensions["A"].width = 12
    ws2.column_dimensions["B"].width = 22
    ws2.column_dimensions["C"].width = 20
    ws2.column_dimensions["D"].width = 22

    # Hoja participantes
    ws3 = wb.create_sheet("Participantes")
    ws3.append(["Teléfono", "Estado", "Última actualización"])

    estados_por_numero = {
        x["telefono"]: x for x in estados
    }

    for numero in numeros:
        x = estados_por_numero.get(numero)
        ws3.append([
            numero,
            x["estado"] if x else "sin_estado",
            x["fecha_actualizacion"].strftime("%Y-%m-%d %H:%M:%S")
            if x and x["fecha_actualizacion"] else ""
        ])

    ws3.column_dimensions["A"].width = 22
    ws3.column_dimensions["B"].width = 25
    ws3.column_dimensions["C"].width = 25

    # Formato básico
    for sheet in [ws, ws2, ws3]:
        for cell in sheet[1]:
            cell.font = Font(bold=True)

    ws["B12"].number_format = "0.0%"

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    nombre = f"encuesta_{id_encuesta}.xlsx"

    return send_file(
        output,
        as_attachment=True,
        download_name=nombre,
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
    )


@app.route("/api/exportar")
def api_exportar_todas():
    if not autenticado():
        return jsonify({"error": "No autorizado"}), 401

    encuestas = listar_encuestas()

    wb = Workbook()
    ws = wb.active
    ws.title = "Encuestas"
    ws.append([
        "ID", "Pregunta", "Tipo", "Estado", "Destinatarios",
        "Respuestas", "Participación", "Fecha creación",
        "Fecha programada", "Fecha lanzamiento", "Fecha cierre real"
    ])

    for e in encuestas:
        e = dict(e)
        total = int(e.get("respuestas", 0) or 0)
        dest = int(e.get("destinatarios", 0) or 0)
        ws.append([
            e["id"],
            e["texto"],
            e["tipo"],
            e["estado"],
            dest,
            total,
            total / dest if dest else 0,
            _fecha_iso(e.get("fecha_creacion")),
            _fecha_iso(e.get("fecha_programada")),
            _fecha_iso(e.get("fecha_lanzamiento")),
            _fecha_iso(e.get("fecha_cierre_real"))
        ])

    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 70
    for col in "CDEFGHIJK":
        ws.column_dimensions[col].width = 20

    ws2 = wb.create_sheet("Respuestas")
    ws2.append(["Encuesta ID", "Pregunta", "Teléfono", "Respuesta", "Fecha"])

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT v.encuesta_id, e.texto, v.telefono, v.respuesta, v.fecha
                FROM votos v
                JOIN encuestas e ON e.id=v.encuesta_id
                ORDER BY v.encuesta_id DESC, v.fecha ASC
            """)
            for r in cur.fetchall():
                ws2.append([
                    r["encuesta_id"],
                    r["texto"],
                    r["telefono"],
                    r["respuesta"],
                    _fecha_iso(r["fecha"])
                ])

    ws2.column_dimensions["A"].width = 15
    ws2.column_dimensions["B"].width = 70
    ws2.column_dimensions["C"].width = 22
    ws2.column_dimensions["D"].width = 20
    ws2.column_dimensions["E"].width = 22

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return __import__("flask").send_file(
        output,
        as_attachment=True,
        download_name="historico_encuestas.xlsx",
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
    )


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
