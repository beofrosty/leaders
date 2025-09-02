# app/routes/provisioning.py
import json
import uuid
from datetime import datetime, timezone
from io import StringIO
import csv

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    session, jsonify, current_app, abort, Response, stream_with_context
)
from flask_babel import gettext as _
from werkzeug.security import generate_password_hash

from ..decorators import provisioner_required
from ..db import get_conn, get_user_by_email

bp = Blueprint('prov', __name__)


# ---------- helpers ----------

def _parse_dt_local(value: str | None):
    """Приходит из <input type=datetime-local>. Если без TZ — помечаем как UTC (без сдвига)."""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _log(actor_id: str, target_id: str | None, action: str, meta: dict | None, ip: str | None):
    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
            INSERT INTO internal_user_logs (id, actor_user_id, target_user_id, action, meta, ip)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            str(uuid.uuid4()), actor_id, target_id, action,
            (None if meta is None else json.dumps(meta, ensure_ascii=False)),
            ip
        ))
        conn.commit()


def _status_to_code(v: str) -> str:
    """UI может прислать 'Одобрено'/'Отклонено' или уже 'approved'/'rejected'."""
    s = (v or "").strip().lower()
    if not s:
        return ""
    if s in ("approved", "одобрено"):
        return "approved"
    if s in ("rejected", "отклонено"):
        return "rejected"
    return s  # на всякий


def _code_to_label(code: str) -> str:
    if code == "approved":
        return _("Одобрено")
    if code == "rejected":
        return _("Отклонено")
    return code or ""


# ---------- routes ----------

@bp.route('/provisioning', methods=['GET'])
@provisioner_required
def dashboard():
    # Список внутренних админов/комиссии
    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
          SELECT id, full_name, email, role, is_active, access_expires_at, created_at,
                 must_change_password, inn, phone, position, priority
            FROM users
           WHERE role = 'admin'
           ORDER BY created_at DESC NULLS LAST
        """)
        users = c.fetchall()

        c.execute("""
          SELECT l.created_at, a.email AS actor_email, t.email AS target_email, l.action, l.meta
            FROM internal_user_logs l
            LEFT JOIN users a ON a.id = l.actor_user_id
            LEFT JOIN users t ON t.id = l.target_user_id
           ORDER BY l.created_at DESC
           LIMIT 100
        """)
        logs = c.fetchall()

    return render_template('provisioning_dashboard.html', users=users, logs=logs)


@bp.post('/provisioning/create')
@provisioner_required
def create_internal_user():
    f = request.form
    full_name  = (f.get('full_name') or '').strip()
    inn        = (f.get('inn') or '').strip()
    phone      = (f.get('phone') or '').strip()
    email      = (f.get('email') or '').strip().lower()
    password   = (f.get('password') or '')
    password2  = (f.get('password2') or '')
    access_raw = (f.get('access_until') or '').strip()
    position   = (f.get('position') or '').strip()
    priority   = (f.get('priority') or '').strip()  # Низкий/Средний/Высокий
    role       = 'admin'  # создаём комиссию/админа

    if not full_name or not inn or not email or not password or not access_raw:
        flash(('error', _('Заполните обязательные поля.')))
        return redirect(url_for('prov.dashboard'))
    if password != password2:
        flash(('error', _('Пароли не совпадают.')))
        return redirect(url_for('prov.dashboard'))

    access_until = _parse_dt_local(access_raw)
    if not access_until:
        flash(('error', _('Некорректная дата/время доступа.')))
        return redirect(url_for('prov.dashboard'))

    if get_user_by_email(email):
        flash(('error', _('Пользователь с таким e-mail уже есть.')))
        return redirect(url_for('prov.dashboard'))

    uid = str(uuid.uuid4())
    try:
        with get_conn() as conn, conn.cursor() as c:
            c.execute("""
              INSERT INTO users (
                id, email, full_name, password_hash, is_verified, created_at, role,
                access_expires_at, is_active, must_change_password,
                inn, phone, position, priority
              ) VALUES (%s, %s, %s, %s, TRUE, NOW(), %s,
                        %s, TRUE, TRUE,
                        %s, %s, %s, %s)
            """, (
                uid, email, full_name, generate_password_hash(password), role,
                access_until, inn or None, phone or None, position or None, priority or None
            ))
            conn.commit()

        _log(
            session.get('user_id'), uid, 'create',
            {
                'inn': inn, 'phone': phone, 'position': position, 'priority': priority,
                'access_expires_at': access_until.isoformat()
            },
            request.remote_addr
        )

        flash(('success', _('Администратор создан. Ему необходимо сменить пароль при первом входе.')))
    except Exception:
        current_app.logger.exception("provisioning create failed")
        flash(('error', _('Ошибка базы данных.')))
    return redirect(url_for('prov.dashboard'))


@bp.post('/provisioning/extend/<user_id>')
@provisioner_required
def extend_access(user_id):
    access_raw = (request.form.get('access_until') or '').strip()
    new_dt = _parse_dt_local(access_raw)
    if not new_dt:
        return jsonify(ok=False, error='bad_date'), 400

    with get_conn() as conn, conn.cursor() as c:
        c.execute("UPDATE users SET access_expires_at = %s WHERE id = %s AND is_active = TRUE", (new_dt, user_id))
        if c.rowcount == 0:
            return jsonify(ok=False, error='not_found_or_inactive'), 404
        conn.commit()

    _log(session.get('user_id'), user_id, 'extend', {'access_expires_at': new_dt.isoformat()}, request.remote_addr)
    return jsonify(ok=True, access_expires_at=new_dt.isoformat())


@bp.post('/provisioning/update/<user_id>')
@provisioner_required
def update_user(user_id):
    # Из модалки "Изменить": ФИО, должность, телефон, время доступа, приоритет
    f = request.form
    full_name = (f.get('full_name') or '').strip()
    phone     = (f.get('phone') or '').strip()
    position  = (f.get('position') or '').strip()
    priority  = (f.get('priority') or '').strip()
    access    = _parse_dt_local((f.get('access_until') or '').strip())

    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT id FROM users WHERE id = %s AND role = 'admin'", (user_id,))
        if not c.fetchone():
            return jsonify(ok=False, error='not_found'), 404

        c.execute("""
          UPDATE users
             SET full_name = COALESCE(NULLIF(%s,''), full_name),
                 phone     = COALESCE(NULLIF(%s,''), phone),
                 position  = COALESCE(NULLIF(%s,''), position),
                 priority  = COALESCE(NULLIF(%s,''), priority),
                 access_expires_at = COALESCE(%s, access_expires_at)
           WHERE id = %s
        """, (full_name, phone, position, priority, access, user_id))
        conn.commit()

    _log(session.get('user_id'), user_id, 'update', {
        'full_name': full_name, 'phone': phone, 'position': position,
        'priority': priority, 'access_expires_at': (access.isoformat() if access else None)
    }, request.remote_addr)
    return jsonify(ok=True)


@bp.post('/provisioning/delete/<user_id>')
@provisioner_required
def delete_user(user_id):
    with get_conn() as conn, conn.cursor() as c:
        c.execute("UPDATE users SET is_active = FALSE WHERE id = %s", (user_id,))
        if c.rowcount == 0:
            return jsonify(ok=False, error='not_found'), 404
        conn.commit()

    _log(session.get('user_id'), user_id, 'delete', {}, request.remote_addr)
    return jsonify(ok=True)


@bp.get('/provisioning/commission-logs')
@provisioner_required
def commission_logs_page():
    q            = (request.args.get('q') or '').strip()
    action       = (request.args.get('action') or '').strip()
    status_after = _status_to_code(request.args.get('status_after') or '')
    actor        = (request.args.get('actor') or '').strip()
    date_from    = request.args.get('date_from') or ''
    date_to      = request.args.get('date_to') or ''
    page         = max(int(request.args.get('page', 1)), 1)
    per_page     = 20
    offset       = (page - 1) * per_page

    params = []
    where = ["1=1"]

    if q:
        where.append("""
           (cl.app_id ILIKE %s OR
            a.public_no::text ILIKE %s OR
            COALESCE(cl.comment,'') ILIKE %s OR
            COALESCE(u.full_name,'') ILIKE %s OR
            COALESCE(cl.user_agent,'') ILIKE %s OR
            COALESCE(cl.ip_addr,'') ILIKE %s)
        """)
        params += [f"%{q}%"]*6

    if action:
        where.append("cl.action = %s")
        params.append(action)

    if status_after:
        where.append("COALESCE(cl.new_status,'') = %s")
        params.append(status_after)

    if actor:
        where.append("(u.full_name ILIKE %s OR u.email ILIKE %s)")
        params += [f"%{actor}%", f"%{actor}%"]

    if date_from:
        where.append("cl.created_at >= %s")
        params.append(date_from)
    if date_to:
        where.append("cl.created_at < %s::date + INTERVAL '1 day'")
        params.append(date_to)

    where_sql = " AND ".join(where)

    with get_conn() as conn, conn.cursor() as c:
        # данные
        c.execute(f"""
          SELECT cl.created_at, cl.action, cl.old_status, cl.new_status,
                 cl.comment, cl.app_id, a.public_no AS app_no,
                 COALESCE(u.full_name,'') AS actor_name,
                 COALESCE(u.email,'')     AS actor_email,
                 COALESCE(cl.ip_addr,'')  AS ip,
                 COALESCE(cl.user_agent,'') AS ua
          FROM commission_logs cl
          LEFT JOIN users u ON u.id = cl.admin_id
          LEFT JOIN applications a ON a.id = cl.app_id
          WHERE {where_sql}
          ORDER BY cl.created_at DESC
          LIMIT {per_page} OFFSET {offset}
        """, params)
        rows = c.fetchall()

        # total
        c.execute(f"""
            SELECT COUNT(*) AS cnt
            FROM commission_logs cl
            LEFT JOIN users u ON u.id = cl.admin_id
            LEFT JOIN applications a ON a.id = cl.app_id
            WHERE {where_sql}
        """, params)
        rc = c.fetchone()
        total = rc['cnt'] if rc else 0
        pages = max(1, (total + per_page - 1)//per_page)

        # агрегаты
        c.execute(f"""
          SELECT cl.action, COUNT(*) AS cnt
          FROM commission_logs cl
          LEFT JOIN users u ON u.id = cl.admin_id
          LEFT JOIN applications a ON a.id = cl.app_id
          WHERE {where_sql}
          GROUP BY cl.action
        """, params)
        counts_by_action = {r['action']: r['cnt'] for r in c.fetchall()}

    logs = [{
        'created_at': r['created_at'],
        'action': r['action'],
        'old_status': r['old_status'],
        'new_status': r['new_status'],
        'comment': r['comment'],
        'app': {'id': r['app_id'], 'public_no': r['app_no']},
        'actor': {'name': r['actor_name'], 'email': r['actor_email']},
        'ip': r['ip'],
        'ua': r['ua'],
    } for r in rows]

    counts = {
        'total': total,
        'decision':      counts_by_action.get('decision', 0),
        'update_status': counts_by_action.get('update_status', 0),
        'comment':       counts_by_action.get('comment', 0),
        'attach':        counts_by_action.get('attach', 0),
        'view':          counts_by_action.get('view', 0),
    }

    # Возвращаем выбранный фильтр в виде кода ('approved'/'rejected') — так проще отметить <option selected>
    return render_template(
        'provisioning_commission_logs.html',
        logs=logs, counts=counts, page=page, pages=pages,
        filters={
            'q': q, 'action': action, 'status_after': status_after,
            'actor': actor, 'date_from': date_from, 'date_to': date_to
        }
    )


@bp.get('/provisioning/commission-logs.csv')
@provisioner_required
def commission_logs_csv():
    q            = (request.args.get('q') or '').strip()
    action       = (request.args.get('action') or '').strip()
    status_after = _status_to_code(request.args.get('status_after') or '')
    actor        = (request.args.get('actor') or '').strip()
    date_from    = request.args.get('date_from') or ''
    date_to      = request.args.get('date_to') or ''

    params = []
    where = ["1=1"]

    if q:
        where.append("""
           (cl.app_id ILIKE %s OR
            a.public_no::text ILIKE %s OR
            COALESCE(cl.comment,'') ILIKE %s OR
            COALESCE(u.full_name,'') ILIKE %s OR
            COALESCE(cl.user_agent,'') ILIKE %s OR
            COALESCE(cl.ip_addr,'') ILIKE %s)
        """)
        params += [f"%{q}%"]*6
    if action:
        where.append("cl.action = %s"); params.append(action)
    if status_after:
        where.append("COALESCE(cl.new_status,'') = %s"); params.append(status_after)
    if actor:
        where.append("(u.full_name ILIKE %s OR u.email ILIKE %s)")
        params += [f"%{actor}%", f"%{actor}%"]
    if date_from:
        where.append("cl.created_at >= %s"); params.append(date_from)
    if date_to:
        where.append("cl.created_at < %s::date + INTERVAL '1 day'"); params.append(date_to)

    where_sql = " AND ".join(where)

    with get_conn() as conn, conn.cursor() as c:
        c.execute(f"""
          SELECT
            to_char(cl.created_at,'YYYY-MM-DD HH24:MI:SS') AS created_at,
            cl.action,
            COALESCE(cl.old_status,'') AS old_status,
            COALESCE(cl.new_status,'') AS new_status,
            COALESCE(cl.comment,'') AS comment,
            cl.app_id,
            a.public_no AS app_no,
            COALESCE(u.full_name,'') AS actor_name,
            COALESCE(u.email,'')     AS actor_email,
            COALESCE(cl.ip_addr,'')  AS ip,
            COALESCE(cl.user_agent,'') AS user_agent
          FROM commission_logs cl
          LEFT JOIN users u ON u.id = cl.admin_id
          LEFT JOIN applications a ON a.id = cl.app_id
          WHERE {where_sql}
          ORDER BY cl.created_at DESC
        """, params)
        data = c.fetchall()

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["created_at", "action", "old_status", "new_status", "comment",
                "app_id", "app_no", "actor_name", "actor_email", "ip", "user_agent"])
    for r in data:
        w.writerow([r['created_at'], r['action'], r['old_status'], r['new_status'],
                    r['comment'], r['app_id'], r['app_no'], r['actor_name'], r['actor_email'],
                    r['ip'], r['user_agent']])

    return Response(
        buf.getvalue().encode('utf-8-sig'),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=commission-logs.csv'}
    )


# ---------- NDJSON экспорт ----------
@bp.get('/provisioning/commission-logs.ndjson', endpoint='commission_logs_ndjson')
@provisioner_required
def commission_logs_ndjson():
    q            = (request.args.get('q') or '').strip()
    action       = (request.args.get('action') or '').strip()
    status_after = _status_to_code(request.args.get('status_after') or '')
    actor        = (request.args.get('actor') or '').strip()
    date_from    = request.args.get('date_from') or ''
    date_to      = request.args.get('date_to') or ''

    params = []
    where = ["1=1"]

    if q:
        where.append("""
           (cl.app_id ILIKE %s OR
            a.public_no::text ILIKE %s OR
            COALESCE(cl.comment,'') ILIKE %s OR
            COALESCE(u.full_name,'') ILIKE %s OR
            COALESCE(cl.user_agent,'') ILIKE %s OR
            COALESCE(cl.ip_addr,'') ILIKE %s)
        """)
        params += [f"%{q}%"]*6
    if action:
        where.append("cl.action = %s"); params.append(action)
    if status_after:
        where.append("COALESCE(cl.new_status,'') = %s"); params.append(status_after)
    if actor:
        where.append("(u.full_name ILIKE %s OR u.email ILIKE %s)")
        params += [f"%{actor}%", f"%{actor}%"]
    if date_from:
        where.append("cl.created_at >= %s"); params.append(date_from)
    if date_to:
        where.append("cl.created_at < %s::date + INTERVAL '1 day'"); params.append(date_to)

    where_sql = " AND ".join(where)

    sql = f"""
      SELECT
        cl.created_at,
        cl.action,
        COALESCE(cl.old_status,'') AS old_status,
        COALESCE(cl.new_status,'') AS new_status,
        COALESCE(cl.comment,'')    AS comment,
        cl.app_id,
        a.public_no AS app_no,
        COALESCE(u.full_name,'')   AS actor_name,
        COALESCE(u.email,'')       AS actor_email,
        COALESCE(cl.ip_addr,'')    AS ip,
        COALESCE(cl.user_agent,'') AS user_agent,
        cl.meta
      FROM commission_logs cl
      LEFT JOIN users u ON u.id = cl.admin_id
      LEFT JOIN applications a ON a.id = cl.app_id
      WHERE {where_sql}
      ORDER BY cl.created_at DESC
    """

    def generate():
        with get_conn() as conn, conn.cursor() as c:
            c.execute(sql, params)
            for r in c:
                meta = r['meta']
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta or "{}")
                    except Exception:
                        meta = {"_raw": meta}
                out = {
                    "ts": (r['created_at'].isoformat() if getattr(r['created_at'], '__class__', None).__name__ == 'datetime' else r['created_at']),
                    "action": r['action'],
                    "old_status": r['old_status'],
                    "new_status": r['new_status'],
                    "comment": r['comment'],
                    "app": {"id": r['app_id'], "public_no": r['app_no']},
                    "actor": {"name": r['actor_name'], "email": r['actor_email']},
                    "ip": r['ip'],
                    "user_agent": r['user_agent'],
                    "meta": meta,
                    "tags": ["commission", r['action']],
                }
                yield json.dumps(out, ensure_ascii=False) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={'Content-Disposition': 'attachment; filename=commission-logs.ndjson'}
    )
