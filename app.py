import os
import json
import unicodedata
import threading
import requests
import psycopg

from flask import Flask, request
from openai import OpenAI
from psycopg.rows import dict_row


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

DATABASE_URL = os.getenv("DATABASE_URL")

GRAPH_API_VERSION = os.getenv(
    "GRAPH_API_VERSION",
    "v24.0"
)

DEFAULT_COMPANY_NAME = os.getenv(
    "DEFAULT_COMPANY_NAME",
    "Empresa Principal"
)


# =========================================================
# OPENAI
# =========================================================

openai_client = OpenAI(
    api_key=OPENAI_API_KEY
)


# =========================================================
# CONTROLE DE INICIALIZAÇÃO DO BANCO
# =========================================================

database_initialized = False
database_lock = threading.Lock()


# =========================================================
# BANCO DE DADOS
# =========================================================

def get_db_connection():

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL não configurada."
        )

    return psycopg.connect(
        DATABASE_URL,
        autocommit=True,
        row_factory=dict_row
    )


def initialize_database():

    global database_initialized

    if database_initialized:
        return

    with database_lock:

        if database_initialized:
            return

        print(
            "Inicializando PostgreSQL..."
        )

        with get_db_connection() as conn:

            with conn.cursor() as cursor:

                # =========================================
                # EMPRESAS
                # =========================================

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS companies (
                        id BIGSERIAL PRIMARY KEY,

                        slug TEXT NOT NULL UNIQUE,

                        name TEXT NOT NULL,

                        active BOOLEAN
                            NOT NULL
                            DEFAULT TRUE,

                        created_at TIMESTAMPTZ
                            NOT NULL
                            DEFAULT NOW()
                    );
                    """
                )


                # =========================================
                # USUÁRIOS
                # =========================================

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGSERIAL PRIMARY KEY,

                        company_id BIGINT
                            NOT NULL
                            REFERENCES companies(id)
                            ON DELETE CASCADE,

                        whatsapp_number TEXT
                            NOT NULL
                            UNIQUE,

                        name TEXT,

                        role TEXT
                            NOT NULL
                            DEFAULT 'user',

                        can_read_ads BOOLEAN
                            NOT NULL
                            DEFAULT TRUE,

                        can_create_campaigns BOOLEAN
                            NOT NULL
                            DEFAULT FALSE,

                        active BOOLEAN
                            NOT NULL
                            DEFAULT TRUE,

                        created_at TIMESTAMPTZ
                            NOT NULL
                            DEFAULT NOW()
                    );
                    """
                )


                # =========================================
                # CONTAS META
                # =========================================

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta_accounts (
                        id BIGSERIAL PRIMARY KEY,

                        company_id BIGINT
                            NOT NULL
                            REFERENCES companies(id)
                            ON DELETE CASCADE,

                        ad_account_id TEXT
                            NOT NULL
                            UNIQUE,

                        name TEXT,

                        active BOOLEAN
                            NOT NULL
                            DEFAULT TRUE,

                        created_at TIMESTAMPTZ
                            NOT NULL
                            DEFAULT NOW()
                    );
                    """
                )


                # =========================================
                # LOGS / AUDITORIA
                # =========================================

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS activity_logs (
                        id BIGSERIAL PRIMARY KEY,

                        company_id BIGINT
                            REFERENCES companies(id)
                            ON DELETE SET NULL,

                        user_id BIGINT
                            REFERENCES users(id)
                            ON DELETE SET NULL,

                        action TEXT NOT NULL,

                        details JSONB,

                        created_at TIMESTAMPTZ
                            NOT NULL
                            DEFAULT NOW()
                    );
                    """
                )


                # =========================================
                # CRIAR EMPRESA INICIAL
                # =========================================

                cursor.execute(
                    """
                    INSERT INTO companies (
                        slug,
                        name
                    )
                    VALUES (
                        'principal',
                        %s
                    )

                    ON CONFLICT (slug)
                    DO UPDATE SET
                        name = EXCLUDED.name

                    RETURNING id;
                    """,
                    (
                        DEFAULT_COMPANY_NAME,
                    )
                )

                company = cursor.fetchone()

                company_id = company["id"]


                # =========================================
                # CADASTRAR USUÁRIO ADMINISTRADOR
                # =========================================

                if ADMIN_WHATSAPP_NUMBER:

                    cursor.execute(
                        """
                        INSERT INTO users (
                            company_id,
                            whatsapp_number,
                            name,
                            role,
                            can_read_ads,
                            can_create_campaigns
                        )

                        VALUES (
                            %s,
                            %s,
                            %s,
                            'admin',
                            TRUE,
                            TRUE
                        )

                        ON CONFLICT (whatsapp_number)
                        DO UPDATE SET

                            company_id =
                                EXCLUDED.company_id,

                            role =
                                'admin',

                            can_read_ads =
                                TRUE,

                            can_create_campaigns =
                                TRUE,

                            active =
                                TRUE;
                        """,
                        (
                            company_id,
                            ADMIN_WHATSAPP_NUMBER,
                            "Administrador"
                        )
                    )


                # =========================================
                # CADASTRAR CONTA META ATUAL
                # =========================================

                if META_AD_ACCOUNT_ID:

                    cursor.execute(
                        """
                        INSERT INTO meta_accounts (
                            company_id,
                            ad_account_id,
                            name
                        )

                        VALUES (
                            %s,
                            %s,
                            %s
                        )

                        ON CONFLICT (ad_account_id)
                        DO UPDATE SET

                            company_id =
                                EXCLUDED.company_id,

                            active =
                                TRUE;
                        """,
                        (
                            company_id,
                            META_AD_ACCOUNT_ID,
                            "Conta Meta Principal"
                        )
                    )


        database_initialized = True

        print(
            "PostgreSQL inicializado com sucesso."
        )


# =========================================================
# BUSCAR USUÁRIO PELO WHATSAPP
# =========================================================

def get_user_context(
    whatsapp_number
):

    initialize_database()

    with get_db_connection() as conn:

        with conn.cursor() as cursor:

            cursor.execute(
                """
                SELECT

                    u.id AS user_id,

                    u.whatsapp_number,

                    u.name AS user_name,

                    u.role,

                    u.can_read_ads,

                    u.can_create_campaigns,

                    c.id AS company_id,

                    c.name AS company_name,

                    m.ad_account_id

                FROM users u

                INNER JOIN companies c
                    ON c.id = u.company_id

                LEFT JOIN meta_accounts m
                    ON m.company_id = c.id
                    AND m.active = TRUE

                WHERE
                    u.whatsapp_number = %s
                    AND u.active = TRUE
                    AND c.active = TRUE

                ORDER BY m.id ASC

                LIMIT 1;
                """,
                (
                    whatsapp_number,
                )
            )

            return cursor.fetchone()


# =========================================================
# LOG DE AUDITORIA
# =========================================================

def log_activity(
    context,
    action,
    details=None
):

    try:

        initialize_database()

        company_id = None
        user_id = None

        if context:

            company_id = context.get(
                "company_id"
            )

            user_id = context.get(
                "user_id"
            )

        with get_db_connection() as conn:

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    INSERT INTO activity_logs (
                        company_id,
                        user_id,
                        action,
                        details
                    )

                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s::jsonb
                    );
                    """,
                    (
                        company_id,
                        user_id,
                        action,
                        json.dumps(
                            details or {}
                        )
                    )
                )

    except Exception as error:

        print(
            "ERRO AO GRAVAR LOG:",
            repr(error)
        )


# =========================================================
# HOME
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return {
        "status": "online",
        "service": "whatsapp-meta-prod",
        "database": "configured",
        "architecture": "multi_company_v1"
    }, 200


# =========================================================
# TESTE DO POSTGRESQL
# =========================================================

@app.route(
    "/db-check",
    methods=["GET"]
)
def database_check():

    try:

        initialize_database()

        with get_db_connection() as conn:

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT

                        (
                            SELECT COUNT(*)
                            FROM companies
                        ) AS companies,

                        (
                            SELECT COUNT(*)
                            FROM users
                        ) AS users,

                        (
                            SELECT COUNT(*)
                            FROM meta_accounts
                        ) AS meta_accounts;
                    """
                )

                result = cursor.fetchone()


        return {
            "status": "database_online",
            "companies": result[
                "companies"
            ],
            "users": result[
                "users"
            ],
            "meta_accounts": result[
                "meta_accounts"
            ]
        }, 200


    except Exception as error:

        print(
            "ERRO DATABASE CHECK:",
            repr(error)
        )

        return {
            "status": "database_error",
            "error": str(error)
        }, 500


# =========================================================
# VERIFICAÇÃO WEBHOOK META
# =========================================================

@app.route(
    "/webhook",
    methods=["GET"]
)
def verify_webhook():

    mode = request.args.get(
        "hub.mode"
    )

    token = request.args.get(
        "hub.verify_token"
    )

    challenge = request.args.get(
        "hub.challenge"
    )

    if (
        mode == "subscribe"
        and token == VERIFY_TOKEN
    ):

        return challenge, 200


    return (
        "Webhook verification failed",
        403
    )


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
        if unicodedata.category(
            char
        ) != "Mn"
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

    return (
        f"R$ {formatted}"
    )


def format_number(value):

    return (
        f"{int(value):,}"
        .replace(",", ".")
    )


# =========================================================
# WHATSAPP
# =========================================================

def send_whatsapp_message(
    to,
    text
):

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization":
            f"Bearer {WHATSAPP_TOKEN}",

        "Content-Type":
            "application/json"
    }

    payload = {

        "messaging_product":
            "whatsapp",

        "recipient_type":
            "individual",

        "to":
            to,

        "type":
            "text",

        "text": {

            "preview_url":
                False,

            "body":
                text
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
# LEADS
# =========================================================

def get_leads(actions):

    if not actions:

        return 0


    actions_dict = {}


    for item in actions:

        action_type = item.get(
            "action_type"
        )

        try:

            value = float(
                item.get(
                    "value",
                    0
                )
            )

        except (
            TypeError,
            ValueError
        ):

            value = 0


        actions_dict[
            action_type
        ] = value


    lead_types = [

        "onsite_conversion.lead_grouped",

        "lead",

        "offsite_conversion.fb_pixel_lead",

        "omni_lead"
    ]


    for lead_type in lead_types:

        if lead_type in actions_dict:

            return actions_dict[
                lead_type
            ]


    return 0


# =========================================================
# META ADS — RELATÓRIO
# =========================================================

def get_last_7_days_report(
    ad_account_id
):

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{ad_account_id}/insights"
    )


    params = {

        "access_token":
            META_ADS_ACCESS_TOKEN,

        "fields":
            "spend,"
            "impressions,"
            "clicks,"
            "actions",

        "date_preset":
            "last_7d",

        "level":
            "account"
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

        return (
            None,
            response.text
        )


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
        row.get(
            "spend",
            0
        )
    )


    impressions = int(
        row.get(
            "impressions",
            0
        )
    )


    clicks = int(
        row.get(
            "clicks",
            0
        )
    )


    leads = get_leads(
        row.get(
            "actions",
            []
        )
    )


    if leads > 0:

        cpl = (
            spend / leads
        )

    else:

        cpl = 0


    return {

        "spend":
            spend,

        "impressions":
            impressions,

        "clicks":
            clicks,

        "leads":
            leads,

        "cpl":
            cpl

    }, None


# =========================================================
# META ADS — CRIAR CAMPANHA TESTE
# =========================================================

def create_test_campaign(
    ad_account_id
):

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{ad_account_id}/campaigns"
    )


    headers = {

        "Authorization":
            f"Bearer "
            f"{META_ADS_ACCESS_TOKEN}"
    }


    payload = {

        "name":
            "TESTE API WHATSAPP - NAO ATIVAR",

        "objective":
            "OUTCOME_TRAFFIC",

        "buying_type":
            "AUCTION",

        "status":
            "PAUSED",

        "special_ad_categories":
            json.dumps([]),

        "is_adset_budget_sharing_enabled":
            "false"
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

        return (
            None,
            response.text
        )


    result = response.json()


    campaign_id = result.get(
        "id"
    )


    if not campaign_id:

        return (
            None,
            "Meta não retornou "
            "o ID da campanha."
        )


    return (
        campaign_id,
        None
    )


# =========================================================
# OPENAI
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


    response = (
        openai_client
        .responses
        .create(

            model=
                OPENAI_MODEL,

            reasoning={
                "effort":
                    "low"
            },

            max_output_tokens=
                700,

            instructions="""
Você é um analista sênior
especializado em Meta Ads.

Analise exclusivamente
os dados fornecidos.

Nunca invente números,
campanhas, anúncios,
conjuntos ou causas.

Diferencie fatos
de hipóteses.

Responda em português
do Brasil.

Estruture em:

📊 RESUMO

✅ PONTOS POSITIVOS

⚠️ PONTOS DE ATENÇÃO

🎯 PRÓXIMA AÇÃO
""",

            input=f"""
PERGUNTA:

{user_question}


DADOS REAIS:

{dados}
"""
        )
    )


    return response.output_text


# =========================================================
# RELATÓRIO FORMATADO
# =========================================================

def build_basic_report(
    report
):

    text = (

        "📊 *META ADS — "
        "ÚLTIMOS 7 DIAS*\n\n"

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
# WEBHOOK
# =========================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
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


        messages = value.get(
            "messages"
        )


        if not messages:

            return (
                "EVENT_RECEIVED",
                200
            )


        message = messages[0]


        if (
            message.get("type")
            != "text"
        ):

            return (
                "EVENT_RECEIVED",
                200
            )


        sender = message["from"]


        original_text = (
            message["text"]["body"]
            .strip()
        )


        received_text = normalize_text(
            original_text
        )


        print(
            "MENSAGEM:",
            original_text
        )


        print(
            "REMETENTE:",
            sender
        )


        # =================================================
        # IDENTIFICAR CLIENTE NO POSTGRESQL
        # =================================================

        context = get_user_context(
            sender
        )


        if not context:

            send_whatsapp_message(
                sender,
                (
                    "⛔ Este número ainda "
                    "não está cadastrado "
                    "no sistema."
                )
            )


            log_activity(
                None,
                "unauthorized_number",
                {
                    "whatsapp_number":
                        sender
                }
            )


            return (
                "EVENT_RECEIVED",
                200
            )


        ad_account_id = (
            context.get(
                "ad_account_id"
            )
        )


        # =================================================
        # QUEM SOU EU
        # =================================================

        if (
            received_text
            == "quem sou eu"
        ):

            leitura = (
                "SIM"
                if context[
                    "can_read_ads"
                ]
                else "NÃO"
            )


            criacao = (
                "SIM"
                if context[
                    "can_create_campaigns"
                ]
                else "NÃO"
            )


            send_whatsapp_message(
                sender,
                (
                    "👤 *USUÁRIO IDENTIFICADO*\n\n"

                    f"*Empresa:*\n"
                    f"{context['company_name']}\n\n"

                    f"*WhatsApp:*\n"
                    f"{context['whatsapp_number']}\n\n"

                    f"*Conta Meta:*\n"
                    f"{ad_account_id}\n\n"

                    f"*Pode consultar Ads:*\n"
                    f"{leitura}\n\n"

                    f"*Pode criar campanhas:*\n"
                    f"{criacao}"
                )
            )


            log_activity(
                context,
                "identity_check"
            )


            return (
                "EVENT_RECEIVED",
                200
            )


        # =================================================
        # CRIAR CAMPANHA TESTE
        # =================================================

        if (
            received_text
            == "criar campanha teste"
        ):


            if not context[
                "can_create_campaigns"
            ]:

                send_whatsapp_message(
                    sender,
                    (
                        "⛔ Você não possui "
                        "permissão para criar "
                        "campanhas."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            if not ad_account_id:

                send_whatsapp_message(
                    sender,
                    (
                        "❌ Nenhuma conta Meta "
                        "está vinculada "
                        "à sua empresa."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            send_whatsapp_message(
                sender,
                (
                    "⚠️ *CONFIRMAÇÃO NECESSÁRIA*\n\n"

                    f"*Empresa:*\n"
                    f"{context['company_name']}\n\n"

                    f"*Conta:*\n"
                    f"{ad_account_id}\n\n"

                    "*Nome:*\n"
                    "TESTE API WHATSAPP - NAO ATIVAR\n\n"

                    "*Objetivo:*\n"
                    "Tráfego\n\n"

                    "*Status:*\n"
                    "PAUSADA\n\n"

                    "Para continuar responda:\n\n"

                    "*CONFIRMAR CAMPANHA TESTE*"
                )
            )


            log_activity(
                context,
                "campaign_test_requested",
                {
                    "ad_account_id":
                        ad_account_id
                }
            )


            return (
                "EVENT_RECEIVED",
                200
            )


        # =================================================
        # CONFIRMAR CAMPANHA
        # =================================================

        if (
            received_text
            == "confirmar campanha teste"
        ):


            if not context[
                "can_create_campaigns"
            ]:

                send_whatsapp_message(
                    sender,
                    "⛔ Acesso negado."
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            if not ad_account_id:

                send_whatsapp_message(
                    sender,
                    (
                        "❌ Nenhuma conta Meta "
                        "está cadastrada."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            send_whatsapp_message(
                sender,
                (
                    "⏳ Criando campanha "
                    "PAUSADA..."
                )
            )


            campaign_id, error = (
                create_test_campaign(
                    ad_account_id
                )
            )


            if error:

                print(
                    "ERRO CAMPANHA:",
                    error
                )


                log_activity(
                    context,
                    "campaign_creation_failed",
                    {
                        "error":
                            error
                    }
                )


                send_whatsapp_message(
                    sender,
                    (
                        "❌ Não consegui "
                        "criar a campanha."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            log_activity(
                context,
                "campaign_created",
                {
                    "campaign_id":
                        campaign_id,

                    "ad_account_id":
                        ad_account_id,

                    "status":
                        "PAUSED"
                }
            )


            send_whatsapp_message(
                sender,
                (
                    "✅ *CAMPANHA CRIADA*\n\n"

                    f"*Empresa:*\n"
                    f"{context['company_name']}\n\n"

                    f"*Conta Meta:*\n"
                    f"{ad_account_id}\n\n"

                    f"*Campaign ID:*\n"
                    f"{campaign_id}\n\n"

                    "*Status:*\n"
                    "PAUSADA"
                )
            )


            return (
                "EVENT_RECEIVED",
                200
            )


        # =================================================
        # RELATÓRIO
        # =================================================

        if (
            "gasto"
            in received_text

            and

            "7"
            in received_text
        ):


            if not context[
                "can_read_ads"
            ]:

                send_whatsapp_message(
                    sender,
                    (
                        "⛔ Você não possui "
                        "permissão de leitura."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            if not ad_account_id:

                send_whatsapp_message(
                    sender,
                    (
                        "Nenhuma conta Meta "
                        "cadastrada."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            send_whatsapp_message(
                sender,
                "Consultando sua conta..."
            )


            report, error = (
                get_last_7_days_report(
                    ad_account_id
                )
            )


            if error:

                send_whatsapp_message(
                    sender,
                    (
                        "Não consegui "
                        "consultar o Meta Ads."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            log_activity(
                context,
                "meta_report_7_days"
            )


            send_whatsapp_message(
                sender,
                build_basic_report(
                    report
                )
            )


            return (
                "EVENT_RECEIVED",
                200
            )


        # =================================================
        # ANÁLISE IA
        # =================================================

        if (
            "analise"
            in received_text

            and

            "7"
            in received_text
        ):


            if not context[
                "can_read_ads"
            ]:

                send_whatsapp_message(
                    sender,
                    (
                        "⛔ Você não possui "
                        "permissão de leitura."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            report, error = (
                get_last_7_days_report(
                    ad_account_id
                )
            )


            if error:

                send_whatsapp_message(
                    sender,
                    (
                        "Não consegui "
                        "consultar a conta."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            send_whatsapp_message(
                sender,
                "Analisando os dados..."
            )


            try:

                analysis = (
                    analyze_meta_report(
                        report,
                        original_text
                    )
                )


                log_activity(
                    context,
                    "ai_analysis_7_days"
                )


                send_whatsapp_message(
                    sender,
                    analysis
                )


            except Exception as error:

                print(
                    "ERRO OPENAI:",
                    repr(error)
                )


                send_whatsapp_message(
                    sender,
                    (
                        "Erro ao gerar "
                        "a análise."
                    )
                )


            return (
                "EVENT_RECEIVED",
                200
            )


        # =================================================
        # MENU
        # =================================================

        send_whatsapp_message(
            sender,
            (
                f"🤖 *ASSISTENTE META ADS*\n\n"

                f"Empresa: "
                f"*{context['company_name']}*\n\n"

                "*Comandos:*\n\n"

                "👤 quem sou eu\n\n"

                "📊 gasto últimos 7 dias\n\n"

                "🤖 analise meus últimos 7 dias\n\n"

                "🔧 criar campanha teste"
            )
        )


    except Exception as error:

        print(
            "ERRO WEBHOOK:",
            repr(error)
        )


    return (
        "EVENT_RECEIVED",
        200
    )


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
