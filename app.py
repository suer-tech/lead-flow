from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
IS_VERCEL = bool(os.getenv("VERCEL"))
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
USE_POSTGRES = bool(DATABASE_URL)
DB_PATH = Path("/tmp/leads.db") if IS_VERCEL else BASE_DIR / "leads.db"

CRM_PASSWORD = os.getenv("CRM_PASSWORD", "")
CRM_SECRET_KEY = os.getenv("CRM_SECRET_KEY") or CRM_PASSWORD or "lead-flow-local-only"
SESSION_COOKIE = "lead_flow_session"

LeadStatus = Literal[
    "new",
    "proposal_sent",
    "interested",
    "follow_up",
    "diagnostics",
    "proposal",
    "negotiations",
    "won",
    "lost",
]


class LeadBase(BaseModel):
    company: str = Field(min_length=1, max_length=180)
    status: LeadStatus = "new"
    source: str = Field(default="hh.ru", max_length=80)
    source_url: HttpUrl | None = None
    vacancy: str = Field(default="", max_length=240)
    contact_name: str = Field(default="", max_length=160)
    contact_role: str = Field(default="", max_length=160)
    phone: str = Field(default="", max_length=80)
    email: str = Field(default="", max_length=180)
    offer: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=5000)
    next_action: str = Field(default="", max_length=500)
    next_action_at: str | None = None
    budget: int | None = Field(default=None, ge=0)
    proposal_sent_at: str | None = None


class LeadCreate(LeadBase):
    pass


class LeadUpdate(BaseModel):
    company: str | None = Field(default=None, min_length=1, max_length=180)
    status: LeadStatus | None = None
    source: str | None = Field(default=None, max_length=80)
    source_url: HttpUrl | None = None
    vacancy: str | None = Field(default=None, max_length=240)
    contact_name: str | None = Field(default=None, max_length=160)
    contact_role: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=180)
    offer: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=5000)
    next_action: str | None = Field(default=None, max_length=500)
    next_action_at: str | None = None
    budget: int | None = Field(default=None, ge=0)
    proposal_sent_at: str | None = None


class LoginPayload(BaseModel):
    password: str = Field(min_length=1, max_length=500)


SEED_LEADS = [
    ("АО «Универсальная лизинговая компания»", "134770896", "Инженер по нейросетям (рабочее место г. Хабаровск)", "RAG-помощник по внутренним регламентам и материалам лизинговой сделки со ссылками на источники и проверкой сотрудником"),
    ("ООО «Бьюти Лайф»", "135732676", "AI-инженер (Claude)", "Автоматизация одного потока обращений или документов с записью результата в Bitrix24 и передачей внутренней IT-команде"),
    ("АО «Камский завод металлоконструкций ТЭМПО»", "135534594", "Специалист по работе с ИИ", "OCR и проверка комплектности одного вида документации с подготовкой структурированных данных для 1С"),
    ("ООО «ТК ИНЖИНИРИНГ»", "135732490", "ИИ-инженер (автоматизация инженерных и строительных процессов)", "OCR входящих счетов, актов, спецификаций и ведомостей с передачей в Bitrix24 или 1С"),
    ("ООО «Ника»", "136059774", "Менеджер проектов по внедрению ИИ", "Внешняя разработка одного AI-пилота с кодом, документацией и метриками"),
    ("ООО «Султан»", "136070122", "Специалист по внедрению ИИ", "OCR документов или сменных отчетов с последующей интеграцией в 1С"),
    ("Нео-Терм", "135520807", "AI-разработчик", "OCR технических документов или RAG-помощник по нормативной базе"),
    ("ОМЕГА", "136118219", "Руководитель проектов по цифровой трансформации", "Документный пилот для закупок, логистики или продаж за 10–15 рабочих дней"),
    ("Мирролла", "133512440", "AI/ML-разработчик", "Узкий OCR/RAG-пилот, совместимый с внутренней архитектурой компании"),
    ("Нижегороднефтегазпроект", "133724345", "AI Engineer / LLM Engineer", "Сверка опросного листа и ТКП с таблицей несоответствий и ссылками на документы"),
    ("Северно", "135471833", "Технический специалист по автоматизации бизнес-процессов", "Разбор коммерческих предложений поставщиков и передача проверенных данных в учетную систему"),
    ("Ландшафт Комплекс", "135223280", "Ведущий AI-инженер по автоматизации", "Извлечение данных из КП или проектного документа с журналом источников и изменений"),
    ("Бухучет-Иркутск", "135233422", "Специалист по внедрению ИИ", "Черновик ответа на требования ИФНС по базе документов с проверкой сотрудником"),
    ("ГК Дикомп", "135434844", "Руководитель проектов ИИ и автоматизации", "Внешняя разработка одного пилота под внутреннего руководителя с передачей кода и метрик"),
    ("Альфа Групп", "135444980", "Эксперт по внедрению AI-решений", "Автоматизация аналитики закупок или сверки данных маркетплейсов"),
    ("ФЕНИКС", "135781618", "Ассистент проекта по внедрению ИИ", "Пилот для процессов продаж, остатков или закупок с интеграцией в 1С"),
    ("Сеть клиник Подружки", "135243952", "AI-инженер", "Административная автоматизация документов с Bitrix24 и 1С"),
    ("ЛУИС+", "134211010", "AI-инженер", "Ограниченный MVP для одного внутреннего процесса с передачей результата команде"),
    ("DOGMA", "135437758", "Инженер ML и LLM", "RAG-пилот по внутренним документам с API-интеграцией"),
    ("Бизнес-Недвижимость", "135750295", "Специалист по сметам и AI", "Извлечение и проверка данных из сметной документации с контролем специалиста"),
]

SEED_STATUSES = {
    "135732676": "interested",
    "136059774": "lost",
    "136118219": "interested",
    "133512440": "lost",
    "133724345": "lost",
    "135471833": "interested",
    "135223280": "interested",
    "135781618": "lost",
    "134211010": "interested",
    "135437758": "lost",
    "135750295": "interested",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    if USE_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def sql(query: str) -> str:
    return query.replace("?", "%s") if USE_POSTGRES else query


def serialize(row: Any) -> dict:
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def init_database() -> None:
    id_column = "BIGSERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with closing(connect()) as db:
        db.execute(
            f"""
            CREATE TABLE IF NOT EXISTS leads (
                id {id_column},
                company TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                source TEXT NOT NULL DEFAULT 'hh.ru',
                source_url TEXT,
                vacancy TEXT NOT NULL DEFAULT '',
                contact_name TEXT NOT NULL DEFAULT '',
                contact_role TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                offer TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                next_action TEXT NOT NULL DEFAULT '',
                next_action_at TEXT,
                budget BIGINT,
                proposal_sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (status IN ('new','proposal_sent','interested','follow_up','diagnostics','proposal','negotiations','won','lost'))
            )
            """
        )
        if USE_POSTGRES:
            db.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS proposal_sent_at TEXT")
            db.execute(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conrelid = 'leads'::regclass
                          AND conname = 'leads_status_check'
                          AND pg_get_constraintdef(oid) NOT LIKE '%follow_up%'
                    ) THEN
                        ALTER TABLE leads DROP CONSTRAINT leads_status_check;
                        ALTER TABLE leads ADD CONSTRAINT leads_status_check
                        CHECK (status IN ('new','proposal_sent','interested','follow_up','diagnostics','proposal','negotiations','won','lost'));
                    END IF;
                END $$
                """
            )
        else:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(leads)").fetchall()}
            if "proposal_sent_at" not in columns:
                db.execute("ALTER TABLE leads ADD COLUMN proposal_sent_at TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS leads_source_url_unique ON leads(source_url)")
        existing_count = db.execute("SELECT COUNT(*) AS count FROM leads").fetchone()["count"]
        if existing_count:
            db.commit()
            return

        now = utc_now()
        values = [
            (
                company,
                SEED_STATUSES.get(vacancy_id, "proposal_sent"),
                f"https://hh.ru/vacancy/{vacancy_id}",
                vacancy,
                offer,
                "Проверить ответ и при отсутствии реакции отправить короткое напоминание",
                None,
                now,
                now,
            )
            for company, vacancy_id, vacancy, offer in SEED_LEADS
        ]
        insert_prefix = "INSERT INTO" if USE_POSTGRES else "INSERT OR IGNORE INTO"
        conflict = " ON CONFLICT (source_url) DO NOTHING" if USE_POSTGRES else ""
        seed_query = sql(
            f"""
            {insert_prefix} leads (
                company, status, source, source_url, vacancy, offer,
                next_action, next_action_at, created_at, updated_at
            ) VALUES (?, ?, 'hh.ru', ?, ?, ?, ?, ?, ?, ?){conflict}
            """
        )
        if USE_POSTGRES:
            with db.cursor() as cursor:
                cursor.executemany(seed_query, values)
        else:
            db.executemany(seed_query, values)
        db.commit()


def session_token() -> str:
    return hmac.new(CRM_SECRET_KEY.encode(), b"lead-flow-authenticated", hashlib.sha256).hexdigest()


def is_authenticated(request: Request) -> bool:
    if not CRM_PASSWORD and not IS_VERCEL:
        return True
    supplied = request.cookies.get(SESSION_COOKIE, "")
    return bool(CRM_PASSWORD) and hmac.compare_digest(supplied, session_token())


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Требуется вход")


app = FastAPI(title="Lead Flow CRM", version="1.2.0")
init_database()


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "database": "postgres" if USE_POSTGRES else "sqlite",
        "persistent": USE_POSTGRES or not IS_VERCEL,
    }


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    return {
        "authenticated": is_authenticated(request),
        "required": bool(CRM_PASSWORD) or IS_VERCEL,
        "configured": bool(CRM_PASSWORD) or not IS_VERCEL,
    }


@app.post("/api/auth/login")
def login(payload: LoginPayload) -> JSONResponse:
    if not CRM_PASSWORD:
        raise HTTPException(status_code=503, detail="На Vercel задайте переменную CRM_PASSWORD")
    if not hmac.compare_digest(payload.password, CRM_PASSWORD):
        raise HTTPException(status_code=401, detail="Неверный пароль")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        SESSION_COOKIE,
        session_token(),
        httponly=True,
        secure=IS_VERCEL,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout() -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/leads", dependencies=[Depends(require_auth)])
def list_leads(
    search: str = Query(default="", max_length=200),
    lead_status: LeadStatus | None = Query(default=None, alias="status"),
) -> list[dict]:
    query = "SELECT * FROM leads WHERE 1=1"
    params: list[str] = []
    if lead_status:
        query += " AND status = ?"
        params.append(lead_status)
    if search.strip():
        query += " AND (LOWER(company) LIKE ? OR LOWER(vacancy) LIKE ? OR LOWER(offer) LIKE ? OR LOWER(notes) LIKE ?)"
        pattern = f"%{search.strip().lower()}%"
        params.extend([pattern] * 4)
    query += " ORDER BY updated_at DESC, id DESC"
    with closing(connect()) as db:
        return [serialize(row) for row in db.execute(sql(query), params).fetchall()]


@app.post("/api/leads", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_auth)])
def create_lead(payload: LeadCreate) -> dict:
    values = payload.model_dump(mode="json")
    now = utc_now()
    if values["status"] == "proposal_sent" and not values.get("proposal_sent_at"):
        values["proposal_sent_at"] = now
    columns = list(values) + ["created_at", "updated_at"]
    parameters = [values[column] for column in values] + [now, now]
    placeholders = ", ".join("?" for _ in columns)
    returning = " RETURNING id" if USE_POSTGRES else ""
    with closing(connect()) as db:
        cursor = db.execute(
            sql(f"INSERT INTO leads ({', '.join(columns)}) VALUES ({placeholders}){returning}"),
            parameters,
        )
        lead_id = cursor.fetchone()["id"] if USE_POSTGRES else cursor.lastrowid
        db.commit()
        row = db.execute(sql("SELECT * FROM leads WHERE id = ?"), (lead_id,)).fetchone()
        return serialize(row)


@app.patch("/api/leads/{lead_id}", dependencies=[Depends(require_auth)])
def update_lead(lead_id: int, payload: LeadUpdate) -> dict:
    values = payload.model_dump(exclude_unset=True, mode="json")
    if not values:
        raise HTTPException(status_code=400, detail="Нет полей для изменения")
    with closing(connect()) as db:
        existing = db.execute(
            sql("SELECT status, proposal_sent_at FROM leads WHERE id = ?"),
            (lead_id,),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Лид не найден")
        now = utc_now()
        if values.get("status") == "proposal_sent" and "proposal_sent_at" not in values:
            values["proposal_sent_at"] = existing["proposal_sent_at"] or now
        values["updated_at"] = now
        assignment = ", ".join(f"{column} = ?" for column in values)
        cursor = db.execute(
            sql(f"UPDATE leads SET {assignment} WHERE id = ?"),
            [*values.values(), lead_id],
        )
        db.commit()
        row = db.execute(sql("SELECT * FROM leads WHERE id = ?"), (lead_id,)).fetchone()
        return serialize(row)


@app.delete("/api/leads/{lead_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_auth)])
def delete_lead(lead_id: int) -> Response:
    with closing(connect()) as db:
        cursor = db.execute(sql("DELETE FROM leads WHERE id = ?"), (lead_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Лид не найден")
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/{path:path}")
def frontend(path: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
