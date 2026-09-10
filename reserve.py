import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL = "https://agenbot.net/deporyatenis"
TZ = ZoneInfo("America/Montevideo")


async def screenshot(page, name, screenshot_dir):
    os.makedirs(screenshot_dir, exist_ok=True)
    path = f"{screenshot_dir}/{name}.png"
    await page.screenshot(path=path, full_page=True)
    print(f"  📸  {name}.png")
    return path


async def run_reservation(username, password, court="5", hour="10:00", days_ahead=1,
                           partner="Kevin Monzon", screenshot_dir="screenshots"):
    """Run the full DeporYA reservation flow.

    Returns {"success": bool, "message": str, "screenshot": str | None}.
    """
    date_str = (datetime.now(TZ) + timedelta(days=days_ahead)).strftime("%d/%m/%Y")
    print(f"🎾  Reserving Court {court} at {hour} on {date_str}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="es-UY")
        page = await context.new_page()

        try:
            # ── 1. LOGIN ──────────────────────────────────────────────────────
            print("1️⃣  Logging in …")
            await page.goto(f"{BASE_URL}/login.aspx", wait_until="networkidle")
            await page.locator("#vUSERNAME").click()
            await page.locator("#vUSERNAME").type(username, delay=100)
            await page.locator("#vUSERNAME").press("Tab")
            await asyncio.sleep(0.5)
            await page.locator("#vUSERPASSWORD").click()
            await page.locator("#vUSERPASSWORD").type(password, delay=100)
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

            # ── 2. DÍAS DISPONIBLES ───────────────────────────────────────────
            print("2️⃣  Clicking Días disponibles …")
            await page.locator("#BTNBTNRESERVAR").click()
            await asyncio.sleep(2)
            await page.wait_for_load_state("networkidle")
            print(f"   ✔ → {page.url}")

            # ── 3. SELECT LADRILLO ────────────────────────────────────────────
            print("3️⃣  Selecting LADRILLO …")
            await page.locator("#vTIPOCANCHAID").select_option(label="LADRILLO")
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle")
            print("   ✔ LADRILLO selected")

            # Navigate to target date only if not already there
            target_short = (datetime.now(TZ) + timedelta(days=days_ahead)).strftime("%d/%m/%y")
            page_text = await page.inner_text("body")
            if target_short in page_text:
                print(f"   ✔ Already on {target_short}")
            else:
                await page.locator("#BTNBTNSIGUIENTE").click()
                await asyncio.sleep(2)
                await page.wait_for_load_state("networkidle")
                print(f"   ✔ Navigated to {target_short}")
            await screenshot(page, "03_schedule", screenshot_dir)

            # ── 4. FIND & CLICK RESERVAR FOR TARGET COURT + HOUR ──────────────
            print(f"4️⃣  Finding Cancha {court} at {hour} …")
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
                await screenshot(page, "04_FAIL_no_grid", screenshot_dir)
                raise RuntimeError("❌ Grid rows not found")

            rows = page.locator(row_selector)
            row_count = await rows.count()
            print(f"   Found {row_count} rows")

            clicked = False
            for i in range(row_count):
                row = rows.nth(i)
                text = await row.inner_text()
                if hour in text and court in text:
                    btn = row.locator("input[type=button], button, a").first
                    await btn.click()
                    print(f"   ✔ Clicked row {i} → matched {hour} + Cancha {court}")
                    clicked = True
                    break
            if not clicked:
                for i in range(row_count):
                    row = rows.nth(i)
                    text = await row.inner_text()
                    if hour in text:
                        btn = row.locator("input[type=button], button, a").first
                        await btn.click()
                        print(f"   ✔ Clicked row {i} → matched {hour} only")
                        clicked = True
                        break
            if not clicked:
                await screenshot(page, "04_FAIL_no_row", screenshot_dir)
                raise RuntimeError(f"❌ No row found for {hour} Cancha {court}")

            await asyncio.sleep(2)
            await page.wait_for_load_state("networkidle")
            await screenshot(page, "04_after_row_click", screenshot_dir)

            # ── 5. CLICK BTNBOTONBUSCAR (navigates to horaactual / opens modal) ─
            print("5️⃣  Clicking BTNBOTONBUSCAR …")
            await page.locator("#BTNBOTONBUSCAR").wait_for(state="visible", timeout=10000)
            await asyncio.sleep(1)

            popup_page = None
            try:
                async with context.expect_page(timeout=5000) as popup_info:
                    await page.locator("#BTNBOTONBUSCAR").click()
                popup_page = await popup_info.value
                await popup_page.wait_for_load_state("networkidle")
                print(f"   ✔ Popup opened: {popup_page.url}")
            except PlaywrightTimeout:
                await asyncio.sleep(3)
                await page.wait_for_load_state("networkidle")
                print(f"   ✔ No popup, current URL: {page.url}")

            working_page = popup_page if popup_page else page
            await screenshot(working_page, "05_reserva_page", screenshot_dir)

            # ── 6. SEARCH FOR PARTNER ─────────────────────────────────────────
            print(f"6️⃣  Searching for {partner} …")

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

                await screenshot(working_page, "06_after_buscar2", screenshot_dir)
                for frame in working_page.frames:
                    try:
                        if await frame.locator("#vTEXTOBUSCAR").count() > 0:
                            target_frame = frame
                            print(f"   ✔ Found #vTEXTOBUSCAR in frame: {frame.url}")
                            break
                    except Exception:
                        continue

            await screenshot(working_page, "06_before_type", screenshot_dir)

            if target_frame:
                await target_frame.evaluate("document.getElementById('vTEXTOBUSCAR').focus()")
            await working_page.keyboard.type(partner.split()[0], delay=100)
            print(f"   ✔ Typed '{partner.split()[0]}'")
            await asyncio.sleep(2)
            await screenshot(working_page, "07_autocomplete", screenshot_dir)

            # ── 7. SELECT PARTNER ──────────────────────────────────────────────
            print(f"7️⃣  Selecting {partner} …")
            partner_upper = partner.upper()
            partner_frame = target_frame if target_frame else working_page.main_frame

            partner_el = partner_frame.locator(
                f"[id*='SOCIOFULLNOMBREAPELLIDO']:has-text('{partner_upper}') a"
            ).first
            await partner_el.wait_for(timeout=8000)
            await partner_el.click()
            print(f"   ✔ Selected {partner}")
            await asyncio.sleep(1)
            await screenshot(working_page, "08_partner_selected", screenshot_dir)

            # ── 8. CONFIRM ────────────────────────────────────────────────────
            print("8️⃣  Confirming …")
            confirm_page = popup_page if popup_page else page
            confirmar = confirm_page.locator("button:has-text('Confirmar'), input[value='Confirmar']").first
            await confirmar.wait_for(timeout=8000)
            await confirmar.click()
            print("   ✔ Clicked Confirmar")
            await asyncio.sleep(2)

            si_btn = confirm_page.locator("#DVELOP_CONFIRMPANEL_ENTERContainer_SaveButton")
            await si_btn.wait_for(timeout=8000)
            await si_btn.click()
            print("   ✔ Clicked Sí (#DVELOP_CONFIRMPANEL_ENTERContainer_SaveButton)")
            await asyncio.sleep(2)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            final_shot = await screenshot(confirm_page, "09_final", screenshot_dir)

            message = f"Cancha {court} el {date_str} a las {hour} con {partner}"
            print(f"\n✅  Done! {message}")
            return {"success": True, "message": message, "screenshot": final_shot}

        except Exception as e:
            print(f"\n❌  Failed: {e}")
            error_shot = None
            try:
                error_shot = await screenshot(page, "ERROR", screenshot_dir)
            except Exception:
                pass
            return {"success": False, "message": str(e), "screenshot": error_shot}

        finally:
            await browser.close()


if __name__ == "__main__":
    result = asyncio.run(run_reservation(
        username=os.environ["TENNIS_USER"],
        password=os.environ["TENNIS_PASS"],
        court=os.environ.get("COURT", "5"),
        hour=os.environ.get("HOUR", "10:00"),
        days_ahead=int(os.environ.get("DAYS_AHEAD", "1")),
        partner=os.environ.get("PARTNER", "Kevin Monzon"),
    ))
    print(result)
    if not result["success"]:
        raise SystemExit(1)
