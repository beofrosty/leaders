# app/routes/auth.py
import uuid, secrets
from datetime import datetime, timedelta, timezone
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, current_app, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_babel import gettext as _
from flask_mail import Message  # Mail(app) должен быть инициализирован
from ..db import get_user_by_email, get_conn, _pool
from ..decorators import current_user  # если используешь где-то ещё
import psycopg
from psycopg import OperationalError
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
    abort(403)
# def admin_register():
#     code_env = current_app.config.get('ADMIN_INVITE_CODE', '')
#
#     # ВАЖНО: считаем флаг через ретрай (и для GET, и для POST)
#     try:
#         has_admin = _count_admins_with_retry()
#     except Exception:
#         current_app.logger.exception("Failed to check admins count")
#         # при недоступной БД покажем форму без кода, но с ошибкой
#         flash(('error', _('База данных недоступна. Попробуйте позже.')))
#         return render_template('admin_register.html', form={'require_code': True})
#
#     if request.method == 'POST':
#         code = (request.form.get('invite_code') or '').strip()
#         full_name = (request.form.get('full_name') or '').strip()
#         email = (request.form.get('email') or '').strip().lower()
#         password = request.form.get('password') or ''
#         password2 = request.form.get('password2') or ''
#
#         if has_admin and code != code_env:
#             flash(('error', _('Неверный код приглашения.')))
#             return render_template('admin_register.html', form=request.form)
#
#         if not full_name or not email or not password or password != password2 or len(password) < 8:
#             flash(('error', _('Проверьте поля формы.')))
#             return render_template('admin_register.html', form=request.form)
#
#         if get_user_by_email(email):
#             flash(('error', _('Пользователь с таким e-mail уже есть.')))
#             return render_template('admin_register.html', form=request.form)
#
#         uid = str(uuid.uuid4())
#         try:
#             with get_conn() as conn, conn.cursor() as c:
#                 c.execute("""
#                     INSERT INTO users (id, email, full_name, password_hash, is_verified, created_at, role)
#                     VALUES (%s, %s, %s, %s, %s, %s, %s)
#                 """, (
#                     uid, email, full_name,
#                     generate_password_hash(password), True, _utc_now(), 'admin'
#                 ))
#                 conn.commit()
#         except OperationalError as e:
#             # ещё одна страховка на момент INSERT
#             msg = str(e)
#             if 'SSL' in msg or 'EOF' in msg or 'bad record mac' in msg:
#                 try:
#                     if _pool:
#                         _pool.check()
#                 except Exception:
#                     pass
#                 # повторная попытка одного INSERT
#                 try:
#                     with get_conn() as conn, conn.cursor() as c:
#                         c.execute("""
#                             INSERT INTO users (id, email, full_name, password_hash, is_verified, created_at, role)
#                             VALUES (%s, %s, %s, %s, %s, %s, %s)
#                         """, (
#                             uid, email, full_name,
#                             generate_password_hash(password), True, _utc_now(), 'admin'
#                         ))
#                         conn.commit()
#                 except Exception:
#                     current_app.logger.exception("Failed to create admin user after retry")
#                     flash(('error', _('Ошибка базы данных. Попробуйте позже.')))
#                     return render_template('admin_register.html', form=request.form)
#             else:
#                 current_app.logger.exception("Failed to create admin user")
#                 flash(('error', _('Ошибка базы данных. Попробуйте позже.')))
#                 return render_template('admin_register.html', form=request.form)
#         except Exception:
#             current_app.logger.exception("Failed to create admin user")
#             flash(('error', _('Ошибка базы данных. Попробуйте позже.')))
#             return render_template('admin_register.html', form=request.form)
#
#         session.permanent = True
#         session['user_email'] = email
#         session['user_id'] = uid
#         flash(('success', _('Администратор создан!')))
#         return redirect(url_for('admin.admin'))
#
#     # GET
#     return render_template('admin_register.html', form={'require_code': has_admin})



# ---- регистрация -----------------------------------------------------------

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full_name = (request.form.get('full_name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        password = (request.form.get('password') or '')
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
        try:
            with get_conn() as conn, conn.cursor() as c:
                c.execute("""
                    INSERT INTO users (id, email, full_name, password_hash, is_verified, created_at, role)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    uid, email, full_name,
                    generate_password_hash(password), True, _utc_now(), 'user'
                ))
                conn.commit()
        except psycopg.Error:
            current_app.logger.exception("Failed to create user")
            flash(('error', _('Ошибка базы данных. Попробуйте позже.')))
            return render_template('register.html', form=request.form)

        session.permanent = True
        session['user_email'] = email
        session['user_id'] = uid
        flash(('success', _('Регистрация успешна!')))
        return redirect(url_for('main.index'))

    return render_template('register.html', form={})


# ---- логин/логаут ----------------------------------------------------------

@bp.route('/login', methods=['GET', 'POST'])
def login():
    # Уже вошли: доведём сессию до ума и отправим по роли
    if request.method == 'GET' and 'user_email' in session:
        u = get_user_by_email(session['user_email'])
        if u:
            session.setdefault('user_id', u['id'])
            role = str((u.get('role') if isinstance(u, dict) else u['role']) or '').lower()
            return redirect(url_for('admin.admin') if role == 'admin' else url_for('main.index'))
        # если пользователя больше нет — почистим сессию
        session.clear()

    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        user = get_user_by_email(email)

        # проверка учётных данных
        pw_hash = (user.get('password_hash') if user and isinstance(user, dict) else (user['password_hash'] if user else None))
        if (not user) or (not pw_hash) or (not check_password_hash(pw_hash, password)):
            flash(('error', _('Неверные e-mail или пароль.')))
            return render_template('login.html', form=request.form, next=request.args.get('next') or '')
        if ('is_verified' in user.keys()) and (not user['is_verified']):
            flash(('error', _('Аккаунт не подтверждён.')))
            return render_template('login.html', form=request.form, next=request.args.get('next') or '')

        # блоки доступа
        if not user.get('is_active', True):
            flash(('error', _('Учетная запись деактивирована.')))
            return render_template('login.html', form=request.form, next=request.args.get('next') or '')
        exp = user.get('access_expires_at')
        if exp and exp < _utc_now():
            flash(('error', _('Срок доступа истёк. Обратитесь к администратору.')))
            return render_template('login.html', form=request.form, next=request.args.get('next') or '')

        # сохранить сессию
        session.permanent = True
        session['user_email'] = email
        session['user_id'] = user['id']

        # принудительная смена при первом входе
        if user.get('must_change_password'):
            return redirect(url_for('auth.force_change_credentials'))

        # вход
        session.permanent = True
        session['user_email'] = email
        session['user_id'] = user['id']
        flash(('success', _('Добро пожаловать!')))

        # если админ — всегда в админку
        role = str((user.get('role') if isinstance(user, dict) else user['role']) or '').lower()
        if role == 'provisioner':
            return redirect(url_for('prov.dashboard'))
        if role == 'admin':
            return redirect(url_for('admin.admin'))

        # иначе уважаем next (с защитой), либо на главную
        next_url = request.form.get('next') or request.args.get('next')
        return redirect(_safe_next(next_url))

    # GET
    return render_template('login.html', form={}, next=request.args.get('next') or '')


@bp.route('/logout', methods=['POST'])  # можно ['GET','POST'] на время разработки
def logout():
    session.clear()
    flash(('success', _('Вы вышли из аккаунта.')))
    return redirect(url_for('auth.login'))


# ---- забыли пароль / сброс --------------------------------------------------

@bp.route('/forgot', methods=['GET', 'POST'], endpoint='forgot')
def forgot():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        try:
            with get_conn() as conn, conn.cursor() as c:
                c.execute("SELECT id, email FROM users WHERE LOWER(email) = LOWER(%s)", (email,))
                u = c.fetchone()
                if u:
                    token = secrets.token_urlsafe(32)
                    expires_at = _utc_now() + timedelta(hours=2)  # TIMESTAMPTZ
                    c.execute("""
                        INSERT INTO password_resets (id, user_id, token, expires_at)
                        VALUES (%s, %s, %s, %s)
                    """, (str(uuid.uuid4()), u['id'], token, expires_at))
                    conn.commit()  # фиксируем запись токена

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

    # шаблон с формой запроса ссылки
    return render_template('auth_forgot.html')


@bp.route('/reset/<token>', methods=['GET', 'POST'], endpoint='reset')
def reset(token):
    with get_conn() as conn, conn.cursor() as c:
        c.execute("""
            SELECT pr.id, pr.user_id, pr.expires_at, pr.used, u.email
              FROM password_resets pr
              JOIN users u ON u.id = pr.user_id
             WHERE pr.token = %s
        """, (token,))
        row = c.fetchone()

    if (not row) or row['used'] or (row['expires_at'] < _utc_now()):
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
            with get_conn() as conn, conn.cursor() as c:
                c.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                          (generate_password_hash(pw), row['user_id']))
                c.execute("UPDATE password_resets SET used = TRUE WHERE id = %s", (row['id'],))
                conn.commit()
            flash(('success', _('Пароль обновлён. Теперь вы можете войти.')))
            return redirect(url_for('auth.login'))
        except Exception as e:
            current_app.logger.exception(e)
            flash(('error', _('Не удалось обновить пароль. Попробуйте позже.')))
            return redirect(request.url)

    # шаблон формы смены пароля
    return render_template('auth_reset.html')
def _count_admins() -> bool:
    with get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT COUNT(*) AS cnt FROM users WHERE role = %s", ('admin',))
        row = c.fetchone()
        return bool(row and (row['cnt'] or 0) > 0)

def _count_admins_with_retry() -> bool:
    try:
        return _count_admins()
    except OperationalError as e:
        msg = str(e)
        # типичные TLS-обрывы на Render / managed Postgres
        if 'SSL' in msg or 'EOF' in msg or 'bad record mac' in msg:
            try:
                if _pool:
                    _pool.check()  # пересоздаст протухшие коннекты в пуле
            except Exception:
                pass
            # повторяем один раз
            return _count_admins()
        # не похожа на сетевую проблему — пробрасываем дальше
        raise
@bp.route('/force-change', methods=['GET', 'POST'], endpoint='force_change_credentials')
def force_change_credentials():
    from ..db import get_user_by_email, get_conn
    u = get_user_by_email(session.get('user_email') or '')
    if not u:
        session.clear()
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        new_email = (request.form.get('email') or '').strip().lower()
        pw = (request.form.get('password') or '').strip()
        pw2 = (request.form.get('password2') or '').strip()

        if not new_email or '@' not in new_email or '.' not in new_email:
            flash(('error', _('Некорректный e-mail.'))); return render_template('force_change.html', form=request.form)
        if len(pw) < 12:
            flash(('error', _('Пароль должен быть не менее 12 символов.'))); return render_template('force_change.html', form=request.form)
        if pw != pw2:
            flash(('error', _('Пароли не совпадают.'))); return render_template('force_change.html', form=request.form)

        other = get_user_by_email(new_email)
        if other and other['id'] != u['id']:
            flash(('error', _('Пользователь с таким e-mail уже есть.'))); return render_template('force_change.html', form=request.form)

        try:
            with get_conn() as conn, conn.cursor() as c:
                c.execute("""
                  UPDATE users
                     SET email=%s, password_hash=%s, must_change_password=FALSE
                   WHERE id=%s
                """, (new_email, generate_password_hash(pw), u['id']))
                conn.commit()
            session['user_email'] = new_email
            flash(('success', _('Данные учётной записи обновлены.')))
            role = (u.get('role') or '').lower()
            if role == 'provisioner':
                return redirect(url_for('prov.dashboard'))
            if role == 'admin':
                return redirect(url_for('admin.admin'))
            return redirect(url_for('main.index'))
        except Exception:
            current_app.logger.exception("force-change failed")
            flash(('error', _('Не удалось обновить. Попробуйте позже.')))
            return render_template('force_change.html', form=request.form)

    return render_template('force_change.html', form={'email': u['email']})
