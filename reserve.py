import asyncio
import gzip
import os
import re
import time
import urllib.parse
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


# Step screenshots cost ~0.5s each, which matters at 08:00. Only the final and failure
# screenshots are taken unless SCREENSHOTS=all.
ALL_SCREENSHOTS = os.environ.get("SCREENSHOTS", "").lower() == "all"


# ── Traffic recording (RECORD_HAR=1): what the site is sent, to rebuild it without a browser
RECORD_HAR = os.environ.get("RECORD_HAR") == "1"


def har_path(screenshot_dir):
    return f"{screenshot_dir}/traffic.har.gz"


async def new_context(browser, screenshot_dir):
    kwargs = {"viewport": {"width": 1280, "height": 900}, "locale": "es-UY"}
    if RECORD_HAR:
        os.makedirs(screenshot_dir, exist_ok=True)
        kwargs.update(record_har_path=f"{screenshot_dir}/traffic.har",
                      record_har_url_filter="**/deporyatenis/**",
                      record_har_content="embed")
    return await browser.new_context(**kwargs)


async def close_context(context, screenshot_dir, username, password):
    """Close the context (this is when Playwright writes the HAR), then redact and gzip it."""
    try:
        await context.close()
    except Exception:
        pass
    raw = f"{screenshot_dir}/traffic.har"
    if not (RECORD_HAR and os.path.exists(raw)):
        return
    with open(raw, encoding="utf-8", errors="replace") as f:
        text = f.read()
    os.remove(raw)
    # Never let the credentials leave the server: every spelling they could take in a request.
    for secret, mask in ((password, "<PASSWORD>"), (username, "<USERNAME>")):
        if not secret:
            continue
        for form in {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret),
                     json_escape(secret)}:
            text = text.replace(form, mask)
    with gzip.open(har_path(screenshot_dir), "wt", encoding="utf-8") as f:
        f.write(text)
    print(f"  🎥  traffic recorded → {har_path(screenshot_dir)}")


def json_escape(value):
    import json
    return json.dumps(value)[1:-1]


async def debug_shot(page, name, screenshot_dir):
    if ALL_SCREENSHOTS:
        await screenshot(page, name, screenshot_dir)


async def act_and_wait(page, action, max_wait):
    """Run `action` (a coroutine function), then wait for the page to react to it.

    This site updates in place over AJAX, where wait_for_load_state() returns at once,
    so instead we wait until the visible text changes, then give it a moment to finish
    rendering. `max_wait` (seconds) is the old fixed sleep: when nothing visibly changes,
    we never wait longer than the bot used to.
    """
    try:
        before = await page.evaluate("document.body ? document.body.innerText : ''")
    except Exception:
        before = None
    await action()
    try:
        await page.wait_for_function(
            "prev => document.body && document.body.innerText !== prev",
            arg=before, timeout=max_wait * 1000, polling=100,
        )
    except PlaywrightTimeout:
        return
    except Exception:
        # The action navigated away mid-wait; wait for the new page instead.
        await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(0.25)


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
    dropdown = page.locator("#vTIPOCANCHAID")
    if await dropdown.input_value() == value:
        return  # already showing; re-selecting would only wait for a refresh that never comes
    await act_and_wait(page, lambda: dropdown.select_option(value=value), max_wait=5)


async def goto_target_date(page, target_short, days_ahead):
    """Click 'Siguiente' until the target day's grid is showing.

    Returns the number of clicks it took, or None if the date never appeared.
    """
    for click_num in range(max(days_ahead + 2, 5)):
        if target_short in await page.inner_text("body"):
            return click_num
        await act_and_wait(page, lambda: page.locator("#BTNBTNSIGUIENTE").click(), max_wait=5)
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
                      f"Reserva link hidden (already booked): {' '.join(text.split())[:80]}")
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


async def open_schedule(page, username, password, lap=print):
    """Log in and open 'Días disponibles', ready to pick a surface and date."""
    # ── 1. LOGIN ──────────────────────────────────────────────────────
    lap("1️⃣  Logging in …")
    await page.goto(f"{BASE_URL}/login.aspx", wait_until="networkidle")
    # Kept exactly as the original, proven login: speeding up the typing and dropping
    # these pauses made DeporYA reject the login (it validates fields as you leave them).
    # Login speed won't matter once it happens before 08:00.
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
    lap("2️⃣  Clicking Días disponibles …")
    # At 08:00 the site is overloaded and this click can silently do nothing (seen
    # 2026-11-11: still on inicio.aspx after the wait → "No encontré el selector de
    # tipo de cancha"). So check the schedule page really opened, and click again if not.
    surfaces = page.locator("#vTIPOCANCHAID option").first
    for attempt in range(1, 4):
        try:
            if "inicio.aspx" in page.url.lower():
                await act_and_wait(page, lambda: page.locator("#BTNBTNRESERVAR").click(timeout=10000),
                                   max_wait=5)
            # Don't read the surface dropdown until it has options.
            await surfaces.wait_for(state="attached", timeout=15000)
            break
        except PlaywrightTimeout:
            print(f"   … schedule not open after try {attempt} ({page.url})")
    print(f"   ✔ → {page.url}")


# Pre-warm: how long after the opening to keep reloading for the court to appear.
OPEN_WINDOW = timedelta(seconds=90)
# Start reloading this long before the opening, in case the site's clock runs ahead.
OPEN_EARLY = timedelta(seconds=2)


async def reload_schedule(page, schedule_url):
    """Fresh copy of the schedule page (the grid, not just the URL)."""
    await page.goto(schedule_url, wait_until="domcontentloaded")
    await page.locator(f"{ROW_SELECTOR}, #BTNBTNRESERVAR, #vTIPOCANCHAID").first.wait_for(timeout=15000)
    if await page.locator("#vTIPOCANCHAID option").count() == 0:
        # The URL alone didn't bring the schedule back; go in through the button.
        await act_and_wait(page, lambda: page.locator("#BTNBTNRESERVAR").click(timeout=10000), max_wait=5)
        await page.locator("#vTIPOCANCHAID option").first.wait_for(state="attached", timeout=15000)


async def wait_for_opening(page, hour_tuple, court_num, options, target_short, days_ahead,
                           open_at, first, lap):
    """Logged in before `open_at`: reload until the court is bookable, then return it.

    `first` is the find_court_across_surfaces() result from before the opening. Before
    08:00 a hidden Reserva link only means "not open yet", so that answer is used just to
    learn which surface the court is on; after that only that surface is reloaded.
    """
    row, found_on, states, booked_court = first
    if row is not None and booked_court == court_num:
        lap(f"   ✔ Cancha {court_num} is already bookable — not waiting for {open_at:%H:%M}")
        return first
    home = [(v, l) for v, l in options if l == found_on] or options
    schedule_url = page.url
    lap(f"⏳  Pre-warmed; waiting for {open_at:%H:%M:%S} "
        f"(watching {', '.join(l for _, l in home)})")
    await sleep_until(open_at - OPEN_EARLY)

    deadline = open_at + OPEN_WINDOW
    tries = 0
    while True:
        tries += 1
        try:
            await reload_schedule(page, schedule_url)
            result = await find_court_across_surfaces(
                page, hour_tuple, court_num, home, target_short, days_ahead
            )
        except Exception as e:
            print(f"      reload {tries} failed: {str(e).splitlines()[0][:120]}")
            result = None
        now = datetime.now(TZ)
        if result is not None:
            row, found_on, states, booked_court = result
            # Before the opening, only the requested court counts: other courts showing
            # free then would just be the grid not having opened yet for everyone.
            if row is not None and (booked_court == court_num or now >= open_at):
                lap(f"   ✔ Open after {tries} reload(s), {(now - open_at).total_seconds():+.1f}s "
                    f"from {open_at:%H:%M:%S}")
                return result
        if now >= deadline:
            lap(f"   ✘ Still not bookable {OPEN_WINDOW.seconds}s after {open_at:%H:%M} "
                f"({tries} reloads)")
            if result is None:
                raise RuntimeError(f"❌ La página no cargó después de las {open_at:%H:%M}")
            return result


async def run_reservation(username, password, court="5", hour="10:00", days_ahead=1,
                           partner="Kevin Monzon", screenshot_dir="screenshots",
                           surface=None, open_at=None):
    """Run the full DeporYA reservation flow.

    With `open_at` (the 08:00 opening, still in the future) it pre-warms: logs in and
    finds the court's surface right away, then from just before `open_at` reloads that
    surface until the court becomes bookable, so the slow login is out of the way when
    the rush starts.

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
    t0 = time.monotonic()

    def lap(msg):
        print(f"[{time.monotonic() - t0:5.1f}s] {msg}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await new_context(browser, screenshot_dir)
        page = await context.new_page()

        try:
            await open_schedule(page, username, password, lap)

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

            lap(f"3️⃣  Looking for Cancha {court_num} across {len(options)} surface(s): "
                  f"{', '.join(label for _, label in options)}")
            row, found_on, states, booked_court = await find_court_across_surfaces(
                page, hour_tuple, court_num, options, target_short, days_ahead
            )
            if open_at is not None and datetime.now(TZ) < open_at + OPEN_WINDOW:
                row, found_on, states, booked_court = await wait_for_opening(
                    page, hour_tuple, court_num, options, target_short, days_ahead,
                    open_at, (row, found_on, states, booked_court), lap,
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
            await debug_shot(page, "03_schedule", screenshot_dir)

            # ── 4. CLICK RESERVAR ON THAT ROW ─────────────────────────────────
            lap(f"4️⃣  Reserving Cancha {booked_court} ({found_on}) at {fmt_hour(hour_tuple)} …")
            await act_and_wait(
                page, lambda: row.locator("input[type=button], button, a").first.click(timeout=5000),
                max_wait=5,
            )
            await debug_shot(page, "04_after_row_click", screenshot_dir)

            # ── 5. CLICK BTNBOTONBUSCAR (navigates to horaactual / opens modal) ─
            lap("5️⃣  Clicking BTNBOTONBUSCAR …")
            await page.locator("#BTNBOTONBUSCAR").wait_for(state="visible", timeout=10000)

            # Buscar can open a popup, navigate this page, or reveal the search box in place.
            # Watch for all three at once and move on at the first one, instead of waiting
            # out a popup timeout first (that alone used to cost 5-8s).
            popups = []

            def on_popup(new_page):
                popups.append(new_page)

            context.on("page", on_popup)
            url_before = page.url
            await page.locator("#BTNBOTONBUSCAR").click()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if popups or page.url != url_before:
                    break
                if await page.locator("#vTEXTOBUSCAR").count() > 0:
                    break
                if any([await f.locator("#vTEXTOBUSCAR").count() > 0 for f in page.frames[1:]]):
                    break
                await asyncio.sleep(0.1)
            context.remove_listener("page", on_popup)

            popup_page = popups[0] if popups else None
            if popup_page:
                await popup_page.wait_for_load_state("networkidle")
                print(f"   ✔ Popup opened: {popup_page.url}")
            else:
                if page.url != url_before:
                    await page.wait_for_load_state("networkidle")
                print(f"   ✔ No popup, current URL: {page.url}")

            working_page = popup_page if popup_page else page
            await debug_shot(working_page, "05_reserva_page", screenshot_dir)

            # ── 6. SEARCH FOR PARTNER ─────────────────────────────────────────
            lap(f"6️⃣  Searching for {partner} …")

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

            await debug_shot(working_page, "06_before_type", screenshot_dir)

            if target_frame:
                await target_frame.evaluate("document.getElementById('vTEXTOBUSCAR').focus()")
            await working_page.keyboard.type(partner.split()[0], delay=30)
            print(f"   ✔ Typed '{partner.split()[0]}'")
            await debug_shot(working_page, "07_autocomplete", screenshot_dir)

            # ── 7. SELECT PARTNER ──────────────────────────────────────────────
            lap(f"7️⃣  Selecting {partner} …")
            partner_upper = partner.upper()
            partner_frame = target_frame if target_frame else working_page.main_frame

            partner_el = partner_frame.locator(
                f"[id*='SOCIOFULLNOMBREAPELLIDO']:has-text('{partner_upper}') a"
            ).first
            await partner_el.wait_for(timeout=8000)
            # The selection posts back before Confirmar will accept it; wait for that.
            await act_and_wait(working_page, partner_el.click, max_wait=1)
            print(f"   ✔ Selected {partner}")
            await debug_shot(working_page, "08_partner_selected", screenshot_dir)

            # ── 8. CONFIRM ────────────────────────────────────────────────────
            lap("8️⃣  Confirming …")
            confirm_page = popup_page if popup_page else page
            confirmar = confirm_page.locator("button:has-text('Confirmar'), input[value='Confirmar']").first
            await confirmar.wait_for(timeout=8000)
            await confirmar.click()
            print("   ✔ Clicked Confirmar")

            si_btn = confirm_page.locator("#DVELOP_CONFIRMPANEL_ENTERContainer_SaveButton")
            await si_btn.wait_for(timeout=8000)
            await si_btn.click()
            lap("   ✔ Clicked Sí — booking submitted")
            await asyncio.sleep(2)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            final_shot = await screenshot(confirm_page, "09_final", screenshot_dir)

            message = f"Cancha {booked_court} ({found_on}) el {date_str} a las {fmt_hour(hour_tuple)} con {partner}"
            if substituted:
                message += f" — la Cancha {court_num} estaba ocupada, reservé la {booked_court}"
            lap(f"✅  Done! {message}")
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
            await close_context(context, screenshot_dir, username, password)
            await browser.close()


# ── Probe: watch the 08:00 opening without booking anything ──────────────────────

GRID_STATE_JS = """sel => {
    const rows = [...document.querySelectorAll(sel)];
    const visible = el => !!el && el.offsetParent !== null;
    const bookable = rows.filter(r => visible(r.querySelector('input[type=button], button, a')));
    return {
        rows: rows.length,
        bookable: bookable.length,
        sample: rows.slice(0, 2).map(r => r.innerText.replace(/\\s+/g, ' ').trim().slice(0, 60)),
    };
}"""


async def grid_state(page, target_short):
    """What the page shows right now: is the target date there, and how bookable is it."""
    body = await page.inner_text("body")
    state = await page.evaluate(GRID_STATE_JS, ROW_SELECTOR)
    state["date"] = target_short in body
    return state


def fmt_state(st):
    return (f"fecha {'✔' if st['date'] else '✘'} · filas {st['rows']} · "
            f"botones Reservar {st['bookable']}")


async def sleep_until(when):
    delay = (when - datetime.now(TZ)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)


async def run_probe(username, password, open_at=None, screenshot_dir="screenshots/probe"):
    """Log in and watch the grid around `open_at` (the 08:00 opening). Never books.

    With open_at=None it just times login + navigation and reports one snapshot, as a
    quick check that the bot can still get through the site.

    Returns {"success": bool, "lines": [str], "screenshots": [path]}.
    """
    lines, shots = [], []
    t0 = time.monotonic()

    def log(msg):
        stamp = datetime.now(TZ).strftime("%H:%M:%S.%f")[:-4]
        line = f"{stamp} {msg}"
        print(line)
        lines.append(line)

    def lap(msg):
        print(f"[{time.monotonic() - t0:5.1f}s] {msg}")

    base = open_at or datetime.now(TZ)
    target_short = (base + timedelta(days=1)).strftime("%d/%m/%y")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await new_context(browser, screenshot_dir)
        page = await context.new_page()
        try:
            await open_schedule(page, username, password, lap)
            schedule_url = page.url
            log(f"login + Días disponibles: {time.monotonic() - t0:.1f}s")
            options = await surface_options(page)
            log(f"superficies: {', '.join(l for _, l in options) or 'ninguna'}")

            async def refresh():
                """Reload and go back to the target date; returns seconds it took."""
                t = time.monotonic()
                await page.goto(schedule_url, wait_until="domcontentloaded")
                await page.locator(f"{ROW_SELECTOR}, #BTNBTNRESERVAR").first.wait_for(timeout=10000)
                if await page.locator(ROW_SELECTOR).count() == 0:
                    # The URL alone doesn't bring the grid back; go in through the button.
                    await act_and_wait(page, lambda: page.locator("#BTNBTNRESERVAR").click(), max_wait=5)
                    await page.locator(ROW_SELECTOR).first.wait_for(timeout=10000)
                await goto_target_date(page, target_short, 1)
                return time.monotonic() - t

            t = time.monotonic()
            clicks = await goto_target_date(page, target_short, 1)
            log(f"ir a {target_short}: {'no aparece' if clicks is None else f'{clicks} clic(s)'} "
                f"en {time.monotonic() - t:.1f}s")
            st = await grid_state(page, target_short)
            log(f"ahora: {fmt_state(st)} · {' | '.join(st['sample'])}")
            shots.append(await screenshot(page, "probe_1_inicio", screenshot_dir))

            if open_at is None:
                try:
                    lap_t = await refresh()
                    log(f"recargar y volver a la fecha: {lap_t:.1f}s")
                except Exception as e:
                    log(f"recargar falló: {str(e).splitlines()[0][:120]}")
                return {"success": True, "lines": lines, "screenshots": shots}

            # Around the opening: compare what the page shows by itself with a fresh load.
            await sleep_until(open_at - timedelta(seconds=10))
            log("— mirando la apertura —")
            last, changed_shot = None, False
            end = open_at + timedelta(seconds=90)
            while datetime.now(TZ) < end:
                passive = await grid_state(page, target_short)
                try:
                    took = await refresh()
                    fresh = await grid_state(page, target_short)
                    summary = f"sin recargar: {fmt_state(passive)} || recargando ({took:.1f}s): {fmt_state(fresh)}"
                except Exception as e:
                    fresh = None
                    summary = f"sin recargar: {fmt_state(passive)} || recarga falló: {str(e).splitlines()[0][:80]}"
                key = (passive["date"], passive["bookable"], fresh and fresh["date"], fresh and fresh["bookable"])
                past_open = datetime.now(TZ) >= open_at
                # Log every change, plus a heartbeat every tick in the 20s around 08:00.
                near = abs((datetime.now(TZ) - open_at).total_seconds()) <= 10
                if key != last or near:
                    log(summary)
                if key != last and last is not None and not changed_shot:
                    shots.append(await screenshot(page, "probe_2_cambio", screenshot_dir))
                    changed_shot = True
                last = key
                await asyncio.sleep(0.5 if near else (5 if past_open else 1))
            shots.append(await screenshot(page, "probe_3_final", screenshot_dir))
            log("— fin —")
            return {"success": True, "lines": lines, "screenshots": shots}
        except Exception as e:
            log(f"❌ {str(e).strip().splitlines()[0][:200]}")
            try:
                shots.append(await screenshot(page, "probe_ERROR", screenshot_dir))
            except Exception:
                pass
            return {"success": False, "lines": lines, "screenshots": shots}
        finally:
            await close_context(context, screenshot_dir, username, password)
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
