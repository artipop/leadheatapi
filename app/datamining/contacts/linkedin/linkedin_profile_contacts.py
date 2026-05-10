import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from linkedin_scraper import (
    AuthenticationError,
    BrowserManager,
    PersonScraper,
    is_logged_in,
    login_with_credentials,
    wait_for_manual_login,
)

DEFAULT_PROFILE_URL = "https://www.linkedin.com/in/---/"
DEFAULT_SESSION_FILE = ".linkedin/session.json"
DEFAULT_OUTPUT_FILE = ".linkedin/contacts.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test script: scrape LinkedIn profile and extract contacts."
    )
    parser.add_argument(
        "--profile-url",
        default=DEFAULT_PROFILE_URL,
        help="LinkedIn profile URL to scrape.",
    )
    parser.add_argument(
        "--session-file",
        default=DEFAULT_SESSION_FILE,
        help="Path to Playwright storage_state JSON file.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help="Where to save resulting JSON.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode.",
    )
    parser.add_argument(
        "--manual-login-timeout-sec",
        type=int,
        default=300,
        help="How long to wait for manual login before failing.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def _group_contacts(contacts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in contacts:
        grouped.setdefault(item["type"], []).append(
            {"value": item["value"], "label": item.get("label")}
        )
    return grouped


async def _ensure_authenticated(
    browser: BrowserManager, session_file: Path, manual_login_timeout_sec: int
) -> str:
    if session_file.exists():
        try:
            await browser.load_session(str(session_file))
            await browser.page.goto(
                "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
            )
            if await is_logged_in(browser.page):
                logging.info("Authenticated using saved session: %s", session_file)
                return "saved_session"
            logging.warning("Saved session exists but appears unauthenticated.")
        except Exception as exc:
            logging.warning("Failed to load saved session: %s", exc)

    email = os.getenv("LINKEDIN_EMAIL") or os.getenv("LINKEDIN_USERNAME")
    password = os.getenv("LINKEDIN_PASSWORD")
    if email and password:
        try:
            await login_with_credentials(browser.page, email=email, password=password)
            await browser.save_session(str(session_file))
            logging.info("Authenticated using LINKEDIN_EMAIL/LINKEDIN_PASSWORD.")
            return "credentials"
        except AuthenticationError as exc:
            logging.warning("Credential login failed, falling back to manual: %s", exc)

    await browser.page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    logging.info("Please login in opened browser window...")
    await wait_for_manual_login(browser.page, timeout=manual_login_timeout_sec * 1000)
    await browser.save_session(str(session_file))
    logging.info("Authenticated manually and session saved: %s", session_file)
    return "manual"


async def run(profile_url: str, session_file: Path, headless: bool, manual_login_timeout_sec: int) -> dict[str, Any]:
    async with BrowserManager(headless=headless, slow_mo=50 if not headless else 0) as browser:
        auth_method = await _ensure_authenticated(
            browser=browser,
            session_file=session_file,
            manual_login_timeout_sec=manual_login_timeout_sec,
        )

        scraper = PersonScraper(browser.page)
        await asyncio.sleep(2)
        person = await scraper.scrape(profile_url)
        print("person:")
        print(person.name)

        contacts = [c.model_dump() for c in person.contacts]
        result: dict[str, Any] = {
            "auth_method": auth_method,
            "profile_url": person.linkedin_url,
            "name": person.name,
            "location": person.location,
            "contacts": contacts,
            "contacts_by_type": _group_contacts(contacts),
        }
        return result


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    session_file = Path(args.session_file)
    output_file = Path(args.output)
    session_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    result = asyncio.run(
        run(
            profile_url=args.profile_url,
            session_file=session_file,
            headless=args.headless,
            manual_login_timeout_sec=args.manual_login_timeout_sec,
        )
    )

    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nSaved result to: {output_file}")


if __name__ == "__main__":
    main()
