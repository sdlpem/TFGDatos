from flask import Flask, request, jsonify, send_from_directory, session, redirect
import os, json, requests, hashlib
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "clave_secreta_cambiar")

VERIFY_TOKEN  = os.environ.get("VERIFY_TOKEN", "mi_token_secreto_123")
WA_TOKEN      = os.environ.get("WA_TOKEN", "")
WA_PHONE_ID   = os.environ.get("WA_PHONE_ID", "")
CECO_USER     = os.environ.get("CECO_USER", "ceco")
CECO_PASS     = os.environ.get("CECO_PASS", "ceco1234")
RESULTADOS_URL = os.environ.get("RESULTADOS_URL", "https://tfgdatos.onrender.com/resultados")

DATA_DIR = os.environ.get("DATA_DIR", "/tmp")


# ═══════════════════════════════════════════════════
# UTILIDADES
# ═══════════════════════════════════════════════════

def cargar_json(f):
    try:
        with open(os.path.join(DATA_DIR, f)) as fp:
            return json.load(fp)
    except:
        return {}

def guardar_json(f, d):
    with open(os.path.join(DATA_DIR, f), "w") as fp:
        json.dump(d, fp, indent=2, ensure_ascii=False)

def cargar_encuesta():
    e = cargar_json("encuesta.json")
    if not e:
        return {
            "texto": "",
            "tipo": "sino",
            "plantilla_sino": os.environ.get("PLANTILLA_SINO", "pregunta_sino"),
            "plantilla_abierta": os.environ.get("PLANTILLA_ABIERTA", "pregunta_abierta"),
            "min": None, "max": None,
            "cierre": None,
            "activa": False
        }
    return e

def encuesta_abierta():
    e = cargar_encuesta()
    if not e.get("activa"):
        return False
    if e.get("cierre"):
        try:
            cierre = datetime.fromisoformat(e["cierre"])
            if datetime.now() > cierre:
                return False
        except:
            pass
    return True

def guardar_voto(numero, respuesta):
    votos = cargar_json("votos.json")
    votos[numero] = {"respuesta": respuesta, "hora": datetime.now().strftime("%H:%M %d/%m/%Y")}
    guardar_json("votos.json", votos)
    print(f"✅ Voto: {numero} → {respuesta}")

def resumen_votos():
    votos = cargar_json("votos.json")
    total = len(votos)
    conteo = {}
    for v in votos.values():
        r = v["respuesta"]
        conteo[r] = conteo.get(r, 0) + 1
    return {"total": total, "conteo": conteo}


# ═══════════════════════════════════════════════════
# WHATSAPP — ENVÍO
# ═══════════════════════════════════════════════════

def enviar_texto(numero, texto):
    if not WA_TOKEN or not WA_PHONE_ID:
        print(f"⚠️ Sin creds: {texto}"); return
    r = requests.post(
        f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": numero, "type": "text", "text": {"body": texto}}
    )
    print(f"📤 Texto {numero}: {r.status_code}")

def enviar_plantilla(numero, encuesta):
    if not WA_TOKEN or not WA_PHONE_ID:
        print("⚠️ Sin creds plantilla"); return

    tipo   = encuesta.get("tipo", "sino")
    nombre = encuesta.get("plantilla_sino", "plantilla_dinamica") if tipo == "sino" else encuesta.get("plantilla_abierta", "plantilla_dinamica")
    formato = "Responda con SÍ o NO" if tipo == "sino" else formato_instrucciones(encuesta)

    componentes = [{
        "type": "body",
        "parameters": [
            {"type": "text", "parameter_name": "pregunta",          "text": encuesta["texto"]},
            {"type": "text", "parameter_name": "formato_respuesta", "text": formato}
        ]
    }]

    r = requests.post(
        f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"},
        json={
            "messaging_product": "whatsapp", "to": numero,
            "type": "template",
            "template": {"name": nombre, "language": {"code": "es"}, "components": componentes}
        }
    )
    print(f"📤 Plantilla '{nombre}' → {numero}: {r.status_code} {r.text}")

def enviar_confirmacion(numero, valor):
    """Mensaje tras votar: resultado + link de resultados"""
    if not WA_TOKEN or not WA_PHONE_ID:
        return
    texto = (
        f"✅ Tu respuesta *{valor}* ha sido registrada.\n\n"
        f"Escribe *CAMBIAR* en cualquier momento para modificarla.\n\n"
        f"📊 Ve cómo están votando los demás:\n{RESULTADOS_URL}"
    )
    enviar_texto(numero, texto)


# ═══════════════════════════════════════════════════
# VALIDACIÓN
# ═══════════════════════════════════════════════════

def validar(texto, encuesta):
    t = texto.strip().upper().replace("Í","I").replace("É","E").replace("Á","A").replace("Ó","O").replace("Ú","U")
    tipo = encuesta.get("tipo", "sino")
    if tipo == "sino":
        if t in ["SI", "S"]: return True, "SÍ"
        if t in ["NO", "N"]: return True, "NO"
        return False, None
    if tipo == "porcentaje":
        try:
            v = float(texto.strip().replace("%","").replace(",","."))
            mn = encuesta.get("min") or 0
            mx = encuesta.get("max") or 100
            return (mn <= v <= mx, f"{v}%") if mn <= v <= mx else (False, None)
        except: return False, None
    if tipo == "numero":
        try:
            v = float(texto.strip().replace(",","."))
            mn, mx = encuesta.get("min"), encuesta.get("max")
            if mn is not None and v < mn: return False, None
            if mx is not None and v > mx: return False, None
            return True, str(v)
        except: return False, None
    return False, None

def formato_instrucciones(encuesta):
    tipo = encuesta.get("tipo")
    if tipo == "porcentaje":
        mn = encuesta.get("min", 0)
        mx = encuesta.get("max", 100)
        return f"con un porcentaje entre {mn}% y {mx}% (ejemplo: 3.5)"
    if tipo == "numero":
        mn, mx = encuesta.get("min"), encuesta.get("max")
        if mn is not None and mx is not None:
            return f"con un número entre {mn} y {mx}"
        return "con un número"
    return ""


# ═══════════════════════════════════════════════════
# CONVERSACIÓN
# ═══════════════════════════════════════════════════

def procesar(numero, texto=None, button_id=None):
    estados  = cargar_json("estados.json")
    estado   = estados.get(numero, "esperando_respuesta")
    encuesta = cargar_encuesta()

    print(f"📊 {numero} | estado: {estado} | texto: {texto} | btn: {button_id}")

    # Botones de cambiar respuesta (interactive)
    if button_id == "cambiar_si":
        if not encuesta_abierta():
            enviar_texto(numero, "⏰ La encuesta ya está cerrada. No es posible cambiar la respuesta.")
            return
        estados[numero] = "esperando_cambio"
        guardar_json("estados.json", estados)
        enviar_plantilla(numero, encuesta)
        return

    if button_id == "cambiar_no":
        enviar_texto(numero, "👍 Tu voto se mantiene. ¡Gracias por participar!")
        return

    # Botón de plantilla Sí/No (llega como button_id con el título)
    if button_id is not None:
        t = button_id.strip().upper().replace("Í","I")
        es_si = t in ["SI", "S"]
        es_no = t in ["NO", "N"]
        if (es_si or es_no) and estado in ["esperando_respuesta", "esperando_cambio"]:
            if not encuesta_abierta():
                enviar_texto(numero, "⏰ La encuesta ya está cerrada.")
                return
            valor = "SÍ" if es_si else "NO"
            guardar_voto(numero, valor)
            estados[numero] = "confirmado"
            guardar_json("estados.json", estados)
            enviar_confirmacion(numero, valor)
            return

    if texto is None:
        return

    # Texto libre
    if estado == "esperando_respuesta":
        if not encuesta_abierta():
            enviar_texto(numero, "⏰ La encuesta ya está cerrada.")
            return
        ok, valor = validar(texto, encuesta)
        if not ok:
            enviar_texto(numero, f"❌ Respuesta no válida.\n\n{encuesta['texto']}\n\nResponde {formato_instrucciones(encuesta) or 'con SÍ o NO'}")
            return
        guardar_voto(numero, valor)
        estados[numero] = "confirmado"
        guardar_json("estados.json", estados)
        enviar_confirmacion(numero, valor)

    elif estado == "confirmado":
        if texto.strip().upper() == "CAMBIAR":
            if not encuesta_abierta():
                enviar_texto(numero, "⏰ La encuesta ya está cerrada. No puedes cambiar tu respuesta.")
                return
            estados[numero] = "esperando_cambio"
            guardar_json("estados.json", estados)
            enviar_plantilla(numero, encuesta)
        else:
            enviar_texto(numero, f"Tu voto ya está registrado. Escribe *CAMBIAR* para modificarlo.\n\n📊 {RESULTADOS_URL}")

    elif estado == "esperando_cambio":
        if not encuesta_abierta():
            enviar_texto(numero, "⏰ La encuesta ya está cerrada.")
            return
        ok, valor = validar(texto, encuesta)
        if not ok:
            enviar_texto(numero, f"❌ Respuesta no válida. Responde {formato_instrucciones(encuesta) or 'con SÍ o NO'}")
            return
        guardar_voto(numero, valor)
        estados[numero] = "confirmado"
        guardar_json("estados.json", estados)
        enviar_confirmacion(numero, valor)


# ═══════════════════════════════════════════════════
# WEBHOOK
# ═══════════════════════════════════════════════════

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Token inválido", 403

    data = request.json
    print(f"\n📩 {json.dumps(data, indent=2)}")
    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" in value:
            msg    = value["messages"][0]
            numero = msg["from"]
            tipo   = msg["type"]
            if tipo == "text":
                procesar(numero, texto=msg["text"]["body"])
            elif tipo == "button":
                procesar(numero, button_id=msg["button"].get("text",""))
            elif tipo == "interactive":
                inter = msg["interactive"]
                if "button_reply" in inter:
                    procesar(numero, button_id=inter["button_reply"].get("id",""))
    except Exception as e:
        print(f"⚠️ Error: {e}")
    return "OK", 200


# ═══════════════════════════════════════════════════
# AUTH CECO
# ═══════════════════════════════════════════════════

def autenticado():
    return session.get("ceco") == True

@app.route("/ceco/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        data = request.json or {}
        if data.get("user") == CECO_USER and data.get("password") == CECO_PASS:
            session["ceco"] = True
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Credenciales incorrectas"}), 401
    return send_from_directory("static", "login.html")

@app.route("/ceco/logout")
def logout():
    session.clear()
    return redirect("/ceco/login")

@app.route("/ceco")
def ceco():
    if not autenticado():
        return redirect("/ceco/login")
    return send_from_directory("static", "ceco.html")

@app.route("/resultados")
def resultados():
    return send_from_directory("static", "resultados.html")


# ═══════════════════════════════════════════════════
# API CECO
# ═══════════════════════════════════════════════════

@app.route("/api/encuesta", methods=["GET"])
def api_get_encuesta():
    if not autenticado(): return jsonify({"error": "No autorizado"}), 401
    return jsonify(cargar_encuesta())

@app.route("/api/encuesta", methods=["POST"])
def api_set_encuesta():
    if not autenticado(): return jsonify({"error": "No autorizado"}), 401
    d = request.json
    encuesta = {
        "texto":    d.get("texto",""),
        "tipo":     d.get("tipo","sino"),
        "plantilla_sino":     d.get("plantilla_sino", "pregunta_sino"),
        "plantilla_abierta":  d.get("plantilla_abierta", "pregunta_abierta"),
        "min":      d.get("min"),
        "max":      d.get("max"),
        "cierre":   d.get("cierre"),
        "activa":   False
    }
    guardar_json("encuesta.json", encuesta)
    return jsonify({"ok": True})

@app.route("/api/lanzar", methods=["POST"])
def api_lanzar():
    if not autenticado(): return jsonify({"error": "No autorizado"}), 401
    d        = request.json
    numeros  = d.get("numeros", [])
    encuesta = cargar_encuesta()
    encuesta["activa"] = True
    guardar_json("encuesta.json", encuesta)
    guardar_json("estados.json", {})
    guardar_json("votos.json", {})

    enviados, errores = 0, []
    for n in numeros:
        n = n.strip().replace(" ","").replace("+","")
        if not n: continue
        try:
            enviar_plantilla(n, encuesta)
            estados = cargar_json("estados.json")
            estados[n] = "esperando_respuesta"
            guardar_json("estados.json", estados)
            enviados += 1
        except Exception as e:
            errores.append({"numero": n, "error": str(e)})

    return jsonify({"ok": True, "enviados": enviados, "errores": errores})

@app.route("/api/cerrar", methods=["POST"])
def api_cerrar():
    if not autenticado(): return jsonify({"error": "No autorizado"}), 401
    e = cargar_encuesta()
    e["activa"] = False
    guardar_json("encuesta.json", e)
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════
# API RESULTADOS (pública)
# ═══════════════════════════════════════════════════

@app.route("/api/resultados")
def api_resultados():
    encuesta = cargar_encuesta()
    res      = resumen_votos()
    return jsonify({
        "pregunta": encuesta.get("texto",""),
        "tipo":     encuesta.get("tipo","sino"),
        "cierre":   encuesta.get("cierre"),
        "activa":   encuesta_abierta(),
        "total":    res["total"],
        "conteo":   res["conteo"]
    })


# ═══════════════════════════════════════════════════
# INICIO
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
