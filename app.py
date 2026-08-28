import os
import re
import json
import uuid
import base64
import hashlib
import hmac
import secrets
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import psycopg
import requests
from cryptography.fernet import Fernet, InvalidToken
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

# Mantidos como fallback apenas para a empresa principal
# enquanto ela ainda não tiver conectado a Meta via OAuth.
META_ADS_ACCESS_TOKEN = os.getenv("META_ADS_ACCESS_TOKEN")
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID")

META_APP_ID = os.getenv("META_APP_ID")
META_APP_SECRET = os.getenv("META_APP_SECRET")
META_REDIRECT_URI = os.getenv("META_REDIRECT_URI")
META_BUSINESS_LOGIN_CONFIG_ID = os.getenv("META_BUSINESS_LOGIN_CONFIG_ID")
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_WHATSAPP_NUMBER = os.getenv("ADMIN_WHATSAPP_NUMBER")

GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v24.0")
DEFAULT_COMPANY_NAME = os.getenv("DEFAULT_COMPANY_NAME", "Empresa Principal")


# =========================================================
# OPENAI
# =========================================================

openai_client = OpenAI(api_key=OPENAI_API_KEY)


# =========================================================
# CONTROLE DE INICIALIZAÇÃO DO BANCO
# =========================================================

database_initialized = False
database_lock = threading.Lock()


# =========================================================
# CRIPTOGRAFIA DOS TOKENS META
# =========================================================

def get_fernet():
    if not TOKEN_ENCRYPTION_KEY:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY não configurada.")

    digest = hashlib.sha256(TOKEN_ENCRYPTION_KEY.encode("utf-8")).digest()
    fernet_key = base64.urlsafe_b64encode(digest)
    return Fernet(fernet_key)


def encrypt_token(token):
    if not token:
        raise ValueError("Token vazio não pode ser criptografado.")
    return get_fernet().encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted_token):
    if not encrypted_token:
        raise ValueError("Token criptografado não encontrado.")

    try:
        return get_fernet().decrypt(
            encrypted_token.encode("utf-8")
        ).decode("utf-8")
    except InvalidToken as error:
        raise RuntimeError(
            "Não foi possível descriptografar a conexão Meta. "
            "Verifique TOKEN_ENCRYPTION_KEY."
        ) from error


# =========================================================
# POSTGRESQL
# =========================================================

def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada.")

    return psycopg.connect(
        DATABASE_URL,
        autocommit=True,
        row_factory=dict_row,
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
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS companies (
                        id BIGSERIAL PRIMARY KEY,
                        slug TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGSERIAL PRIMARY KEY,
                        company_id BIGINT NOT NULL
                            REFERENCES companies(id) ON DELETE CASCADE,
                        whatsapp_number TEXT NOT NULL UNIQUE,
                        name TEXT,
                        role TEXT NOT NULL DEFAULT 'user',
                        can_read_ads BOOLEAN NOT NULL DEFAULT TRUE,
                        can_create_campaigns BOOLEAN NOT NULL DEFAULT FALSE,
                        is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )

                cursor.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS is_platform_admin BOOLEAN
                    NOT NULL DEFAULT FALSE;
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta_connections (
                        id BIGSERIAL PRIMARY KEY,
                        company_id BIGINT NOT NULL UNIQUE
                            REFERENCES companies(id) ON DELETE CASCADE,
                        encrypted_access_token TEXT NOT NULL,
                        token_expires_at TIMESTAMPTZ,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS oauth_states (
                        id BIGSERIAL PRIMARY KEY,
                        state_hash TEXT NOT NULL UNIQUE,
                        company_id BIGINT NOT NULL
                            REFERENCES companies(id) ON DELETE CASCADE,
                        user_id BIGINT NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,
                        expires_at TIMESTAMPTZ NOT NULL,
                        used_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta_accounts (
                        id BIGSERIAL PRIMARY KEY,
                        company_id BIGINT NOT NULL
                            REFERENCES companies(id) ON DELETE CASCADE,
                        ad_account_id TEXT NOT NULL,
                        name TEXT,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )

                # Migração da tabela antiga para o modelo multi-conexão.
                cursor.execute(
                    """
                    ALTER TABLE meta_accounts
                    ADD COLUMN IF NOT EXISTS connection_id BIGINT
                    REFERENCES meta_connections(id) ON DELETE SET NULL;
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE meta_accounts
                    ADD COLUMN IF NOT EXISTS account_status INTEGER;
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE meta_accounts
                    ADD COLUMN IF NOT EXISTS currency TEXT;
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE meta_accounts
                    ADD COLUMN IF NOT EXISTS timezone_name TEXT;
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE meta_accounts
                    ADD COLUMN IF NOT EXISTS selected BOOLEAN
                    NOT NULL DEFAULT FALSE;
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE meta_accounts
                    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
                    NOT NULL DEFAULT NOW();
                    """
                )

                # Remove a unicidade global antiga e passa a permitir
                # o mesmo ad account em empresas distintas, se necessário.
                cursor.execute(
                    """
                    ALTER TABLE meta_accounts
                    DROP CONSTRAINT IF EXISTS meta_accounts_ad_account_id_key;
                    """
                )
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_meta_accounts_company_ad_account
                    ON meta_accounts(company_id, ad_account_id);
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS activity_logs (
                        id BIGSERIAL PRIMARY KEY,
                        company_id BIGINT
                            REFERENCES companies(id) ON DELETE SET NULL,
                        user_id BIGINT
                            REFERENCES users(id) ON DELETE SET NULL,
                        action TEXT NOT NULL,
                        details JSONB,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )

                # Empresa principal.
                cursor.execute(
                    """
                    INSERT INTO companies (slug, name)
                    VALUES ('principal', %s)
                    ON CONFLICT (slug)
                    DO UPDATE SET name = EXCLUDED.name
                    RETURNING id;
                    """,
                    (DEFAULT_COMPANY_NAME,),
                )
                company = cursor.fetchone()
                company_id = company["id"]

                # Admin principal.
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
                        VALUES (%s, %s, %s, 'admin', TRUE, TRUE, TRUE)
                        ON CONFLICT (whatsapp_number)
                        DO UPDATE SET
                            company_id = EXCLUDED.company_id,
                            role = 'admin',
                            can_read_ads = TRUE,
                            can_create_campaigns = TRUE,
                            is_platform_admin = TRUE,
                            active = TRUE;
                        """,
                        (
                            company_id,
                            ADMIN_WHATSAPP_NUMBER,
                            "Administrador Principal",
                        ),
                    )

                # Conta antiga/global fica selecionada para não quebrar
                # o fluxo já validado, até essa empresa usar OAuth.
                if META_AD_ACCOUNT_ID:
                    cursor.execute(
                        """
                        INSERT INTO meta_accounts (
                            company_id,
                            ad_account_id,
                            name,
                            selected
                        )
                        VALUES (%s, %s, %s, TRUE)
                        ON CONFLICT (company_id, ad_account_id)
                        DO UPDATE SET
                            name = EXCLUDED.name,
                            active = TRUE,
                            selected = TRUE,
                            updated_at = NOW();
                        """,
                        (
                            company_id,
                            META_AD_ACCOUNT_ID,
                            "Conta Meta Principal",
                        ),
                    )

        database_initialized = True
        print("PostgreSQL inicializado com sucesso.")


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
                    c.slug AS company_slug,
                    c.name AS company_name,
                    m.id AS meta_account_db_id,
                    m.ad_account_id,
                    m.name AS meta_account_name,
                    m.connection_id
                FROM users u
                INNER JOIN companies c
                    ON c.id = u.company_id
                LEFT JOIN meta_accounts m
                    ON m.company_id = c.id
                    AND m.active = TRUE
                    AND m.selected = TRUE
                WHERE
                    u.whatsapp_number = %s
                    AND u.active = TRUE
                    AND c.active = TRUE
                ORDER BY m.id ASC
                LIMIT 1;
                """,
                (whatsapp_number,),
            )
            return cursor.fetchone()


def log_activity(context, action, details=None):
    try:
        initialize_database()
        company_id = context.get("company_id") if context else None
        user_id = context.get("user_id") if context else None

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
                    VALUES (%s, %s, %s, %s::jsonb);
                    """,
                    (
                        company_id,
                        user_id,
                        action,
                        json.dumps(details or {}, ensure_ascii=False),
                    ),
                )
    except Exception as error:
        print("ERRO LOG:", repr(error))


def create_slug(name):
    text = unicodedata.normalize("NFD", name.lower())
    text = "".join(
        char for char in text
        if unicodedata.category(char) != "Mn"
    )
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not text:
        text = "cliente"
    return f"{text}-{uuid.uuid4().hex[:6]}"


def normalize_phone_number(number):
    return "".join(char for char in number if char.isdigit())


def create_client(company_name, whatsapp_number, can_read_ads, can_create_campaigns):
    initialize_database()
    whatsapp_number = normalize_phone_number(whatsapp_number)

    if len(whatsapp_number) < 10 or len(whatsapp_number) > 15:
        return None, "Número de WhatsApp inválido."

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM users WHERE whatsapp_number = %s;",
                (whatsapp_number,),
            )
            if cursor.fetchone():
                return None, "Esse WhatsApp já está cadastrado."

            slug = create_slug(company_name)

            cursor.execute(
                """
                INSERT INTO companies (slug, name)
                VALUES (%s, %s)
                RETURNING id;
                """,
                (slug, company_name),
            )
            company_id = cursor.fetchone()["id"]

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
                VALUES (%s, %s, %s, 'admin', %s, %s, FALSE)
                RETURNING id;
                """,
                (
                    company_id,
                    whatsapp_number,
                    "Administrador",
                    can_read_ads,
                    can_create_campaigns,
                ),
            )
            user_id = cursor.fetchone()["id"]

    return {
        "company_id": company_id,
        "user_id": user_id,
        "company_name": company_name,
        "whatsapp_number": whatsapp_number,
        "can_read_ads": can_read_ads,
        "can_create_campaigns": can_create_campaigns,
    }, None


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
                    AND m.selected = TRUE
                WHERE
                    c.active = TRUE
                    AND u.active = TRUE
                ORDER BY c.id ASC;
                """
            )
            return cursor.fetchall()


# =========================================================
# OAUTH META — STATE
# =========================================================

def hash_oauth_state(state):
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def create_oauth_state(context):
    initialize_database()

    raw_state = secrets.token_urlsafe(32)
    state_hash = hash_oauth_state(raw_state)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO oauth_states (
                    state_hash,
                    company_id,
                    user_id,
                    expires_at
                )
                VALUES (%s, %s, %s, %s);
                """,
                (
                    state_hash,
                    context["company_id"],
                    context["user_id"],
                    expires_at,
                ),
            )

    return raw_state


def consume_oauth_state(raw_state):
    initialize_database()

    if not raw_state:
        return None

    state_hash = hash_oauth_state(raw_state)

    # Aqui usamos transação explícita para impedir reutilização.
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    s.id,
                    s.company_id,
                    s.user_id,
                    s.expires_at,
                    s.used_at,
                    u.whatsapp_number,
                    c.name AS company_name
                FROM oauth_states s
                INNER JOIN users u ON u.id = s.user_id
                INNER JOIN companies c ON c.id = s.company_id
                WHERE s.state_hash = %s
                FOR UPDATE;
                """,
                (state_hash,),
            )
            row = cursor.fetchone()

            if not row:
                conn.rollback()
                return None

            if row["used_at"] is not None:
                conn.rollback()
                return None

            if row["expires_at"] < datetime.now(timezone.utc):
                conn.rollback()
                return None

            cursor.execute(
                """
                UPDATE oauth_states
                SET used_at = NOW()
                WHERE id = %s;
                """,
                (row["id"],),
            )
            conn.commit()
            return row


# =========================================================
# OAUTH META — LOGIN / TOKEN / CONTAS
# =========================================================

def validate_meta_oauth_config():
    missing = []
    required = {
        "META_APP_ID": META_APP_ID,
        "META_APP_SECRET": META_APP_SECRET,
        "META_REDIRECT_URI": META_REDIRECT_URI,
        "META_BUSINESS_LOGIN_CONFIG_ID": META_BUSINESS_LOGIN_CONFIG_ID,
        "TOKEN_ENCRYPTION_KEY": TOKEN_ENCRYPTION_KEY,
    }

    for name, value in required.items():
        if not value:
            missing.append(name)

    return missing


def build_meta_login_url(context):
    missing = validate_meta_oauth_config()
    if missing:
        raise RuntimeError(
            "Variáveis OAuth ausentes: " + ", ".join(missing)
        )

    state = create_oauth_state(context)

    params = {
        "client_id": META_APP_ID,
        "redirect_uri": META_REDIRECT_URI,
        "config_id": META_BUSINESS_LOGIN_CONFIG_ID,
        "response_type": "code",
        "override_default_response_type": "true",
        "state": state,
    }

    return (
        f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth?"
        f"{urlencode(params)}"
    )


def exchange_code_for_token(code):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/oauth/access_token"

    params = {
        "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET,
        "redirect_uri": META_REDIRECT_URI,
        "code": code,
    }

    response = requests.get(url, params=params, timeout=30)

    if response.status_code != 200:
        raise RuntimeError(
            f"Meta recusou a troca do código: {response.status_code} {response.text}"
        )

    data = response.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Meta não retornou access_token.")

    expires_in = data.get("expires_in")
    expires_at = None
    if expires_in:
        try:
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(expires_in)
            )
        except (TypeError, ValueError):
            expires_at = None

    return token, expires_at


def appsecret_proof(access_token):
    if not META_APP_SECRET:
        return None
    return hmac.new(
        META_APP_SECRET.encode("utf-8"),
        access_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def fetch_ad_accounts(access_token):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/adaccounts"

    params = {
        "fields": "id,name,account_status,currency,timezone_name,business_name",
        "limit": 200,
        "access_token": access_token,
    }

    proof = appsecret_proof(access_token)
    if proof:
        params["appsecret_proof"] = proof

    accounts = []
    next_url = url
    next_params = params
    page_count = 0

    while next_url and page_count < 10:
        response = requests.get(
            next_url,
            params=next_params,
            timeout=30,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Não foi possível listar contas de anúncios: "
                f"{response.status_code} {response.text}"
            )

        data = response.json()
        accounts.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        next_params = None
        page_count += 1

    return accounts


def save_meta_connection_and_accounts(company_id, access_token, expires_at, accounts):
    initialize_database()
    encrypted = encrypt_token(access_token)

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO meta_connections (
                    company_id,
                    encrypted_access_token,
                    token_expires_at,
                    active,
                    connected_at,
                    updated_at
                )
                VALUES (%s, %s, %s, TRUE, NOW(), NOW())
                ON CONFLICT (company_id)
                DO UPDATE SET
                    encrypted_access_token = EXCLUDED.encrypted_access_token,
                    token_expires_at = EXCLUDED.token_expires_at,
                    active = TRUE,
                    updated_at = NOW()
                RETURNING id;
                """,
                (company_id, encrypted, expires_at),
            )
            connection_id = cursor.fetchone()["id"]

            # Ao reconectar, nenhuma conta nova fica selecionada automaticamente.
            cursor.execute(
                """
                UPDATE meta_accounts
                SET selected = FALSE, updated_at = NOW()
                WHERE company_id = %s;
                """,
                (company_id,),
            )

            discovered_ids = []
            for account in accounts:
                ad_account_id = account.get("id")
                if not ad_account_id:
                    continue

                discovered_ids.append(ad_account_id)

                cursor.execute(
                    """
                    INSERT INTO meta_accounts (
                        company_id,
                        connection_id,
                        ad_account_id,
                        name,
                        account_status,
                        currency,
                        timezone_name,
                        selected,
                        active,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, TRUE, NOW())
                    ON CONFLICT (company_id, ad_account_id)
                    DO UPDATE SET
                        connection_id = EXCLUDED.connection_id,
                        name = EXCLUDED.name,
                        account_status = EXCLUDED.account_status,
                        currency = EXCLUDED.currency,
                        timezone_name = EXCLUDED.timezone_name,
                        active = TRUE,
                        selected = FALSE,
                        updated_at = NOW();
                    """,
                    (
                        company_id,
                        connection_id,
                        ad_account_id,
                        account.get("name"),
                        account.get("account_status"),
                        account.get("currency"),
                        account.get("timezone_name"),
                    ),
                )

            # Contas que pertenciam à conexão anterior e não vieram mais
            # ficam inativas. A conta legacy sem connection_id é preservada.
            if discovered_ids:
                cursor.execute(
                    """
                    UPDATE meta_accounts
                    SET active = FALSE, selected = FALSE, updated_at = NOW()
                    WHERE company_id = %s
                      AND connection_id = %s
                      AND NOT (ad_account_id = ANY(%s));
                    """,
                    (company_id, connection_id, discovered_ids),
                )

            conn.commit()

    return connection_id


def list_company_meta_accounts(company_id):
    initialize_database()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    ad_account_id,
                    name,
                    account_status,
                    currency,
                    timezone_name,
                    selected
                FROM meta_accounts
                WHERE company_id = %s
                  AND active = TRUE
                  AND connection_id IS NOT NULL
                ORDER BY LOWER(COALESCE(name, '')), ad_account_id;
                """,
                (company_id,),
            )
            return cursor.fetchall()


def select_company_meta_account(company_id, position):
    accounts = list_company_meta_accounts(company_id)

    if position < 1 or position > len(accounts):
        return None, "Número de conta inválido."

    chosen = accounts[position - 1]

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE meta_accounts
                SET selected = FALSE, updated_at = NOW()
                WHERE company_id = %s;
                """,
                (company_id,),
            )
            cursor.execute(
                """
                UPDATE meta_accounts
                SET selected = TRUE, active = TRUE, updated_at = NOW()
                WHERE id = %s AND company_id = %s;
                """,
                (chosen["id"], company_id),
            )

    chosen["selected"] = True
    return chosen, None


def get_meta_credentials(context):
    ad_account_id = context.get("ad_account_id")
    connection_id = context.get("connection_id")

    if not ad_account_id:
        return None, None, "Nenhuma conta Meta está selecionada."

    # Nova conexão OAuth por cliente.
    if connection_id:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT encrypted_access_token, token_expires_at, active
                    FROM meta_connections
                    WHERE id = %s AND company_id = %s;
                    """,
                    (connection_id, context["company_id"]),
                )
                connection = cursor.fetchone()

        if not connection or not connection["active"]:
            return None, None, "Conexão Meta inativa ou inexistente."

        expires_at = connection.get("token_expires_at")
        if expires_at and expires_at <= datetime.now(timezone.utc):
            return None, None, "A autorização Meta expirou. Conecte novamente."

        token = decrypt_token(connection["encrypted_access_token"])
        return ad_account_id, token, None

    # Fallback legado somente para a empresa principal já validada.
    if (
        context.get("company_slug") == "principal"
        and META_ADS_ACCESS_TOKEN
        and ad_account_id == META_AD_ACCOUNT_ID
    ):
        return ad_account_id, META_ADS_ACCESS_TOKEN, None

    return None, None, "Essa conta não possui uma autorização Meta válida."


# =========================================================
# HOME / HEALTH
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return {
        "status": "online",
        "service": "whatsapp-meta-prod",
        "database": "online",
        "architecture": "multi_company_oauth_v1",
    }, 200


@app.route("/db-check", methods=["GET"])
def database_check():
    try:
        initialize_database()

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM companies) AS companies,
                        (SELECT COUNT(*) FROM users) AS users,
                        (SELECT COUNT(*) FROM meta_connections WHERE active = TRUE)
                            AS meta_connections,
                        (SELECT COUNT(*) FROM meta_accounts WHERE active = TRUE)
                            AS meta_accounts,
                        (SELECT COUNT(*) FROM activity_logs) AS activity_logs;
                    """
                )
                result = cursor.fetchone()

        return {
            "status": "database_online",
            "companies": result["companies"],
            "users": result["users"],
            "meta_connections": result["meta_connections"],
            "meta_accounts": result["meta_accounts"],
            "activity_logs": result["activity_logs"],
        }, 200

    except Exception as error:
        print("ERRO DATABASE CHECK:", repr(error))
        return {
            "status": "database_error",
            "error": str(error),
        }, 500


# =========================================================
# CALLBACK OAUTH META
# =========================================================

@app.route("/meta/callback", methods=["GET"])
def meta_callback():
    error = request.args.get("error")
    error_description = request.args.get("error_description")

    if error:
        return (
            "<h2>Conexão com a Meta cancelada ou recusada.</h2>"
            f"<p>{error_description or error}</p>"
            "<p>Volte ao WhatsApp e envie <b>conectar meta</b> novamente.</p>"
        ), 400

    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        return "Callback inválido: code/state ausente.", 400

    state_data = consume_oauth_state(state)
    if not state_data:
        return (
            "<h2>Link inválido ou expirado.</h2>"
            "<p>Volte ao WhatsApp e envie <b>conectar meta</b> novamente.</p>"
        ), 400

    context = {
        "company_id": state_data["company_id"],
        "user_id": state_data["user_id"],
        "company_name": state_data["company_name"],
    }

    try:
        access_token, expires_at = exchange_code_for_token(code)
        accounts = fetch_ad_accounts(access_token)

        save_meta_connection_and_accounts(
            state_data["company_id"],
            access_token,
            expires_at,
            accounts,
        )

        log_activity(
            context,
            "meta_oauth_connected",
            {"accounts_found": len(accounts)},
        )

        if not accounts:
            send_whatsapp_message(
                state_data["whatsapp_number"],
                (
                    "✅ Sua Meta foi autorizada, mas não encontrei nenhuma "
                    "conta de anúncios disponível para essa conexão."
                ),
            )
        else:
            lines = [
                "✅ *META CONECTADA COM SUCESSO*\n\n",
                "Encontrei estas contas de anúncios:\n",
            ]

            stored_accounts = list_company_meta_accounts(
                state_data["company_id"]
            )

            for index, account in enumerate(stored_accounts, start=1):
                name = account.get("name") or "Sem nome"
                lines.append(
                    f"\n*{index}.* {name}\n{account['ad_account_id']}\n"
                )

            lines.append(
                "\nPara escolher a conta principal, responda no WhatsApp:\n\n"
                "*usar conta 1*\n\n"
                "(troque 1 pelo número da conta desejada)"
            )

            send_whatsapp_message(
                state_data["whatsapp_number"],
                "".join(lines),
            )

        return (
            "<h2>Meta conectada com sucesso.</h2>"
            "<p>Você já pode fechar esta página e voltar ao WhatsApp.</p>"
        ), 200

    except Exception as oauth_error:
        print("ERRO OAUTH META:", repr(oauth_error))

        log_activity(
            context,
            "meta_oauth_failed",
            {"error": str(oauth_error)},
        )

        send_whatsapp_message(
            state_data["whatsapp_number"],
            (
                "❌ A autorização foi recebida, mas ocorreu um erro ao "
                "finalizar a conexão com a Meta. Verifique os logs do Railway."
            ),
        )

        return (
            "<h2>Erro ao finalizar a conexão.</h2>"
            "<p>Volte ao WhatsApp. O administrador pode verificar os logs.</p>"
        ), 500


# =========================================================
# WEBHOOK META — VERIFICAÇÃO
# =========================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Webhook verification failed", 403


# =========================================================
# UTILIDADES GERAIS
# =========================================================

def normalize_text(text):
    text = text.strip().lower()
    text = unicodedata.normalize("NFD", text)
    return "".join(
        char for char in text
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


def text_to_bool(text):
    normalized = normalize_text(text)
    if normalized in ["sim", "s", "true", "1"]:
        return True
    if normalized in ["nao", "n", "false", "0"]:
        return False
    return None


# =========================================================
# WHATSAPP
# =========================================================

def send_whatsapp_message(to, text):
    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "preview_url": True,
            "body": text,
        },
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=30,
    )

    print("RESPOSTA WHATSAPP:", response.status_code, response.text)
    return response


# =========================================================
# META ADS — LEADS / RELATÓRIO / CRIAÇÃO
# =========================================================

def get_leads(actions):
    if not actions:
        return 0

    actions_dict = {}
    for item in actions:
        action_type = item.get("action_type")
        try:
            value = float(item.get("value", 0))
        except (TypeError, ValueError):
            value = 0
        actions_dict[action_type] = value

    lead_types = [
        "onsite_conversion.lead_grouped",
        "lead",
        "offsite_conversion.fb_pixel_lead",
        "omni_lead",
    ]

    for lead_type in lead_types:
        if lead_type in actions_dict:
            return actions_dict[lead_type]

    return 0


def get_last_7_days_report(ad_account_id, access_token):
    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{ad_account_id}/insights"
    )

    params = {
        "access_token": access_token,
        "fields": "spend,impressions,clicks,actions",
        "date_preset": "last_7d",
        "level": "account",
    }

    proof = appsecret_proof(access_token)
    if proof:
        params["appsecret_proof"] = proof

    response = requests.get(url, params=params, timeout=30)

    print("RESPOSTA META ADS:", response.status_code, response.text)

    if response.status_code != 200:
        return None, response.text

    result = response.json()

    if not result.get("data"):
        return {
            "spend": 0,
            "impressions": 0,
            "clicks": 0,
            "leads": 0,
            "cpl": 0,
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
        "cpl": cpl,
    }, None


def create_test_campaign(ad_account_id, access_token):
    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{ad_account_id}/campaigns"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    payload = {
        "name": "TESTE API WHATSAPP - NAO ATIVAR",
        "objective": "OUTCOME_TRAFFIC",
        "buying_type": "AUCTION",
        "status": "PAUSED",
        "special_ad_categories": json.dumps([]),
        "is_adset_budget_sharing_enabled": "false",
    }

    proof = appsecret_proof(access_token)
    if proof:
        payload["appsecret_proof"] = proof

    response = requests.post(
        url,
        headers=headers,
        data=payload,
        timeout=30,
    )

    print("CRIAÇÃO CAMPANHA:", response.status_code, response.text)

    if response.status_code != 200:
        return None, response.text

    result = response.json()
    campaign_id = result.get("id")

    if not campaign_id:
        return None, "Meta não retornou o ID da campanha."

    return campaign_id, None


# =========================================================
# OPENAI
# =========================================================

def analyze_meta_report(report, user_question):
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
        reasoning={"effort": "low"},
        max_output_tokens=700,
        instructions="""
Você é um analista sênior especializado em Meta Ads.
Analise somente os dados fornecidos.
Nunca invente números, campanhas, anúncios, conjuntos ou causas.
Diferencie fatos de hipóteses.
Responda em português do Brasil.

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
""",
    )

    return response.output_text


def build_basic_report(report):
    text = (
        "📊 *META ADS — ÚLTIMOS 7 DIAS*\n\n"
        f"💰 Investimento: {format_brl(report['spend'])}\n"
        f"👁 Impressões: {format_number(report['impressions'])}\n"
        f"🖱 Cliques: {format_number(report['clicks'])}\n"
        f"🎯 Leads: {int(report['leads'])}\n"
    )

    if report["leads"] > 0:
        text += f"💵 CPL: {format_brl(report['cpl'])}"
    else:
        text += "💵 CPL: sem leads registrados"

    return text


# =========================================================
# WEBHOOK WHATSAPP
# =========================================================

@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True) or {}
    print("WEBHOOK RECEBIDO:", data)

    try:
        value = data["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")

        if not messages:
            return "EVENT_RECEIVED", 200

        message = messages[0]
        if message.get("type") != "text":
            return "EVENT_RECEIVED", 200

        sender = message["from"]
        original_text = message["text"]["body"].strip()
        received_text = normalize_text(original_text)

        print("MENSAGEM:", original_text)
        print("REMETENTE:", sender)

        context = get_user_context(sender)

        if not context:
            send_whatsapp_message(
                sender,
                "⛔ Este número ainda não está cadastrado no sistema.",
            )
            return "EVENT_RECEIVED", 200

        # =================================================
        # CONECTAR META — OAUTH POR CLIENTE
        # =================================================

        if received_text in ["conectar meta", "conectar minha meta"]:
            try:
                login_url = build_meta_login_url(context)

                log_activity(
                    context,
                    "meta_oauth_started",
                )

                send_whatsapp_message(
                    sender,
                    (
                        "🔗 *CONECTAR SUA CONTA META*\n\n"
                        "Abra o link abaixo e faça login na Meta. "
                        "Escolha os ativos que deseja autorizar para o produto.\n\n"
                        f"{login_url}\n\n"
                        "Este link expira em aproximadamente 15 minutos."
                    ),
                )

            except Exception as error:
                print("ERRO AO GERAR LOGIN META:", repr(error))
                send_whatsapp_message(
                    sender,
                    "❌ Não consegui iniciar a conexão com a Meta. Verifique os logs.",
                )

            return "EVENT_RECEIVED", 200

        # =================================================
        # LISTAR / ESCOLHER CONTAS META
        # =================================================

        if received_text == "minhas contas meta":
            accounts = list_company_meta_accounts(context["company_id"])

            if not accounts:
                send_whatsapp_message(
                    sender,
                    "Nenhuma conta foi encontrada. Envie *conectar meta* primeiro.",
                )
                return "EVENT_RECEIVED", 200

            lines = ["📂 *SUAS CONTAS META*\n"]
            for index, account in enumerate(accounts, start=1):
                marker = " ✅" if account.get("selected") else ""
                lines.append(
                    f"\n*{index}.* {account.get('name') or 'Sem nome'}{marker}\n"
                    f"{account['ad_account_id']}\n"
                )

            lines.append(
                "\nPara selecionar, envie: *usar conta 1*"
            )

            send_whatsapp_message(sender, "".join(lines))
            return "EVENT_RECEIVED", 200

        account_match = re.fullmatch(r"usar conta\s+(\d+)", received_text)
        if account_match:
            position = int(account_match.group(1))
            chosen, error = select_company_meta_account(
                context["company_id"],
                position,
            )

            if error:
                send_whatsapp_message(sender, f"❌ {error}")
                return "EVENT_RECEIVED", 200

            log_activity(
                context,
                "meta_account_selected",
                {
                    "ad_account_id": chosen["ad_account_id"],
                    "name": chosen.get("name"),
                },
            )

            send_whatsapp_message(
                sender,
                (
                    "✅ *CONTA META SELECIONADA*\n\n"
                    f"*Nome:*\n{chosen.get('name') or 'Sem nome'}\n\n"
                    f"*Conta:*\n{chosen['ad_account_id']}\n\n"
                    "A partir de agora, consultas e criações usarão esta conta."
                ),
            )
            return "EVENT_RECEIVED", 200

        # Atualiza contexto caso a pessoa tenha acabado de escolher conta
        # em uma mensagem anterior.
        context = get_user_context(sender)

        # =================================================
        # CADASTRAR CLIENTE — ADMIN PLATAFORMA
        # =================================================

        if received_text.startswith("cadastrar cliente"):
            if not context["is_platform_admin"]:
                send_whatsapp_message(
                    sender,
                    "⛔ Este comando é exclusivo do administrador da plataforma.",
                )
                return "EVENT_RECEIVED", 200

            parts = [item.strip() for item in original_text.split("|")]

            if len(parts) != 5:
                send_whatsapp_message(
                    sender,
                    (
                        "❌ *FORMATO INCORRETO*\n\n"
                        "Use exatamente:\n\n"
                        "cadastrar cliente | Nome da empresa | 5531999999999 | sim | nao\n\n"
                        "O primeiro SIM/NAO = leitura.\n"
                        "O segundo SIM/NAO = criação."
                    ),
                )
                return "EVENT_RECEIVED", 200

            company_name = parts[1]
            new_whatsapp = parts[2]
            read_permission = text_to_bool(parts[3])
            create_permission = text_to_bool(parts[4])

            if read_permission is None or create_permission is None:
                send_whatsapp_message(
                    sender,
                    "❌ Nas permissões use apenas SIM ou NAO.",
                )
                return "EVENT_RECEIVED", 200

            client, error = create_client(
                company_name,
                new_whatsapp,
                read_permission,
                create_permission,
            )

            if error:
                send_whatsapp_message(
                    sender,
                    f"❌ Não consegui cadastrar o cliente.\n\n{error}",
                )
                return "EVENT_RECEIVED", 200

            log_activity(context, "client_created", client)

            send_whatsapp_message(
                sender,
                (
                    "✅ *CLIENTE CADASTRADO*\n\n"
                    f"*Empresa:*\n{company_name}\n\n"
                    f"*WhatsApp:*\n{client['whatsapp_number']}\n\n"
                    "O cliente já pode enviar *conectar meta* para vincular a própria conta."
                ),
            )
            return "EVENT_RECEIVED", 200

        if received_text == "listar clientes":
            if not context["is_platform_admin"]:
                send_whatsapp_message(sender, "⛔ Acesso negado.")
                return "EVENT_RECEIVED", 200

            clients = list_clients()
            lines = ["👥 *CLIENTES CADASTRADOS*\n"]

            for client in clients:
                conta = client["ad_account_id"] or "Sem Meta selecionada"
                lines.append(
                    f"\n*{client['company_name']}*\n"
                    f"WhatsApp: {client['whatsapp_number']}\n"
                    f"Meta: {conta}\n"
                )

            send_whatsapp_message(sender, "".join(lines))
            return "EVENT_RECEIVED", 200

        # =================================================
        # QUEM SOU EU
        # =================================================

        if received_text == "quem sou eu":
            conta = context.get("ad_account_id") or "Não selecionada"
            leitura = "SIM" if context["can_read_ads"] else "NÃO"
            criacao = "SIM" if context["can_create_campaigns"] else "NÃO"

            send_whatsapp_message(
                sender,
                (
                    "👤 *USUÁRIO IDENTIFICADO*\n\n"
                    f"*Empresa:*\n{context['company_name']}\n\n"
                    f"*WhatsApp:*\n{context['whatsapp_number']}\n\n"
                    f"*Conta Meta:*\n{conta}\n\n"
                    f"*Pode consultar Ads:*\n{leitura}\n\n"
                    f"*Pode criar campanhas:*\n{criacao}"
                ),
            )
            return "EVENT_RECEIVED", 200

        # =================================================
        # CRIAÇÃO DE CAMPANHA TESTE
        # =================================================

        if received_text == "criar campanha teste":
            if not context["can_create_campaigns"]:
                send_whatsapp_message(
                    sender,
                    "⛔ Você não possui permissão para criar campanhas.",
                )
                return "EVENT_RECEIVED", 200

            ad_account_id, access_token, credential_error = get_meta_credentials(context)

            if credential_error:
                send_whatsapp_message(
                    sender,
                    f"❌ {credential_error}\n\nEnvie *conectar meta* se necessário.",
                )
                return "EVENT_RECEIVED", 200

            send_whatsapp_message(
                sender,
                (
                    "⚠️ *CONFIRMAÇÃO NECESSÁRIA*\n\n"
                    f"Empresa:\n*{context['company_name']}*\n\n"
                    f"Conta:\n*{ad_account_id}*\n\n"
                    "Campanha:\n*TESTE API WHATSAPP - NAO ATIVAR*\n\n"
                    "Status:\n*PAUSADA*\n\n"
                    "Para continuar:\n\n*CONFIRMAR CAMPANHA TESTE*"
                ),
            )
            return "EVENT_RECEIVED", 200

        if received_text == "confirmar campanha teste":
            if not context["can_create_campaigns"]:
                send_whatsapp_message(sender, "⛔ Acesso negado.")
                return "EVENT_RECEIVED", 200

            ad_account_id, access_token, credential_error = get_meta_credentials(context)

            if credential_error:
                send_whatsapp_message(
                    sender,
                    f"❌ {credential_error}",
                )
                return "EVENT_RECEIVED", 200

            send_whatsapp_message(sender, "⏳ Criando campanha PAUSADA...")

            campaign_id, error = create_test_campaign(
                ad_account_id,
                access_token,
            )

            if error:
                print("ERRO CAMPANHA:", error)
                log_activity(
                    context,
                    "campaign_creation_failed",
                    {"error": error},
                )
                send_whatsapp_message(
                    sender,
                    "❌ Não consegui criar a campanha. Verifique os logs.",
                )
                return "EVENT_RECEIVED", 200

            log_activity(
                context,
                "campaign_created",
                {
                    "campaign_id": campaign_id,
                    "ad_account_id": ad_account_id,
                    "status": "PAUSED",
                },
            )

            send_whatsapp_message(
                sender,
                (
                    "✅ *CAMPANHA CRIADA*\n\n"
                    f"Empresa:\n*{context['company_name']}*\n\n"
                    f"Campaign ID:\n*{campaign_id}*\n\n"
                    "Status:\n*PAUSADA*"
                ),
            )
            return "EVENT_RECEIVED", 200

        # =================================================
        # RELATÓRIO / IA
        # =================================================

        if "gasto" in received_text and "7" in received_text:
            if not context["can_read_ads"]:
                send_whatsapp_message(sender, "⛔ Você não possui permissão de leitura.")
                return "EVENT_RECEIVED", 200

            ad_account_id, access_token, credential_error = get_meta_credentials(context)

            if credential_error:
                send_whatsapp_message(
                    sender,
                    f"❌ {credential_error}\n\nEnvie *conectar meta* se necessário.",
                )
                return "EVENT_RECEIVED", 200

            send_whatsapp_message(sender, "Consultando sua conta...")

            report, error = get_last_7_days_report(
                ad_account_id,
                access_token,
            )

            if error:
                send_whatsapp_message(sender, "Não consegui consultar o Meta Ads.")
                return "EVENT_RECEIVED", 200

            log_activity(context, "meta_report_7_days")
            send_whatsapp_message(sender, build_basic_report(report))
            return "EVENT_RECEIVED", 200

        if "analise" in received_text and "7" in received_text:
            if not context["can_read_ads"]:
                send_whatsapp_message(sender, "⛔ Você não possui permissão de leitura.")
                return "EVENT_RECEIVED", 200

            ad_account_id, access_token, credential_error = get_meta_credentials(context)

            if credential_error:
                send_whatsapp_message(
                    sender,
                    f"❌ {credential_error}\n\nEnvie *conectar meta* se necessário.",
                )
                return "EVENT_RECEIVED", 200

            report, error = get_last_7_days_report(
                ad_account_id,
                access_token,
            )

            if error:
                send_whatsapp_message(sender, "Não consegui consultar a conta.")
                return "EVENT_RECEIVED", 200

            send_whatsapp_message(sender, "Analisando os dados...")

            try:
                analysis = analyze_meta_report(report, original_text)
                log_activity(context, "ai_analysis_7_days")
                send_whatsapp_message(sender, analysis)
            except Exception as error:
                print("ERRO OPENAI:", repr(error))
                send_whatsapp_message(sender, "Erro ao gerar a análise.")

            return "EVENT_RECEIVED", 200

        # =================================================
        # MENU
        # =================================================

        menu = (
            "🤖 *ASSISTENTE META ADS*\n\n"
            f"Empresa: *{context['company_name']}*\n\n"
            "🔗 conectar meta\n\n"
            "📂 minhas contas meta\n\n"
            "👤 quem sou eu\n\n"
            "📊 gasto últimos 7 dias\n\n"
            "🤖 analise meus últimos 7 dias\n\n"
            "🔧 criar campanha teste"
        )

        if context["is_platform_admin"]:
            menu += (
                "\n\n━━━━━━━━━━━━━━\n\n"
                "🔐 *ADMINISTRAÇÃO*\n\n"
                "👥 listar clientes\n\n"
                "➕ cadastrar cliente"
            )

        send_whatsapp_message(sender, menu)

    except Exception as error:
        print("ERRO WEBHOOK:", repr(error))

    return "EVENT_RECEIVED", 200


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
