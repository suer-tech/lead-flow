from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "leads.db"
STATIC_DIR = BASE_DIR / "static"

LeadStatus = Literal[
    "new",
    "proposal_sent",
    "interested",
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


SEED_LEADS = [
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_database() -> None:
    with closing(connect()) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                budget INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (status IN ('new','proposal_sent','interested','diagnostics','proposal','negotiations','won','lost'))
            )
            """
        )
        count = db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        if count == 0:
            now = utc_now()
            db.executemany(
                """
                INSERT INTO leads (
                    company, status, source, source_url, vacancy, offer,
                    next_action, next_action_at, created_at, updated_at
                ) VALUES (?, 'proposal_sent', 'hh.ru', ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        company,
                        f"https://hh.ru/vacancy/{vacancy_id}",
                        vacancy,
                        offer,
                        "Проверить ответ и при отсутствии реакции отправить короткое напоминание",
                        None,
                        now,
                        now,
                    )
                    for company, vacancy_id, vacancy, offer in SEED_LEADS
                ],
            )
        db.commit()


def serialize(row: sqlite3.Row) -> dict:
    return dict(row)


app = FastAPI(title="Lead Flow CRM", version="1.0.0")
init_database()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/leads")
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
        query += " AND (company LIKE ? OR vacancy LIKE ? OR offer LIKE ? OR notes LIKE ?)"
        pattern = f"%{search.strip()}%"
        params.extend([pattern] * 4)
    query += " ORDER BY updated_at DESC, id DESC"
    with closing(connect()) as db:
        return [serialize(row) for row in db.execute(query, params).fetchall()]


@app.post("/api/leads", status_code=status.HTTP_201_CREATED)
def create_lead(payload: LeadCreate) -> dict:
    values = payload.model_dump(mode="json")
    now = utc_now()
    columns = list(values) + ["created_at", "updated_at"]
    parameters = [values[column] for column in values] + [now, now]
    placeholders = ", ".join("?" for _ in columns)
    with closing(connect()) as db:
        cursor = db.execute(
            f"INSERT INTO leads ({', '.join(columns)}) VALUES ({placeholders})",
            parameters,
        )
        db.commit()
        row = db.execute("SELECT * FROM leads WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return serialize(row)


@app.patch("/api/leads/{lead_id}")
def update_lead(lead_id: int, payload: LeadUpdate) -> dict:
    values = payload.model_dump(exclude_unset=True, mode="json")
    if not values:
        raise HTTPException(status_code=400, detail="Нет полей для изменения")
    values["updated_at"] = utc_now()
    assignment = ", ".join(f"{column} = ?" for column in values)
    with closing(connect()) as db:
        cursor = db.execute(
            f"UPDATE leads SET {assignment} WHERE id = ?",
            [*values.values(), lead_id],
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Лид не найден")
        db.commit()
        row = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return serialize(row)


@app.delete("/api/leads/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lead(lead_id: int) -> Response:
    with closing(connect()) as db:
        cursor = db.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Лид не найден")
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/{path:path}")
def frontend(path: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")

