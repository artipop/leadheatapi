#!/usr/bin/env python3
"""
Async SMTP Email Verifier
=========================
Проверяет email-адреса через SMTP без реальной отправки писем.

Использование:
  # Напрямую к MX-серверам (нужен pip install aiodns tqdm):
  python email_verifier.py emails.csv --output results.csv --workers 50 --column email_candidate

  # Через relay (только pip install tqdm, aiodns не нужен):
  python email_verifier.py emails.csv --output results.csv --column email_candidate \
      --relay myserver.com --relay-port 25

  # Через relay с авторизацией:
  python email_verifier.py emails.csv --output results.csv --column email_candidate \
      --relay myserver.com --relay-port 587 --relay-user user@domain.com --relay-pass secret

Зависимости:
  pip install aiodns tqdm   # для прямого режима
  pip install tqdm          # для relay-режима
"""

import asyncio
import csv
import re
import sys
import time
import argparse
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import socket

try:
    import aiodns
except ImportError:
    print("Установите зависимости: pip install aiodns aiosmtplib tqdm")
    sys.exit(1)

try:
    from tqdm.asyncio import tqdm as async_tqdm
    from tqdm import tqdm
except ImportError:
    print("Установите зависимости: pip install aiodns aiosmtplib tqdm")
    sys.exit(1)


# ─── Настройки ────────────────────────────────────────────────────────────────

DEFAULT_WORKERS = 50          # Параллельных воркеров
SMTP_TIMEOUT = 10             # Секунд на соединение/команду
FROM_EMAIL = "verify@check.local"  # От кого (не важно, но нужно для SMTP)
HELLO_HOST = "check.local"    # EHLO hostname

# Домены, которые принимают всё (catch-all или антихарвест) — пропускаем SMTP
SKIP_SMTP_DOMAINS = {
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.ru", "yahoo.co.uk",
    "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me",
    "mail.ru", "inbox.ru", "list.ru", "bk.ru",
    "yandex.ru", "yandex.com", "ya.ru",
}


# ─── Статусы ──────────────────────────────────────────────────────────────────

class Status(str, Enum):
    VALID        = "valid"          # SMTP подтвердил существование
    INVALID      = "invalid"        # SMTP вернул 5xx — адрес не существует
    CATCH_ALL    = "catch_all"      # Домен принимает всё (не можем проверить)
    SKIPPED      = "skipped"        # Крупный провайдер — пропущен
    NO_MX        = "no_mx"          # У домена нет MX-записей
    DOMAIN_ERROR = "domain_error"   # Домен не резолвится
    SMTP_ERROR   = "smtp_error"     # Ошибка соединения / таймаут
    SYNTAX_ERROR = "syntax_error"   # Невалидный формат адреса


STATUS_COLORS = {
    Status.VALID:        "✅",
    Status.INVALID:      "❌",
    Status.CATCH_ALL:    "⚠️",
    Status.SKIPPED:      "⏭️",
    Status.NO_MX:        "🚫",
    Status.DOMAIN_ERROR: "🚫",
    Status.SMTP_ERROR:   "⚡",
    Status.SYNTAX_ERROR: "🔤",
}


@dataclass
class Result:
    email: str
    status: Status
    detail: str = ""
    mx_host: str = ""


# ─── Валидация синтаксиса ─────────────────────────────────────────────────────

EMAIL_RE = re.compile(
    r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
)

def is_valid_syntax(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


# ─── DNS lookup ───────────────────────────────────────────────────────────────

async def get_mx_records(domain: str, resolver: aiodns.DNSResolver) -> list[str]:
    """Возвращает список MX-хостов отсортированных по приоритету."""
    try:
        records = await resolver.query(domain, "MX")
        sorted_mx = sorted(records, key=lambda r: r.priority)
        return [r.host.rstrip(".") for r in sorted_mx]
    except aiodns.error.DNSError:
        return []


# ─── SMTP проверка ────────────────────────────────────────────────────────────

async def smtp_verify(email: str, mx_host: str) -> tuple[bool | None, str]:
    """
    Возвращает:
      True  — адрес существует (250)
      False — адрес не существует (5xx)
      None  — не удалось определить (ошибка, таймаут, 4xx)
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(mx_host, 25),
            timeout=SMTP_TIMEOUT
        )

        async def read_response():
            lines = []
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=SMTP_TIMEOUT)
                line = line.decode(errors="replace").rstrip()
                lines.append(line)
                # Последняя строка многострочного ответа не имеет "-" после кода
                if len(line) < 4 or line[3] != "-":
                    break
            return lines

        async def send(cmd: str):
            writer.write((cmd + "\r\n").encode())
            await writer.drain()

        # Читаем приветствие
        await read_response()

        # EHLO
        await send(f"EHLO {HELLO_HOST}")
        await read_response()

        # MAIL FROM
        await send(f"MAIL FROM:<{FROM_EMAIL}>")
        from_resp = await read_response()
        from_code = int(from_resp[-1][:3]) if from_resp else 0

        if from_code != 250:
            writer.close()
            return None, f"MAIL FROM rejected: {from_resp[-1] if from_resp else 'no response'}"

        # RCPT TO — ключевой момент
        await send(f"RCPT TO:<{email}>")
        rcpt_resp = await read_response()
        rcpt_code = int(rcpt_resp[-1][:3]) if rcpt_resp else 0

        # QUIT
        try:
            await send("QUIT")
            await asyncio.wait_for(reader.read(256), timeout=2)
        except Exception:
            pass
        writer.close()

        if rcpt_code == 250 or rcpt_code == 251:
            return True, rcpt_resp[-1]
        elif 500 <= rcpt_code < 600:
            return False, rcpt_resp[-1]
        else:
            return None, f"Ambiguous response: {rcpt_resp[-1] if rcpt_resp else 'no response'}"

    except asyncio.TimeoutError:
        return None, "Connection timeout"
    except ConnectionRefusedError:
        return None, "Connection refused"
    except OSError as e:
        return None, f"Network error: {e}"
    except Exception as e:
        return None, f"Unexpected error: {e}"


# ─── Catch-all detection ──────────────────────────────────────────────────────

async def is_catch_all(domain: str, mx_host: str) -> bool:
    """Проверяем, принимает ли сервер несуществующий случайный адрес."""
    fake = f"no-such-user-xk9z2q@{domain}"
    result, _ = await smtp_verify(fake, mx_host)
    return result is True  # Принял несуществующий — catch-all


# ─── Основная логика проверки одного адреса ───────────────────────────────────

async def verify_email(email: str, resolver: aiodns.DNSResolver) -> Result:
    email = email.strip().lower()

    # 1. Синтаксис
    if not is_valid_syntax(email):
        return Result(email, Status.SYNTAX_ERROR, "Invalid email format")

    domain = email.split("@")[1]

    # 2. Пропускаем крупных провайдеров
    if domain in SKIP_SMTP_DOMAINS:
        return Result(email, Status.SKIPPED, "Major provider, SMTP check skipped")

    # 3. MX lookup
    mx_hosts = await get_mx_records(domain, resolver)
    if not mx_hosts:
        # Пробуем A-запись напрямую как fallback
        try:
            await resolver.query(domain, "A")
            mx_hosts = [domain]  # Иногда MX не настроен, но A есть
        except aiodns.error.DNSError:
            return Result(email, Status.DOMAIN_ERROR, f"Domain {domain} does not resolve")
        return Result(email, Status.NO_MX, f"No MX records for {domain}")

    mx_host = mx_hosts[0]

    # 4. Catch-all detection
    if await is_catch_all(domain, mx_host):
        return Result(email, Status.CATCH_ALL, "Domain accepts all addresses", mx_host)

    # 5. SMTP verify
    exists, detail = await smtp_verify(email, mx_host)

    if exists is True:
        return Result(email, Status.VALID, detail, mx_host)
    elif exists is False:
        return Result(email, Status.INVALID, detail, mx_host)
    else:
        return Result(email, Status.SMTP_ERROR, detail, mx_host)


# ─── Worker pool ──────────────────────────────────────────────────────────────

async def worker(
    queue: asyncio.Queue,
    results: list,
    resolver: aiodns.DNSResolver,
    pbar,
    semaphore: asyncio.Semaphore,
):
    while True:
        email = await queue.get()
        if email is None:
            queue.task_done()
            break
        try:
            async with semaphore:
                result = await verify_email(email, resolver)
            results.append(result)
        except Exception as e:
            results.append(Result(email, Status.SMTP_ERROR, str(e)))
        finally:
            queue.task_done()
            pbar.update(1)


# ─── Чтение CSV ───────────────────────────────────────────────────────────────

def read_emails_from_csv(path: str, column: Optional[str] = None) -> list[str]:
    emails = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            # Нет заголовков — читаем первую колонку
            f.seek(0)
            for row in csv.reader(f):
                if row:
                    emails.append(row[0].strip())
            return emails

        # Определяем нужную колонку
        if column:
            col = column
        else:
            # Ищем колонку с "email" в названии
            col = next(
                (c for c in reader.fieldnames if "email" in c.lower()),
                reader.fieldnames[0]
            )

        for row in reader:
            val = row.get(col, "").strip()
            if val:
                emails.append(val)

    return emails


# ─── Сохранение результатов ───────────────────────────────────────────────────

def save_results(results: list[Result], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "status", "detail", "mx_host"])
        for r in results:
            writer.writerow([r.email, r.status.value, r.detail, r.mx_host])


def print_summary(results: list[Result], elapsed: float):
    from collections import Counter
    counts = Counter(r.status for r in results)
    total = len(results)

    print("\n" + "═" * 50)
    print(f"  ИТОГО: {total} адресов за {elapsed:.1f}с")
    print("═" * 50)
    for status in Status:
        if counts[status]:
            icon = STATUS_COLORS[status]
            print(f"  {icon}  {status.value:<15} {counts[status]:>6}  ({counts[status]/total*100:.1f}%)")
    print("═" * 50)


# ─── main ─────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Async SMTP Email Verifier")
    parser.add_argument("input", help="Входной CSV-файл с email-адресами")
    parser.add_argument("--output", "-o", default="results.csv", help="Выходной CSV (default: results.csv)")
    parser.add_argument("--column", "-c", help="Название колонки с email (по умолчанию ищем 'email')")
    parser.add_argument("--workers", "-w", type=int, default=DEFAULT_WORKERS, help=f"Параллельных воркеров (default: {DEFAULT_WORKERS})")
    parser.add_argument("--resume", "-r", action="store_true", help="Пропустить уже проверенные адреса из output-файла")
    parser.add_argument("--verbose", "-v", action="store_true", help="Подробный лог")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # Читаем входной файл
    print(f"📂 Читаем {args.input}...")
    emails = read_emails_from_csv(args.input, args.column)
    print(f"   Найдено {len(emails)} адресов")

    # Resume mode: пропускаем уже проверенные
    already_done = set()
    if args.resume:
        try:
            with open(args.output, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    already_done.add(row["email"].strip().lower())
            print(f"   Пропускаем {len(already_done)} уже проверенных")
        except FileNotFoundError:
            pass

    emails_to_check = [e for e in emails if e.strip().lower() not in already_done]
    print(f"   К проверке: {len(emails_to_check)}")

    if not emails_to_check:
        print("Нечего проверять!")
        return

    # Настраиваем DNS resolver
    resolver = aiodns.DNSResolver(timeout=5, tries=2)

    # Очередь и воркеры
    queue: asyncio.Queue = asyncio.Queue()
    results: list[Result] = []
    semaphore = asyncio.Semaphore(args.workers)

    start = time.time()

    with tqdm(total=len(emails_to_check), desc="Проверка", unit="email") as pbar:
        # Запускаем воркеры
        tasks = [
            asyncio.create_task(worker(queue, results, resolver, pbar, semaphore))
            for _ in range(args.workers)
        ]

        # Наполняем очередь
        for email in emails_to_check:
            await queue.put(email)

        # Сигнал завершения для каждого воркера
        for _ in range(args.workers):
            await queue.put(None)

        await queue.join()
        for task in tasks:
            task.cancel()

    elapsed = time.time() - start

    # Дописываем к уже существующим результатам (если resume)
    write_mode = "a" if args.resume and already_done else "w"
    if write_mode == "w":
        save_results(results, args.output)
    else:
        with open(args.output, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for r in results:
                writer.writerow([r.email, r.status.value, r.detail, r.mx_host])

    print_summary(results, elapsed)
    print(f"\n💾 Результаты сохранены в {args.output}")
    print(f"⚡ Скорость: {len(results)/elapsed:.0f} адресов/сек")


if __name__ == "__main__":
    asyncio.run(main())
