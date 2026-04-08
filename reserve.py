import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config (injected via env vars in GitHub Actions) ──────────────────────────
BASE_URL  = "https://agenbot.net/deporyatenis"
USERNAME  = os.environ["TENNIS_USER"]
PASSWORD  = os.environ["TENNIS_PASS"]
COURT     = "5"          # Court number to reserve
HOUR      = "19:00"      # 7 PM  (19:00 in 24h)
DAYS_AHEAD = 1           # Book 1 day from today
TZ        = ZoneInfo("America/Montevideo")

# ── Helpers ───────────────────────────────────────────────────────────────────
def target_date() -> str:
    """Returns tomorrow's date as DD/MM/YYYY (site formaat)."""
    d = datetime.now(TZ) + timedelta(days=DAYS_AHEAD)
    return d.strftime("%d/%m/%Y")

async def screenshot(page, name: str):
    path = f"screenshots/{name}.png"
    os.makedirs("screenshots", exist_ok=True)
    await page.screenshot(path=path, full_page=True)
    print(f"  📸  {path}")

# ── Main flow ─────────────────────────────────────────────────────────────────
async def main():
    date_str = target_date()
    print(f"🎾  Reserving Court {COURT} at {HOUR} on {date_str}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="es-UY",
        )
        page = await context.new_page()

        # ── 1. LOGIN ──────────────────────────────────────────────────────────
        print("1️⃣  Logging in …")
        await page.goto(f"{BASE_URL}/login.aspx", wait_until="networkidle")
        await screenshot(page, "01_login_page")

        # Exact field IDs confirmed from page source (GeneXus AJAX app)
        await page.fill("#vUSERNAME", USERNAME)
        print("   ✔ username filled (#vUSERNAME)")

        await page.fill("#vUSERPASSWORD", PASSWORD)
        print("   ✔ password filled (#vUSERPASSWORD)")

        # Login button is input[type="button"] — NOT a submit button
        await page.click("#BTNENTER")
        print("   ✔ login button clicked (#BTNENTER)")

        await page.wait_for_load_state("networkidle")
        await screenshot(page, "02_after_login")

        current = page.url
        if "login" in current.lower():
            os.makedirs("screenshots", exist_ok=True)
            html2 = await page.content()
            with open("screenshots/login_failed_source.html", "w", encoding="utf-8") as f:
                f.write(html2)
            raise RuntimeError("❌ Login failed — credentials may be wrong or session was blocked.")
        print(f"   ✔ Logged in → {current}")

        # ── 2. NAVIGATE TO RESERVATIONS ───────────────────────────────────────
        print("2️⃣  Looking for reservation section …")
        await screenshot(page, "03_home")

        # Try common navigation links
        for text in ["Reservar", "Reservas", "Canchas", "Turnos", "Booking", "Courts"]:
            loc = page.locator(f"a:has-text('{text}'), button:has-text('{text}')")
            if await loc.count() > 0:
                await loc.first.click()
                await page.wait_for_load_state("networkidle")
                print(f"   ✔ Clicked nav link: '{text}'")
                await screenshot(page, "04_reservations_page")
                break

        # ── 3. SELECT DATE ────────────────────────────────────────────────────
        print(f"3️⃣  Selecting date {date_str} …")

        # Try a visible date input
        for sel in [
            "input[type='date']",
            "input[placeholder*='fecha' i]",
            "input[id*='date' i]",
            "input[id*='fecha' i]",
            "input[class*='date' i]",
        ]:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    # Convert to YYYY-MM-DD for HTML date inputs
                    d = datetime.now(TZ) + timedelta(days=DAYS_AHEAD)
                    await loc.fill(d.strftime("%Y-%m-%d"))
                    print(f"   ✔ date input: {sel}")
                    await page.wait_for_load_state("networkidle")
                    await screenshot(page, "05_date_selected")
                    break
            except Exception:
                continue

        # Fallback: look for a clickable "tomorrow" cell in a calendar
        try:
            d = datetime.now(TZ) + timedelta(days=DAYS_AHEAD)
            day_num = str(d.day)
            cal_day = page.locator(
                f"td:has-text('{day_num}'), "
                f"div[class*='day']:has-text('{day_num}'), "
                f"button[aria-label*='{day_num}']"
            ).first
            if await cal_day.is_visible():
                await cal_day.click()
                print(f"   ✔ Clicked calendar day {day_num}")
                await page.wait_for_load_state("networkidle")
                await screenshot(page, "05_date_selected")
        except Exception:
            pass

        # ── 4. SELECT COURT 5 ─────────────────────────────────────────────────
        print(f"4️⃣  Selecting Court {COURT} …")
        for sel in [
            f"*:has-text('Cancha {COURT}')",
            f"*:has-text('Court {COURT}')",
            f"option[value='{COURT}']",
            f"[id*='court'][value='{COURT}']",
            f"[id*='cancha'][value='{COURT}']",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "option":
                        # It's inside a <select> — select by value
                        select = page.locator(f"select:has(option[value='{COURT}'])")
                        await select.select_option(value=COURT)
                    else:
                        await loc.click()
                    print(f"   ✔ Court selector: {sel}")
                    await page.wait_for_load_state("networkidle")
                    await screenshot(page, "06_court_selected")
                    break
            except Exception:
                continue

        # ── 5. SELECT TIME SLOT 19:00 ─────────────────────────────────────────
        print(f"5️⃣  Selecting time slot {HOUR} …")
        hour_variants = [HOUR, "19:00 hs", "7:00 PM", "7 PM", "19 hs", "19h"]
        for variant in hour_variants:
            try:
                loc = page.locator(
                    f"*:has-text('{variant}'):not(html):not(body):not(div > div > div)"
                ).first
                if await loc.is_visible(timeout=2000):
                    await loc.click()
                    print(f"   ✔ Time slot clicked: '{variant}'")
                    await page.wait_for_load_state("networkidle")
                    await screenshot(page, "07_time_selected")
                    break
            except Exception:
                continue

        # ── 6. CONFIRM RESERVATION ────────────────────────────────────────────
        print("6️⃣  Confirming reservation …")
        for sel in [
            "button:has-text('Confirmar')",
            "button:has-text('Reservar')",
            "button:has-text('Aceptar')",
            "input[type='submit'][value*='onfirm' i]",
            "input[type='submit'][value*='eserv' i]",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=3000):
                    await loc.click()
                    print(f"   ✔ Confirm button: {sel}")
                    await page.wait_for_load_state("networkidle")
                    await screenshot(page, "08_confirmed")
                    break
            except Exception:
                continue

        await screenshot(page, "09_final_state")
        print(f"\n✅  Done! Court {COURT} on {date_str} at {HOUR}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
