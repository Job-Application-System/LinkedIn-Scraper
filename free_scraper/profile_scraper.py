import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from linkedin_scraper import (
    BrowserManager,
    CompanyScraper,
    PersonScraper,
    RateLimitError,
    is_logged_in,
)


SESSION_FILE = Path("session.json")
DEBUG_DIR = Path("AI-POC/scraped_profiles/debug")
LOGIN_URL = "https://www.linkedin.com/login"


async def save_debug_page(browser: BrowserManager, name: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    html_path = DEBUG_DIR / f"{name}.html"
    screenshot_path = DEBUG_DIR / f"{name}.png"
    info_path = DEBUG_DIR / f"{name}.json"
    html_path.write_text(await browser.page.content(), encoding="utf-8")
    info = await browser.page.evaluate(
        """() => ({
            url: location.href,
            title: document.title,
            inputs: Array.from(document.querySelectorAll('input')).map((input) => ({
                type: input.type,
                name: input.name,
                id: input.id,
                autocomplete: input.autocomplete,
                placeholder: input.placeholder,
                ariaLabel: input.getAttribute('aria-label'),
                visible: !!(input.offsetWidth || input.offsetHeight || input.getClientRects().length)
            })),
            buttons: Array.from(document.querySelectorAll('button')).map((button) => ({
                type: button.type,
                text: button.innerText,
                ariaLabel: button.getAttribute('aria-label'),
                visible: !!(button.offsetWidth || button.offsetHeight || button.getClientRects().length)
            }))
        })"""
    )
    info_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    await browser.page.screenshot(path=str(screenshot_path), full_page=True)
    print(f"Saved login debug files to {html_path}, {screenshot_path}, and {info_path}")


async def fill_first_available(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(f"{selector}:visible").last
            await locator.wait_for(state="visible", timeout=3000)
            await locator.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await locator.press_sequentially(value, delay=35)
            return True
        except Exception:
            continue
    return False


async def fill_login_form_with_dom(page, email: str, password: str) -> bool:
    return await page.evaluate(
        """({ email, password }) => {
            const isVisible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible);
            const emailInput = inputs.find((input) =>
                input.type === 'email' ||
                input.name === 'session_key' ||
                (input.autocomplete || '').includes('username')
            );
            const passwordInput = inputs.find((input) =>
                input.type === 'password' ||
                input.name === 'session_password' ||
                (input.autocomplete || '').includes('current-password')
            );
            if (!emailInput || !passwordInput) {
                return false;
            }
            for (const [input, value] of [[emailInput, email], [passwordInput, password]]) {
                input.focus();
                input.value = value;
                input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                input.blur();
            }
            return true;
        }""",
        {"email": email, "password": password},
    )


async def click_sign_in_with_dom(page) -> bool:
    return await page.evaluate(
        """() => {
            const isVisible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible);
            const button = buttons.find((candidate) =>
                (candidate.type === 'submit' && !/microsoft|google|apple/i.test(candidate.innerText || candidate.textContent || '')) ||
                /^sign\\s*in$/i.test((candidate.innerText || candidate.textContent || '').trim())
            );
            if (!button) {
                return false;
            }
            button.click();
            return true;
        }"""
    )


async def click_linkedin_sign_in(page) -> bool:
    candidates = [
        "button:visible:has-text('Sign in')",
        "button:visible:has-text('Sign In')",
        "button:visible",
    ]
    for selector in candidates:
        try:
            count = await page.locator(selector).count()
            for index in range(count - 1, -1, -1):
                button = page.locator(selector).nth(index)
                text = (await button.inner_text()).strip()
                if text.lower() == "sign in":
                    await button.click()
                    return True
        except Exception:
            continue
    return await click_sign_in_with_dom(page)


async def programmatic_login(browser: BrowserManager, email: str, password: str) -> None:
    page = browser.page
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)

    if await is_logged_in(page):
        return

    email_filled = await fill_first_available(
        page,
        [
            "#username",
            "input[name='session_key']",
            "input[autocomplete='username']",
            "input[autocomplete*='username']",
            "input[type='email']",
        ],
        email,
    )
    password_filled = await fill_first_available(
        page,
        [
            "#password",
            "input[name='session_password']",
            "input[autocomplete='current-password']",
            "input[autocomplete*='current-password']",
            "input[type='password']",
        ],
        password,
    )
    if not email_filled or not password_filled:
        dom_filled = await fill_login_form_with_dom(page, email=email, password=password)
        email_filled = email_filled or dom_filled
        password_filled = password_filled or dom_filled

    if not email_filled or not password_filled:
        await save_debug_page(browser, "linkedin_login_form_not_found")
        raise RuntimeError(
            "Login form not found in headless browser. LinkedIn likely served a "
            "checkpoint, captcha, or changed login page. See debug HTML/screenshot."
        )

    if not await click_linkedin_sign_in(page):
        await save_debug_page(browser, "linkedin_login_submit_not_found")
        raise RuntimeError("Login submit button not found. See debug HTML/screenshot.")

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(5000)

    if not await is_logged_in(page):
        await save_debug_page(browser, "linkedin_login_not_authenticated")
        raise RuntimeError(
            "Programmatic login did not reach an authenticated LinkedIn page. "
            "LinkedIn may require 2FA, captcha, checkpoint verification, or may block headless login."
        )


async def authenticate(browser: BrowserManager, force_login: bool = False) -> None:
    load_dotenv()

    if SESSION_FILE.is_dir():
        raise RuntimeError(
            f"{SESSION_FILE} is a directory, but it must be a JSON session file. "
            "Remove or rename that directory before running the scraper."
        )

    if SESSION_FILE.exists() and not force_login:
        await browser.load_session(str(SESSION_FILE))
        return

    email = os.getenv("LINKEDIN_EMAIL")
    password = os.getenv("LINKEDIN_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "Missing LinkedIn credentials. Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD in .env."
        )

    try:
        await programmatic_login(browser, email=email, password=password)
    except RateLimitError:
        await save_debug_page(browser, "linkedin_rate_limited_during_login")
        raise RuntimeError(
            "LinkedIn rate limiter was detected during login. Stop running the scraper "
            "for now; repeated retries usually make the block last longer."
        )
    await browser.save_session(str(SESSION_FILE))


async def scrape(profile_url: str, profile_type: str = "person", force_login: bool = False) -> dict:
    async with BrowserManager(headless=True) as browser:
        try:
            await authenticate(browser, force_login=force_login)

            await asyncio.sleep(5)

            match profile_type:
                case "person":
                    scraper = PersonScraper(browser.page)
                    person = await scraper.scrape(profile_url)

                    print(f"Name: {person.name}")
                    print(f"Headline: {person.job_title}")
                    print(f"Location: {person.location}")
                    print(f"Experiences: {len(person.experiences)}")
                    print(f"Education: {len(person.educations)}")

                    return person.to_dict()
                case "company":
                    scraper = CompanyScraper(browser.page)
                    company = await scraper.scrape(profile_url)

                    print(f"Company: {company.name}")
                    print(f"Industry: {company.industry}")
                    print(f"Size: {company.company_size}")
                    about = company.about_us or ""
                    print(f"About: {about[:200]}...")

                    return company.to_dict()
                case _:
                    raise NotImplementedError(
                        f"Profile type '{profile_type}' is not supported. Use 'person' or 'company'."
                    )
        except RateLimitError:
            await save_debug_page(browser, "linkedin_rate_limited_during_scrape")
            raise RuntimeError(
                "LinkedIn rate limiter was detected during scraping. Stop running the "
                "script for a while; repeated retries usually make the block last longer."
            )


def save_result(data: dict, output: str) -> None:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Saved result to {output_path}")


if __name__ == "__main__":
    force_login = False
    scrape_company = False

    profile_url = "https://www.linkedin.com/in/kelvin-mock-548666173/"
    profile_output = "AI-POC/scraped_profiles/linkedin_kelvin.json"
    profile_data = asyncio.run(
        scrape(
            profile_url=profile_url,
            profile_type="person",
            force_login=force_login,
        )
    )
    save_result(profile_data, profile_output)

    if scrape_company:
        company_url = "https://www.linkedin.com/company/amazon-web-services/"
        company_output = "AI-POC/scraped_profiles/linkedin_aws.json"
        company_data = asyncio.run(
            scrape(
                profile_url=company_url,
                profile_type="company",
                force_login=force_login,
            )
        )
        save_result(company_data, company_output)
