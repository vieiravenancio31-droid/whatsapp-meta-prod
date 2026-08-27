import os
import requests
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# Use a versão da Graph API configurada no seu app.
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v24.0")


@app.route("/", methods=["GET"])
def home():
    return {
        "status": "online",
        "service": "whatsapp-meta-prod"
    }, 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Webhook verification failed", 403


def send_whatsapp_message(to, text):
    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=20
    )

    print(
        "Resposta da Meta:",
        response.status_code,
        response.text
    )

    return response


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True) or {}

    print("Webhook recebido:", data)

    try:
        value = data["entry"][0]["changes"][0]["value"]

        # Ignora webhooks que são apenas status
        messages = value.get("messages")

        if not messages:
            return "EVENT_RECEIVED", 200

        message = messages[0]

        # Por enquanto responderemos apenas texto
        if message.get("type") != "text":
            return "EVENT_RECEIVED", 200

        sender = message["from"]
        received_text = message["text"]["body"]

        print("Mensagem recebida:", received_text)
        print("Remetente:", sender)

        send_whatsapp_message(
            sender,
            "Olá! Seu assistente de análise Meta Ads está online."
        )

    except Exception as e:
        print("Erro ao processar webhook:", str(e))

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
