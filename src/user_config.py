import os
from pathlib import Path
from typing import Any, Final

import tomlkit
from dotenv import load_dotenv
from tomlkit.exceptions import TOMLKitError

dotenv_path = Path(__file__, '.env')
load_dotenv('.env')

# Default configuration file path and environment variable name
DEFAULT_CONFIG_FILENAME: Final[str] = "config.toml"
ENV_CONFIG_PATH: Final[str] = "AUTO_BUYER_CONFIG"
ENCODING_UTF: Final[str] = "utf-8"

# TOML section and field keys (DRY)
KEY_LOGIN_URLS: Final[str] = "login_urls"
KEY_WATCHED_PRODUCTS: Final[str] = "watched_products"
KEY_PRODUCT_URL: Final[str] = "url"
KEY_MAX_PRICE: Final[str] = "max_price"
KEY_BOUGHT: Final[str] = "bought"

# Telegram bot configuration
BOT_TOKEN: Final[str] = os.getenv("TG_BOT_TOKEN", "")
BOT_CHAT_ID: Final[str] = os.getenv("TG_BOT_CHAT_ID", "")

# Store authentication credentials
AMAZON_EMAIL: Final[str] = os.getenv("AMAZON_EMAIL", "")
AMAZON_PASSWORD: Final[str] = os.getenv("AMAZON_PASSWORD", "")

FNAC_EMAIL: Final[str] = os.getenv("FNAC_EMAIL", "")
FNAC_PASSWORD: Final[str] = os.getenv("FNAC_PASSWORD", "")

# Built-in fallback login URLs if omitted from config.toml
DEFAULT_LOGIN_URLS: Final[dict[str, str]] = {
    "amazon.fr": "https://www.amazon.fr/ap/signin?openid.pape.max_auth_age=0&openid.return_to=https%3A%2F%2Fwww.amazon.fr%2F%3Fref_%3Dnav_signin&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.assoc_handle=frflex&openid.mode=checkid_setup&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0",
    "amazon.de": "https://www.amazon.de/ap/signin?openid.pape.max_auth_age=0&openid.return_to=https%3A%2F%2Fwww.amazon.de%2F%3Fref_%3Dnav_signin&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.assoc_handle=deflex&openid.mode=checkid_setup&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0",
    "amazon.it": "https://www.amazon.it/ap/signin?openid.pape.max_auth_age=0&openid.return_to=https%3A%2F%2Fwww.amazon.it%2F%3Fref_%3Dnav_signin&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.assoc_handle=itflex&openid.mode=checkid_setup&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0",
    "amazon.es": "https://www.amazon.es/ap/signin?openid.pape.max_auth_age=0&openid.return_to=https%3A%2F%2Fwww.amazon.es%2F%3Fref_%3Dnav_signin&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.assoc_handle=esflex&openid.mode=checkid_setup&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0",
    "fnac.com": "https://secure.fnac.com/identity/server/gateway/signin",
}


def _load_raw_toml(config_path: Path) -> dict[str, Any]:
    """Read and parse the TOML file into native Python structures via tomlkit."""
    if not config_path.is_file():
        return {}
    try:
        content = config_path.read_text(encoding=ENCODING_UTF)
        return tomlkit.parse(content).unwrap()
    except (OSError, TOMLKitError) as exc:
        msg = f"Failed to load configuration file {config_path}: {exc}"
        raise RuntimeError(msg) from exc


def _parse_login_urls(raw_data: dict[str, Any]) -> dict[str, str]:
    """Merge default regional login URLs with any user overrides from TOML."""
    merged_urls = dict(DEFAULT_LOGIN_URLS)
    custom_urls = raw_data.get(KEY_LOGIN_URLS)
    if isinstance(custom_urls, dict):
        for domain, url in custom_urls.items():
            if isinstance(domain, str) and isinstance(url, str):
                merged_urls[domain.strip().lower()] = url.strip()
    return merged_urls


def _parse_watched_products(raw_data: dict[str, Any]) -> list[tuple[str, float]]:
    """Extract active watched product tuples that have not been marked as bought."""
    products: list[tuple[str, float]] = []
    raw_products = raw_data.get(KEY_WATCHED_PRODUCTS)
    if not isinstance(raw_products, list):
        return products

    for entry in raw_products:
        if not isinstance(entry, dict):
            continue
        if entry.get(KEY_BOUGHT) is True:
            continue
        url = entry.get(KEY_PRODUCT_URL)
        max_price = entry.get(KEY_MAX_PRICE)
        if isinstance(url, str) and isinstance(max_price, (int, float)):
            products.append((url.strip(), float(max_price)))
    return products


def mark_product_as_bought(target_url: str, config_path: Path | None = None) -> None:
    """Persist bought=true in config.toml while preserving comments and formatting."""
    path = config_path or _CONFIG_PATH
    if not path.is_file():
        return
    try:
        doc = tomlkit.parse(path.read_text(encoding=ENCODING_UTF))
        products = doc.get(KEY_WATCHED_PRODUCTS)
        if isinstance(products, list):
            for item in products:
                if isinstance(item, dict) and item.get(KEY_PRODUCT_URL) == target_url:
                    item[KEY_BOUGHT] = True
            path.write_text(tomlkit.dumps(doc), encoding=ENCODING_UTF)
    except (OSError, TOMLKitError):
        return


_CONFIG_PATH: Final[Path] = Path(os.getenv(ENV_CONFIG_PATH, DEFAULT_CONFIG_FILENAME)).resolve()
_RAW_CONFIG: Final[dict[str, Any]] = _load_raw_toml(_CONFIG_PATH)

LOGIN_URLS_BY_DOMAIN: Final[dict[str, str]] = _parse_login_urls(_RAW_CONFIG)
WATCHED_PRODUCTS: Final[list[tuple[str, float]]] = _parse_watched_products(_RAW_CONFIG)