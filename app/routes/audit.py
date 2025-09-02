from __future__ import annotations

import uuid
import json
import os
import re
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timezone
from typing import Optional, Mapping, Any

from flask import request, has_request_context
try:
    # берем путь к логу из Flask-конфига, если есть контекст приложения
    from flask import current_app
except Exception:  # на случай запуска вне Flask
    current_app = None  # type: ignore

from .db import get_conn


# ========================
# --- NDJSON логгер ---
# ========================

# простая маскировка e-mail / телефонов в строках (чтобы случайно не утащить PII)
_EMAIL_RE = re.compile(r"([a-zA-Z0-9_.+\-]+)@([a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+)")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s\-]?){7,15}\d(?!\d)")

def _mask_str(s: str) -> str:
    s = _EMAIL_RE.sub(lambda m: "***@" + m.group(2), s)
    s = _PHONE_RE.sub(lambda m: "*" * (len(m.group(0)) - 2) + m.group(0)[-2:], s)
    return s

def _mask(obj: Any) -> Any:
    if isinstance(obj, str):
        return _mask_str(obj)
    if isinstance(obj, Mapping):
        return {k: _mask(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [ _mask(x) for x in obj ]
    return obj

class _NdjsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # базовые поля
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        # переносим extra-поля как есть
        for k, v in record.__dict__.items():
            if k in ("name","msg","args","levelname","levelno","pathname","filename","module",
                     "exc_info","exc_text","stack_info","lineno","funcName","created","msecs",
                     "relativeCreated","thread","threadName","processName","process","message","asctime"):
                continue
            payload[k] = v
        # безопасно маскируем строки
        payload = _mask(payload)
        return json.dumps(payload, ensure_ascii=False)

_file_logger: Optional[logging.Logger] = None

def _resolve_log_path() -> str:
    # 1) Flask config COMMISSIONS_LOG_FILE
    if current_app and hasattr(current_app, "config"):
        p = current_app.config.get("COMMISSIONS_LOG_FILE")
        if p:
            return p
    # 2) env var
    p = os.getenv("COMMISSIONS_LOG_FILE")
    if p:
        return p
    # 3) дефолт
    return "logs/commission_actions.ndjson"

def _get_file_logger() -> logging.Logger:
    global _file_logger
    if _file_logger:
        return _file_logger

    log_path = _resolve_log_path()
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)

    logger = logging.getLogger("commission_ndjson")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # не дублировать в корневой логгер

    # если хэндлер уже висит (перезагрузка модуля), не добавляем второй раз
    if not logger.handlers:
        fmt = _NdjsonFormatter()
        # ротация: полуночь UTC, 14 бэкапов
        fh = TimedRotatingFileHandler(
            log_path, when="midnight", interval=1, backupCount=14, utc=True, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        logger.addHandler(fh)
    _file_logger = logger
    return logger


# ===============================
# --- Основная функция записи ---
# ===============================

def log_commission_action(*,
    app_id: str,
    admin_id: str,
    action: str,          # 'decision' | 'update_status' | 'comment' | 'attach' | 'view'
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
    comment: Optional[str] = None,
    meta: Optional[dict] = None,
    ip_addr: Optional[str] = None,
    user_agent: Optional[str] = None,
    conn=None
) -> None:
    """
    Пишет запись в commission_logs (PostgreSQL) и дублирует событие в NDJSON-файл.

    Транзакционность:
      - Если передан conn — пишем в ТУ ЖЕ транзакцию БД без commit (commit делает вызывающий код).
        Файловый лог при этом пишется сразу (вне транзакции), т.к. это независимый канал.
      - Если conn не передан — откроем соединение сами и выполним commit, затем запишем в файл.

    Путь к NDJSON-файлу:
      - Flask config: COMMISSIONS_LOG_FILE
      - ENV: COMMISSIONS_LOG_FILE
      - default: logs/commission_actions.ndjson
    """
    # безопасно берём ip/ua из request-контекста, если он есть
    if not ip_addr and has_request_context():
        ip_addr = (request.headers.get("X-Forwarded-For") or request.remote_addr or "")
        if ip_addr and "," in ip_addr:  # берём первый адрес из списка
            ip_addr = ip_addr.split(",")[0].strip()
    if not user_agent and has_request_context():
        user_agent = request.headers.get("User-Agent", "")

    args = (
        str(uuid.uuid4()), app_id, admin_id, action,
        old_status, new_status, comment,
        ip_addr or "", user_agent or "",
        json.dumps(meta or {}, ensure_ascii=False)
    )

    if conn is None:
        with get_conn() as _conn, _conn.cursor() as c:
            c.execute("""
                INSERT INTO commission_logs
                    (id, app_id, admin_id, action, old_status, new_status, comment, ip_addr, user_agent, meta)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, args)
            _conn.commit()
        # После успешного коммита — пишем в файл
        _write_ndjson(action=action, app_id=app_id, admin_id=admin_id,
                      old_status=old_status, new_status=new_status, comment=comment,
                      ip=ip_addr, user_agent=user_agent, meta=meta)
    else:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO commission_logs
                    (id, app_id, admin_id, action, old_status, new_status, comment, ip_addr, user_agent, meta)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, args)
        # Коммит делает вызывающий код. Файл — пишем best-effort, без влияния на транзакцию:
        _write_ndjson(action=action, app_id=app_id, admin_id=admin_id,
                      old_status=old_status, new_status=new_status, comment=comment,
                      ip=ip_addr, user_agent=user_agent, meta=meta)


# =======================================
# --- Вспомогательная запись в NDJSON ---
# =======================================

def _write_ndjson(*, action: str, app_id: str, admin_id: Optional[str],
                  old_status: Optional[str], new_status: Optional[str],
                  comment: Optional[str], ip: Optional[str],
                  user_agent: Optional[str], meta: Optional[dict]) -> None:
    """Пишет одну строку NDJSON. Любые ошибки подавляются (не мешаем основному потоку)."""
    try:
        logger = _get_file_logger()
        payload = {
            "action": action,
            "app_id": app_id,
            "admin_id": admin_id,
            "old_status": old_status,
            "new_status": new_status,
            "comment": comment,
            "ip": ip,
            "user_agent": user_agent,
            "meta": meta or {},
            "tags": ["commission", action],
        }
        # добавим маршрут, если есть request-контекст
        if has_request_context():
            payload["route"] = request.path
        logger.info("commission action", extra=payload)
    except Exception:
        # намеренно глушим любые исключения логирования в файл
        pass
