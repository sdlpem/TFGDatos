from flask import Flask, request, jsonify
import os
import json
from datetime import datetime

app = Flask(__name__)

# Token de verificación (lo defines tú, debe coincidir con el que pongas en Meta)
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "mi_token_secreto_123")


# ─────────────────────────────────────────
# WEBHOOK: Meta lo llama aquí
# ─────────────────────────────────────────

@app.route("/webhook", methods=["GET", "POST"])
def webhook():

    # GET → Meta verifica que el webhook existe
    if request.method == "GET":
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("✅ Webhook verificado correctamente")
            return challenge, 200
        else:
            print("❌ Token de verificación incorrecto")
            return "Token inválido", 403

    # POST → Llega un mensaje de un empresario
    if request.method == "POST":
        data = request.json
        print(f"\n📩 Mensaje recibido: {json.dumps(data, indent=2)}")

        try:
            # Extraemos el mensaje
            entry   = data["entry"][0]
            changes = entry["changes"][0]
            value   = changes["value"]

            if "messages" in value:
                mensaje = value["messages"][0]
                numero  = mensaje["from"]
                tipo    = mensaje["type"]

                if tipo == "text":
                    texto = mensaje["text"]["body"]
                    hora  = datetime.fromtimestamp(int(mensaje["timestamp"])).strftime("%H:%M %d/%m/%Y")
                    
                    print(f"📱 De: {numero}")
                    print(f"💬 Texto: '{texto}'")
                    print(f"🕐 Hora: {hora}")

                    # Aquí guardaremos el voto más adelante
                    guardar_voto(numero, texto, hora)

        except (KeyError, IndexError) as e:
            print(f"⚠️  Error procesando mensaje: {e}")

        return "OK", 200


# ─────────────────────────────────────────
# GUARDAR VOTO (por ahora en un archivo)
# ─────────────────────────────────────────

def guardar_voto(numero, texto, hora):
    voto = {
        "numero": numero,
        "respuesta": texto,
        "hora": hora
    }

    # Cargamos votos existentes
    try:
        with open("votos.json", "r") as f:
            votos = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        votos = []

    # Añadimos el nuevo voto
    votos.append(voto)

    # Guardamos
    with open("votos.json", "w") as f:
        json.dump(votos, f, indent=2, ensure_ascii=False)

    print(f"✅ Voto guardado: {numero} → {texto}")


# ─────────────────────────────────────────
# VER VOTOS (para comprobar que funciona)
# ─────────────────────────────────────────

@app.route("/votos", methods=["GET"])
def ver_votos():
    try:
        with open("votos.json", "r") as f:
            votos = json.load(f)
        return jsonify({"total": len(votos), "votos": votos})
    except FileNotFoundError:
        return jsonify({"total": 0, "votos": []})


# ─────────────────────────────────────────
# INICIO
# ─────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Servidor arrancando en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
