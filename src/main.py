import argparse
import sys
import time
from typing import Final

import requests

import config_logic
from common import (
    MSG_TELEGRAM_AVAILABLE,
    MSG_TELEGRAM_BOUGHT,
    MSG_TELEGRAM_BUYING,
    MSG_TELEGRAM_FAILED,
    MSG_TELEGRAM_HEARTBEAT,
    MSG_TELEGRAM_STOPPED,
    MSG_TELEGRAM_TEST,
    STORE_AMAZON,
    STORE_FNAC,
    StoreProvider,
    TargetProduct,
    logger,
    resolve_target_product,
    setup_logger,
    sleep_with_spinner,
    telegram_bot_sendtext,
)
from stores.amazon_store import AmazonStore
from stores.fnac_store import FnacStore

MODE_MANUAL: Final[str] = "manual"
MODE_LOOP: Final[str] = "loop"
HEARTBEAT_INTERVAL_SECONDS: Final[float] = 3600.0


def build_store_registry(headless: bool) -> dict[str, StoreProvider]:
    """Instantiate and map store providers by store identifier."""
    return {
        STORE_AMAZON: AmazonStore(headless=headless),
        STORE_FNAC: FnacStore(headless=headless),
    }


def load_targets(
    cli_url: str | None, cli_max_price: float | None
) -> list[TargetProduct]:
    """Build the list of validated target products from CLI arguments or user_config."""
    raw_pairs: list[tuple[str, float]] = (
        [(cli_url, cli_max_price if cli_max_price is not None else 9999.0)]
        if cli_url is not None
        else list(config_logic.WATCHED_PRODUCTS)
    )

    targets: list[TargetProduct] = []
    for url, max_price in raw_pairs:
        targets.append(resolve_target_product(url=url, max_price=max_price))
    return targets


def attempt_single_target(
    session: requests.Session,
    target: TargetProduct,
    provider: StoreProvider,
    dry_run: bool,
    direct_buy: bool,
) -> bool:
    """Check availability (unless direct_buy is enabled) and trigger purchase if eligible."""
    if not direct_buy:
        detected_price = provider.check_stock_and_price(session=session, target=target)
        if detected_price is None or detected_price > target.max_price:
            return False

        logger.info(
            "Target eligible for purchase: %s at %.2f", target.url, detected_price
        )
        telegram_bot_sendtext(
            MSG_TELEGRAM_AVAILABLE.format(
                price=detected_price,
                limit=target.max_price,
                url=target.url,
            )
        )

    telegram_bot_sendtext(MSG_TELEGRAM_BUYING)
    bought = provider.purchase_product(target=target, dry_run=dry_run)
    if bought:
        if not dry_run:
            config_logic.mark_product_as_bought(target.url)
            logger.info(
                "Marked product as bought in configuration file: %s", target.url
            )
        telegram_bot_sendtext(MSG_TELEGRAM_BOUGHT.format(url=target.url))
        return True

    telegram_bot_sendtext(MSG_TELEGRAM_FAILED.format(url=target.url))
    return False


def run_application(args: argparse.Namespace) -> int:
    """Execute either a manual single pass or a continuous background monitoring loop."""
    setup_logger(verbose=args.verbose)
    logger.info(
        "Starting application (mode=%s, dry_run=%s, direct_buy=%s)",
        args.mode,
        args.dry_run,
        args.direct_buy,
    )

    if args.test_telegram:
        logger.info("Sending test notification to Telegram")
        success = telegram_bot_sendtext(MSG_TELEGRAM_TEST)
        return 0 if success else 1

    try:
        targets = load_targets(cli_url=args.url, cli_max_price=args.max_price)
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    if not targets:
        logger.info(
            "No active unbought products found in configuration. Nothing to monitor."
        )
        return 0

    providers = build_store_registry(headless=args.headless)
    pending = [True for _ in targets]
    last_heartbeat = time.time()

    with requests.Session() as session:
        while any(pending):
            for idx, target in enumerate(targets):
                if not pending[idx]:
                    continue

                provider = providers[target.store_name]
                purchased = attempt_single_target(
                    session=session,
                    target=target,
                    provider=provider,
                    dry_run=args.dry_run,
                    direct_buy=args.direct_buy,
                )
                if purchased:
                    pending[idx] = False

            # In manual mode, stop immediately after the first pass
            if args.mode == MODE_MANUAL:
                break

            if not any(pending):
                telegram_bot_sendtext(MSG_TELEGRAM_STOPPED)
                break

            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL_SECONDS:
                timestamp = time.ctime(int(time.time()))
                telegram_bot_sendtext(
                    MSG_TELEGRAM_HEARTBEAT.format(timestamp=timestamp)
                )
                last_heartbeat = time.time()

            sleep_with_spinner(
                seconds=args.interval,
                reason="Waiting before next stock check cycle",
            )

    completed_count = pending.count(False)
    logger.info(
        "Application finished cleanly (purchased/verified %d of %d targets)",
        completed_count,
        len(targets),
    )
    return 0


def parse_cli_args() -> argparse.Namespace:
    """Parse command-line options for manual execution, background loop, and testing."""
    parser = argparse.ArgumentParser(description="Modular multi-store auto-buyer")
    parser.add_argument(
        "--mode",
        choices=[MODE_MANUAL, MODE_LOOP],
        default=MODE_MANUAL,
        help="Execution mode: single manual pass or continuous background loop",
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="Optional product URL to check/buy immediately, overriding config.toml",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=None,
        help="Maximum price ceiling when --url is provided",
    )
    parser.add_argument(
        "--direct-buy",
        action="store_true",
        help="Skip HTTP scraping check and launch Selenium purchase immediately",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Test mode: perform all steps and login without clicking final payment confirmation",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test message to Telegram and exit immediately",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Delay in seconds between polling cycles in loop mode",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Selenium Firefox in headless mode",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging output",
    )
    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_cli_args()
    try:
        sys.exit(run_application(cli_args))
    except KeyboardInterrupt:
        logger.info("Application interrupted by user signal, shutting down cleanly")
        sys.exit(0)
