"""Tests for deporya.py, the browser-free client, against a small fake of the site.

The fake speaks the same protocol as DeporYA (GeneXus): pages carry their state in a
GXState field, <page>.js lists what each event sends, and events are JSON POSTs. It
checks what the client sends the way the real server would (signed values returned
untouched, parms in the script's order) — so these pin the request format that was
verified byte-for-byte against real recorded traffic on 2026-11-12.

Run with: python3 test_deporya.py   (needs httpx, which python-telegram-bot installs)
"""
import asyncio
import html
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

import deporya as d

TZ = ZoneInfo("America/Montevideo")
KEY, IV = "86D9C7C970B255391CC373EAB76F98E7", "C01D04B1610243D2A2AF23E7952E8B18"
SEC = {"GX_AJAX_KEY": KEY, "GX_AJAX_IV": IV, "AJAX_SECURITY_TOKEN": "TOKEN"}

LOGIN_JS = ('this.EvtParms.ENTER=[[{av:"AV1",fld:"vEMPRESANOMBRE",pic:""},{ctrl:"vLOGONTO"},'
            '{av:"AV2",fld:"vLOGONTO",pic:""},{av:"AV3",fld:"vUSERNAME",pic:""},'
            '{av:"AV4",fld:"vUSERPASSWORD",pic:""},{av:"AV5",fld:"vEMPRESAID",pic:"ZZZZZZZZZ9"},'
            '{av:"AV6",fld:"vPGMNAME",pic:"",hsh:!0},{av:"Gx_date",fld:"vTODAY",pic:""},'
            '{av:"AV7",fld:"vKEEPMELOGGEDIN",pic:""}],[{av:"AV5",fld:"vEMPRESAID",pic:"ZZZZZZZZZ9"}]];')
INICIO_JS = ("this.EvtParms[\"'DOBTNRESERVAR'\"]=[[{av:\"AV11\",fld:\"vEMPRESAID\",pic:\"ZZZZZZZZZ9\"},"
             "{av:\"Gx_date\",fld:\"vTODAY\",pic:\"\"}],[]];")
HORAS_JS = ('this.EvtParms.REFRESH=[[{av:"GRIDS_nFirstRecordOnPage"},{av:"GRIDS_nEOF"},'
            '{ctrl:"GRIDS",prop:"Rows"},{av:"AV10SDT",fld:"vSDTHORASDELDIA",pic:""},'
            '{av:"nRC_GXsfl_129",ctrl:"GRIDS",prop:"GridRC",grid:129},{av:"AV9Fecha",fld:"vFECHA",pic:""},'
            '{av:"AV21",fld:"vSOCIOID",pic:"ZZZZZZZZZ9",hsh:!0},{ctrl:"vTIPOCANCHAID"},'
            '{av:"AV25",fld:"vTIPOCANCHAID",pic:"ZZZZZZZZZ9"},{av:"AV32",fld:"vBLOQUEAR",pic:""}],'
            '[{av:"AV10SDT",fld:"vSDTHORASDELDIA",pic:""}]];'
            'this.EvtParms["VRESERVARHORA.CLICK"]=[[{av:"AV21",fld:"vSOCIOID",pic:"ZZZZZZZZZ9",hsh:!0},'
            '{av:"AV10SDT",fld:"vSDTHORASDELDIA",pic:""},{av:"GRIDS_nFirstRecordOnPage"},'
            '{av:"nRC_GXsfl_129",ctrl:"GRIDS",prop:"GridRC",grid:129},{av:"AV9Fecha",fld:"vFECHA",pic:""}],[]];')


def page(state, body=""):
    state = dict(SEC, **state)
    return (f"<html><form>{body}<input type=\"hidden\" name=\"GXState\" "
            f"value='{html.escape(json.dumps(state), quote=True)}'/></form></html>")


def day_data(day):
    def slot(court, surface, hour, status, text):
        return {"CanchaId": court, "TipoCanchaDesc": surface, "CanchaNombre": f"CANCHA{court}",
                "StrHora": hour, "EventId": 1000 + court, "EstadoHoraId": status, "EstadoHoraDesc": text}
    return {"EmpresaId": 1, "Fecha": day.isoformat(), "Horas": [
        slot(1, "CEMENTO", "19:00", 1, "LIBRE"),
        slot(5, "LADRILLO", "18:00", 1, "LIBRE"),
        slot(5, "LADRILLO", "19:00", 2, "RESERVADO"),
        slot(6, "LADRILLO", "19:00", 5, "Clases"),
        slot(8, "LADRILLO", "19:00", 1, "LIBRE"),
    ]}


class FakeSite:
    """Just enough of DeporYA to log in, open the schedule, read a day and click a slot."""

    def __init__(self, booked_already=False):
        self.today = datetime.now(TZ).date()
        self.booked_already = booked_already
        self.problems = []
        self.events = []

    def check(self, ok, what):
        if not ok:
            self.problems.append(what)

    def __call__(self, request):
        path = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET":
            return httpx.Response(200, text=self.get(path))
        body = json.loads(request.content)
        query = request.url.query.decode()
        self.check(query.startswith(d.ajax_token(KEY, IV) + ","), f"{path}: wrong event address")
        self.check("gx-no-cache=" in query, f"{path}: no gx-no-cache")
        self.check(request.headers.get("GxAjaxRequest") == "1", f"{path}: GxAjaxRequest header")
        self.check(request.headers.get("AJAX_SECURITY_TOKEN") == "TOKEN", f"{path}: security token")
        self.check(request.headers.get("X-GXAUTH-TOKEN") == "AUTH-" + path.split(".")[0].upper(),
                   f"{path}: auth token")
        self.check(body["objClass"] == path.split(".")[0], f"{path}: objClass")
        self.events.append(body["events"][0])
        return httpx.Response(200, json=self.post(path, body))

    def get(self, path):
        if path == "login.js":
            return LOGIN_JS
        if path == "inicio.js":
            return INICIO_JS
        if path == "horasdeundia.js":
            return HORAS_JS
        today = self.today.strftime("%Y/%m/%d")
        if path == "login.aspx":
            return page(
                {"GX_AUTH_LOGIN": "AUTH-LOGIN", "vPGMNAME": "Login", "gxhash_vPGMNAME": "sig-pgm",
                 "vTODAY": today, "nRC_GXsfl_70": "0", "GridauthtypesContainerData": '{"GridName":"Gridauthtypes"}'},
                '<select name="vLOGONTO"><option value="local" selected>local</option></select>'
                '<input type="text" name="vEMPRESANOMBRE" value="club"/>'
                '<input type="text" name="vUSERNAME" value=""/><input type="password" name="vUSERPASSWORD" value=""/>'
                '<input type="checkbox" name="vKEEPMELOGGEDIN" value=""/>'
                '<input type="number" name="vEMPRESAID" value="0"/>')
        if path == "inicio.aspx":
            return page({"GX_AUTH_INICIO": "AUTH-INICIO", "vEMPRESAID": "1", "vTODAY": today,
                         "nRC_GXsfl_49": "6", "GridContainerDataV": "[]"})
        if path == "horasdeundia.aspx":
            return page(
                {"GX_AUTH_HORASDEUNDIA": "AUTH-HORASDEUNDIA", "vSOCIOID": "4283", "gxhash_vSOCIOID": "sig-socio",
                 "GRIDS_nFirstRecordOnPage": "0", "GRIDS_nEOF": "1", "GRIDS_Rows": "0", "nRC_GXsfl_129": "3",
                 "vSDTHORASDELDIA": day_data(self.today), "GridsContainerDataV": "[]"},
                f'<input type="text" name="vFECHA" value="{self.today:%d/%m/%y}"/>'
                '<select name="vTIPOCANCHAID"><option value="0" selected>- TODAS -</option>'
                '<option value="2">LADRILLO</option></select><input type="checkbox" name="vBLOQUEAR" value=""/>')
        return "not found"

    def post(self, path, body):
        parms, event = body["parms"], body["events"][0]
        if path == "login.aspx":
            self.check(parms == ["club", {"s": "local", "v": [["local", "local"]]}, "local", "me", "secret",
                                 0, "Login", self.today.strftime("%Y/%m/%d") + " 00:00:00", False],
                       f"login parms: {parms}")
            self.check(body["hsh"] == [{"hsh": "sig-pgm", "row": ""}], "login signature")
            self.check(body["grids"] == {"Gridauthtypes": {"id": 70, "lastRow": 0, "pRow": ""}}, "login grids")
            return {"gxCommands": [{"redirect": {"url": "inicio.aspx?abc", "forceDisableFrm": 1}}]}
        if path == "inicio.aspx":
            self.check(parms == [1, self.today.strftime("%Y/%m/%d") + " 00:00:00"], f"inicio parms: {parms}")
            return {"gxCommands": [{"redirect": {"url": "horasdeundia.aspx?xyz"}}]}
        self.check(body["hsh"] == [{"hsh": "sig-socio", "row": ""}], f"{event}: signature")
        if event == "REFRESH":
            self.check(parms[4] == {"gridRC": "3", "rowSuffix": "", "grid": 129}, f"REFRESH GridRC: {parms[4]}")
            self.check(parms[6] == 4283 and parms[8] == 0 and parms[9] is False, f"REFRESH types: {parms[6:]}")
            day = datetime.strptime(parms[5], "%Y/%m/%d %H:%M:%S").date()
            self.shown = day
            return {"gxHiddens": {"GRIDS_nEOF": "1"},
                    "gxValues": [{"CmpContext": "", "IsMasterPage": "False", "AV10SDT": day_data(day)}],
                    "gxGrids": [{"GridName": "Grids", "GRIDS_nEOF": 1, "GRIDS_nFirstRecordOnPage": 0, "Count": 5}],
                    "gxMessages": {"MAIN": []}}
        if event == "VRESERVARHORA.CLICK":
            self.check(parms[1]["Fecha"] == self.shown.isoformat(), "click: day data not the day shown")
            self.check(body["grid"] == 129 and body["pRow"] == "", "click: grid fields")
            self.clicked = body["row"]
            if self.booked_already:
                return {"gxMessages": {"MAIN": [{"text": "Ya tienes el máximo de reservas", "type": 0}]}}
            return {"gxCommands": [{"redirect": {"url": "horaactual.aspx?next"}}], "gxMessages": {"MAIN": []}}
        self.check(False, f"unexpected event {event}")
        return {}


CASES = []


def case(desc, got, want):
    CASES.append((desc, got, want))


async def main():
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)

    fake = FakeSite()
    async with d.DeporYA(transport=httpx.MockTransport(fake)) as site:
        await site.login("me", "secret")
        await site.open_schedule()
        slots = await site.load_day(tomorrow)
        case("reads the asked day's slots", [(s.court, s.hour, s.free) for s in slots],
             [(1, "19:00", True), (5, "18:00", True), (5, "19:00", False), (6, "19:00", False), (8, "19:00", True)])
        slot, why = d.pick_slot(slots, 5, "18:00")
        case("free court is picked as is", (slot.court, why), (5, "free"))
        slot, why = d.pick_slot(slots, 5, "19:00")
        case("taken court → closest free on the same surface, not the cement one", (slot.court, why), (8, "substitute"))
        case("court that doesn't exist", d.pick_slot(slots, 3, "19:00"), (None, "missing"))
        case("surface full at that hour", d.pick_slot([s for s in slots if s.court != 8], 5, "19:00"), (None, "full"))
        answer = await site.click_slot(slot)
        case("click sends the slot's grid row", fake.clicked, "0005")
        case("accepted click gives the next screen", answer["next"], "horaactual.aspx?next")
    case("events sent, in order", fake.events, ["ENTER", "'DOBTNRESERVAR'", "REFRESH", "VRESERVARHORA.CLICK"])
    case("fake server found nothing wrong with the requests", fake.problems, [])

    fake = FakeSite(booked_already=True)
    async with d.DeporYA(transport=httpx.MockTransport(fake)) as site:
        await site.login("me", "secret")
        await site.open_schedule()
        slots = await site.load_day(tomorrow)
        answer = await site.click_slot(slots[0])
        case("refused click reports the site's notice", (answer["next"], answer["messages"]),
             (None, ["Ya tienes el máximo de reservas"]))

    fake = FakeSite()
    report = await d.run_direct_probe("me", "secret", TZ, court="5", hour="19:00",
                                      transport=httpx.MockTransport(fake))
    case("direct probe succeeds and never clicks",
         (report["success"], "VRESERVARHORA.CLICK" in fake.events, fake.problems), (True, False, []))
    case("direct probe reports the court asked about", report["lines"][-1],
         "Cancha 5 19:00: RESERVADO → reservaría la 8")

    def bad_login(request):
        if request.method == "GET":
            return httpx.Response(200, text=fake.get(request.url.path.rsplit("/", 1)[-1]))
        return httpx.Response(200, json={"gxMessages": {"MAIN": [{"text": "Usuario o contraseña incorrectos"}]}})
    report = await d.run_direct_probe("me", "wrong", TZ, transport=httpx.MockTransport(bad_login))
    case("wrong password is reported, not crashed on", (report["success"], report["lines"][-1]),
         (False, "❌ SiteError: ❌ Login failed: Usuario o contraseña incorrectos"))


# Fixed checks that need no site.
case("AES-128 (FIPS-197 example)",
     d.aes128_encrypt_block(bytes.fromhex("000102030405060708090a0b0c0d0e0f"),
                            bytes.fromhex("00112233445566778899aabbccddeeff")).hex(),
     "69c4e0d86a7b0430d8cdb78070b4c55a")
case("event address token, as recorded from the real site", d.ajax_token(KEY, IV),
     "c01d04b1610243d2a2af23e7952e8b18ba3efe338a9d51348efc3957c07afef3")
case("event names with and without quotes are both read",
     sorted(d.parse_evt_parms(HORAS_JS + INICIO_JS)), ["'DOBTNRESERVAR'", "REFRESH", "VRESERVARHORA.CLICK"])


if __name__ == "__main__":
    asyncio.run(main())
    failed = 0
    for desc, got, want in CASES:
        if got == want:
            print(f"[PASS] {desc}")
        else:
            failed += 1
            print(f"[FAIL] {desc}\n       got={got!r} want={want!r}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    raise SystemExit(1 if failed else 0)
