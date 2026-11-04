from http import HTTPStatus
from typing import Final

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config_logic
from common import (
    DEFAULT_HEADERS,
    PARSER_LXML,
    TargetProduct,
    create_firefox_driver,
    loading_step,
    logger,
    price_conversion,
    sleep_with_spinner,
)

STORE_NAME_FNAC: Final[str] = "fnac"
FNAC_DOMAINS: Final[tuple[str, ...]] = ("fnac.com",)

SEL_FNAC_PRICE: Final[str] = (
    ".f-faPriceBox__price, .userPrice, [data-automation-id='product-price']"
)
SEL_FNAC_BUY_BTN: Final[str] = (
    "button.js-ProductBuy-add, [data-automation-id='add-to-cart-btn']"
)
FNAC_OFFICIAL_SELLER_MARKERS: Final[tuple[str, ...]] = (
    "vendu et expédié par fnac",
    "vendu par fnac",
)
FNAC_BASKET_URL: Final[str] = "https://secure.fnac.com/basket/app/basket"
DATADOME_MARKER: Final[str] = "captcha-delivery.com"

CSS_FNAC_COOKIE_ACCEPT: Final[str] = "#onetrust-accept-btn-handler"
CSS_FNAC_EMAIL: Final[str] = "input[type='email'], input[name='email']"
CSS_FNAC_PASSWORD: Final[str] = "input[type='password'], input[name='password']"
MANUAL_CHECKOUT_HOLD_SECONDS: Final[float] = 90.0


class FnacStore:
    """Store provider implementation for Fnac.com."""

    def __init__(self, headless: bool = False) -> None:
        """Initialize the Fnac store provider."""
        self.headless = headless

    @property
    def store_name(self) -> str:
        """Return unique identifier for Fnac store."""
        return STORE_NAME_FNAC

    @property
    def supported_domains(self) -> tuple[str, ...]:
        """Return tuple of supported Fnac domains."""
        return FNAC_DOMAINS

    def check_stock_and_price(
        self,
        session: requests.Session,
        target: TargetProduct,
    ) -> float | None:
        """Scrape the Fnac product page and return the price if available from official Fnac."""
        try:
            with loading_step(f"Checking Fnac ({target.domain}) product page"):
                response = session.get(target.url, headers=DEFAULT_HEADERS, timeout=10)
                session.cookies.clear()
                if response.status_code == HTTPStatus.FORBIDDEN:
                    logger.warning(
                        "Fnac blocked the lightweight HTTP check (DataDome). "
                        "Use --direct-buy for Fnac links to check via Firefox."
                    )
                    return None
                response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("HTTP error while checking Fnac URL %s: %s", target.url, exc)
            return None

        soup = BeautifulSoup(response.text, features=PARSER_LXML)
        page_text = soup.get_text(" ", strip=True).lower()

        if not any(marker in page_text for marker in FNAC_OFFICIAL_SELLER_MARKERS):
            logger.info("Status on Fnac: Not sold directly by official Fnac seller")
            return None

        if soup.select_one(SEL_FNAC_BUY_BTN) is None:
            logger.info("Status on Fnac: Add-to-cart button unavailable")
            return None

        price_tag = soup.select_one(SEL_FNAC_PRICE)
        if price_tag is None:
            logger.warning(
                "Fnac product appears in stock but price element was not found"
            )
            return None

        detected_price = price_conversion(price_tag.get_text(strip=True))
        if detected_price is None:
            logger.warning("Unable to parse Fnac price string")
            return None

        logger.info(
            "Detected Fnac stock at %.2f (limit: %.2f)",
            detected_price,
            target.max_price,
        )
        return detected_price

    def _wait_for_datadome_if_present(self, driver: webdriver.Firefox) -> None:
        """Pause if DataDome displays an anti-bot challenge so the user can solve it once."""
        if DATADOME_MARKER in driver.page_source.lower():
            logger.warning(
                "Fnac anti-bot challenge detected. Please solve the puzzle in the Firefox window."
            )
            with loading_step(
                "Waiting for manual DataDome puzzle resolution in Firefox"
            ):
                WebDriverWait(driver, 90).until(
                    lambda d: DATADOME_MARKER not in d.page_source.lower()
                )

    def _dismiss_cookie_banner(self, driver: webdriver.Firefox) -> None:
        """Accept the OneTrust cookie banner on Fnac if visible."""
        buttons = driver.find_elements(By.CSS_SELECTOR, CSS_FNAC_COOKIE_ACCEPT)
        if buttons and buttons[0].is_displayed():
            buttons[0].click()

    def _authenticate_if_prompted(
        self, driver: webdriver.Firefox, wait: WebDriverWait
    ) -> None:  # type: ignore[type-arg]
        """Fill Fnac login credentials if the browser is currently on the sign-in gateway."""
        email_inputs = driver.find_elements(By.CSS_SELECTOR, CSS_FNAC_EMAIL)
        if not email_inputs or not email_inputs[0].is_displayed():
            return

        if not config_logic.FNAC_EMAIL or not config_logic.FNAC_PASSWORD:
            logger.warning(
                "Fnac credentials not set in environment; please log in manually in Firefox"
            )
            return

        logger.info("Submitting Fnac credentials")
        email_inputs[0].clear()
        email_inputs[0].send_keys(config_logic.FNAC_EMAIL + Keys.RETURN)

        try:
            password_field = wait.until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, CSS_FNAC_PASSWORD))
            )
            password_field.clear()
            password_field.send_keys(config_logic.FNAC_PASSWORD + Keys.RETURN)
        except TimeoutException:
            logger.debug("Fnac password input did not appear immediately")

    def purchase_product(self, target: TargetProduct, dry_run: bool) -> bool:
        """Open Fnac product page in Firefox, verify stock and price, and add to basket."""
        logger.info("Launching Firefox browser for Fnac checkout")
        driver = create_firefox_driver(headless=self.headless)
        wait = WebDriverWait(driver, 12)

        try:
            with loading_step("Opening Fnac product page"):
                driver.get(target.url)
                self._wait_for_datadome_if_present(driver)
                self._dismiss_cookie_banner(driver)

            # Check stock and seller directly in the DOM before any login attempt
            add_buttons = driver.find_elements(By.CSS_SELECTOR, SEL_FNAC_BUY_BTN)
            if not add_buttons or not add_buttons[0].is_displayed():
                logger.info(
                    "Status on Fnac: Product is out of stock (Add to Cart button absent)"
                )
                return False

            body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            if not any(marker in body_text for marker in FNAC_OFFICIAL_SELLER_MARKERS):
                logger.warning(
                    "Aborting Fnac checkout: Offer is not sold by official Fnac"
                )
                return False

            price_elements = driver.find_elements(By.CSS_SELECTOR, SEL_FNAC_PRICE)
            if price_elements:
                detected_price = price_conversion(price_elements[0].text)
                if detected_price is not None and detected_price > target.max_price:
                    logger.warning(
                        "Aborting Fnac checkout: Price %.2f exceeds limit %.2f",
                        detected_price,
                        target.max_price,
                    )
                    return False

            if dry_run:
                logger.info(
                    "Dry-run active: Fnac Add to Cart button verified. Stopping before click."
                )
                return True

            with loading_step("Adding product to Fnac basket and opening checkout"):
                add_buttons[0].click()
                driver.get(FNAC_BASKET_URL)
                self._wait_for_datadome_if_present(driver)
                self._authenticate_if_prompted(driver, wait)

            logger.info(
                "Product added to Fnac basket. Complete final payment in the open Firefox window."
            )
            sleep_with_spinner(
                seconds=MANUAL_CHECKOUT_HOLD_SECONDS,
                reason="Holding Firefox open so you can confirm payment on Fnac",
            )
            return True

        except (TimeoutException, WebDriverException) as exc:
            logger.error("Selenium error during Fnac checkout: %s", exc)
            return False
        finally:
            driver.quit()
