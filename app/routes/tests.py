# app/routes/tests.py
import sqlite3, json, uuid, datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app
from ..decorators import login_required
from ..db import get_user_by_email
from flask_babel import gettext as _

bp = Blueprint('tests', __name__)

# ---- helpers ---------------------------------------------------------------

def _user_has_approved(user_id: str) -> bool:
    """Есть ли у пользователя одобренная заявка (статус начинается с 'одобр'). LOWER() в SQLite не работает по-кириллице, поэтому проверяем в Python."""
    DB = current_app.config['DB_PATH']
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
          SELECT commission_status
            FROM applications
           WHERE user_id = ?
           ORDER BY datetime(created_at) DESC
           LIMIT 1
        """, (user_id,))
        row = c.fetchone()
        if not row:
            return False
        s = (row['commission_status'] or '').strip()
        return s.lower().startswith('одобр')

def _require_approved_or_redirect():
    u = get_user_by_email(session.get('user_email') or '')
    if not u:
        return None, redirect(url_for('auth.login'))
    if not _user_has_approved(u['id']):
        flash(('error', _('Доступ к тестам появляется после одобрения вашей заявки.')))
        return None, redirect(url_for('main.applications', need_approval=1))
    return u, None


# ---- список тестов ---------------------------------------------------------

@bp.get('/tests', endpoint='tests')
@login_required
def tests_select():
    u, resp = _require_approved_or_redirect()
    if resp:
        return resp

    DB = current_app.config['DB_PATH']
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
          SELECT id, title, description, duration_minutes, created_at
            FROM tests
           ORDER BY datetime(created_at) DESC
        """)
        tests = c.fetchall()

    if not tests:
        flash(('error', _('Пока нет доступных тестов.')))
        return redirect(url_for('main.applications'))

    # Если тест один — сразу запускаем его
    if len(tests) == 1:
        return redirect(url_for('tests.take_test', test_id=tests[0]['id']))

    return render_template('tests_select.html', tests=tests)

# ---- альтернативный старт (удобно для ссылок) ------------------------------

@bp.route('/tests/start/<test_id>', methods=['GET', 'POST'], endpoint='tests_start')
@login_required
def tests_start(test_id):
    return take_test(test_id)

# ---- пройти тест -----------------------------------------------------------

@bp.route('/tests/<test_id>', methods=['GET', 'POST'])
@login_required
def take_test(test_id):
    u, resp = _require_approved_or_redirect()
    if resp:
        return resp

    DB = current_app.config['DB_PATH']
    end_key = f'test_end_{test_id}'
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # 1) Загружаем тест
        c.execute("SELECT * FROM tests WHERE id=?", (test_id,))
        t = c.fetchone()
        if not t:
            flash(('error', _('Тест не найден.')))
            return redirect(url_for('tests.tests'))

        # 2) Считываем вопросы и total сразу (чтобы были доступны при ранних return)
        try:
            questions = json.loads(t['questions'] or '[]')
        except Exception:
            questions = []
        total = len(questions)

        # 3) Запрет повторного прохождения (блокируем и GET, и POST)
        c.execute("""
          SELECT id, score, started_at, finished_at
            FROM test_attempts
           WHERE user_id=? AND test_id=?
           ORDER BY datetime(finished_at) DESC
           LIMIT 1
        """, (u['id'], test_id))
        last = c.fetchone()
        if last:
            if request.method == 'GET':
                flash(('info', _('Вы уже проходили этот тест. Результат: %(score)s', score=last['score'])))
            else:
                flash(('error', _('Повторное прохождение запрещено. Ваш предыдущий результат: %(score)s', score=last['score'])))
            return render_template('test_result.html', test=t, total=total, score=last['score'])

        # 4) Обработка отправки ответов
        if request.method == 'POST':
            # Серверная проверка таймера: берём дедлайн из сессии
            ends_at_str = session.get(end_key)
            if ends_at_str:
                try:
                    ends_at = datetime.datetime.fromisoformat(ends_at_str)
                except Exception:
                    ends_at = None
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                # Нормализуем ends_at к aware-формату, если вдруг был naive
                if ends_at and ends_at.tzinfo is None:
                    ends_at = ends_at.replace(tzinfo=datetime.timezone.utc)
                if ends_at and now_utc > ends_at:
                    session.pop(end_key, None)
                    flash(('error', _('Время вышло')))
                    return redirect(url_for('tests.tests'))

            # Валидация: ответы на все вопросы обязательны
            answers = []
            for i in range(total):
                v = request.form.get(f'q{i}')
                if v is None:
                    flash(('error', _('Пожалуйста, ответьте на все вопросы.')))
                    # Вернём форму с тем же дедлайном
                    return render_template('test_take.html', test=t, questions=questions, ends_at=session.get(end_key))
                answers.append(int(v) if isinstance(v, str) and v.isdigit() else -1)

            # Подсчёт баллов
            score = 0
            for i, q in enumerate(questions):
                try:
                    correct_idx = int(q.get('correct_index'))
                except (TypeError, ValueError, AttributeError):
                    correct_idx = None
                if correct_idx is not None and answers[i] == correct_idx:
                    score += 1

            # Запись попытки
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

            # Очистим дедлайн из сессии
            session.pop(end_key, None)

            return render_template('test_result.html', test=t, total=total, score=score)

    # 5) Показ формы (GET) + установка серверного дедлайна в сессию
    ends_at = None
    try:
        minutes = int(t['duration_minutes'] or 0)
    except Exception:
        minutes = 0
    if minutes > 0:
        ends_at_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
        ends_at = ends_at_dt.isoformat()
        session[end_key] = ends_at  # серверная «истина»

    return render_template('test_take.html', test=t, questions=questions, ends_at=ends_at)
