# app/__init__.py
from datetime import timedelta
from flask import Flask, request, session, current_app, redirect, url_for
from .config import Config
from .extensions import mail, babel
from .db import init_pool, bootstrap_schema
from .routes.auth import bp as auth_bp
from .routes.main import bp as main_bp
from .routes.admin import bp as admin_bp
from .routes.tests import bp as tests_bp
from flask_babel import gettext as _, ngettext, get_locale as babel_get_locale
from pathlib import Path
from .routes.provisioning import bp as prov_bp
def select_locale():
    lang = session.get('lang')
    if lang:
        return lang
    supported = current_app.config.get('BABEL_SUPPORTED_LOCALES', ['ru', 'ky'])
    default = current_app.config.get('BABEL_DEFAULT_LOCALE', 'ru')
    return request.accept_languages.best_match(supported) or default

def _safe_next(url: str | None):
    if url and url.startswith('/') and not url.startswith('//'):
        return url
    return url_for('main.index')

def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(Config)

    # ⚠️ ОДИН-ЕДИНСТВЕННЫЙ dev-конфиг для работы по http://IP:5000
    app.config.update(
        SECRET_KEY=app.config.get('SECRET_KEY', 'dev-secret-change-me'),
        SESSION_COOKIE_NAME='leaders_session_devip',   # новое имя — чтобы не путаться со старой кукой
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=False,                   # ВАЖНО: без Secure
        SESSION_COOKIE_DOMAIN=None,                    # ВАЖНО: без домена при доступе по IP
        PERMANENT_SESSION_LIFETIME=timedelta(days=31),
        TEMPLATES_AUTO_RELOAD=True,
        BABEL_DEFAULT_LOCALE='ru',
        BABEL_SUPPORTED_LOCALES=['ru', 'ky'],
        PREFERRED_URL_SCHEME='http',
    )

    # где лежат переводы
    proj_root = Path(app.root_path).parent
    app_root = Path(app.root_path)
    app.config['BABEL_TRANSLATION_DIRECTORIES'] = (
        f"{proj_root / 'translations'};{app_root / 'translations'}"
    )

    # простой лог, чтобы убедиться, что значения реально такие
    print(
        "SESSION cfg -> NAME=%r SECURE=%r DOMAIN=%r SAMESITE=%r"
        % (
            app.config.get('SESSION_COOKIE_NAME'),
            app.config.get('SESSION_COOKIE_SECURE'),
            app.config.get('SESSION_COOKIE_DOMAIN'),
            app.config.get('SESSION_COOKIE_SAMESITE'),
        )
    )

    babel.init_app(app, locale_selector=select_locale)

    @app.context_processor
    def inject_i18n():
        return {'_': _, 'ngettext': ngettext, 'get_locale': lambda: str(babel_get_locale())}

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

    mail.init_app(app)

    # БД
    init_pool(app)
    # with app.app_context():
    #     bootstrap_schema()

    # блюпринты
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(tests_bp)
    app.register_blueprint(prov_bp)
    return app
