import os
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")


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


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json()

    print("Webhook recebido:", data)

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
