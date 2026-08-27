import os
import requests
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

META_ADS_ACCESS_TOKEN = os.getenv("META_ADS_ACCESS_TOKEN")
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID")

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
        timeout=30
    )

    print("Resposta WhatsApp:", response.status_code, response.text)

    return response


def format_brl(value):
    value = float(value)

    formatted = f"{value:,.2f}"

    formatted = (
        formatted
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )

    return f"R$ {formatted}"


def get_leads(actions):
    if not actions:
        return 0

    actions_dict = {
        item.get("action_type"): float(item.get("value", 0))
        for item in actions
    }

    # Prioridade para evitar contar o mesmo lead duas vezes
    lead_types = [
        "onsite_conversion.lead_grouped",
        "lead",
        "offsite_conversion.fb_pixel_lead",
        "omni_lead"
    ]

    for lead_type in lead_types:
        if lead_type in actions_dict:
            return actions_dict[lead_type]

    return 0


def get_last_7_days_report():
    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/{META_AD_ACCOUNT_ID}/insights"
    )

    params = {
        "access_token": META_ADS_ACCESS_TOKEN,
        "fields": "spend,impressions,clicks,actions",
        "date_preset": "last_7d",
        "level": "account"
    }

    response = requests.get(
        url,
        params=params,
        timeout=30
    )

    print("Resposta Meta Ads:", response.status_code, response.text)

    if response.status_code != 200:
        return None, response.text

    result = response.json()

    if not result.get("data"):
        return {
            "spend": 0,
            "impressions": 0,
            "clicks": 0,
            "leads": 0,
            "cpl": 0
        }, None

    row = result["data"][0]

    spend = float(row.get("spend", 0))
    impressions = int(row.get("impressions", 0))
    clicks = int(row.get("clicks", 0))
    leads = get_leads(row.get("actions", []))

    cpl = spend / leads if leads > 0 else 0

    return {
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "leads": leads,
        "cpl": cpl
    }, None


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True) or {}

    print("Webhook recebido:", data)

    try:
        value = data["entry"][0]["changes"][0]["value"]

        messages = value.get("messages")

        # Ignora atualizações de status
        if not messages:
            return "EVENT_RECEIVED", 200

        message = messages[0]

        # Por enquanto aceita apenas texto
        if message.get("type") != "text":
            return "EVENT_RECEIVED", 200

        sender = message["from"]

        received_text = (
            message["text"]["body"]
            .strip()
            .lower()
        )

        print("Mensagem:", received_text)

        if "gasto" in received_text and "7" in received_text:

            send_whatsapp_message(
                sender,
                "Consultando sua conta de anúncios..."
            )

            report, error = get_last_7_days_report()

            if error:
                send_whatsapp_message(
                    sender,
                    "Não consegui consultar o Meta Ads. "
                    "Verifique os logs do servidor."
                )

                return "EVENT_RECEIVED", 200

            resposta = (
                "📊 *META ADS — ÚLTIMOS 7 DIAS*\n\n"
                f"💰 Investimento: {format_brl(report['spend'])}\n"
                f"👁 Impressões: {report['impressions']:,}\n"
                f"🖱 Cliques: {report['clicks']:,}\n"
                f"🎯 Leads: {int(report['leads'])}\n"
            )

            if report["leads"] > 0:
                resposta += (
                    f"💵 CPL: {format_brl(report['cpl'])}"
                )
            else:
                resposta += "💵 CPL: sem leads registrados"

            send_whatsapp_message(sender, resposta)

        else:

            send_whatsapp_message(
                sender,
                "Seu assistente Meta Ads está online.\n\n"
                "Teste agora:\n"
                "*gasto últimos 7 dias*"
            )

    except Exception as e:
        print("Erro ao processar webhook:", str(e))

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))

    app.run(
        host="0.0.0.0",
        port=port
    )
