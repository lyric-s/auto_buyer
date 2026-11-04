from typing import Final

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
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
)

STORE_NAME_AMAZON: Final[str] = "amazon"
AMAZON_DOMAINS: Final[tuple[str, ...]] = (
    "amazon.fr",
    "amazon.de",
    "amazon.it",
    "amazon.es",
    "amazon.co.uk",
    "amazon.com",
)

# Amazon DOM selectors for HTTP stock check
SEL_AVAILABILITY_OUT: Final[str] = (
    "#availability .a-color-state, #availability .a-color-price"
)
SEL_AVAILABILITY_SUCCESS: Final[str] = "#availability .a-color-success"
SEL_BUY_NOW_SPAN: Final[str] = "span#submit\\.buy-now-announce, input#buy-now-button"
SEL_MERCHANT_INFO: Final[str] = "#tabular-buybox, #merchant-info"
SEL_PRICE: Final[str] = (
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, "
    "#corePrice_feature_div .a-price .a-offscreen, "
    "#priceblock_ourprice, "
    ".a-price-whole"
)

# Amazon DOM selectors for Selenium authentication and checkout
CSS_COOKIE_ACCEPT: Final[str] = "#sp-cc-accept"
CSS_NAV_SIGNOUT: Final[str] = "#nav-item-signout"
CSS_NAV_ACCOUNT: Final[str] = "#nav-link-accountList"
CSS_EMAIL_INPUT: Final[str] = "input#ap_email, input#ap_email_login"
CSS_CONTINUE_BTN: Final[str] = "input#continue, span#continue input"
CSS_REMEMBER_ME: Final[str] = "input[name='rememberMe']"
CSS_PASSWORD_INPUT: Final[str] = "input#ap_password"
CSS_SIGNIN_SUBMIT: Final[str] = "input#signInSubmit"

ID_BUY_NOW_BTN: Final[str] = "buy-now-button"
ID_TURBO_IFRAME: Final[str] = "turbo-checkout-iframe"
ID_TURBO_CONFIRM_BTN: Final[str] = "turbo-checkout-pyo-button"
XPATH_FALLBACK_CONFIRM: Final[str] = (
    "//input[@name='placeYourOrder1'] | //*[@id='submitOrderButtonId']//input"
)

SIGNIN_URL_MARKERS: Final[tuple[str, ...]] = (
    "/ap/signin",
    "/ap/mfa",
    "/ap/cvf",
    "validatecaptcha",
)
CHALLENGE_URL_MARKERS: Final[tuple[str, ...]] = (
    "/ap/mfa",
    "/ap/cvf",
    "validatecaptcha",
    "passkey",
)

FALSE_AVAILABILITY_MARKERS: Final[tuple[str, ...]] = (
    "voir les offres de ces vendeurs",
    "available from these sellers",
    "erhältlich bei diesen anbietern",
    "disponibile presso questi venditori",
    "disponible a través de estos vendedores",
)


class AmazonStore:
    """Store provider implementation for all regional Amazon domains."""

    def __init__(self, headless: bool = False) -> None:
        """Initialize the Amazon store provider."""
        self.headless = headless

    @property
    def store_name(self) -> str:
        """Return unique identifier for Amazon store."""
        return STORE_NAME_AMAZON

    @property
    def supported_domains(self) -> tuple[str, ...]:
        """Return tuple of supported regional Amazon domains."""
        return AMAZON_DOMAINS

    def check_stock_and_price(
        self,
        session: requests.Session,
        target: TargetProduct,
    ) -> float | None:
        """Scrape the Amazon product page and return the price if eligible for purchase."""
        try:
            with loading_step(f"Checking Amazon ({target.domain}) product page"):
                response = session.get(target.url, headers=DEFAULT_HEADERS, timeout=10)
                session.cookies.clear()
                response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("HTTP error while checking %s: %s", target.url, exc)
            return None

        soup = BeautifulSoup(response.text, features=PARSER_LXML)

        out_of_stock_tag = soup.select_one(SEL_AVAILABILITY_OUT)
        if out_of_stock_tag is not None and out_of_stock_tag.get_text(strip=True):
            logger.info("Status on %s: Out of stock", target.domain)
            return None

        success_tag = soup.select_one(SEL_AVAILABILITY_SUCCESS)
        buy_now_tag = soup.select_one(SEL_BUY_NOW_SPAN)
        if success_tag is None and buy_now_tag is None:
            logger.info(
                "Status on %s: Unavailable or anti-bot page encountered", target.domain
            )
            return None

        availability_text = (
            success_tag.get_text(strip=True).lower() if success_tag is not None else ""
        )
        if any(marker in availability_text for marker in FALSE_AVAILABILITY_MARKERS):
            logger.info(
                "Status on %s: Third-party seller offer only, skipping", target.domain
            )
            return None

        merchant_tag = soup.select_one(SEL_MERCHANT_INFO)
        if (
            merchant_tag is not None
            and STORE_NAME_AMAZON not in merchant_tag.get_text(strip=True).lower()
        ):
            logger.info(
                "Status on %s: Buy Box is not held by official Amazon seller",
                target.domain,
            )
            return None

        price_tag = soup.select_one(SEL_PRICE)
        if price_tag is None:
            logger.warning(
                "Product appears available on %s but price element is missing",
                target.domain,
            )
            return None

        detected_price = price_conversion(price_tag.get_text(strip=True))
        if detected_price is None:
            logger.warning("Failed to parse price string on %s", target.domain)
            return None

        logger.info(
            "Detected Amazon stock at %.2f (limit: %.2f)",
            detected_price,
            target.max_price,
        )
        return detected_price

    def _dismiss_cookie_banner(self, driver: webdriver.Firefox) -> None:
        """Accept the Amazon cookie banner if present on screen."""
        cookie_buttons = driver.find_elements(By.CSS_SELECTOR, CSS_COOKIE_ACCEPT)
        if cookie_buttons and cookie_buttons[0].is_displayed():
            cookie_buttons[0].click()

    def _is_authenticated(self, driver: webdriver.Firefox) -> bool:
        """Check whether the browser session is already logged into Amazon."""
        if driver.find_elements(By.CSS_SELECTOR, CSS_NAV_SIGNOUT):
            return True
        account_links = driver.find_elements(By.CSS_SELECTOR, CSS_NAV_ACCOUNT)
        if account_links:
            href = (account_links[0].get_attribute("href") or "").lower()
            if "/ap/signin" not in href:
                return True
        return False

    def _verify_dom_price_and_seller(
        self,
        driver: webdriver.Firefox,
        target: TargetProduct,
    ) -> bool:
        """Verify seller and price directly inside the browser DOM before clicking Buy Now."""
        merchant_elements = driver.find_elements(By.CSS_SELECTOR, SEL_MERCHANT_INFO)
        if merchant_elements:
            merchant_text = merchant_elements[0].text.lower()
            if STORE_NAME_AMAZON not in merchant_text:
                logger.warning(
                    "Aborting in-browser checkout: Seller is not official Amazon"
                )
                return False

        price_elements = driver.find_elements(By.CSS_SELECTOR, SEL_PRICE)
        if price_elements:
            raw_price = (
                price_elements[0].get_attribute("textContent") or price_elements[0].text
            )
            detected_price = price_conversion(raw_price)
            if detected_price is not None and detected_price > target.max_price:
                logger.warning(
                    "Aborting in-browser checkout: Price %.2f exceeds limit %.2f",
                    detected_price,
                    target.max_price,
                )
                return False
        return True

    def _authenticate_if_needed(
        self, driver: webdriver.Firefox, target: TargetProduct
    ) -> None:
        """Complete the Amazon sign-in flow if the session is not already authenticated."""
        short_wait = WebDriverWait(driver, 5)
        standard_wait = WebDriverWait(driver, 12)

        email_inputs = driver.find_elements(By.CSS_SELECTOR, CSS_EMAIL_INPUT)
        if email_inputs and email_inputs[0].is_displayed():
            logger.info("Submitting Amazon email address on %s", target.domain)
            email_field = email_inputs[0]
            email_field.clear()
            email_field.send_keys(config_logic.AMAZON_EMAIL)

            continue_buttons = driver.find_elements(By.CSS_SELECTOR, CSS_CONTINUE_BTN)
            if continue_buttons:
                continue_buttons[0].click()
            else:
                email_field.submit()

        try:
            password_field = short_wait.until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, CSS_PASSWORD_INPUT))
            )
            logger.info("Submitting Amazon password on %s", target.domain)

            remember_boxes = driver.find_elements(By.CSS_SELECTOR, CSS_REMEMBER_ME)
            if remember_boxes and not remember_boxes[0].is_selected():
                remember_boxes[0].click()

            password_field.clear()
            password_field.send_keys(config_logic.AMAZON_PASSWORD)

            signin_btn = standard_wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, CSS_SIGNIN_SUBMIT))
            )
            signin_btn.click()
        except TimeoutException:
            logger.debug(
                "Password field did not appear immediately (URL: %s)",
                driver.current_url,
            )

        current_url = driver.current_url.lower()
        if any(marker in current_url for marker in CHALLENGE_URL_MARKERS):
            logger.warning(
                "Amazon security challenge detected (%s). Please complete it in the Firefox window.",
                driver.current_url,
            )
            with loading_step("Waiting for manual security verification in Firefox"):
                WebDriverWait(driver, 90).until(
                    lambda d: (
                        not any(m in d.current_url.lower() for m in SIGNIN_URL_MARKERS)
                    )
                )

    def purchase_product(self, target: TargetProduct, dry_run: bool) -> bool:
        """Open the product page in Firefox, authenticate only if needed, and execute checkout."""
        logger.info("Launching Firefox browser for Amazon (%s) checkout", target.domain)
        driver = create_firefox_driver(headless=self.headless)
        wait = WebDriverWait(driver, 12)

        try:
            with loading_step(f"Opening product page on {target.domain}"):
                driver.get(target.url)
                self._dismiss_cookie_banner(driver)

            # Authenticate only when the browser session is actually logged out
            if not self._is_authenticated(driver):
                logger.info(
                    "Browser session is not logged in, navigating to login page"
                )
                with loading_step(f"Authenticating on {target.domain}"):
                    driver.get(target.login_url)
                    self._authenticate_if_needed(driver, target)
                    driver.get(target.url)
                    self._dismiss_cookie_banner(driver)

            # Check whether the instant purchase button exists on the product page
            buy_buttons = driver.find_elements(By.ID, ID_BUY_NOW_BTN)
            if not buy_buttons or not buy_buttons[0].is_displayed():
                logger.info(
                    "Product is currently out of stock on %s (Buy Now button unavailable)",
                    target.domain,
                )
                return False

            if not self._verify_dom_price_and_seller(driver, target):
                return False

            buy_buttons[0].click()
            logger.info("Clicked Buy Now button")

            if any(
                marker in driver.current_url.lower() for marker in SIGNIN_URL_MARKERS
            ):
                logger.info("Amazon requested re-authentication after clicking Buy Now")
                with loading_step("Completing re-authentication"):
                    self._authenticate_if_needed(driver, target)

            if dry_run:
                logger.info(
                    "Dry-run active: reached checkout stage successfully (URL: %s). Stopping before final order.",
                    driver.current_url,
                )
                return True

            with loading_step("Confirming final order"):
                try:
                    wait.until(
                        EC.frame_to_be_available_and_switch_to_it(
                            (By.ID, ID_TURBO_IFRAME)
                        )
                    )
                    confirm_btn = wait.until(
                        EC.element_to_be_clickable((By.ID, ID_TURBO_CONFIRM_BTN))
                    )
                    confirm_btn.click()
                    logger.info("Order confirmed via Amazon Turbo Checkout modal")
                    return True
                except TimeoutException:
                    driver.switch_to.default_content()
                    fallback_btn = wait.until(
                        EC.element_to_be_clickable((By.XPATH, XPATH_FALLBACK_CONFIRM))
                    )
                    fallback_btn.click()
                    logger.info("Order confirmed via standard Amazon checkout page")
                    return True

        except TimeoutException:
            logger.error(
                "Timed out waiting for Amazon checkout element (Last URL: %s | Title: %s)",
                driver.current_url,
                driver.title,
            )
            return False
        except WebDriverException as exc:
            logger.error("Selenium WebDriver error during Amazon checkout: %s", exc.msg)
            return False
        finally:
            driver.quit()
