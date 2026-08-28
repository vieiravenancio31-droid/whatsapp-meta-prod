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
from zoneinfo import ZoneInfo
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
APP_BUILD = "dynamic-analysis-v2-2026-08-27"
print("APP BUILD:", APP_BUILD)


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

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_conversations (
                        user_id BIGINT PRIMARY KEY
                            REFERENCES users(id) ON DELETE CASCADE,
                        company_id BIGINT NOT NULL
                            REFERENCES companies(id) ON DELETE CASCADE,
                        previous_response_id TEXT,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
                    m.currency AS meta_account_currency,
                    m.timezone_name AS meta_account_timezone,
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
        "architecture": "multi_company_oauth_dynamic_ai_v2",
        "build": APP_BUILD,
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
# META ADS — LEADS / INSIGHTS / RELATÓRIO / CRIAÇÃO
# =========================================================

META_INSIGHT_LEVELS = {"account", "campaign", "adset", "ad"}
META_GRANULARITIES = {"aggregate", "daily"}
MAX_META_PAGES = 10
MAX_META_ROWS = 1000


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def action_value(actions, accepted_types):
    if not actions:
        return 0.0

    accepted = set(accepted_types)
    total = 0.0
    for item in actions:
        if item.get("action_type") in accepted:
            total += safe_float(item.get("value", 0))
    return total


def get_leads(actions):
    # Usamos prioridade para evitar somar versões diferentes do mesmo lead.
    if not actions:
        return 0.0

    actions_dict = {}
    for item in actions:
        action_type = item.get("action_type")
        if action_type:
            actions_dict[action_type] = safe_float(item.get("value", 0))

    lead_types = [
        "onsite_conversion.lead_grouped",
        "lead",
        "offsite_conversion.fb_pixel_lead",
        "omni_lead",
    ]

    for lead_type in lead_types:
        if lead_type in actions_dict:
            return actions_dict[lead_type]

    return 0.0


def get_landing_page_views(actions):
    return action_value(
        actions,
        [
            "landing_page_view",
            "omni_landing_page_view",
        ],
    )


def validate_iso_date(value, field_name):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{field_name} deve estar no formato YYYY-MM-DD."
        ) from error


def account_today(context):
    timezone_name = context.get("meta_account_timezone") or "UTC"
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).date()


def insight_fields_for_level(level):
    dimensions = {
        "account": ["account_id", "account_name"],
        "campaign": [
            "account_id",
            "account_name",
            "campaign_id",
            "campaign_name",
        ],
        "adset": [
            "account_id",
            "account_name",
            "campaign_id",
            "campaign_name",
            "adset_id",
            "adset_name",
        ],
        "ad": [
            "account_id",
            "account_name",
            "campaign_id",
            "campaign_name",
            "adset_id",
            "adset_name",
            "ad_id",
            "ad_name",
        ],
    }

    metrics = [
        "spend",
        "impressions",
        "reach",
        "frequency",
        "clicks",
        "inline_link_clicks",
        "ctr",
        "cpc",
        "cpm",
        "actions",
        "date_start",
        "date_stop",
    ]

    return dimensions[level] + metrics


def normalize_insight_row(row, level):
    spend = safe_float(row.get("spend"))
    impressions = safe_int(row.get("impressions"))
    reach = safe_int(row.get("reach"))
    clicks = safe_int(row.get("clicks"))
    link_clicks = safe_int(row.get("inline_link_clicks"))
    leads = get_leads(row.get("actions", []))
    landing_page_views = get_landing_page_views(row.get("actions", []))

    ctr = (clicks / impressions * 100) if impressions else 0.0
    link_ctr = (link_clicks / impressions * 100) if impressions else 0.0
    cpc = (spend / clicks) if clicks else 0.0
    link_cpc = (spend / link_clicks) if link_clicks else 0.0
    cpm = (spend / impressions * 1000) if impressions else 0.0
    cpl = (spend / leads) if leads else 0.0
    cost_per_lpv = (
        spend / landing_page_views if landing_page_views else 0.0
    )

    normalized = {
        "date_start": row.get("date_start"),
        "date_stop": row.get("date_stop"),
        "spend": round(spend, 4),
        "impressions": impressions,
        "reach": reach,
        "frequency": round(
            safe_float(row.get("frequency"), impressions / reach if reach else 0),
            4,
        ),
        "clicks": clicks,
        "link_clicks": link_clicks,
        "ctr": round(ctr, 4),
        "link_ctr": round(link_ctr, 4),
        "cpc": round(cpc, 4),
        "link_cpc": round(link_cpc, 4),
        "cpm": round(cpm, 4),
        "leads": round(leads, 4),
        "cpl": round(cpl, 4),
        "landing_page_views": round(landing_page_views, 4),
        "cost_per_landing_page_view": round(cost_per_lpv, 4),
    }

    for key in [
        "account_id",
        "account_name",
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "ad_id",
        "ad_name",
    ]:
        if row.get(key) is not None:
            normalized[key] = row.get(key)

    return normalized


def meta_get_paginated(url, params, max_pages=MAX_META_PAGES):
    rows = []
    next_url = url
    next_params = params
    pages = 0

    while next_url and pages < max_pages and len(rows) < MAX_META_ROWS:
        response = requests.get(
            next_url,
            params=next_params,
            timeout=45,
        )

        print(
            "META GET:",
            response.status_code,
            "endpoint=",
            next_url.split("?")[0],
        )

        if response.status_code != 200:
            try:
                error_payload = response.json()
            except Exception:
                error_payload = {"raw": response.text[:1000]}
            raise RuntimeError(
                "Meta API recusou a consulta: "
                + json.dumps(error_payload, ensure_ascii=False)
            )

        payload = response.json()
        rows.extend(payload.get("data", []))
        next_url = payload.get("paging", {}).get("next")
        next_params = None
        pages += 1

    return rows[:MAX_META_ROWS]


def query_meta_insights(
    ad_account_id,
    access_token,
    start_date,
    end_date,
    level="account",
    granularity="aggregate",
    campaign_id=None,
    adset_id=None,
    ad_id=None,
):
    level = (level or "account").lower()
    granularity = (granularity or "aggregate").lower()

    if level not in META_INSIGHT_LEVELS:
        raise ValueError(
            "level inválido. Use account, campaign, adset ou ad."
        )
    if granularity not in META_GRANULARITIES:
        raise ValueError(
            "granularity inválida. Use aggregate ou daily."
        )

    start = validate_iso_date(start_date, "start_date")
    end = validate_iso_date(end_date, "end_date")
    if start > end:
        raise ValueError("start_date não pode ser posterior a end_date.")

    days = (end - start).days + 1
    if days > 1095:
        raise ValueError("A consulta não pode ultrapassar 1095 dias.")
    if granularity == "daily" and days > 180:
        raise ValueError(
            "Para granularidade diária, use no máximo 180 dias por consulta."
        )

    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{ad_account_id}/insights"
    )

    params = {
        "access_token": access_token,
        "fields": ",".join(insight_fields_for_level(level)),
        "time_range": json.dumps(
            {"since": start.isoformat(), "until": end.isoformat()}
        ),
        "level": level,
        "limit": 100,
    }

    if granularity == "daily":
        params["time_increment"] = 1

    proof = appsecret_proof(access_token)
    if proof:
        params["appsecret_proof"] = proof

    raw_rows = meta_get_paginated(url, params)
    normalized = [normalize_insight_row(row, level) for row in raw_rows]

    # Filtro local. Evita depender de sintaxes de filtering diferentes
    # entre versões da Marketing API e mantém o motor previsível.
    if campaign_id:
        normalized = [
            row for row in normalized
            if str(row.get("campaign_id")) == str(campaign_id)
        ]
    if adset_id:
        normalized = [
            row for row in normalized
            if str(row.get("adset_id")) == str(adset_id)
        ]
    if ad_id:
        normalized = [
            row for row in normalized
            if str(row.get("ad_id")) == str(ad_id)
        ]

    return normalized


def summarize_insight_rows(rows):
    if not rows:
        return {
            "spend": 0.0,
            "impressions": 0,
            "reach": 0,
            "frequency": 0.0,
            "clicks": 0,
            "link_clicks": 0,
            "ctr": 0.0,
            "link_ctr": 0.0,
            "cpc": 0.0,
            "link_cpc": 0.0,
            "cpm": 0.0,
            "leads": 0.0,
            "cpl": 0.0,
            "landing_page_views": 0.0,
            "cost_per_landing_page_view": 0.0,
        }

    spend = sum(safe_float(row.get("spend")) for row in rows)
    impressions = sum(safe_int(row.get("impressions")) for row in rows)
    reach = sum(safe_int(row.get("reach")) for row in rows)
    clicks = sum(safe_int(row.get("clicks")) for row in rows)
    link_clicks = sum(safe_int(row.get("link_clicks")) for row in rows)
    leads = sum(safe_float(row.get("leads")) for row in rows)
    lpv = sum(safe_float(row.get("landing_page_views")) for row in rows)

    return {
        "spend": round(spend, 4),
        "impressions": impressions,
        "reach": reach,
        "frequency": round(impressions / reach, 4) if reach else 0.0,
        "clicks": clicks,
        "link_clicks": link_clicks,
        "ctr": round(clicks / impressions * 100, 4) if impressions else 0.0,
        "link_ctr": round(link_clicks / impressions * 100, 4)
        if impressions else 0.0,
        "cpc": round(spend / clicks, 4) if clicks else 0.0,
        "link_cpc": round(spend / link_clicks, 4) if link_clicks else 0.0,
        "cpm": round(spend / impressions * 1000, 4) if impressions else 0.0,
        "leads": round(leads, 4),
        "cpl": round(spend / leads, 4) if leads else 0.0,
        "landing_page_views": round(lpv, 4),
        "cost_per_landing_page_view": round(spend / lpv, 4) if lpv else 0.0,
    }


def compare_value(old_value, new_value):
    old = safe_float(old_value)
    new = safe_float(new_value)
    absolute = new - old
    percent = None if old == 0 else (absolute / old * 100)
    return {
        "absolute_change": round(absolute, 4),
        "percent_change": round(percent, 4) if percent is not None else None,
    }


def compare_summaries(summary_a, summary_b):
    comparison = {}
    for metric in [
        "spend",
        "impressions",
        "reach",
        "frequency",
        "clicks",
        "link_clicks",
        "ctr",
        "link_ctr",
        "cpc",
        "link_cpc",
        "cpm",
        "leads",
        "cpl",
        "landing_page_views",
        "cost_per_landing_page_view",
    ]:
        comparison[metric] = compare_value(
            summary_a.get(metric, 0),
            summary_b.get(metric, 0),
        )
    return comparison


def entity_key_for_level(level):
    return {
        "campaign": ("campaign_id", "campaign_name"),
        "adset": ("adset_id", "adset_name"),
        "ad": ("ad_id", "ad_name"),
    }.get(level)


def merge_entity_comparison(rows_a, rows_b, level, limit=30):
    key_pair = entity_key_for_level(level)
    if not key_pair:
        return []

    id_key, name_key = key_pair

    def aggregate(rows):
        grouped = {}
        for row in rows:
            entity_id = row.get(id_key)
            if not entity_id:
                continue
            bucket = grouped.setdefault(
                str(entity_id),
                {
                    "id": str(entity_id),
                    "name": row.get(name_key) or "Sem nome",
                    "rows": [],
                },
            )
            bucket["rows"].append(row)
        for bucket in grouped.values():
            bucket["summary"] = summarize_insight_rows(bucket.pop("rows"))
        return grouped

    a = aggregate(rows_a)
    b = aggregate(rows_b)
    ids = set(a) | set(b)
    merged = []

    for entity_id in ids:
        item_a = a.get(entity_id)
        item_b = b.get(entity_id)
        summary_a = item_a["summary"] if item_a else summarize_insight_rows([])
        summary_b = item_b["summary"] if item_b else summarize_insight_rows([])
        merged.append(
            {
                "id": entity_id,
                "name": (
                    (item_b or item_a or {}).get("name") or "Sem nome"
                ),
                "period_a": summary_a,
                "period_b": summary_b,
                "change": compare_summaries(summary_a, summary_b),
            }
        )

    merged.sort(
        key=lambda item: max(
            safe_float(item["period_a"].get("spend")),
            safe_float(item["period_b"].get("spend")),
        ),
        reverse=True,
    )
    return merged[: max(1, min(int(limit or 30), 100))]


def get_last_7_days_report(ad_account_id, access_token):
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=6)
    try:
        rows = query_meta_insights(
            ad_account_id,
            access_token,
            start.isoformat(),
            end.isoformat(),
            level="account",
        )
        return summarize_insight_rows(rows), None
    except Exception as error:
        return None, str(error)


def list_meta_structure(
    ad_account_id,
    access_token,
    entity_type,
    status="all",
    limit=50,
):
    entity_type = (entity_type or "campaign").lower()
    mapping = {
        "campaign": "campaigns",
        "adset": "adsets",
        "ad": "ads",
    }
    if entity_type not in mapping:
        raise ValueError("entity_type deve ser campaign, adset ou ad.")

    fields = {
        "campaign": "id,name,status,effective_status,objective,created_time,updated_time",
        "adset": (
            "id,name,status,effective_status,campaign_id,optimization_goal,"
            "billing_event,daily_budget,lifetime_budget,start_time,end_time,"
            "created_time,updated_time"
        ),
        "ad": (
            "id,name,status,effective_status,campaign_id,adset_id,"
            "created_time,updated_time"
        ),
    }[entity_type]

    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{ad_account_id}/{mapping[entity_type]}"
    )
    params = {
        "access_token": access_token,
        "fields": fields,
        "limit": min(max(int(limit or 50), 1), 100),
    }
    proof = appsecret_proof(access_token)
    if proof:
        params["appsecret_proof"] = proof

    rows = meta_get_paginated(url, params, max_pages=5)

    normalized_status = (status or "all").upper()
    if normalized_status != "ALL":
        rows = [
            row for row in rows
            if str(row.get("effective_status", "")).upper() == normalized_status
            or str(row.get("status", "")).upper() == normalized_status
        ]

    return rows[: min(max(int(limit or 50), 1), 100)]


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

    print("CRIAÇÃO CAMPANHA:", response.status_code, response.text[:1000])

    if response.status_code != 200:
        return None, response.text

    result = response.json()
    campaign_id = result.get("id")

    if not campaign_id:
        return None, "Meta não retornou o ID da campanha."

    return campaign_id, None


# =========================================================
# OPENAI — MEMÓRIA DE CONVERSA
# =========================================================

def get_ai_previous_response_id(context):
    initialize_database()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT previous_response_id
                FROM ai_conversations
                WHERE user_id = %s AND company_id = %s;
                """,
                (context["user_id"], context["company_id"]),
            )
            row = cursor.fetchone()
    return row.get("previous_response_id") if row else None


def save_ai_previous_response_id(context, response_id):
    if not response_id:
        return
    initialize_database()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ai_conversations (
                    user_id,
                    company_id,
                    previous_response_id,
                    updated_at
                )
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id)
                DO UPDATE SET
                    company_id = EXCLUDED.company_id,
                    previous_response_id = EXCLUDED.previous_response_id,
                    updated_at = NOW();
                """,
                (
                    context["user_id"],
                    context["company_id"],
                    response_id,
                ),
            )


def clear_ai_conversation(context):
    initialize_database()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM ai_conversations WHERE user_id = %s;",
                (context["user_id"],),
            )


# =========================================================
# OPENAI — FERRAMENTAS DE LEITURA META
# =========================================================

AI_TOOLS = [
    {
        "type": "function",
        "name": "consultar_insights",
        "description": (
            "Consulta métricas reais do Meta Ads em um período. Use para analisar "
            "conta, campanhas, conjuntos ou anúncios. Pode retornar agregado ou diário."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Data inicial no formato YYYY-MM-DD.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Data final no formato YYYY-MM-DD.",
                },
                "level": {
                    "type": "string",
                    "enum": ["account", "campaign", "adset", "ad"],
                    "description": "Nível da análise.",
                },
                "granularity": {
                    "type": "string",
                    "enum": ["aggregate", "daily"],
                    "description": "aggregate para total do período; daily para evolução diária.",
                },
                "campaign_id": {
                    "type": "string",
                    "description": "Opcional: restringe o resultado a uma campanha específica.",
                },
                "adset_id": {
                    "type": "string",
                    "description": "Opcional: restringe a um conjunto específico.",
                },
                "ad_id": {
                    "type": "string",
                    "description": "Opcional: restringe a um anúncio específico.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Quantidade máxima de linhas detalhadas devolvidas ao modelo.",
                },
            },
            "required": ["start_date", "end_date", "level", "granularity"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "comparar_periodos",
        "description": (
            "Compara dois períodos reais do Meta Ads e calcula as variações. "
            "Use para mês contra mês, semana contra semana ou períodos personalizados."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "period_a_start": {"type": "string"},
                "period_a_end": {"type": "string"},
                "period_b_start": {"type": "string"},
                "period_b_end": {"type": "string"},
                "level": {
                    "type": "string",
                    "enum": ["account", "campaign", "adset", "ad"],
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": [
                "period_a_start",
                "period_a_end",
                "period_b_start",
                "period_b_end",
                "level",
            ],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "listar_estrutura_meta",
        "description": (
            "Lista campanhas, conjuntos de anúncios ou anúncios da conta, com status e IDs. "
            "Use quando precisar descobrir qual entidade o usuário está mencionando."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "enum": ["campaign", "adset", "ad"],
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Status desejado, por exemplo ALL, ACTIVE, PAUSED, ARCHIVED. "
                        "Use ALL quando não houver filtro."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["entity_type", "status"],
            "additionalProperties": False,
        },
    },
]


def build_ai_instructions(context):
    today = account_today(context)
    timezone_name = context.get("meta_account_timezone") or "UTC"
    currency = context.get("meta_account_currency") or "não informada"

    return f"""
Você é um analista sênior de Meta Ads dentro de um produto conversacional.
Você está analisando EXCLUSIVAMENTE a conta Meta selecionada desta empresa.

DATA ATUAL NA CONTA: {today.isoformat()}
FUSO DA CONTA: {timezone_name}
MOEDA DA CONTA: {currency}
EMPRESA: {context.get('company_name')}
CONTA: {context.get('ad_account_id')}

REGRAS OBRIGATÓRIAS:
1. Para qualquer afirmação sobre desempenho real, consulte as ferramentas. Não invente dados.
2. Nunca invente campanhas, anúncios, conjuntos, valores, causas ou tendências.
3. Cálculos numéricos vêm do backend. Use os valores devolvidos pelas ferramentas.
4. Diferencie fato observado de hipótese/diagnóstico.
5. Se o usuário pedir comparação entre "este mês" e "mês passado", compare períodos equivalentes:
   do dia 1 até o dia atual em ambos os meses, salvo se ele pedir explicitamente meses completos.
6. Se o usuário citar meses/datas específicos, respeite exatamente o pedido.
7. Se uma resposta exigir aprofundamento, você pode consultar conta → campanha → conjunto → anúncio em sequência.
8. Se não houver dados suficientes para concluir a causa, diga quais dados mostram o problema e trate causas como hipóteses.
9. Não faça alterações na Meta. Suas ferramentas são apenas de leitura.
10. Responda em português do Brasil, de forma clara e útil para um gestor de tráfego/empresário.
11. Quando houver comparação, destaque números absolutos e percentuais relevantes.
12. Não despeje todas as métricas sem necessidade. Priorize as que respondem à pergunta.
13. Se o usuário pedir "por quê", investigue antes de concluir. Uma queda de CPL, CTR ou volume não prova sozinha a causa.

MÉTRICAS DISPONÍVEIS NAS CONSULTAS:
investimento (spend), impressões, alcance, frequência, cliques, cliques no link,
CTR, CTR de link, CPC, CPC de link, CPM, leads, CPL, visualizações de página de destino
e custo por visualização de página de destino.
""".strip()


def execute_ai_tool(context, tool_name, arguments):
    ad_account_id, access_token, credential_error = get_meta_credentials(context)
    if credential_error:
        return {"ok": False, "error": credential_error}

    try:
        if tool_name == "consultar_insights":
            level = arguments.get("level", "account")
            rows = query_meta_insights(
                ad_account_id,
                access_token,
                arguments["start_date"],
                arguments["end_date"],
                level=level,
                granularity=arguments.get("granularity", "aggregate"),
                campaign_id=arguments.get("campaign_id"),
                adset_id=arguments.get("adset_id"),
                ad_id=arguments.get("ad_id"),
            )
            limit = min(max(int(arguments.get("limit", 30)), 1), 100)
            summary = summarize_insight_rows(rows)
            return {
                "ok": True,
                "period": {
                    "start": arguments["start_date"],
                    "end": arguments["end_date"],
                },
                "level": level,
                "granularity": arguments.get("granularity", "aggregate"),
                "summary": summary,
                "rows_returned": min(len(rows), limit),
                "rows_available_in_query": len(rows),
                "rows": rows[:limit],
                "note": (
                    "Os totais em summary consideram todas as linhas obtidas; "
                    "rows pode estar limitado para caber na análise."
                ),
            }

        if tool_name == "comparar_periodos":
            level = arguments.get("level", "account")
            rows_a = query_meta_insights(
                ad_account_id,
                access_token,
                arguments["period_a_start"],
                arguments["period_a_end"],
                level=level,
                granularity="aggregate",
            )
            rows_b = query_meta_insights(
                ad_account_id,
                access_token,
                arguments["period_b_start"],
                arguments["period_b_end"],
                level=level,
                granularity="aggregate",
            )
            summary_a = summarize_insight_rows(rows_a)
            summary_b = summarize_insight_rows(rows_b)
            limit = min(max(int(arguments.get("limit", 30)), 1), 100)

            return {
                "ok": True,
                "period_a": {
                    "start": arguments["period_a_start"],
                    "end": arguments["period_a_end"],
                    "summary": summary_a,
                },
                "period_b": {
                    "start": arguments["period_b_start"],
                    "end": arguments["period_b_end"],
                    "summary": summary_b,
                },
                "change_b_vs_a": compare_summaries(summary_a, summary_b),
                "level": level,
                "entities": merge_entity_comparison(
                    rows_a,
                    rows_b,
                    level,
                    limit=limit,
                ),
            }

        if tool_name == "listar_estrutura_meta":
            rows = list_meta_structure(
                ad_account_id,
                access_token,
                arguments["entity_type"],
                status=arguments.get("status", "ALL"),
                limit=arguments.get("limit", 50),
            )
            return {
                "ok": True,
                "entity_type": arguments["entity_type"],
                "status_filter": arguments.get("status", "ALL"),
                "count": len(rows),
                "items": rows,
            }

        return {"ok": False, "error": f"Ferramenta desconhecida: {tool_name}"}

    except Exception as error:
        print("ERRO FERRAMENTA IA:", tool_name, repr(error))
        return {
            "ok": False,
            "error": str(error),
            "tool": tool_name,
        }


def ask_openai_about_meta(context, user_question):
    previous_response_id = get_ai_previous_response_id(context)
    instructions = build_ai_instructions(context)

    request_kwargs = {
        "model": OPENAI_MODEL,
        "reasoning": {"effort": "low"},
        "max_output_tokens": 1800,
        "instructions": instructions,
        "tools": AI_TOOLS,
        "input": user_question,
        "store": True,
    }

    if previous_response_id:
        request_kwargs["previous_response_id"] = previous_response_id

    print(
        "OPENAI: iniciando resposta",
        "previous_response_id=",
        bool(previous_response_id),
    )

    try:
        response = openai_client.responses.create(**request_kwargs)
    except Exception as error:
        # Se a conversa anterior expirou ou ficou inválida, tentamos uma vez
        # como uma conversa nova antes de falhar.
        if previous_response_id:
            print("OPENAI: contexto anterior falhou; reiniciando conversa:", repr(error))
            clear_ai_conversation(context)
            request_kwargs.pop("previous_response_id", None)
            response = openai_client.responses.create(**request_kwargs)
        else:
            raise

    for iteration in range(8):
        function_calls = [
            item for item in response.output
            if getattr(item, "type", None) == "function_call"
        ]

        print(
            "OPENAI: resposta recebida",
            "id=",
            response.id,
            "tool_calls=",
            len(function_calls),
            "iteration=",
            iteration,
        )

        if not function_calls:
            final_text = (response.output_text or "").strip()
            if not final_text:
                final_text = (
                    "Não consegui concluir a análise com uma resposta textual. "
                    "Tente reformular a pergunta."
                )
            save_ai_previous_response_id(context, response.id)
            return final_text

        tool_outputs = []
        for call in function_calls:
            try:
                arguments = json.loads(call.arguments or "{}")
            except json.JSONDecodeError as error:
                result = {
                    "ok": False,
                    "error": f"Argumentos inválidos enviados à ferramenta: {error}",
                }
            else:
                print(
                    "OPENAI TOOL:",
                    call.name,
                    json.dumps(arguments, ensure_ascii=False),
                )
                result = execute_ai_tool(
                    context,
                    call.name,
                    arguments,
                )

            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(
                        result,
                        ensure_ascii=False,
                        default=str,
                    ),
                }
            )

        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            reasoning={"effort": "low"},
            max_output_tokens=1800,
            instructions=instructions,
            tools=AI_TOOLS,
            previous_response_id=response.id,
            input=tool_outputs,
            store=True,
        )

    raise RuntimeError(
        "A análise excedeu o limite de aprofundamento de ferramentas."
    )


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
# ANÁLISE ASSÍNCRONA
# =========================================================

active_analysis_users = set()
active_analysis_lock = threading.Lock()


def start_analysis_job(sender, context, user_question):
    user_id = context["user_id"]

    with active_analysis_lock:
        if user_id in active_analysis_users:
            return False
        active_analysis_users.add(user_id)

    thread = threading.Thread(
        target=process_analysis_job,
        args=(sender, dict(context), user_question),
        daemon=True,
    )
    thread.start()
    return True


def process_analysis_job(sender, context, user_question):
    user_id = context["user_id"]
    print(
        "ANÁLISE ASSÍNCRONA: iniciando",
        "user_id=",
        user_id,
        "company_id=",
        context.get("company_id"),
    )

    try:
        answer = ask_openai_about_meta(context, user_question)
        log_activity(
            context,
            "ai_dynamic_analysis",
            {"question": user_question[:500]},
        )
        send_whatsapp_message(sender, answer)
        print("ANÁLISE ASSÍNCRONA: concluída", "user_id=", user_id)

    except Exception as error:
        print("ERRO ANÁLISE DINÂMICA:", repr(error))
        log_activity(
            context,
            "ai_dynamic_analysis_failed",
            {
                "question": user_question[:500],
                "error": str(error)[:1000],
            },
        )
        send_whatsapp_message(
            sender,
            (
                "❌ Não consegui concluir essa análise agora. "
                "O erro foi registrado para diagnóstico."
            ),
        )

    finally:
        with active_analysis_lock:
            active_analysis_users.discard(user_id)


# =========================================================
# WEBHOOK WHATSAPP
# =========================================================

@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True) or {}
    print("APP BUILD:", APP_BUILD)
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

            clear_ai_conversation(context)

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
                    "A partir de agora, consultas e análises usarão esta conta."
                ),
            )
            return "EVENT_RECEIVED", 200

        # Atualiza contexto caso a pessoa tenha escolhido a conta antes.
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

        if received_text in [
            "limpar conversa",
            "nova analise",
            "nova análise",
            "reiniciar analise",
            "reiniciar análise",
        ]:
            clear_ai_conversation(context)
            send_whatsapp_message(
                sender,
                "✅ Contexto da análise foi limpo. Pode começar uma nova pergunta.",
            )
            return "EVENT_RECEIVED", 200

        # =================================================
        # CRIAÇÃO DE CAMPANHA TESTE — MANTIDA SEPARADA DA IA
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
        # RELATÓRIO FIXO SIMPLES — CONTINUA DISPONÍVEL
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
                print("ERRO RELATÓRIO 7 DIAS:", error)
                send_whatsapp_message(sender, "Não consegui consultar o Meta Ads.")
                return "EVENT_RECEIVED", 200

            log_activity(context, "meta_report_7_days")
            send_whatsapp_message(sender, build_basic_report(report))
            return "EVENT_RECEIVED", 200

        # =================================================
        # MENU EXPLÍCITO
        # =================================================

        if received_text in ["menu", "ajuda", "comandos"]:
            menu = (
                "🤖 *ASSISTENTE META ADS*\n\n"
                f"Empresa: *{context['company_name']}*\n\n"
                "Agora você pode fazer perguntas naturais, por exemplo:\n\n"
                "• Compare este mês com o mês passado\n"
                "• Quais campanhas mais prejudicaram meu CPL este mês?\n"
                "• Analise os últimos 15 dias\n"
                "• Quais anúncios gastaram mais e trouxeram menos leads?\n\n"
                "Comandos:\n"
                "🔗 conectar meta\n"
                "📂 minhas contas meta\n"
                "👤 quem sou eu\n"
                "📊 gasto últimos 7 dias\n"
                "🧹 limpar conversa\n"
                "🔧 criar campanha teste"
            )

            if context["is_platform_admin"]:
                menu += (
                    "\n\n━━━━━━━━━━━━━━\n\n"
                    "🔐 *ADMINISTRAÇÃO*\n\n"
                    "👥 listar clientes\n"
                    "➕ cadastrar cliente"
                )

            send_whatsapp_message(sender, menu)
            return "EVENT_RECEIVED", 200

        # =================================================
        # PERGUNTA LIVRE → OPENAI + FERRAMENTAS META
        # =================================================

        if not context["can_read_ads"]:
            send_whatsapp_message(
                sender,
                "⛔ Você não possui permissão para consultar os dados do Meta Ads.",
            )
            return "EVENT_RECEIVED", 200

        ad_account_id, access_token, credential_error = get_meta_credentials(context)
        if credential_error:
            send_whatsapp_message(
                sender,
                f"❌ {credential_error}\n\nEnvie *conectar meta* se necessário.",
            )
            return "EVENT_RECEIVED", 200

        started = start_analysis_job(sender, context, original_text)
        if not started:
            send_whatsapp_message(
                sender,
                (
                    "⏳ Já existe uma análise sua em andamento. "
                    "Aguarde a resposta antes de enviar outra pergunta."
                ),
            )
            return "EVENT_RECEIVED", 200

        send_whatsapp_message(
            sender,
            (
                "🔎 Entendi. Vou consultar sua conta e analisar os dados necessários. "
                "Perguntas mais profundas podem levar alguns segundos."
            ),
        )
        return "EVENT_RECEIVED", 200

    except Exception as error:
        print("ERRO WEBHOOK:", repr(error))

    return "EVENT_RECEIVED", 200


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
