import os
import re
import json
import uuid
import io
import time
import mimetypes
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
from requests.adapters import HTTPAdapter
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, request
from openai import OpenAI
from psycopg.rows import dict_row


# =========================================================
# APP
# =========================================================

app = Flask(__name__)

BUILD_ID = "v10.2-hybrid-wizard-media-progress-2026-09-04"
ANALYSIS_ENGINE = "meta_driven_v9_1_action_ai_v10_2"
OBJECTIVE_MAPPING_HARDCODED = False
print("BOOT BUILD_ID:", BUILD_ID)
print("BOOT ANALYSIS_ENGINE:", ANALYSIS_ENGINE)


# =========================================================
# V9.1 — META-DRIVEN ANALYSIS
# =========================================================
# Esta versão NÃO possui tabela fixa objective -> KPI.
# Em tempo de execução, ela consulta a Meta para obter:
# - campaign.objective
# - adset.optimization_goal / optimization_sub_event
# - billing_event / promoted_object / destination_type
# - objective_results / cost_per_objective_result / cost_per_result quando disponíveis
# - actions / cost_per_action_type / action_values como fallback de evidência
# A OpenAI interpreta o setup real e, quando escolhe um action_type,
# o backend valida esse evento contra os Insights antes da conclusão.

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

# O motor dinâmico pode usar um modelo mais forte sem alterar o modelo legado.
# Para custo/qualidade, Terra é o padrão da v8; pode ser sobrescrito no Railway.
OPENAI_ANALYSIS_MODEL = os.getenv("OPENAI_ANALYSIS_MODEL", "gpt-5.6-terra")
OPENAI_ANALYSIS_REASONING_EFFORT = os.getenv(
    "OPENAI_ANALYSIS_REASONING_EFFORT",
    "medium",
).strip().lower()
if OPENAI_ANALYSIS_REASONING_EFFORT not in {
    "none", "low", "medium", "high", "xhigh", "max"
}:
    OPENAI_ANALYSIS_REASONING_EFFORT = "medium"

# V10 — áudio e Action AI
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
ACTION_AI_ENABLED = os.getenv("ACTION_AI_ENABLED", "true").strip().lower() in {"1", "true", "yes", "sim"}
REVIEW_WATCH_ENABLED = os.getenv("REVIEW_WATCH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "sim"}
try:
    REVIEW_WATCH_INTERVAL_SECONDS = max(60, int(os.getenv("REVIEW_WATCH_INTERVAL_SECONDS", "300")))
except ValueError:
    REVIEW_WATCH_INTERVAL_SECONDS = 300
WHATSAPP_REVIEW_APPROVED_TEMPLATE = os.getenv("WHATSAPP_REVIEW_APPROVED_TEMPLATE")
WHATSAPP_REVIEW_REJECTED_TEMPLATE = os.getenv("WHATSAPP_REVIEW_REJECTED_TEMPLATE")
WHATSAPP_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "pt_BR")

# V10.2 — Campaign Wizard / mídia / progresso
CAMPAIGN_WIZARD_ENABLED = os.getenv("CAMPAIGN_WIZARD_ENABLED", "true").strip().lower() in {"1", "true", "yes", "sim"}
try:
    PROGRESS_UPDATE_SECONDS = max(20, int(os.getenv("PROGRESS_UPDATE_SECONDS", "50")))
except ValueError:
    PROGRESS_UPDATE_SECONDS = 50
try:
    WIZARD_MAX_MEDIA_BYTES = max(1_000_000, int(os.getenv("WIZARD_MAX_MEDIA_BYTES", str(25 * 1024 * 1024))))
except ValueError:
    WIZARD_MAX_MEDIA_BYTES = 25 * 1024 * 1024

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_WHATSAPP_NUMBER = os.getenv("ADMIN_WHATSAPP_NUMBER")

GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v24.0")
DEFAULT_COMPANY_NAME = os.getenv("DEFAULT_COMPANY_NAME", "Empresa Principal")


# =========================================================
# OPENAI / HTTP
# =========================================================

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Reaproveita conexões HTTP para reduzir latência/overhead sem adicionar dependências.
http_session = requests.Session()
http_session.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=0))


# =========================================================
# CONTROLE DE INICIALIZAÇÃO DO BANCO
# =========================================================

database_initialized = False
database_lock = threading.Lock()

# Controle simples por processo para impedir duas análises simultâneas
# do mesmo usuário. Em escala horizontal, migrar para lock distribuído.
active_analysis_users = set()
active_analysis_lock = threading.Lock()
review_watcher_started = False
review_watcher_lock = threading.Lock()


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

                # V10 — estado operacional, idempotência e auditoria de ações.
                cursor.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS last_inbound_at TIMESTAMPTZ;
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS inbound_messages (
                        message_id TEXT PRIMARY KEY,
                        company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
                        user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                        whatsapp_number TEXT,
                        message_type TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_actions (
                        id BIGSERIAL PRIMARY KEY,
                        action_key TEXT NOT NULL UNIQUE,
                        company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        ad_account_id TEXT NOT NULL,
                        action_type TEXT NOT NULL,
                        target_type TEXT,
                        target_id TEXT,
                        spec JSONB NOT NULL DEFAULT '{}'::jsonb,
                        summary TEXT,
                        status TEXT NOT NULL DEFAULT 'PENDING_CONFIRMATION',
                        requires_confirmation BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        confirmed_at TIMESTAMPTZ,
                        executed_at TIMESTAMPTZ,
                        result JSONB,
                        error TEXT
                    );
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_pending_actions_user_status
                    ON pending_actions(user_id, status, created_at DESC);
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ad_review_watch (
                        id BIGSERIAL PRIMARY KEY,
                        company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                        user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                        whatsapp_number TEXT NOT NULL,
                        ad_account_id TEXT NOT NULL,
                        campaign_id TEXT,
                        adset_id TEXT,
                        ad_id TEXT NOT NULL,
                        ad_name TEXT,
                        last_effective_status TEXT,
                        last_review_feedback JSONB,
                        last_issues_info JSONB,
                        notified_state TEXT,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE(company_id, ad_id)
                    );
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_notifications (
                        id BIGSERIAL PRIMARY KEY,
                        company_id BIGINT REFERENCES companies(id) ON DELETE CASCADE,
                        user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                        whatsapp_number TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        body TEXT NOT NULL,
                        delivered_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )

                # V10.2 — estado persistente do Campaign Creation Wizard.
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS campaign_wizards (
                        id BIGSERIAL PRIMARY KEY,
                        company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        ad_account_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'DRAFT',
                        draft JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_ids JSONB,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        completed_at TIMESTAMPTZ
                    );
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_campaign_wizards_user_status
                    ON campaign_wizards(user_id, status, updated_at DESC);
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS campaign_wizard_assets (
                        id BIGSERIAL PRIMARY KEY,
                        wizard_id BIGINT NOT NULL REFERENCES campaign_wizards(id) ON DELETE CASCADE,
                        company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                        whatsapp_media_id TEXT,
                        media_type TEXT NOT NULL,
                        mime_type TEXT,
                        original_name TEXT,
                        caption TEXT,
                        meta_image_hash TEXT,
                        meta_video_id TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE(company_id, whatsapp_media_id)
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
        ensure_review_watcher_started()


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
                    m.timezone_name AS meta_timezone_name,
                    m.currency AS meta_currency
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
        "analysis_engine": ANALYSIS_ENGINE,
        "objective_aware": "meta_driven",
        "analysis_model": OPENAI_ANALYSIS_MODEL,
        "analysis_reasoning_effort": OPENAI_ANALYSIS_REASONING_EFFORT,
        "action_ai": "enabled" if ACTION_AI_ENABLED else "disabled",
        "audio_input": "enabled",
        "image_video_input": "enabled",
        "campaign_wizard": "enabled" if CAMPAIGN_WIZARD_ENABLED else "disabled",
        "progress_update_seconds": PROGRESS_UPDATE_SECONDS,
        "review_watcher": "enabled" if REVIEW_WATCH_ENABLED else "disabled",
        "transcribe_model": OPENAI_TRANSCRIBE_MODEL,
    }, 200


@app.route("/format-test", methods=["GET"])
def format_test():
    raw = """### **📊 RESUMO**
**Investimento:** R$ 12.450
**Leads:** 184
**CPL:** R$ 67,66

## ✅ PONTOS POSITIVOS
- A campanha **Imersão** melhorou.

### ⚠️ PONTOS DE ATENÇÃO
* A campanha Sul concentrou gasto.

### 🎯 PRÓXIMA AÇÃO
Investigue os anúncios da campanha Sul."""
    return {
        "build_id": BUILD_ID,
        "formatted": clean_ai_text_for_whatsapp(raw),
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

    response = http_session.post(
        url,
        headers=headers,
        json=payload,
        timeout=30,
    )

    print("RESPOSTA WHATSAPP:", response.status_code, response.text)
    return response


def clean_ai_text_for_whatsapp(text):
    """
    Formata respostas analíticas para WhatsApp de forma DETERMINÍSTICA.

    Regra desta versão:
    - nenhum #;
    - nenhum *;
    - nenhum **;
    - nenhum bloco de código;
    - nenhuma tabela Markdown;
    - títulos com emoji + MAIÚSCULAS;
    - listas com •;
    - espaçamento curto e consistente.

    Assim, a apresentação final não depende da OpenAI obedecer ao prompt.
    """
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    # Remove cercas e links Markdown, preservando o conteúdo útil.
    text = re.sub(r"```(?:[a-zA-Z0-9_+.-]+)?\s*", "", text)
    text = text.replace("```", "")
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1: \2", text)

    section_emojis = {
        "resumo": "📊",
        "visao geral": "📊",
        "resultado": "📊",
        "resultados": "📊",
        "comparacao": "📊",
        "comparativo": "📊",
        "metricas": "📈",
        "principais metricas": "📈",
        "evidencias": "📌",
        "evidencia": "📌",
        "o que isso significa": "🔎",
        "leitura": "🔎",
        "o que eu faria agora": "🎯",
        "pontos positivos": "✅",
        "ponto positivo": "✅",
        "melhor sinal": "✅",
        "destaques positivos": "✅",
        "o que melhorou": "✅",
        "pontos de atencao": "⚠️",
        "ponto de atencao": "⚠️",
        "atencao": "⚠️",
        "alerta": "⚠️",
        "alertas": "⚠️",
        "problemas": "⚠️",
        "riscos": "⚠️",
        "o que piorou": "⚠️",
        "proxima acao": "🎯",
        "proximas acoes": "🎯",
        "recomendacao": "🎯",
        "recomendacoes": "🎯",
        "o que fazer": "🎯",
        "prioridade": "🎯",
        "prioridades": "🎯",
        "campanhas": "📣",
        "campanha": "🎯",
        "anuncios": "🎨",
        "anuncio": "🎨",
        "conjunto de anuncios": "👥",
        "conjuntos": "👥",
        "conjuntos de anuncios": "🧩",
        "conclusao": "🧠",
        "diagnostico": "🧠",
    }

    def strip_markup(value):
        value = value.strip()
        value = re.sub(r"^>+\s*", "", value)
        value = re.sub(r"^#{1,6}\s*", "", value)
        value = re.sub(r"\s*#+$", "", value)
        value = value.replace("**", "")
        value = value.replace("__", "")
        value = value.replace("~~", "")
        value = value.replace("`", "")
        value = value.replace("*", "")
        # Remove apenas underscores usados como ênfase; não mexe em URLs/IDs inteiros.
        value = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", value)
        value = re.sub(r"[ \t]{2,}", " ", value)
        return value.strip()

    def normalize_key(value):
        value = strip_markup(value)
        value = re.sub(r"^[^\wÀ-ÿ]+", "", value)
        value = re.sub(r"[^\wÀ-ÿ]+$", "", value)
        normalized = unicodedata.normalize("NFD", value.lower())
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        return re.sub(r"\s+", " ", normalized).strip()

    def split_emoji(value):
        match = re.match(r"^([\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]+)\s*", value)
        if not match:
            return None, value
        return match.group(1).strip(), value[match.end():].strip()

    lines = []
    for raw in text.split("\n"):
        raw = raw.strip()
        if not raw:
            lines.append("")
            continue

        # Descarta separadores e linha de separação de tabela Markdown.
        if re.fullmatch(r"[-*_~| ]{3,}", raw):
            continue
        if re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?", raw):
            continue

        # Tabelas simples viram texto legível.
        if "|" in raw:
            cells = [strip_markup(c) for c in raw.strip("|").split("|")]
            cells = [c for c in cells if c]
            if len(cells) == 2:
                raw = f"{cells[0]}: {cells[1]}"
            elif len(cells) > 2:
                raw = " • ".join(cells)

        # Lista Markdown/numerada -> bullet único.
        if re.match(r"^(?:[-+*•·]|\d+[.)])\s+", raw):
            body = re.sub(r"^(?:[-+*•·]|\d+[.)])\s+", "", raw)
            body = strip_markup(body)
            if body:
                lines.append(f"• {body}")
            continue

        was_heading = bool(re.match(r"^#{1,6}\s*", raw))
        cleaned = strip_markup(raw)
        if not cleaned:
            continue

        existing_emoji, title_candidate = split_emoji(cleaned)
        title_candidate = title_candidate.rstrip(":").strip()
        key = normalize_key(title_candidate)
        is_upper_heading = (
            len(title_candidate) <= 60
            and len(title_candidate.split()) <= 8
            and any(ch.isalpha() for ch in title_candidate)
            and title_candidate.upper() == title_candidate
        )
        is_known_heading = key in section_emojis

        if was_heading or is_known_heading or (existing_emoji and is_upper_heading):
            emoji = existing_emoji or section_emojis.get(key, "")
            title = title_candidate.upper()
            lines.append(f"{emoji + ' ' if emoji else ''}{title}")
            continue

        cleaned = re.sub(r"^[•·]\s*", "", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if cleaned:
            lines.append(cleaned)

    # Colapsa linhas vazias duplicadas.
    compact = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        compact.append(line)
        previous_blank = blank

    result = "\n".join(compact).strip()

    # Trava final: a resposta analítica enviada ao WhatsApp não pode conter
    # os marcadores que estavam poluindo a mensagem.
    result = result.replace("#", "")
    result = result.replace("*", "")
    result = result.replace("```", "")
    result = re.sub(r"\n{3,}", "\n\n", result)

    return improve_whatsapp_readability(result.strip())


def improve_whatsapp_readability(text, max_paragraph_chars=420):
    """Mantém respostas fáceis de escanear no WhatsApp sem depender de Markdown."""
    text = str(text or "").strip()
    if not text:
        return ""

    def looks_like_heading(line):
        core = re.sub(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]+\s*", "", line).strip().rstrip(":")
        return (
            1 <= len(core) <= 70
            and any(ch.isalpha() for ch in core)
            and core.upper() == core
            and len(core.split()) <= 9
        )

    def append_wrapped(line, output):
        line = line.strip()
        if not line:
            return
        if len(line) <= max_paragraph_chars or line.startswith("• "):
            output.append(line)
            return
        sentences = re.split(r"(?<=[.!?])\s+", line)
        current = []
        current_len = 0
        for sentence in sentences:
            projected = current_len + (1 if current else 0) + len(sentence)
            if current and projected > max_paragraph_chars:
                output.append(" ".join(current).strip())
                current = [sentence]
                current_len = len(sentence)
            else:
                current.append(sentence)
                current_len = projected
        if current:
            output.append(" ".join(current).strip())

    paragraphs = []
    for block in re.split(r"\n\s*\n", text):
        block_lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not block_lines:
            continue

        # Se o bloco começou por um título, deixa o título isolado para criar respiro visual.
        if looks_like_heading(block_lines[0]):
            paragraphs.append(block_lines[0])
            block_lines = block_lines[1:]

        if not block_lines:
            continue

        if all(line.startswith("• ") for line in block_lines):
            paragraphs.append("\n".join(block_lines))
            continue

        for line in block_lines:
            append_wrapped(line, paragraphs)

    return "\n\n".join(paragraphs).strip()


def send_whatsapp_long_message(to, text, max_chars=3500):
    text = clean_ai_text_for_whatsapp(text)
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
# V10.2 — PROGRESSO / ERROS AMIGÁVEIS
# =========================================================

class WhatsAppProgressHeartbeat:
    def __init__(self, sender, interval_seconds=None):
        self.sender = sender
        self.interval_seconds = int(interval_seconds or PROGRESS_UPDATE_SECONDS)
        self.stop_event = threading.Event()
        self.thread = None
        self.started_at = None

    def start(self):
        self.started_at = time.monotonic()
        send_whatsapp_message(self.sender, "🔎 Analisando sua solicitação no Meta Ads...")
        self.thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"progress-{self.sender}",
        )
        self.thread.start()
        return self

    def _loop(self):
        count = 0
        while not self.stop_event.wait(self.interval_seconds):
            count += 1
            elapsed = count * self.interval_seconds
            if count == 1:
                body = (
                    f"⏳ Ainda estou analisando sua solicitação. Já se passaram {elapsed} segundos. "
                    "Estou cruzando os dados no Meta Ads para te responder com segurança."
                )
            elif count == 2:
                body = (
                    f"🔎 A análise continua em andamento. Já se passaram {elapsed} segundos. "
                    "Estou aprofundando a verificação — aguarde mais um pouco."
                )
            else:
                body = (
                    f"⏳ Continuo trabalhando na sua solicitação. Já se passaram {elapsed} segundos. "
                    "O processamento segue normalmente e eu te aviso assim que concluir."
                )
            try:
                send_whatsapp_message(self.sender, body)
            except Exception as error:
                print("[V10.2] PROGRESS_MESSAGE_ERROR", repr(error))

    def stop(self):
        self.stop_event.set()


def start_request_progress(sender):
    return WhatsAppProgressHeartbeat(sender).start()


def build_user_friendly_error(error, stage="solicitação"):
    raw = str(error or "")
    normalized = normalize_text(raw)

    if "expirada" in normalized or "expired" in normalized or "oauth" in normalized or "access token" in normalized:
        return (
            "🔐 PRECISO RECONECTAR A META\n\n"
            "Não consegui concluir porque a autorização da sua conta Meta precisa ser renovada.\n\n"
            "Envie: conectar meta\n\n"
            "Depois disso, você pode repetir a solicitação."
        )
    if "permission" in normalized or "permiss" in normalized or "acesso negado" in normalized:
        return (
            "⛔ NÃO CONSEGUI CONCLUIR\n\n"
            "A Meta não autorizou uma das etapas necessárias para essa ação. "
            "Sua estrutura atual não foi alterada.\n\n"
            "Vou precisar que a permissão da conta seja revisada antes de tentar novamente."
        )
    if "timeout" in normalized or "timed out" in normalized or "tempo" in normalized:
        return (
            "⏱️ A CONSULTA DEMOROU MAIS QUE O ESPERADO\n\n"
            "Não finalizei a operação para evitar uma resposta incompleta ou uma alteração pela metade.\n\n"
            "Sua conta permanece segura. Você pode repetir a solicitação em alguns instantes."
        )
    if "transcri" in normalized or "audio" in normalized:
        return (
            "🎙️ NÃO CONSEGUI INTERPRETAR O ÁUDIO\n\n"
            "O arquivo chegou, mas eu não consegui transformar a mensagem em uma solicitação confiável.\n\n"
            "Tente enviar o áudio novamente ou escreva a solicitação em texto."
        )
    return (
        "❌ NÃO CONSEGUI CONCLUIR ESSA SOLICITAÇÃO\n\n"
        f"Tive um problema durante o processamento da {stage}. "
        "Para sua segurança, não vou assumir que alguma alteração foi concluída.\n\n"
        "Se a solicitação envolvia mudança na Meta, confira: nenhuma nova ação será ativada automaticamente. "
        "Você pode tentar novamente e eu retomo a partir do ponto seguro."
    )


# =========================================================
# V10 — WHATSAPP ÁUDIO / IDPOTÊNCIA / NOTIFICAÇÕES
# =========================================================

def register_inbound_message(context, message_id, message_type, whatsapp_number):
    """Retorna True somente na primeira vez que o webhook processa este message_id."""
    if not message_id:
        return True
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO inbound_messages (
                        message_id, company_id, user_id, whatsapp_number, message_type
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (message_id) DO NOTHING
                    RETURNING message_id;
                    """,
                    (
                        message_id,
                        context.get("company_id") if context else None,
                        context.get("user_id") if context else None,
                        whatsapp_number,
                        message_type,
                    ),
                )
                return cursor.fetchone() is not None
    except Exception as error:
        print("[V10] INBOUND_IDEMPOTENCY_ERROR", repr(error))
        # Falhar aberto aqui evita perder uma mensagem legítima por erro de auditoria.
        return True


def touch_last_inbound(context):
    if not context:
        return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET last_inbound_at = NOW() WHERE id = %s;",
                    (context["user_id"],),
                )
    except Exception as error:
        print("[V10] LAST_INBOUND_ERROR", repr(error))


def send_whatsapp_template(to, template_name, language_code=None):
    if not template_name:
        return None
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
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code or WHATSAPP_TEMPLATE_LANGUAGE},
        },
    }
    response = http_session.post(url, headers=headers, json=payload, timeout=30)
    print("[V10] WHATSAPP_TEMPLATE:", response.status_code, response.text)
    return response


def queue_pending_notification(context, whatsapp_number, kind, body):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO pending_notifications (
                        company_id, user_id, whatsapp_number, kind, body
                    ) VALUES (%s, %s, %s, %s, %s);
                    """,
                    (
                        context.get("company_id") if context else None,
                        context.get("user_id") if context else None,
                        whatsapp_number,
                        kind,
                        body,
                    ),
                )
    except Exception as error:
        print("[V10] PENDING_NOTIFICATION_ERROR", repr(error))


def deliver_pending_notifications(context, whatsapp_number):
    if not context:
        return
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, body
                    FROM pending_notifications
                    WHERE user_id = %s AND delivered_at IS NULL
                    ORDER BY id ASC
                    LIMIT 10;
                    """,
                    (context["user_id"],),
                )
                rows = cursor.fetchall()
                for row in rows:
                    response = send_whatsapp_message(whatsapp_number, row["body"])
                    if response is not None and response.status_code < 300:
                        cursor.execute(
                            "UPDATE pending_notifications SET delivered_at = NOW() WHERE id = %s;",
                            (row["id"],),
                        )
    except Exception as error:
        print("[V10] DELIVER_PENDING_NOTIFICATION_ERROR", repr(error))


def retrieve_whatsapp_media(media_id):
    if not media_id:
        raise ValueError("Media ID ausente.")
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_TOKEN/PHONE_NUMBER_ID não configurados.")

    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    metadata_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}"
    metadata_response = requests.get(
        metadata_url,
        params={"phone_number_id": PHONE_NUMBER_ID},
        headers=headers,
        timeout=30,
    )
    if metadata_response.status_code != 200:
        raise RuntimeError(
            f"Não foi possível obter a URL do áudio: "
            f"{metadata_response.status_code} {metadata_response.text}"
        )
    metadata = metadata_response.json()
    media_url = metadata.get("url")
    if not media_url:
        raise RuntimeError("WhatsApp não retornou URL da mídia.")

    download_response = http_session.get(media_url, headers=headers, timeout=60)
    if download_response.status_code != 200:
        raise RuntimeError(
            f"Não foi possível baixar o áudio: "
            f"{download_response.status_code} {download_response.text}"
        )

    return {
        "content": download_response.content,
        "mime_type": metadata.get("mime_type") or download_response.headers.get("Content-Type") or "audio/ogg",
        "file_size": metadata.get("file_size") or len(download_response.content),
    }


def audio_filename_for_mime(mime_type):
    mime_type = (mime_type or "").split(";")[0].strip().lower()
    mapping = {
        "audio/ogg": ".ogg",
        "audio/opus": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".mp4",
        "audio/aac": ".aac",
        "audio/amr": ".amr",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/webm": ".webm",
    }
    return "whatsapp_audio" + mapping.get(mime_type, mimetypes.guess_extension(mime_type) or ".ogg")


def transcribe_whatsapp_audio(media_id):
    media = retrieve_whatsapp_media(media_id)
    file_obj = io.BytesIO(media["content"])
    file_obj.name = audio_filename_for_mime(media.get("mime_type"))
    print(
        "[V10] AUDIO_TRANSCRIBE_START",
        {"mime_type": media.get("mime_type"), "file_size": media.get("file_size")},
    )
    transcript = openai_client.audio.transcriptions.create(
        model=OPENAI_TRANSCRIBE_MODEL,
        file=file_obj,
        language="pt",
    )
    text = getattr(transcript, "text", None)
    if not text and isinstance(transcript, dict):
        text = transcript.get("text")
    text = (text or "").strip()
    if not text:
        raise RuntimeError("A transcrição do áudio veio vazia.")
    print("[V10] AUDIO_TRANSCRIBE_DONE", {"chars": len(text)})
    return text


def process_audio_message(sender, user_id, media_id, progress=None):
    progress = progress or start_request_progress(sender)
    try:
        transcript = transcribe_whatsapp_audio(media_id)
        process_dynamic_analysis(sender, user_id, transcript, progress=progress)
    except Exception as error:
        print("[V10.2] AUDIO_PROCESS_ERROR", repr(error))
        traceback.print_exc()
        progress.stop()
        send_whatsapp_message(sender, build_user_friendly_error(error, stage="transcrição do áudio"))
        release_analysis_lock(user_id)

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
    "lpv_rate_from_link_clicks",
    "lead_rate_from_lpv",
    "lead_rate_from_link_clicks",
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


def safe_rate(numerator, denominator, multiplier=100):
    numerator = safe_float(numerator)
    denominator = safe_float(denominator)
    if denominator <= 0:
        return 0
    return metric_round((numerator / denominator) * multiplier, 4)


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
        "ctr": safe_rate(clicks, impressions),
        "link_ctr": safe_rate(link_clicks, impressions),
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
        # Taxas do funil calculadas no backend para permitir diagnóstico por etapa.
        "lpv_rate_from_link_clicks": safe_rate(lpv, link_clicks),
        "lead_rate_from_lpv": safe_rate(leads, lpv),
        "lead_rate_from_link_clicks": safe_rate(leads, link_clicks),
    }


def share_percent(value, total):
    total = safe_float(total)
    if total <= 0:
        return 0
    return metric_round(safe_float(value) / total * 100, 2)


def build_entity_analysis(rows, summary, level):
    """
    Converte uma lista extensa de métricas em sinais de decisão.
    Não inventa causa: apenas mede concentração, eficiência e materialidade.
    """
    if level == "account":
        return {
            "funnel": {
                "cpm": summary.get("cpm"),
                "link_ctr": summary.get("link_ctr"),
                "link_cpc": summary.get("link_cpc"),
                "lpv_rate_from_link_clicks": summary.get("lpv_rate_from_link_clicks"),
                "lead_rate_from_lpv": summary.get("lead_rate_from_lpv"),
                "lead_rate_from_link_clicks": summary.get("lead_rate_from_link_clicks"),
                "cpl": summary.get("cpl"),
            }
        }

    total_spend = safe_float(summary.get("spend"))
    total_leads = safe_float(summary.get("leads"))
    benchmark_cpl = summary.get("cpl")
    enriched = []

    for row in rows:
        spend = safe_float(row.get("spend"))
        leads = safe_float(row.get("leads"))
        cpl = row.get("cpl")
        spend_share = share_percent(spend, total_spend)
        lead_share = share_percent(leads, total_leads)
        share_gap = metric_round(spend_share - lead_share, 2)

        cpl_vs_account_pct = None
        if cpl is not None and benchmark_cpl not in (None, 0):
            cpl_vs_account_pct = metric_round(
                (safe_float(cpl) / safe_float(benchmark_cpl) - 1) * 100,
                2,
            )

        flags = []
        if spend > 0 and leads <= 0:
            flags.append("spend_without_leads")
        if cpl_vs_account_pct is not None and cpl_vs_account_pct >= 25 and spend_share >= 5:
            flags.append("material_cpl_above_account")
        if cpl_vs_account_pct is not None and cpl_vs_account_pct <= -20 and lead_share >= 5:
            flags.append("material_efficiency_above_account")
        if share_gap >= 8:
            flags.append("spend_share_above_lead_share")
        if share_gap <= -8:
            flags.append("lead_share_above_spend_share")

        attention_score = (
            max(share_gap, 0)
            + (max(cpl_vs_account_pct or 0, 0) / 100.0) * spend_share
            + (spend_share if leads <= 0 and spend > 0 else 0)
        )
        strength_score = (
            max(-share_gap, 0)
            + (max(-(cpl_vs_account_pct or 0), 0) / 100.0) * lead_share
        )

        enriched.append({
            "id": entity_id_for_row(row, level),
            "name": entity_name_for_row(row, level),
            "spend": row.get("spend"),
            "leads": row.get("leads"),
            "cpl": row.get("cpl"),
            "link_ctr": row.get("link_ctr"),
            "link_cpc": row.get("link_cpc"),
            "spend_share_pct": spend_share,
            "lead_share_pct": lead_share,
            "spend_minus_lead_share_pp": share_gap,
            "cpl_vs_account_pct": cpl_vs_account_pct,
            "flags": flags,
            "attention_score": metric_round(attention_score, 4),
            "strength_score": metric_round(strength_score, 4),
        })

    by_spend = sorted(enriched, key=lambda x: safe_float(x.get("spend")), reverse=True)
    attention = sorted(enriched, key=lambda x: safe_float(x.get("attention_score")), reverse=True)
    strengths = sorted(enriched, key=lambda x: safe_float(x.get("strength_score")), reverse=True)

    return {
        "benchmark": {
            "account_cpl": benchmark_cpl,
            "total_spend": summary.get("spend"),
            "total_leads": summary.get("leads"),
        },
        "concentration": {
            "top_3_spend_share_pct": metric_round(
                sum(safe_float(item.get("spend_share_pct")) for item in by_spend[:3]),
                2,
            ),
        },
        "top_attention": attention[:5],
        "top_strengths": [item for item in strengths[:5] if safe_float(item.get("strength_score")) > 0],
        "spend_without_leads": [
            item for item in by_spend
            if "spend_without_leads" in item.get("flags", [])
        ][:5],
    }


def funnel_change_signals(summary_a, summary_b):
    checks = [
        ("cpm", "auction_cost"),
        ("link_ctr", "traffic_response"),
        ("link_cpc", "click_cost"),
        ("lpv_rate_from_link_clicks", "landing_arrival"),
        ("lead_rate_from_lpv", "post_landing_conversion"),
        ("lead_rate_from_link_clicks", "click_to_lead_conversion"),
        ("cpl", "lead_cost"),
    ]
    signals = []
    for metric, stage in checks:
        old = summary_a.get(metric)
        new = summary_b.get(metric)
        if old in (None, 0) or new is None:
            continue
        delta = percent_change(old, new)
        if delta is None:
            continue
        signals.append({
            "stage": stage,
            "metric": metric,
            "period_a": old,
            "period_b": new,
            "change_pct": delta,
        })
    return signals


def build_comparison_analysis(period_a_summary, period_b_summary, entities):
    total_spend_b = safe_float(period_b_summary.get("spend"))
    total_leads_b = safe_float(period_b_summary.get("leads"))
    benchmark_cpl_b = period_b_summary.get("cpl")
    drivers = []

    for item in entities:
        a = item.get("period_a", {})
        b = item.get("period_b", {})
        spend_b = safe_float(b.get("spend"))
        leads_b = safe_float(b.get("leads"))
        spend_share_b = share_percent(spend_b, total_spend_b)
        lead_share_b = share_percent(leads_b, total_leads_b)
        share_gap_b = metric_round(spend_share_b - lead_share_b, 2)
        cpl_change = item.get("deltas", {}).get("cpl", {}).get("percent")
        spend_change = item.get("deltas", {}).get("spend", {}).get("absolute")
        leads_change = item.get("deltas", {}).get("leads", {}).get("absolute")
        cpl_b = b.get("cpl")

        cpl_vs_account_b = None
        if cpl_b is not None and benchmark_cpl_b not in (None, 0):
            cpl_vs_account_b = metric_round(
                (safe_float(cpl_b) / safe_float(benchmark_cpl_b) - 1) * 100,
                2,
            )

        negative_score = (
            max(share_gap_b, 0)
            + (max(safe_float(cpl_change), 0) / 100.0) * spend_share_b
            + (spend_share_b if spend_b > 0 and leads_b <= 0 else 0)
        )
        positive_score = (
            max(-share_gap_b, 0)
            + (max(-safe_float(cpl_change), 0) / 100.0) * lead_share_b
        )

        drivers.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "current_spend": b.get("spend"),
            "current_leads": b.get("leads"),
            "current_cpl": b.get("cpl"),
            "spend_change_absolute": spend_change,
            "leads_change_absolute": leads_change,
            "cpl_change_pct": cpl_change,
            "current_spend_share_pct": spend_share_b,
            "current_lead_share_pct": lead_share_b,
            "spend_minus_lead_share_pp": share_gap_b,
            "current_cpl_vs_account_pct": cpl_vs_account_b,
            "negative_impact_score": metric_round(negative_score, 4),
            "positive_impact_score": metric_round(positive_score, 4),
        })

    negative = sorted(drivers, key=lambda x: safe_float(x.get("negative_impact_score")), reverse=True)
    positive = sorted(drivers, key=lambda x: safe_float(x.get("positive_impact_score")), reverse=True)

    return {
        "account_direction": {
            "spend_change_pct": percent_change(period_a_summary.get("spend"), period_b_summary.get("spend")),
            "leads_change_pct": percent_change(period_a_summary.get("leads"), period_b_summary.get("leads")),
            "cpl_change_pct": percent_change(period_a_summary.get("cpl"), period_b_summary.get("cpl")),
        },
        "funnel_change_signals": funnel_change_signals(period_a_summary, period_b_summary),
        "main_negative_drivers": [
            item for item in negative[:5]
            if safe_float(item.get("negative_impact_score")) > 0
        ],
        "main_positive_drivers": [
            item for item in positive[:5]
            if safe_float(item.get("positive_impact_score")) > 0
        ],
        "interpretation_note": (
            "Os scores são heurísticas de priorização por materialidade e eficiência, "
            "não prova causal. Use os dados brutos para sustentar a conclusão final."
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
        "analysis": build_comparison_analysis(
            report_a["summary"],
            report_b["summary"],
            entities,
        ),
        "note": (
            "Percentual nulo significa que o valor do período A era zero; "
            "não é possível calcular variação percentual convencional."
        ),
    }




# =========================================================
# OPENAI — MOTOR DINÂMICO / TOOL CALLING
# =========================================================







def create_dynamic_openai_response(context, user_question, previous_response_id=None):
    kwargs = {
        "model": OPENAI_ANALYSIS_MODEL,
        "reasoning": {"effort": OPENAI_ANALYSIS_REASONING_EFFORT},
        "max_output_tokens": 1200,
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
            "model": OPENAI_ANALYSIS_MODEL,
            "reasoning_effort": OPENAI_ANALYSIS_REASONING_EFFORT,
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

    max_tool_rounds = 8
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
            final_text = clean_ai_text_for_whatsapp(final_text)
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
            model=OPENAI_ANALYSIS_MODEL,
            reasoning={"effort": OPENAI_ANALYSIS_REASONING_EFFORT},
            max_output_tokens=1200,
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


def process_dynamic_analysis(sender, user_id, original_text, progress=None):
    print(
        "[DYNAMIC] JOB_START",
        {"sender": sender, "user_id": user_id, "question": original_text},
    )

    progress = progress or start_request_progress(sender)

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
            send_whatsapp_message(sender, "⛔ Este número ainda não está cadastrado no sistema.")
            return

        context = dict(context)
        context["_current_user_text"] = original_text

        if not context.get("can_read_ads"):
            send_whatsapp_message(sender, "⛔ Você não possui permissão para consultar Meta Ads.")
            return

        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY não configurada.")

        # Injeta no prompt o estado persistente do wizard, quando existir.
        if CAMPAIGN_WIZARD_ENABLED:
            try:
                context["_campaign_wizard"] = get_active_campaign_wizard(context)
            except Exception as wizard_error:
                print("[V10.2] WIZARD_CONTEXT_ERROR", repr(wizard_error))
                context["_campaign_wizard"] = None

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

        formatted_answer = clean_ai_text_for_whatsapp(answer)
        print(
            "[DYNAMIC] FORMAT_CHECK",
            {
                "contains_hash": "#" in formatted_answer,
                "contains_asterisk": "*" in formatted_answer,
                "chars": len(formatted_answer),
            },
        )
        progress.stop()
        send_whatsapp_long_message(sender, formatted_answer)
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

        progress.stop()
        send_whatsapp_message(sender, build_user_friendly_error(error, stage="análise no Meta Ads"))
    finally:
        progress.stop()
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

FORMATO PARA WHATSAPP:
- Use TEXTO PURO, sem Markdown.
- Não use #, *, _, `, >, |, tabelas ou cercas de código.
- Destaque blocos apenas com emoji + título curto em MAIÚSCULAS.
- Use • somente quando realmente houver uma lista.
- Mantenha a resposta limpa, compacta e fácil de escanear.

Quando fizer sentido, organize em:
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

    return clean_ai_text_for_whatsapp(response.output_text)


def build_basic_report(report):
    text = (
        "📊 META ADS — ÚLTIMOS 7 DIAS\n\n"
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
        message_type = message.get("type")
        sender = message.get("from")
        if not sender:
            return "EVENT_RECEIVED", 200

        context = get_user_context(sender)
        print(
            "[WEBHOOK] CONTEXT",
            {
                "build_id": BUILD_ID,
                "user_id": context.get("user_id") if context else None,
                "company_id": context.get("company_id") if context else None,
                "ad_account_id": context.get("ad_account_id") if context else None,
                "message_type": message_type,
            },
        )

        if not context:
            send_whatsapp_message(
                sender,
                "⛔ Este número ainda não está cadastrado no sistema.",
            )
            return "EVENT_RECEIVED", 200

        if not register_inbound_message(context, message.get("id"), message_type, sender):
            print("[V10] DUPLICATE_WEBHOOK_IGNORED", message.get("id"))
            return "EVENT_RECEIVED", 200

        touch_last_inbound(context)
        deliver_pending_notifications(context, sender)

        if message_type == "audio":
            media_id = (message.get("audio") or {}).get("id")
            if not media_id:
                send_whatsapp_message(sender, "❌ Recebi o áudio, mas não encontrei o arquivo para transcrever.")
                return "EVENT_RECEIVED", 200

            if not acquire_analysis_lock(context["user_id"]):
                send_whatsapp_message(sender, "⏳ Já estou processando outra solicitação sua. Tente novamente em instantes.")
                return "EVENT_RECEIVED", 200

            progress = start_request_progress(sender)
            try:
                audio_thread = threading.Thread(
                    target=process_audio_message,
                    args=(sender, context["user_id"], media_id, progress),
                    daemon=True,
                    name=f"audio-analysis-{context['user_id']}",
                )
                audio_thread.start()
            except Exception:
                progress.stop()
                release_analysis_lock(context["user_id"])
                raise
            return "EVENT_RECEIVED", 200

        if message_type in {"image", "video"}:
            media_payload = message.get(message_type) or {}
            media_id = media_payload.get("id")
            caption = (media_payload.get("caption") or "").strip()
            if not media_id:
                send_whatsapp_message(sender, "❌ Recebi o arquivo, mas não encontrei a mídia para processar.")
                return "EVENT_RECEIVED", 200
            if not acquire_analysis_lock(context["user_id"]):
                send_whatsapp_message(sender, "⏳ Já estou processando outra solicitação sua. Aguarde um instante.")
                return "EVENT_RECEIVED", 200
            progress = start_request_progress(sender)
            try:
                media_thread = threading.Thread(
                    target=process_wizard_media_message,
                    args=(sender, context["user_id"], media_id, message_type, caption, progress),
                    daemon=True,
                    name=f"wizard-media-{context['user_id']}",
                )
                media_thread.start()
            except Exception:
                progress.stop()
                release_analysis_lock(context["user_id"])
                raise
            return "EVENT_RECEIVED", 200

        if message_type != "text":
            send_whatsapp_message(sender, "ℹ️ Neste momento eu entendo texto, áudio, imagem e vídeo.")
            return "EVENT_RECEIVED", 200

        original_text = (message.get("text") or {}).get("body", "").strip()
        received_text = normalize_text(original_text)

        print("MENSAGEM:", original_text)
        print("REMETENTE:", sender)

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

        if received_text == "gasto legado 7 dias":
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

        if received_text == "analise legado 7 dias":
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
                "• Analise minha conta respeitando o setup real de cada campanha\n"
                "• Identifique na Meta o objetivo desta campanha e analise o resultado correto\n"
                "• Quais campanhas realmente merecem atenção considerando o próprio setup?\n"
                "• Compare os últimos 15 dias com os 15 anteriores\n"
                "• Agora aprofunde na campanha que você acabou de citar\n"
                "• Quero criar uma campanha nova\n"
                "• Durante a criação, envie imagem ou vídeo direto aqui no WhatsApp\n\n"
                "Comandos administrativos/técnicos:\n\n"
                "🔗 conectar meta\n"
                "📂 minhas contas meta\n"
                "👤 quem sou eu\n"
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

        progress = start_request_progress(sender)
        try:
            analysis_thread = threading.Thread(
                target=process_dynamic_analysis,
                args=(sender, context["user_id"], original_text, progress),
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
            progress.stop()
            release_analysis_lock(context["user_id"])
            raise

        return "EVENT_RECEIVED", 200

    except Exception as error:
        print("ERRO WEBHOOK:", repr(error))
        traceback.print_exc()

    return "EVENT_RECEIVED", 200



# =========================================================
# V9.1 — MOTOR META-DRIVEN / SEM TABELA FIXA DE OBJETIVOS
# =========================================================
#
# Princípio central:
# - a Meta é a fonte da verdade sobre o setup;
# - o backend busca objective, optimization_goal e demais campos reais;
# - o backend NÃO mantém uma tabela dizendo "objective X => KPI Y";
# - a OpenAI interpreta o setup retornado pela própria Meta;
# - quando a IA escolhe um action_type como resultado relevante, o backend
#   valida se esse action_type realmente existe nos Insights antes de usá-lo.
#
# Isso evita tratar "Resultados" como sinônimo de lead e evita engessar o
# produto quando a Meta cria/renomeia objetivos, performance goals ou eventos.

V91_BASE_SORT_METRICS = {
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
    "landing_page_views",
    "cost_per_landing_page_view",
    "selected_action_value",
    "selected_action_cost",
}


def v91_json_safe(value):
    """Converte objetos aninhados em estruturas JSON simples sem inventar semântica."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): v91_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [v91_json_safe(v) for v in value]
    return str(value)


def v91_action_catalog(items):
    """
    Converte AdsActionStats em mapa action_type -> value.
    Não seleciona qual action_type é "resultado"; apenas preserva o que a Meta retornou.
    """
    catalog = {}
    raw = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        clean = v91_json_safe(item)
        raw.append(clean)
        action_type = item.get("action_type")
        if not action_type:
            continue
        value = item.get("value")
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = value
        catalog[str(action_type)] = numeric
    return catalog, raw[:120]


def v91_sum_action_catalogs(rows, field_name):
    total = {}
    for row in rows:
        catalog = row.get(field_name) or {}
        for key, value in catalog.items():
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0.0) + float(value)
    return {key: metric_round(value, 4) for key, value in total.items()}


def v91_meta_get_edge_with_fallback(url, access_token, field_variants, limit=500, max_pages=20):
    """Tenta conjuntos de campos do mais rico ao mais conservador."""
    proof = appsecret_proof(access_token)
    last_error = None

    for fields in field_variants:
        params = {
            "access_token": access_token,
            "fields": ",".join(fields),
            "limit": limit,
        }
        if proof:
            params["appsecret_proof"] = proof
        try:
            rows = meta_get_paginated(url, params, max_pages=max_pages)
            print("[V9.1] META_SETUP_FIELDS_OK", fields)
            return rows, fields
        except RuntimeError as error:
            last_error = error
            print("[V9.1] META_SETUP_FIELDS_FALLBACK", fields, repr(error))

    if last_error:
        raise last_error
    return [], []


def v91_fetch_setup_metadata(ad_account_id, access_token):
    """Busca da própria Meta o setup real de campanhas e conjuntos."""
    campaigns_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ad_account_id}/campaigns"
    adsets_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ad_account_id}/adsets"

    campaign_variants = [
        [
            "id", "name", "objective", "status", "effective_status", "buying_type",
            "bid_strategy", "promoted_object", "smart_promotion_type",
            "special_ad_categories",
        ],
        ["id", "name", "objective", "status", "effective_status", "buying_type", "bid_strategy"],
        ["id", "name", "objective", "status", "effective_status", "buying_type"],
    ]
    adset_variants = [
        [
            "id", "name", "campaign_id", "optimization_goal", "optimization_sub_event",
            "billing_event", "bid_strategy", "promoted_object", "destination_type",
            "attribution_spec", "status", "effective_status",
        ],
        [
            "id", "name", "campaign_id", "optimization_goal", "billing_event",
            "bid_strategy", "promoted_object", "status", "effective_status",
        ],
        [
            "id", "name", "campaign_id", "optimization_goal", "billing_event",
            "status", "effective_status",
        ],
    ]

    campaigns, campaign_fields_used = v91_meta_get_edge_with_fallback(
        campaigns_url, access_token, campaign_variants
    )
    adsets, adset_fields_used = v91_meta_get_edge_with_fallback(
        adsets_url, access_token, adset_variants
    )

    campaigns_by_id = {
        str(row.get("id")): v91_json_safe(row)
        for row in campaigns
        if row.get("id")
    }
    adsets_by_id = {
        str(row.get("id")): v91_json_safe(row)
        for row in adsets
        if row.get("id")
    }
    adsets_by_campaign = {}
    for adset in adsets:
        campaign_id = str(adset.get("campaign_id") or "")
        if campaign_id:
            adsets_by_campaign.setdefault(campaign_id, []).append(v91_json_safe(adset))

    return {
        "campaigns": campaigns_by_id,
        "adsets": adsets_by_id,
        "adsets_by_campaign": adsets_by_campaign,
        "fields_used": {
            "campaign": campaign_fields_used,
            "adset": adset_fields_used,
        },
    }


def v91_compact_promoted_object(value):
    """Mantém o promoted_object vindo da Meta, removendo apenas valores vazios."""
    if not isinstance(value, dict):
        return v91_json_safe(value)
    return {
        str(k): v91_json_safe(v)
        for k, v in value.items()
        if v not in (None, "", [], {})
    }


def v91_adset_setup_view(adset):
    if not adset:
        return None
    return {
        "id": adset.get("id"),
        "name": adset.get("name"),
        "optimization_goal": adset.get("optimization_goal"),
        "optimization_sub_event": adset.get("optimization_sub_event"),
        "billing_event": adset.get("billing_event"),
        "destination_type": adset.get("destination_type"),
        "bid_strategy": adset.get("bid_strategy"),
        "promoted_object": v91_compact_promoted_object(adset.get("promoted_object")),
        "attribution_spec": v91_json_safe(adset.get("attribution_spec")),
        "status": adset.get("status"),
        "effective_status": adset.get("effective_status"),
    }


def v91_campaign_setup_view(campaign):
    if not campaign:
        return None
    return {
        "id": campaign.get("id"),
        "name": campaign.get("name"),
        "objective": campaign.get("objective"),
        "buying_type": campaign.get("buying_type"),
        "bid_strategy": campaign.get("bid_strategy"),
        "promoted_object": v91_compact_promoted_object(campaign.get("promoted_object")),
        "smart_promotion_type": campaign.get("smart_promotion_type"),
        "special_ad_categories": v91_json_safe(campaign.get("special_ad_categories")),
        "status": campaign.get("status"),
        "effective_status": campaign.get("effective_status"),
    }


def v91_setup_signature(campaign, adset=None):
    """
    Assinatura apenas para saber se dois setups são iguais.
    Ela NÃO traduz o setup para um KPI.
    """
    payload = {
        "campaign_objective": (campaign or {}).get("objective"),
        "campaign_promoted_object": v91_compact_promoted_object((campaign or {}).get("promoted_object")),
        "optimization_goal": (adset or {}).get("optimization_goal"),
        "optimization_sub_event": (adset or {}).get("optimization_sub_event"),
        "billing_event": (adset or {}).get("billing_event"),
        "destination_type": (adset or {}).get("destination_type"),
        "adset_promoted_object": v91_compact_promoted_object((adset or {}).get("promoted_object")),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def v91_setup_for_insight(raw, level, metadata):
    campaign_id = str(raw.get("campaign_id") or "")
    adset_id = str(raw.get("adset_id") or "")
    campaign = metadata.get("campaigns", {}).get(campaign_id, {})
    adset = metadata.get("adsets", {}).get(adset_id, {}) if adset_id else {}

    result = {
        "campaign": v91_campaign_setup_view(campaign),
        "adset": v91_adset_setup_view(adset) if adset else None,
        "setup_signature": v91_setup_signature(campaign, adset if adset else None),
        "source": "meta_marketing_api",
    }

    if level == "campaign" and campaign_id:
        adsets = metadata.get("adsets_by_campaign", {}).get(campaign_id, [])
        variants = []
        seen = set()
        for child in adsets:
            signature = v91_setup_signature(campaign, child)
            if signature in seen:
                continue
            seen.add(signature)
            variants.append({
                "setup_signature": signature,
                "adset_setup": v91_adset_setup_view(child),
            })
        result["adset_setup_variants"] = variants[:30]
        result["setup_variant_count"] = len(variants)
        result["requires_adset_drilldown"] = len(variants) > 1

    return result


def v91_base_metrics(raw):
    """Métricas universais. Nenhuma delas é declarada automaticamente como KPI principal."""
    spend = safe_float(raw.get("spend"))
    impressions = safe_int(raw.get("impressions"))
    reach = safe_int(raw.get("reach"))
    clicks = safe_int(raw.get("clicks"))
    link_clicks = safe_int(raw.get("inline_link_clicks"))
    lpv = get_landing_page_views(raw.get("actions", []))

    return {
        "spend": metric_round(spend, 2),
        "impressions": impressions,
        "reach": reach,
        "frequency": metric_round(impressions / reach if reach else 0, 4),
        "clicks": clicks,
        "link_clicks": link_clicks,
        "ctr": safe_rate(clicks, impressions),
        "link_ctr": safe_rate(link_clicks, impressions),
        "cpc": metric_round(spend / clicks, 4) if clicks else (None if spend > 0 else 0),
        "link_cpc": metric_round(spend / link_clicks, 4) if link_clicks else (None if spend > 0 else 0),
        "cpm": metric_round(spend / impressions * 1000, 4) if impressions else (None if spend > 0 else 0),
        "landing_page_views": metric_round(lpv, 2),
        "cost_per_landing_page_view": metric_round(spend / lpv, 4) if lpv else (None if spend > 0 else 0),
    }


def v91_insight_field_variants(level):
    entity_fields = entity_fields_for_level(level)
    common = [
        "account_id", "account_name", "date_start", "date_stop",
        "spend", "impressions", "reach", "clicks", "inline_link_clicks",
    ]
    for field in entity_fields:
        if field not in common:
            common.append(field)

    # Estes campos são da própria camada de Insights da Meta. A v9.1 tenta primeiro
    # obter inclusive os resultados contextualizados pela plataforma; se uma conta/
    # versão não expuser algum deles, há fallback progressivo.
    rich = common + [
        "objective", "optimization_goal",
        "objective_results", "objective_result_rate",
        "cost_per_objective_result", "cost_per_result",
        "actions", "cost_per_action_type", "action_values",
        "outbound_clicks", "cost_per_outbound_click",
        "purchase_roas", "website_purchase_roas", "mobile_app_purchase_roas",
        "video_thruplay_watched_actions", "video_2_sec_continuous_watched_actions",
    ]
    standard = common + [
        "objective", "optimization_goal",
        "objective_results", "cost_per_objective_result", "cost_per_result",
        "actions", "cost_per_action_type", "action_values",
    ]
    actions_only = common + ["objective", "optimization_goal", "actions", "cost_per_action_type", "action_values"]
    minimal = common + ["actions"]
    return [rich, standard, actions_only, minimal]


def v91_fetch_raw_insights(ad_account_id, access_token, since, until, level):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ad_account_id}/insights"
    proof = appsecret_proof(access_token)
    last_error = None

    for fields in v91_insight_field_variants(level):
        params = {
            "access_token": access_token,
            "fields": ",".join(fields),
            "time_range": json.dumps({"since": since, "until": until}),
            "level": level,
            "limit": 500,
        }
        if proof:
            params["appsecret_proof"] = proof
        try:
            rows = meta_get_paginated(url, params, max_pages=20)
            print("[V9.1] INSIGHT_FIELDS_OK", fields)
            return rows, fields
        except RuntimeError as error:
            last_error = error
            print("[V9.1] INSIGHT_FIELDS_FALLBACK", fields, repr(error))

    if last_error:
        raise last_error
    return [], []


def v91_extract_list_scalar(value):
    """Preserva listas/objetos da Meta; usado só para deixar o payload serializável."""
    return v91_json_safe(value)


def v91_normalize_insight_row(raw, level, metadata, selected_action_type=None):
    row = {
        "date_start": raw.get("date_start"),
        "date_stop": raw.get("date_stop"),
    }
    for field in entity_fields_for_level(level):
        row[field] = raw.get(field)
    row.update(v91_base_metrics(raw))

    actions, actions_raw = v91_action_catalog(raw.get("actions"))
    action_costs, action_costs_raw = v91_action_catalog(raw.get("cost_per_action_type"))
    action_values, action_values_raw = v91_action_catalog(raw.get("action_values"))

    row["meta_setup"] = v91_setup_for_insight(raw, level, metadata) if level != "account" else {
        "source": "meta_marketing_api",
        "note": "Conta não possui um único objective/optimization_goal. Use setup_portfolio por conjunto/campanha.",
    }
    row["meta_reported_result"] = {
        "insights_objective": raw.get("objective"),
        "insights_optimization_goal": raw.get("optimization_goal"),
        "objective_results": v91_extract_list_scalar(raw.get("objective_results")),
        "objective_result_rate": v91_extract_list_scalar(raw.get("objective_result_rate")),
        "cost_per_objective_result": v91_extract_list_scalar(raw.get("cost_per_objective_result")),
        "cost_per_result": v91_extract_list_scalar(raw.get("cost_per_result")),
        "note": (
            "Campos retornados diretamente pelos Insights da Meta. Se estiverem preenchidos, "
            "eles têm precedência sobre qualquer inferência manual de 'Resultados'."
        ),
    }
    row["available_actions"] = actions
    row["available_cost_per_action"] = action_costs
    row["available_action_values"] = action_values
    row["raw_action_stats"] = {
        "actions": actions_raw,
        "cost_per_action_type": action_costs_raw,
        "action_values": action_values_raw,
    }

    # Mantém outros campos ricos sem transformá-los em KPI automaticamente.
    row["additional_meta_metrics"] = {
        "outbound_clicks": v91_json_safe(raw.get("outbound_clicks")),
        "cost_per_outbound_click": v91_json_safe(raw.get("cost_per_outbound_click")),
        "purchase_roas": v91_json_safe(raw.get("purchase_roas")),
        "website_purchase_roas": v91_json_safe(raw.get("website_purchase_roas")),
        "mobile_app_purchase_roas": v91_json_safe(raw.get("mobile_app_purchase_roas")),
        "video_thruplay_watched_actions": v91_json_safe(raw.get("video_thruplay_watched_actions")),
        "video_2_sec_continuous_watched_actions": v91_json_safe(raw.get("video_2_sec_continuous_watched_actions")),
    }

    if selected_action_type:
        value = actions.get(selected_action_type)
        meta_cost = action_costs.get(selected_action_type)
        derived_cost = None
        if isinstance(value, (int, float)) and float(value) > 0:
            derived_cost = metric_round(safe_float(row.get("spend")) / float(value), 4)
        row["selected_action_type"] = selected_action_type
        row["selected_action_found"] = selected_action_type in actions
        row["selected_action_value"] = value
        row["selected_action_cost"] = meta_cost if meta_cost is not None else derived_cost
        row["selected_action_cost_source"] = (
            "meta_cost_per_action_type"
            if meta_cost is not None
            else ("derived_spend_divided_by_action" if derived_cost is not None else None)
        )

    return row


def v91_aggregate_base(rows):
    spend = sum(safe_float(row.get("spend")) for row in rows)
    impressions = sum(safe_int(row.get("impressions")) for row in rows)
    clicks = sum(safe_int(row.get("clicks")) for row in rows)
    link_clicks = sum(safe_int(row.get("link_clicks")) for row in rows)
    lpv = sum(safe_float(row.get("landing_page_views")) for row in rows)

    # Reach somado entre entidades pode duplicar pessoas. Por isso fica explicitamente rotulado.
    reach_sum = sum(safe_int(row.get("reach")) for row in rows)
    return {
        "spend": metric_round(spend, 2),
        "impressions": impressions,
        "reach_sum_not_deduplicated": reach_sum,
        "clicks": clicks,
        "link_clicks": link_clicks,
        "ctr": safe_rate(clicks, impressions),
        "link_ctr": safe_rate(link_clicks, impressions),
        "cpc": metric_round(spend / clicks, 4) if clicks else (None if spend > 0 else 0),
        "link_cpc": metric_round(spend / link_clicks, 4) if link_clicks else (None if spend > 0 else 0),
        "cpm": metric_round(spend / impressions * 1000, 4) if impressions else (None if spend > 0 else 0),
        "landing_page_views": metric_round(lpv, 2),
        "cost_per_landing_page_view": metric_round(spend / lpv, 4) if lpv else (None if spend > 0 else 0),
    }


def v91_build_setup_portfolio(rows):
    """Agrupa apenas setups iguais; não decide qual KPI cada grupo deve usar."""
    groups = {}
    total_spend = sum(safe_float(row.get("spend")) for row in rows)

    for row in rows:
        setup = row.get("meta_setup") or {}
        signature = setup.get("setup_signature") or "account_or_unknown"
        groups.setdefault(signature, []).append(row)

    output = []
    for signature, group_rows in groups.items():
        first = group_rows[0]
        setup = first.get("meta_setup") or {}
        spend = sum(safe_float(row.get("spend")) for row in group_rows)
        campaigns = {
            row.get("campaign_id")
            for row in group_rows
            if row.get("campaign_id")
        }
        adsets = {
            row.get("adset_id")
            for row in group_rows
            if row.get("adset_id")
        }
        output.append({
            "setup_signature": signature,
            "campaign_setup": setup.get("campaign"),
            "adset_setup": setup.get("adset"),
            "spend": metric_round(spend, 2),
            "spend_share_pct": share_percent(spend, total_spend),
            "campaign_count": len(campaigns),
            "adset_count": len(adsets),
            "available_action_types": sorted({
                action_type
                for row in group_rows
                for action_type in (row.get("available_actions") or {}).keys()
            })[:120],
            "meta_result_examples": [
                row.get("meta_reported_result")
                for row in group_rows[:3]
                if row.get("meta_reported_result")
            ],
        })

    output.sort(key=lambda item: safe_float(item.get("spend")), reverse=True)
    return {
        "group_count": len(output),
        "total_spend": metric_round(total_spend, 2),
        "groups": output,
        "rule": (
            "Grupos refletem configurações retornadas pela Meta. O backend não atribui "
            "um KPI fixo a nenhum grupo; a interpretação é feita pela IA a partir do setup real."
        ),
    }


def v91_sort_rows(rows, sort_by, sort_order):
    reverse = (sort_order or "desc").lower() == "desc"

    def key(row):
        value = row.get(sort_by)
        if value is None:
            return float("-inf") if reverse else float("inf")
        return safe_float(value)

    rows.sort(key=key, reverse=reverse)
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
    action_type=None,
):
    print("[V9.1] QUERY_META_DRIVEN_START", {
        "since": since,
        "until": until,
        "level": level,
        "search": search,
        "sort_by": sort_by,
        "action_type": action_type,
    })

    if level not in ALLOWED_LEVELS:
        raise ValueError(f"Nível inválido: {level}")
    if sort_by not in V91_BASE_SORT_METRICS:
        raise ValueError(f"Métrica de ordenação inválida na v9.1: {sort_by}")
    if sort_by in {"selected_action_value", "selected_action_cost"} and not action_type:
        raise ValueError("Para ordenar por resultado dinâmico, informe action_type após inspecionar o setup da Meta.")
    if (sort_order or "desc").lower() not in {"asc", "desc"}:
        raise ValueError("sort_order deve ser asc ou desc.")

    limit = max(1, min(int(limit or 25), 100))
    min_spend = max(0.0, safe_float(min_spend))
    today = get_today_for_context(context)
    validate_date_range(since, until, today=today)

    ad_account_id, access_token, credential_error = get_meta_credentials(context)
    if credential_error:
        raise RuntimeError(credential_error)

    metadata = v91_fetch_setup_metadata(ad_account_id, access_token)
    raw_rows, insight_fields_used = v91_fetch_raw_insights(
        ad_account_id, access_token, since, until, level
    )
    rows = [
        v91_normalize_insight_row(row, level, metadata, selected_action_type=action_type)
        for row in raw_rows
    ]

    if search:
        needle = normalize_text(str(search))
        rows = [
            row for row in rows
            if needle in normalize_text(" ".join(
                str(row.get(field) or "") for field in entity_fields_for_level(level)
            ))
        ]

    rows = [row for row in rows if safe_float(row.get("spend")) >= min_spend]
    if not include_zero_spend:
        rows = [row for row in rows if safe_float(row.get("spend")) > 0]

    # Para conta, buscamos adsets em paralelo para mostrar a composição real do setup.
    # Isso evita fingir que a conta inteira possui um único objetivo.
    setup_entities = []
    if level == "account":
        adset_raw, _ = v91_fetch_raw_insights(
            ad_account_id, access_token, since, until, "adset"
        )
        setup_entities = [
            v91_normalize_insight_row(
                row, "adset", metadata, selected_action_type=action_type
            )
            for row in adset_raw
        ]
        setup_entities = [
            row for row in setup_entities
            if safe_float(row.get("spend")) >= min_spend
            and (include_zero_spend or safe_float(row.get("spend")) > 0)
        ]
        setup_entities.sort(key=lambda item: safe_float(item.get("spend")), reverse=True)

    v91_sort_rows(rows, sort_by, sort_order)
    returned_rows = rows[:limit]

    if level == "account" and returned_rows:
        # O row de conta vem deduplicado pela Meta e é a melhor fonte para métricas globais.
        summary = {
            key: returned_rows[0].get(key)
            for key in [
                "spend", "impressions", "reach", "frequency", "clicks", "link_clicks",
                "ctr", "link_ctr", "cpc", "link_cpc", "cpm", "landing_page_views",
                "cost_per_landing_page_view",
            ]
        }
        summary["meta_reported_result"] = returned_rows[0].get("meta_reported_result")
        summary["available_actions"] = returned_rows[0].get("available_actions")
        summary["available_cost_per_action"] = returned_rows[0].get("available_cost_per_action")
        if action_type:
            summary["selected_action_type"] = action_type
            summary["selected_action_value"] = returned_rows[0].get("selected_action_value")
            summary["selected_action_cost"] = returned_rows[0].get("selected_action_cost")
    else:
        summary = v91_aggregate_base(rows)
        summary["available_actions_aggregated"] = v91_sum_action_catalogs(rows, "available_actions")
        if action_type:
            selected_value = sum(
                safe_float(row.get("selected_action_value"))
                for row in rows
                if row.get("selected_action_found")
            )
            selected_cost = metric_round(
                safe_float(summary.get("spend")) / selected_value, 4
            ) if selected_value > 0 else None
            summary["selected_action_type"] = action_type
            summary["selected_action_value"] = metric_round(selected_value, 4)
            summary["selected_action_cost_derived_for_selection"] = selected_cost

    portfolio_source = setup_entities if level == "account" else rows
    setup_portfolio = v91_build_setup_portfolio(portfolio_source)

    result = {
        "engine": ANALYSIS_ENGINE,
        "period": {"since": since, "until": until},
        "level": level,
        "account_id": ad_account_id,
        "matched_rows": len(rows),
        "returned_rows": len(returned_rows),
        "summary": summary,
        "rows": returned_rows,
        "setup_portfolio": setup_portfolio,
        "setup_entities": setup_entities[:30] if level == "account" else None,
        "meta_fields_used": {
            "campaign_setup": metadata.get("fields_used", {}).get("campaign"),
            "adset_setup": metadata.get("fields_used", {}).get("adset"),
            "insights": insight_fields_used,
        },
        "interpretation_contract": {
            "source_of_truth": "Meta Marketing API",
            "do_not_assume_results_equals_leads": True,
            "do_not_infer_kpi_from_campaign_name": True,
            "first_choice": (
                "Leia objective_results/cost_per_objective_result/cost_per_result quando a Meta os retornar."
            ),
            "fallback": (
                "Se os campos contextualizados estiverem vazios, interprete objective + optimization_goal + "
                "optimization_sub_event + promoted_object + destination_type e confronte com available_actions."
            ),
            "validation": (
                "Ao escolher um action_type como resultado principal, use validar_resultado_meta antes de "
                "tratar o evento como fato na resposta final."
            ),
        },
    }
    print("[V9.1] QUERY_META_DRIVEN_DONE", {
        "rows": len(returned_rows),
        "setup_groups": setup_portfolio.get("group_count"),
        "action_type": action_type,
    })
    return result


def validate_meta_result(
    context,
    since,
    until,
    level,
    entity_id,
    action_type,
):
    """
    Valida uma interpretação feita pela IA sem possuir uma tabela de objetivos.
    A IA escolhe o action_type; o backend confirma se a Meta realmente o retornou.
    """
    if level not in {"campaign", "adset", "ad"}:
        raise ValueError("A validação de resultado exige level campaign, adset ou ad.")
    if not entity_id or not action_type:
        raise ValueError("entity_id e action_type são obrigatórios.")

    report = query_meta_insights(
        context,
        since=since,
        until=until,
        level=level,
        search=str(entity_id),
        limit=100,
        sort_by="spend",
        sort_order="desc",
        min_spend=0,
        include_zero_spend=True,
        action_type=str(action_type),
    )

    exact = None
    for row in report.get("rows", []):
        if str(entity_id_for_row(row, level)) == str(entity_id):
            exact = row
            break

    if exact is None:
        return {
            "validated": False,
            "reason": "entity_not_found",
            "entity_id": entity_id,
            "action_type": action_type,
        }

    found = bool(exact.get("selected_action_found"))
    return {
        "validated": found,
        "entity_id": entity_id,
        "entity_name": entity_name_for_row(exact, level),
        "level": level,
        "action_type": action_type,
        "value": exact.get("selected_action_value"),
        "cost_per_action": exact.get("selected_action_cost"),
        "cost_source": exact.get("selected_action_cost_source"),
        "spend": exact.get("spend"),
        "meta_setup": exact.get("meta_setup"),
        "meta_reported_result": exact.get("meta_reported_result"),
        "available_action_types": sorted((exact.get("available_actions") or {}).keys())[:150],
        "note": (
            "validated=true significa apenas que esse action_type existe nos Insights retornados pela Meta. "
            "A pertinência estratégica continua sendo uma interpretação do setup real."
        ),
    }


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
    action_type=None,
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

    report_a = query_meta_insights(
        context,
        periods["a"]["since"], periods["a"]["until"],
        level=level, search=search, limit=max(limit, 50), sort_by=sort_by,
        sort_order="desc", include_zero_spend=True, action_type=action_type,
    )
    report_b = query_meta_insights(
        context,
        periods["b"]["since"], periods["b"]["until"],
        level=level, search=search, limit=max(limit, 50), sort_by=sort_by,
        sort_order="desc", include_zero_spend=True, action_type=action_type,
    )

    base_metrics = [
        "spend", "impressions", "reach", "frequency", "clicks", "link_clicks",
        "ctr", "link_ctr", "cpc", "link_cpc", "cpm", "landing_page_views",
        "cost_per_landing_page_view",
    ]
    if action_type:
        base_metrics += ["selected_action_value", "selected_action_cost"]

    summary_deltas = {
        metric: metric_delta(
            report_a.get("summary", {}).get(metric),
            report_b.get("summary", {}).get(metric),
        )
        for metric in base_metrics
    }

    entities = []
    if level != "account":
        rows_a = {
            str(entity_id_for_row(row, level)): row
            for row in report_a.get("rows", [])
            if entity_id_for_row(row, level)
        }
        rows_b = {
            str(entity_id_for_row(row, level)): row
            for row in report_b.get("rows", [])
            if entity_id_for_row(row, level)
        }
        for entity_id in set(rows_a) | set(rows_b):
            a = rows_a.get(entity_id, {})
            b = rows_b.get(entity_id, {})
            base = b or a
            sig_a = (a.get("meta_setup") or {}).get("setup_signature")
            sig_b = (b.get("meta_setup") or {}).get("setup_signature")
            setup_unchanged = not (sig_a and sig_b and sig_a != sig_b)
            entities.append({
                "id": entity_id,
                "name": entity_name_for_row(base, level),
                "setup_unchanged": setup_unchanged,
                "period_a_setup": a.get("meta_setup"),
                "period_b_setup": b.get("meta_setup"),
                "period_a_meta_result": a.get("meta_reported_result"),
                "period_b_meta_result": b.get("meta_reported_result"),
                "period_a": {metric: a.get(metric) for metric in base_metrics},
                "period_b": {metric: b.get(metric) for metric in base_metrics},
                "deltas": {
                    metric: metric_delta(a.get(metric), b.get(metric))
                    for metric in base_metrics
                },
            })
        entities.sort(
            key=lambda item: safe_float(item.get("period_b", {}).get(sort_by)),
            reverse=True,
        )
        entities = entities[:max(1, min(int(limit or 25), 50))]

    return {
        "engine": ANALYSIS_ENGINE,
        "mode": mode,
        "level": level,
        "selected_action_type": action_type,
        "period_a": {
            **periods["a"],
            "summary": report_a.get("summary"),
            "setup_portfolio": report_a.get("setup_portfolio"),
        },
        "period_b": {
            **periods["b"],
            "summary": report_b.get("summary"),
            "setup_portfolio": report_b.get("setup_portfolio"),
        },
        "summary_deltas_a_to_b": summary_deltas,
        "entities": entities,
        "guardrail": (
            "Nenhum KPI principal foi escolhido pelo código. Compare a métrica compatível com o setup "
            "retornado pela Meta; se usar action_type, valide-o. Se setup_unchanged=false, não atribua "
            "a mudança apenas à performance, pois a configuração também mudou."
        ),
    }


def list_meta_structure(context, level="campaign", search=None, limit=50):
    """Lista a configuração REAL da Meta, sem mapear objetivo para KPI no código."""
    if level not in {"campaign", "adset", "ad"}:
        raise ValueError("level deve ser campaign, adset ou ad.")

    ad_account_id, access_token, credential_error = get_meta_credentials(context)
    if credential_error:
        raise RuntimeError(credential_error)
    metadata = v91_fetch_setup_metadata(ad_account_id, access_token)

    if level == "campaign":
        rows = []
        for campaign_id, campaign in metadata.get("campaigns", {}).items():
            children = metadata.get("adsets_by_campaign", {}).get(campaign_id, [])
            variants = []
            seen = set()
            for adset in children:
                signature = v91_setup_signature(campaign, adset)
                if signature in seen:
                    continue
                seen.add(signature)
                variants.append({
                    "setup_signature": signature,
                    "adset_setup": v91_adset_setup_view(adset),
                })
            rows.append({
                "id": campaign_id,
                "name": campaign.get("name"),
                "campaign_setup": v91_campaign_setup_view(campaign),
                "adset_setup_variants": variants,
                "setup_variant_count": len(variants),
                "requires_adset_drilldown": len(variants) > 1,
            })
    elif level == "adset":
        rows = []
        for adset_id, adset in metadata.get("adsets", {}).items():
            campaign = metadata.get("campaigns", {}).get(str(adset.get("campaign_id") or ""), {})
            rows.append({
                "id": adset_id,
                "name": adset.get("name"),
                "campaign_setup": v91_campaign_setup_view(campaign),
                "adset_setup": v91_adset_setup_view(adset),
                "setup_signature": v91_setup_signature(campaign, adset),
            })
    else:
        # Para anúncios buscamos somente identidade/hierarquia e anexamos o setup do conjunto/campanha.
        ads_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ad_account_id}/ads"
        ads, _ = v91_meta_get_edge_with_fallback(
            ads_url,
            access_token,
            [
                ["id", "name", "campaign_id", "adset_id", "status", "effective_status", "conversion_specs", "tracking_specs"],
                ["id", "name", "campaign_id", "adset_id", "status", "effective_status"],
                ["id", "name", "campaign_id", "adset_id"],
            ],
            limit=500,
            max_pages=20,
        )
        rows = []
        for ad in ads:
            campaign = metadata.get("campaigns", {}).get(str(ad.get("campaign_id") or ""), {})
            adset = metadata.get("adsets", {}).get(str(ad.get("adset_id") or ""), {})
            rows.append({
                "id": ad.get("id"),
                "name": ad.get("name"),
                "status": ad.get("status"),
                "effective_status": ad.get("effective_status"),
                "conversion_specs": v91_json_safe(ad.get("conversion_specs")),
                "tracking_specs": v91_json_safe(ad.get("tracking_specs")),
                "campaign_setup": v91_campaign_setup_view(campaign),
                "adset_setup": v91_adset_setup_view(adset),
                "setup_signature": v91_setup_signature(campaign, adset),
            })

    if search:
        needle = normalize_text(str(search))
        rows = [
            row for row in rows
            if needle in normalize_text(f"{row.get('id', '')} {row.get('name', '')}")
        ]

    limit = max(1, min(int(limit or 50), 100))
    return {
        "engine": ANALYSIS_ENGINE,
        "level": level,
        "matched_rows": len(rows),
        "rows": rows[:limit],
        "rule": "Configuração exibida como a Meta retornou; nenhum KPI foi pré-definido pelo backend.",
    }



# =========================================================
# V10 — ACTION AI / ESCRITA CONTROLADA NA META
# =========================================================

ACTION_TARGET_TYPES = {"campaign", "adset", "ad"}
ZERO_DECIMAL_CURRENCIES = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}

ALLOWED_ADSET_UPDATE_FIELDS = {
    "name", "targeting", "optimization_goal", "optimization_sub_event",
    "billing_event", "promoted_object", "destination_type", "bid_amount",
    "bid_strategy", "attribution_spec", "start_time", "end_time",
    "daily_budget", "lifetime_budget",
}


def normalize_account_id(value):
    value = str(value or "").strip()
    return value[4:] if value.startswith("act_") else value


def graph_payload(data):
    output = {}
    for key, value in (data or {}).items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            output[key] = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            output[key] = "true" if value else "false"
        else:
            output[key] = value
    return output


def meta_graph_get(context, path, fields=None, params=None, timeout=30):
    ad_account_id, access_token, error = get_meta_credentials(context)
    if error:
        raise RuntimeError(error)
    query = dict(params or {})
    if fields:
        query["fields"] = fields
    proof = appsecret_proof(access_token)
    if proof:
        query["appsecret_proof"] = proof
    response = http_session.get(
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/{path}",
        params=query,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Meta GET {path}: {response.status_code} {response.text}")
    return response.json()


def meta_graph_post(context, path, data=None, timeout=45):
    ad_account_id, access_token, error = get_meta_credentials(context)
    if error:
        raise RuntimeError(error)
    payload = graph_payload(data)
    proof = appsecret_proof(access_token)
    if proof:
        payload["appsecret_proof"] = proof
    response = http_session.post(
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/{path}",
        data=payload,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Meta POST {path}: {response.status_code} {response.text}")
    return response.json()


def ensure_target_belongs_to_account(context, target_type, target_id):
    if target_type not in ACTION_TARGET_TYPES:
        raise ValueError("Tipo de alvo inválido.")
    if not target_id:
        raise ValueError("ID do alvo ausente.")
    data = meta_graph_get(context, str(target_id), fields="id,account_id")
    selected = normalize_account_id(context.get("ad_account_id"))
    target_account = normalize_account_id(data.get("account_id"))
    if target_account and selected and target_account != selected:
        raise PermissionError("Esse objeto não pertence à conta Meta selecionada.")
    return data


def currency_to_minor(context, amount_major):
    amount = float(amount_major)
    if amount <= 0:
        raise ValueError("O orçamento precisa ser maior que zero.")
    currency = str(context.get("meta_currency") or "BRL").upper()
    multiplier = 1 if currency in ZERO_DECIMAL_CURRENCIES else 100
    return int(round(amount * multiplier))


def is_explicit_confirmation_text(text):
    n = normalize_text(text or "")
    negative = ["nao", "não", "cancela", "cancelar", "deixa", "nao faca", "não faça"]
    if any(term in n for term in negative):
        return False
    confirmations = [
        "sim", "confirmo", "confirmar", "pode fazer", "pode executar", "faca", "faça",
        "faz isso", "execute", "pode ativar", "ativa", "ative", "manda bala", "pode",
    ]
    return any(term in n for term in confirmations)


def is_explicit_creation_request(text):
    n = normalize_text(text or "")
    negatives = ["nao crie", "não crie", "nao criar", "não criar"]
    if any(x in n for x in negatives):
        return False
    return any(x in n for x in ["crie", "criar", "cria", "monte", "montar", "nova campanha", "quero uma campanha"])


def action_summary(action_type, target_type, target_id, spec, summary=None):
    if summary:
        return summary.strip()
    if action_type == "set_status":
        return f"Alterar {target_type} {target_id} para {spec.get('status')}."
    if action_type == "set_budget":
        return f"Alterar orçamento de {target_type} {target_id} para {spec.get('amount_major')} {spec.get('budget_kind', 'daily_budget')}."
    if action_type == "duplicate":
        return f"Duplicar {target_type} {target_id} mantendo a cópia pausada."
    if action_type == "create_pixel":
        return f"Criar Pixel '{spec.get('name')}'."
    if action_type == "update_adset":
        return f"Atualizar configurações do conjunto {target_id}."
    if action_type == "activate_structure":
        return "Ativar a estrutura de campanha que foi criada pausada."
    return f"Executar {action_type}."


def validate_action_request(context, action_type, target_type=None, target_id=None, spec=None):
    spec = dict(spec or {})
    if not ACTION_AI_ENABLED:
        raise RuntimeError("Action AI está desativitado no ambiente.")
    if not context.get("can_create_campaigns"):
        raise PermissionError("Este usuário não possui permissão de escrita na Meta.")

    if action_type in {"set_status", "set_budget", "duplicate", "update_adset"}:
        ensure_target_belongs_to_account(context, target_type, target_id)

    if action_type == "set_status":
        status = str(spec.get("status") or "").upper()
        if status not in {"ACTIVE", "PAUSED"}:
            raise ValueError("Status permitido: ACTIVE ou PAUSED.")
        spec["status"] = status

    elif action_type == "set_budget":
        if target_type not in {"campaign", "adset"}:
            raise ValueError("Orçamento pode ser alterado em campaign ou adset.")
        kind = spec.get("budget_kind", "daily_budget")
        if kind not in {"daily_budget", "lifetime_budget"}:
            raise ValueError("budget_kind inválido.")
        spec["amount_major"] = float(spec.get("amount_major"))
        spec["budget_kind"] = kind

    elif action_type == "duplicate":
        if target_type not in ACTION_TARGET_TYPES:
            raise ValueError("Só é possível duplicar campaign/adset/ad.")
        spec["deep_copy"] = bool(spec.get("deep_copy", True))

    elif action_type == "create_pixel":
        name = str(spec.get("name") or "").strip()
        if not name:
            raise ValueError("Informe o nome do Pixel.")
        spec["name"] = name

    elif action_type == "update_adset":
        if target_type != "adset":
            raise ValueError("update_adset exige target_type=adset.")
        updates = dict(spec.get("updates") or {})
        forbidden = set(updates) - ALLOWED_ADSET_UPDATE_FIELDS
        if forbidden:
            raise ValueError("Campos de conjunto não permitidos: " + ", ".join(sorted(forbidden)))
        if not updates:
            raise ValueError("Nenhuma alteração de conjunto foi informada.")
        # Status e orçamento têm fluxos próprios para deixar o risco evidente.
        updates.pop("status", None)
        spec["updates"] = updates

    elif action_type == "activate_structure":
        ids = dict(spec.get("ids") or {})
        if not ids.get("campaign_id"):
            raise ValueError("Estrutura sem campaign_id.")
        ensure_target_belongs_to_account(context, "campaign", ids["campaign_id"])
        spec["ids"] = ids

    else:
        raise ValueError(f"Ação não suportada: {action_type}")

    return spec


def create_pending_action(context, action_type, target_type=None, target_id=None, spec=None, summary=None):
    spec = validate_action_request(context, action_type, target_type, target_id, spec)
    action_key = uuid.uuid4().hex
    summary = action_summary(action_type, target_type, target_id, spec, summary)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pending_actions (
                    action_key, company_id, user_id, ad_account_id,
                    action_type, target_type, target_id, spec, summary,
                    status, requires_confirmation
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, 'PENDING_CONFIRMATION', TRUE)
                RETURNING id, action_key, created_at;
                """,
                (
                    action_key,
                    context["company_id"], context["user_id"], context["ad_account_id"],
                    action_type, target_type, target_id,
                    json.dumps(spec, ensure_ascii=False), summary,
                ),
            )
            row = cursor.fetchone()
    log_activity(context, "action_proposed", {"action_id": row["id"], "action_type": action_type, "target_type": target_type, "target_id": target_id})
    return {
        "action_id": row["id"],
        "action_key": row["action_key"],
        "status": "PENDING_CONFIRMATION",
        "summary": summary,
        "confirmation_required": True,
        "confirmation_text": "A ação ainda NÃO foi executada. Peça uma confirmação simples, por exemplo: 'Posso executar?'.",
    }


def get_pending_action(context, action_id=None):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if action_id:
                cursor.execute(
                    """
                    SELECT * FROM pending_actions
                    WHERE id = %s AND company_id = %s AND user_id = %s;
                    """,
                    (int(action_id), context["company_id"], context["user_id"]),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM pending_actions
                    WHERE company_id = %s AND user_id = %s AND status = 'PENDING_CONFIRMATION'
                    ORDER BY created_at DESC LIMIT 1;
                    """,
                    (context["company_id"], context["user_id"]),
                )
            return cursor.fetchone()


def execute_action_spec(context, action):
    action_type = action["action_type"]
    target_type = action.get("target_type")
    target_id = action.get("target_id")
    spec = validate_action_request(context, action_type, target_type, target_id, action.get("spec") or {})

    if action_type == "set_status":
        result = meta_graph_post(context, str(target_id), {"status": spec["status"]})
        return {"target_type": target_type, "target_id": target_id, "status": spec["status"], "meta": result}

    if action_type == "set_budget":
        amount_minor = currency_to_minor(context, spec["amount_major"])
        result = meta_graph_post(context, str(target_id), {spec["budget_kind"]: amount_minor})
        return {
            "target_type": target_type, "target_id": target_id,
            "budget_kind": spec["budget_kind"], "amount_major": spec["amount_major"],
            "amount_minor": amount_minor, "currency": context.get("meta_currency") or "BRL", "meta": result,
        }

    if action_type == "duplicate":
        payload = {"status_option": "PAUSED"}
        if target_type in {"campaign", "adset"}:
            payload["deep_copy"] = bool(spec.get("deep_copy", True))
        if spec.get("rename_options"):
            payload["rename_options"] = spec["rename_options"]
        result = meta_graph_post(context, f"{target_id}/copies", payload)
        return {"target_type": target_type, "source_id": target_id, "copy_status": "PAUSED", "meta": result}

    if action_type == "create_pixel":
        account_path = context["ad_account_id"]
        result = meta_graph_post(context, f"{account_path}/adspixels", {"name": spec["name"]})
        return {"name": spec["name"], "pixel_id": result.get("id"), "meta": result}

    if action_type == "update_adset":
        result = meta_graph_post(context, str(target_id), spec["updates"])
        return {"target_id": target_id, "updated_fields": sorted(spec["updates"].keys()), "meta": result}

    if action_type == "activate_structure":
        ids = spec["ids"]
        results = []
        # Ordem superior -> inferior. Se um nível ainda estiver em revisão, effective_status refletirá isso.
        for entity_type, key in [("campaign", "campaign_id"), ("adset", "adset_id")]:
            entity_id = ids.get(key)
            if entity_id:
                ensure_target_belongs_to_account(context, entity_type, entity_id)
                results.append({entity_type: meta_graph_post(context, str(entity_id), {"status": "ACTIVE"})})
        for ad_id in ids.get("ad_ids") or []:
            ensure_target_belongs_to_account(context, "ad", ad_id)
            results.append({"ad": meta_graph_post(context, str(ad_id), {"status": "ACTIVE"})})
        return {"activated": ids, "meta_results": results}

    raise ValueError("Ação sem executor.")


def confirm_pending_action(context, action_id=None):
    user_text = context.get("_current_user_text") or ""
    if not is_explicit_confirmation_text(user_text):
        return {
            "executed": False,
            "reason": "A mensagem atual não contém uma confirmação explícita.",
            "instruction": "Peça ao cliente para responder algo como 'pode fazer' ou 'pode ativar'.",
        }

    action = get_pending_action(context, action_id)
    if not action:
        return {"executed": False, "reason": "Não existe ação pendente para este cliente."}
    if action["status"] != "PENDING_CONFIRMATION":
        return {"executed": False, "reason": f"Ação está em {action['status']}."}

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pending_actions
                SET status = 'EXECUTING', confirmed_at = NOW()
                WHERE id = %s AND status = 'PENDING_CONFIRMATION'
                RETURNING id;
                """,
                (action["id"],),
            )
            if not cursor.fetchone():
                return {"executed": False, "reason": "A ação já foi processada por outra execução."}

    try:
        result = execute_action_spec(context, action)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE pending_actions
                    SET status='EXECUTED', executed_at=NOW(), result=%s::jsonb, error=NULL
                    WHERE id=%s;
                    """,
                    (json.dumps(result, ensure_ascii=False, default=str), action["id"]),
                )
        log_activity(context, "action_executed", {"action_id": action["id"], "action_type": action["action_type"]})
        return {"executed": True, "action_id": action["id"], "summary": action.get("summary"), "result": result}
    except Exception as error:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE pending_actions SET status='FAILED', error=%s WHERE id=%s;",
                    (str(error), action["id"]),
                )
        log_activity(context, "action_failed", {"action_id": action["id"], "error": str(error)})
        raise


def cancel_pending_action(context, action_id=None):
    action = get_pending_action(context, action_id)
    if not action:
        return {"cancelled": False, "reason": "Nenhuma ação pendente encontrada."}
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pending_actions SET status='CANCELLED'
                WHERE id=%s AND company_id=%s AND user_id=%s AND status='PENDING_CONFIRMATION';
                """,
                (action["id"], context["company_id"], context["user_id"]),
            )
    return {"cancelled": True, "action_id": action["id"], "summary": action.get("summary")}


def list_pending_actions(context):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, action_type, target_type, target_id, summary, status, created_at
                FROM pending_actions
                WHERE company_id=%s AND user_id=%s
                ORDER BY created_at DESC LIMIT 10;
                """,
                (context["company_id"], context["user_id"]),
            )
            return cursor.fetchall()


def list_pixels(context):
    account_path = context["ad_account_id"]
    data = meta_graph_get(context, f"{account_path}/adspixels", fields="id,name,creation_time,last_fired_time", params={"limit": 100})
    return data.get("data", [])


def register_ad_review_watch(context, campaign_id, adset_id, ad_id, ad_name):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ad_review_watch (
                    company_id, user_id, whatsapp_number, ad_account_id,
                    campaign_id, adset_id, ad_id, ad_name, active, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE,NOW())
                ON CONFLICT (company_id, ad_id) DO UPDATE SET
                    user_id=EXCLUDED.user_id,
                    whatsapp_number=EXCLUDED.whatsapp_number,
                    ad_account_id=EXCLUDED.ad_account_id,
                    campaign_id=EXCLUDED.campaign_id,
                    adset_id=EXCLUDED.adset_id,
                    ad_name=EXCLUDED.ad_name,
                    active=TRUE,
                    updated_at=NOW();
                """,
                (
                    context["company_id"], context["user_id"], context["whatsapp_number"],
                    context["ad_account_id"], campaign_id, adset_id, ad_id, ad_name,
                ),
            )


def create_paused_structure(context, campaign, adset=None, ads=None):
    if not ACTION_AI_ENABLED:
        raise RuntimeError("Action AI está desativado.")
    if not context.get("can_create_campaigns"):
        raise PermissionError("Usuário sem permissão para criar campanhas.")
    if not is_explicit_creation_request(context.get("_current_user_text")):
        return {
            "created": False,
            "reason": "A criação só pode ocorrer quando o cliente pede explicitamente para criar/montar a campanha.",
        }

    campaign = dict(campaign or {})
    campaign_name = str(campaign.get("name") or "").strip()
    objective = str(campaign.get("objective") or "").strip()
    if not campaign_name or not objective:
        return {
            "created": False,
            "missing": [x for x, value in [("campaign.name", campaign_name), ("campaign.objective", objective)] if not value],
            "instruction": "Pergunte somente as informações faltantes em linguagem simples.",
        }

    campaign_payload = dict(campaign)
    campaign_payload["name"] = campaign_name
    campaign_payload["objective"] = objective
    campaign_payload["status"] = "PAUSED"
    campaign_payload.setdefault("buying_type", "AUCTION")
    campaign_payload.setdefault("special_ad_categories", [])
    campaign_payload.setdefault("is_adset_budget_sharing_enabled", False)

    created = {"campaign_id": None, "adset_id": None, "ad_ids": [], "creative_ids": []}
    try:
        campaign_result = meta_graph_post(context, f"{context['ad_account_id']}/campaigns", campaign_payload)
        campaign_id = campaign_result.get("id")
        if not campaign_id:
            raise RuntimeError("Meta não retornou campaign_id.")
        created["campaign_id"] = campaign_id

        adset_id = None
        adset_payload = None
        if adset:
            adset_payload = dict(adset)
            adset_payload["campaign_id"] = campaign_id
            adset_payload["status"] = "PAUSED"
            if not adset_payload.get("name"):
                raise ValueError("adset.name é obrigatório para criar o conjunto.")
            adset_result = meta_graph_post(context, f"{context['ad_account_id']}/adsets", adset_payload)
            adset_id = adset_result.get("id")
            if not adset_id:
                raise RuntimeError("Meta não retornou adset_id.")
            created["adset_id"] = adset_id

        for ad_spec in (ads or []):
            if not adset_id:
                raise ValueError("Não é possível criar anúncio sem conjunto de anúncios.")
            ad_spec = dict(ad_spec or {})
            ad_name = str(ad_spec.get("name") or "").strip()
            if not ad_name:
                raise ValueError("Cada anúncio precisa de name.")

            creative_id = ad_spec.get("creative_id")
            creative_payload = ad_spec.get("creative")
            if creative_payload:
                creative_payload = dict(creative_payload)
                creative_payload.setdefault("name", f"Creative - {ad_name}")
                creative_result = meta_graph_post(context, f"{context['ad_account_id']}/adcreatives", creative_payload)
                creative_id = creative_result.get("id")
                if creative_id:
                    created["creative_ids"].append(creative_id)
            if not creative_id:
                raise ValueError(
                    f"O anúncio '{ad_name}' precisa de creative_id ou creative com os dados que a Meta exige."
                )

            ad_payload = {
                "name": ad_name,
                "adset_id": adset_id,
                "creative": {"creative_id": creative_id},
                "status": "PAUSED",
            }
            for optional_key in ["tracking_specs", "conversion_domain", "adlabels"]:
                if ad_spec.get(optional_key) is not None:
                    ad_payload[optional_key] = ad_spec[optional_key]
            ad_result = meta_graph_post(context, f"{context['ad_account_id']}/ads", ad_payload)
            ad_id = ad_result.get("id")
            if not ad_id:
                raise RuntimeError(f"Meta não retornou ad_id para {ad_name}.")
            created["ad_ids"].append(ad_id)
            register_ad_review_watch(context, campaign_id, adset_id, ad_id, ad_name)

        # A criação pausada já foi explicitamente solicitada pelo cliente.
        # A ativação, por outro lado, fica obrigatoriamente pendente de confirmação.
        activation = create_pending_action(
            context,
            action_type="activate_structure",
            target_type="campaign",
            target_id=campaign_id,
            spec={"ids": created},
            summary=f"Ativar a campanha '{campaign_name}' e sua estrutura criada pausada.",
        )

        log_activity(context, "paused_structure_created", {"created": created, "activation_action_id": activation["action_id"]})

        adset_summary = adset_payload or {}
        return {
            "created": True,
            "status": "PAUSED",
            "created_ids": created,
            "activation_action_id": activation["action_id"],
            "campaign_summary": {
                "name": campaign_name,
                "objective": objective,
                "buying_type": campaign_payload.get("buying_type"),
                "bid_strategy": campaign_payload.get("bid_strategy"),
            },
            "adset_summary": {
                "name": adset_summary.get("name"),
                "daily_budget": adset_summary.get("daily_budget"),
                "lifetime_budget": adset_summary.get("lifetime_budget"),
                "optimization_goal": adset_summary.get("optimization_goal"),
                "billing_event": adset_summary.get("billing_event"),
                "targeting": adset_summary.get("targeting"),
                "start_time": adset_summary.get("start_time"),
                "end_time": adset_summary.get("end_time"),
                "promoted_object": adset_summary.get("promoted_object"),
                "destination_type": adset_summary.get("destination_type"),
            },
            "ads_summary": [
                {"name": a.get("name"), "creative_id": a.get("creative_id"), "has_new_creative": bool(a.get("creative"))}
                for a in (ads or [])
            ],
            "mandatory_user_message": (
                "✅ CAMPANHA CRIADA\n\n"
                "A estrutura foi criada na Meta e está PAUSADA.\n\n"
                "⚠️ SUA CAMPANHA ESTÁ CRIADA, PORÉM PAUSADA.\n"
                "É preciso que você confirme para que eu possa colocá-la em veiculação.\n\n"
                "Você pode responder: Pode ativar."
            ),
        }
    except Exception as error:
        log_activity(context, "paused_structure_creation_failed", {"partial_created": created, "error": str(error)})
        return {
            "created": False,
            "partial_created": created,
            "error": str(error),
            "important": "Qualquer objeto criado antes do erro permanece PAUSADO. Não ative nada automaticamente.",
        }


def get_meta_credentials_for_company_account(company_id, ad_account_id):
    initialize_database()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.slug AS company_slug, m.ad_account_id, m.connection_id,
                       mc.encrypted_access_token, mc.token_expires_at, mc.active AS connection_active
                FROM companies c
                JOIN meta_accounts m ON m.company_id=c.id
                LEFT JOIN meta_connections mc ON mc.id=m.connection_id
                WHERE c.id=%s AND m.ad_account_id=%s AND m.active=TRUE
                LIMIT 1;
                """,
                (company_id, ad_account_id),
            )
            row = cursor.fetchone()
    if not row:
        return None, "Conta Meta não encontrada para a empresa."
    if row.get("connection_id"):
        if not row.get("connection_active"):
            return None, "Conexão Meta inativa."
        expires_at = row.get("token_expires_at")
        if expires_at and expires_at <= datetime.now(timezone.utc):
            return None, "Autorização Meta expirada."
        return decrypt_token(row["encrypted_access_token"]), None
    if row.get("company_slug") == "principal" and META_ADS_ACCESS_TOKEN and ad_account_id == META_AD_ACCOUNT_ID:
        return META_ADS_ACCESS_TOKEN, None
    return None, "Token Meta indisponível."


def review_status_message(ad_name, status, feedback=None, issues=None):
    name = ad_name or "Anúncio"
    if status in {"ACTIVE", "PREAPPROVED"}:
        return (
            "✅ ANÚNCIO APROVADO\n\n"
            f"A Meta aprovou o anúncio: {name}.\n\n"
            "Se a estrutura ainda estiver pausada, ela continuará sem gastar até você autorizar a ativação."
        )
    if status == "DISAPPROVED":
        details = feedback or issues
        details_text = json.dumps(details, ensure_ascii=False, default=str) if details else "A Meta não informou um motivo detalhado nesta consulta."
        return (
            "❌ ANÚNCIO REPROVADO\n\n"
            f"A Meta reprovou o anúncio: {name}.\n\n"
            f"Motivo/feedback disponível: {details_text}\n\n"
            "Posso analisar o problema e sugerir a correção."
        )
    if status == "WITH_ISSUES":
        return (
            "⚠️ ANÚNCIO COM PROBLEMA\n\n"
            f"A Meta sinalizou um problema no anúncio: {name}.\n\n"
            "Posso consultar os detalhes e orientar a correção."
        )
    return None


def notify_review_state(row, status, feedback, issues):
    message = review_status_message(row.get("ad_name"), status, feedback, issues)
    if not message:
        return False

    context = {"company_id": row["company_id"], "user_id": row.get("user_id")}
    last_inbound = row.get("last_inbound_at")
    inside_window = bool(last_inbound and last_inbound >= datetime.now(timezone.utc) - timedelta(hours=23))

    if inside_window:
        response = send_whatsapp_message(row["whatsapp_number"], message)
        return bool(response is not None and response.status_code < 300)

    template = None
    if status in {"ACTIVE", "PREAPPROVED"}:
        template = WHATSAPP_REVIEW_APPROVED_TEMPLATE
    elif status in {"DISAPPROVED", "WITH_ISSUES"}:
        template = WHATSAPP_REVIEW_REJECTED_TEMPLATE

    if template:
        response = send_whatsapp_template(row["whatsapp_number"], template)
        queue_pending_notification(context, row["whatsapp_number"], "review_detail", message)
        return bool(response is not None and response.status_code < 300)

    # Sem template aprovado não é permitido iniciar texto livre fora da janela.
    # Guardamos o detalhe para a próxima mensagem do cliente.
    queue_pending_notification(context, row["whatsapp_number"], "review_detail", message)
    print("[V10] REVIEW_NOTIFICATION_QUEUED_NO_TEMPLATE", {"ad_id": row["ad_id"], "status": status})
    return False


def review_watcher_loop():
    """Um único worker por banco segura advisory lock e monitora revisão de anúncios."""
    if not REVIEW_WATCH_ENABLED:
        return
    while True:
        lock_conn = None
        try:
            lock_conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
            with lock_conn.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(910202610) AS acquired;")
                acquired = bool(cursor.fetchone()["acquired"])
            if not acquired:
                lock_conn.close()
                time.sleep(REVIEW_WATCH_INTERVAL_SECONDS)
                continue

            print("[V10] REVIEW_WATCHER_LEADER")
            while REVIEW_WATCH_ENABLED:
                try:
                    with get_db_connection() as conn:
                        with conn.cursor() as cursor:
                            cursor.execute(
                                """
                                SELECT w.*, u.last_inbound_at
                                FROM ad_review_watch w
                                LEFT JOIN users u ON u.id=w.user_id
                                WHERE w.active=TRUE
                                ORDER BY w.updated_at ASC
                                LIMIT 100;
                                """
                            )
                            watches = cursor.fetchall()

                    for row in watches:
                        token, token_error = get_meta_credentials_for_company_account(row["company_id"], row["ad_account_id"])
                        if token_error:
                            print("[V10] REVIEW_TOKEN_ERROR", row["ad_id"], token_error)
                            continue
                        params = {
                            "fields": "id,name,effective_status,configured_status,ad_review_feedback,issues_info",
                        }
                        proof = appsecret_proof(token)
                        if proof:
                            params["appsecret_proof"] = proof
                        response = requests.get(
                            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{row['ad_id']}",
                            params=params,
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=30,
                        )
                        if response.status_code >= 300:
                            print("[V10] REVIEW_META_ERROR", row["ad_id"], response.status_code, response.text)
                            continue
                        data = response.json()
                        status = data.get("effective_status")
                        feedback = data.get("ad_review_feedback")
                        issues = data.get("issues_info")
                        state_key = status if status in {"ACTIVE", "PREAPPROVED", "DISAPPROVED", "WITH_ISSUES"} else None

                        if state_key and row.get("notified_state") != state_key:
                            notified = notify_review_state(row, status, feedback, issues)
                            # Mesmo sem template, marcamos estado como processado porque a mensagem detalhada foi enfileirada.
                            processed_state = state_key
                        else:
                            processed_state = row.get("notified_state")

                        with get_db_connection() as conn:
                            with conn.cursor() as cursor:
                                cursor.execute(
                                    """
                                    UPDATE ad_review_watch
                                    SET last_effective_status=%s,
                                        last_review_feedback=%s::jsonb,
                                        last_issues_info=%s::jsonb,
                                        notified_state=%s,
                                        updated_at=NOW()
                                    WHERE id=%s;
                                    """,
                                    (
                                        status,
                                        json.dumps(feedback, ensure_ascii=False, default=str) if feedback is not None else None,
                                        json.dumps(issues, ensure_ascii=False, default=str) if issues is not None else None,
                                        processed_state,
                                        row["id"],
                                    ),
                                )
                except Exception as cycle_error:
                    print("[V10] REVIEW_WATCH_CYCLE_ERROR", repr(cycle_error))
                    traceback.print_exc()
                time.sleep(REVIEW_WATCH_INTERVAL_SECONDS)
        except Exception as error:
            print("[V10] REVIEW_WATCHER_ERROR", repr(error))
            time.sleep(REVIEW_WATCH_INTERVAL_SECONDS)
        finally:
            if lock_conn:
                try:
                    lock_conn.close()
                except Exception:
                    pass


def ensure_review_watcher_started():
    global review_watcher_started
    if not REVIEW_WATCH_ENABLED:
        return
    with review_watcher_lock:
        if review_watcher_started:
            return
        thread = threading.Thread(target=review_watcher_loop, daemon=True, name="meta-review-watcher")
        thread.start()
        review_watcher_started = True
        print("[V10] REVIEW_WATCHER_THREAD_STARTED")


def consult_review_status(context, search=None):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if search:
                cursor.execute(
                    """
                    SELECT ad_id, ad_name, campaign_id, adset_id, last_effective_status,
                           last_review_feedback, last_issues_info, updated_at
                    FROM ad_review_watch
                    WHERE company_id=%s AND (ad_name ILIKE %s OR ad_id=%s)
                    ORDER BY updated_at DESC LIMIT 30;
                    """,
                    (context["company_id"], f"%{search}%", search),
                )
            else:
                cursor.execute(
                    """
                    SELECT ad_id, ad_name, campaign_id, adset_id, last_effective_status,
                           last_review_feedback, last_issues_info, updated_at
                    FROM ad_review_watch
                    WHERE company_id=%s
                    ORDER BY updated_at DESC LIMIT 30;
                    """,
                    (context["company_id"],),
                )
            return cursor.fetchall()


# =========================================================
# V10.2 — HYBRID CAMPAIGN CREATION WIZARD + MÍDIA
# =========================================================

WIZARD_ACTIVE_STATUSES = {"DRAFT", "WAITING_CREATION_CONFIRMATION"}
WIZARD_CAMPAIGN_ALLOWED_FIELDS = {
    "name", "objective", "buying_type", "special_ad_categories",
    "bid_strategy", "is_adset_budget_sharing_enabled",
}
WIZARD_ADSET_ALLOWED_FIELDS = {
    "name", "daily_budget", "lifetime_budget", "billing_event", "optimization_goal",
    "optimization_sub_event", "bid_strategy", "bid_amount", "targeting", "promoted_object",
    "destination_type", "start_time", "end_time", "attribution_spec",
}


def deep_merge_dict(base, updates):
    result = dict(base or {})
    for key, value in (updates or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def get_active_campaign_wizard(context):
    initialize_database()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, company_id, user_id, ad_account_id, status, draft,
                       created_ids, created_at, updated_at
                FROM campaign_wizards
                WHERE company_id=%s AND user_id=%s AND ad_account_id=%s
                  AND status IN ('DRAFT','WAITING_CREATION_CONFIRMATION')
                ORDER BY updated_at DESC
                LIMIT 1;
                """,
                (context["company_id"], context["user_id"], context["ad_account_id"]),
            )
            row = cursor.fetchone()
    if not row:
        return None
    row = dict(row)
    row["draft"] = dict(row.get("draft") or {})
    row["assets"] = list_campaign_wizard_assets(row["id"])
    row["missing"] = campaign_wizard_missing(row["draft"], row["assets"])
    return row


def start_campaign_wizard(context, initial=None):
    if not CAMPAIGN_WIZARD_ENABLED:
        raise RuntimeError("Campaign Wizard está desativado.")
    if not context.get("can_create_campaigns"):
        raise PermissionError("Usuário sem permissão para criar campanhas.")
    if not context.get("ad_account_id"):
        raise RuntimeError("Selecione uma conta Meta antes de iniciar a campanha.")

    existing = get_active_campaign_wizard(context)
    if existing:
        return existing

    draft = dict(initial or {})
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO campaign_wizards (company_id, user_id, ad_account_id, status, draft)
                VALUES (%s,%s,%s,'DRAFT',%s::jsonb)
                RETURNING id;
                """,
                (
                    context["company_id"], context["user_id"], context["ad_account_id"],
                    json.dumps(draft, ensure_ascii=False, default=str),
                ),
            )
            wizard_id = cursor.fetchone()["id"]
    log_activity(context, "campaign_wizard_started", {"wizard_id": wizard_id})
    return get_active_campaign_wizard(context)


def update_campaign_wizard(context, updates):
    wizard = get_active_campaign_wizard(context)
    if not wizard:
        wizard = start_campaign_wizard(context)
    if wizard["status"] != "DRAFT":
        return {
            "updated": False,
            "reason": "O resumo já está aguardando confirmação. Se quiser mudar algo, peça para editar antes de criar.",
            "wizard": wizard,
        }
    draft = deep_merge_dict(wizard.get("draft") or {}, updates or {})
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE campaign_wizards
                SET draft=%s::jsonb, updated_at=NOW()
                WHERE id=%s AND company_id=%s AND user_id=%s;
                """,
                (
                    json.dumps(draft, ensure_ascii=False, default=str),
                    wizard["id"], context["company_id"], context["user_id"],
                ),
            )
    return get_active_campaign_wizard(context)


def reopen_campaign_wizard(context):
    wizard = get_active_campaign_wizard(context)
    if not wizard:
        return {"updated": False, "reason": "Nenhuma campanha em montagem."}
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE campaign_wizards SET status='DRAFT', updated_at=NOW() WHERE id=%s;",
                (wizard["id"],),
            )
    return get_active_campaign_wizard(context)


def cancel_campaign_wizard(context):
    wizard = get_active_campaign_wizard(context)
    if not wizard:
        return {"cancelled": False, "reason": "Nenhuma campanha em montagem."}
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE campaign_wizards SET status='CANCELLED', completed_at=NOW(), updated_at=NOW() WHERE id=%s;",
                (wizard["id"],),
            )
    log_activity(context, "campaign_wizard_cancelled", {"wizard_id": wizard["id"]})
    return {"cancelled": True, "wizard_id": wizard["id"]}


def list_campaign_wizard_assets(wizard_id):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, media_type, mime_type, original_name, caption,
                       meta_image_hash, meta_video_id, created_at
                FROM campaign_wizard_assets
                WHERE wizard_id=%s
                ORDER BY id ASC;
                """,
                (wizard_id,),
            )
            return [dict(row) for row in cursor.fetchall()]


def campaign_wizard_missing(draft, assets=None):
    draft = dict(draft or {})
    campaign = dict(draft.get("campaign") or {})
    adset = dict(draft.get("adset") or {})
    creative = dict(draft.get("creative") or {})
    assets = assets or []
    missing = []

    if not str(campaign.get("name") or "").strip():
        missing.append("nome da campanha")
    if not str(campaign.get("objective") or "").strip():
        missing.append("objetivo principal")
    if "special_ad_categories" not in campaign:
        missing.append("categoria especial (imóveis/habitação, crédito, emprego, política ou nenhuma)")
    if not str(adset.get("name") or "").strip():
        missing.append("nome do conjunto de anúncios")
    if adset.get("daily_budget_major") is None and adset.get("lifetime_budget_major") is None:
        missing.append("orçamento")
    if not adset.get("targeting"):
        missing.append("público/segmentação")
    if not str(adset.get("optimization_goal") or "").strip():
        missing.append("otimização do conjunto")
    if not str(adset.get("billing_event") or "").strip():
        missing.append("forma de cobrança")
    existing_creatives = list(draft.get("existing_creative_ids") or [])
    if assets and not str(creative.get("page_id") or "").strip():
        missing.append("Página do Facebook/identidade do anúncio")

    if not assets and not existing_creatives:
        missing.append("pelo menos um criativo (imagem/vídeo ou creative_id existente)")

    # Para criativos novos em formato de link, precisamos de destino e texto.
    if assets:
        if not str(creative.get("destination_url") or "").strip():
            missing.append("link/destino do anúncio")
        if not str(creative.get("primary_text") or "").strip():
            missing.append("texto principal do anúncio")
        if not str(creative.get("headline") or "").strip():
            missing.append("título do anúncio")

    return missing


def list_pages_for_wizard(context):
    data = meta_graph_get(context, "me/accounts", fields="id,name,category", params={"limit": 100})
    return data.get("data", [])


def search_meta_locations(context, query, location_types=None, limit=20):
    params = {
        "type": "adgeolocation",
        "q": str(query or "").strip(),
        "limit": max(1, min(int(limit or 20), 50)),
    }
    if location_types:
        params["location_types"] = json.dumps(location_types, ensure_ascii=False)
    data = meta_graph_get(context, "search", params=params)
    return data.get("data", [])


def search_meta_interests(context, query, limit=20):
    data = meta_graph_get(
        context,
        "search",
        params={"type": "adinterest", "q": str(query or "").strip(), "limit": max(1, min(int(limit or 20), 50))},
    )
    return data.get("data", [])


def meta_graph_post_file(context, path, field_name, filename, content, mime_type, data=None, timeout=120):
    _, access_token, error = get_meta_credentials(context)
    if error:
        raise RuntimeError(error)
    payload = dict(data or {})
    proof = appsecret_proof(access_token)
    if proof:
        payload["appsecret_proof"] = proof
    response = http_session.post(
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/{path}",
        data=payload,
        files={field_name: (filename, content, mime_type or "application/octet-stream")},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Meta upload {path}: {response.status_code} {response.text}")
    return response.json()


def upload_wizard_media_to_meta(context, wizard, media_id, media_type, caption=None):
    if media_type not in {"image", "video"}:
        raise ValueError("O wizard aceita imagem ou vídeo.")
    media = retrieve_whatsapp_media(media_id)
    content = media["content"]
    if len(content) > WIZARD_MAX_MEDIA_BYTES:
        raise ValueError(
            f"O arquivo tem {len(content) / 1024 / 1024:.1f} MB e ultrapassa o limite operacional de "
            f"{WIZARD_MAX_MEDIA_BYTES / 1024 / 1024:.0f} MB desta versão."
        )
    mime = (media.get("mime_type") or "application/octet-stream").split(";")[0]
    extension = mimetypes.guess_extension(mime) or (".jpg" if media_type == "image" else ".mp4")
    filename = f"wizard_{wizard['id']}_{uuid.uuid4().hex[:8]}{extension}"

    image_hash = None
    video_id = None
    if media_type == "image":
        result = meta_graph_post_file(
            context, f"{context['ad_account_id']}/adimages", "filename", filename, content, mime, timeout=90,
        )
        images = result.get("images") or {}
        if isinstance(images, dict) and images:
            first = next(iter(images.values()))
            if isinstance(first, dict):
                image_hash = first.get("hash")
        image_hash = image_hash or result.get("hash")
        if not image_hash:
            raise RuntimeError("A Meta recebeu a imagem, mas não retornou o image_hash.")
    else:
        result = meta_graph_post_file(
            context, f"{context['ad_account_id']}/advideos", "source", filename, content, mime,
            data={"title": filename}, timeout=180,
        )
        video_id = result.get("id") or result.get("video_id")
        if not video_id:
            raise RuntimeError("A Meta recebeu o vídeo, mas não retornou o video_id.")

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO campaign_wizard_assets (
                    wizard_id, company_id, whatsapp_media_id, media_type, mime_type,
                    original_name, caption, meta_image_hash, meta_video_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (company_id, whatsapp_media_id)
                DO UPDATE SET caption=EXCLUDED.caption
                RETURNING id;
                """,
                (
                    wizard["id"], context["company_id"], media_id, media_type, mime,
                    filename, caption, image_hash, video_id,
                ),
            )
            asset_id = cursor.fetchone()["id"]

    # Se a pessoa usou a legenda como copy e ainda não havia texto principal, aproveita-a como rascunho.
    if caption:
        draft = dict(wizard.get("draft") or {})
        creative = dict(draft.get("creative") or {})
        if not creative.get("primary_text"):
            update_campaign_wizard(context, {"creative": {"primary_text": caption}})

    log_activity(context, "campaign_wizard_media_uploaded", {"wizard_id": wizard["id"], "asset_id": asset_id, "media_type": media_type})
    return {
        "asset_id": asset_id,
        "media_type": media_type,
        "meta_image_hash": image_hash,
        "meta_video_id": video_id,
    }


def process_wizard_media_message(sender, user_id, media_id, media_type, caption=None, progress=None):
    progress = progress or start_request_progress(sender)
    try:
        context = get_user_context(sender)
        if not context:
            raise RuntimeError("Usuário não cadastrado.")
        context = dict(context)
        wizard = get_active_campaign_wizard(context)
        if not wizard:
            progress.stop()
            send_whatsapp_message(
                sender,
                "📎 RECEBI O ARQUIVO\n\nAinda não existe uma campanha em montagem. "
                "Diga algo como: Quero criar uma campanha. Depois eu uso a imagem ou o vídeo dentro do wizard.",
            )
            return
        result = upload_wizard_media_to_meta(context, wizard, media_id, media_type, caption)
        wizard = get_active_campaign_wizard(context)
        progress.stop()
        send_whatsapp_message(
            sender,
            (
                f"✅ {media_type.upper()} ADICIONADO À CAMPANHA\n\n"
                f"O criativo foi preparado para uso no anúncio. Agora faltam: "
                f"{', '.join(wizard.get('missing') or []) if wizard.get('missing') else 'nenhuma informação obrigatória'}.\n\n"
                "Pode continuar me dizendo, por texto ou áudio, como quer montar a campanha."
            ),
        )
    except Exception as error:
        print("[V10.2] WIZARD_MEDIA_ERROR", repr(error))
        traceback.print_exc()
        progress.stop()
        send_whatsapp_message(sender, build_user_friendly_error(error, stage="envio do criativo"))
    finally:
        progress.stop()
        release_analysis_lock(user_id)


def build_wizard_creative_payload(asset, creative):
    page_id = str(creative.get("page_id") or "").strip()
    destination_url = str(creative.get("destination_url") or "").strip()
    primary_text = str(creative.get("primary_text") or "").strip()
    headline = str(creative.get("headline") or "").strip()
    description = str(creative.get("description") or "").strip()
    cta_type = str(creative.get("call_to_action_type") or "LEARN_MORE").upper()

    if not page_id or not destination_url:
        raise ValueError("Para criativo novo, page_id e destination_url são obrigatórios.")

    cta = {"type": cta_type, "value": {"link": destination_url}}
    if asset.get("media_type") == "image":
        link_data = {
            "link": destination_url,
            "message": primary_text,
            "name": headline,
            "call_to_action": cta,
            "image_hash": asset.get("meta_image_hash"),
        }
        if description:
            link_data["description"] = description
        return {"object_story_spec": {"page_id": page_id, "link_data": link_data}}

    if asset.get("media_type") == "video":
        video_data = {
            "video_id": asset.get("meta_video_id"),
            "message": primary_text,
            "title": headline,
            "call_to_action": cta,
        }
        if description:
            video_data["link_description"] = description
        return {"object_story_spec": {"page_id": page_id, "video_data": video_data}}

    raise ValueError("Tipo de criativo não suportado pelo wizard.")


def build_wizard_structure(context, wizard):
    draft = dict(wizard.get("draft") or {})
    assets = list(wizard.get("assets") or [])
    missing = campaign_wizard_missing(draft, assets)
    if missing:
        return None, missing

    campaign_src = dict(draft.get("campaign") or {})
    campaign = {k: v for k, v in campaign_src.items() if k in WIZARD_CAMPAIGN_ALLOWED_FIELDS and v is not None}
    campaign.setdefault("buying_type", "AUCTION")
    campaign.setdefault("is_adset_budget_sharing_enabled", False)

    adset_src = dict(draft.get("adset") or {})
    adset = {k: v for k, v in adset_src.items() if k in WIZARD_ADSET_ALLOWED_FIELDS and v is not None}
    if adset_src.get("daily_budget_major") is not None:
        adset["daily_budget"] = currency_to_minor(context, adset_src["daily_budget_major"])
    if adset_src.get("lifetime_budget_major") is not None:
        adset["lifetime_budget"] = currency_to_minor(context, adset_src["lifetime_budget_major"])

    creative = dict(draft.get("creative") or {})
    ads = []
    existing_creatives = list(draft.get("existing_creative_ids") or [])
    for index, creative_id in enumerate(existing_creatives, start=1):
        ads.append({
            "name": f"{campaign['name']} - Anúncio {index}",
            "creative_id": str(creative_id),
        })

    for index, asset in enumerate(assets, start=len(ads) + 1):
        asset = dict(asset)
        ad_name = asset.get("caption") or f"{campaign['name']} - Criativo {index}"
        ads.append({
            "name": str(ad_name)[:120],
            "creative": build_wizard_creative_payload(asset, creative),
        })

    return {"campaign": campaign, "adset": adset, "ads": ads}, []


def campaign_wizard_summary(context, wizard):
    draft = dict(wizard.get("draft") or {})
    campaign = dict(draft.get("campaign") or {})
    adset = dict(draft.get("adset") or {})
    creative = dict(draft.get("creative") or {})
    assets = list(wizard.get("assets") or [])

    budget = "não definido"
    if adset.get("daily_budget_major") is not None:
        budget = f"{format_brl(adset['daily_budget_major'])}/dia"
    elif adset.get("lifetime_budget_major") is not None:
        budget = f"{format_brl(adset['lifetime_budget_major'])} total"

    return {
        "wizard_id": wizard["id"],
        "campaign": {
            "name": campaign.get("name"),
            "objective": campaign.get("objective"),
            "business_goal": campaign.get("business_goal"),
            "special_ad_categories": campaign.get("special_ad_categories"),
        },
        "adset": {
            "name": adset.get("name"),
            "budget": budget,
            "targeting": adset.get("targeting"),
            "optimization_goal": adset.get("optimization_goal"),
            "billing_event": adset.get("billing_event"),
            "destination_type": adset.get("destination_type"),
            "start_time": adset.get("start_time"),
            "end_time": adset.get("end_time"),
            "promoted_object": adset.get("promoted_object"),
        },
        "creative": {
            "page_id": creative.get("page_id"),
            "destination_url": creative.get("destination_url"),
            "primary_text": creative.get("primary_text"),
            "headline": creative.get("headline"),
            "description": creative.get("description"),
            "call_to_action_type": creative.get("call_to_action_type") or "LEARN_MORE",
            "assets": len(assets),
            "existing_creatives": len(draft.get("existing_creative_ids") or []),
        },
        "missing": campaign_wizard_missing(draft, assets),
    }


def prepare_campaign_wizard_confirmation(context):
    wizard = get_active_campaign_wizard(context)
    if not wizard:
        return {"ready": False, "reason": "Nenhuma campanha em montagem."}
    if wizard["status"] == "WAITING_CREATION_CONFIRMATION":
        return {"ready": True, "summary": campaign_wizard_summary(context, wizard), "confirmation": "Pode criar"}
    summary = campaign_wizard_summary(context, wizard)
    if summary["missing"]:
        return {
            "ready": False,
            "missing": summary["missing"],
            "instruction": "Pergunte apenas o próximo bloco lógico de informações faltantes, em linguagem simples.",
        }
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE campaign_wizards SET status='WAITING_CREATION_CONFIRMATION', updated_at=NOW() WHERE id=%s;",
                (wizard["id"],),
            )
    wizard = get_active_campaign_wizard(context)
    return {
        "ready": True,
        "summary": campaign_wizard_summary(context, wizard),
        "mandatory_message": (
            "📋 RESUMO DA CAMPANHA\n\n"
            "Revise campanha, conjunto e anúncios antes da criação.\n\n"
            "A estrutura será criada PAUSADA e não haverá gasto neste momento.\n\n"
            "Se estiver tudo certo, responda: Pode criar."
        ),
    }


def is_explicit_wizard_create_confirmation(text):
    n = normalize_text(text or "")
    negatives = ["nao", "não", "cancela", "cancelar", "nao crie", "não crie"]
    if any(x in n for x in negatives):
        return False
    phrases = ["pode criar", "crie agora", "pode montar", "confirma criacao", "confirmo a criacao", "criar agora"]
    return any(x in n for x in phrases)


def create_campaign_from_wizard(context):
    wizard = get_active_campaign_wizard(context)
    if not wizard:
        return {"created": False, "reason": "Nenhuma campanha em montagem."}
    if wizard["status"] != "WAITING_CREATION_CONFIRMATION":
        return {"created": False, "reason": "Primeiro preciso apresentar o resumo e pedir a confirmação de criação."}
    if not is_explicit_wizard_create_confirmation(context.get("_current_user_text")):
        return {"created": False, "reason": "A mensagem atual não confirmou explicitamente a criação. Peça: Pode criar."}

    structure, missing = build_wizard_structure(context, wizard)
    if missing:
        reopen_campaign_wizard(context)
        return {"created": False, "missing": missing}

    # create_paused_structure mantém o contrato de segurança: tudo nasce PAUSED.
    result = create_paused_structure(
        context,
        campaign=structure["campaign"],
        adset=structure["adset"],
        ads=structure["ads"],
    )
    if result.get("created"):
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE campaign_wizards
                    SET status='CREATED', created_ids=%s::jsonb, completed_at=NOW(), updated_at=NOW()
                    WHERE id=%s;
                    """,
                    (json.dumps(result.get("created_ids") or {}, ensure_ascii=False), wizard["id"]),
                )
    return result


ACTION_AI_TOOLS = [
    {
        "type": "function",
        "name": "propor_acao_meta",
        "description": (
            "Cria uma ação pendente que ainda NÃO altera a Meta. Use para pausar/reativar, mudar orçamento, "
            "duplicar, alterar público/posicionamentos/otimização de conjunto ou criar Pixel. Sempre peça confirmação depois."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_type": {"type": "string", "enum": ["set_status", "set_budget", "duplicate", "update_adset", "create_pixel"]},
                "target_type": {"type": ["string", "null"], "enum": ["campaign", "adset", "ad", None]},
                "target_id": {"type": ["string", "null"]},
                "spec": {"type": "object", "additionalProperties": True},
                "summary": {"type": ["string", "null"]},
            },
            "required": ["action_type", "spec"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "confirmar_acao_pendente",
        "description": "Executa uma ação pendente SOMENTE quando a mensagem atual do cliente contém confirmação explícita.",
        "parameters": {
            "type": "object",
            "properties": {"action_id": {"type": ["integer", "null"]}},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "cancelar_acao_pendente",
        "description": "Cancela a ação pendente mais recente ou um action_id específico.",
        "parameters": {
            "type": "object",
            "properties": {"action_id": {"type": ["integer", "null"]}},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "listar_acoes_pendentes",
        "description": "Lista ações propostas/executadas recentemente para evitar confusão em confirmações.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "criar_estrutura_pausada",
        "description": (
            "Fluxo legado de criação pausada. Na conversa normal com cliente, prefira SEMPRE o Campaign Wizard V10.2. "
            "Use este caminho somente para compatibilidade técnica/controlada. Nunca use em mera recomendação."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "campaign": {"type": "object", "additionalProperties": True},
                "adset": {"type": ["object", "null"], "additionalProperties": True},
                "ads": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
            },
            "required": ["campaign"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "listar_pixels",
        "description": "Lista os Pixels/fontes AdsPixel vinculados à conta selecionada.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "consultar_revisao_anuncios",
        "description": "Consulta o status de revisão monitorado dos anúncios criados pela ferramenta.",
        "parameters": {
            "type": "object",
            "properties": {"search": {"type": ["string", "null"]}},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "iniciar_wizard_campanha",
        "description": "Inicia ou retorna o Campaign Creation Wizard persistente. Use quando o cliente disser que quer criar/montar uma campanha.",
        "parameters": {
            "type": "object",
            "properties": {"initial": {"type": ["object", "null"], "additionalProperties": True}},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "atualizar_wizard_campanha",
        "description": "Salva no rascunho do wizard informações que o cliente já forneceu em linguagem natural ou áudio.",
        "parameters": {
            "type": "object",
            "properties": {"updates": {"type": "object", "additionalProperties": True}},
            "required": ["updates"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "consultar_wizard_campanha",
        "description": "Retorna o estado atual, ativos recebidos e campos faltantes do wizard.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "editar_wizard_campanha",
        "description": "Reabre para edição um wizard que já estava aguardando confirmação de criação.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "cancelar_wizard_campanha",
        "description": "Cancela a campanha em montagem sem criar nada na Meta.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "listar_paginas_meta",
        "description": "Lista Páginas disponíveis para usar como identidade do anúncio. Mostre nomes ao cliente, não IDs técnicos quando possível.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "buscar_localizacoes_meta",
        "description": "Pesquisa localizações válidas da Meta para transformar cidades/regiões ditas pelo cliente em targeting real.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "location_types": {"type": ["array", "null"], "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "buscar_interesses_meta",
        "description": "Pesquisa interesses válidos da Meta quando a estratégia realmente exigir segmentação por interesses.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "preparar_confirmacao_wizard",
        "description": "Valida o rascunho e, quando completo, gera o resumo final antes de criar qualquer objeto na Meta.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "criar_campanha_do_wizard",
        "description": "Cria campanha/conjunto/anúncios SOMENTE após o resumo e a confirmação explícita 'Pode criar'. Tudo nasce PAUSED.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

DYNAMIC_ANALYSIS_TOOLS = [
    {
        "type": "function",
        "name": "consultar_insights",
        "description": (
            "Consulta Insights e o setup real da Meta. Retorna objective/optimization_goal, campos "
            "contextuais de resultado da própria Meta e catálogos de actions/cost_per_action_type. "
            "Não pressupõe que resultado seja lead e não escolhe KPI por tabela fixa."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "Data inicial inclusiva YYYY-MM-DD."},
                "until": {"type": "string", "description": "Data final inclusiva YYYY-MM-DD."},
                "level": {"type": "string", "enum": ["account", "campaign", "adset", "ad"]},
                "search": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "sort_by": {"type": "string", "enum": sorted(V91_BASE_SORT_METRICS)},
                "sort_order": {"type": "string", "enum": ["asc", "desc"]},
                "min_spend": {"type": "number", "minimum": 0},
                "include_zero_spend": {"type": "boolean"},
                "action_type": {
                    "type": ["string", "null"],
                    "description": (
                        "Use somente depois de identificar nos dados um action_type pertinente ao setup. "
                        "O backend extrairá e validará esse evento sem possuir uma tabela fixa."
                    ),
                },
            },
            "required": ["since", "until", "level"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "comparar_periodos",
        "description": (
            "Compara períodos preservando o setup real retornado pela Meta. Não cria CPL/KPI universal. "
            "Pode comparar um action_type escolhido após inspeção do setup."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["current_month_vs_previous_equivalent", "last_n_days_vs_previous_n_days", "custom"]},
                "level": {"type": "string", "enum": ["account", "campaign", "adset", "ad"]},
                "n_days": {"type": ["integer", "null"], "minimum": 1, "maximum": 365},
                "period_a_since": {"type": ["string", "null"]},
                "period_a_until": {"type": ["string", "null"]},
                "period_b_since": {"type": ["string", "null"]},
                "period_b_until": {"type": ["string", "null"]},
                "period_a_label": {"type": ["string", "null"]},
                "period_b_label": {"type": ["string", "null"]},
                "search": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "sort_by": {"type": "string", "enum": sorted(V91_BASE_SORT_METRICS)},
                "action_type": {"type": ["string", "null"]},
            },
            "required": ["mode", "level"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "listar_estrutura_meta",
        "description": (
            "Lê da própria Meta o setup técnico: campaign objective, optimization_goal, "
            "optimization_sub_event, billing_event, promoted_object, destination_type e campos disponíveis. "
            "Use para entender para que a campanha/conjunto foi configurado antes de julgá-lo."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["campaign", "adset", "ad"]},
                "search": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["level"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "validar_resultado_meta",
        "description": (
            "Depois de interpretar o setup e escolher um action_type como resultado pertinente, valida "
            "contra os Insights da Meta se esse evento realmente existe para a entidade e retorna valor/custo."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "until": {"type": "string"},
                "level": {"type": "string", "enum": ["campaign", "adset", "ad"]},
                "entity_id": {"type": "string"},
                "action_type": {"type": "string"},
            },
            "required": ["since", "until", "level", "entity_id", "action_type"],
            "additionalProperties": False,
        },
    },
]


DYNAMIC_ANALYSIS_TOOLS.extend(ACTION_AI_TOOLS)


def build_dynamic_instructions(context):
    today = get_today_for_context(context)
    return f"""
Você é um GESTOR DE TRÁFEGO IA operando Meta Ads por WhatsApp dentro de um SaaS multiempresa.
Você conversa com pessoas leigas. Elas não devem precisar abrir o Gerenciador de Anúncios nem conhecer termos técnicos para tomar decisões.

Empresa: {context.get('company_name')}.
Conta Meta: {context.get('ad_account_id') or 'nenhuma'}.
Data atual na timezone da conta: {today.isoformat()}.
Estado do Campaign Wizard: {json.dumps(context.get("_campaign_wizard"), ensure_ascii=False, default=str) if context.get("_campaign_wizard") else "nenhum wizard ativo"}.

MISSÃO
1. Entender o que o cliente quer, seja texto ou uma transcrição de áudio.
2. Ler o setup REAL retornado pela Meta antes de avaliar performance.
3. Analisar de verdade: conclusão -> evidência -> interpretação -> ação.
4. Quando houver uma ação segura que o sistema consegue executar, traduzir a intenção leiga em operação Meta.
5. Nunca inventar números, IDs, resultados, público, orçamento, evento ou configuração.

REGRA CENTRAL DA V9.1 PRESERVADA
- A Meta é a fonte da verdade sobre objetivo/setup.
- Não existe tabela fixa objetivo -> KPI.
- Não deduza objetivo pelo nome da campanha.
- Não trate Resultados como sinônimo de lead.
- Leia campaign objective e depois optimization_goal/optimization_sub_event/billing_event/promoted_object/destination_type do conjunto.
- Use objective_results/cost_per_objective_result/cost_per_result quando disponíveis; caso contrário confronte setup com actions/cost_per_action_type.
- Se escolher um action_type como resultado principal, valide com validar_resultado_meta.
- Em conta com setups mistos, não invente um KPI universal.

PROTOCOLO ANALÍTICO
- Comece pela conclusão, não por uma planilha narrada.
- Use 2 a 4 números como evidência.
- Explique o que eles significam e qual decisão eles sustentam.
- Separe fato, leitura e hipótese.
- Quando necessário, aprofunde conta -> campanha -> conjunto -> anúncio.
- Se o cliente perguntar "o que você faria?", entregue uma recomendação executável e priorizada.

V10 ACTION AI — PRINCÍPIO DE SEGURANÇA
- IA interpreta e propõe; backend valida propriedade, permissão e payload; Meta executa.
- Nunca faça POST livre para a Meta fora das ferramentas controladas.
- Toda alteração em uma estrutura EXISTENTE exige confirmação antes da execução.
- Sempre que a recomendação for uma ação que a ferramenta consegue realizar, você PODE usar propor_acao_meta para deixar o plano pendente. Isso NÃO executa nada. Depois explique em linguagem simples e peça confirmação.
- Se o cliente responder "faça", "pode fazer", "pode ativar", "confirmo" etc., use confirmar_acao_pendente. O backend ainda verifica a mensagem real antes de executar.
- Se houver mais de uma ação pendente e a referência estiver ambígua, liste as ações e pergunte qual delas.
- Nunca trate um "sim" fora de contexto como autorização para gasto ou alteração.

CRIAÇÃO DE CAMPANHA — WIZARD HÍBRIDO V10.2
- Quando o cliente disser que quer criar/montar uma campanha, use iniciar_wizard_campanha.
- O cliente fala de forma leiga; você traduz para configuração técnica, mas nunca inventa informação crítica.
- Salve cada informação recebida com atualizar_wizard_campanha. O wizard persiste entre mensagens e áudios.
- Pergunte apenas UM BLOCO LÓGICO por vez. Ordem preferida: objetivo/nome -> categoria especial -> orçamento/período -> público -> destino/Pixel/evento -> Página -> criativos/copy -> revisão.
- Para localização/interesses, use buscar_localizacoes_meta/buscar_interesses_meta em vez de inventar IDs.
- Para Pixel e Página, use listar_pixels/listar_paginas_meta e apresente nomes simples.
- O cliente pode enviar IMAGEM ou VÍDEO pelo WhatsApp durante o wizard; o backend fará upload controlado para a Meta e anexará o ativo ao rascunho.
- Você pode escrever/sugerir copy, headline e CTA, mas o cliente deve conseguir revisar antes da criação.
- Antes de criar qualquer objeto, use preparar_confirmacao_wizard e mostre o resumo.
- Só use criar_campanha_do_wizard quando a mensagem atual confirmar explicitamente: "Pode criar" ou equivalente inequívoco.
- Toda campaign, adset e ad criados pelo wizard nascem PAUSED.
- Depois da criação, a ATIVAÇÃO permanece uma segunda ação pendente e exige nova confirmação.
- Se o cliente quiser mudar algo depois do resumo, use editar_wizard_campanha, ajuste e gere novo resumo.
- Não use criar_estrutura_pausada diretamente quando houver wizard ativo; prefira o fluxo determinístico do wizard.
- Depois de criar a estrutura pausada, mostre uma confirmação curta com três blocos:

🎯 CAMPANHA
Objetivo/direcionamento principal.

👥 CONJUNTO DE ANÚNCIOS
Orçamento, público/segmentação, período e otimização/destino principais.

🎨 ANÚNCIOS
Quantidade, nome dos anúncios/criativos e destino/texto quando disponíveis.

Depois destaque exatamente a ideia:
⚠️ CAMPANHA PAUSADA
Sua campanha está criada, porém pausada.
É preciso que você confirme para que eu possa colocá-la em veiculação.

- Não ative a estrutura no mesmo turno da criação. A ativação fica como ação pendente separada.

AÇÕES EM ESTRUTURAS EXISTENTES
Use propor_acao_meta para:
- pausar/reativar campanha, conjunto ou anúncio (set_status);
- alterar orçamento de campanha/conjunto (set_budget);
- duplicar campanha/conjunto/anúncio, sempre com cópia PAUSED (duplicate);
- alterar targeting, posicionamentos, otimização, evento/destino e outros campos permitidos do conjunto (update_adset);
- criar Pixel (create_pixel).

PIXEL
- listar_pixels consulta Pixels da conta.
- criar Pixel exige ação pendente + confirmação.
- Criar o objeto Pixel na Meta NÃO instala o Pixel no site. Se o cliente pedir instalação, explique que será necessária uma integração com site/GTM/CAPI/WordPress ou equivalente.

REVISÃO DE ANÚNCIOS
- A ferramenta monitora anúncios criados por ela.
- Quando o status for aprovado/ativo/preapproved, o cliente recebe mensagem de aprovação.
- Quando for DISAPPROVED/WITH_ISSUES, recebe reprovação/problema e você pode analisar o feedback.
- Não diga "a campanha foi aprovada" como fato técnico se o que a Meta revisou foi o anúncio. Prefira "anúncio aprovado" ou "todos os anúncios estão aptos".

LINGUAGEM PARA LEIGOS
O cliente pode falar coisas como:
- "Minhas campanhas estão boas?"
- "O que está errado?"
- "O que você faria?"
- "Então faça."
- "Quero vender mais sem aumentar o orçamento."
- "Arruma as campanhas ruins."
- "Crie uma campanha para essa nova oferta."
- "Pausa aquele anúncio."
- "Aumenta esse orçamento para 150 por dia."
Você deve traduzir isso para análise/ação sem exigir que ele conheça Campaign ID, AdSet ID, CTR ou nomes da API. Use IDs internamente quando necessário.

PERÍODOS
- Sem período explícito: mês atual até hoje, salvo contexto anterior.
- "este mês x mês passado" com mês incompleto: períodos equivalentes.
- Datas explícitas: respeite exatamente.
- Follow-up: preserve período e entidade da conversa quando fizer sentido.

FORMATO DE RESPOSTA ANALÍTICA
- Comece com a resposta principal em 1 ou 2 frases.
- Use no máximo 3 a 5 blocos visuais.
- Cada bloco deve ter um título curto com UM emoji e MAIÚSCULAS.
- Prefira bullets quando houver 2+ itens.
- Nunca faça um parágrafo longo com vários números.

🧠 DIAGNÓSTICO
Conclusão direta em até 3 linhas.

📌 EVIDÊNCIAS
2 a 4 dados realmente necessários.

🔎 O QUE ISSO SIGNIFICA
Traduza os dados para uma decisão de negócio, sem jargão desnecessário.

🎯 O QUE EU FARIA AGORA
Uma ação concreta, priorizada e explicada em linguagem simples. Se puder ser executada, proponha a ação e peça confirmação.

FORMATAÇÃO WHATSAPP
- O cliente é leigo e lê no celular. Facilite o escaneamento.
- Sem #, ##, **, _, crases ou tabelas Markdown.
- Não escreva parede de texto.
- Títulos curtos em MAIÚSCULAS com um emoji.
- Parágrafos de no máximo 2 a 3 frases.
- Use uma linha em branco entre blocos.
- Use • para listas.
- Não repita a mesma conclusão com palavras diferentes.
- O backend fará sanitização e quebra final de parágrafos.
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
            action_type=arguments.get("action_type"),
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
            action_type=arguments.get("action_type"),
        )

    if tool_name == "listar_estrutura_meta":
        return list_meta_structure(
            context,
            level=arguments["level"],
            search=arguments.get("search"),
            limit=arguments.get("limit", 50),
        )

    if tool_name == "validar_resultado_meta":
        return validate_meta_result(
            context,
            since=arguments["since"],
            until=arguments["until"],
            level=arguments["level"],
            entity_id=arguments["entity_id"],
            action_type=arguments["action_type"],
        )

    if tool_name == "propor_acao_meta":
        return create_pending_action(
            context,
            action_type=arguments["action_type"],
            target_type=arguments.get("target_type"),
            target_id=arguments.get("target_id"),
            spec=arguments.get("spec") or {},
            summary=arguments.get("summary"),
        )

    if tool_name == "confirmar_acao_pendente":
        return confirm_pending_action(context, arguments.get("action_id"))

    if tool_name == "cancelar_acao_pendente":
        return cancel_pending_action(context, arguments.get("action_id"))

    if tool_name == "listar_acoes_pendentes":
        return list_pending_actions(context)

    if tool_name == "criar_estrutura_pausada":
        return create_paused_structure(
            context,
            campaign=arguments.get("campaign") or {},
            adset=arguments.get("adset"),
            ads=arguments.get("ads") or [],
        )

    if tool_name == "listar_pixels":
        return list_pixels(context)

    if tool_name == "consultar_revisao_anuncios":
        return consult_review_status(context, arguments.get("search"))

    if tool_name == "iniciar_wizard_campanha":
        return start_campaign_wizard(context, arguments.get("initial") or {})

    if tool_name == "atualizar_wizard_campanha":
        return update_campaign_wizard(context, arguments.get("updates") or {})

    if tool_name == "consultar_wizard_campanha":
        return get_active_campaign_wizard(context) or {"active": False}

    if tool_name == "editar_wizard_campanha":
        return reopen_campaign_wizard(context)

    if tool_name == "cancelar_wizard_campanha":
        return cancel_campaign_wizard(context)

    if tool_name == "listar_paginas_meta":
        return list_pages_for_wizard(context)

    if tool_name == "buscar_localizacoes_meta":
        return search_meta_locations(
            context, arguments.get("query"), arguments.get("location_types"), arguments.get("limit", 20)
        )

    if tool_name == "buscar_interesses_meta":
        return search_meta_interests(context, arguments.get("query"), arguments.get("limit", 20))

    if tool_name == "preparar_confirmacao_wizard":
        return prepare_campaign_wizard_confirmation(context)

    if tool_name == "criar_campanha_do_wizard":
        return create_campaign_from_wizard(context)

    raise ValueError(f"Tool desconhecida: {tool_name}")


@app.route("/v91-capabilities", methods=["GET"])
def v91_capabilities():
    return {
        "build_id": BUILD_ID,
        "engine": ANALYSIS_ENGINE,
        "objective_mapping_hardcoded": OBJECTIVE_MAPPING_HARDCODED,
        "meta_setup_fields": [
            "campaign.objective",
            "adset.optimization_goal",
            "adset.optimization_sub_event",
            "adset.billing_event",
            "adset.promoted_object",
            "adset.destination_type",
        ],
        "meta_result_fields_attempted": [
            "objective_results",
            "cost_per_objective_result",
            "cost_per_result",
            "objective_result_rate",
            "actions",
            "cost_per_action_type",
            "action_values",
        ],
        "validation_rule": "AI interprets setup; backend validates selected action_type against Meta Insights.",
    }, 200

@app.route("/v10-capabilities", methods=["GET"])
@app.route("/v102-capabilities", methods=["GET"])
def v10_capabilities():
    return {
        "build_id": BUILD_ID,
        "engine": ANALYSIS_ENGINE,
        "action_ai_enabled": ACTION_AI_ENABLED,
        "audio_input": True,
        "image_video_input": True,
        "campaign_wizard_enabled": CAMPAIGN_WIZARD_ENABLED,
        "progress_update_seconds": PROGRESS_UPDATE_SECONDS,
        "transcribe_model": OPENAI_TRANSCRIBE_MODEL,
        "review_watcher_enabled": REVIEW_WATCH_ENABLED,
        "writes_require_confirmation": True,
        "new_structures_forced_paused": True,
        "supported_actions": [
            "pause_activate_campaign_adset_ad",
            "change_budget_campaign_adset",
            "update_adset_targeting_optimization",
            "duplicate_campaign_adset_ad_paused",
            "create_campaign_adset_ad_paused",
            "list_pixels",
            "create_pixel_with_confirmation",
            "review_status_notifications",
            "whatsapp_audio_transcription",
            "campaign_creation_wizard_persistent",
            "whatsapp_image_video_to_meta_assets",
            "meta_pages_location_interest_lookup",
            "progress_heartbeat_every_50s_default",
        ],
    }, 200


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
