from __future__ import annotations

import argparse
import csv
import json
import random
import re
import smtplib
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def decode_smtp_message(message: bytes | str | None) -> str:
    if message is None:
        return ""
    if isinstance(message, bytes):
        return message.decode("utf-8", errors="replace").strip()
    return str(message).strip()


def run_command(args: list[str], timeout: int = 8) -> str:
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")


def resolve_mx_hosts_doh(domain: str) -> list[tuple[int, str]]:
    query = urlencode({"name": domain, "type": "MX"})
    url = f"https://dns.google/resolve?{query}"
    req = Request(url, headers={"Accept": "application/dns-json", "User-Agent": "leadheatapi/1.0"})
    try:
        with urlopen(req, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return []

    answers = payload.get("Answer") or []
    out: list[tuple[int, str]] = []
    for answer in answers:
        data = str(answer.get("data", "")).strip()
        if not data:
            continue
        parts = data.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pref = int(parts[0])
        host = parts[1].rstrip(".").lower()
        if host:
            out.append((pref, host))
    return out


def resolve_mx_hosts(domain: str) -> list[str]:
    mx_records: list[tuple[int, str]] = []

    mx_records.extend(resolve_mx_hosts_doh(domain))

    if not mx_records:
        dig_out = run_command(["dig", "+short", "MX", domain])
        for line in dig_out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            pref_raw, host_raw = parts[0], parts[1]
            if not pref_raw.isdigit():
                continue
            host = host_raw.rstrip(".").lower()
            if host:
                mx_records.append((int(pref_raw), host))

    if not mx_records:
        ns_out = run_command(["nslookup", "-type=mx", domain])
        for line in ns_out.splitlines():
            line = line.strip()
            if "mail exchanger" not in line:
                continue
            # Example: example.com   mail exchanger = 10 mx.example.com.
            tail = line.split("mail exchanger", 1)[-1]
            if "=" not in tail:
                continue
            value = tail.split("=", 1)[-1].strip()
            parts = value.split()
            if len(parts) < 2 or not parts[0].isdigit():
                continue
            host = parts[1].rstrip(".").lower()
            if host:
                mx_records.append((int(parts[0]), host))

    if mx_records:
        mx_records.sort(key=lambda item: item[0])
        seen: set[str] = set()
        ordered_hosts: list[str] = []
        for _, host in mx_records:
            if host in seen:
                continue
            seen.add(host)
            ordered_hosts.append(host)
        return ordered_hosts

    return [domain.lower()]


def classify_smtp_code(code: int | None) -> str:
    if code is None:
        return "network_error"
    if code in {250, 251, 252}:
        return "accepted"
    if code in {550, 551, 552, 553, 554}:
        return "rejected"
    if code in {421, 450, 451, 452}:
        return "temporary_failure"
    if 200 <= code < 300:
        return "accepted"
    if 500 <= code < 600:
        return "rejected"
    if 400 <= code < 500:
        return "temporary_failure"
    return "unknown"


def smtp_rcpt_check(
    *,
    mx_host: str,
    mail_from: str,
    rcpt_to: str,
    helo_domain: str,
    timeout_seconds: float,
    smtp_port: int,
    use_starttls: bool,
) -> dict[str, Any]:
    started = time.time()
    try:
        with smtplib.SMTP(mx_host, smtp_port, timeout=timeout_seconds) as smtp:
            smtp.ehlo(helo_domain)
            if use_starttls and smtp.has_extn("starttls"):
                smtp.starttls()
                smtp.ehlo(helo_domain)
            mail_code, mail_msg = smtp.mail(mail_from)
            if mail_code >= 400:
                return {
                    "ok": False,
                    "code": mail_code,
                    "message": decode_smtp_message(mail_msg),
                    "step": "MAIL FROM",
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            rcpt_code, rcpt_msg = smtp.rcpt(rcpt_to)
            smtp.noop()
            return {
                "ok": True,
                "code": rcpt_code,
                "message": decode_smtp_message(rcpt_msg),
                "step": "RCPT TO",
                "elapsed_ms": int((time.time() - started) * 1000),
            }
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        return {
            "ok": False,
            "code": None,
            "message": f"{type(exc).__name__}: {exc}",
            "step": "CONNECT",
            "elapsed_ms": int((time.time() - started) * 1000),
        }


def detect_catch_all(
    *,
    domain: str,
    mx_hosts: list[str],
    mail_from: str,
    helo_domain: str,
    timeout_seconds: float,
    smtp_port: int,
    use_starttls: bool,
    max_mx_attempts: int,
) -> dict[str, Any]:
    probe_local = f"zzprobe-{uuid.uuid4().hex[:14]}"
    probe_email = f"{probe_local}@{domain}"
    attempts = 0
    for mx in mx_hosts[: max(1, max_mx_attempts)]:
        attempts += 1
        result = smtp_rcpt_check(
            mx_host=mx,
            mail_from=mail_from,
            rcpt_to=probe_email,
            helo_domain=helo_domain,
            timeout_seconds=timeout_seconds,
            smtp_port=smtp_port,
            use_starttls=use_starttls,
        )
        status = classify_smtp_code(result["code"])
        if status in {"accepted", "rejected"}:
            return {
                "probe_email": probe_email,
                "mx_host": mx,
                "status": status,
                "code": result["code"],
                "message": result["message"],
                "attempts": attempts,
            }
    return {
        "probe_email": probe_email,
        "mx_host": mx_hosts[0] if mx_hosts else "",
        "status": "unknown",
        "code": None,
        "message": "No decisive response from SMTP server for catch-all probe.",
        "attempts": attempts,
    }


def build_final_status(rcpt_status: str, catch_all_status: str) -> str:
    if rcpt_status == "rejected":
        return "invalid"
    if rcpt_status == "accepted":
        if catch_all_status == "accepted":
            return "catch_all"
        return "valid_like"
    if rcpt_status == "temporary_failure":
        return "unknown"
    if rcpt_status == "network_error":
        return "unknown"
    return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "SMTP RCPT check (no message delivery): runs EHLO, MAIL FROM, RCPT TO, QUIT "
            "for each email candidate and writes statuses."
        )
    )
    parser.add_argument(
        "--input",
        default="output/generated_email_candidates_from_egrul_people_flat.csv",
        help="Input CSV with email candidates.",
    )
    parser.add_argument(
        "--output",
        default="output/generated_email_candidates_smtp_probe.csv",
        help="Output CSV path.",
    )
    parser.add_argument(
        "--email-column",
        default="email_candidate",
        help="Column name with candidate email.",
    )
    parser.add_argument(
        "--mail-from",
        default="admin@deffun.ru",
        help="MAIL FROM envelope sender used during SMTP session.",
    )
    parser.add_argument(
        "--helo-domain",
        default="deffun.ru",
        help="HELO/EHLO domain.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=8.0,
        help="Socket timeout for each SMTP connection.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.25,
        help="Pause between checks to reduce server load.",
    )
    parser.add_argument(
        "--smtp-port",
        type=int,
        default=25,
        help="SMTP port to use (normally 25).",
    )
    parser.add_argument(
        "--no-starttls",
        action="store_true",
        help="Disable STARTTLS attempt even if server supports it.",
    )
    parser.add_argument(
        "--max-mx-attempts",
        type=int,
        default=2,
        help="How many MX hosts to try before giving up on a candidate.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Optional limit for number of processed rows (0 = all).",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Shuffle input rows before checking.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for --shuffle.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise SystemExit(f"Input CSV not found: {input_path}")
    if not EMAIL_RE.match(args.mail_from):
        raise SystemExit(f"Invalid --mail-from address: {args.mail_from}")

    with input_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        input_fields = list(reader.fieldnames or [])

    if not rows:
        raise SystemExit("Input CSV is empty.")
    if args.email_column not in input_fields:
        raise SystemExit(f"Email column not found: {args.email_column}")

    if args.shuffle:
        random.seed(args.seed)
        random.shuffle(rows)
    if args.max_rows > 0:
        rows = rows[: args.max_rows]

    mx_cache: dict[str, list[str]] = {}
    catch_all_cache: dict[str, dict[str, Any]] = {}
    out_rows: list[dict[str, str]] = []

    total = len(rows)
    for idx, row in enumerate(rows, start=1):
        candidate = (row.get(args.email_column) or "").strip().lower()
        if not EMAIL_RE.match(candidate):
            out = dict(row)
            out.update(
                {
                    "probe_email": candidate,
                    "probe_domain": "",
                    "mx_hosts": "",
                    "mx_used": "",
                    "smtp_step": "",
                    "smtp_code": "",
                    "smtp_message": "Invalid email syntax",
                    "rcpt_status": "invalid_syntax",
                    "catch_all_status": "",
                    "catch_all_probe_email": "",
                    "catch_all_code": "",
                    "catch_all_message": "",
                    "final_status": "invalid",
                }
            )
            out_rows.append(out)
            continue

        local, domain = candidate.split("@", 1)
        mx_hosts = mx_cache.get(domain)
        if mx_hosts is None:
            mx_hosts = resolve_mx_hosts(domain)
            mx_cache[domain] = mx_hosts

        catch_all = catch_all_cache.get(domain)
        if catch_all is None:
            catch_all = detect_catch_all(
                domain=domain,
                mx_hosts=mx_hosts,
                mail_from=args.mail_from,
                helo_domain=args.helo_domain,
                timeout_seconds=args.timeout_seconds,
                smtp_port=args.smtp_port,
                use_starttls=not args.no_starttls,
                max_mx_attempts=args.max_mx_attempts,
            )
            catch_all_cache[domain] = catch_all

        probe_result: dict[str, Any] | None = None
        used_mx = ""
        for mx in mx_hosts[: max(1, args.max_mx_attempts)]:
            used_mx = mx
            probe_result = smtp_rcpt_check(
                mx_host=mx,
                mail_from=args.mail_from,
                rcpt_to=candidate,
                helo_domain=args.helo_domain,
                timeout_seconds=args.timeout_seconds,
                smtp_port=args.smtp_port,
                use_starttls=not args.no_starttls,
            )
            rcpt_status = classify_smtp_code(probe_result["code"])
            if rcpt_status in {"accepted", "rejected"}:
                break

        if probe_result is None:
            probe_result = {
                "code": None,
                "message": "No SMTP attempt performed",
                "step": "",
            }

        rcpt_status = classify_smtp_code(probe_result["code"])
        final_status = build_final_status(rcpt_status, catch_all.get("status", "unknown"))

        out = dict(row)
        out.update(
            {
                "probe_email": candidate,
                "probe_domain": domain,
                "mx_hosts": "; ".join(mx_hosts),
                "mx_used": used_mx,
                "smtp_step": str(probe_result.get("step", "")),
                "smtp_code": "" if probe_result.get("code") is None else str(probe_result["code"]),
                "smtp_message": str(probe_result.get("message", "")),
                "rcpt_status": rcpt_status,
                "catch_all_status": str(catch_all.get("status", "")),
                "catch_all_probe_email": str(catch_all.get("probe_email", "")),
                "catch_all_code": "" if catch_all.get("code") is None else str(catch_all.get("code")),
                "catch_all_message": str(catch_all.get("message", "")),
                "final_status": final_status,
            }
        )
        out_rows.append(out)

        if idx % 10 == 0 or idx == total:
            print(f"Processed {idx}/{total}")
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    extra_fields = [
        "probe_email",
        "probe_domain",
        "mx_hosts",
        "mx_used",
        "smtp_step",
        "smtp_code",
        "smtp_message",
        "rcpt_status",
        "catch_all_status",
        "catch_all_probe_email",
        "catch_all_code",
        "catch_all_message",
        "final_status",
    ]
    fieldnames = list(input_fields)
    for field in extra_fields:
        if field not in fieldnames:
            fieldnames.append(field)

    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    final_counts: dict[str, int] = {}
    for row in out_rows:
        key = row.get("final_status", "") or "unknown"
        final_counts[key] = final_counts.get(key, 0) + 1
    print(f"Saved {len(out_rows)} rows to {output_path}")
    print(f"Status summary: {final_counts}")


if __name__ == "__main__":
    main()
