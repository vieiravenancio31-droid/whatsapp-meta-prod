import os
import unicodedata
import requests

from flask import Flask, request
from openai import OpenAI


# =========================================================
# APP
# =========================================================

app = Flask(__name__)


# =========================================================
# VARIÁVEIS DE AMBIENTE
# =========================================================

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

META_ADS_ACCESS_TOKEN = os.getenv("META_ADS_ACCESS_TOKEN")
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-5.6-luna"
)

GRAPH_API_VERSION = os.getenv(
    "GRAPH_API_VERSION",
    "v24.0"
)


# =========================================================
# OPENAI
# =========================================================

openai_client = OpenAI(
    api_key=OPENAI_API_KEY
)


# =========================================================
# ROTA DE TESTE
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return {
        "status": "online",
        "service": "whatsapp-meta-prod",
        "meta_ads": "read_only",
        "openai": "configured"
    }, 200


# =========================================================
# VERIFICAÇÃO DO WEBHOOK DA META
# =========================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if (
        mode == "subscribe"
        and token == VERIFY_TOKEN
    ):
        return challenge, 200

    return "Webhook verification failed", 403


# =========================================================
# UTILIDADES
# =========================================================

def normalize_text(text):

    text = text.strip().lower()

    text = unicodedata.normalize(
        "NFD",
        text
    )

    text = "".join(
        char
        for char in text
        if unicodedata.category(char) != "Mn"
    )

    return text


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


def format_number(value):

    return f"{int(value):,}".replace(",", ".")


# =========================================================
# ENVIO DE MENSAGEM PELO WHATSAPP
# =========================================================

def send_whatsapp_message(to, text):

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{PHONE_NUMBER_ID}/messages"
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

    print(
        "Resposta WhatsApp:",
        response.status_code,
        response.text
    )

    return response


# =========================================================
# IDENTIFICAÇÃO DE LEADS
# =========================================================

def get_leads(actions):

    if not actions:
        return 0

    actions_dict = {}

    for item in actions:

        action_type = item.get("action_type")

        try:
            value = float(
                item.get("value", 0)
            )
        except (TypeError, ValueError):
            value = 0

        actions_dict[action_type] = value

    # Mantemos uma prioridade.
    # Não somamos todos para evitar duplicidade.
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


# =========================================================
# META ADS — RELATÓRIO ÚLTIMOS 7 DIAS
# =========================================================

def get_last_7_days_report():

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{META_AD_ACCOUNT_ID}/insights"
    )

    params = {
        "access_token": META_ADS_ACCESS_TOKEN,
        "fields": (
            "spend,"
            "impressions,"
            "clicks,"
            "actions"
        ),
        "date_preset": "last_7d",
        "level": "account"
    }

    response = requests.get(
        url,
        params=params,
        timeout=30
    )

    print(
        "Resposta Meta Ads:",
        response.status_code,
        response.text
    )

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

    spend = float(
        row.get("spend", 0)
    )

    impressions = int(
        row.get("impressions", 0)
    )

    clicks = int(
        row.get("clicks", 0)
    )

    leads = get_leads(
        row.get("actions", [])
    )

    if leads > 0:
        cpl = spend / leads
    else:
        cpl = 0

    return {
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "leads": leads,
        "cpl": cpl
    }, None


# =========================================================
# OPENAI — ANÁLISE DOS DADOS
# =========================================================

def analyze_meta_report(
    report,
    user_question
):

    dados = f"""
PERÍODO:
Últimos 7 dias

INVESTIMENTO:
R$ {report['spend']:.2f}

IMPRESSÕES:
{report['impressions']}

CLIQUES:
{report['clicks']}

LEADS:
{report['leads']}

CPL:
R$ {report['cpl']:.2f}
"""

    response = openai_client.responses.create(

        model=OPENAI_MODEL,

        reasoning={
            "effort": "low"
        },

        max_output_tokens=700,

        instructions="""
Você é um analista sênior especializado em Meta Ads.

Você está analisando dados reais de uma conta de anúncios.

REGRAS IMPORTANTES:

1. Analise exclusivamente os dados fornecidos.
2. Nunca invente números.
3. Nunca invente campanhas, anúncios ou conjuntos.
4. Nunca afirme uma causa que os dados não comprovam.
5. Se não houver informação suficiente para concluir algo, diga isso claramente.
6. Não recomende aumentar orçamento sem evidência suficiente.
7. Não diga que determinado criativo é ruim se não recebeu dados de criativo.
8. Não diga que uma campanha é vencedora se não recebeu dados por campanha.
9. Diferencie fato de hipótese.
10. Responda sempre em português do Brasil.

A resposta será enviada pelo WhatsApp.

Use texto simples, organizado e direto.

Estrutura preferencial:

📊 RESUMO

✅ PONTOS POSITIVOS

⚠️ PONTOS DE ATENÇÃO

🎯 PRÓXIMA AÇÃO

Não transforme a resposta em um texto longo.
Priorize clareza, números e ação.
""",

        input=f"""
PERGUNTA DO USUÁRIO:

{user_question}


DADOS REAIS DO META ADS:

{dados}
"""
    )

    return response.output_text


# =========================================================
# RELATÓRIO FORMATADO SEM IA
# =========================================================

def build_basic_report(report):

    text = (
        "📊 *META ADS — ÚLTIMOS 7 DIAS*\n\n"

        f"💰 Investimento: "
        f"{format_brl(report['spend'])}\n"

        f"👁 Impressões: "
        f"{format_number(report['impressions'])}\n"

        f"🖱 Cliques: "
        f"{format_number(report['clicks'])}\n"

        f"🎯 Leads: "
        f"{int(report['leads'])}\n"
    )

    if report["leads"] > 0:

        text += (
            f"💵 CPL: "
            f"{format_brl(report['cpl'])}"
        )

    else:

        text += (
            "💵 CPL: "
            "sem leads registrados"
        )

    return text


# =========================================================
# RECEBIMENTO DE MENSAGENS
# =========================================================

@app.route("/webhook", methods=["POST"])
def receive_webhook():

    data = request.get_json(
        silent=True
    ) or {}

    print(
        "Webhook recebido:",
        data
    )

    try:

        value = (
            data["entry"][0]
            ["changes"][0]
            ["value"]
        )

        # Eventos de status do WhatsApp
        # não precisam ser processados.
        messages = value.get("messages")

        if not messages:

            return "EVENT_RECEIVED", 200

        message = messages[0]

        # Por enquanto trabalhamos
        # somente com mensagens de texto.
        if message.get("type") != "text":

            return "EVENT_RECEIVED", 200

        sender = message["from"]

        original_text = (
            message["text"]["body"]
            .strip()
        )

        received_text = normalize_text(
            original_text
        )

        print(
            "Mensagem recebida:",
            original_text
        )

        print(
            "Remetente:",
            sender
        )


        # =================================================
        # COMANDO 1
        # RELATÓRIO NUMÉRICO
        # =================================================

        if (
            "gasto" in received_text
            and "7" in received_text
        ):

            send_whatsapp_message(
                sender,
                "Consultando sua conta de anúncios..."
            )

            report, error = (
                get_last_7_days_report()
            )

            if error:

                send_whatsapp_message(
                    sender,
                    "Não consegui consultar o Meta Ads. "
                    "Verifique os logs do servidor."
                )

                return "EVENT_RECEIVED", 200

            resposta = build_basic_report(
                report
            )

            send_whatsapp_message(
                sender,
                resposta
            )

            return "EVENT_RECEIVED", 200


        # =================================================
        # COMANDO 2
        # ANÁLISE COM IA
        # =================================================

        if (
            "analise" in received_text
            and "7" in received_text
        ):

            send_whatsapp_message(
                sender,
                "Consultando os dados do Meta Ads..."
            )

            report, error = (
                get_last_7_days_report()
            )

            if error:

                send_whatsapp_message(
                    sender,
                    "Não consegui consultar os dados "
                    "da sua conta do Meta Ads."
                )

                return "EVENT_RECEIVED", 200


            send_whatsapp_message(
                sender,
                "Dados encontrados. "
                "Estou analisando o desempenho..."
            )


            try:

                analysis = (
                    analyze_meta_report(
                        report,
                        original_text
                    )
                )

                send_whatsapp_message(
                    sender,
                    analysis
                )

            except Exception as e:

                print(
                    "Erro OpenAI:",
                    repr(e)
                )

                send_whatsapp_message(
                    sender,
                    "Os dados do Meta Ads foram "
                    "consultados corretamente, "
                    "mas ocorreu um erro ao gerar "
                    "a análise com IA."
                )

            return "EVENT_RECEIVED", 200


        # =================================================
        # MENSAGEM PADRÃO
        # =================================================

        send_whatsapp_message(

            sender,

            (
                "🤖 *Assistente Meta Ads online*\n\n"

                "Você pode testar:\n\n"

                "1️⃣ *gasto últimos 7 dias*\n"
                "Recebe os números da conta.\n\n"

                "2️⃣ *analise meus últimos 7 dias*\n"
                "Recebe uma análise com IA."
            )
        )


    except Exception as e:

        print(
            "Erro ao processar webhook:",
            repr(e)
        )


    return "EVENT_RECEIVED", 200


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            8080
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
