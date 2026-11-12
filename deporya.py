"""DeporYA without a browser: the site's own requests, sent directly.

The site is a GeneXus web app. Every button is one POST to the page's own URL carrying a
JSON body: the event name plus a fixed list of values ("parms") the page's script says
that event needs. Which values, and in which order, is published in <page>.js
(`EvtParms`); the values themselves come from the page's state (hidden GXState, form
fields, dropdowns) and from whatever earlier responses changed. A few values are signed
by the server (`gxhash_<field>`) and are sent back with their signature.

Checked against traffic recorded from the real site on 2026-11-12: login, opening the
schedule, loading a day and clicking a slot rebuild the browser's requests exactly.
What happens after a slot is accepted (partner search, Confirmar) is not recorded yet,
so booking still goes through the browser flow in reserve.py.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import time
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit

BASE_URL = "https://agenbot.net/deporyatenis"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/125.0.0.0 Safari/537.36")

# EstadoHoraId values in the day's data.
FREE, BOOKED, CLASS = 1, 2, 5


class SiteError(RuntimeError):
    """The site answered, but not with what this client expects."""


# ── Reading a page ───────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<(input|select)\b([^>]*)>", re.I)
_ATTR_RE = re.compile(r"""([\w-]+)\s*=\s*("([^"]*)"|'([^']*)')""")
_SELECT_RE = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.I | re.S)
_OPTION_RE = re.compile(r"<option\b([^>]*)>(.*?)</option>", re.I | re.S)


def _attrs(text):
    return {m.group(1).lower(): html_lib.unescape(m.group(3) if m.group(3) is not None else m.group(4))
            for m in _ATTR_RE.finditer(text)}


def parse_page(text):
    """(hiddens, fields, combos) of a page.

    hiddens  the GXState values (full page) or the saved JSON response (in-app page)
    fields   visible form values: {name: str | bool}
    combos   dropdowns: {name: {"s": selected, "v": [[value, label], ...]}}
    """
    hiddens = {}
    m = re.search(r"""<input[^>]*name="GXState"[^>]*value='(.*?)'\s*/?>""", text, re.S)
    if m:
        hiddens = json.loads(html_lib.unescape(m.group(1)))
    else:
        at = text.find("gx.ajax.saveJsonResponse(")
        if at < 0:
            raise SiteError("page has no GXState")
        saved, _ = json.JSONDecoder().raw_decode(text, at + len("gx.ajax.saveJsonResponse("))
        hiddens = saved.get("gxHiddens", {})

    fields, combos = {}, {}
    for m in _TAG_RE.finditer(text):
        a = _attrs(m.group(2))
        name = a.get("name")
        if not name or name == "GXState" or m.group(1).lower() == "select":
            continue
        kind = a.get("type", "text").lower()
        if kind in ("button", "submit", "hidden"):
            continue
        fields[name] = ("checked" in m.group(2).lower().split()) if kind == "checkbox" else a.get("value", "")
    for m in _SELECT_RE.finditer(text):
        name = _attrs(m.group(1)).get("name")
        if not name:
            continue
        options, selected = [], None
        for o in _OPTION_RE.finditer(m.group(2)):
            value = _attrs(o.group(1)).get("value", "")
            options.append([value, html_lib.unescape(o.group(2))])
            if re.search(r"\bselected\b", o.group(1), re.I):
                selected = value
        if selected is None and options:
            selected = options[0][0]
        combos[name] = {"s": selected or "", "v": options}
    # A dropdown the page fills from its state rather than from <option> tags.
    for key, value in hiddens.items():
        if key.endswith("_Values") and isinstance(value, str):
            name = key[: -len("_Values")]
            if not combos.get(name, {}).get("v"):
                combos[name] = _combo(value)
    return hiddens, fields, combos


def _combo(values_json):
    data = json.loads(values_json)
    return {"s": data.get("s", ""), "v": data.get("v", [])}


# ── Reading <page>.js: which values each event sends ─────────────────────────────

_EVT_RE = re.compile(r"""EvtParms(?:\[(?P<q>"(?:[^"\\]|\\.)*")\]|\.(?P<n>\w+))\s*=\s*\[""")
_PAIR_RE = re.compile(r"""(\w+):("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|![01]|\d+)""")


def _matching(text, start, open_ch, close_ch):
    """Index just past the bracket that closes the one at `start` (strings skipped)."""
    depth, i, quote = 0, start, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise SiteError("unbalanced brackets in page script")


def parse_evt_parms(js):
    """{event: [spec, ...]} — the values each event sends, in order.

    A spec is the script's own description: {av, fld, pic, hsh, ctrl, prop, grid}.
    """
    events = {}
    for m in _EVT_RE.finditer(js):
        name = json.loads(m.group("q")) if m.group("q") else m.group("n")
        outer = m.end() - 1                       # the '[' of [[inputs],[outputs]]
        inner = js.index("[", outer + 1)
        end = _matching(js, inner, "[", "]")
        specs, i = [], inner + 1
        while True:
            start = js.find("{", i, end)
            if start < 0:
                break
            stop = _matching(js, start, "{", "}")
            spec = {}
            for key, raw in _PAIR_RE.findall(js[start:stop]):
                if raw in ("!0", "!1"):
                    spec[key] = raw == "!0"
                elif raw.isdigit():
                    spec[key] = int(raw)
                else:
                    spec[key] = raw[1:-1]
            specs.append(spec)
            i = stop
        events[name] = specs
    return events


# ── The address every event is posted to ─────────────────────────────────────────

def _aes_tables():
    sbox, p, q = [0] * 256, 1, 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ (q << 1 | q >> 7) ^ (q << 2 | q >> 6) ^ (q << 3 | q >> 5) ^ (q << 4 | q >> 4)
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_SBOX = _aes_tables()


def _xtime(b):
    return ((b << 1) ^ 0x1B) & 0xFF if b & 0x80 else b << 1


def aes128_encrypt_block(key, block):
    """AES-128 of one 16-byte block. (Only this one block is ever needed, so no dependency.)"""
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        w = list(words[i - 1])
        if i % 4 == 0:
            w = [_SBOX[b] for b in w[1:] + w[:1]]
            w[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([a ^ b for a, b in zip(words[i - 4], w)])
    keys = [sum(words[r * 4:r * 4 + 4], []) for r in range(11)]

    s = [b ^ k for b, k in zip(block, keys[0])]
    for rnd in range(1, 11):
        s = [_SBOX[b] for b in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]          # ShiftRows
        if rnd < 10:                                                 # MixColumns
            mixed = []
            for c in range(0, 16, 4):
                a = s[c:c + 4]
                t = a[0] ^ a[1] ^ a[2] ^ a[3]
                mixed += [a[i] ^ t ^ _xtime(a[i] ^ a[(i + 1) % 4]) for i in range(4)]
            s = mixed
        s = [b ^ k for b, k in zip(s, keys[rnd])]
    return bytes(s)


def ajax_token(key_hex, iv_hex):
    """What the browser puts first in an event URL: IV + AES-CBC("gxfullajaxEvt")."""
    iv = bytes.fromhex(iv_hex)
    plain = b"gxfullajaxEvt".ljust(16, b"\0")
    cipher = aes128_encrypt_block(bytes.fromhex(key_hex), bytes(a ^ b for a, b in zip(plain, iv)))
    return (iv_hex + cipher.hex()).lower()


# ── One loaded page ──────────────────────────────────────────────────────────────

_DATE_YMD = re.compile(r"^\d{4}/\d\d/\d\d$")
_DATE_DMY = re.compile(r"^(\d\d)/(\d\d)/(\d\d)$")


class GxPage:
    """A page as the browser would hold it: its state, and how to fire its events."""

    def __init__(self, url, text, js):
        self.url = url
        self.name = urlsplit(url).path.rsplit("/", 1)[-1].split(".")[0].lower()
        self.hiddens, self.fields, self.combos = parse_page(text)
        self.events = parse_evt_parms(js)
        self.values = {}          # set by the caller or by responses; wins over the page
        self.refreshed = set()    # grids an event response has redrawn
        self._fld_of = {s["av"]: s["fld"] for specs in self.events.values()
                        for s in specs if "av" in s and "fld" in s}
        self._grid_names = {}
        for key, value in self.hiddens.items():
            if key.endswith("ContainerData") and isinstance(value, str) and value.startswith("{"):
                self._grid_names[key[: -len("ContainerData")].upper()] = json.loads(value).get("GridName")
            elif key.endswith("ContainerDataV"):
                self._grid_names.setdefault(key[: -len("ContainerDataV")].upper(), key[: -len("ContainerDataV")])

    # values ------------------------------------------------------------------
    def get(self, fld):
        if fld in self.values:
            return self.values[fld]
        if fld in self.fields:
            return self.fields[fld]
        if fld in self.combos:
            return self.combos[fld]["s"]
        return self.hiddens.get(fld)

    def _parm(self, spec):
        if "fld" in spec:
            value = self.get(spec["fld"])
            if re.search(r"[Z9]", spec.get("pic", "")):
                return int(str(value).strip() or 0)
            if isinstance(value, str):
                if _DATE_YMD.match(value):
                    return value + " 00:00:00"
                m = _DATE_DMY.match(value)
                if m:
                    return f"20{m.group(3)}/{m.group(2)}/{m.group(1)} 00:00:00"
            return value
        if "ctrl" in spec and spec.get("prop") == "GridRC":
            return {"gridRC": self.hiddens.get(f"nRC_GXsfl_{spec['grid']}", "0"),
                    "rowSuffix": "", "grid": spec["grid"]}
        if "ctrl" in spec and "prop" in spec:
            return self.hiddens.get(f"{spec['ctrl']}_{spec['prop']}")
        if "ctrl" in spec:
            combo = self.combos.get(spec["ctrl"], {"s": "", "v": []})
            return {"s": str(self.get(spec["ctrl"]) if spec["ctrl"] in self.values else combo["s"]),
                    "v": combo["v"]}
        return self.values.get(spec["av"], self.hiddens.get(spec["av"]))

    def _grids(self):
        grids = {}
        for key, count in self.hiddens.items():
            if not key.startswith("nRC_GXsfl_"):
                continue
            grid_id = int(key.rsplit("_", 1)[1])
            name = None
            for specs in self.events.values():
                for s in specs:
                    if s.get("prop") == "GridRC" and s.get("grid") == grid_id:
                        name = self._grid_names.get(s["ctrl"])
            if name is None and len(self._grid_names) == 1:
                name = next(iter(self._grid_names.values()))
            if name:
                last = 0 if grid_id in self.refreshed else int(count or 0)
                grids[name] = {"id": grid_id, "lastRow": last, "pRow": ""}
        return grids

    # events ------------------------------------------------------------------
    def body(self, event, row=None, grid=None):
        """The JSON the browser would POST for `event` (row = 1-based grid row)."""
        if event not in self.events:
            raise SiteError(f"{self.name} has no event {event}")
        specs = self.events[event]
        body = {
            "MPage": False, "cmpCtx": "",
            "parms": [self._parm(s) for s in specs],
            "hsh": [{"hsh": self.hiddens.get("gxhash_" + s["fld"]), "row": ""}
                    for s in specs if s.get("hsh") and "fld" in s],
            "objClass": self.name, "pkgName": "GeneXus.Programs",
            "events": [event],
        }
        if row is not None:
            body["grid"] = grid
        body["grids"] = self._grids()
        if row is not None:
            body["row"] = f"{row:04d}"
            body["pRow"] = ""
        return body

    def event_url(self, now_ms=None):
        parts = urlsplit(self.url)
        token = ajax_token(self.hiddens["GX_AJAX_KEY"], self.hiddens["GX_AJAX_IV"])
        query = ",".join(p for p in (token, parts.query,
                                     f"gx-no-cache={now_ms or int(time.time() * 1000)}") if p)
        return f"{parts.scheme}://{parts.netloc}{parts.path}?{query}"

    def headers(self):
        return {
            "Content-Type": "application/json",
            "GxAjaxRequest": "1",
            "AJAX_SECURITY_TOKEN": self.hiddens["AJAX_SECURITY_TOKEN"],
            "X-GXAUTH-TOKEN": self.hiddens["GX_AUTH_" + self.name.upper()],
            "Referer": self.url,
            "Origin": "{0.scheme}://{0.netloc}".format(urlsplit(self.url)),
        }

    def apply(self, response):
        """Take in an event's response, as the page's script would."""
        self.hiddens.update(response.get("gxHiddens") or {})
        for group in response.get("gxValues") or []:
            if group.get("IsMasterPage") == "True":
                continue
            for av, value in group.items():
                if av in self._fld_of:
                    self.values[self._fld_of[av]] = value
        for group in response.get("gxProps") or []:
            for ctrl, props in group.items():
                if isinstance(props, dict) and "Values" in props:
                    self.combos[ctrl] = _combo(props["Values"])
        for grid in response.get("gxGrids") or []:
            for key, value in grid.items():
                if key.endswith(("_nEOF", "_nFirstRecordOnPage")):
                    self.values[key] = value
            for key in self.hiddens:
                if key.startswith("nRC_GXsfl_"):
                    self.refreshed.add(int(key.rsplit("_", 1)[1]))


def messages(response):
    """Texts the site wants shown to the user after an event."""
    return [m.get("text", "") for m in (response.get("gxMessages") or {}).get("MAIN", [])]


def redirect_of(response):
    for command in response.get("gxCommands") or []:
        if "redirect" in command:
            return command["redirect"]["url"]
        if "popup" in command:
            return command["popup"].get("url")
    return None


# ── The day's schedule ───────────────────────────────────────────────────────────

class Slot:
    """One court-hour of a day. `row` is its 1-based position in the grid."""

    def __init__(self, row, raw):
        self.row = row
        self.court = int(raw["CanchaId"])
        self.surface = raw.get("TipoCanchaDesc", "")
        self.hour = raw.get("StrHora", "")
        self.status = int(raw["EstadoHoraId"])
        self.status_text = raw.get("EstadoHoraDesc", "")

    @property
    def free(self):
        return self.status == FREE

    def __repr__(self):
        return f"<Cancha {self.court} {self.surface} {self.hour} {self.status_text}>"


def slots_of(day_data):
    return [Slot(i, raw) for i, raw in enumerate((day_data or {}).get("Horas", []), start=1)]


def pick_slot(slots, court, hour):
    """(slot, why) for a booking request: the court asked for, else the closest-numbered
    free court on the same surface at that hour. slot is None when nothing is bookable;
    `why` is "free", "substitute", "taken", "full" (surface full) or "missing"."""
    at_hour = [s for s in slots if s.hour == hour]
    wanted = [s for s in at_hour if s.court == court]
    if wanted and wanted[0].free:
        return wanted[0], "free"
    surface = next((s.surface for s in slots if s.court == court), None)
    if surface is None:
        return None, "missing"
    others = [s for s in at_hour if s.free and s.surface == surface and s.court != court]
    if others:
        return min(others, key=lambda s: (abs(s.court - court), s.court)), "substitute"
    return None, ("full" if wanted else "taken")


# ── The client ───────────────────────────────────────────────────────────────────

class DeporYA:
    """A logged-in session. Use as `async with DeporYA() as site:`."""

    def __init__(self, base_url=BASE_URL, timeout=30.0, transport=None):
        import httpx
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "es-UY"},
        )
        self._js = {}
        self.schedule = None   # the "Días disponibles" page, once opened

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()

    async def _load(self, url, referer=None):
        url = urljoin(self.base_url + "/", url)
        r = await self.http.get(url, headers={"Referer": referer} if referer else None)
        r.raise_for_status()
        name = urlsplit(str(r.url)).path.rsplit("/", 1)[-1].split(".")[0].lower()
        if name not in self._js:
            js = await self.http.get(f"{self.base_url}/{name}.js")
            js.raise_for_status()
            self._js[name] = js.text
        return GxPage(str(r.url), r.text, self._js[name])

    async def _fire(self, page, event, row=None, grid=None):
        payload = json.dumps(page.body(event, row, grid), separators=(",", ":"), ensure_ascii=False)
        r = await self.http.post(page.event_url(), headers=page.headers(), content=payload.encode("utf-8"))
        r.raise_for_status()
        try:
            response = r.json()
        except ValueError:
            raise SiteError(f"{page.name}/{event}: the site did not answer in JSON "
                            f"(session expired?)") from None
        page.apply(response)
        return response

    async def login(self, username, password):
        page = await self._load("login.aspx")
        page.values.update(vUSERNAME=username, vUSERPASSWORD=password)
        response = await self._fire(page, "ENTER")
        target = redirect_of(response)
        if not target or "login" in target.lower():
            raise SiteError("❌ Login failed" + "".join(f": {t}" for t in messages(response)[:1]))
        self.home = await self._load(target, referer=page.url)
        return self.home

    async def open_schedule(self):
        response = await self._fire(self.home, "'DOBTNRESERVAR'")
        target = redirect_of(response)
        if not target:
            raise SiteError("❌ Días disponibles no abrió" + "".join(f": {t}" for t in messages(response)[:1]))
        self.schedule = await self._load(target, referer=self.home.url)
        return self.schedule

    async def load_day(self, day: date):
        """The slots of `day` (one request). Also makes it the day a click applies to."""
        page = self.schedule
        page.values["vFECHA"] = day.strftime("%Y/%m/%d") + " 00:00:00"
        response = await self._fire(page, "REFRESH")
        data = page.get("vSDTHORASDELDIA") or {}
        if isinstance(data, str):
            data = json.loads(data)
        got = (data.get("Fecha") or "")[:10]
        if got != day.isoformat():
            raise SiteError(f"asked for {day.isoformat()}, the site answered with {got or 'nothing'}"
                            + "".join(f": {t}" for t in messages(response)[:1]))
        return slots_of(data)

    async def click_slot(self, slot):
        """Press 'Reserva' on a slot of the day last loaded. Returns the site's answer:
        {"next": url | None, "messages": [str], "raw": response}."""
        response = await self._fire(self.schedule, "VRESERVARHORA.CLICK", row=slot.row, grid=129)
        return {"next": redirect_of(response), "messages": messages(response), "raw": response}


def today(tz):
    return datetime.now(tz).date()


async def run_direct_probe(username, password, tz, days_ahead=1, court=None, hour=None, transport=None):
    """Log in and read a day without a browser, timing each step. Never clicks a slot.

    Returns {"success": bool, "lines": [str], "screenshots": []} like reserve.run_probe.
    """
    lines = []

    async def timed(label, coro):
        t = time.monotonic()
        result = await coro
        lines.append(f"{label}: {time.monotonic() - t:.2f}s")
        return result

    try:
        async with DeporYA(transport=transport) as site:
            await timed("login", site.login(username, password))
            await timed("Días disponibles", site.open_schedule())
            day = datetime.now(tz).date()
            day = date.fromordinal(day.toordinal() + days_ahead)
            slots = await timed(f"leer el {day:%d/%m}", site.load_day(day))
            await timed("leerlo de nuevo", site.load_day(day))
            counts = {}
            for s in slots:
                kind = {FREE: "libres", BOOKED: "reservadas", CLASS: "clases/bloqueos"}.get(s.status, "otras")
                counts[kind] = counts.get(kind, 0) + 1
            lines.append(f"{len(slots)} horarios: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
            courts = sorted({s.court for s in slots})
            lines.append("canchas: " + ", ".join(
                f"{c} ({next(s.surface for s in slots if s.court == c).lower()})" for c in courts))
            if court and hour:
                slot, why = pick_slot(slots, int(court), hour)
                wanted = next((s for s in slots if s.court == int(court) and s.hour == hour), None)
                lines.append(f"Cancha {court} {hour}: " + (wanted.status_text if wanted else "no figura")
                             + (f" → reservaría la {slot.court}" if slot and why == "substitute" else ""))
            return {"success": True, "lines": lines, "screenshots": []}
    except Exception as e:
        lines.append(f"❌ {type(e).__name__}: {str(e).strip().splitlines()[0][:200] if str(e).strip() else ''}")
        return {"success": False, "lines": lines, "screenshots": []}
