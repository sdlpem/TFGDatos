# 📊 Mercado de Predicciones — Webhook WhatsApp

Servidor que recibe y guarda las respuestas de los empresarios vía WhatsApp Business API.

---

## 🚀 Pasos para ponerlo en marcha

### 1. Subir a GitHub
- Crea un repositorio nuevo en github.com (puede ser privado)
- Sube estos archivos tal cual

### 2. Desplegar en Render
- Ve a render.com y crea una cuenta gratuita
- Haz clic en **New → Web Service**
- Conecta tu repositorio de GitHub
- Render detectará el `render.yaml` automáticamente
- Haz clic en **Deploy**
- En 2-3 minutos tendrás una URL pública: `https://whatsapp-predicciones.onrender.com`

### 3. Configurar el Webhook en Meta
- Ve a Meta for Developers → Tu app → WhatsApp → Configuración
- En **Webhook URL** pon: `https://tu-url.onrender.com/webhook`
- En **Verify Token** pon: `mi_token_secreto_123`
- Haz clic en **Verificar y guardar**

### 4. Suscribirse a mensajes
- En la misma página, activa la suscripción a **messages**

---

## 🔐 Cambiar el token secreto

En Render, ve a **Environment Variables** y cambia:
```
VERIFY_TOKEN = lo_que_quieras_pero_recuerdalo
```
El mismo valor debe ir en Meta al configurar el webhook.

---

## 📡 Endpoints disponibles

| Endpoint | Método | Para qué sirve |
|---|---|---|
| `/webhook` | GET | Meta verifica que el servidor existe |
| `/webhook` | POST | Recibe mensajes de los empresarios |
| `/votos` | GET | Ver todos los votos recibidos |

---

## 🧪 Comprobar que funciona

Una vez desplegado, abre en el navegador:
```
https://tu-url.onrender.com/votos
```
Debería devolver:
```json
{"total": 0, "votos": []}
```
Cuando alguien envíe un mensaje, aparecerá aquí.

---

## 📁 Estructura del proyecto

```
whatsapp-predicciones/
├── app.py            ← El servidor principal
├── requirements.txt  ← Dependencias Python
├── render.yaml       ← Configuración de Render
└── README.md         ← Esta guía
```
