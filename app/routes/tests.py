# app/routes/tests.py
import json, uuid, datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, abort
from ..decorators import login_required
from ..db import get_user_by_email, get_conn
from flask_babel import gettext as _

bp = Blueprint('tests', __name__)


# ---------------------------- helpers --------------------------------------- #

def _user_has_approved(user_id: str) -> bool:
    """
    Есть ли у пользователя одобренная заявка (статус начинается с 'одобр').
    Проверяем в Python — надёжно для кириллицы.
    """
    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
          SELECT commission_status
            FROM applications
           WHERE user_id = %s
           ORDER BY created_at DESC NULLS LAST
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


def _parse_options(raw_opts):
    """
    Приводим варианты к списку строк:
    - если строка — режем по переводам строк/точке с запятой
    - если список — приводим элементы к строкам и убираем пустые
    """
    opts = raw_opts or []
    if isinstance(opts, str):
        opts = opts.replace('\r', '').replace(';', '\n').split('\n')
        opts = [s.strip() for s in opts if s.strip()]
    else:
        opts = [str(s).strip() for s in opts if str(s).strip()]
    return opts


def _is_multiple(q: dict) -> bool:
    qtype = str(q.get('type', '')).lower()
    return bool(q.get('multiple')) or qtype in ('multi', 'multiple', 'checkbox', 'checkboxes', 'multi_select')


def _parse_correct_indices(q: dict):
    """
    Поддерживаем несколько вариантов задания правильного ответа:
    - correct: 1                   -> [1]
    - correct: [1,3]               -> [1,3]
    - correct_index: 2             -> [2]
    - correct_indexes: [0,2]       -> [0,2]
    - correct: "1,3" / "1;3"       -> [1,3]
    """
    cand = q.get('correct', None)
    if cand is None:
        cand = q.get('correct_index', None)
    if cand is None:
        cand = q.get('correct_indexes', None)

    def _str_to_list(s: str):
        s = s.replace(';', ',').replace(' ', ',')
        parts = [p for p in s.split(',') if p != '']
        return [int(p) for p in parts]

    indices = []
    if isinstance(cand, list):
        try:
            indices = [int(x) for x in cand]
        except Exception:
            indices = []
    elif isinstance(cand, str):
        try:
            indices = _str_to_list(cand)
        except Exception:
            indices = []
    elif cand is not None:
        try:
            indices = [int(cand)]
        except Exception:
            indices = []

    return sorted(set(indices))


def _normalize_questions(raw):
    """
    Не теряем множественный/одиночный тип.
    multiple := true, если:
      - multiple: true, ИЛИ
      - type in {multi, multiple, checkbox, checkboxes}, ИЛИ
      - correct — это список индексов (строка '0,2' тоже поддерживается).
    """
    norm = []
    for q in (raw or []):
        text = (q.get('text') or q.get('question') or q.get('q') or '').strip()

        opts = q.get('options') or q.get('answers') or q.get('choices') or []
        if isinstance(opts, str):
            opts = [s.strip() for s in opts.replace('\r', '').replace(';', '\n').split('\n') if s.strip()]
        else:
            opts = [str(s).strip() for s in opts if str(s).strip()]

        t = str(q.get('type') or '').lower()
        corr_raw = q.get('correct')

        if isinstance(corr_raw, str):
            corr = [int(x) for x in corr_raw.replace(';', ',').split(',') if x.strip().isdigit()]
        elif isinstance(corr_raw, list):
            corr = [int(x) for x in corr_raw if str(x).isdigit()]
        else:
            corr = []

        multiple = bool(q.get('multiple')) or (t in ('multi', 'multiple', 'checkbox', 'checkboxes')) or (len(corr) > 0)

        if multiple:
            correct_index = None
        else:
            try:
                correct_index = int(q.get('correct_index'))
            except Exception:
                correct_index = None
            corr = None

        norm.append({
            'text': text,
            'options': opts,
            'multiple': multiple,
            'type': q.get('type') or ('multiple' if multiple else 'single'),
            'correct_index': correct_index,  # одиночный
            'correct': corr,                 # множественный
        })
    return norm


# -------------------------- список тестов ----------------------------------- #

@bp.get('/tests', endpoint='tests')
@login_required
def tests_select():
    u, resp = _require_approved_or_redirect()
    if resp:
        return resp

    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
          SELECT id, title, description, duration_minutes, created_at
            FROM tests
           WHERE COALESCE(is_published, FALSE) = TRUE
           ORDER BY created_at DESC NULLS LAST
        """)
        tests = c.fetchall()

    if not tests:
        flash(('error', _('Пока нет доступных тестов.')))
        return redirect(url_for('main.applications'))

    # Если тест один — сразу запускаем его
    if len(tests) == 1:
        return redirect(url_for('tests.take_test', test_id=tests[0]['id']))

    return render_template('tests_select.html', tests=tests)


# -------------- альтернативный старт (удобно для прямых ссылок) ------------- #

@bp.route('/tests/start/<test_id>', methods=['GET', 'POST'], endpoint='tests_start')
@login_required
def tests_start(test_id):
    return take_test(test_id)


# ---------------------------- пройти тест ----------------------------------- #

@bp.route('/tests/<test_id>', methods=['GET', 'POST'])
@login_required
def take_test(test_id):
    u, resp = _require_approved_or_redirect()
    if resp:
        return resp

    end_key = f'test_end_{test_id}'

    with get_conn() as conn, conn.cursor() as c:
        # 1) Загружаем тест
        c.execute("SELECT * FROM tests WHERE id = %s", (test_id,))
        t = c.fetchone()
        if not t:
            flash(('error', _('Тест не найден.')))
            return redirect(url_for('tests.tests'))

        # проверка публикации с допуском админа
        published = bool(t.get('is_published'))
        is_admin = str((u.get('role') if isinstance(u, dict) else u['role']) or '').lower() == 'admin'
        if not published and not is_admin:
            abort(404)

        # 2) Вопросы (унифицированные) + total
        try:
            questions = _normalize_questions(json.loads(t.get('questions') or '[]'))
        except Exception:
            questions = []
        total = len(questions)

        # 3) Запрет повторного прохождения (блокируем и GET, и POST)
        c.execute("""
          SELECT id, score, started_at, finished_at
            FROM test_attempts
           WHERE user_id = %s AND test_id = %s
           ORDER BY finished_at DESC NULLS LAST
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
            # Серверная проверка таймера по сессии
            ends_at_str = session.get(end_key)
            if ends_at_str:
                try:
                    ends_at = datetime.datetime.fromisoformat(ends_at_str)
                except Exception:
                    ends_at = None
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if ends_at and ends_at.tzinfo is None:
                    ends_at = ends_at.replace(tzinfo=datetime.timezone.utc)
                if ends_at and now_utc > ends_at:
                    session.pop(end_key, None)
                    flash(('error', _('Время вышло')))
                    return redirect(url_for('tests.tests'))

            # ----- Валидация и сбор ответов -----
            answers = []
            for i in range(total):
                if questions[i].get('multiple'):
                    vals = request.form.getlist(f'q{i}')  # список строк
                    if not vals:
                        flash(('error', _('Пожалуйста, ответьте на все вопросы.')))
                        return render_template('test_take.html', test=t, questions=questions,
                                               ends_at=session.get(end_key))
                    arr = sorted(set(int(v) for v in vals if str(v).isdigit()))
                    answers.append(arr)
                else:
                    v = request.form.get(f'q{i}')
                    if v is None:
                        flash(('error', _('Пожалуйста, ответьте на все вопросы.')))
                        return render_template('test_take.html', test=t, questions=questions,
                                               ends_at=session.get(end_key))
                    answers.append(int(v) if str(v).isdigit() else -1)

            # ----- Подсчёт баллов -----
            score = 0
            for i, q in enumerate(questions):
                if q.get('multiple'):
                    corr = sorted([int(x) for x in (q.get('correct') or [])])
                    if corr and answers[i] == corr:
                        score += 1
                else:
                    ci = q.get('correct_index')
                    if ci is not None and answers[i] == int(ci):
                        score += 1

            # Запись попытки
            c.execute("""
              INSERT INTO test_attempts
                (id, user_id, test_id, started_at, finished_at, score, answers)
              VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                str(uuid.uuid4()),
                u['id'],
                test_id,
                datetime.datetime.now(datetime.timezone.utc),
                datetime.datetime.now(datetime.timezone.utc),
                score,
                json.dumps(answers, ensure_ascii=False)
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

    # шаблон должен уметь чекбоксы для multiple
    return render_template('test_take.html', test=t, questions=questions, ends_at=ends_at)
