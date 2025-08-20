# app/__init__.py
from datetime import timedelta
from flask import Flask, request, session, current_app, redirect, url_for

from .config import Config
from .extensions import mail, babel
from .db import (
    init_db, ensure_user_columns, ensure_applications_table, ensure_tests_tables,
    ensure_users_index_email, ensure_password_resets_table,
)
from .routes.auth import bp as auth_bp
from .routes.main import bp as main_bp
from .routes.admin import bp as admin_bp
from .routes.tests import bp as tests_bp

from flask_babel import gettext as _, ngettext, get_locale as babel_get_locale

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

def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(Config)
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True

    # --- cookie/локаль/сессии ---
    secure_env = not app.debug and not app.testing
    app.config.setdefault('SECRET_KEY', 'dev-secret-change-me')
    app.config.setdefault('BABEL_DEFAULT_LOCALE', 'ru')
    app.config.setdefault('BABEL_SUPPORTED_LOCALES', ['ru', 'ky'])
    app.config.setdefault('BABEL_TRANSLATION_DIRECTORIES', 'translations')
    app.config.update(
        SESSION_COOKIE_NAME='leaders_session',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=secure_env,
        PERMANENT_SESSION_LIFETIME=timedelta(days=31),  # timedelta вместо int
        TEMPLATES_AUTO_RELOAD=True if app.debug else app.config.get('TEMPLATES_AUTO_RELOAD', False),
    )

    # ВАЖНО: не чистим flash глобально — иначе они пропадут после redirect
    # Если очень нужно где-то очищать — делай это только для API-роутов:
    @app.after_request
    def _maybe_clear_flashes(resp):
        if request.path.startswith('/api/'):
            session.pop('_flashes', None)
        return resp

    # Babel
    babel.init_app(app, locale_selector=select_locale)

    # Делаем i18n-хелперы доступными в шаблонах
    @app.context_processor
    def inject_i18n():
        return {'_': _, 'ngettext': ngettext, 'get_locale': lambda: str(babel_get_locale())}

    # Переключатель языка (поддерживает ?next=)
    @app.route('/set-locale/<lang>')
    def set_locale(lang):
        supported = current_app.config.get('BABEL_SUPPORTED_LOCALES', ['ru', 'ky'])
        if lang not in supported:
            return redirect(_safe_next(request.referrer))
        session['lang'] = lang
        session.permanent = True
        session.modified = True
        nxt = request.args.get('next') or request.referrer
        return redirect(_safe_next(nxt))

    # Extensions
    mail.init_app(app)

    # DB schema bootstrap
    with app.app_context():
        init_db()
        ensure_user_columns()
        ensure_users_index_email()
        ensure_applications_table()
        ensure_tests_tables()
        ensure_password_resets_table()


    # Blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(tests_bp)
    return app

