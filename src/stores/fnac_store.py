from typing import Final

import requests
from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import user_config
from common import (
    DEFAULT_HEADERS,
    PARSER_LXML,
    TargetProduct,
    create_firefox_driver,
    loading_step,
    logger,
    price_conversion,
)

SEL_FNAC_PRICE: Final[str] = (
    ".f-faPriceBox__price, .userPrice, [data-automation-id='product-price']"
)
SEL_FNAC_BUY_BTN: Final[str] = (
    "button.js-ProductBuy-add, [data-automation-id='add-to-cart-btn']"
)
FNAC_OFFICIAL_SELLER_MARKER: Final[str] = "vendu et expédié par fnac"
FNAC_BASKET_URL: Final[str] = "https://secure.fnac.com/basket/app/basket"

CSS_FNAC_EMAIL: Final[str] = "input[type='email'], input[name='email']"
CSS_FNAC_PASSWORD: Final[str] = "input[type='password'], input[name='password']"
CSS_FNAC_ADD_CART: Final[str] = (
    "button.js-ProductBuy-add, [data-automation-id='add-to-cart-btn']"
)


class FnacStore:
    """Store provider implementation for Fnac.com."""

    def __init__(self, headless: bool = False) -> None:
        """Initialize the Fnac store provider."""
        self.headless = headless

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
                response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("HTTP error while checking Fnac URL %s: %s", target.url, exc)
            return None

        soup = BeautifulSoup(response.text, features=PARSER_LXML)
        page_text = soup.get_text(" ", strip=True).lower()

        if FNAC_OFFICIAL_SELLER_MARKER not in page_text:
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

    def purchase_product(self, target: TargetProduct, dry_run: bool) -> bool:
        """Log into Fnac with Selenium Firefox, add the product to cart, and open checkout."""
        logger.info("Launching Firefox browser for Fnac checkout")
        driver = create_firefox_driver(headless=self.headless)
        wait = WebDriverWait(driver, 10)

        try:
            with loading_step("Authenticating on Fnac.com"):
                driver.get(target.login_url)
                email_field = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, CSS_FNAC_EMAIL))
                )
                email_field.send_keys(user_config.FNAC_EMAIL + Keys.RETURN)

                password_field = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, CSS_FNAC_PASSWORD))
                )
                password_field.send_keys(user_config.FNAC_PASSWORD + Keys.RETURN)

            with loading_step(
                "Loading Fnac product page and locating Add to Cart button"
            ):
                driver.get(target.url)
                add_btn = wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, CSS_FNAC_ADD_CART))
                )

            if dry_run:
                logger.info(
                    "Dry-run active: Fnac Add to Cart button verified. Stopping before click."
                )
                return True

            with loading_step("Adding product to Fnac basket and opening checkout"):
                add_btn.click()
                driver.get(FNAC_BASKET_URL)
                logger.info("Product added to Fnac basket and checkout page opened")
                return True

        except (TimeoutException, WebDriverException) as exc:
            logger.error("Selenium error during Fnac checkout: %s", exc)
            return False
        finally:
            driver.quit()
