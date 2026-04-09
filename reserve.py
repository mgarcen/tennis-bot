import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL   = "https://agenbot.net/deporyatenis"
USERNAME   = os.environ["TENNIS_USER"]
PASSWORD   = os.environ["TENNIS_PASS"]
COURT      = "5"
HOUR       = "12:00"
DAYS_AHEAD = 1
PARTNER    = "Kevin Monzon"
TZ         = ZoneInfo("America/Montevideo")

def target_date():
    return (datetime.now(TZ) + timedelta(days=DAYS_AHEAD)).strftime("%d/%m/%Y")

async def screenshot(page, name):
    os.makedirs("screenshots", exist_ok=True)
    await page.screenshot(path=f"screenshots/{name}.png", full_page=True)
    print(f"  📸  {name}.png")

async def main():
    date_str = target_date()
    print(f"🎾  Reserving Court {COURT} at {HOUR} on {date_str}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="es-UY")
        page = await context.new_page()

        # ── 1. LOGIN ──────────────────────────────────────────────────────────
        print("1️⃣  Logging in …")
        await page.goto(f"{BASE_URL}/login.aspx", wait_until="networkidle")
        await page.locator("#vUSERNAME").click()
        await page.locator("#vUSERNAME").type(USERNAME, delay=100)
        await page.locator("#vUSERNAME").press("Tab")
        await asyncio.sleep(0.5)
        await page.locator("#vUSERPASSWORD").click()
        await page.locator("#vUSERPASSWORD").type(PASSWORD, delay=100)
        await page.locator("#vUSERPASSWORD").press("Tab")
        await asyncio.sleep(1)
        try:
            async with page.expect_navigation(timeout=20000):
                await page.locator("#BTNENTER").click()
        except PlaywrightTimeout:
            pass
        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        if "login" in page.url.lower():
            raise RuntimeError("❌ Login failed")
        print(f"   ✔ Logged in → {page.url}")

        # ── 2. DÍAS DISPONIBLES ───────────────────────────────────────────────
        print("2️⃣  Clicking Días disponibles …")
        await page.locator("#BTNBTNRESERVAR").click()
        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        print(f"   ✔ → {page.url}")

        # ── 3. SELECT LADRILLO ────────────────────────────────────────────────
        print("3️⃣  Selecting LADRILLO …")
        await page.locator("#vTIPOCANCHAID").select_option(label="LADRILLO")
        await asyncio.sleep(3)
        await page.wait_for_load_state("networkidle")
        print("   ✔ LADRILLO selected")

        # Navigate to target date only if not already there
        target_short = (datetime.now(TZ) + timedelta(days=DAYS_AHEAD)).strftime("%d/%m/%y")
        page_text = await page.inner_text("body")
        if target_short in page_text:
            print(f"   ✔ Already on {target_short}")
        else:
            await page.locator("#BTNBTNSIGUIENTE").click()
            await asyncio.sleep(2)
            await page.wait_for_load_state("networkidle")
            print(f"   ✔ Navigated to {target_short}")
        await screenshot(page, "03_schedule")

        # ── 4. FIND & CLICK RESERVAR FOR CANCHA 5 AT TARGET HOUR ─────────────
        print(f"4️⃣  Finding Cancha {COURT} at {HOUR} …")
        row_selector = "[id*='Gridsdthorasdeldia_horassContainerRow']"
        try:
            await page.locator(row_selector).first.wait_for(timeout=10000)
        except PlaywrightTimeout:
            grid_ids = await page.evaluate("""() =>
                [...document.querySelectorAll('[id]')]
                    .map(el => el.id)
                    .filter(id => id.toLowerCase().includes('grid') || id.toLowerCase().includes('row'))
                    .slice(0, 20)
            """)
            print("   Grid IDs found:", grid_ids)
            await screenshot(page, "04_FAIL_no_grid")
            raise RuntimeError("❌ Grid rows not found")

        rows = page.locator(row_selector)
        row_count = await rows.count()
        print(f"   Found {row_count} rows")

        clicked = False
        for i in range(row_count):
            row = rows.nth(i)
            text = await row.inner_text()
            if HOUR in text and COURT in text:
                btn = row.locator("input[type=button], button, a").first
                await btn.click()
                print(f"   ✔ Clicked row {i} → matched {HOUR} + Cancha {COURT}")
                clicked = True
                break
        if not clicked:
            for i in range(row_count):
                row = rows.nth(i)
                text = await row.inner_text()
                if HOUR in text:
                    btn = row.locator("input[type=button], button, a").first
                    await btn.click()
                    print(f"   ✔ Clicked row {i} → matched {HOUR} only")
                    clicked = True
                    break
        if not clicked:
            await screenshot(page, "04_FAIL_no_row")
            raise RuntimeError(f"❌ No row found for {HOUR} Cancha {COURT}")

        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "04_after_row_click")

        # ── 5. CLICK BTNBOTONBUSCAR (navigates to horaactual / opens modal) ───
        print("5️⃣  Clicking BTNBOTONBUSCAR …")
        await page.locator("#BTNBOTONBUSCAR").wait_for(state="visible", timeout=10000)
        await asyncio.sleep(1)

        # BTNBOTONBUSCAR might open a popup — listen for it
        popup_page = None
        try:
            async with context.expect_page(timeout=5000) as popup_info:
                await page.locator("#BTNBOTONBUSCAR").click()
            popup_page = await popup_info.value
            await popup_page.wait_for_load_state("networkidle")
            print(f"   ✔ Popup opened: {popup_page.url}")
        except PlaywrightTimeout:
            # No popup — normal navigation
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle")
            print(f"   ✔ No popup, current URL: {page.url}")

        working_page = popup_page if popup_page else page
        await screenshot(working_page, "05_reserva_page")

        # ── 6. SEARCH FOR PARTNER ─────────────────────────────────────────────
        print(f"6️⃣  Searching for {PARTNER} …")

        # Find vTEXTOBUSCAR in main page, any iframe, or popup
        target_frame = None
        search_pages = [working_page] + ([page] if popup_page else [])
        for sp in search_pages:
            for frame in sp.frames:
                try:
                    if await frame.locator("#vTEXTOBUSCAR").count() > 0:
                        target_frame = frame
                        print(f"   ✔ Found #vTEXTOBUSCAR in frame: {frame.url}")
                        break
                except Exception:
                    continue
            if target_frame:
                break

        if target_frame:
            await target_frame.evaluate("document.getElementById('vTEXTOBUSCAR').focus()")
        else:
            # Not found yet — maybe need to click Buscar on the reserva page first
            print("   vTEXTOBUSCAR not found yet — clicking Buscar on reserva page …")
            buscar2 = working_page.locator("#BTNBOTONBUSCAR, button:has-text('Buscar'), input[value='Buscar']").first
            try:
                await buscar2.wait_for(state="visible", timeout=5000)
                async with context.expect_page(timeout=4000) as p2_info:
                    await buscar2.click()
                popup_page2 = await p2_info.value
                await popup_page2.wait_for_load_state("networkidle")
                working_page = popup_page2
                print(f"   ✔ Popup 2: {popup_page2.url}")
            except PlaywrightTimeout:
                await buscar2.click()
                await asyncio.sleep(3)

            await screenshot(working_page, "06_after_buscar2")
            for frame in working_page.frames:
                try:
                    if await frame.locator("#vTEXTOBUSCAR").count() > 0:
                        target_frame = frame
                        print(f"   ✔ Found #vTEXTOBUSCAR in frame: {frame.url}")
                        break
                except Exception:
                    continue

        await screenshot(working_page, "06_before_type")

        # Type into the search input
        if target_frame:
            await target_frame.evaluate("document.getElementById('vTEXTOBUSCAR').focus()")
        await working_page.keyboard.type(PARTNER.split()[0], delay=100)
        print(f"   ✔ Typed '{PARTNER.split()[0]}'")
        await asyncio.sleep(2)
        await screenshot(working_page, "07_autocomplete")

        # ── 7. SELECT KEVIN MONZON ────────────────────────────────────────────
        print(f"7️⃣  Selecting {PARTNER} …")
        partner_upper = PARTNER.upper()
        partner_frame = target_frame if target_frame else working_page.main_frame

        partner_el = partner_frame.locator(
            f"[id*='SOCIOFULLNOMBREAPELLIDO']:has-text('{partner_upper}') a"
        ).first
        await partner_el.wait_for(timeout=8000)
        await partner_el.click()
        print(f"   ✔ Selected {PARTNER}")
        await asyncio.sleep(1)
        await screenshot(working_page, "08_partner_selected")

        # ── 8. CONFIRM ────────────────────────────────────────────────────────
        print("8️⃣  Confirming …")
        confirm_page = popup_page if popup_page else page
        confirmar = confirm_page.locator("button:has-text('Confirmar'), input[value='Confirmar']").first
        await confirmar.wait_for(timeout=8000)
        await confirmar.click()
        print("   ✔ Clicked Confirmar")
        await asyncio.sleep(2)

        # A "¿Confirma la reserva?" dialog appears — click Sí
        si_btn = confirm_page.locator("#DVELOP_CONFIRMPANEL_ENTERContainer_SaveButton")
        await si_btn.wait_for(timeout=8000)
        await si_btn.click()
        print("   ✔ Clicked Sí (#DVELOP_CONFIRMPANEL_ENTERContainer_SaveButton)")
        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        await screenshot(confirm_page, "09_final")

        print(f"\n✅  Done! Court {COURT} on {date_str} at {HOUR} with {PARTNER}")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
