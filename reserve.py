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

# ── Helpers ───────────────────────────────────────────────────────────────────
def target_date():
    d = datetime.now(TZ) + timedelta(days=DAYS_AHEAD)
    return d.strftime("%d/%m/%Y")

async def screenshot(page, name):
    os.makedirs("screenshots", exist_ok=True)
    await page.screenshot(path=f"screenshots/{name}.png", full_page=True)
    print(f"  📸  {name}.png")

async def js_click_text(page, text):
    """Click any element whose text contains `text` — no offsetParent check (GeneXus compat)."""
    clicked = await page.evaluate(f"""() => {{
        // Search ALL elements, not just buttons — GeneXus uses divs/spans as clickable items
        const all = document.querySelectorAll('*');
        for (const el of all) {{
            const t = el.textContent.trim();
            if (t.includes('{text}') && t.length < 60) {{
                el.click();
                return t;
            }}
        }}
        return null;
    }}""")
    return clicked

async def list_buttons(page):
    """Print ALL clickable-looking elements — helpful for debugging."""
    btns = await page.evaluate("""() => {
        const all = document.querySelectorAll('button, a, input[type=button], input[type=submit], [onclick], [data-gx-evt]');
        return Array.from(all).map(el => (el.tagName + '|' + el.textContent.trim().substring(0, 50)));
    }""")
    print("   Clickable elements:", btns)

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    date_str = target_date()
    print(f"🎾  Reserving Court {COURT} at {HOUR} on {date_str}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context(
            viewport={"width": 1280, "height": 900}, locale="es-UY"
        )).new_page()

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
        await screenshot(page, "01_after_login")

        if "login" in page.url.lower():
            raise RuntimeError("❌ Login failed")
        print(f"   ✔ Logged in → {page.url}")

        # ── 2. CLICK "DÍAS DISPONIBLES" ───────────────────────────────────────
        print("2️⃣  Clicking 'Días disponibles' …")
        await asyncio.sleep(1)
        await list_buttons(page)

        # Exact GeneXus button ID confirmed from page source
        await page.locator("#BTNBTNRESERVAR").click()
        print("   ✔ Clicked #BTNBTNRESERVAR (Días disponibles)")

        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "02_dias_disponibles")
        print(f"   → URL: {page.url}")

        # ── 3. NAVIGATE TO TOMORROW ───────────────────────────────────────────
        print("3️⃣  Navigating to tomorrow (clicking next arrow) …")
        await asyncio.sleep(1)

        # Exact GeneXus button ID confirmed
        await page.locator("#BTNBTNSIGUIENTE").click()
        print("   ✔ Clicked #BTNBTNSIGUIENTE (next day)")

        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "03_tomorrow")

        # ── 3b. SELECT SURFACE TYPE: LADRILLO ────────────────────────────────
        print("3b. Selecting surface type LADRILLO …")
        await page.locator("#vTIPOCANCHAID").select_option(label="LADRILLO")
        await asyncio.sleep(1)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "03b_ladrillo_selected")
        print("   ✔ Selected LADRILLO")

        # ── 4. FIND CANCHA 5 AT 19:00 AND CLICK RESERVAR ─────────────────────
        print(f"4️⃣  Looking for Cancha {COURT} at {HOUR} …")
        await asyncio.sleep(1)
        await screenshot(page, "04_schedule")

        # Dynamically find the row containing HOUR and COURT, then click its Reservar button
        row_selector = "[id*='Gridsdthorasdeldia_horassContainerRow']"
        await page.locator(row_selector).first.wait_for(timeout=10000)

        rows = page.locator(row_selector)
        row_count = await rows.count()
        print(f"   Found {row_count} grid rows")

        clicked = False
        for i in range(row_count):
            row = rows.nth(i)
            text = await row.inner_text()
            if HOUR in text and COURT in text:
                reservar_btn = row.locator("button, a, input[type=button], input[type=submit]").first
                await reservar_btn.click()
                print(f"   ✔ Clicked Reservar in row {i} — matched '{HOUR}' + 'Cancha {COURT}'")
                clicked = True
                break

        if not clicked:
            # Fallback: match by hour only (in case court label differs)
            for i in range(row_count):
                row = rows.nth(i)
                text = await row.inner_text()
                if HOUR in text:
                    reservar_btn = row.locator("button, a, input[type=button], input[type=submit]").first
                    await reservar_btn.click()
                    print(f"   ✔ Clicked Reservar in row {i} — matched '{HOUR}' only")
                    clicked = True
                    break

        if not clicked:
            await screenshot(page, "04_FAIL_reservar")
            raise RuntimeError(f"❌ No row found with hour={HOUR} court={COURT} — check 04_FAIL_reservar.png")

        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "05_after_reservar")

        # ── 5. BUSCAR + SELECT KEVIN MONZON ───────────────────────────────────
        print(f"5️⃣  Searching for {PARTNER} …")
        await asyncio.sleep(1)
        await screenshot(page, "06_before_search")

        # Type in the search box
        search_box = page.locator("input[type='text'], input[type='search']").first
        await search_box.wait_for(timeout=8000)
        await search_box.click()
        await search_box.type(PARTNER.split()[0], delay=80)   # type first name
        print(f"   ✔ Typed search term")

        # Click Buscar
        result2 = await js_click_text(page, "Buscar")
        if not result2:
            raise RuntimeError("❌ Could not find Buscar button")
        print(f"   ✔ Clicked Buscar")

        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "07_search_results")

        # Click Kevin Monzon in results
        partner_el = page.locator(f"*:has-text('{PARTNER}'):not(html):not(body):not(head)").first
        await partner_el.wait_for(timeout=8000)
        await partner_el.click()
        print(f"   ✔ Selected {PARTNER}")

        await asyncio.sleep(1)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "08_partner_selected")

        # ── 6. CONFIRM ────────────────────────────────────────────────────────
        print("6️⃣  Confirming …")
        confirmed = await js_click_text(page, "Confirmar") or \
                    await js_click_text(page, "Reservar")  or \
                    await js_click_text(page, "Aceptar")
        if confirmed:
            print(f"   ✔ Confirmed via: '{confirmed}'")
        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "09_final")

        print(f"\n✅  Done! Court {COURT} on {date_str} at {HOUR} with {PARTNER}")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
