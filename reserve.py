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

        # Select LADRILLO first — doing it after next-arrow resets the date
        print("3b. Selecting surface type LADRILLO …")
        await page.locator("#vTIPOCANCHAID").select_option(label="LADRILLO")
        print("   ✔ Selected LADRILLO — waiting for grid to reload …")
        await asyncio.sleep(3)
        await page.wait_for_load_state("networkidle")

        await screenshot(page, "03b_ladrillo_set")

        # Navigate to target date — only click next if not already there
        target_short = (datetime.now(TZ) + timedelta(days=DAYS_AHEAD)).strftime("%d/%m/%y")
        page_text = await page.inner_text("body")
        if target_short in page_text:
            print(f"   ✔ Already on target date {target_short}, skipping next arrow")
        else:
            await page.locator("#BTNBTNSIGUIENTE").click()
            print(f"   ✔ Clicked next arrow → {target_short}")
            await asyncio.sleep(2)
            await page.wait_for_load_state("networkidle")
        await screenshot(page, "03_tomorrow")

        # ── 4. FIND CANCHA 5 AT 19:00 AND CLICK RESERVAR ─────────────────────
        print(f"4️⃣  Looking for Cancha {COURT} at {HOUR} …")
        await asyncio.sleep(1)
        await screenshot(page, "04_schedule")

        # Dynamically find the row containing HOUR and COURT, then click its Reservar button
        row_selector = "[id*='Gridsdthorasdeldia_horassContainerRow']"

        # Wait longer and debug if not found
        try:
            await page.locator(row_selector).first.wait_for(timeout=10000)
        except PlaywrightTimeout:
            # Print actual grid-related IDs on the page to find the correct prefix
            grid_ids = await page.evaluate("""() =>
                [...document.querySelectorAll('[id]')]
                    .map(el => el.id)
                    .filter(id => id.toLowerCase().includes('grid') || id.toLowerCase().includes('row') || id.toLowerCase().includes('hora'))
                    .slice(0, 30)
            """)
            print("   Grid-related IDs found:", grid_ids)
            await screenshot(page, "04_FAIL_no_grid")
            raise RuntimeError("❌ Grid rows not found — check 04_FAIL_no_grid.png and logs above")

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

        # Wait for modal/overlay to appear after Reservar click
        await asyncio.sleep(3)
        await screenshot(page, "05_after_reservar")
        print(f"   URL after reservar: {page.url}")

        # ── 5. BUSCAR + SELECT KEVIN MONZON ───────────────────────────────────
        # We are now on the RESERVA CANCHA page (HORA ACTUAL)
        # Flow: click Buscar → search input appears → type name → select → Confirmar
        print(f"5️⃣  Searching for {PARTNER} …")
        await screenshot(page, "06_reserva_cancha_page")

        # Exact GeneXus button ID confirmed
        await page.locator("#BTNBOTONBUSCAR").click()
        print("   ✔ Clicked #BTNBOTONBUSCAR")
        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "07_after_buscar")

        # Exact input ID confirmed
        search_box = page.locator("#vTEXTOBUSCAR")
        await search_box.wait_for(timeout=8000)
        await search_box.click()
        await search_box.type(PARTNER.split()[0], delay=100)  # type "Kevin"
        print(f"   ✔ Typed '{PARTNER.split()[0]}' into #vTEXTOBUSCAR")
        await asyncio.sleep(2)  # wait for autocomplete
        await screenshot(page, "08_autocomplete")

        # Name stored as ALL CAPS in the system
        partner_upper = PARTNER.upper()  # "KEVIN MONZON"
        partner_el = page.locator(
            f"[id*='SOCIOFULLNOMBREAPELLIDO']:has-text('{partner_upper}') a"
        ).first
        await partner_el.wait_for(timeout=8000)
        await partner_el.click()
        print(f"   ✔ Selected {PARTNER}")
        await asyncio.sleep(1)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "09_partner_selected")

        # ── 6. CONFIRM ────────────────────────────────────────────────────────
        print("6️⃣  Confirming …")
        confirmar = page.locator("button:has-text('Confirmar'), input[value='Confirmar']").first
        await confirmar.wait_for(timeout=8000)
        await confirmar.click()
        print("   ✔ Clicked Confirmar")
        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
        await screenshot(page, "10_final")


        print(f"\n✅  Done! Court {COURT} on {date_str} at {HOUR} with {PARTNER}")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
