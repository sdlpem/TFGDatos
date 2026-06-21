from flask import Flask, request, jsonify
import os
import json
import requests
from datetime import datetime

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "mi_token_secreto_123")
WA_TOKEN     = os.environ.get("WA_TOKEN", "")
WA_PHONE_ID  = os.environ.get("WA_PHONE_ID", "")


# ═══════════════════════════════════════════════════════
# PREGUNTA ACTIVA
# ═══════════════════════════════════════════════════════

PREGUNTA_ACTIVA = {
    "texto": "¿Crees que subirá el PIB en 2027?",
    "tipo": "sino",
    "plantilla": "pregunta",   # Nombre de la plantilla en Meta
    "min": None,
    "max": None,
}


# ═══════════════════════════════════════════════════════
# ESTADOS Y VOTOS
# ═══════════════════════════════════════════════════════

def cargar_json(fichero):
    try:
        with open(fichero, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def guardar_json(fichero, datos):
    with open(fichero, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)

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
    print(f"📤 Texto enviado a {numero}: {r.status_code}")

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
    print(f"📤 Plantilla '{nombre_plantilla}' enviada a {numero}: {r.status_code}")

def enviar_botones_cambiar(numero, respuesta_actual):
    """Envía botones Sí/No para confirmar si quiere cambiar su respuesta"""
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
    print(f"📤 Botones cambiar enviados a {numero}: {r.status_code}")


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
            mn = pregunta.get("min", 0)
            mx = pregunta.get("max", 100)
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
        return f"Responde con un porcentaje entre {pregunta.get('min', 0)}% y {pregunta.get('max', 100)}%"
    if tipo == "numero":
        mn, mx = pregunta.get("min"), pregunta.get("max")
        if mn is not None and mx is not None:
            return f"Responde con un número entre {mn} y {mx}"
        return "Responde con un número"


# ═══════════════════════════════════════════════════════
# LÓGICA DE CONVERSACIÓN
# ═══════════════════════════════════════════════════════

def procesar_mensaje(numero, texto=None, button_id=None):
    estados = cargar_json("estados.json")
    estado  = estados.get(numero, "esperando_respuesta")

    print(f"📊 Estado de {numero}: {estado} | texto: {texto} | button_id: {button_id}")

    # ── Respuesta via botón de la plantilla (Sí / No) ──
    if button_id in ["si", "no", "sí"] or (texto and texto.upper() in ["SÍ", "SI", "NO"]):

        if estado in ["esperando_respuesta", "esperando_cambio"]:
            # Determinar valor desde botón o texto
            if button_id:
                valor = "SÍ" if button_id in ["si", "sí"] else "NO"
            else:
                valido, valor = validar_respuesta(texto, PREGUNTA_ACTIVA)
                if not valido:
                    enviar_texto(numero, f"❌ Respuesta no válida.\n\n{mensaje_formato(PREGUNTA_ACTIVA)}")
                    return

            guardar_voto(numero, valor)
            estados[numero] = "confirmado"
            guardar_json("estados.json", estados)
            enviar_botones_cambiar(numero, valor)
            return

    # ── Botón de cambiar respuesta ──
    if button_id == "cambiar_si":
        estados[numero] = "esperando_cambio"
        guardar_json("estados.json", estados)
        enviar_plantilla(numero, PREGUNTA_ACTIVA["plantilla"])
        return

    if button_id == "cambiar_no":
        enviar_texto(numero, "👍 Tu voto se mantiene. ¡Gracias por participar!")
        return

    # ── Estado: esperando respuesta (texto libre) ──
    if estado == "esperando_respuesta":
        valido, valor = validar_respuesta(texto, PREGUNTA_ACTIVA)
        if not valido:
            enviar_texto(numero,
                f"❌ Respuesta no válida.\n\n"
                f"{PREGUNTA_ACTIVA['texto']}\n\n"
                f"{mensaje_formato(PREGUNTA_ACTIVA)}"
            )
            return
        guardar_voto(numero, valor)
        estados[numero] = "confirmado"
        guardar_json("estados.json", estados)
        enviar_botones_cambiar(numero, valor)

    # ── Estado: confirmado, escribe CAMBIAR ──
    elif estado == "confirmado":
        if texto and texto.strip().upper() == "CAMBIAR":
            estados[numero] = "esperando_cambio"
            guardar_json("estados.json", estados)
            enviar_plantilla(numero, PREGUNTA_ACTIVA["plantilla"])
        else:
            enviar_texto(numero, "Tu voto ya está registrado. Escribe *CAMBIAR* si quieres modificarlo.")

    # ── Estado: esperando cambio (texto libre) ──
    elif estado == "esperando_cambio":
        if not texto:
            return
        valido, valor = validar_respuesta(texto, PREGUNTA_ACTIVA)
        if not valido:
            enviar_texto(numero, f"❌ Respuesta no válida.\n\n{mensaje_formato(PREGUNTA_ACTIVA)}")
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
            print("✅ Webhook verificado")
            return challenge, 200
        return "Token inválido", 403

    if request.method == "POST":
        data = request.json
        print(f"\n📩 Payload: {json.dumps(data, indent=2)}")

        try:
            value = data["entry"][0]["changes"][0]["value"]
            if "messages" in value:
                mensaje   = value["messages"][0]
                numero    = mensaje["from"]
                tipo      = mensaje["type"]

                if tipo == "text":
                    texto = mensaje["text"]["body"]
                    procesar_mensaje(numero, texto=texto)

                elif tipo == "interactive":
                    # Respuesta a botón
                    button_id = mensaje["interactive"]["button_reply"]["id"]
                    procesar_mensaje(numero, button_id=button_id)

        except (KeyError, IndexError) as e:
            print(f"⚠️  Error: {e}")

        return "OK", 200


# ═══════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════

@app.route("/votos", methods=["GET"])
def ver_votos():
    votos = cargar_json("votos.json")
    return jsonify({"total": len(votos), "votos": votos})

@app.route("/estados", methods=["GET"])
def ver_estados():
    return jsonify(cargar_json("estados.json"))

@app.route("/resetear/<numero>", methods=["GET"])
def resetear_usuario(numero):
    estados = cargar_json("estados.json")
    votos   = cargar_json("votos.json")
    estados.pop(numero, None)
    votos.pop(numero, None)
    guardar_json("estados.json", estados)
    guardar_json("votos.json", votos)
    return jsonify({"ok": True, "mensaje": f"{numero} reseteado"})

@app.route("/enviar/<numero>", methods=["GET"])
def enviar_pregunta(numero):
    """Envía la pregunta manualmente a un número"""
    enviar_plantilla(numero, PREGUNTA_ACTIVA["plantilla"])
    estados = cargar_json("estados.json")
    estados[numero] = "esperando_respuesta"
    guardar_json("estados.json", estados)
    return jsonify({"ok": True, "mensaje": f"Pregunta enviada a {numero}"})


# ═══════════════════════════════════════════════════════
# INICIO
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Servidor en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
