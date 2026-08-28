import os
import re
import json
import uuid
import base64
import hashlib
import hmac
import secrets
import threading
import traceback
import unicodedata
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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

BUILD_ID = "dynamic-analysis-v3-2026-08-27"
print("BOOT BUILD_ID:", BUILD_ID)


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

# Controle simples por processo para impedir duas análises simultâneas
# do mesmo usuário. Em escala horizontal, migrar para lock distribuído.
active_analysis_users = set()
active_analysis_lock = threading.Lock()


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
                    CREATE TABLE IF NOT EXISTS ai_conversation_states (
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
                    m.connection_id,
                    m.timezone_name AS meta_timezone_name
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


def get_ai_conversation_state(user_id):
    initialize_database()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT previous_response_id, updated_at
                FROM ai_conversation_states
                WHERE user_id = %s;
                """,
                (user_id,),
            )
            return cursor.fetchone()


def save_ai_conversation_state(context, response_id):
    if not response_id:
        return

    initialize_database()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ai_conversation_states (
                    user_id, company_id, previous_response_id, updated_at
                )
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id)
                DO UPDATE SET
                    company_id = EXCLUDED.company_id,
                    previous_response_id = EXCLUDED.previous_response_id,
                    updated_at = NOW();
                """,
                (context["user_id"], context["company_id"], response_id),
            )


def clear_ai_conversation_state(user_id):
    initialize_database()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM ai_conversation_states WHERE user_id = %s;",
                (user_id,),
            )


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
        "build_id": BUILD_ID,
        "dynamic_analysis": "enabled",
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


def send_whatsapp_long_message(to, text, max_chars=3500):
    text = str(text or "").strip()
    if not text:
        return []

    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n", 0, max_chars)
        if cut < int(max_chars * 0.6):
            cut = remaining.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)

    responses = []
    for chunk in chunks:
        responses.append(send_whatsapp_message(to, chunk))
    return responses


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
# META ADS — MOTOR DINÂMICO DE INSIGHTS
# =========================================================

ALLOWED_LEVELS = {"account", "campaign", "adset", "ad"}
ALLOWED_SORT_METRICS = {
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
}


def safe_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_int(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def metric_round(value, digits=4):
    if value is None:
        return None
    return round(float(value), digits)


def get_today_for_context(context):
    timezone_name = context.get("meta_timezone_name") or "America/Sao_Paulo"
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("America/Sao_Paulo")
    return datetime.now(tz).date()


def parse_iso_date(value, field_name):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{field_name} deve estar no formato YYYY-MM-DD."
        ) from error


def validate_date_range(since, until, today=None):
    since_date = parse_iso_date(since, "since")
    until_date = parse_iso_date(until, "until")

    if since_date > until_date:
        raise ValueError("A data inicial não pode ser posterior à data final.")

    if (until_date - since_date).days > 730:
        raise ValueError("O período máximo por consulta é de 731 dias.")

    if today and until_date > today:
        raise ValueError("A data final não pode estar no futuro.")

    return since_date, until_date


def extract_action_value(actions, preferred_types):
    if not actions:
        return 0.0

    values = {}
    for item in actions:
        action_type = item.get("action_type")
        if not action_type:
            continue
        values[action_type] = values.get(action_type, 0.0) + safe_float(
            item.get("value")
        )

    for action_type in preferred_types:
        if action_type in values:
            return values[action_type]

    return 0.0


def get_landing_page_views(actions):
    return extract_action_value(
        actions,
        [
            "landing_page_view",
            "offsite_conversion.fb_pixel_view_content",
        ],
    )


def build_metrics(spend, impressions, reach, clicks, link_clicks, leads, lpv):
    spend = safe_float(spend)
    impressions = safe_int(impressions)
    reach = safe_int(reach)
    clicks = safe_int(clicks)
    link_clicks = safe_int(link_clicks)
    leads = safe_float(leads)
    lpv = safe_float(lpv)

    return {
        "spend": metric_round(spend, 2),
        "impressions": impressions,
        "reach": reach,
        "frequency": metric_round(impressions / reach if reach else 0, 4),
        "clicks": clicks,
        "link_clicks": link_clicks,
        "ctr": metric_round((clicks / impressions * 100) if impressions else 0, 4),
        "link_ctr": metric_round(
            (link_clicks / impressions * 100) if impressions else 0,
            4,
        ),
        "cpc": metric_round(spend / clicks, 4) if clicks else (None if spend > 0 else 0),
        "link_cpc": (
            metric_round(spend / link_clicks, 4)
            if link_clicks
            else (None if spend > 0 else 0)
        ),
        "cpm": (
            metric_round(spend / impressions * 1000, 4)
            if impressions
            else (None if spend > 0 else 0)
        ),
        "leads": metric_round(leads, 2),
        "cpl": metric_round(spend / leads, 4) if leads else (None if spend > 0 else 0),
        "landing_page_views": metric_round(lpv, 2),
        "cost_per_landing_page_view": (
            metric_round(spend / lpv, 4)
            if lpv
            else (None if spend > 0 else 0)
        ),
    }


def entity_fields_for_level(level):
    if level == "campaign":
        return ["campaign_id", "campaign_name"]
    if level == "adset":
        return ["campaign_id", "campaign_name", "adset_id", "adset_name"]
    if level == "ad":
        return [
            "campaign_id",
            "campaign_name",
            "adset_id",
            "adset_name",
            "ad_id",
            "ad_name",
        ]
    return ["account_id", "account_name"]


def entity_id_for_row(row, level):
    if level == "campaign":
        return row.get("campaign_id")
    if level == "adset":
        return row.get("adset_id")
    if level == "ad":
        return row.get("ad_id")
    return row.get("account_id") or "account"


def entity_name_for_row(row, level):
    if level == "campaign":
        return row.get("campaign_name")
    if level == "adset":
        return row.get("adset_name")
    if level == "ad":
        return row.get("ad_name")
    return row.get("account_name") or "Conta"


def normalize_insight_row(raw, level):
    spend = safe_float(raw.get("spend"))
    impressions = safe_int(raw.get("impressions"))
    reach = safe_int(raw.get("reach"))
    clicks = safe_int(raw.get("clicks"))
    link_clicks = safe_int(raw.get("inline_link_clicks"))
    leads = get_leads(raw.get("actions", []))
    lpv = get_landing_page_views(raw.get("actions", []))

    row = {
        "date_start": raw.get("date_start"),
        "date_stop": raw.get("date_stop"),
    }

    for field in entity_fields_for_level(level):
        row[field] = raw.get(field)

    row.update(
        build_metrics(
            spend,
            impressions,
            reach,
            clicks,
            link_clicks,
            leads,
            lpv,
        )
    )
    return row


def aggregate_rows(rows):
    spend = sum(safe_float(row.get("spend")) for row in rows)
    impressions = sum(safe_int(row.get("impressions")) for row in rows)
    reach = sum(safe_int(row.get("reach")) for row in rows)
    clicks = sum(safe_int(row.get("clicks")) for row in rows)
    link_clicks = sum(safe_int(row.get("link_clicks")) for row in rows)
    leads = sum(safe_float(row.get("leads")) for row in rows)
    lpv = sum(safe_float(row.get("landing_page_views")) for row in rows)

    return build_metrics(
        spend,
        impressions,
        reach,
        clicks,
        link_clicks,
        leads,
        lpv,
    )


def meta_get_paginated(url, params, max_pages=20):
    rows = []
    next_url = url
    next_params = params
    page = 0

    while next_url and page < max_pages:
        print(f"[DYNAMIC] META_REQUEST page={page + 1} url={next_url}")
        response = requests.get(next_url, params=next_params, timeout=45)
        print(
            "[DYNAMIC] META_RESPONSE",
            response.status_code,
            response.text[:1000],
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Meta Insights retornou {response.status_code}: {response.text}"
            )

        payload = response.json()
        rows.extend(payload.get("data", []))
        next_url = payload.get("paging", {}).get("next")
        next_params = None
        page += 1

    return rows


def query_meta_insights(
    context,
    since,
    until,
    level="account",
    search=None,
    limit=25,
    sort_by="spend",
    sort_order="desc",
    min_spend=0,
    include_zero_spend=True,
):
    print(
        "[DYNAMIC] QUERY_INSIGHTS_START",
        {
            "since": since,
            "until": until,
            "level": level,
            "search": search,
            "limit": limit,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "min_spend": min_spend,
        },
    )

    if level not in ALLOWED_LEVELS:
        raise ValueError(f"Nível inválido: {level}")

    if sort_by not in ALLOWED_SORT_METRICS:
        raise ValueError(f"Métrica de ordenação inválida: {sort_by}")

    sort_order = (sort_order or "desc").lower()
    if sort_order not in {"asc", "desc"}:
        raise ValueError("sort_order deve ser asc ou desc.")

    limit = max(1, min(int(limit or 25), 100))
    min_spend = max(0.0, safe_float(min_spend))
    today = get_today_for_context(context)
    validate_date_range(since, until, today=today)

    ad_account_id, access_token, credential_error = get_meta_credentials(context)
    if credential_error:
        raise RuntimeError(credential_error)

    fields = [
        "account_id",
        "account_name",
        "date_start",
        "date_stop",
        "spend",
        "impressions",
        "reach",
        "clicks",
        "inline_link_clicks",
        "actions",
    ]

    for field in entity_fields_for_level(level):
        if field not in fields:
            fields.append(field)

    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{ad_account_id}/insights"
    )
    params = {
        "access_token": access_token,
        "fields": ",".join(fields),
        "time_range": json.dumps({"since": since, "until": until}),
        "level": level,
        "limit": 500,
    }

    proof = appsecret_proof(access_token)
    if proof:
        params["appsecret_proof"] = proof

    raw_rows = meta_get_paginated(url, params)
    normalized = [normalize_insight_row(row, level) for row in raw_rows]

    if search:
        needle = normalize_text(str(search))
        normalized = [
            row
            for row in normalized
            if needle in normalize_text(
                " ".join(
                    str(row.get(field) or "")
                    for field in entity_fields_for_level(level)
                )
            )
        ]

    normalized = [
        row for row in normalized
        if safe_float(row.get("spend")) >= min_spend
    ]

    if not include_zero_spend:
        normalized = [
            row for row in normalized
            if safe_float(row.get("spend")) > 0
        ]

    reverse = sort_order == "desc"

    def sort_key(row):
        value = row.get(sort_by)
        undefined_with_spend = value is None and safe_float(row.get("spend")) > 0
        # Em métricas de custo, ausência de conversão/clique com gasto é pior,
        # não um falso zero. No DESC sobe para o topo; no ASC vai para o fim.
        if sort_by in {
            "cpl",
            "cpc",
            "link_cpc",
            "cpm",
            "cost_per_landing_page_view",
        }:
            return (1 if undefined_with_spend else 0, safe_float(value))
        return (0, safe_float(value))

    normalized.sort(key=sort_key, reverse=reverse)

    full_summary = aggregate_rows(normalized)
    returned_rows = normalized[:limit]

    result = {
        "period": {"since": since, "until": until},
        "level": level,
        "account_id": ad_account_id,
        "matched_rows": len(normalized),
        "returned_rows": len(returned_rows),
        "summary": full_summary,
        "rows": returned_rows,
    }

    print(
        "[DYNAMIC] QUERY_INSIGHTS_DONE",
        {
            "matched_rows": result["matched_rows"],
            "returned_rows": result["returned_rows"],
            "summary": result["summary"],
        },
    )
    return result


def percent_change(old_value, new_value):
    old_value = safe_float(old_value)
    new_value = safe_float(new_value)
    if old_value == 0:
        return None
    return metric_round((new_value - old_value) / old_value * 100, 4)


def metric_delta(old_value, new_value):
    if old_value is None or new_value is None:
        return {
            "absolute": None,
            "percent": None,
            "status": "undefined_in_one_or_both_periods",
        }

    return {
        "absolute": metric_round(safe_float(new_value) - safe_float(old_value), 4),
        "percent": percent_change(old_value, new_value),
    }


def previous_month(year, month):
    if month == 1:
        return year - 1, 12
    return year, month - 1


def last_day_of_month(year, month):
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def resolve_comparison_periods(
    context,
    mode,
    n_days=None,
    period_a_since=None,
    period_a_until=None,
    period_b_since=None,
    period_b_until=None,
    period_a_label=None,
    period_b_label=None,
):
    today = get_today_for_context(context)

    if mode == "current_month_vs_previous_equivalent":
        py, pm = previous_month(today.year, today.month)
        equivalent_day = min(today.day, last_day_of_month(py, pm))
        return {
            "a": {
                "since": date(py, pm, 1).isoformat(),
                "until": date(py, pm, equivalent_day).isoformat(),
                "label": period_a_label or "Mês anterior (período equivalente)",
            },
            "b": {
                "since": date(today.year, today.month, 1).isoformat(),
                "until": date(today.year, today.month, equivalent_day).isoformat(),
                "label": period_b_label or "Mês atual (período equivalente)",
            },
        }

    if mode == "last_n_days_vs_previous_n_days":
        n_days = int(n_days or 7)
        if n_days < 1 or n_days > 365:
            raise ValueError("n_days deve estar entre 1 e 365.")

        b_until = today
        b_since = today - timedelta(days=n_days - 1)
        a_until = b_since - timedelta(days=1)
        a_since = a_until - timedelta(days=n_days - 1)
        return {
            "a": {
                "since": a_since.isoformat(),
                "until": a_until.isoformat(),
                "label": period_a_label or f"{n_days} dias anteriores",
            },
            "b": {
                "since": b_since.isoformat(),
                "until": b_until.isoformat(),
                "label": period_b_label or f"Últimos {n_days} dias",
            },
        }

    if mode == "custom":
        validate_date_range(period_a_since, period_a_until, today=today)
        validate_date_range(period_b_since, period_b_until, today=today)
        return {
            "a": {
                "since": period_a_since,
                "until": period_a_until,
                "label": period_a_label or "Período A",
            },
            "b": {
                "since": period_b_since,
                "until": period_b_until,
                "label": period_b_label or "Período B",
            },
        }

    raise ValueError(f"Modo de comparação inválido: {mode}")


def compare_meta_periods(
    context,
    mode,
    level="account",
    n_days=None,
    period_a_since=None,
    period_a_until=None,
    period_b_since=None,
    period_b_until=None,
    period_a_label=None,
    period_b_label=None,
    search=None,
    limit=25,
    sort_by="spend",
):
    periods = resolve_comparison_periods(
        context,
        mode,
        n_days=n_days,
        period_a_since=period_a_since,
        period_a_until=period_a_until,
        period_b_since=period_b_since,
        period_b_until=period_b_until,
        period_a_label=period_a_label,
        period_b_label=period_b_label,
    )

    print("[DYNAMIC] COMPARE_PERIODS_RESOLVED", periods)

    # Buscamos mais entidades internamente para cruzar os dois períodos;
    # o retorno ao modelo continua limitado.
    internal_limit = max(50, min(int(limit or 25) * 4, 100))

    report_a = query_meta_insights(
        context,
        periods["a"]["since"],
        periods["a"]["until"],
        level=level,
        search=search,
        limit=internal_limit,
        sort_by=sort_by,
        sort_order="desc",
        include_zero_spend=True,
    )
    report_b = query_meta_insights(
        context,
        periods["b"]["since"],
        periods["b"]["until"],
        level=level,
        search=search,
        limit=internal_limit,
        sort_by=sort_by,
        sort_order="desc",
        include_zero_spend=True,
    )

    summary_deltas = {}
    for metric in ALLOWED_SORT_METRICS:
        summary_deltas[metric] = metric_delta(
            report_a["summary"].get(metric),
            report_b["summary"].get(metric),
        )

    rows_a = {
        entity_id_for_row(row, level): row
        for row in report_a["rows"]
        if entity_id_for_row(row, level)
    }
    rows_b = {
        entity_id_for_row(row, level): row
        for row in report_b["rows"]
        if entity_id_for_row(row, level)
    }

    entity_ids = set(rows_a) | set(rows_b)
    entities = []
    zero_metrics = build_metrics(0, 0, 0, 0, 0, 0, 0)

    for entity_id in entity_ids:
        a = rows_a.get(entity_id)
        b = rows_b.get(entity_id)
        base = b or a or {}
        a_metrics = {metric: (a or {}).get(metric, zero_metrics.get(metric, 0)) for metric in ALLOWED_SORT_METRICS}
        b_metrics = {metric: (b or {}).get(metric, zero_metrics.get(metric, 0)) for metric in ALLOWED_SORT_METRICS}
        deltas = {
            metric: metric_delta(a_metrics.get(metric), b_metrics.get(metric))
            for metric in ALLOWED_SORT_METRICS
        }
        entities.append(
            {
                "id": entity_id,
                "name": entity_name_for_row(base, level),
                "period_a": a_metrics,
                "period_b": b_metrics,
                "deltas": deltas,
            }
        )

    def comparison_sort_key(item):
        value = item["period_b"].get(sort_by)
        undefined_with_spend = (
            value is None
            and safe_float(item["period_b"].get("spend")) > 0
        )
        if sort_by in {
            "cpl",
            "cpc",
            "link_cpc",
            "cpm",
            "cost_per_landing_page_view",
        }:
            return (1 if undefined_with_spend else 0, safe_float(value))
        return (0, safe_float(value))

    entities.sort(key=comparison_sort_key, reverse=True)
    entities = entities[: max(1, min(int(limit or 25), 50))]

    return {
        "mode": mode,
        "level": level,
        "period_a": {
            **periods["a"],
            "summary": report_a["summary"],
        },
        "period_b": {
            **periods["b"],
            "summary": report_b["summary"],
        },
        "summary_deltas_a_to_b": summary_deltas,
        "entities": entities,
        "note": (
            "Percentual nulo significa que o valor do período A era zero; "
            "não é possível calcular variação percentual convencional."
        ),
    }


def list_meta_structure(context, level="campaign", search=None, limit=50):
    if level not in {"campaign", "adset", "ad"}:
        raise ValueError("level deve ser campaign, adset ou ad.")

    ad_account_id, access_token, credential_error = get_meta_credentials(context)
    if credential_error:
        raise RuntimeError(credential_error)

    endpoint = {
        "campaign": "campaigns",
        "adset": "adsets",
        "ad": "ads",
    }[level]

    fields = ["id", "name", "status", "effective_status"]
    if level == "adset":
        fields.append("campaign_id")
    if level == "ad":
        fields.extend(["campaign_id", "adset_id"])

    url = (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        f"{ad_account_id}/{endpoint}"
    )
    params = {
        "access_token": access_token,
        "fields": ",".join(fields),
        "limit": 200,
    }
    proof = appsecret_proof(access_token)
    if proof:
        params["appsecret_proof"] = proof

    rows = meta_get_paginated(url, params, max_pages=10)

    if search:
        needle = normalize_text(str(search))
        rows = [
            row
            for row in rows
            if needle in normalize_text(
                f"{row.get('id', '')} {row.get('name', '')}"
            )
        ]

    limit = max(1, min(int(limit or 50), 100))
    return {
        "level": level,
        "matched_rows": len(rows),
        "rows": rows[:limit],
    }


# =========================================================
# OPENAI — MOTOR DINÂMICO / TOOL CALLING
# =========================================================

DYNAMIC_ANALYSIS_TOOLS = [
    {
        "type": "function",
        "name": "consultar_insights",
        "description": (
            "Consulta métricas reais de Meta Ads em um período exato, no nível "
            "de conta, campanha, conjunto ou anúncio. Use para responder perguntas "
            "quantitativas, rankings e aprofundamentos."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "Data inicial inclusiva no formato YYYY-MM-DD.",
                },
                "until": {
                    "type": "string",
                    "description": "Data final inclusiva no formato YYYY-MM-DD.",
                },
                "level": {
                    "type": "string",
                    "enum": ["account", "campaign", "adset", "ad"],
                },
                "search": {
                    "type": ["string", "null"],
                    "description": "Nome ou ID para filtrar uma entidade específica.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "sort_by": {
                    "type": "string",
                    "enum": sorted(ALLOWED_SORT_METRICS),
                },
                "sort_order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                },
                "min_spend": {"type": "number", "minimum": 0},
                "include_zero_spend": {"type": "boolean"},
            },
            "required": ["since", "until", "level"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "comparar_periodos",
        "description": (
            "Compara dois períodos de Meta Ads e calcula as variações no backend. "
            "Para 'este mês x mês passado', use current_month_vs_previous_equivalent. "
            "Para 'últimos N dias x N anteriores', use last_n_days_vs_previous_n_days. "
            "Para datas ou meses explicitamente pedidos, use custom."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": [
                        "current_month_vs_previous_equivalent",
                        "last_n_days_vs_previous_n_days",
                        "custom",
                    ],
                },
                "level": {
                    "type": "string",
                    "enum": ["account", "campaign", "adset", "ad"],
                },
                "n_days": {"type": ["integer", "null"], "minimum": 1, "maximum": 365},
                "period_a_since": {"type": ["string", "null"]},
                "period_a_until": {"type": ["string", "null"]},
                "period_b_since": {"type": ["string", "null"]},
                "period_b_until": {"type": ["string", "null"]},
                "period_a_label": {"type": ["string", "null"]},
                "period_b_label": {"type": ["string", "null"]},
                "search": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "sort_by": {
                    "type": "string",
                    "enum": sorted(ALLOWED_SORT_METRICS),
                },
            },
            "required": ["mode", "level"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "listar_estrutura_meta",
        "description": (
            "Lista campanhas, conjuntos ou anúncios da conta para localizar nomes/IDs "
            "e resolver referências do usuário. Não retorna performance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["campaign", "adset", "ad"],
                },
                "search": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["level"],
            "additionalProperties": False,
        },
    },
]


def build_dynamic_instructions(context):
    today = get_today_for_context(context)
    return f"""
Você é o analista sênior de Meta Ads de um produto SaaS conversacional.
Empresa atual: {context.get('company_name')}.
Conta Meta selecionada: {context.get('ad_account_id') or 'nenhuma'}.
Data atual na timezone da conta: {today.isoformat()}.

REGRAS ABSOLUTAS:
- Responda em português do Brasil, de forma prática, direta e analítica.
- Para qualquer afirmação quantitativa sobre a conta, use as ferramentas.
- Se o usuário não informar período e o contexto anterior não estabelecer um, use o mês atual até hoje.
- Nunca invente números, campanhas, conjuntos, anúncios ou causas.
- A ferramenta traz fatos; causas que não estejam provadas devem ser chamadas de hipótese.
- CPL, CTR, CPC, CPM e demais métricas derivadas são calculadas pelo backend.
- Quando o usuário disser 'este mês x mês passado' e o mês atual estiver incompleto,
  use comparar_periodos com mode=current_month_vs_previous_equivalent.
- Se o usuário pedir meses completos ou datas específicas, use mode=custom e respeite as datas.
- Para 'últimos N dias', quando houver comparação, use períodos de mesmo tamanho.
- Se precisar aprofundar uma campanha/conjunto/anúncio citado anteriormente, mantenha o contexto
  e consulte o nível inferior apropriado, filtrando pelo nome ou ID conhecido.
- Não execute criação ou alteração de campanhas por estas ferramentas; elas são somente leitura.
- Se a pergunta não for sobre Meta Ads, explique brevemente o escopo e ofereça exemplos do que pode analisar.
- Se não houver dados, diga explicitamente que a Meta não retornou dados para aquele recorte.

FORMATO:
Adapte o formato à pergunta. Em análises, prefira:
📊 Resumo
✅ O que melhorou
⚠️ O que piorou / atenção
🔎 Onde está o impacto
🎯 Próxima ação
Não force todas as seções quando uma resposta curta for suficiente.
"""


def execute_dynamic_tool(context, tool_name, arguments):
    print(f"[DYNAMIC] TOOL_EXECUTE name={tool_name} args={arguments}")

    if tool_name == "consultar_insights":
        return query_meta_insights(
            context,
            since=arguments["since"],
            until=arguments["until"],
            level=arguments.get("level", "account"),
            search=arguments.get("search"),
            limit=arguments.get("limit", 25),
            sort_by=arguments.get("sort_by", "spend"),
            sort_order=arguments.get("sort_order", "desc"),
            min_spend=arguments.get("min_spend", 0),
            include_zero_spend=arguments.get("include_zero_spend", True),
        )

    if tool_name == "comparar_periodos":
        return compare_meta_periods(
            context,
            mode=arguments["mode"],
            level=arguments.get("level", "account"),
            n_days=arguments.get("n_days"),
            period_a_since=arguments.get("period_a_since"),
            period_a_until=arguments.get("period_a_until"),
            period_b_since=arguments.get("period_b_since"),
            period_b_until=arguments.get("period_b_until"),
            period_a_label=arguments.get("period_a_label"),
            period_b_label=arguments.get("period_b_label"),
            search=arguments.get("search"),
            limit=arguments.get("limit", 25),
            sort_by=arguments.get("sort_by", "spend"),
        )

    if tool_name == "listar_estrutura_meta":
        return list_meta_structure(
            context,
            level=arguments["level"],
            search=arguments.get("search"),
            limit=arguments.get("limit", 50),
        )

    raise ValueError(f"Tool desconhecida: {tool_name}")


def create_dynamic_openai_response(context, user_question, previous_response_id=None):
    kwargs = {
        "model": OPENAI_MODEL,
        "reasoning": {"effort": "low"},
        "max_output_tokens": 1400,
        "instructions": build_dynamic_instructions(context),
        "tools": DYNAMIC_ANALYSIS_TOOLS,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "store": True,
        "input": user_question,
    }
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id

    return openai_client.responses.create(**kwargs)


def run_dynamic_openai_analysis(context, user_question):
    state = get_ai_conversation_state(context["user_id"])
    previous_response_id = state.get("previous_response_id") if state else None

    print(
        "[DYNAMIC] OPENAI_FIRST_CALL",
        {
            "model": OPENAI_MODEL,
            "has_previous_response": bool(previous_response_id),
        },
    )

    try:
        response = create_dynamic_openai_response(
            context,
            user_question,
            previous_response_id=previous_response_id,
        )
    except Exception as error:
        if previous_response_id:
            print(
                "[DYNAMIC] PREVIOUS_RESPONSE_FAILED_RETRY_WITHOUT_MEMORY",
                repr(error),
            )
            clear_ai_conversation_state(context["user_id"])
            response = create_dynamic_openai_response(
                context,
                user_question,
                previous_response_id=None,
            )
        else:
            raise

    print(
        "[DYNAMIC] OPENAI_FIRST_RESPONSE",
        {
            "response_id": response.id,
            "output_types": [getattr(item, "type", None) for item in response.output],
        },
    )

    max_tool_rounds = 6
    for round_number in range(1, max_tool_rounds + 1):
        tool_calls = [
            item for item in response.output
            if getattr(item, "type", None) == "function_call"
        ]

        if not tool_calls:
            final_text = (response.output_text or "").strip()
            if not final_text:
                raise RuntimeError("OpenAI não retornou texto final.")
            save_ai_conversation_state(context, response.id)
            return final_text, response.id

        print(
            f"[DYNAMIC] TOOL_ROUND {round_number}",
            [getattr(call, "name", None) for call in tool_calls],
        )

        tool_outputs = []
        for call in tool_calls:
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError as error:
                tool_result = {
                    "ok": False,
                    "error": f"Argumentos inválidos da ferramenta: {error}",
                }
            else:
                try:
                    result = execute_dynamic_tool(context, call.name, args)
                    tool_result = {"ok": True, "data": result}
                except Exception as tool_error:
                    print(
                        f"[DYNAMIC] TOOL_ERROR name={call.name}",
                        repr(tool_error),
                    )
                    traceback.print_exc()
                    tool_result = {
                        "ok": False,
                        "error": str(tool_error),
                    }

            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(
                        tool_result,
                        ensure_ascii=False,
                        default=str,
                    ),
                }
            )

        print(
            "[DYNAMIC] OPENAI_CONTINUATION_CALL",
            {
                "previous_response_id": response.id,
                "tool_outputs": len(tool_outputs),
            },
        )

        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            reasoning={"effort": "low"},
            max_output_tokens=1400,
            instructions=build_dynamic_instructions(context),
            tools=DYNAMIC_ANALYSIS_TOOLS,
            tool_choice="auto",
            parallel_tool_calls=False,
            store=True,
            previous_response_id=response.id,
            input=tool_outputs,
        )

        print(
            "[DYNAMIC] OPENAI_CONTINUATION_RESPONSE",
            {
                "response_id": response.id,
                "output_types": [
                    getattr(item, "type", None) for item in response.output
                ],
            },
        )

    raise RuntimeError("Limite de rodadas de ferramentas excedido.")


def acquire_analysis_lock(user_id):
    with active_analysis_lock:
        if user_id in active_analysis_users:
            return False
        active_analysis_users.add(user_id)
        return True


def release_analysis_lock(user_id):
    with active_analysis_lock:
        active_analysis_users.discard(user_id)


def process_dynamic_analysis(sender, user_id, original_text):
    print(
        "[DYNAMIC] JOB_START",
        {"sender": sender, "user_id": user_id, "question": original_text},
    )

    try:
        context = get_user_context(sender)
        print(
            "[DYNAMIC] USER_CONTEXT_OK",
            {
                "company_id": context.get("company_id") if context else None,
                "user_id": context.get("user_id") if context else None,
                "ad_account_id": context.get("ad_account_id") if context else None,
            },
        )

        if not context:
            send_whatsapp_message(
                sender,
                "⛔ Este número ainda não está cadastrado no sistema.",
            )
            return

        if not context.get("can_read_ads"):
            send_whatsapp_message(
                sender,
                "⛔ Você não possui permissão para consultar Meta Ads.",
            )
            return

        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY não configurada.")

        send_whatsapp_message(sender, "🔎 Analisando sua pergunta no Meta Ads...")

        log_activity(
            context,
            "dynamic_analysis_started",
            {"question": original_text, "build_id": BUILD_ID},
        )

        answer, response_id = run_dynamic_openai_analysis(context, original_text)

        print(
            "[DYNAMIC] FINAL_ANSWER_READY",
            {"response_id": response_id, "chars": len(answer)},
        )

        log_activity(
            context,
            "dynamic_analysis_completed",
            {
                "question": original_text,
                "response_id": response_id,
                "build_id": BUILD_ID,
            },
        )

        send_whatsapp_long_message(sender, answer)
        print("[DYNAMIC] JOB_DONE")

    except Exception as error:
        print("[DYNAMIC] JOB_ERROR:", repr(error))
        traceback.print_exc()

        try:
            context = get_user_context(sender)
            log_activity(
                context,
                "dynamic_analysis_failed",
                {
                    "question": original_text,
                    "error": repr(error),
                    "build_id": BUILD_ID,
                },
            )
        except Exception:
            traceback.print_exc()

        send_whatsapp_message(
            sender,
            (
                "❌ Ocorreu um erro ao processar a análise. "
                "A mensagem chegou ao motor dinâmico; verifique os logs do Railway "
                f"procurando por *{BUILD_ID}* e *[DYNAMIC] JOB_ERROR*."
            ),
        )
    finally:
        release_analysis_lock(user_id)
        print("[DYNAMIC] JOB_RELEASED", {"user_id": user_id})


# =========================================================
# OPENAI — ANÁLISE LEGADA DE 7 DIAS
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
        print(
            "[WEBHOOK] CONTEXT",
            {
                "build_id": BUILD_ID,
                "user_id": context.get("user_id") if context else None,
                "company_id": context.get("company_id") if context else None,
                "ad_account_id": context.get("ad_account_id") if context else None,
            },
        )

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

            # Evita que um follow-up use contexto de outra conta selecionada antes.
            clear_ai_conversation_state(context["user_id"])

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
        # MENU EXPLÍCITO / FALLBACK DE IA DINÂMICA
        # =================================================

        if received_text in ["menu", "ajuda", "help", "comandos"]:
            menu = (
                "🤖 *ASSISTENTE META ADS*\n\n"
                f"Empresa: *{context['company_name']}*\n\n"
                "Você pode perguntar em linguagem natural, por exemplo:\n\n"
                "• Compare este mês com o mês passado\n"
                "• Quais campanhas mais prejudicaram meu CPL?\n"
                "• Quais anúncios gastaram mais de R$ 500 sem gerar leads?\n"
                "• Compare os últimos 15 dias com os 15 anteriores\n"
                "• Agora aprofunde na campanha que você acabou de citar\n\n"
                "Comandos administrativos/técnicos:\n\n"
                "🔗 conectar meta\n"
                "📂 minhas contas meta\n"
                "👤 quem sou eu\n"
                "📊 gasto últimos 7 dias\n"
                "🤖 analise meus últimos 7 dias\n"
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

        print(
            "[DYNAMIC] ROUTING_TO_AI",
            {"build_id": BUILD_ID, "sender": sender, "text": original_text},
        )

        if not acquire_analysis_lock(context["user_id"]):
            send_whatsapp_message(
                sender,
                (
                    "⏳ Já existe uma análise sua em andamento. "
                    "Quando ela terminar, envie a próxima pergunta."
                ),
            )
            return "EVENT_RECEIVED", 200

        try:
            analysis_thread = threading.Thread(
                target=process_dynamic_analysis,
                args=(sender, context["user_id"], original_text),
                daemon=True,
                name=f"dynamic-analysis-{context['user_id']}",
            )
            analysis_thread.start()
            print(
                "[DYNAMIC] THREAD_STARTED",
                {
                    "thread": analysis_thread.name,
                    "alive": analysis_thread.is_alive(),
                    "build_id": BUILD_ID,
                },
            )
        except Exception:
            release_analysis_lock(context["user_id"])
            raise

        return "EVENT_RECEIVED", 200

    except Exception as error:
        print("ERRO WEBHOOK:", repr(error))
        traceback.print_exc()

    return "EVENT_RECEIVED", 200


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
