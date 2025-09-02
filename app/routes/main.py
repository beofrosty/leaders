# app/routes/main.py
import re
import uuid
import json
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, current_app, jsonify, abort
)
from ..decorators import login_required
from ..db import get_user_by_email, get_conn
from flask_babel import gettext as _
import psycopg
from psycopg.errors import UniqueViolation

bp = Blueprint('main', __name__)

@bp.route('/')
def index():
    # Требуем авторизацию
    if 'user_email' not in session:
        return redirect(url_for('auth.login'))

    u = get_user_by_email(session['user_email'])
    if not u:
        session.clear()
        return redirect(url_for('auth.login'))

    # === NEW: админ — сразу в админку ===
    if (u.get('role') == 'admin'):
        return redirect(url_for('admin.admin'))

    with get_conn() as conn, conn.cursor() as c:
        # Все заявки пользователя (последние сверху)
        c.execute("""
          SELECT id, commission_status, commission_comment, created_at
            FROM applications
           WHERE user_id = %s
           ORDER BY created_at DESC NULLS LAST
        """, (u['id'],))
        apps = c.fetchall()

    # Флаг наличия заявки
    already_submitted = len(apps) > 0
    # Одноразовая плашка после отправки (?submitted=1)
    just_submitted = (request.args.get('submitted') == '1')
    # Для UI скрываем модалку/кнопку всегда, если есть хотя бы одна заявка
    submitted = already_submitted

    return render_template(
        'form.html',
        submitted=submitted,
        just_submitted=just_submitted,
        applications=apps,
        already_submitted=already_submitted,
        error=None
    )


@bp.route('/form', methods=['GET', 'POST'])
@login_required
def form():
    u = get_user_by_email(session['user_email'])
    if not u:
        flash(('error', _('Пользователь не найден.')))
        return redirect(url_for('auth.login'))

    # GET — возвращаем на главную (или в админку для админа)
    if request.method == 'GET':
        if (u.get('role') == 'admin'):
            return redirect(url_for('admin.admin'))
        return redirect(url_for('main.index'))

    # POST — создание заявки
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT COUNT(*) AS cnt FROM applications WHERE user_id = %s", (u['id'],))
        already_submitted = (c.fetchone()['cnt'] or 0) > 0

    if already_submitted:
        flash(('error', _('Вы уже отправили заявку. Повторная подача невозможна.')))
        return redirect(url_for('main.applications'))

    data = request.form.to_dict()
    try:
        with get_conn() as conn, conn.cursor() as c:
            app_id = str(uuid.uuid4())
            c.execute("""
                INSERT INTO applications (
                    id, user_id, form_data, commission_comment, commission_status,
                    test_score, test_answers, created_at, test_link
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                app_id,
                u['id'],
                json.dumps(data, ensure_ascii=False),
                None,  # commission_comment
                None,  # commission_status
                None,  # test_score
                None,  # test_answers
                datetime.now(timezone.utc),
                None,  # test_link
            ))
            conn.commit()
        # PRG: на главную с флагом одноразовой плашки
        return redirect(url_for('main.index', submitted=1))
    except UniqueViolation:
        flash(('error', _('Заявка уже существует. Повторная подача невозможна.')))
        return redirect(url_for('main.applications'))
    except psycopg.Error:
        flash(('error', _('Ошибка базы данных. Попробуйте позже.')))
        return redirect(url_for('main.applications'))


@bp.route('/profile/<user_id>')
@login_required
def profile(user_id):
    with get_conn() as conn, conn.cursor() as c:
        # сам пользователь
        c.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = c.fetchone()

        # последняя заявка пользователя
        c.execute("""
          SELECT id, form_data, test_answers, commission_status, commission_comment, created_at
            FROM applications
           WHERE user_id = %s
           ORDER BY created_at DESC NULLS LAST
           LIMIT 1
        """, (user_id,))
        last_app = c.fetchone()

    # данные формы и ответы теста из applications (а не из users)
    form_data = json.loads(last_app['form_data']) if last_app and last_app['form_data'] else {}
    test_answers = json.loads(last_app['test_answers']) if last_app and last_app['test_answers'] else {}

    labels = {
        "full_name": _("ФИО"),
        "birth_date": _("Дата рождения"),
        "position": _("Занимаемая должность"),
        "contacts": _("Контактные связи"),
        "email": _("Email"),
        "address": _("Адрес проживания"),
        "education_institution": _("Учебное заведение"),
        "specialization": _("Специальность/направление"),
        "graduation_year": _("Год окончания"),
        "leadership_experience": _("Опыт лидерства"),
        "leader_skills": _("Навыки и качества лидера"),
        "personal_achievements": _("Личные достижения"),
        "motivation": _("Как вы мотивируете команду"),
        "decision_case": _("Сложное решение в условиях неопределенности"),
        "conflict_resolution": _("Как справляетесь с конфликтами"),
        "goals": _("Цели на 3-5 лет"),
        "contest_benefit": _("Как конкурс поможет достичь целей"),
        "leader_definition": _("Что значит быть лидером"),
        "reason": _("Почему решили участвовать"),
        "similar_experience": _("Опыт участия в конкурсах"),
    }

    return render_template(
        "profile.html",
        user=user,
        form_data=form_data,
        test_answers=test_answers,
        labels=labels,
        latest_status=(last_app['commission_status'] if last_app else None),
        latest_comment=(last_app['commission_comment'] if last_app else None),
    )


@bp.route('/profile', endpoint='profile_me')
@login_required
def profile_me():
    return redirect(url_for('.profile', user_id=session['user_id']))


@bp.route('/applications')
@login_required
def applications():
    u = get_user_by_email(session['user_email'])

    PASS_PCT = int(current_app.config.get('TEST_PASS_THRESHOLD_PCT', 60))
    def ceil_pct(total, pct): return (total * pct + 99) // 100

    with get_conn() as conn, conn.cursor() as c:
        # все заявки пользователя
        c.execute("""
          SELECT a.id,
                 u.full_name,
                 u.email,
                 a.commission_status,
                 a.commission_comment,
                 a.created_at,
                 a.test_link
            FROM applications a
            JOIN users u ON u.id = a.user_id
           WHERE a.user_id = %s
           ORDER BY a.created_at DESC NULLS LAST
        """, (u['id'],))
        items = c.fetchall()

        latest = items[0] if items else None

        # последняя попытка и проверка "пройдено"
        c.execute("""
          SELECT ta.score, t.questions
            FROM test_attempts ta
            JOIN tests t ON t.id = ta.test_id
           WHERE ta.user_id = %s
           ORDER BY ta.finished_at DESC NULLS LAST
           LIMIT 1
        """, (u['id'],))
        att = c.fetchone()

    test_passed = False
    if att:
        try:
            total = len(json.loads(att['questions'] or '[]'))
        except Exception:
            total = 0
        min_score = ceil_pct(total, PASS_PCT) if total else None
        test_passed = bool(att['score'] is not None and total and att['score'] >= min_score)

    return render_template(
        'applications.html',
        items=items,
        test_passed=test_passed,
        latest_app_id=(latest['id'] if latest else None),
        test_link=(latest['test_link'] if latest and 'test_link' in latest.keys() else None),
    )


@bp.get('/api/my-application')
@login_required
def api_my_application():
    """Возвращает последний статус заявки текущего пользователя."""
    u = get_user_by_email(session['user_email'])
    if not u:
        return jsonify(exists=False), 404

    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
          SELECT a.id,
                 a.public_no,                -- ← ДОБАВИЛИ
                 u.full_name,
                 u.email,
                 a.commission_status,
                 a.commission_comment,
                 a.created_at,
                 a.test_link
            FROM applications a
            JOIN users u ON u.id = a.user_id
           WHERE a.user_id = %s
           ORDER BY a.created_at DESC NULLS LAST
        """, (u['id'],))
        row = c.fetchone()

    if not row:
        return jsonify(exists=False)

    return jsonify(
        exists=True,
        id=row['id'],
        status=row['commission_status'],
        comment=row['commission_comment'],
        created_at=row['created_at'].isoformat() if row['created_at'] else None,
    )


@bp.post('/applications/<string:app_id>/test_link')
@login_required
def save_test_link(app_id):
    # 1) получаем ссылку
    link = (request.form.get('test_link') or '').strip()

    # 2) простая валидация URL
    if link and not re.match(r'^https?://', link, re.IGNORECASE):
        return jsonify(ok=False, error='bad_url'), 400

    # 3) текущий пользователь
    u = get_user_by_email(session['user_email'])
    if not u:
        return jsonify(ok=False, error='unauth'), 401

    # 4) обновляем в БД, проверяя владельца заявки
    with get_conn() as conn, conn.cursor() as c:
        app_row = c.execute(
            "SELECT id, user_id FROM applications WHERE id = %s",
            (app_id,)
        ).fetchone()
        if not app_row:
            abort(404)
        if app_row['user_id'] != u['id']:
            abort(403)

        c.execute("UPDATE applications SET test_link = %s WHERE id = %s", (link, app_id))
        conn.commit()

    # 5) ответ (AJAX или обычный POST)
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify(ok=True, test_link=link)
    return redirect(url_for('main.applications'))
