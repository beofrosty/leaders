# app/routes/admin.py
import json
import uuid
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify, session, abort
from flask_babel import gettext as _

from ..decorators import admin_required, current_user
from ..email_utils import send_accept_email, send_reject_email
from ..db import get_user_by_email, get_conn

from psycopg.types.json import Json  # адаптер для JSON

bp = Blueprint('admin', __name__)

def _normalize_status(value: str):
    """Приводим любые варианты к ('Одобрено' | 'Отклонено') либо None."""
    if not value:
        return None
    s = value.strip().lower()
    approved = {'одобрено', 'approve', 'approved', 'ok', 'accept', 'accepted', 'да', 'true', '1'}
    rejected = {'отклонено', 'reject', 'rejected', 'deny', 'denied', 'cancel', 'нет', 'false', '0'}
    if s in approved:
        return 'Одобрено'
    if s in rejected:
        return 'Отклонено'
    return None


@bp.route('/admin', endpoint='admin')
@admin_required
def admin_dashboard():
    u = current_user()
    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
          SELECT a.id, u.full_name, u.email, a.commission_status, a.created_at
            FROM applications a
            JOIN users u ON u.id = a.user_id
           ORDER BY a.created_at DESC NULLS LAST
        """)
        items = c.fetchall()
    has_tests = ('admin_tests' in current_app.view_functions)
    return render_template(
        'admin_dashboard.html',
        user=u,
        items=items,
        active='apps',
        page_title=_('Заявки'),
        has_tests=has_tests
    )


@bp.route('/admin/app/<app_id>/update_status', methods=['POST'])
@admin_required
def admin_update_app_status(app_id):
    status = request.form.get('commission_status')
    comment = request.form.get('commission_comment')

    # статусы остаются на русском
    if status not in ['Одобрено', 'Отклонено']:
        flash(('error', _('Выберите корректный статус.')))
        return redirect(url_for('admin.admin'))

    with get_conn() as conn, conn.cursor() as c:
        # блок повторного решения
        c.execute("SELECT commission_status FROM applications WHERE id = %s", (app_id,))
        row = c.fetchone()
        if not row:
            flash(('error', _('Заявка не найдена.')))
            return redirect(url_for('admin.admin'))
        if row['commission_status']:
            flash(('error', _('Решение уже принято: %(s)s', s=row['commission_status'])))
            return redirect(url_for('admin.admin'))

        # обновляем
        c.execute("""
          UPDATE applications
             SET commission_status = %s, commission_comment = %s
           WHERE id = %s
        """, (status, comment, app_id))
        conn.commit()

        # e-mail пользователя
        c.execute("""
          SELECT u.email
            FROM applications a
            JOIN users u ON u.id = a.user_id
           WHERE a.id = %s
        """, (app_id,))
        row = c.fetchone()

    if row and row['email']:
        if status == 'Одобрено':
            send_accept_email(row['email'], 'https://example.com/test_link')
        else:
            send_reject_email(row['email'], comment or _('Причина не указана'))

    flash(('success', _('Статус обновлён и письмо отправлено.')))
    return redirect(url_for('admin.admin'))


@bp.route('/admin/tests', endpoint='admin_tests')
@admin_required
def admin_tests():
    u = current_user()
    with get_conn() as conn, conn.cursor() as c:
        # q_count считаем по JSONB; если поле TEXT — приводим через ::jsonb
        c.execute("""
            SELECT
              id,
              title,
              description,
              duration_minutes,
              created_at,
              COALESCE(is_published, FALSE) AS is_published,
              COALESCE(
                jsonb_array_length(
                  CASE
                    WHEN jsonb_typeof(questions::jsonb) = 'array' THEN questions::jsonb
                    ELSE '[]'::jsonb
                  END
                ), 0
              ) AS q_count
            FROM tests
            ORDER BY created_at DESC NULLS LAST
        """)
        rows = c.fetchall()
        tests = [dict(r) for r in rows]
    return render_template(
        'admin_tests.html',
        user=u,
        tests=tests,
        page_title=_('Тесты'),
        active='tests'
    )


@bp.route('/admin/tests/new', methods=['GET', 'POST'])
@admin_required
def admin_tests_new():
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        duration = int(request.form.get('duration_minutes') or 0)
        description = (request.form.get('description') or '').strip()
        try:
            questions = json.loads(request.form.get('questions') or '[]')
        except Exception:
            flash(('error', _('Некорректный JSON вопросов.')))
            return render_template('admin_test_form.html', form=request.form, mode='new')

        if not title or not questions:
            flash(('error', _('Название и вопросы обязательны.')))
            return render_template('admin_test_form.html', form=request.form, mode='new')

        with get_conn() as conn, conn.cursor() as c:
            c.execute("""
              INSERT INTO tests (id, title, description, duration_minutes, questions, created_at, is_published)
              VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                str(uuid.uuid4()),
                title,
                description,
                duration,
                Json(questions),  # пусть хранится jsonb
                datetime.now(timezone.utc),
                False
            ))
            conn.commit()
        flash(('success', _('Тест создан.')))
        return redirect(url_for('admin.admin_tests'))
    # GET
    return render_template('admin_test_form.html', form={}, mode='new')


@bp.route('/admin/tests/<test_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_tests_edit(test_id):
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        duration = int(request.form.get('duration_minutes') or 0)
        description = (request.form.get('description') or '').strip()
        try:
            questions = json.loads(request.form.get('questions') or '[]')
        except Exception:
            flash(('error', _('Некорректный JSON вопросов.')))
            return render_template('admin_test_form.html', form=request.form, mode='edit', test_id=test_id)

        with get_conn() as conn, conn.cursor() as c:
            c.execute("""
              UPDATE tests
                 SET title = %s, description = %s, duration_minutes = %s, questions = %s
               WHERE id = %s
            """, (title, description, duration, Json(questions), test_id))
            conn.commit()
        flash(('success', _('Тест обновлён.')))
        return redirect(url_for('admin.admin_tests'))

    # GET
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT * FROM tests WHERE id = %s", (test_id,))
        t = c.fetchone()
    if not t:
        flash(('error', _('Тест не найден.')))
        return redirect(url_for('admin.admin_tests'))

    # questions может прийти как list/dict (jsonb) — возвращаем строкой для textarea
    q_raw = t['questions']
    q_str = json.dumps(q_raw, ensure_ascii=False) if not isinstance(q_raw, str) else (q_raw or '[]')

    form = {
        'title': t['title'],
        'description': t['description'] or '',
        'duration_minutes': t['duration_minutes'] or 0,
        'questions': q_str
    }
    return render_template('admin_test_form.html', form=form, mode='edit', test_id=test_id)


@bp.route('/admin/tests/<test_id>/delete', methods=['POST'])
@admin_required
def admin_tests_delete(test_id):
    with get_conn() as conn, conn.cursor() as c:
        c.execute("DELETE FROM tests WHERE id = %s", (test_id,))
        conn.commit()
    flash(('success', _('Тест удалён.')))
    return redirect(url_for('admin.admin_tests'))


@bp.route('/admin/api/tests/<test_id>', endpoint='admin_tests_json')
@admin_required
def admin_tests_json(test_id):
    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
            SELECT id, title, description, duration_minutes, questions
              FROM tests
             WHERE id = %s
        """, (test_id,))
        t = c.fetchone()
    if not t:
        return jsonify({'ok': False, 'error': 'not_found'}), 404

    q = t['questions']
    q_str = json.dumps(q, ensure_ascii=False) if not isinstance(q, str) else (q or '[]')

    return jsonify({
        'ok': True,
        'id': t['id'],
        'title': t['title'],
        'description': t['description'] or '',
        'duration_minutes': t['duration_minutes'] or 0,
        'questions': q_str
    })


@bp.post('/admin/app/<app_id>/decision')
def admin_decide(app_id):
    # доступ только админам
    email = session.get('user_email') or ''
    u = get_user_by_email(email)
    role = dict(u).get('role') if u else None
    if role != 'admin':
        abort(403)

    data = request.get_json(silent=True) or request.form
    status = (data.get('status') or '').lower().strip()
    reason = (data.get('reason') or '').strip()

    if status not in ('approved', 'rejected'):
        return jsonify(ok=False, error='bad_status'), 400
    if status == 'rejected' and not reason:
        return jsonify(ok=False, error='need_reason'), 400

    status_label = _('Одобрено') if status == 'approved' else _('Отклонено')

    with get_conn() as conn, conn.cursor() as c:
        # проверяем, не принято ли уже решение
        c.execute("SELECT commission_status FROM applications WHERE id = %s", (app_id,))
        row = c.fetchone()
        if not row:
            return jsonify(ok=False, error='not_found'), 404
        if row['commission_status']:
            return jsonify(ok=False, error='already_decided', status=row['commission_status']), 409

        # 1) Обновляем статус
        c.execute("""
          UPDATE applications
             SET commission_status = %s, commission_comment = %s
           WHERE id = %s
        """, (status_label, (reason or None), app_id))
        conn.commit()

        # 2) Почта и имя пользователя
        c.execute("""
          SELECT u.email, u.full_name, u.id AS user_id
            FROM applications a
            JOIN users u ON u.id = a.user_id
           WHERE a.id = %s
        """, (app_id,))
        dest = c.fetchone()

        # 3) найдём "последний" тест (опционально)
        test_link = None
        if status == 'approved':
            c.execute("""
              SELECT id
                FROM tests
               ORDER BY created_at DESC NULLS LAST
               LIMIT 1
            """)
            t = c.fetchone()
            test_link = (
                url_for('tests.tests_start', test_id=t['id'], _external=True)
                if t else url_for('tests.tests', _external=True)
            )

    # 4) Шлём письмо
    if dest and dest['email']:
        if status == 'approved':
            send_accept_email(dest['email'],
                              full_name=dest['full_name'],
                              test_url=test_link)
        else:
            send_reject_email(dest['email'],
                              reason=reason,
                              full_name=dest['full_name'])

    return jsonify(ok=True, status=status_label, comment=reason or '')


@bp.route('/admin/api/app/<app_id>')
@admin_required
def admin_app_json(app_id):
    PASS_PCT = int(current_app.config.get('TEST_PASS_THRESHOLD_PCT', 60))

    def _ceil_pct(total, pct):
        return (total * pct + 99) // 100

    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
          SELECT a.id AS app_id,
                 a.form_data,
                 a.created_at,
                 a.commission_status,
                 a.commission_comment,
                 a.test_link,
                 u.id AS user_id,
                 u.full_name,
                 u.email
            FROM applications a
            JOIN users u ON u.id = a.user_id
           WHERE a.id = %s
        """, (app_id,))
        row = c.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not_found"}), 404

        # form_data может прийти как jsonb (dict) — нормализуем в dict
        form = {}
        try:
            if isinstance(row["form_data"], (dict, list)):
                form = row["form_data"]
            elif row["form_data"]:
                form = json.loads(row["form_data"])
        except Exception:
            form = {}

        # последняя попытка теста пользователя
        c.execute("""
          SELECT ta.test_id,
                 ta.score,
                 ta.started_at,
                 ta.finished_at,
                 ta.answers,
                 t.title,
                 t.questions,
                 t.duration_minutes
            FROM test_attempts ta
            JOIN tests t ON t.id = ta.test_id
           WHERE ta.user_id = %s
        ORDER BY ta.finished_at DESC NULLS LAST
           LIMIT 1
        """, (row["user_id"],))
        att = c.fetchone()

    test_payload = None
    flat = {}

    if att:
        # total
        try:
            q_list = att['questions']
            if isinstance(q_list, str):
                q_list = json.loads(q_list or '[]')
            total = len(q_list or [])
        except Exception:
            total = None

        # answers
        answers = att['answers']
        if isinstance(answers, str):
            try:
                answers = json.loads(answers or '[]')
            except Exception:
                answers = None

        score = att['score']
        percent = None
        if score is not None and total:
            try:
                percent = round(score * 100 / total)
            except Exception:
                percent = None

        # время в секундах (TIMESTAMPTZ → datetime)
        time_spent = None
        try:
            st = att['started_at']
            fn = att['finished_at']
            if st and fn:
                time_spent = int((fn - st).total_seconds())
        except Exception:
            time_spent = None

        min_score = None
        passed = None
        if total:
            min_score = _ceil_pct(total, PASS_PCT)
            passed = (score is not None and score >= min_score)

        test_payload = {
            "id": att["test_id"],
            "title": att["title"],
            "score": score,
            "total": total,
            "percent": percent,
            "time_spent": time_spent,
            "answers": answers,
            "min_score": min_score,
            "passed": passed,
        }

        flat = {
            "test_id": att["test_id"],
            "test_title": att["title"],
            "test_score": score,
            "test_total": total,
            "test_percent": percent,
            "test_time_spent": time_spent,
            "test_answers": answers,
            "test_min_score": min_score,
            "test_passed": passed,
        }

    return jsonify({
        "ok": True,
        "id": row["app_id"],
        "user_id": row["user_id"],
        "full_name": row["full_name"],
        "email": row["email"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "status": row["commission_status"],
        "comment": row["commission_comment"],
        "form": form,
        "test": test_payload,
        "test_link": row["test_link"],
        **flat
    })


@bp.post('/admin/tests/<test_id>/publish', endpoint='admin_tests_publish')
@admin_required
def admin_tests_publish(test_id):
    state = True if request.form.get('state') == '1' else False
    with get_conn() as conn, conn.cursor() as c:
        c.execute('UPDATE tests SET is_published = %s WHERE id = %s', (state, test_id))
        conn.commit()
    flash(_('Статус теста обновлён'), 'success')
    return redirect(url_for('admin.admin_tests'))
