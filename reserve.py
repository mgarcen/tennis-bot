import asyncio
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL = "https://agenbot.net/deporyatenis"
TZ = ZoneInfo("America/Montevideo")

ROW_SELECTOR = "[id*='Gridsdthorasdeldia_horassContainerRow']"
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
COURT_LABEL_RE = re.compile(r"cancha\s*n?[°º]?\s*0*(\d{1,2})", re.IGNORECASE)


def parse_hour(text):
    """'9:00' or '09:00' → (9, 0). None if it isn't a time."""
    m = TIME_RE.fullmatch(str(text).strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def fmt_hour(hour_tuple):
    return f"{hour_tuple[0]:02d}:{hour_tuple[1]:02d}"


def row_hours(text):
    """Every time mentioned in a row, as (h, m) — so '9:00' matches '09:00'."""
    return [(int(h), int(m)) for h, m in TIME_RE.findall(text)]


def row_courts(text):
    """Court numbers mentioned in a row.

    Times are stripped first, so Cancha 5 is never matched by a 15:00 slot. When the
    row labels its court ('Cancha 5') only labelled numbers count; otherwise we fall
    back to any standalone number in the row.
    """
    without_times = TIME_RE.sub(" ", text)
    labelled = COURT_LABEL_RE.findall(without_times)
    if labelled:
        return [int(n) for n in labelled]
    return [int(n) for n in re.findall(r"\b0*(\d{1,2})\b", without_times)]


async def screenshot(page, name, screenshot_dir):
    os.makedirs(screenshot_dir, exist_ok=True)
    path = f"{screenshot_dir}/{name}.png"
    await page.screenshot(path=path, full_page=True)
    print(f"  📸  {name}.png")
    return path


async def surface_options(page):
    """[(value, label)] of the surface dropdown, in the order the page lists them."""
    return await page.evaluate("""() =>
        [...document.querySelectorAll('#vTIPOCANCHAID option')]
            .map(o => [o.value, (o.textContent || '').trim()])
            .filter(([value, label]) => value && label)
    """)


async def select_surface(page, value):
    await page.locator("#vTIPOCANCHAID").select_option(value=value)
    await asyncio.sleep(3)
    await page.wait_for_load_state("networkidle")


async def goto_target_date(page, target_short, days_ahead):
    """Click 'Siguiente' until the target day's grid is showing.

    Returns the number of clicks it took, or None if the date never appeared.
    """
    for click_num in range(max(days_ahead + 2, 5)):
        if target_short in await page.inner_text("body"):
            return click_num
        await page.locator("#BTNBTNSIGUIENTE").click()
        await asyncio.sleep(2)
        await page.wait_for_load_state("networkidle")
    return None


async def scan_surface(page, hour_tuple, court_num):
    """Read the grid of the surface now showing, for one hour.

    Returns a dict:
      state      "free" (requested court bookable), "taken" (its row is there but the
                 Reserva link is hidden), "missing" (no row for it at that hour) or
                 "no-grid" (no rows at all, i.e. the markup changed)
      row        the requested court's row when state is "free"
      home       True if the requested court appears on this surface at any hour; court
                 numbers are unique, so that is how we know which surface it lives on
                 even when the site hides a booked slot entirely
      others     [(court, row)] other courts free at that hour on this surface
    """
    result = {"state": "missing", "row": None, "home": False, "others": []}
    try:
        await page.locator(ROW_SELECTOR).first.wait_for(timeout=10000)
    except PlaywrightTimeout:
        # No grid at all — dump candidate ids so a markup change is diagnosable.
        grid_ids = await page.evaluate("""() =>
            [...document.querySelectorAll('[id]')]
                .map(el => el.id)
                .filter(id => id.toLowerCase().includes('grid') || id.toLowerCase().includes('row'))
                .slice(0, 20)
        """)
        print("      no grid rows; candidate ids:", grid_ids)
        result["state"] = "no-grid"
        return result

    rows = page.locator(ROW_SELECTOR)
    row_count = await rows.count()
    print(f"      {row_count} rows")

    for i in range(row_count):
        row = rows.nth(i)
        text = await row.inner_text()
        courts = row_courts(text)
        if court_num in courts:
            result["home"] = True
        if hour_tuple not in row_hours(text):
            continue
        free = await row.locator("input[type=button], button, a").first.is_visible()
        if court_num in courts:
            if free:
                print(f"      ✔ row {i} → Cancha {court_num} at {fmt_hour(hour_tuple)}")
                result["state"], result["row"] = "free", row
            else:
                print(f"      • row {i} is Cancha {court_num} at {fmt_hour(hour_tuple)}, "
                      f"Reserva link hidden (already booked)")
                if result["state"] != "free":
                    result["state"] = "taken"
        elif free and len(courts) == 1:
            result["others"].append((courts[0], row))
    return result


def pick_substitute(court_num, others):
    """The free court closest in number to the one requested (lower wins a tie)."""
    if not others:
        return None
    return min(others, key=lambda c: (abs(c[0] - court_num), c[0]))


async def find_court_across_surfaces(page, hour_tuple, court_num, options,
                                     target_short, days_ahead):
    """Search each surface for this court + hour, falling back to its own surface.

    Returns (row, label, states, booked_court). If the requested court is free it is
    `booked_court`. If it is taken, or its slot is hidden, the closest-numbered court
    free at the same hour on the same surface is returned instead. (None, None, states,
    None) means nothing bookable: `states` holds one outcome per surface tried, with
    "full" meaning the court's surface had nothing else free at that hour.
    """
    states = []
    for value, label in options:
        print(f"   → {label} …")
        await select_surface(page, value)
        if await goto_target_date(page, target_short, days_ahead) is None:
            print(f"      • could not reach {target_short} on {label}")
            states.append("no-date")
            continue
        scan = await scan_surface(page, hour_tuple, court_num)
        if scan["state"] == "free":
            states.append("free")
            return scan["row"], label, states, court_num
        if not scan["home"] and scan["state"] != "taken":
            states.append(scan["state"])
            continue
        # The requested court lives on this surface but isn't bookable: take another here.
        pick = pick_substitute(court_num, scan["others"])
        if pick:
            print(f"      ↪ Cancha {court_num} not available; Cancha {pick[0]} is free "
                  f"on {label} at {fmt_hour(hour_tuple)}")
            states.append("substitute")
            return pick[1], label, states, pick[0]
        print(f"      • no other court free on {label} at {fmt_hour(hour_tuple)}")
        states.append("full")
        return None, label, states, None
    return None, None, states, None


async def run_reservation(username, password, court="5", hour="10:00", days_ahead=1,
                           partner="Kevin Monzon", screenshot_dir="screenshots",
                           surface=None):
    """Run the full DeporYA reservation flow.

    Court numbers are unique across the club, so `court` alone identifies a court and
    the surface it sits on is discovered by searching each one. Pass `surface` (a
    dropdown label) to skip that search and look only there.

    Returns {"success": bool, "message": str, "screenshot": str | None}.
    """
    hour_tuple = parse_hour(hour)
    if hour_tuple is None:
        return {"success": False, "message": f"❌ '{hour}' no es una hora válida (formato HH:MM)", "screenshot": None}
    if not str(court).strip().isdigit():
        return {"success": False, "message": f"❌ '{court}' no es un número de cancha válido", "screenshot": None}
    court_num = int(court)

    target = datetime.now(TZ) + timedelta(days=days_ahead)
    date_str = target.strftime("%d/%m/%Y")
    target_short = target.strftime("%d/%m/%y")
    print(f"🎾  Reserving Cancha {court_num} at {fmt_hour(hour_tuple)} on {date_str}")

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

            # ── 3. FIND THE COURT, WHICHEVER SURFACE IT'S ON ──────────────────
            options = await surface_options(page)
            if not options:
                await screenshot(page, "03_FAIL_no_surfaces", screenshot_dir)
                raise RuntimeError("❌ No encontré el selector de tipo de cancha")
            if surface:
                wanted = surface.strip().upper()
                options = [(v, l) for v, l in options if l.upper() == wanted]
                if not options:
                    raise RuntimeError(f"❌ No existe la superficie '{surface}'")

            print(f"3️⃣  Looking for Cancha {court_num} across {len(options)} surface(s): "
                  f"{', '.join(label for _, label in options)}")
            row, found_on, states, booked_court = await find_court_across_surfaces(
                page, hour_tuple, court_num, options, target_short, days_ahead
            )

            if row is None:
                await screenshot(page, "03_FAIL_no_row", screenshot_dir)
                surface_list = ", ".join(label for _, label in options)
                if "full" in states:
                    raise RuntimeError(
                        f"❌ Cancha {court_num} a las {fmt_hour(hour_tuple)} ya está reservada y no "
                        f"hay otra cancha libre en {found_on} a esa hora"
                    )
                if "taken" in states:
                    raise RuntimeError(
                        f"❌ Cancha {court_num} a las {fmt_hour(hour_tuple)} ya no está disponible "
                        f"(reservada por otra persona)"
                    )
                if all(st == "no-date" for st in states):
                    raise RuntimeError(f"❌ No pude navegar hasta la fecha {target_short}")
                if all(st == "no-grid" for st in states):
                    raise RuntimeError("❌ Grid rows not found")
                raise RuntimeError(
                    f"❌ No encontré Cancha {court_num} a las {fmt_hour(hour_tuple)} en ninguna "
                    f"superficie ({surface_list})"
                )
            substituted = booked_court != court_num
            print(f"   ✔ Booking Cancha {booked_court} on {found_on}"
                  + (f" (instead of Cancha {court_num})" if substituted else ""))
            await screenshot(page, "03_schedule", screenshot_dir)

            # ── 4. CLICK RESERVAR ON THAT ROW ─────────────────────────────────
            print(f"4️⃣  Reserving Cancha {booked_court} ({found_on}) at {fmt_hour(hour_tuple)} …")
            await row.locator("input[type=button], button, a").first.click(timeout=5000)
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

            message = f"Cancha {booked_court} ({found_on}) el {date_str} a las {fmt_hour(hour_tuple)} con {partner}"
            if substituted:
                message += f" — la Cancha {court_num} estaba ocupada, reservé la {booked_court}"
            print(f"\n✅  Done! {message}")
            return {"success": True, "message": message, "screenshot": final_shot}

        except Exception as e:
            print(f"\n❌  Failed: {e}")
            error_shot = None
            try:
                error_shot = await screenshot(page, "ERROR", screenshot_dir)
            except Exception:
                pass
            # Playwright timeout errors include a long multi-line call log —
            # keep just the first line so it fits in a Telegram message.
            short_message = str(e).strip().split("\n")[0][:300]
            return {"success": False, "message": short_message, "screenshot": error_shot}

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
        surface=os.environ.get("SURFACE") or None,
    ))
    print(result)
    if not result["success"]:
        raise SystemExit(1)
