from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate


@dataclass
class SmtpConfig:
    host: str
    port: int
    encryption: str
    username: str = ""
    from_name: str = "循知"


def _result(ok: bool, code: str, message: str, *, uncertain: bool = False) -> dict:
    return {"ok": ok, "code": code, "message": message, "uncertain": uncertain}


def send_email(
    config: SmtpConfig,
    password: str,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    *,
    timeout: int = 20,
) -> dict:
    host = str(config.host or "").strip()
    recipient = str(to_email or "").strip()
    if not host or not recipient:
        return _result(False, "smtp_config_incomplete", "SMTP 服务器或收件人未配置。")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((config.from_name or "循知", config.username or recipient))
    message["To"] = recipient
    message["Date"] = formatdate(localtime=True)
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    client = None
    try:
        if config.encryption == "ssl":
            client = smtplib.SMTP_SSL(host, int(config.port or 465), timeout=timeout, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(host, int(config.port or 587), timeout=timeout)
            client.ehlo()
            if config.encryption == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
        if config.username:
            client.login(config.username, password or "")
        client.send_message(message)
        return _result(True, "sent", "邮件服务已接收。")
    except smtplib.SMTPAuthenticationError:
        return _result(False, "smtp_auth_failed", "SMTP 授权失败，请检查账号和密码/授权码。")
    except smtplib.SMTPRecipientsRefused:
        return _result(False, "smtp_recipient_rejected", "收件地址被邮件服务拒绝。")
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected):
        return _result(False, "smtp_network_error", "无法连接 SMTP 服务器或连接中断。")
    except (socket.timeout, TimeoutError):
        return _result(False, "smtp_timeout", "连接 SMTP 服务器超时。")
    except (ConnectionRefusedError, OSError):
        return _result(False, "smtp_network_error", "网络错误：无法连接 SMTP 服务器。")
    except smtplib.SMTPException:
        return _result(
            False,
            "smtp_uncertain",
            "邮件发送结果待确认：SMTP 在投递过程中返回异常，服务是否接收不明确。",
            uncertain=True,
        )
    except Exception:  # noqa: BLE001
        return _result(
            False,
            "smtp_uncertain",
            "邮件发送结果待确认：发送过程中出现异常，服务是否接收不明确。",
            uncertain=True,
        )
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass