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
        await page.wait_for_load_state("networkidle")
        dias_btn = page.locator("button:has-text('Días disponibles'), a:has-text('Días disponibles')")
        await dias_btn.wait_for(timeout=10000)
        await dias_btn.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)
        await screenshot(page, "02_dias_disponibles")
        print(f"   ✔ Clicked → {page.url}")

        # ── 3. NAVIGATE TO TOMORROW ───────────────────────────────────────────
        print("3️⃣  Navigating to tomorrow …")

        # The page shows a date with prev/next arrows — click the next (>) arrow
        next_arrow = page.locator(
            "button[title*='siguiente' i], "
            "button[title*='next' i], "
            "button[aria-label*='siguiente' i], "
            "button[aria-label*='next' i], "
            "a[title*='siguiente' i], "
            "[class*='next' i]:not(input):not(select), "
            "span.glyphicon-chevron-right, "
            "i.fa-chevron-right, "
            "i.fa-arrow-right"
        ).first

        await next_arrow.wait_for(timeout=10000)
        await next_arrow.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)
        await screenshot(page, "03_tomorrow")
        print("   ✔ Navigated to next day")

        # ── 4. FIND CANCHA 5 AT 19:00 AND CLICK RESERVAR ─────────────────────
        print(f"4️⃣  Looking for Cancha {COURT} at {HOUR} …")
        await screenshot(page, "04_schedule_view")

        # Strategy: find a row/cell that contains both the hour and cancha number,
        # then click its "Reservar" button
        # Common patterns in DeporYA:
        #   - A table where rows = hours, columns = canchas
        #   - Or rows = canchas, columns = hours

        # Try to find a reservar button near "19:00" AND "5" or "Cancha 5"
        # First try: find cell/button with both references nearby
        hour_variants  = ["19:00", "19:00 hs", "19 hs", "19h", "7:00 PM"]
        court_variants = [f"Cancha {COURT}", f"cancha{COURT}", f"Court {COURT}", COURT]

        clicked = False

        # Approach 1: look for a row containing the hour, then find Reservar in that row
        for hv in hour_variants:
            rows = page.locator(f"tr:has-text('{hv}'), div[class*='row']:has-text('{hv}')")
            count = await rows.count()
            if count > 0:
                print(f"   Found {count} row(s) with '{hv}'")
                for i in range(count):
                    row = rows.nth(i)
                    row_text = await row.inner_text()
                    # Check if this row mentions court 5
                    if any(cv.lower() in row_text.lower() for cv in court_variants) or count == 1:
                        reservar = row.locator(
                            "button:has-text('Reservar'), "
                            "a:has-text('Reservar'), "
                            "input[value*='Reservar' i]"
                        ).first
                        if await reservar.count() > 0:
                            await reservar.click()
                            await page.wait_for_load_state("networkidle")
                            await asyncio.sleep(1)
                            await screenshot(page, "05_reservar_clicked")
                            print(f"   ✔ Clicked Reservar in row with '{hv}'")
                            clicked = True
                            break
                if clicked:
                    break

        # Approach 2: find a cell/link that contains the hour text and click Reservar nearby
        if not clicked:
            print("   Trying approach 2: locate hour cell then nearby Reservar …")
            for hv in hour_variants:
                hour_cell = page.locator(f"td:has-text('{hv}'), span:has-text('{hv}')").first
                if await hour_cell.count() > 0:
                    # Walk up to parent row/container
                    parent = hour_cell.locator("xpath=ancestor::tr[1]")
                    if await parent.count() == 0:
                        parent = hour_cell.locator("xpath=ancestor::div[contains(@class,'row')][1]")
                    reservar = parent.locator(
                        "button:has-text('Reservar'), a:has-text('Reservar')"
                    ).first
                    if await reservar.count() > 0:
                        await reservar.click()
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(1)
                        await screenshot(page, "05_reservar_clicked")
                        print(f"   ✔ Clicked Reservar via hour cell '{hv}'")
                        clicked = True
                        break

        if not clicked:
            await screenshot(page, "05_reservar_NOT_found")
            raise RuntimeError(
                f"❌ Could not find Reservar button for Court {COURT} at {HOUR}. "
                "Check screenshot 04_schedule_view.png to see the page layout."
            )

        # ── 5. SEARCH FOR PARTNER ─────────────────────────────────────────────
        print(f"5️⃣  Searching for partner: {PARTNER} …")
        await screenshot(page, "06_before_buscar")

        # Fill the search/partner field
        search_filled = False
        for sel in [
            "input[placeholder*='buscar' i]",
            "input[placeholder*='nombre' i]",
            "input[placeholder*='jugador' i]",
            "input[placeholder*='socio' i]",
            "input[type='search']",
            "input[type='text']",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=3000):
                    await loc.click()
                    await loc.type(PARTNER, delay=80)
                    print(f"   ✔ Typed partner name in {sel}")
                    search_filled = True
                    break
            except Exception:
                continue

        await asyncio.sleep(0.5)

        # Click "Buscar"
        buscar = page.locator(
            "button:has-text('Buscar'), "
            "input[value='Buscar'], "
            "a:has-text('Buscar')"
        ).first
        await buscar.wait_for(timeout=8000)
        await buscar.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)
        await screenshot(page, "07_search_results")
        print("   ✔ Clicked Buscar")

        # Select Kevin Monzon from results
        result = page.locator(
            f"*:has-text('{PARTNER}')"
            ":not(html):not(body):not(script)"
        ).first
        await result.wait_for(timeout=8000)
        await result.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)
        await screenshot(page, "08_partner_selected")
        print(f"   ✔ Selected {PARTNER}")

        # ── 6. CONFIRM RESERVATION ────────────────────────────────────────────
        print("6️⃣  Confirming reservation …")
        for sel in [
            "button:has-text('Confirmar')",
            "button:has-text('Reservar')",
            "button:has-text('Aceptar')",
            "input[value*='Confirm' i]",
            "input[value*='Reserv' i]",
            "input[value*='Aceptar' i]",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=4000):
                    await loc.click()
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(1)
                    await screenshot(page, "09_confirmed")
                    print(f"   ✔ Confirmed via {sel}")
                    break
            except Exception:
                continue

        await screenshot(page, "10_final_state")
        print(f"\n✅  Done! Court {COURT} on {date_str} at {HOUR} with {PARTNER}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
