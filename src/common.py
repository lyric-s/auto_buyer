import itertools
import logging
import random
import re
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.firefox.options import Options

import config_logic

LOGGER_NAME: Final[str] = "auto_buyer"
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)-8s | %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

PARSER_LXML: Final[str] = "lxml"
PAGE_LOAD_EAGER: Final[str] = "eager"
ARG_HEADLESS: Final[str] = "-headless"
ARG_PROFILE: Final[str] = "-profile"
FIREFOX_PROFILE_DIR: Final[str] = "firefox_profile"

CARRIAGE_CLEAR: Final[str] = "\r\033[K"
SPINNER_FRAMES: Final[tuple[str, ...]] = (
    "⠋",
    "⠙",
    "⠹",
    "⠸",
    "⠼",
    "⠴",
    "⠦",
    "⠧",
    "⠇",
    "⠏",
)

DEFAULT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
}

TELEGRAM_API_TEMPLATE: Final[str] = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_PARSE_MODE: Final[str] = "Markdown"

MSG_TELEGRAM_AVAILABLE: Final[str] = (
    "Product available under target price ({price:.2f} <= {limit:.2f}):\n{url}"
)
MSG_TELEGRAM_BUYING: Final[str] = "Initiating automated checkout sequence 🔄🛒"
MSG_TELEGRAM_BOUGHT: Final[str] = "Order workflow completed successfully ✅🛒\n{url}"
MSG_TELEGRAM_FAILED: Final[str] = "Automated checkout did not complete ❌\n{url}"
MSG_TELEGRAM_HEARTBEAT: Final[str] = "Monitor is active and running ⚙️:\n🗓 {timestamp}"
MSG_TELEGRAM_STOPPED: Final[str] = (
    "Monitor stopped because all target products were processed 🏁🚀"
)
MSG_TELEGRAM_TEST: Final[str] = "Telegram notification test succeeded ✅"

PRICE_REGEX: Final[re.Pattern[str]] = re.compile(r"(\d+)(?:[,.€\s]\s*(\d{2}))?")


@dataclass(frozen=True)
class TargetProduct:
    """Represents a product URL, its price ceiling, and its resolved store metadata."""

    url: str
    max_price: float
    domain: str
    store_name: str
    login_url: str


class StoreProvider(Protocol):
    """Common interface that every store module must implement."""

    @property
    def store_name(self) -> str:
        """Unique identifier for the store module."""
        ...

    @property
    def supported_domains(self) -> tuple[str, ...]:
        """Domain suffixes handled by this store provider."""
        ...

    def check_stock_and_price(
        self,
        session: requests.Session,
        target: TargetProduct,
    ) -> float | None:
        """Return the detected price if available from official seller, else None."""
        ...

    def purchase_product(self, target: TargetProduct, dry_run: bool) -> bool:
        """Execute the Selenium checkout workflow and return True on success."""
        ...


class SpinnerSafeStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Stream handler that clears any active terminal spinner line before logging."""

    def emit(self, record: logging.LogRecord) -> None:
        sys.stdout.write(CARRIAGE_CLEAR)
        sys.stdout.flush()
        super().emit(record)



# Constants to add in common.py
JITTER_MIN_RATIO: Final[float] = 0.75
JITTER_MAX_RATIO: Final[float] = 1.25
LOGIN_POLL_SECONDS: Final[float] = 1.0
SCRIPT_OPEN_TAB: Final[str] = "window.open(arguments[0], '_blank');"


def run_interactive_login_session(domains: Sequence[str]) -> None:
    """Open Firefox with the persistent profile so the user can log in to stores manually."""
    unique_urls: list[str] = []
    for domain in domains:
        login_url = config_logic.LOGIN_URLS_BY_DOMAIN.get(domain)
        if login_url and login_url not in unique_urls:
            unique_urls.append(login_url)

    if not unique_urls:
        unique_urls = list(config_logic.LOGIN_URLS_BY_DOMAIN.values())

    logger.info("Launching interactive Firefox session to save store cookies")
    driver = create_firefox_driver(headless=False)

    try:
        first_url = unique_urls[0]
        driver.get(first_url)
        for extra_url in unique_urls[1:]:
            driver.execute_script(SCRIPT_OPEN_TAB, extra_url)

        logger.info(
            "Log in to your store accounts in the opened tabs, then close the Firefox window."
        )
        with loading_step("Waiting for you to close the Firefox window"):
            while True:
                try:
                    if not driver.window_handles:
                        break
                except WebDriverException:
                    break
                time.sleep(LOGIN_POLL_SECONDS)
    finally:
        try:
            driver.quit()
        except WebDriverException:
            pass
    logger.info("Firefox persistent profile saved successfully")

def setup_logger(verbose: bool = False) -> logging.Logger:
    """Configure and return the application logger."""
    app_logger = logging.getLogger(LOGGER_NAME)
    if not app_logger.handlers:
        handler = SpinnerSafeStreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
        app_logger.addHandler(handler)
    app_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return app_logger


logger = setup_logger()


@contextmanager
def loading_step(description: str) -> Iterator[None]:
    """Display an animated terminal spinner while a blocking operation runs."""
    stop_event = threading.Event()

    def render_spinner() -> None:
        frame_cycle = itertools.cycle(SPINNER_FRAMES)
        while not stop_event.is_set():
            frame = next(frame_cycle)
            sys.stdout.write(f"\r{frame} {description}...")
            sys.stdout.flush()
            stop_event.wait(0.1)
        sys.stdout.write(CARRIAGE_CLEAR)
        sys.stdout.flush()

    worker = threading.Thread(target=render_spinner, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop_event.set()
        worker.join()


def sleep_with_spinner(seconds: float, reason: str, apply_jitter: bool = True) -> None:
    """Pause execution with an animated spinner and optional random jitter."""
    effective_delay = (
        random.uniform(seconds * JITTER_MIN_RATIO, seconds * JITTER_MAX_RATIO)
        if apply_jitter
        else seconds
    )
    with loading_step(f"{reason} ({effective_delay:.1f}s)"):
        time.sleep(effective_delay)


def price_conversion(raw_price: str) -> float | None:
    """Convert a localized price string into a float value safely."""
    cleaned = raw_price.replace("\xa0", " ").replace("\n", ".").strip()
    match = PRICE_REGEX.search(cleaned)
    if match is None:
        return None
    whole_part = match.group(1)
    cents_part = match.group(2) or "00"
    try:
        return float(f"{whole_part}.{cents_part}")
    except ValueError:
        return None


def resolve_target_product(
    url: str,
    max_price: float,
    providers: Sequence[StoreProvider],
) -> TargetProduct:
    """Match a product URL against registered store providers and configured login URLs."""
    hostname = urlparse(url).netloc.lower()

    for provider in providers:
        for domain_key in provider.supported_domains:
            if domain_key in hostname:
                login_url = config_logic.LOGIN_URLS_BY_DOMAIN.get(domain_key, "")
                if not login_url:
                    msg = f"Missing login URL in configuration for domain: {domain_key}"
                    raise ValueError(msg)
                return TargetProduct(
                    url=url,
                    max_price=max_price,
                    domain=domain_key,
                    store_name=provider.store_name,
                    login_url=login_url,
                )

    msg = f"No registered store provider supports the domain in URL: {url}"
    raise ValueError(msg)


def create_firefox_driver(headless: bool = False) -> webdriver.Firefox:
    """Create a configured Selenium Firefox WebDriver instance with a persistent profile."""
    profile_path = Path(FIREFOX_PROFILE_DIR).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.page_load_strategy = PAGE_LOAD_EAGER
    if headless:
        options.add_argument(ARG_HEADLESS)
    options.add_argument(ARG_PROFILE)
    options.add_argument(str(profile_path))
    options.set_preference("dom.webdriver.enabled", False)
    options.set_preference("useAutomationExtension", False)
    return webdriver.Firefox(options=options)


def telegram_bot_sendtext(bot_message: str) -> bool:
    """Send a notification message via Telegram Bot API."""
    if not config_logic.BOT_TOKEN:
        logger.debug(
            "Telegram token not configured, skipping notification: %s", bot_message
        )
        return False

    endpoint = TELEGRAM_API_TEMPLATE.format(token=config_logic.BOT_TOKEN)
    payload = {
        "chat_id": config_logic.BOT_CHAT_ID,
        "parse_mode": TELEGRAM_PARSE_MODE,
        "text": bot_message,
    }

    try:
        response = requests.get(endpoint, params=payload, timeout=10)
        response.raise_for_status()
        logger.debug("Telegram notification delivered")
        return True
    except requests.RequestException as exc:
        logger.error("Failed to send Telegram notification: %s", exc)
        return False
