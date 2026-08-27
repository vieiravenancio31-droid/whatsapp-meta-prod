import os
import re
import json
import uuid
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

DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_WHATSAPP_NUMBER = os.getenv(
    "ADMIN_WHATSAPP_NUMBER"
)

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
# POSTGRESQL
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

        print("Inicializando PostgreSQL...")

        with get_db_connection() as conn:

            with conn.cursor() as cursor:

                # =================================================
                # EMPRESAS
                # =================================================

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


                # =================================================
                # USUÁRIOS
                # =================================================

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

                        is_platform_admin BOOLEAN
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


                # Se a tabela já existia antes desta versão,
                # adicionamos o novo campo automaticamente.

                cursor.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS
                    is_platform_admin BOOLEAN
                    NOT NULL
                    DEFAULT FALSE;
                    """
                )


                # =================================================
                # CONTAS META
                # =================================================

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


                # =================================================
                # LOGS
                # =================================================

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


                # =================================================
                # EMPRESA INICIAL
                # =================================================

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


                # =================================================
                # ADMINISTRADOR PRINCIPAL
                # =================================================

                if ADMIN_WHATSAPP_NUMBER:

                    cursor.execute(
                        """
                        INSERT INTO users (

                            company_id,
                            whatsapp_number,
                            name,
                            role,
                            can_read_ads,
                            can_create_campaigns,
                            is_platform_admin

                        )

                        VALUES (

                            %s,
                            %s,
                            %s,
                            'admin',
                            TRUE,
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

                            is_platform_admin =
                                TRUE,

                            active =
                                TRUE;
                        """,
                        (
                            company_id,
                            ADMIN_WHATSAPP_NUMBER,
                            "Administrador Principal"
                        )
                    )


                # =================================================
                # CONTA META INICIAL
                # =================================================

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
# UTILIDADES DE BANCO
# =========================================================

def get_user_context(whatsapp_number):

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

                    u.is_platform_admin,

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
                            details or {},
                            ensure_ascii=False
                        )
                    )
                )

    except Exception as error:

        print(
            "ERRO LOG:",
            repr(error)
        )


# =========================================================
# CRIAÇÃO DE CLIENTE
# =========================================================

def create_slug(name):

    text = unicodedata.normalize(
        "NFD",
        name.lower()
    )

    text = "".join(
        char
        for char in text
        if unicodedata.category(char)
        != "Mn"
    )

    text = re.sub(
        r"[^a-z0-9]+",
        "-",
        text
    )

    text = text.strip("-")

    if not text:
        text = "cliente"

    suffix = uuid.uuid4().hex[:6]

    return f"{text}-{suffix}"


def normalize_phone_number(number):

    return "".join(
        char
        for char in number
        if char.isdigit()
    )


def create_client(

    company_name,

    whatsapp_number,

    can_read_ads,

    can_create_campaigns

):

    initialize_database()

    whatsapp_number = normalize_phone_number(
        whatsapp_number
    )

    if (
        len(whatsapp_number) < 10
        or len(whatsapp_number) > 15
    ):

        return (
            None,
            "Número de WhatsApp inválido."
        )


    with get_db_connection() as conn:

        with conn.cursor() as cursor:

            # Verifica se o WhatsApp já existe.

            cursor.execute(
                """
                SELECT id
                FROM users
                WHERE whatsapp_number = %s;
                """,
                (
                    whatsapp_number,
                )
            )

            existing_user = cursor.fetchone()

            if existing_user:

                return (
                    None,
                    "Esse WhatsApp já está cadastrado."
                )


            slug = create_slug(
                company_name
            )


            # Cria empresa.

            cursor.execute(
                """
                INSERT INTO companies (

                    slug,
                    name

                )

                VALUES (

                    %s,
                    %s

                )

                RETURNING id;
                """,
                (
                    slug,
                    company_name
                )
            )

            company = cursor.fetchone()

            company_id = company["id"]


            # Cria administrador daquela empresa.

            cursor.execute(
                """
                INSERT INTO users (

                    company_id,
                    whatsapp_number,
                    name,
                    role,
                    can_read_ads,
                    can_create_campaigns,
                    is_platform_admin

                )

                VALUES (

                    %s,
                    %s,
                    %s,
                    'admin',
                    %s,
                    %s,
                    FALSE

                )

                RETURNING id;
                """,
                (
                    company_id,
                    whatsapp_number,
                    "Administrador",
                    can_read_ads,
                    can_create_campaigns
                )
            )

            user = cursor.fetchone()


    return {
        "company_id":
            company_id,

        "user_id":
            user["id"],

        "company_name":
            company_name,

        "whatsapp_number":
            whatsapp_number,

        "can_read_ads":
            can_read_ads,

        "can_create_campaigns":
            can_create_campaigns

    }, None


# =========================================================
# LISTAR CLIENTES
# =========================================================

def list_clients():

    initialize_database()

    with get_db_connection() as conn:

        with conn.cursor() as cursor:

            cursor.execute(
                """
                SELECT

                    c.id AS company_id,

                    c.name AS company_name,

                    u.whatsapp_number,

                    u.can_read_ads,

                    u.can_create_campaigns,

                    m.ad_account_id

                FROM companies c

                INNER JOIN users u
                    ON u.company_id = c.id

                LEFT JOIN meta_accounts m
                    ON m.company_id = c.id
                    AND m.active = TRUE

                WHERE

                    c.active = TRUE

                    AND u.active = TRUE

                ORDER BY c.id ASC;
                """
            )

            return cursor.fetchall()


# =========================================================
# HOME
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return {

        "status":
            "online",

        "service":
            "whatsapp-meta-prod",

        "database":
            "online",

        "architecture":
            "multi_company_v2"

    }, 200


# =========================================================
# DB CHECK
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
                        ) AS meta_accounts,

                        (
                            SELECT COUNT(*)
                            FROM activity_logs
                        ) AS activity_logs;
                    """
                )

                result = cursor.fetchone()


        return {

            "status":
                "database_online",

            "companies":
                result["companies"],

            "users":
                result["users"],

            "meta_accounts":
                result["meta_accounts"],

            "activity_logs":
                result["activity_logs"]

        }, 200


    except Exception as error:

        print(
            "ERRO DATABASE CHECK:",
            repr(error)
        )

        return {

            "status":
                "database_error",

            "error":
                str(error)

        }, 500


# =========================================================
# WEBHOOK VERIFICATION
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

    return f"R$ {formatted}"


def format_number(value):

    return (
        f"{int(value):,}"
        .replace(",", ".")
    )


def text_to_bool(text):

    normalized = normalize_text(
        text
    )

    if normalized in [
        "sim",
        "s",
        "true",
        "1"
    ]:

        return True

    if normalized in [
        "nao",
        "n",
        "false",
        "0"
    ]:

        return False

    return None


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
# META ADS — LEADS
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

            "spend":
                0,

            "impressions":
                0,

            "clicks":
                0,

            "leads":
                0,

            "cpl":
                0

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


    cpl = (
        spend / leads
        if leads > 0
        else 0
    )


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
# META ADS — CRIAÇÃO
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


    return (
        result.get("id"),
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

Analise somente
os dados fornecidos.

Nunca invente números,
campanhas, anúncios,
conjuntos ou causas.

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

DADOS:

{dados}
"""
        )
    )


    return response.output_text


# =========================================================
# RELATÓRIO
# =========================================================

def build_basic_report(report):

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
        # DESCOBRIR QUEM ESTÁ FALANDO
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


            return (
                "EVENT_RECEIVED",
                200
            )


        ad_account_id = context.get(
            "ad_account_id"
        )


        # =================================================
        # CADASTRAR CLIENTE
        # SOMENTE ADMINISTRADOR DA PLATAFORMA
        # =================================================

        if received_text.startswith(
            "cadastrar cliente"
        ):


            if not context[
                "is_platform_admin"
            ]:

                send_whatsapp_message(
                    sender,
                    (
                        "⛔ Este comando é "
                        "exclusivo do administrador "
                        "da plataforma."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            parts = [
                item.strip()
                for item
                in original_text.split("|")
            ]


            if len(parts) != 5:

                send_whatsapp_message(
                    sender,
                    (
                        "❌ *FORMATO INCORRETO*\n\n"

                        "Use exatamente:\n\n"

                        "cadastrar cliente | "
                        "Nome da empresa | "
                        "5531999999999 | "
                        "sim | nao\n\n"

                        "O primeiro SIM/NAO define "
                        "permissão de leitura.\n\n"

                        "O segundo SIM/NAO define "
                        "permissão de criação."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            company_name = parts[1]

            new_whatsapp = parts[2]

            read_permission = text_to_bool(
                parts[3]
            )

            create_permission = text_to_bool(
                parts[4]
            )


            if (
                read_permission is None
                or
                create_permission is None
            ):

                send_whatsapp_message(
                    sender,
                    (
                        "❌ Nas permissões use "
                        "apenas SIM ou NAO."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            client, error = create_client(

                company_name,

                new_whatsapp,

                read_permission,

                create_permission
            )


            if error:

                send_whatsapp_message(
                    sender,
                    (
                        "❌ Não consegui "
                        "cadastrar o cliente.\n\n"
                        f"{error}"
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            log_activity(
                context,
                "client_created",
                client
            )


            leitura = (
                "SIM"
                if read_permission
                else "NÃO"
            )


            criacao = (
                "SIM"
                if create_permission
                else "NÃO"
            )


            send_whatsapp_message(
                sender,
                (
                    "✅ *CLIENTE CADASTRADO*\n\n"

                    f"*Empresa:*\n"
                    f"{company_name}\n\n"

                    f"*WhatsApp:*\n"
                    f"{client['whatsapp_number']}\n\n"

                    f"*Consultar Meta Ads:*\n"
                    f"{leitura}\n\n"

                    f"*Criar campanhas:*\n"
                    f"{criacao}\n\n"

                    "*Conta Meta:*\n"
                    "Ainda não vinculada."
                )
            )


            return (
                "EVENT_RECEIVED",
                200
            )


        # =================================================
        # LISTAR CLIENTES
        # =================================================

        if received_text == "listar clientes":

            if not context[
                "is_platform_admin"
            ]:

                send_whatsapp_message(
                    sender,
                    "⛔ Acesso negado."
                )


                return (
                    "EVENT_RECEIVED",
                    200
                )


            clients = list_clients()


            lines = [
                "👥 *CLIENTES CADASTRADOS*\n"
            ]


            for client in clients:

                conta = (
                    client[
                        "ad_account_id"
                    ]
                    or "Sem Meta vinculada"
                )


                lines.append(
                    (
                        f"\n*{client['company_name']}*\n"
                        f"WhatsApp: "
                        f"{client['whatsapp_number']}\n"
                        f"Meta: {conta}\n"
                    )
                )


            send_whatsapp_message(
                sender,
                "".join(lines)
            )


            return (
                "EVENT_RECEIVED",
                200
            )


        # =================================================
        # QUEM SOU EU
        # =================================================

        if received_text == "quem sou eu":

            conta = (
                ad_account_id
                or "Não vinculada"
            )


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
                    f"{conta}\n\n"

                    f"*Pode consultar Ads:*\n"
                    f"{leitura}\n\n"

                    f"*Pode criar campanhas:*\n"
                    f"{criacao}"
                )
            )


            return (
                "EVENT_RECEIVED",
                200
            )


        # =================================================
        # CRIAR CAMPANHA TESTE
        # =================================================

        if received_text == "criar campanha teste":

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
                        "❌ Sua empresa ainda "
                        "não possui uma conta Meta "
                        "vinculada."
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

                    f"Empresa:\n"
                    f"*{context['company_name']}*\n\n"

                    f"Conta:\n"
                    f"*{ad_account_id}*\n\n"

                    "Campanha:\n"
                    "*TESTE API WHATSAPP - NAO ATIVAR*\n\n"

                    "Status:\n"
                    "*PAUSADA*\n\n"

                    "Para continuar:\n\n"

                    "*CONFIRMAR CAMPANHA TESTE*"
                )
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
                        "❌ Conta Meta "
                        "não vinculada."
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
                        ad_account_id
                }
            )


            send_whatsapp_message(
                sender,
                (
                    "✅ *CAMPANHA CRIADA*\n\n"

                    f"Empresa:\n"
                    f"*{context['company_name']}*\n\n"

                    f"Campaign ID:\n"
                    f"*{campaign_id}*\n\n"

                    "Status:\n"
                    "*PAUSADA*"
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
            "gasto" in received_text
            and
            "7" in received_text
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
                        "❌ Sua empresa ainda "
                        "não possui uma conta Meta "
                        "vinculada."
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
                        "Não consegui consultar "
                        "o Meta Ads."
                    )
                )


                return (
                    "EVENT_RECEIVED",
                    200
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
            "analise" in received_text
            and
            "7" in received_text
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
                        "❌ Sua empresa ainda "
                        "não possui conta Meta "
                        "vinculada."
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
                        "Não consegui consultar "
                        "a conta."
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

        menu = (

            "🤖 *ASSISTENTE META ADS*\n\n"

            f"Empresa: "
            f"*{context['company_name']}*\n\n"

            "👤 quem sou eu\n\n"

            "📊 gasto últimos 7 dias\n\n"

            "🤖 analise meus últimos 7 dias\n\n"

            "🔧 criar campanha teste"
        )


        if context[
            "is_platform_admin"
        ]:

            menu += (

                "\n\n━━━━━━━━━━━━━━\n\n"

                "🔐 *ADMINISTRAÇÃO*\n\n"

                "👥 listar clientes\n\n"

                "➕ cadastrar cliente"
            )


        send_whatsapp_message(
            sender,
            menu
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
