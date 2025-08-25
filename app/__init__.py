# app/__init__.py
from datetime import timedelta
from flask import Flask, request, session, current_app, redirect, url_for

from .config import Config
from .extensions import mail, babel
from .db import init_pool, bootstrap_schema  # ← NEW: из postgres-версии db.py

from .routes.auth import bp as auth_bp
from .routes.main import bp as main_bp
from .routes.admin import bp as admin_bp
from .routes.tests import bp as tests_bp

from flask_babel import gettext as _, ngettext, get_locale as babel_get_locale
from pathlib import Path


def select_locale():
    # 1) явный выбор пользователя
    lang = session.get('lang')
    if lang:
        return lang
    # 2) best-match из списка поддерживаемых
    supported = current_app.config.get('BABEL_SUPPORTED_LOCALES', ['ru', 'ky'])
    default = current_app.config.get('BABEL_DEFAULT_LOCALE', 'ru')
    return request.accept_languages.best_match(supported) or default


def _safe_next(url: str | None):
    # разрешаем только внутренние пути типа "/..." (не //, не http://)
    if url and url.startswith('/') and not url.startswith('//'):
        return url
    return url_for('main.index')


from pathlib import Path
from flask_babel import gettext as _, ngettext, get_locale as babel_get_locale

def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(Config)

    # авто-перезагрузка шаблонов
    app.config.setdefault('TEMPLATES_AUTO_RELOAD', app.debug)
    app.jinja_env.auto_reload = app.config['TEMPLATES_AUTO_RELOAD']

    # базовые i18n
    app.config.setdefault('BABEL_DEFAULT_LOCALE', 'ru')
    app.config.setdefault('BABEL_SUPPORTED_LOCALES', ['ru', 'ky'])

    # ✅ укажем обе директории переводов: <project>/translations И <project>/app/translations
    proj_root = Path(app.root_path).parent
    app_root = Path(app.root_path)
    app.config['BABEL_TRANSLATION_DIRECTORIES'] = (
        f"{proj_root / 'translations'};{app_root / 'translations'}"
    )

    # сессии (в dev ОТКЛЮЧИВАЕМ secure)
    secure_env = not app.debug and not app.testing
    app.config.update(
        SECRET_KEY=app.config.get('SECRET_KEY', 'dev-secret-change-me'),
        SESSION_COOKIE_NAME='leaders_session',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=False if app.debug else secure_env,  # ← важно для локалки!
        PERMANENT_SESSION_LIFETIME=timedelta(days=31),
    )

    from .extensions import mail, babel
    babel.init_app(app, locale_selector=select_locale)

    @app.context_processor
    def inject_i18n():
        return {'_': _, 'ngettext': ngettext, 'get_locale': lambda: str(babel_get_locale())}

    # остальной код и регистрация блюпринтов...


    @app.route('/set-locale/<lang>')
    def set_locale(lang):
        supported = current_app.config.get('BABEL_SUPPORTED_LOCALES', ['ru', 'ky'])
        if lang not in supported:
            return redirect(_safe_next(request.args.get('next') or request.referrer))
        session['lang'] = lang
        session.permanent = True
        session.modified = True
        nxt = request.args.get('next') or request.referrer or url_for('main.index')
        return redirect(_safe_next(nxt))

    # Extensions
    mail.init_app(app)

    if not app.config.get('MAIL_USERNAME') or not app.config.get('MAIL_PASSWORD'):
        app.logger.warning("MAIL_USERNAME/MAIL_PASSWORD не заданы — письма не отправятся.")
    if not app.config.get('MAIL_DEFAULT_SENDER'):
        app.logger.warning("MAIL_DEFAULT_SENDER пуст — добавь MAIL_USERNAME/MAIL_FROM_ADDRESS.")

    # ---------- DB (PostgreSQL) ----------
    # init_pool читает DSN из app.config["DB_DSN"] или DATABASE_URL
    init_pool(app)
    with app.app_context():
        # создаём/обновляем схему (users, applications, tests, test_attempts, password_resets)
        bootstrap_schema()

    # Blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(tests_bp)

    return app

