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


def _search_cases():
    """The partner search box and the booking screen, as recorded on 2026-11-15."""
    two_blocks = "gxajaxGridRefresh_Grid1,Kevin,1,S"            # 33 characters → 3 blocks
    token = bytes.fromhex(d.gx_encrypt(KEY, IV, two_blocks))
    plain = two_blocks.encode().ljust(48, b"\0")
    chained = d.aes128_encrypt_block(bytes.fromhex(KEY), bytes(a ^ b for a, b in zip(plain[16:32], token[16:32])))
    case("longer texts are encrypted block after block (CBC)", (len(token), token[32:48]), (64, chained))

    js = ('this.EvtParms["SOCIOFULLNOMBREAPELLIDO.CLICK"]=[[{av:"A39",fld:"SOCIOID",pic:"ZZZZZZZZZ9",hsh:!0},'
          '{av:"AV8",fld:"vEMPRESAID",pic:"ZZZZZZZZZ9",hsh:!0},{av:"A43",fld:"SOCIOFULLNOMBREAPELLIDO",pic:"",hsh:!0},'
          '{av:"AV6",fld:"vXSOCIODOC",pic:""},{av:"AV10",fld:"vHORA",pic:"99:99",hsh:!0},'
          '{av:\'gx.fn.getCtrlProperty("vBTN","Visible")\',ctrl:"vBTN",prop:"Visible"}],[]];')
    popup = d.GxPage("https://x/deporyatenis/xselsociodatos.aspx?abc",
                     page({"vEMPRESAID": "1", "gxhash_vEMPRESAID": "h-emp", "nRC_GXsfl_20": "0",
                           "Grid1ContainerDataV": "[]", "vHORA": "0001/01/01 12:00:00",
                           "gxhash_vHORA": "h-hora", "vBTN_Visible": "-1"}), js)
    popup.apply({"gxHiddens": {"gxhash_SOCIOID_0002": "h-id2", "gxhash_SOCIOFULLNOMBREAPELLIDO_0002": "h-name2"},
                 "gxGrids": [{"GridName": "Grid1",
                              "0": {"Props": [["SOCIOID_0001", "  746", "746"], ["SOCIOFULLNOMBREAPELLIDO_0001", "", "ANA PAZ"]]},
                              "1": {"Props": [["SOCIOID_0002", " 4281", "4281"], ["SOCIOFULLNOMBREAPELLIDO_0002", "", "KEVIN MONZON "]]}}]})
    found = d.partners_of(popup)
    case("search results are read from the answer", [(p.row, p.id, p.name) for p in found],
         [(1, 746, "ANA PAZ"), (2, 4281, "KEVIN MONZON")])
    case("a partner is matched whatever the word order or accents; part of a name only if one fits",
         (d.pick_partner(found, "monzón kevin").id, d.pick_partner(found, "Paz").id,
          d.pick_partner(found + [d.Partner(1, popup.rows)], "Paz"), d.pick_partner(found, "Lopez")),
         (4281, 746, None, None))
    body = popup.body("SOCIOFULLNOMBREAPELLIDO.CLICK", row=2, grid=20)
    case("picking a result sends that row's values, a never-filled box as empty text, "
         "a time of day dated 2001 and a shown control as true",
         body["parms"], [4281, 1, "KEVIN MONZON", "", "2001/01/01 12:00:00", True])
    case("…with that row's signatures, marked with the row",
         body["hsh"], [{"hsh": "h-id2", "row": "0002"}, {"hsh": "h-emp", "row": ""},
                       {"hsh": "h-name2", "row": "0002"}, {"hsh": "h-hora", "row": ""}])
    case("…and the rows before it counted", (body["grids"], body["row"]),
         ({"Grid1": {"id": 20, "lastRow": 1, "pRow": ""}}, "0002"))
    url = popup.refresh_url("Grid1", ["Kevin", "1", "S", "20261115"], now_ms=7)
    case("the search address is the page's own, without its query",
         (url.split("?")[0], url.rsplit(",", 1)[1], len(url.split("?")[1].split(",")[0])),
         ("https://x/deporyatenis/xselsociodatos.aspx", "gx-no-cache=7", 128))

    confirm_js = ('this.EvtParms["DVELOP_CONFIRMPANEL_ENTER.CLOSE"]=[[{av:"this.DVELOP_CONFIRMPANEL_ENTERContainer.Result",'
                  'ctrl:"DVELOP_CONFIRMPANEL_ENTER",prop:"Result"},{av:"AV62",fld:"vPRECIOMONTOPARCIAL",pic:"ZZZZZZ9.99"},'
                  '{av:"AV59",fld:"vPRECIOMONTO",pic:"ZZZZZZ9.99"}],[]];')
    booking = d.GxPage("https://x/deporyatenis/horaactual.aspx?abc",
                       page({"DVELOP_CONFIRMPANEL_ENTER_Result": "", "vPRECIOMONTOPARCIAL": "0,00",
                             "vPRECIOMONTO": "1.250,50"}), confirm_js)
    booking.hiddens["DVELOP_CONFIRMPANEL_ENTER_Result"] = "Yes"
    case("the final confirmation says Yes, and prices go as the page's script reads them",
         booking.body("DVELOP_CONFIRMPANEL_ENTER.CLOSE")["parms"], ["Yes", 0, 1250.5])

    booked = {"gxCommands": [{"popup": ["wpmensaje.aspx?abc", 1, 0, 0, 0, 0, 0, [], ["AV22Titulo"], ""]},
                             {"redirect": {"url": "misreservas.aspx?xyz", "forceDisableFrm": 1}}]}
    case("the answer to Sí (as recorded): a message popup, then Mis reservas",
         (d.popup_of(booked), d.redirect_of(booked)), ("wpmensaje.aspx?abc", "misreservas.aspx?xyz"))

    kevin = d.Partner.known({"id": "4281", "doc": 19890, "name": "KEVIN MONZON"})
    case("a remembered partner keeps the ids a search gave, and is found on the booking",
         (kevin.ids(), d.on_booking(kevin, ["MICHEL GARCEN", "KEVIN MONZON"]), d.on_booking(kevin, ["MICHEL GARCEN"])),
         ({"id": 4281, "doc": "19890", "name": "KEVIN MONZON"}, True, False))

    def row(n, court, hour, status, surface):
        return d.Slot(n, {"CanchaId": court, "TipoCanchaDesc": surface, "StrHora": hour,
                          "EstadoHoraId": status, "EstadoHoraDesc": ""})
    clay = [5, 6, 7, 8]
    half = [row(1, 5, "14:00", 1, "LADRILLO")]                      # as at 08:00 on 2026-11-18
    full = [row(1, 5, "19:00", 2, "LADRILLO"), row(2, 6, "19:00", 1, "LADRILLO"),
            row(3, 7, "19:00", 2, "LADRILLO"), row(4, 8, "19:00", 5, "LADRILLO")]
    taken_partly = full[:1] + full[2:3]                             # 5 booked, 6 and 8 not there yet
    case("a half-built day: the court's missing row means read again, not taken",
         d.decide(half, 5, "19:00", clay)[1:], ("not-yet", False))
    case("booked, others at that hour still coming: read again",
         d.decide(taken_partly, 5, "19:00", clay)[1:], ("not-yet", False))
    case("booked, a free one on its surface: take it",
         (d.decide(full, 5, "19:00", clay)[0].court, d.decide(full, 5, "19:00", clay)[1:]), (6, ("substitute", True)))
    case("booked and every court there and none free: give up",
         d.decide([row(2, 6, "19:00", 2, "LADRILLO") if r.court == 6 else r for r in full], 5, "19:00", clay)[1:],
         ("full", True))
    case("time's up on a half-built day: stop, and say it never appeared",
         d.decide(half, 5, "19:00", clay, last_chance=True)[1:], ("not-yet", True))


_search_cases()
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
