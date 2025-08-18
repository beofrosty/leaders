# app/decorators.py
from functools import wraps
from flask import session, redirect, url_for, flash
from .db import get_user_by_email
from flask_babel import gettext as _

def current_user():
    email = session.get('user_email')
    if not email:
        return None
    row = get_user_by_email(email)
    return dict(row) if row else None   # <-- теперь .get() будет работать

def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if 'user_email' not in session:
            return redirect(url_for('auth.login'))
        return view(*args, **kwargs)
    return wrapper

def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u or u.get('role') != 'admin':
            flash(_('Доступ только для администраторов'), 'error')
            return redirect(url_for('main.index'))
        return view(*args, **kwargs)
    return wrapper
