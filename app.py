from flask import Flask, request, jsonify
import os
import json
import requests
from datetime import datetime

app = Flask(__name__)

VERIFY_TOKEN   = os.environ.get("VERIFY_TOKEN", "mi_token_secreto_123")
WA_TOKEN       = os.environ.get("WA_TOKEN", "")        # Token de Meta
WA_PHONE_ID    = os.environ.get("WA_PHONE_ID", "")     # ID del número en Meta


# ═══════════════════════════════════════════════════════
# PREGUNTA ACTIVA
# Define aquí la pregunta actual y su tipo de validación
# ═══════════════════════════════════════════════════════

PREGUNTA_ACTIVA = {
    "texto": "¿Crees que subirá el PIB español en 2026?",
    "tipo": "sino",       # Opciones: "sino", "porcentaje", "numero"
    "min": None,          # Solo para tipo "numero" o "porcentaje"
    "max": None,          # Solo para tipo "numero" o "porcentaje"
}

# Ejemplos de otros tipos:
# {"texto": "¿Qué % crees que crecerá el PIB?", "tipo": "porcentaje", "min": -10, "max": 10}
# {"texto": "¿Cuántos empleos se crearán (en miles)?", "tipo": "numero", "min": 0, "max": 500}


# ═══════════════════════════════════════════════════════
# ESTADOS DE CONVERSACIÓN
# Guarda en qué punto está cada empresario
# ═══════════════════════════════════════════════════════

def cargar_estados():
    try:
        with open("estados.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def guardar_estados(estados):
    with open("estados.json", "w") as f:
        json.dump(estados, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# VOTOS
# ═══════════════════════════════════════════════════════

def cargar_votos():
    try:
        with open("votos.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def guardar_voto(numero, respuesta):
    votos = cargar_votos()
    votos[numero] = {
        "respuesta": respuesta,
        "hora": datetime.now().strftime("%H:%M %d/%m/%Y")
    }
    with open("votos.json", "w") as f:
        json.dump(votos, f, indent=2, ensure_ascii=False)
    print(f"✅ Voto guardado: {numero} → {respuesta}")


# ═══════════════════════════════════════════════════════
# VALIDACIÓN DE RESPUESTAS
# ═══════════════════════════════════════════════════════

def validar_respuesta(texto, pregunta):
    texto = texto.strip().upper()
    tipo  = pregunta["tipo"]

    if tipo == "sino":
        if texto in ["SI", "SÍ", "S", "NO", "N"]:
            return True, normalizar_sino(texto)
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

def normalizar_sino(texto):
    return "SÍ" if texto in ["SI", "SÍ", "S"] else "NO"

def mensaje_formato(pregunta):
    tipo = pregunta["tipo"]
    if tipo == "sino":
        return "Responde *SÍ* o *NO*"
    if tipo == "porcentaje":
        mn = pregunta.get("min", 0)
        mx = pregunta.get("max", 100)
        return f"Responde con un porcentaje entre {mn}% y {mx}% (ejemplo: 3.5)"
    if tipo == "numero":
        mn = pregunta.get("min")
        mx = pregunta.get("max")
        if mn is not None and mx is not None:
            return f"Responde con un número entre {mn} y {mx}"
        return "Responde con un número"


# ═══════════════════════════════════════════════════════
# ENVIAR MENSAJE
# ═══════════════════════════════════════════════════════

def enviar_mensaje(numero, texto):
    if not WA_TOKEN or not WA_PHONE_ID:
        print(f"⚠️  Sin credenciales. Mensaje que se enviaría a {numero}: {texto}")
        return

    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": texto}
    }
    r = requests.post(url, headers=headers, json=body)
    print(f"📤 Mensaje enviado a {numero}: {r.status_code}")


# ═══════════════════════════════════════════════════════
# LÓGICA PRINCIPAL DE CONVERSACIÓN
# ═══════════════════════════════════════════════════════

def procesar_mensaje(numero, texto):
    estados = cargar_estados()
    estado  = estados.get(numero, "esperando_respuesta")
    texto_u = texto.strip().upper()

    print(f"📊 Estado actual de {numero}: {estado}")

    # ── Esperando respuesta inicial ──
    if estado == "esperando_respuesta":
        valido, valor = validar_respuesta(texto, PREGUNTA_ACTIVA)

        if not valido:
            enviar_mensaje(numero,
                f"❌ Respuesta no válida.\n\n"
                f"{PREGUNTA_ACTIVA['texto']}\n\n"
                f"{mensaje_formato(PREGUNTA_ACTIVA)}"
            )
            return

        guardar_voto(numero, valor)
        estados[numero] = "confirmado"
        guardar_estados(estados)

        enviar_mensaje(numero,
            f"✅ ¡Gracias! Tu respuesta *{valor}* ha sido registrada.\n\n"
            f"Si quieres cambiarla, escribe *CAMBIAR*."
        )

    # ── Ya confirmado, esperando si quiere cambiar ──
    elif estado == "confirmado":
        if texto_u == "CAMBIAR":
            estados[numero] = "esperando_cambio"
            guardar_estados(estados)
            enviar_mensaje(numero,
                f"De acuerdo. Dime tu nueva respuesta.\n\n"
                f"{PREGUNTA_ACTIVA['texto']}\n\n"
                f"{mensaje_formato(PREGUNTA_ACTIVA)}"
            )
        else:
            enviar_mensaje(numero,
                "Tu voto ya está registrado. Si quieres cambiarlo, escribe *CAMBIAR*."
            )

    # ── Esperando nueva respuesta tras CAMBIAR ──
    elif estado == "esperando_cambio":
        valido, valor = validar_respuesta(texto, PREGUNTA_ACTIVA)

        if not valido:
            enviar_mensaje(numero,
                f"❌ Respuesta no válida.\n\n"
                f"{mensaje_formato(PREGUNTA_ACTIVA)}"
            )
            return

        guardar_voto(numero, valor)
        estados[numero] = "confirmado"
        guardar_estados(estados)

        enviar_mensaje(numero,
            f"✅ ¡Perfecto! Tu nueva respuesta *{valor}* ha sido registrada."
        )


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
        print(f"\n📩 Payload recibido: {json.dumps(data, indent=2)}")

        try:
            value = data["entry"][0]["changes"][0]["value"]
            if "messages" in value:
                mensaje = value["messages"][0]
                numero  = mensaje["from"]
                tipo    = mensaje["type"]

                if tipo == "text":
                    texto = mensaje["text"]["body"]
                    print(f"📱 De: {numero} | Texto: '{texto}'")
                    procesar_mensaje(numero, texto)

        except (KeyError, IndexError) as e:
            print(f"⚠️  Error: {e}")

        return "OK", 200


# ═══════════════════════════════════════════════════════
# ENDPOINTS DE CONSULTA
# ═══════════════════════════════════════════════════════

@app.route("/votos", methods=["GET"])
def ver_votos():
    votos = cargar_votos()
    return jsonify({"total": len(votos), "votos": votos})

@app.route("/estados", methods=["GET"])
def ver_estados():
    return jsonify(cargar_estados())

@app.route("/resetear/<numero>", methods=["GET"])
def resetear_usuario(numero):
    estados = cargar_estados()
    if numero in estados:
        del estados[numero]
        guardar_estados(estados)
    return jsonify({"ok": True, "mensaje": f"{numero} reseteado"})


# ═══════════════════════════════════════════════════════
# INICIO
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Servidor en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
