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

        # GeneXus apps need type() with delay — fill() bypasses JS event handlers
        await page.locator("#vUSERNAME").click()
        await page.locator("#vUSERNAME").type(USERNAME, delay=100)
        await page.locator("#vUSERNAME").press("Tab")
        print("   ✔ username typed")

        await asyncio.sleep(0.5)

        await page.locator("#vUSERPASSWORD").click()
        await page.locator("#vUSERPASSWORD").type(PASSWORD, delay=100)
        await page.locator("#vUSERPASSWORD").press("Tab")
        print("   ✔ password typed")

        await asyncio.sleep(1)

        # Click login and wait for navigation away from login page
        try:
            async with page.expect_navigation(timeout=20000):
                await page.locator("#BTNENTER").click()
            print("   ✔ navigation detected after login")
        except PlaywrightTimeout:
            print("   ⚠ No full navigation — checking URL anyway")

        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "02_after_login")

        current = page.url
        print(f"   Current URL: {current}")

        if "login" in current.lower():
            err_text = ""
            for err_sel in [".ErrorViewer", ".gx_ev", "[class*='error' i]"]:
                try:
                    loc = page.locator(err_sel)
                    if await loc.count() > 0:
                        err_text = await loc.first.inner_text()
                        if err_text.strip():
                            break
                except Exception:
                    pass
            os.makedirs("screenshots", exist_ok=True)
            html2 = await page.content()
            with open("screenshots/login_failed_source.html", "w", encoding="utf-8") as f:
                f.write(html2)
            raise RuntimeError(f"❌ Login failed. Site message: '{err_text.strip()}'")

        print(f"   ✔ Logged in → {current}")

        # ── 2. NAVIGATE TO RESERVATIONS ───────────────────────────────────────
        print("2️⃣  Looking for reservation section …")
        await screenshot(page, "03_home")

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

        for sel in [
            "input[type='date']",
            "input[placeholder*='fecha' i]",
            "input[id*='date' i]",
            "input[id*='fecha' i]",
        ]:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    d = datetime.now(TZ) + timedelta(days=DAYS_AHEAD)
                    await loc.fill(d.strftime("%Y-%m-%d"))
                    print(f"   ✔ date input: {sel}")
                    await page.wait_for_load_state("networkidle")
                    await screenshot(page, "05_date_selected")
                    break
            except Exception:
                continue

        try:
            d = datetime.now(TZ) + timedelta(days=DAYS_AHEAD)
            day_num = str(d.day)
            cal_day = page.locator(
                f"td:has-text('{day_num}'), div[class*='day']:has-text('{day_num}')"
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
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "option":
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
        for variant in [HOUR, "19:00 hs", "7:00 PM", "7 PM", "19 hs"]:
            try:
                loc = page.locator(
                    f"*:has-text('{variant}'):not(html):not(body)"
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
            "input[value*='onfirm' i]",
            "input[value*='eserv' i]",
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
