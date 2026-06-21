from flask import Flask, request, jsonify, send_from_directory
import os
import json
import requests
from datetime import datetime

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "mi_token_secreto_123")
WA_TOKEN     = os.environ.get("WA_TOKEN", "")
WA_PHONE_ID  = os.environ.get("WA_PHONE_ID", "")

# ═══════════════════════════════════════════════════════
# ALMACENAMIENTO — usa variables de entorno para persistir
# Los datos se guardan como JSON en variables de entorno
# de Render vía su API, o en memoria si no hay API key.
# Solución simple: guardar en /tmp (persiste en sesión)
# y usar variables de entorno como fallback de config.
# ═══════════════════════════════════════════════════════

DATA_DIR = os.environ.get("DATA_DIR", "/tmp")

def cargar_json(fichero):
    path = os.path.join(DATA_DIR, fichero)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def guardar_json(fichero, datos):
    path = os.path.join(DATA_DIR, fichero)
    with open(path, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# PREGUNTA ACTIVA
# ═══════════════════════════════════════════════════════

PREGUNTA_DEFAULT = {
    "texto":     os.environ.get("PREGUNTA_TEXTO", "¿Crees que subirá el PIB en 2027?"),
    "tipo":      os.environ.get("PREGUNTA_TIPO", "sino"),
    "plantilla": os.environ.get("PREGUNTA_PLANTILLA", "pregunta"),
    "min":       None,
    "max":       None,
}

def cargar_pregunta():
    p = cargar_json("pregunta.json")
    if not p or not p.get("texto"):
        return PREGUNTA_DEFAULT
    return p

def guardar_pregunta(p):
    guardar_json("pregunta.json", p)


# ═══════════════════════════════════════════════════════
# VOTOS Y ESTADOS
# ═══════════════════════════════════════════════════════

def guardar_voto(numero, respuesta):
    votos = cargar_json("votos.json")
    votos[numero] = {
        "respuesta": respuesta,
        "hora": datetime.now().strftime("%H:%M %d/%m/%Y")
    }
    guardar_json("votos.json", votos)
    print(f"✅ Voto guardado: {numero} → {respuesta}")


# ═══════════════════════════════════════════════════════
# ENVIAR MENSAJES
# ═══════════════════════════════════════════════════════

def enviar_texto(numero, texto):
    if not WA_TOKEN or not WA_PHONE_ID:
        print(f"⚠️  Sin credenciales. Mensaje: {texto}")
        return
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    body = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": texto}
    }
    r = requests.post(url, headers=headers, json=body)
    print(f"📤 Texto enviado a {numero}: {r.status_code} {r.text}")

def enviar_plantilla(numero, nombre_plantilla):
    if not WA_TOKEN or not WA_PHONE_ID:
        print(f"⚠️  Sin credenciales. Plantilla: {nombre_plantilla}")
        return
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    body = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "template",
        "template": {
            "name": nombre_plantilla,
            "language": {"code": "es"}
        }
    }
    r = requests.post(url, headers=headers, json=body)
    print(f"📤 Plantilla '{nombre_plantilla}' enviada a {numero}: {r.status_code} {r.text}")

def enviar_botones_cambiar(numero, respuesta_actual):
    if not WA_TOKEN or not WA_PHONE_ID:
        print("⚠️  Sin credenciales para botones")
        return
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    body = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": f"✅ Tu respuesta *{respuesta_actual}* ha sido registrada.\n\n¿Quieres cambiarla?"
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "cambiar_si", "title": "Sí, cambiar"}},
                    {"type": "reply", "reply": {"id": "cambiar_no", "title": "No, mantener"}}
                ]
            }
        }
    }
    r = requests.post(url, headers=headers, json=body)
    print(f"📤 Botones cambiar enviados a {numero}: {r.status_code} {r.text}")


# ═══════════════════════════════════════════════════════
# VALIDACIÓN
# ═══════════════════════════════════════════════════════

def validar_respuesta(texto, pregunta):
    texto = texto.strip().upper()
    tipo  = pregunta["tipo"]

    if tipo == "sino":
        if texto in ["SI", "SÍ", "S", "NO", "N"]:
            return True, "SÍ" if texto in ["SI", "SÍ", "S"] else "NO"
        return False, None

    if tipo == "porcentaje":
        try:
            valor = float(texto.replace("%", "").replace(",", "."))
            mn = pregunta.get("min") if pregunta.get("min") is not None else 0
            mx = pregunta.get("max") if pregunta.get("max") is not None else 100
            if mn <= valor <= mx:
                return True, f"{valor}%"
            return False, None
        except ValueError:
            return False, None

    if tipo == "numero":
        try:
            valor = float(texto.replace(",", "."))
            mn = pregunta.get("min")
            mx = pregunta.get("max")
            if mn is not None and valor < mn:
                return False, None
            if mx is not None and valor > mx:
                return False, None
            return True, str(valor)
        except ValueError:
            return False, None

    return False, None

def mensaje_formato(pregunta):
    tipo = pregunta["tipo"]
    if tipo == "sino":
        return "Responde *SÍ* o *NO*"
    if tipo == "porcentaje":
        return f"Responde con un porcentaje entre {pregunta.get('min',0)}% y {pregunta.get('max',100)}%"
    if tipo == "numero":
        mn, mx = pregunta.get("min"), pregunta.get("max")
        if mn is not None and mx is not None:
            return f"Responde con un número entre {mn} y {mx}"
        return "Responde con un número"


# ═══════════════════════════════════════════════════════
# LÓGICA DE CONVERSACIÓN
# ═══════════════════════════════════════════════════════

def procesar_mensaje(numero, texto=None, button_id=None):
    estados  = cargar_json("estados.json")
    estado   = estados.get(numero, "esperando_respuesta")
    pregunta = cargar_pregunta()

    print(f"📊 Estado de {numero}: {estado} | texto: {texto} | button_id: {button_id}")

    # ── Botón de confirmar/cambiar ──
    if button_id == "cambiar_si":
        estados[numero] = "esperando_cambio"
        guardar_json("estados.json", estados)
        enviar_plantilla(numero, pregunta["plantilla"])
        return

    if button_id == "cambiar_no":
        enviar_texto(numero, "👍 Tu voto se mantiene. ¡Gracias por participar!")
        return

    # ── Botón de la plantilla (Sí / No) ──
    # Meta envía type "button" con text "Sí" o "No" (con tildes, minúsculas)
    if button_id is not None:
        titulo = button_id.strip().upper().replace("Í", "I").replace("É", "E").replace("Á", "A")
        es_si = titulo in ["SI", "S", "YES"]
        es_no = titulo in ["NO", "N"]

        if (es_si or es_no) and estado in ["esperando_respuesta", "esperando_cambio"]:
            valor = "SÍ" if es_si else "NO"
            guardar_voto(numero, valor)
            estados[numero] = "confirmado"
            guardar_json("estados.json", estados)
            enviar_botones_cambiar(numero, valor)
            return

    # ── Respuesta por texto ──
    if texto is None:
        return

    if estado == "esperando_respuesta":
        valido, valor = validar_respuesta(texto, pregunta)
        if not valido:
            enviar_texto(numero,
                f"❌ Respuesta no válida.\n\n"
                f"{pregunta['texto']}\n\n"
                f"{mensaje_formato(pregunta)}"
            )
            return
        guardar_voto(numero, valor)
        estados[numero] = "confirmado"
        guardar_json("estados.json", estados)
        enviar_botones_cambiar(numero, valor)

    elif estado == "confirmado":
        if texto.strip().upper() == "CAMBIAR":
            estados[numero] = "esperando_cambio"
            guardar_json("estados.json", estados)
            enviar_plantilla(numero, pregunta["plantilla"])
        else:
            enviar_texto(numero, "Tu voto ya está registrado. Escribe *CAMBIAR* si quieres modificarlo.")

    elif estado == "esperando_cambio":
        valido, valor = validar_respuesta(texto, pregunta)
        if not valido:
            enviar_texto(numero, f"❌ Respuesta no válida.\n\n{mensaje_formato(pregunta)}")
            return
        guardar_voto(numero, valor)
        estados[numero] = "confirmado"
        guardar_json("estados.json", estados)
        enviar_botones_cambiar(numero, valor)


# ═══════════════════════════════════════════════════════
# WEBHOOK
# ═══════════════════════════════════════════════════════

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Token inválido", 403

    if request.method == "POST":
        data = request.json
        print(f"\n📩 Payload completo: {json.dumps(data, indent=2)}")
        try:
            value = data["entry"][0]["changes"][0]["value"]
            if "messages" in value:
                mensaje = value["messages"][0]
                numero  = mensaje["from"]
                tipo    = mensaje["type"]

                print(f"📱 Tipo de mensaje: {tipo}")

                if tipo == "text":
                    procesar_mensaje(numero, texto=mensaje["text"]["body"])

                elif tipo == "button":
                    # Respuesta a botón de plantilla — llega como type "button"
                    btn_text = mensaje["button"].get("text", "")
                    btn_payload = mensaje["button"].get("payload", "")
                    print(f"🔘 Button plantilla — text: '{btn_text}' | payload: '{btn_payload}'")
                    procesar_mensaje(numero, button_id=btn_text or btn_payload)

                elif tipo == "interactive":
                    inter = mensaje["interactive"]
                    print(f"🔘 Interactive completo: {json.dumps(inter, indent=2)}")

                    if "button_reply" in inter:
                        btn_id    = inter["button_reply"].get("id", "")
                        btn_title = inter["button_reply"].get("title", "")
                        print(f"🔘 Button ID: '{btn_id}' | Title: '{btn_title}'")
                        procesar_mensaje(numero, button_id=btn_id or btn_title)

        except (KeyError, IndexError) as e:
            print(f"⚠️  Error: {e}")

        return "OK", 200


# ═══════════════════════════════════════════════════════
# PANEL DE GESTIÓN — API
# ═══════════════════════════════════════════════════════

@app.route("/admin")
def admin():
    return send_from_directory("static", "admin.html")

@app.route("/api/pregunta", methods=["GET"])
def api_get_pregunta():
    return jsonify(cargar_pregunta())

@app.route("/api/pregunta", methods=["POST"])
def api_set_pregunta():
    data = request.json
    print(f"💾 Guardando pregunta: {data}")
    pregunta = {
        "texto":     data.get("texto", ""),
        "tipo":      data.get("tipo", "sino"),
        "plantilla": data.get("plantilla", "pregunta"),
        "min":       data.get("min"),
        "max":       data.get("max"),
    }
    guardar_pregunta(pregunta)
    # Verificar que se guardó
    verificacion = cargar_pregunta()
    print(f"✅ Pregunta guardada y verificada: {verificacion}")
    return jsonify({"ok": True, "pregunta": verificacion})

@app.route("/api/enviar", methods=["POST"])
def api_enviar():
    data     = request.json
    numeros  = data.get("numeros", [])
    pregunta = cargar_pregunta()
    estados  = cargar_json("estados.json")
    enviados = 0
    errores  = []

    for numero in numeros:
        numero = numero.strip().replace(" ", "").replace("+", "")
        if not numero:
            continue
        try:
            enviar_plantilla(numero, pregunta["plantilla"])
            estados[numero] = "esperando_respuesta"
            enviados += 1
        except Exception as e:
            errores.append({"numero": numero, "error": str(e)})

    guardar_json("estados.json", estados)
    return jsonify({"ok": True, "enviados": enviados, "errores": errores})

@app.route("/api/votos", methods=["GET"])
def api_votos():
    votos = cargar_json("votos.json")
    return jsonify({"total": len(votos), "votos": votos})

@app.route("/api/resetear", methods=["POST"])
def api_resetear():
    guardar_json("estados.json", {})
    guardar_json("votos.json", {})
    return jsonify({"ok": True})

@app.route("/votos", methods=["GET"])
def ver_votos():
    votos = cargar_json("votos.json")
    return jsonify({"total": len(votos), "votos": votos})

@app.route("/enviar/<numero>", methods=["GET"])
def enviar_uno(numero):
    pregunta = cargar_pregunta()
    enviar_plantilla(numero, pregunta["plantilla"])
    estados = cargar_json("estados.json")
    estados[numero] = "esperando_respuesta"
    guardar_json("estados.json", estados)
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════
# INICIO
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Servidor en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
