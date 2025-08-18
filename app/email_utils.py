# app/email_utils.py
from flask import current_app
from flask_mail import Message
from .extensions import mail
from flask_babel import gettext as _


def _send_email(subject: str, to_email: str, body_text: str) -> bool:
    """Безопасная отправка простого текстового письма. Возвращает True/False."""
    try:
        if not mail or 'mail' not in current_app.extensions:
            current_app.logger.warning("Mail is not configured; email to %s skipped.", to_email)
            return False
        msg = Message(
            subject=subject,
            recipients=[to_email],
            body=body_text
        )
        mail.send(msg)
        return True
    except Exception as e:
        current_app.logger.exception("Failed to send email to %s: %s", to_email, e)
        return False


def send_accept_email(to_email: str, full_name: str | None = None, test_url: str | None = None) -> bool:
    """Письмо при одобрении заявки. test_url берём из конфигурации, если не передали."""
    if not test_url:
        # Можно задать переменную окружения TEST_URL, чтобы класть прямую ссылку
        test_url = current_app.config.get("TEST_URL") or ""

    subject = _("Ваша заявка одобрена")
    lines = [
        _("%(name)s, ваша заявка одобрена!", name=(full_name or _("Участник"))),
        "",
        _("Вы допущены на следующий этап конкурса."),
    ]
    if test_url:
        lines += [
            _("Перейдите по ссылке, чтобы пройти тест:"),
            test_url,
        ]
    else:
        lines += [
            _("Чтобы пройти тест, войдите в личный кабинет и откройте раздел «Тест»."),
        ]
    lines += ["", _("С уважением,"), _("Оргкомитет «Лидеры года»")]
    return _send_email(subject, to_email, "\n".join(lines))


def send_reject_email(to_email: str, reason: str | None = None, full_name: str | None = None) -> bool:
    """Письмо при отклонении заявки (с указанием причины)."""
    subject = _("Ваша заявка отклонена")
    lines = [
        _("%(name)s, к сожалению, ваша заявка отклонена.", name=(full_name or _("Участник"))),
    ]
    if reason:
        lines += ["", _("Причина:"), reason]
    lines += ["", _("Спасибо за участие."), _("С уважением,"), _("Оргкомитет «Лидеры года»")]
    return _send_email(subject, to_email, "\n".join(lines))
