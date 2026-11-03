import itertools
import logging
import re
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlparse

import requests
from selenium import webdriver
from selenium.webdriver.firefox.options import Options

import user_config

# Shared string constants to enforce DRY across all modules
LOGGER_NAME: Final[str] = "auto_buyer"
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)-8s | %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

STORE_AMAZON: Final[str] = "amazon"
STORE_FNAC: Final[str] = "fnac"

PARSER_LXML: Final[str] = "lxml"
PAGE_LOAD_EAGER: Final[str] = "eager"
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
MSG_TELEGRAM_BOUGHT: Final[str] = "Order successfully completed ✅🛒\n{url}"
MSG_TELEGRAM_FAILED: Final[str] = (
    "Automated checkout failed during browser step ❌\n{url}"
)
MSG_TELEGRAM_HEARTBEAT: Final[str] = "Monitor is active and running ⚙️:\n🗓 {timestamp}"
MSG_TELEGRAM_STOPPED: Final[str] = (
    "Monitor stopped because all target products were processed 🏁🚀"
)
MSG_TELEGRAM_TEST: Final[str] = "Telegram notification test succeeded ✅"

PRICE_REGEX: Final[re.Pattern[str]] = re.compile(r"(\d+)(?:[,.€\s]\s*(\d{2}))?")

ARG_HEADLESS: Final[str] = "-headless"
ARG_PROFILE: Final[str] = "-profile"
FIREFOX_PROFILE_DIR: Final[str] = "firefox_profile"


@dataclass(frozen=True)
class TargetProduct:
    """Represents a product URL, its price ceiling, and its resolved store."""

    url: str
    max_price: float
    domain: str
    store_name: str
    login_url: str


class StoreProvider(Protocol):
    """Common interface that every store module must implement."""

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


def setup_logger(verbose: bool = False) -> logging.Logger:
    """Configure and return the application logger."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = SpinnerSafeStreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


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


def sleep_with_spinner(seconds: float, reason: str) -> None:
    """Pause execution with an animated spinner so the user knows the app is alive."""
    with loading_step(reason):
        time.sleep(seconds)


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


def resolve_target_product(url: str, max_price: float) -> TargetProduct:
    """Detect the store and regional login endpoint from any product URL."""
    hostname = urlparse(url).netloc.lower()
    for domain_key, login_url in user_config.LOGIN_URLS_BY_DOMAIN.items():
        if domain_key in hostname:
            store_name = STORE_AMAZON if STORE_AMAZON in domain_key else STORE_FNAC
            return TargetProduct(
                url=url,
                max_price=max_price,
                domain=domain_key,
                store_name=store_name,
                login_url=login_url,
            )
    msg = f"Unsupported store or regional domain in URL: {url}"
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
    if not user_config.BOT_TOKEN or "REPLACE" in user_config.BOT_TOKEN:
        logger.debug(
            "Telegram token not configured, skipping notification: %s", bot_message
        )
        return False

    endpoint = TELEGRAM_API_TEMPLATE.format(token=user_config.BOT_TOKEN)
    payload = {
        "chat_id": user_config.BOT_CHAT_ID,
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
