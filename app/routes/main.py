# app/routes/main.py
import sqlite3, uuid, json
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app, jsonify
from ..decorators import login_required
from ..db import get_user_by_email
from flask_babel import gettext as _

bp = Blueprint('main', __name__)

@bp.route('/')
def index():
    # Требуем авторизацию
    if 'user_email' not in session:
        return redirect(url_for('auth.login'))

    DB = current_app.config['DB_PATH']
    u = get_user_by_email(session['user_email'])
    if not u:
        session.clear()
        return redirect(url_for('auth.login'))

    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        # Все заявки пользователя (последние сверху)
        c.execute("""
          SELECT id, commission_status, commission_comment, created_at
          FROM applications
          WHERE user_id=?
          ORDER BY datetime(created_at) DESC
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
        submitted=submitted,                 # скрывать кнопку/модалку при наличии заявки
        just_submitted=just_submitted,       # показать плашку один раз после отправки
        applications=apps,
        already_submitted=already_submitted,
        error=None
    )


@bp.route('/form', methods=['GET', 'POST'])
@login_required
def form():
    DB = current_app.config['DB_PATH']

    u = get_user_by_email(session['user_email'])
    if not u:
        flash(('error', _('Пользователь не найден.')))
        return redirect(url_for('auth.login'))

    # GET — возвращаем на главную (чтобы форма всегда открывалась модалкой на /)
    if request.method == 'GET':
        return redirect(url_for('main.index'))

    # POST — создание заявки
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM applications WHERE user_id=?", (u['id'],))
        already_submitted = c.fetchone()[0] > 0

    if already_submitted:
        flash(('error', _('Вы уже отправили заявку. Повторная подача невозможна.')))
        return redirect(url_for('main.applications'))

    data = request.form.to_dict()
    try:
        with sqlite3.connect(DB) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            app_id = str(uuid.uuid4())
            c.execute("""
                INSERT INTO applications (
                    id, user_id, form_data, commission_comment, commission_status,
                    test_score, test_answers, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                app_id, u['id'], json.dumps(data, ensure_ascii=False),
                None, None, None, None, datetime.utcnow().isoformat()
            ))
            conn.commit()
        # PRG: на главную с флагом одноразовой плашки
        return redirect(url_for('main.index', submitted=1))
    except sqlite3.IntegrityError:
        flash(('error', _('Заявка уже существует. Повторная подача невозможна.')))
        return redirect(url_for('main.applications'))


@bp.route('/profile/<user_id>')
@login_required
def profile(user_id):
    DB = current_app.config['DB_PATH']
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        # сам пользователь
        c.execute("SELECT * FROM users WHERE id=?", (user_id,))
        user = c.fetchone()

        # последняя заявка пользователя
        c.execute("""
          SELECT id, form_data, test_answers, commission_status, commission_comment, created_at
          FROM applications
          WHERE user_id=?
          ORDER BY datetime(created_at) DESC
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


@bp.route('/applications')
@login_required
def applications():
    DB = current_app.config['DB_PATH']
    u = get_user_by_email(session['user_email'])
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
          SELECT a.id,
                 u.full_name,
                 u.email,
                 a.commission_status,
                 a.commission_comment,
                 a.created_at
          FROM applications a
          JOIN users u ON u.id = a.user_id
          WHERE a.user_id = ?
          ORDER BY datetime(a.created_at) DESC
        """, (u['id'],))
        items = c.fetchall()
    return render_template('applications.html', items=items)



@bp.get('/api/my-application')
@login_required
def api_my_application():
    """Возвращает последний статус заявки текущего пользователя."""
    DB = current_app.config['DB_PATH']
    u = get_user_by_email(session['user_email'])
    if not u:
        return jsonify(exists=False), 404

    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
          SELECT id, commission_status, commission_comment, created_at
          FROM applications
          WHERE user_id=?
          ORDER BY datetime(created_at) DESC
          LIMIT 1
        """, (u['id'],))
        row = c.fetchone()

    if not row:
        return jsonify(exists=False)

    return jsonify(
        exists=True,
        id=row['id'],
        status=row['commission_status'],
        comment=row['commission_comment'],
        created_at=row['created_at']
    )
