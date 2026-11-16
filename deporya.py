"""DeporYA without a browser: the site's own requests, sent directly.

The site is a GeneXus web app. Every button is one POST to the page's own URL carrying a
JSON body: the event name plus a fixed list of values ("parms") the page's script says
that event needs. Which values, and in which order, is published in <page>.js
(`EvtParms`); the values themselves come from the page's state (hidden GXState, form
fields, dropdowns) and from whatever earlier responses changed. A few values are signed
by the server (`gxhash_<field>`) and are sent back with their signature.

Checked against traffic recorded from the real site (2026-11-12 to 2026-11-15): login,
opening the schedule, loading a day, clicking a slot, Buscar, the partner search, picking
a partner and reporting them to the booking screen rebuild the browser's requests
exactly. Confirmar and the final "Sí" were never recorded: they are built from the site's
own scripts (horaactual.js, the confirm panel's script) and checked by the result: after
"Sí" the day is read again and the slot must show as booked.
"""
from __future__ import annotations

import gzip
import html as html_lib
import json
import os
import re
import time
import asyncio
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, quote_plus, urljoin, urlsplit

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


def gx_encrypt(key_hex, iv_hex, text):
    """How the site's script encrypts what it puts in a URL: IV + AES-CBC(zero-padded text)."""
    key, prev = bytes.fromhex(key_hex), bytes.fromhex(iv_hex)
    plain = text.encode("utf-8")
    plain = plain.ljust(max(16, -(-len(plain) // 16) * 16), b"\0")
    out = b""
    for i in range(0, len(plain), 16):
        prev = aes128_encrypt_block(key, bytes(a ^ b for a, b in zip(plain[i:i + 16], prev)))
        out += prev
    return (iv_hex + out.hex()).lower()


def ajax_token(key_hex, iv_hex):
    """What the browser puts first in an event URL."""
    return gx_encrypt(key_hex, iv_hex, "gxfullajaxEvt")


# ── One loaded page ──────────────────────────────────────────────────────────────

_DATE_YMD = re.compile(r"^\d{4}/\d\d/\d\d$")
_DATE_DMY = re.compile(r"^(\d\d)/(\d\d)/(\d\d)$")
_NUMBER_PIC = re.compile(r"^[Z9]+$")
_DECIMAL_PIC = re.compile(r"^[Z9]+\.9+$")


def _decimal(value):
    """A price as the page's script reads it ("1.234,50" on the page, or a plain number)."""
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value or "").strip()
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        try:
            number = float(text or 0)
        except ValueError:
            number = 0.0
    return int(number) if number.is_integer() else number
_NO_DATE = "0001/01/01 "


class GxPage:
    """A page as the browser would hold it: its state, and how to fire its events."""

    def __init__(self, url, text, js):
        self.url = url
        self.name = urlsplit(url).path.rsplit("/", 1)[-1].split(".")[0].lower()
        self.hiddens, self.fields, self.combos = parse_page(text)
        self.events = parse_evt_parms(js)
        self.values = {}          # set by the caller or by responses; wins over the page
        self.rows = {}            # grid cells from responses: {"SOCIOID_0003": "4281", ...}
        self.grid_rows = {}       # rows each grid was redrawn with: {"Grid1": 4}
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

    def _parm(self, spec, row=None):
        if "fld" in spec:
            value = self._row_value(spec["fld"], row)
            if value is None:
                value = self.get(spec["fld"])
            if _NUMBER_PIC.match(spec.get("pic", "")):
                return int(str(value or 0).strip() or 0)
            if _DECIMAL_PIC.match(spec.get("pic", "")):
                return _decimal(value)
            if value is None and "pic" in spec:
                return ""                     # a text box that was never filled in
            if isinstance(value, str):
                if _DATE_YMD.match(value):
                    return value + " 00:00:00"
                m = _DATE_DMY.match(value)
                if m:
                    return f"20{m.group(3)}/{m.group(2)}/{m.group(1)} 00:00:00"
                if value.startswith(_NO_DATE):    # a time of day: the script dates it 2001
                    return "2001/01/01 " + value[len(_NO_DATE):]
            if "grid" in spec and isinstance(value, list):
                # A list shown as a grid travels with its current item (seen with one item).
                return [{"pRow": "", "c": value, "v": value[0] if value else {}}]
            return value
        if "ctrl" in spec and spec.get("prop") == "GridRC":
            return {"gridRC": self.hiddens.get(f"nRC_GXsfl_{spec['grid']}", "0"),
                    "rowSuffix": "", "grid": spec["grid"]}
        if "ctrl" in spec and spec.get("prop") == "Visible":
            return str(self.hiddens.get(f"{spec['ctrl']}_Visible", "1")) != "0"
        if "ctrl" in spec and "prop" in spec:
            return self.hiddens.get(f"{spec['ctrl']}_{spec['prop']}")
        if "ctrl" in spec:
            combo = self.combos.get(spec["ctrl"], {"s": "", "v": []})
            return {"s": str(self.get(spec["ctrl"]) if spec["ctrl"] in self.values else combo["s"]),
                    "v": combo["v"]}
        return self.values.get(spec["av"], self.hiddens.get(spec["av"]))

    def _row_value(self, fld, row):
        """A grid column's value on `row` (1-based), or None if `fld` is not a column."""
        if row is None:
            return None
        return self.rows.get(f"{fld}_{row:04d}")

    def _grids(self, row=None, grid=None):
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
                last = int(count or 0)
                if grid_id in self.refreshed:
                    last = self.grid_rows.get(name, 0)   # the browser counts the rows it was sent
                if row is not None and grid_id == grid:
                    last = row - 1            # an event on a row: the rows before it
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
            "parms": [self._parm(s, row) for s in specs],
            "hsh": [self._hsh(s["fld"], row) for s in specs if s.get("hsh") and "fld" in s],
            "objClass": self.name, "pkgName": "GeneXus.Programs",
            "events": [event],
        }
        if row is not None:
            body["grid"] = grid
        body["grids"] = self._grids(row, grid)
        if row is not None:
            body["row"] = f"{row:04d}"
            body["pRow"] = ""
        return body

    def _hsh(self, fld, row):
        if self._row_value(fld, row) is not None:
            return {"hsh": self.hiddens.get(f"gxhash_{fld}_{row:04d}"), "row": f"{row:04d}"}
        return {"hsh": self.hiddens.get("gxhash_" + fld), "row": ""}

    def refresh_url(self, grid_name, parms, now_ms=None):
        """Where the script GETs a grid's rows again (a search box does this on every key)."""
        parts = urlsplit(self.url)
        token = gx_encrypt(self.hiddens["GX_AJAX_KEY"], self.hiddens["GX_AJAX_IV"],
                           ",".join([f"gxajaxGridRefresh_{grid_name}"] + [str(p) for p in parms]))
        return (f"{parts.scheme}://{parts.netloc}{parts.path}?{token},"
                f"gx-no-cache={now_ms or int(time.time() * 1000)}")

    def refresh_headers(self):
        return {"GxAjaxRequest": "1", "AJAX_SECURITY_TOKEN": self.hiddens["AJAX_SECURITY_TOKEN"],
                "Referer": self.url}

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
            if grid.get("GridName"):
                self.grid_rows[grid["GridName"]] = sum(1 for k in grid if k.isdigit())
            cells = {}
            for key, item in grid.items():
                if key.isdigit() and isinstance(item, dict):
                    for prop in item.get("Props") or []:
                        if prop and isinstance(prop[0], str):
                            cells[prop[0]] = str(prop[-1]).rstrip()
            if cells:
                self.rows = cells
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
    """Where the site sends the page next (a redirect first, else a popup it opens)."""
    commands = [c for c in response.get("gxCommands") or [] if isinstance(c, dict)]
    for command in commands:
        if isinstance(command.get("redirect"), dict):
            return command["redirect"].get("url")
    return popup_of(response)


def popup_of(response):
    """The page a response opens in a popup. (It comes as a list: [url, ...].)"""
    for command in response.get("gxCommands") or []:
        popup = command.get("popup") if isinstance(command, dict) else None
        if isinstance(popup, list) and popup:
            return popup[0]
        if isinstance(popup, dict):
            return popup.get("url")
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


class Partner:
    """One row of the partner search. `row` is its 1-based position in the results."""

    def __init__(self, row, cells):
        self.row = row
        self.id = int(cells.get(f"SOCIOID_{row:04d}") or 0)
        self.doc = cells.get(f"SOCIODOC_{row:04d}", "")
        self.name = cells.get(f"SOCIOFULLNOMBREAPELLIDO_{row:04d}", "")

    def __repr__(self):
        return f"<{self.name} #{self.id}>"

    @classmethod
    def known(cls, ids):
        """A partner whose ids were learned from an earlier search: {"id", "doc", "name"}."""
        p = cls.__new__(cls)
        p.row, p.id, p.doc, p.name = None, int(ids["id"]), str(ids["doc"]), str(ids["name"])
        return p

    def ids(self):
        return {"id": self.id, "doc": self.doc, "name": self.name}


def on_booking(partner, players):
    return any(set(partner.name.split()) <= set(name.split()) for name in players)


def partners_of(page):
    rows = sorted(int(k.rsplit("_", 1)[1]) for k in page.rows if k.startswith("SOCIOID_"))
    return [Partner(r, page.rows) for r in rows]


def pick_partner(partners, name):
    """The search result that is `name` (words in any order, accents and case aside)."""
    def words(text):
        text = text.upper()
        for a, b in zip("ÁÉÍÓÚÜ", "AEIOUU"):
            text = text.replace(a, b)
        return set(text.split())
    wanted = words(name)
    exact = [p for p in partners if words(p.name) == wanted]
    if exact:
        return exact[0]
    close = [p for p in partners if wanted <= words(p.name)]
    return close[0] if len(close) == 1 else None


def write_traffic(path, entries, lines, secrets):
    """Save what a browser-free run sent and got, in the shape of the browser recordings
    (gzip, credentials masked), so both kinds can be read the same way."""
    text = json.dumps({"log": {"creator": {"name": "deporya.py"}, "entries": entries,
                               "_bot": {"commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "?")[:7],
                                        "lines": lines}}}, ensure_ascii=False)
    for secret, mask in zip(secrets, ("<PASSWORD>", "<USERNAME>")):
        if not secret:
            continue
        for form in {secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]}:
            text = text.replace(form, mask)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)


# ── The client ───────────────────────────────────────────────────────────────────

class DeporYA:
    """A logged-in session. Use as `async with DeporYA() as site:`."""

    def __init__(self, base_url=BASE_URL, timeout=60.0, transport=None):
        import httpx
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(
            timeout=timeout,  # at 08:00 the site has taken 25s to answer one request
            follow_redirects=True, transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "es-UY"},
        )
        self._js = {}
        self.schedule = None   # the "Días disponibles" page, once opened
        self.booking = None    # the booking screen of a slot, once one was accepted
        self.search = None     # its partner search box, once opened
        self.confirm_sent = False  # True once "Sí" has left: the booking may exist
        self.traffic = []      # every request and answer, for write_traffic()

    def _record(self, r, started):
        self.traffic.append({
            "startedDateTime": started.isoformat().replace("+00:00", "Z"),
            "time": (datetime.now(timezone.utc) - started).total_seconds() * 1000,
            "request": {"method": r.request.method, "url": str(r.request.url),
                        "headers": [{"name": k, "value": v} for k, v in r.request.headers.items()
                                    if k.lower() != "cookie"],
                        "postData": {"text": r.request.content.decode("utf-8", "replace")}},
            "response": {"status": r.status_code,
                         "headers": [],
                         "content": {"mimeType": r.headers.get("content-type", ""), "text": r.text}},
        })

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()

    async def prefetch(self, *names):
        """Fetch pages' scripts ahead (they say what each event sends), so that the
        booking screens cost no extra requests once the day opens."""
        for name in names:
            if name not in self._js:
                js = await self.http.get(f"{self.base_url}/{name}.js")
                js.raise_for_status()
                self._js[name] = js.text

    async def _load(self, url, referer=None):
        url = urljoin(self.base_url + "/", url)
        started = datetime.now(timezone.utc)
        r = await self.http.get(url, headers={"Referer": referer} if referer else None)
        self._record(r, started)
        r.raise_for_status()
        name = urlsplit(str(r.url)).path.rsplit("/", 1)[-1].split(".")[0].lower()
        if name not in self._js:
            js = await self.http.get(f"{self.base_url}/{name}.js")
            js.raise_for_status()
            self._js[name] = js.text
        return GxPage(str(r.url), r.text, self._js[name])

    async def _fire(self, page, event, row=None, grid=None, may_be_empty=False):
        payload = json.dumps(page.body(event, row, grid), separators=(",", ":"), ensure_ascii=False)
        started = datetime.now(timezone.utc)
        r = await self.http.post(page.event_url(), headers=page.headers(), content=payload.encode("utf-8"))
        self._record(r, started)
        r.raise_for_status()
        if may_be_empty and not r.text.strip():
            return {}
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

    async def open_booking(self, slot):
        """Press 'Reserva' on a slot and open its booking screen. Returns click_slot's answer;
        "next" is None when the site refused the slot (its reason is in "messages")."""
        answer = await self.click_slot(slot)
        if answer["next"]:
            self.booking = await self._load(answer["next"], referer=self.schedule.url)
        return answer

    async def find_partners(self, text):
        """Press Buscar on the booking screen and search its box for `text`. → [Partner]"""
        response = await self._fire(self.booking, "'DOBOTONBUSCAR'")
        popup = next((c["popup"][0] for c in response.get("gxCommands") or [] if "popup" in c), None)
        if not popup:
            raise SiteError("❌ Buscar no abrió la búsqueda de socios"
                            + "".join(f": {t}" for t in messages(response)[:1]))
        page = self.search = await self._load(popup + ",gxPopupLevel%3D0%3B", referer=self.booking.url)
        # What the box sends on every key: the text, then three values of the page.
        parms = [quote(text, safe=""), page.get("vEMPRESAID"), page.get("vBLOQUEACARNESALUD"),
                 str(page.get("vTODAY") or "").replace("/", "")[:8]]
        started = datetime.now(timezone.utc)
        r = await self.http.get(page.refresh_url("Grid1", parms), headers=page.refresh_headers())
        self._record(r, started)
        r.raise_for_status()
        try:
            page.apply(r.json())
        except ValueError:
            raise SiteError("the partner search did not answer in JSON") from None
        return partners_of(page)

    async def choose_partner(self, partner):
        """Pick a search result and report it to the booking screen, as closing the search
        box does. Returns {"messages": [str], "players": [names], "raw": response}."""
        picked = await self._fire(self.search, "SOCIOFULLNOMBREAPELLIDO.CLICK", row=partner.row,
                                  grid=20, may_be_empty=True)
        added = await self.add_partner(partner)
        added["messages"] = messages(picked) + added["messages"]
        return added

    async def add_partner(self, partner):
        """Report a partner to the booking screen, as closing the search box does. With a
        partner whose ids are known this is the whole step: no Buscar, no search."""
        page = self.booking
        page.values.update(vXSOCIOID=partner.id, vXSOCIODOC=partner.doc,
                           vXSOCIOFULLNOMBREAPELLIDO=partner.name)
        response = await self._fire(page, "GX.EXTENSIONS.WEB.POPUP.ONPOPUPCLOSED")
        players = page.get("vSDTJUGADORES") or []
        if isinstance(players, str):
            players = json.loads(players)
        return {"messages": messages(response),
                "players": [p.get("SocioFullNombreApellido", "") for p in players],
                "raw": response}

    async def confirm(self):
        """Press Confirmar, then "Sí" on the dialog it opens. This is what books.

        Returns {"asked": bool, "sent": bool, "messages": [str], "next": url | None}.
        "asked" is False when the site answered Confirmar with something other than its
        "¿Confirma la reserva?" dialog (then nothing was sent and "messages" says why).
        """
        page = self.booking
        self.confirm_sent = False
        asked = await self._fire(page, "ENTER", may_be_empty=True)
        notes = messages(asked)
        if "DVELOP_CONFIRMPANEL_ENTER" not in json.dumps(asked):
            return {"asked": False, "sent": False, "messages": notes,
                    "next": redirect_of(asked), "raw": asked}
        page.hiddens["DVELOP_CONFIRMPANEL_ENTER_Result"] = "Yes"      # the "Sí" button
        # From here on the booking may exist, whatever happens to this request's answer.
        self.confirm_sent = True
        done = await self._fire(page, "DVELOP_CONFIRMPANEL_ENTER.CLOSE", may_be_empty=True)
        notes += messages(done)
        # The site answers a booking with a message box (wpmensaje) and a move to
        # "Mis reservas" (seen 2026-11-16). Read the box's text to report it.
        box = popup_of(done)
        if box:
            try:
                notes += await self._popup_text(box, referer=page.url)
            except Exception as e:
                notes.append(f"(no pude leer el mensaje del sitio: {type(e).__name__})")
        return {"asked": True, "sent": True, "messages": notes,
                "next": redirect_of(done), "raw": done}

    async def _popup_text(self, url, referer):
        """The texts a message popup shows (its title and message values)."""
        url = urljoin(self.base_url + "/", url + ",gxPopupLevel%3D0%3B")
        started = datetime.now(timezone.utc)
        r = await self.http.get(url, headers={"Referer": referer})
        self._record(r, started)
        r.raise_for_status()
        # The box shows its title and message as read-only text areas (vTITULO, vMENSAJE),
        # e.g. "Reserva realizada" / "17/11/26 - 11:00 hs. Jugadores: …" (2026-11-16).
        texts = []
        for m in re.finditer(r"<textarea\b[^>]*\bname=\"(v\w+)\"[^>]*>(.*?)</textarea>", r.text, re.S | re.I):
            text = " ".join(html_lib.unescape(m.group(2)).split())
            if text:
                texts.append(text)
        return texts


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


async def put_partner(site, partner, known, note):
    """Get `partner` onto the booking: directly when their ids are `known`, else (or if the
    site won't take that) through the search box. Returns (Partner | None, players, how)."""
    if known:
        p = Partner.known(known)
        try:
            added = await site.add_partner(p)
            if on_booking(p, added["players"]):
                note(f"socio agregado sin buscar: {', '.join(added['players'])}")
                return p, added, "atajo"
            note("el atajo no agregó al socio" + "".join(f": {m}" for m in added["messages"][:1])
                 + " — busco")
        except Exception as e:
            note(f"el atajo falló ({type(e).__name__}) — busco")
    found = await site.find_partners(partner.split()[0])
    note("búsqueda: " + (", ".join(p.name for p in found) or "sin resultados"))
    chosen = pick_partner(found, partner)
    if chosen is None:
        return None, None, "búsqueda"
    added = await site.choose_partner(chosen)
    note("jugadores: " + ", ".join(added["players"]))
    return chosen, added, "búsqueda"


async def run_direct_rehearsal(username, password, tz, partner, days_ahead=1, traffic_path=None,
                               transport=None, known_partner=None):
    """The booking screens without a browser, up to the partner being on the booking, timing
    each step. Never sends Confirmar, so nothing is booked.

    Uses the first free slot of the day (like the browser rehearsal). With `traffic_path`,
    everything sent and received is saved there (credentials masked). With `known_partner`
    (ids from an earlier search) the partner is added without the search box. The result's
    "partner_ids" are the partner's ids, to remember for next time.
    """
    lines = []
    learned = None
    site = DeporYA(transport=transport)

    async def timed(label, coro):
        t = time.monotonic()
        result = await coro
        lines.append(f"{label}: {time.monotonic() - t:.2f}s")
        return result

    t0 = time.monotonic()
    try:
        await timed("login", site.login(username, password))
        await timed("Días disponibles", site.open_schedule())
        await timed("scripts de la reserva", site.prefetch("horaactual", "xselsociodatos"))
        day = date.fromordinal(datetime.now(tz).date().toordinal() + days_ahead)
        slots = await timed(f"leer el {day:%d/%m}", site.load_day(day))
        slot = next((s for s in slots if s.free), None)
        if slot is None:
            lines.append(f"❌ El {day:%d/%m} no tiene ningún horario libre para ensayar")
            return {"success": False, "lines": lines, "screenshots": []}
        lines.append(f"ensayo sobre: Cancha {slot.court} {slot.hour}")
        answer = await timed("abrir la reserva", site.open_booking(slot))
        if not answer["next"]:
            lines.append("❌ DeporYA no dejó abrir la reserva: "
                         + (" / ".join(answer["messages"]) or "sin explicación"))
            return {"success": False, "lines": lines, "screenshots": []}
        t = time.monotonic()
        chosen, result, how = await put_partner(site, partner, known_partner, lines.append)
        lines.append(f"socio ({how}): {time.monotonic() - t:.2f}s")
        if chosen is None:
            lines.append(f"❌ No encontré a {partner} entre los resultados")
            return {"success": False, "lines": lines, "screenshots": []}
        for text in result["messages"]:
            lines.append(f"el sitio dice: {text}")
        lines.append(f"total: {time.monotonic() - t0:.2f}s — frené antes de Confirmar")
        ok = on_booking(chosen, result["players"])
        if ok:
            learned = chosen.ids()
            if how == "búsqueda":
                lines.append("(guardé sus datos: el próximo ensayo prueba agregarlo sin buscar)")
        else:
            lines.append("⚠️ El socio no quedó en la lista de jugadores")
        return {"success": ok, "lines": lines, "screenshots": [], "partner_ids": learned}
    except Exception as e:
        lines.append(f"❌ {type(e).__name__}: {str(e).strip().splitlines()[0][:200] if str(e).strip() else ''}")
        return {"success": False, "lines": lines, "screenshots": []}
    finally:
        await site.http.aclose()
        if traffic_path:
            try:
                write_traffic(traffic_path, site.traffic, lines, (password, username))
            except Exception as e:
                lines.append(f"(no pude guardar el tráfico: {e})")


async def run_direct_reservation(username, password, tz, court, hour, partner, days_ahead=1,
                                 open_at=None, traffic_path=None, transport=None,
                                 window=timedelta(seconds=90), known_partner=None):
    """Book `court` at `hour` with `partner`, `days_ahead` days from today, without a browser.

    With `open_at` (08:00 the day before), log in first, then read the day from
    `open_at` on until it opens, one request at a time. Takes the court asked for, or the
    closest-numbered free court on its surface at that hour.

    Returns {"success", "message", "lines", "confirm_sent", "partner_ids"}. confirm_sent=True
    means "Sí" went out: then the booking may exist even if this run could not confirm it,
    so don't try again blindly. With `known_partner` (ids from an earlier search) the
    partner is added without the search box; "partner_ids" are theirs, to remember.
    """
    lines, t0 = [], time.monotonic()
    learned = []
    site = DeporYA(transport=transport)
    confirm_sent = False

    def note(text):
        lines.append(f"[{time.monotonic() - t0:5.1f}s] {text}")

    def done(success, message):
        note(("✅ " if success else "❌ ") + message)
        return {"success": success, "message": message, "lines": lines,
                "confirm_sent": confirm_sent or site.confirm_sent,
                "partner_ids": learned[0] if learned else None}

    try:
        await site.login(username, password)
        note("login")
        await site.open_schedule()
        note("Días disponibles")
        try:
            await site.prefetch("horaactual", "xselsociodatos")
            note("scripts de la reserva cargados")
        except Exception as e:
            note(f"no pude cargar los scripts antes ({type(e).__name__}); se cargan después")
        day = date.fromordinal(datetime.now(tz).date().toordinal() + days_ahead)

        if open_at is not None:
            wait = (open_at - datetime.now(tz)).total_seconds()
            if wait > 0:
                note(f"esperando a las {open_at:%H:%M:%S} ({wait:.0f}s)")
                await asyncio.sleep(wait)
        deadline = datetime.now(tz) + (window if open_at is not None else timedelta(seconds=20))

        tries, slot, why = 0, None, "missing"
        while True:
            tries += 1
            try:
                slots = await site.load_day(day)
            except SiteError as e:
                slots = []
                note(f"lectura {tries}: {e}")
            if slots:
                slot, why = pick_slot(slots, int(court), hour)
                if slot is not None or why in ("taken", "full"):
                    break
            if datetime.now(tz) >= deadline:
                break
            await asyncio.sleep(0.2)
        note(f"día leído ({tries} lectura{'s' if tries > 1 else ''}): "
             + (f"Cancha {slot.court} {slot.hour} libre" if slot else why))
        if slot is None:
            return done(False, {
                "taken": f"Cancha {court} a las {hour} ya está reservada y no hay otra libre en esa superficie",
                "full": f"Cancha {court} a las {hour} no está libre y no hay otra libre en esa superficie",
            }.get(why, f"El {day:%d/%m} no abrió o no figura la Cancha {court} a las {hour}"))

        answer = await site.open_booking(slot)
        note(f"reserva abierta para Cancha {slot.court}" if answer["next"] else "el sitio no abrió la reserva")
        if not answer["next"]:
            return done(False, "DeporYA no dejó reservar: " + (" / ".join(answer["messages"]) or "sin explicación"))

        chosen, players, how = await put_partner(site, partner, known_partner, note)
        if chosen is None:
            return done(False, f"No encontré a {partner} entre los socios")
        if not on_booking(chosen, players["players"]):
            return done(False, f"{chosen.name} no quedó en la reserva"
                        + "".join(f": {m}" for m in players["messages"][:1]))
        learned.append(chosen.ids())

        result = await site.confirm()
        confirm_sent = result["sent"]
        for m in result["messages"]:
            note(f"el sitio dice: {m}")
        if not result["asked"]:
            return done(False, "Confirmar no abrió la confirmación"
                        + "".join(f": {m}" for m in result["messages"][:1]))
        to_list = "misreservas" in (result["next"] or "").lower()
        note("Sí enviado" + (" → el sitio pasó a Mis reservas" if to_list else ""))

        message = f"Cancha {slot.court} ({slot.surface.lower()}) el {day:%d/%m/%Y} a las {slot.hour} con {chosen.name.title()}"
        if why == "substitute":
            message += f" — la Cancha {court} estaba ocupada, reservé la {slot.court}"
        # The proof: the slot now shows as booked.
        try:
            after = next((s for s in await site.load_day(day) if s.row == slot.row), None)
        except Exception as e:
            after = None
            note(f"no pude releer el día: {type(e).__name__}")
        if after is not None:
            note(f"después: Cancha {slot.court} {slot.hour} → {after.status_text}")
        if (after is not None and after.status == BOOKED) or (after is None and to_list):
            return done(True, message)
        return done(False, f"Mandé la confirmación pero el horario figura como "
                    f"«{after.status_text if after else '?'}». Revisá en DeporYA: {message}")
    except Exception as e:
        return done(False, f"{type(e).__name__}: {str(e).strip().splitlines()[0][:200] if str(e).strip() else ''}")
    finally:
        await site.http.aclose()
        if traffic_path:
            try:
                write_traffic(traffic_path, site.traffic, lines, (password, username))
            except Exception as e:
                lines.append(f"(no pude guardar el tráfico: {e})")
