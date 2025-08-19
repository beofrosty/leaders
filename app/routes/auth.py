# app/routes/auth.py
import sqlite3, uuid, secrets
from datetime import datetime, timedelta, timezone
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, current_app
)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_babel import gettext as _
from flask_mail import Message  # Mail(app) должен быть инициализирован
from ..db import get_user_by_email
from ..decorators import current_user  # если используешь где-то ещё

bp = Blueprint('auth', __name__)

# ---- helpers ---------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _now_utc_iso() -> str:
    return _utc_now().isoformat()

def _get_mail():
    return current_app.extensions.get('mail')  # может вернуть None

def _safe_next(url: str | None) -> str:
    # Разрешаем только внутренние пути "/...". Защита от open-redirect.
    if url and url.startswith('/') and not url.startswith('//'):
        return url
    return url_for('main.index')

# Jinja helper для русских форм множественного числа (если нужен в шаблонах)
@bp.app_template_global('ru_plural')
def ru_plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return few
    return many


# ---- админская регистрация -------------------------------------------------

@bp.route('/admin/register', methods=['GET', 'POST'])
def admin_register():
    DB = current_app.config['DB_PATH']
    code_env = current_app.config.get('ADMIN_INVITE_CODE', '')

    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
        has_admin = c.fetchone()[0] > 0

    if request.method == 'POST':
        code = (request.form.get('invite_code') or '').strip()
        full_name = (request.form.get('full_name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        password2 = request.form.get('password2') or ''

        if has_admin and code != code_env:
            flash(('error', _('Неверный код приглашения.')))
            return render_template('admin_register.html', form=request.form)

        if not full_name or not email or not password or password != password2 or len(password) < 8:
            flash(('error', _('Проверьте поля формы.')))
            return render_template('admin_register.html', form=request.form)

        if get_user_by_email(email):
            flash(('error', _('Пользователь с таким e-mail уже есть.')))
            return render_template('admin_register.html', form=request.form)

        uid = str(uuid.uuid4())
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO users (id, email, full_name, password_hash, is_verified, created_at, role)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                uid, email, full_name,
                generate_password_hash(password), 1, _now_utc_iso(), 'admin'
            ))
            conn.commit()

        session.permanent = True
        session['user_email'] = email
        session['user_id'] = uid
        flash(('success', _('Администратор создан!')))
        return redirect(url_for('admin.admin'))

    return render_template('admin_register.html', form={'require_code': has_admin})


# ---- регистрация -----------------------------------------------------------

@bp.route('/register', methods=['GET', 'POST'])
def register():
    DB = current_app.config['DB_PATH']
    if request.method == 'POST':
        full_name = (request.form.get('full_name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        password2 = (request.form.get('password2') or '')

        if not full_name or not email or not password or not password2:
            flash(('error', _('Заполните все поля.')))
            return render_template('register.html', form=request.form)
        if '@' not in email or '.' not in email:
            flash(('error', _('Некорректный e-mail.')))
            return render_template('register.html', form=request.form)
        if len(password) < 12:
            flash(('error', _('Пароль должен быть не короче 12 символов.')))
            return render_template('register.html', form=request.form)
        if password != password2:
            flash(('error', _('Пароли не совпадают.')))
            return render_template('register.html', form=request.form)
        if get_user_by_email(email):
            flash(('error', _('Пользователь с таким e-mail уже есть.')))
            return render_template('register.html', form=request.form)

        uid = str(uuid.uuid4())
        with sqlite3.connect(DB) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO users (id, email, full_name, password_hash, is_verified, created_at, role)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                uid, email, full_name,
                generate_password_hash(password), 1, _now_utc_iso(), 'user'
            ))
            conn.commit()

        session.permanent = True
        session['user_email'] = email
        session['user_id'] = uid
        flash(('success', _('Регистрация успешна!')))
        return redirect(url_for('main.index'))

    return render_template('register.html', form={})


# ---- логин/логаут ----------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        try:
            user = get_user_by_email(email)
        except Exception as e:
            # аккуратно логируем и показываем пользователю дружелюбное сообщение
            # (в проде — лог через logger)
            flash(("error", "Временная ошибка подключения к базе. Попробуйте ещё раз."))
            return render_template("login.html"), 500

        if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
            flash(("error", "Неверная почта или пароль."))
            return render_template("login.html"), 401

        session["user_email"] = user["email"]
        # ... остальная логика
        return redirect(url_for("main.applications"))

    return render_template("login.html")

@bp.route('/logout')
def logout():
    session.clear()
    flash(('success', _('Вы вышли из аккаунта.')))
    return redirect(url_for('auth.login'))


# ---- забыли пароль / сброс --------------------------------------------------

@bp.route('/forgot', methods=['GET', 'POST'], endpoint='forgot')
def forgot():
    DB = current_app.config['DB_PATH']
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        try:
            with sqlite3.connect(DB) as conn:
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT id, email FROM users WHERE lower(email)=?", (email,))
                u = c.fetchone()
                if u:
                    token = secrets.token_urlsafe(32)
                    expires_at = (_utc_now() + timedelta(hours=2)).isoformat()
                    c.execute("""
                        INSERT INTO password_resets (id, user_id, token, expires_at)
                        VALUES (?, ?, ?, ?)
                    """, (str(uuid.uuid4()), u['id'], token, expires_at))
                    conn.commit()  # <-- ВАЖНО: фиксируем запись токена

                    reset_link = url_for('auth.reset', token=token, _external=True)

                    mail = _get_mail()
                    if mail:
                        msg = Message(
                            _('Сброс пароля'),
                            recipients=[u['email']],
                            body=_("Чтобы сбросить пароль, перейдите по ссылке:\n%(link)s\n\nСсылка действует 2 часа и одноразовая.", link=reset_link)
                        )
                        mail.send(msg)
                    else:
                        current_app.logger.warning("Mail is not configured. Reset link: %s", reset_link)

            flash(('success', _('Если такой e-mail существует, мы отправили ссылку для сброса. Проверьте почту.')))
        except Exception as e:
            current_app.logger.exception(e)
            flash(('error', _('Не удалось отправить ссылку. Попробуйте позже.')))
        return redirect(url_for('auth.forgot'))

    # твой шаблон с формой запроса ссылки
    return render_template('auth_forgot.html')

@bp.route('/reset/<token>', methods=['GET', 'POST'], endpoint='reset')
def reset(token):
    DB = current_app.config['DB_PATH']
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""SELECT pr.id, pr.user_id, pr.expires_at, pr.used, u.email
                     FROM password_resets pr
                     JOIN users u ON u.id=pr.user_id
                     WHERE pr.token=?""", (token,))
        row = c.fetchone()

    if (not row) or row['used'] or datetime.fromisoformat(row['expires_at']) < _utc_now():
        flash(('error', _('Ссылка недействительна или устарела. Запросите новую.')))
        return redirect(url_for('auth.forgot'))

    if request.method == 'POST':
        pw = (request.form.get('password') or '').strip()
        pw2 = (request.form.get('password2') or '').strip()
        if len(pw) < 12:
            flash(('error', _('Пароль должен быть не менее 12 символов.')))
            return redirect(request.url)
        if pw != pw2:
            flash(('error', _('Пароли не совпадают.')))
            return redirect(request.url)
        try:
            with sqlite3.connect(DB) as conn:
                c = conn.cursor()
                c.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), row['user_id']))
                c.execute("UPDATE password_resets SET used=1 WHERE id=?", (row['id'],))
                conn.commit()
            flash(('success', _('Пароль обновлён. Теперь вы можете войти.')))
            return redirect(url_for('auth.login'))
        except Exception as e:
            current_app.logger.exception(e)
            flash(('error', _('Не удалось обновить пароль. Попробуйте позже.')))
            return redirect(request.url)

    # твой шаблон формы смены пароля
    return render_template('auth_reset.html')
