import os
import json
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

ADMIN_WHATSAPP_NUMBER = os.getenv("ADMIN_WHATSAPP_NUMBER")

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
# HOME / HEALTH CHECK
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return {
        "status": "online",
        "service": "whatsapp-meta-prod",
        "meta_ads": "read_and_write",
        "campaign_creation": "paused_only",
        "openai": "configured"
    }, 200


# =========================================================
# VERIFICAÇÃO DO WEBHOOK
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

    return "".join(
        char
        for char in text
        if unicodedata.category(char) != "Mn"
    )


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
# WHATSAPP
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
        "RESPOSTA WHATSAPP:",
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
# META ADS — LEITURA
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
        "RESPOSTA META ADS:",
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
# META ADS — CRIAÇÃO DE CAMPANHA
# =========================================================

def create_test_campaign():

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{META_AD_ACCOUNT_ID}/campaigns"
    )

    headers = {
        "Authorization": f"Bearer {META_ADS_ACCESS_TOKEN}"
    }

    payload = {
        "name": "TESTE API WHATSAPP - NAO ATIVAR",

        "objective": "OUTCOME_TRAFFIC",

        "buying_type": "AUCTION",

        # A campanha sempre nasce pausada.
        "status": "PAUSED",

        # Não é campanha de categoria especial.
        "special_ad_categories": json.dumps([]),

        # IMPORTANTE:
        # obrigatório para esse cenário na API atual.
        #
        # false = não haverá compartilhamento
        # automático de orçamento entre conjuntos.
        "is_adset_budget_sharing_enabled": "false"
    }

    response = requests.post(
        url,
        headers=headers,
        data=payload,
        timeout=30
    )

    print(
        "CRIAÇÃO CAMPANHA:",
        response.status_code,
        response.text
    )

    if response.status_code != 200:

        return None, response.text

    result = response.json()

    campaign_id = result.get("id")

    if not campaign_id:

        return None, (
            "Meta retornou sucesso, "
            "mas não retornou ID da campanha."
        )

    return campaign_id, None


# =========================================================
# OPENAI — ANÁLISE
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

REGRAS:

1. Analise exclusivamente os dados fornecidos.
2. Nunca invente números.
3. Nunca invente campanhas.
4. Nunca invente anúncios.
5. Nunca invente conjuntos.
6. Nunca invente causas que os dados não comprovam.
7. Diferencie fatos de hipóteses.
8. Se não houver informação suficiente, diga claramente.
9. Não recomende aumento de orçamento sem evidência.
10. Responda em português do Brasil.

A resposta será enviada por WhatsApp.

Seja direto e organizado.

Estrutura:

📊 RESUMO

✅ PONTOS POSITIVOS

⚠️ PONTOS DE ATENÇÃO

🎯 PRÓXIMA AÇÃO
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
# RELATÓRIO BÁSICO
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
# WEBHOOK — RECEBIMENTO
# =========================================================

@app.route("/webhook", methods=["POST"])
def receive_webhook():

    data = request.get_json(
        silent=True
    ) or {}

    print(
        "WEBHOOK RECEBIDO:",
        data
    )

    try:

        value = (
            data["entry"][0]
            ["changes"][0]
            ["value"]
        )

        # A Meta também manda atualizações de:
        # entregue
        # lido
        # enviado
        #
        # Esses eventos não possuem "messages".
        messages = value.get("messages")

        if not messages:

            return "EVENT_RECEIVED", 200

        message = messages[0]

        # Por enquanto trabalhamos apenas com texto.
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
            "MENSAGEM RECEBIDA:",
            original_text
        )

        print(
            "REMETENTE:",
            sender
        )


        # =================================================
        # CRIAR CAMPANHA — ETAPA DE SOLICITAÇÃO
        # =================================================

        if received_text == "criar campanha teste":

            if sender != ADMIN_WHATSAPP_NUMBER:

                send_whatsapp_message(
                    sender,
                    (
                        "⛔ *ACESSO NEGADO*\n\n"
                        "Comandos de criação de campanhas "
                        "estão restritos ao administrador."
                    )
                )

                return "EVENT_RECEIVED", 200

            send_whatsapp_message(
                sender,
                (
                    "⚠️ *CONFIRMAÇÃO NECESSÁRIA*\n\n"

                    "Será criada uma campanha no Meta Ads.\n\n"

                    "*Nome:*\n"
                    "TESTE API WHATSAPP - NAO ATIVAR\n\n"

                    "*Objetivo:*\n"
                    "Tráfego\n\n"

                    "*Compra:*\n"
                    "Leilão\n\n"

                    "*Status:*\n"
                    "PAUSADA\n\n"

                    "*Orçamento:*\n"
                    "Nenhum\n\n"

                    "*Conjuntos:*\n"
                    "Nenhum\n\n"

                    "*Anúncios:*\n"
                    "Nenhum\n\n"

                    "⚠️ A campanha NÃO ficará veiculando "
                    "e NÃO terá orçamento configurado.\n\n"

                    "Para continuar, responda exatamente:\n\n"

                    "*CONFIRMAR CAMPANHA TESTE*"
                )
            )

            return "EVENT_RECEIVED", 200


        # =================================================
        # CRIAR CAMPANHA — CONFIRMAÇÃO
        # =================================================

        if received_text == "confirmar campanha teste":

            if sender != ADMIN_WHATSAPP_NUMBER:

                send_whatsapp_message(
                    sender,
                    (
                        "⛔ *ACESSO NEGADO*\n\n"
                        "Você não possui autorização "
                        "para criar campanhas."
                    )
                )

                return "EVENT_RECEIVED", 200


            send_whatsapp_message(
                sender,
                "⏳ Criando campanha PAUSADA no Meta Ads..."
            )


            campaign_id, error = create_test_campaign()


            if error:

                print(
                    "ERRO AO CRIAR CAMPANHA:",
                    error
                )

                send_whatsapp_message(
                    sender,
                    (
                        "❌ *NÃO CONSEGUI CRIAR A CAMPANHA*\n\n"
                        "A Meta recusou a operação.\n\n"
                        "Verifique os logs do Railway."
                    )
                )

                return "EVENT_RECEIVED", 200


            send_whatsapp_message(
                sender,
                (
                    "✅ *CAMPANHA CRIADA COM SUCESSO*\n\n"

                    "*Nome:*\n"
                    "TESTE API WHATSAPP - NAO ATIVAR\n\n"

                    "*Objetivo:*\n"
                    "Tráfego\n\n"

                    "*Status:*\n"
                    "PAUSADA\n\n"

                    f"*Campaign ID:*\n"
                    f"{campaign_id}\n\n"

                    "Nenhum conjunto foi criado.\n"
                    "Nenhum anúncio foi criado.\n"
                    "Nenhum orçamento foi configurado.\n\n"

                    "✅ Portanto, não há possibilidade "
                    "de começar a gastar."
                )
            )

            return "EVENT_RECEIVED", 200


        # =================================================
        # RELATÓRIO — ÚLTIMOS 7 DIAS
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

                print(
                    "ERRO META ADS:",
                    error
                )

                send_whatsapp_message(
                    sender,
                    (
                        "Não consegui consultar "
                        "os dados do Meta Ads."
                    )
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

                print(
                    "ERRO META ADS:",
                    error
                )

                send_whatsapp_message(
                    sender,
                    (
                        "Não consegui consultar "
                        "os dados da conta."
                    )
                )

                return "EVENT_RECEIVED", 200


            send_whatsapp_message(
                sender,
                "Dados encontrados. Analisando..."
            )


            try:

                analysis = analyze_meta_report(
                    report,
                    original_text
                )


                send_whatsapp_message(
                    sender,
                    analysis
                )


            except Exception as e:

                print(
                    "ERRO OPENAI:",
                    repr(e)
                )

                send_whatsapp_message(
                    sender,
                    (
                        "Os dados do Meta Ads foram "
                        "consultados corretamente, "
                        "mas ocorreu um erro "
                        "na análise com IA."
                    )
                )


            return "EVENT_RECEIVED", 200


        # =================================================
        # MENU
        # =================================================

        send_whatsapp_message(
            sender,
            (
                "🤖 *ASSISTENTE META ADS*\n\n"

                "*Consultas disponíveis:*\n\n"

                "📊 gasto últimos 7 dias\n\n"

                "🤖 analise meus últimos 7 dias\n\n"

                "━━━━━━━━━━━━━━\n\n"

                "🔐 *ADMINISTRADOR*\n\n"

                "criar campanha teste"
            )
        )


    except Exception as e:

        print(
            "ERRO AO PROCESSAR WEBHOOK:",
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
