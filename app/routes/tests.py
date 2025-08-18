# app/routes/tests.py
import sqlite3, json, uuid, datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app
from ..decorators import login_required
from ..db import get_user_by_email
from flask_babel import gettext as _

bp = Blueprint('tests', __name__)

# ---- helpers ---------------------------------------------------------------

def _user_has_approved(user_id: str) -> bool:
    """Есть ли у пользователя одобренная заявка."""
    with sqlite3.connect(current_app.config['DB_PATH']) as conn:
        c = conn.cursor()
        c.execute("""
          SELECT 1
            FROM applications
           WHERE user_id = ?
             AND lower(COALESCE(commission_status,'')) LIKE 'одобр%%'
           LIMIT 1
        """, (user_id,))
        return bool(c.fetchone())

def _require_approved_or_redirect():
    """Проверка допуска к тестам: редиректит на 'Мои заявки', если не одобрено."""
    u = get_user_by_email(session['user_email'])
    if not u:
        return None, redirect(url_for('auth.login'))
    if not _user_has_approved(u['id']):
        flash(('error', _('Доступ к тестам появляется после одобрения вашей заявки.')))
        return None, redirect(url_for('main.applications'))
    return u, None

# ---- список тестов ---------------------------------------------------------

@bp.get('/tests')
@login_required
def tests_select():
    u, resp = _require_approved_or_redirect()
    if resp:  # редирект, если не допущен
        return resp

    with sqlite3.connect(current_app.config['DB_PATH']) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
          SELECT id, title, description, duration_minutes, created_at
            FROM tests
           ORDER BY datetime(created_at) DESC
        """)
        tests = c.fetchall()
    return render_template('tests_select.html', tests=tests)

# ---- старт теста (новый URL) ----------------------------------------------

@bp.route('/tests/start/<test_id>', methods=['GET', 'POST'], endpoint='tests_start')
@login_required
def tests_start(test_id):
    # просто делегируем в общий обработчик
    return take_test(test_id)

# ---- пройти тест (ваш старый URL/endpoint) --------------------------------

@bp.route('/tests/<test_id>', methods=['GET', 'POST'])
@login_required
def take_test(test_id):
    # проверка допуска
    u, resp = _require_approved_or_redirect()
    if resp:
        return resp

    DB = current_app.config['DB_PATH']

    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM tests WHERE id=?", (test_id,))
        t = c.fetchone()
        if not t:
            flash(('error', _('Тест не найден.')))
            return redirect(url_for('tests.tests_select'))

        questions = json.loads(t['questions'] or '[]')

        if request.method == 'POST':
            # собираем ответы (индексы, -1 если не выбрано)
            answers = []
            for i in range(len(questions)):
                v = request.form.get(f'q{i}')
                answers.append(int(v) if v is not None and v.isdigit() else -1)

            # считаем баллы
            score = 0
            for i, q in enumerate(questions):
                try:
                    if answers[i] == int(q.get('correct_index')):
                        score += 1
                except Exception:
                    # если структура вопроса неожиданная — просто пропускаем
                    pass

            # записываем попытку
            c.execute("""
              INSERT INTO test_attempts (id, user_id, test_id, started_at, finished_at, score, answers)
              VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
              str(uuid.uuid4()), u['id'], test_id,
              datetime.datetime.utcnow().isoformat(),
              datetime.datetime.utcnow().isoformat(),
              score, json.dumps(answers, ensure_ascii=False)
            ))
            conn.commit()

            return render_template('test_result.html', test=t, total=len(questions), score=score)

    # GET — отрисовать сам тест
    return render_template('test_take.html', test=t, questions=questions)
