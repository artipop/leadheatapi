from __future__ import annotations

import argparse
import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


def clean_value(value: str) -> str:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return value.strip()


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = clean_value(value)
    return data


def env_or_file(key: str, file_values: dict[str, str], default: str = "") -> str:
    return clean_value(os.environ.get(key, "")) or file_values.get(key, default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a test email via SMTP (STARTTLS or SSL).")
    parser.add_argument("--env-file", default=".smtp_env", help="Path to env file with SMTP_* keys.")
    parser.add_argument("--host", help="SMTP host. Fallback: SMTP_HOST from env file/env.")
    parser.add_argument("--port", type=int, help="SMTP port. Fallback: SMTP_PORT.")
    parser.add_argument("--user", help="SMTP auth username. Fallback: SMTP_USER.")
    parser.add_argument("--password", help="SMTP auth password. Fallback: SMTP_PASS.")
    parser.add_argument("--to", dest="rcpt_to", help="Recipient email. Fallback: MAIL_TO.")
    parser.add_argument(
        "--mail-from",
        help="Envelope/header From. Default: same as --user.",
    )
    parser.add_argument("--subject", default="test", help="Email subject.")
    parser.add_argument("--body", default="привет", help="Email body text.")
    parser.add_argument(
        "--ssl",
        action="store_true",
        help="Use implicit SSL (SMTP_SSL). If omitted: use STARTTLS when supported.",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Socket timeout seconds.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    file_values = load_env_file(Path(args.env_file))

    host = clean_value(args.host) if args.host else env_or_file("SMTP_HOST", file_values)
    port_raw = str(args.port) if args.port else env_or_file("SMTP_PORT", file_values, "587")
    user = clean_value(args.user) if args.user else env_or_file("SMTP_USER", file_values)
    password = clean_value(args.password) if args.password else env_or_file("SMTP_PASS", file_values)
    rcpt_to = clean_value(args.rcpt_to) if args.rcpt_to else env_or_file("MAIL_TO", file_values)

    if not user:
        # practical fallback used in your local flow
        user = rcpt_to

    if not host:
        raise SystemExit("SMTP host is empty. Set --host or SMTP_HOST.")
    if not port_raw.isdigit():
        raise SystemExit(f"Invalid SMTP port: {port_raw!r}")
    port = int(port_raw)
    if not user:
        raise SystemExit("SMTP user is empty. Set --user or SMTP_USER.")
    if not password:
        raise SystemExit("SMTP password is empty. Set --password or SMTP_PASS.")
    if not rcpt_to:
        raise SystemExit("Recipient is empty. Set --to or MAIL_TO.")

    mail_from = clean_value(args.mail_from) if args.mail_from else user
    use_ssl = args.ssl or port == 465

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = rcpt_to
    msg["Subject"] = args.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(mail_from.split("@", 1)[1] if "@" in mail_from else "localhost"))
    msg.set_content(args.body)

    print(
        f"SMTP send start host={host} port={port} user={user} "
        f"from={mail_from} to={rcpt_to} ssl={use_ssl}"
    )

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=args.timeout) as smtp:
            code, resp = smtp.login(user, password)
            resp_text = resp.decode("utf-8", errors="replace") if isinstance(resp, bytes) else str(resp)
            print(f"login: {code} {resp_text}")
            refused = smtp.sendmail(mail_from, [rcpt_to], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=args.timeout) as smtp:
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls()
                smtp.ehlo()
            code, resp = smtp.login(user, password)
            resp_text = resp.decode("utf-8", errors="replace") if isinstance(resp, bytes) else str(resp)
            print(f"login: {code} {resp_text}")
            refused = smtp.sendmail(mail_from, [rcpt_to], msg.as_string())

    print(f"refused: {refused}")
    print(f"message-id: {msg['Message-ID']}")
    print("sent")


if __name__ == "__main__":
    main()
