# app/routes/admin.py
import json
import uuid
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify

from ..decorators import admin_required, current_user
from ..email_utils import send_accept_email, send_reject_email
from flask_babel import gettext as _
import sqlite3
from flask import session, abort
from ..db import get_user_by_email

bp = Blueprint('admin', __name__)

@bp.route('/admin', endpoint='admin')
@admin_required
def admin_dashboard():
    DB = current_app.config['DB_PATH']
    u = current_user()
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
          SELECT a.id, u.full_name, u.email, a.commission_status, a.created_at
          FROM applications a
          JOIN users u ON u.id=a.user_id
          ORDER BY a.created_at DESC
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
    DB = current_app.config['DB_PATH']
    status = request.form.get('commission_status')
    comment = request.form.get('commission_comment')

    # ВАЖНО: статусы остаются на русском, т.к. хранятся/сравниваются так же
    if status not in ['Одобрено', 'Отклонено']:
        flash(('error', _('Выберите корректный статус.')))
        return redirect(url_for('admin.admin'))

    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
          UPDATE applications
          SET commission_status=?, commission_comment=?
          WHERE id=?
        """, (status, comment, app_id))
        conn.commit()
        # получим e-mail пользователя
        c.execute("""
          SELECT u.email FROM applications a
          JOIN users u ON u.id=a.user_id
          WHERE a.id=?
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
    DB = current_app.config['DB_PATH']
    u = current_user()
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT
              id,
              title,
              description,
              duration_minutes,
              created_at,
              COALESCE(json_array_length(questions), 0) AS q_count
            FROM tests
            ORDER BY created_at DESC
        """)
        rows = c.fetchall()
        tests = [dict(r) for r in rows]  # чтобы в шаблоне был t['q_count']
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
        # вопросы ожидаем JSON из textarea
        try:
            questions = json.loads(request.form.get('questions') or '[]')
        except Exception:
            flash(('error', _('Некорректный JSON вопросов.')))
            return render_template('admin_test_form.html', form=request.form, mode='new')

        if not title or not questions:
            flash(('error', _('Название и вопросы обязательны.')))
            return render_template('admin_test_form.html', form=request.form, mode='new')

        DB = current_app.config['DB_PATH']
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("""
              INSERT INTO tests (id, title, description, duration_minutes, questions, created_at)
              VALUES (?, ?, ?, ?, ?, ?)
            """, (str(uuid.uuid4()), title, description, duration, json.dumps(questions, ensure_ascii=False),
                  datetime.utcnow().isoformat()))
            conn.commit()
        flash(('success', _('Тест создан.')))
        return redirect(url_for('admin.admin_tests'))
    # GET
    return render_template('admin_test_form.html', form={}, mode='new')

@bp.route('/admin/tests/<test_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_tests_edit(test_id):
    DB = current_app.config['DB_PATH']
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        duration = int(request.form.get('duration_minutes') or 0)
        description = (request.form.get('description') or '').strip()
        try:
            questions = json.loads(request.form.get('questions') or '[]')
        except Exception:
            flash(('error', _('Некорректный JSON вопросов.')))
            return render_template('admin_test_form.html', form=request.form, mode='edit', test_id=test_id)

        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("""
              UPDATE tests SET title=?, description=?, duration_minutes=?, questions=?
              WHERE id=?
            """, (title, description, duration, json.dumps(questions, ensure_ascii=False), test_id))
            conn.commit()
        flash(('success', _('Тест обновлён.')))
        return redirect(url_for('admin.admin_tests'))

    # GET
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM tests WHERE id=?", (test_id,))
        t = c.fetchone()
    if not t:
        flash(('error', _('Тест не найден.')))
        return redirect(url_for('admin.admin_tests'))
    form = {
        'title': t['title'],
        'description': t['description'] or '',
        'duration_minutes': t['duration_minutes'] or 0,
        'questions': t['questions'] or '[]'
    }
    return render_template('admin_test_form.html', form=form, mode='edit', test_id=test_id)

@bp.route('/admin/tests/<test_id>/delete', methods=['POST'])
@admin_required
def admin_tests_delete(test_id):
    DB = current_app.config['DB_PATH']
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM tests WHERE id=?", (test_id,))
        conn.commit()
    flash(('success', _('Тест удалён.')))
    return redirect(url_for('admin.admin_tests'))

@bp.route('/admin/api/tests/<test_id>', endpoint='admin_tests_json')
@admin_required
def admin_tests_json(test_id):
    DB = current_app.config['DB_PATH']
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, title, description, duration_minutes, questions FROM tests WHERE id=?", (test_id,))
        t = c.fetchone()
    if not t:
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    return jsonify({
        'ok': True,
        'id': t['id'],
        'title': t['title'],
        'description': t['description'] or '',
        'duration_minutes': t['duration_minutes'] or 0,
        'questions': t['questions'] or '[]'
    })
@bp.post('/admin/app/<app_id>/decision')
def admin_decide(app_id):
    # доступ только админам
    email = session.get('user_email') or ''
    u = get_user_by_email(email)
    role = dict(u).get('role') if u else None
    if role != 'admin':
        abort(403)

    DB = current_app.config['DB_PATH']
    data = request.get_json(silent=True) or request.form
    status = (data.get('status') or '').lower().strip()
    reason = (data.get('reason') or '').strip()

    if status not in ('approved', 'rejected'):
        return jsonify(ok=False, error='bad_status'), 400
    if status == 'rejected' and not reason:
        return jsonify(ok=False, error='need_reason'), 400

    status_label = _('Одобрено') if status == 'approved' else _('Отклонено')

    # 1) Обновляем статус
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
          UPDATE applications
             SET commission_status = ?, commission_comment = ?
           WHERE id = ?
        """, (status_label, reason if reason else None, app_id))
        conn.commit()

        # 2) Почта и имя пользователя
        c.execute("""
          SELECT u.email, u.full_name, u.id AS user_id
            FROM applications a
            JOIN users u ON u.id = a.user_id
           WHERE a.id = ?
        """, (app_id,))
        dest = c.fetchone()

        # 3) найдём "последний" тест (опционально)
        test_link = None
        if status == 'approved':
            c.execute("""
              SELECT id
                FROM tests
               ORDER BY datetime(created_at) DESC
               LIMIT 1
            """)
            t = c.fetchone()
            if t:
                # прямая ссылка на старт теста
                test_link = url_for('tests.tests_start', test_id=t['id'], _external=True)
            else:
                # общий список тестов
                test_link = url_for('tests.tests', _external=True)

    # 4) Шлём письмо
    if dest and dest['email']:
        from ..email_utils import send_accept_email, send_reject_email
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
    DB = current_app.config['DB_PATH']
    PASS_PCT = int(current_app.config.get('TEST_PASS_THRESHOLD_PCT', 60))

    def _ceil_pct(total, pct):
        # потолок от total*pct/100 без math.ceil
        return (total * pct + 99) // 100

    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
          SELECT a.id AS app_id,
                 a.form_data,
                 a.created_at,
                 a.commission_status,
                 a.commission_comment,
                 u.id AS user_id,
                 u.full_name,
                 u.email
          FROM applications a
          JOIN users u ON u.id = a.user_id
          WHERE a.id=?
        """, (app_id,))
        row = c.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not_found"}), 404

        try:
            form = json.loads(row["form_data"] or "{}")
        except Exception:
            form = {}

        # ---- Последняя попытка теста пользователя (если есть)
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
           WHERE ta.user_id = ?
           ORDER BY datetime(ta.finished_at) DESC
           LIMIT 1
        """, (row["user_id"],))
        att = c.fetchone()

        test_payload = None
        flat = {}  # плоские поля (дубли), если нужно

        if att:
            # total
            try:
                q_list = json.loads(att['questions'] or '[]')
                total = len(q_list)
            except Exception:
                total = None

            # answers (вернём массив)
            try:
                answers = json.loads(att['answers'] or '[]')
            except Exception:
                answers = None

            score = att['score']
            percent = None
            if score is not None and total:
                try:
                    percent = round(score * 100 / total)
                except Exception:
                    percent = None

            # время в секундах
            time_spent = None
            try:
                st = datetime.fromisoformat(att['started_at']) if att['started_at'] else None
                fn = datetime.fromisoformat(att['finished_at']) if att['finished_at'] else None
                if st and fn:
                    time_spent = int((fn - st).total_seconds())
            except Exception:
                time_spent = None

            # порог и passed (настраиваемый процент)
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

            # дубли плоскими (если на фронте так удобнее)
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
            "created_at": row["created_at"],
            "status": row["commission_status"],
            "comment": row["commission_comment"],
            "form": form,
            "test": test_payload,
            **flat
        })
