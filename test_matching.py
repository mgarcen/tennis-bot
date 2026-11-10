"""Tests for the grid-row matching in reserve.py.

Court numbers are unique across the club, so a booking is identified by number alone
and the row matcher has to be exact: matching "5" loosely against a row's text picks up
the 5 in "15:00" or in "Cancha 15" and books the wrong court. These cases pin that down.

Run with: python3 test_matching.py
"""
import sys
import types

# reserve.py imports playwright at module level; stub it so these pure-function tests
# run without the browser dependency installed (in the Docker image it's already there).
try:
    import playwright.async_api  # noqa: F401
except ImportError:
    sa = types.ModuleType("playwright.async_api")
    sa.async_playwright = lambda: None
    sa.TimeoutError = type("TimeoutError", (Exception,), {})
    sys.modules["playwright"] = types.ModuleType("playwright")
    sys.modules["playwright.async_api"] = sa

from reserve import parse_hour, row_courts, row_hours  # noqa: E402

CASES = []


def case(desc, got, want):
    CASES.append((desc, got, want))


case("'19:00' parses", parse_hour("19:00"), (19, 0))
case("'9:00' normalizes to 09:00", parse_hour("9:00"), (9, 0))
case("a bare court number is not an hour", parse_hour("5"), None)
case("junk is not an hour", parse_hour("abc"), None)

# A 15:00 slot on court 3 must never satisfy a request for court 5.
ROW_15H_COURT3 = "15:00\tCancha 3\tLADRILLO\tReservar"
case("court 5 not matched by the 5 in '15:00'", 5 in row_courts(ROW_15H_COURT3), False)
case("court 3 matched", 3 in row_courts(ROW_15H_COURT3), True)
case("hour 15:00 matched", (15, 0) in row_hours(ROW_15H_COURT3), True)
case("hour 05:00 not matched", (5, 0) in row_hours(ROW_15H_COURT3), False)

# Nor may Cancha 15 satisfy a request for court 5.
ROW_COURT15 = "19:00\tCancha 15\tCEMENTO\tReservar"
case("court 5 not matched by Cancha 15", 5 in row_courts(ROW_COURT15), False)
case("court 15 matched", 15 in row_courts(ROW_COURT15), True)

ROW_OK = "19:00\tCancha 5\tLADRILLO\tReservar"
case("happy path: court 5", 5 in row_courts(ROW_OK), True)
case("happy path: hour 19:00", (19, 0) in row_hours(ROW_OK), True)

for variant in [
    "19:00 CANCHA 5 LADRILLO",
    "19:00  cancha 05  cemento",
    "19:00 Cancha N 5 Ladrillo",
    "19:00 Cancha N° 5 Ladrillo",
    "19:00 - 20:00 | Cancha 5 | LADRILLO | Reservar",
]:
    case(f"court 5 found in {variant!r}", 5 in row_courts(variant), True)

# Some grids render the court as a bare column with no "Cancha" label.
ROW_BARE = "19:00\t5\tLADRILLO\tReservar"
case("bare court column found", 5 in row_courts(ROW_BARE), True)
case("hour digits never read as a court", 19 in row_courts(ROW_BARE), False)

ROW_RANGE = "19:00 - 20:00\tCancha 5\tLADRILLO"
case("range start matched", (19, 0) in row_hours(ROW_RANGE), True)
case("range end present", (20, 0) in row_hours(ROW_RANGE), True)




# ── Surface sweep ─────────────────────────────────────────────────────────────
# find_court_across_surfaces() drives the real page, so it gets a fake one: a dict of
# surface label → the grid rows that surface shows on the target date.

import asyncio  # noqa: E402

import reserve  # noqa: E402
from reserve import find_court_across_surfaces  # noqa: E402


class FakeButton:
    def __init__(self, visible):
        self._visible = visible

    async def is_visible(self):
        return self._visible


class FakeRow:
    def __init__(self, text, visible=True):
        self.text = text
        self._visible = visible

    async def inner_text(self):
        return self.text

    def locator(self, _selector):
        return _First(FakeButton(self._visible))


class _First:
    def __init__(self, value):
        self.first = value


class FakeRowSet:
    def __init__(self, rows):
        self.rows = rows
        self.first = self

    async def wait_for(self, timeout=None):
        if not self.rows:
            raise reserve.PlaywrightTimeout("no rows")

    async def count(self):
        return len(self.rows)

    def nth(self, i):
        return self.rows[i]


class FakeSelect:
    def __init__(self, page):
        self.page = page

    async def select_option(self, value=None):
        self.page.current = self.page.by_value[value]

    async def input_value(self):
        return next((v for v, l in self.page.by_value.items() if l == self.page.current), "")

    async def click(self):
        self.page.clicks += 1


class FakePage:
    """A grid showing `surfaces[label]` rows, reachable at `target_short`."""

    def __init__(self, surfaces, by_value, date_text="04/11/26", reachable=True):
        self.surfaces = surfaces
        self.by_value = by_value
        self.current = None
        self.date_text = date_text
        self.reachable = reachable
        self.clicks = 0

    def locator(self, selector):
        if "vTIPOCANCHAID" in selector:
            return FakeSelect(self)
        if "Grid" in selector:
            return FakeRowSet(self.surfaces.get(self.current, []))
        return FakeSelect(self)

    async def inner_text(self, _selector):
        return self.date_text if self.reachable else "01/01/00"

    async def wait_for_load_state(self, _state=None):
        return None

    async def wait_for_function(self, _script, arg=None, timeout=None, polling=None):
        return None  # the fake grid updates instantly

    async def evaluate(self, _script):
        # Stands in for the grid-id diagnostic dump when no rows render.
        return []


def sweep(surfaces, court=5, hour=(19, 0), reachable=True):
    by_value = {f"v{i}": label for i, label in enumerate(surfaces)}
    page = FakePage(surfaces, by_value, reachable=reachable)
    options = [(v, l) for v, l in by_value.items()]
    row, label, states, booked = asyncio.run(
        find_court_across_surfaces(page, hour, court, options, "04/11/26", 1)
    )
    return (row is not None), label, states, booked


# Court 5 lives on ladrillo; the sweep must skip cemento and find it there.
found, label, states, booked = sweep({
    "CEMENTO": [FakeRow("19:00\tCancha 1\tCEMENTO\tReservar"),
                FakeRow("19:00\tCancha 2\tCEMENTO\tReservar")],
    "LADRILLO": [FakeRow("19:00\tCancha 5\tLADRILLO\tReservar")],
})
case("sweep finds court 5 on the second surface", (found, label), (True, "LADRILLO"))
case("sweep reports it missing on the first", states[0], "missing")

# Court 2 is on cemento — the first surface tried — so ladrillo is never visited.
found, label, states, booked = sweep({
    "CEMENTO": [FakeRow("19:00\tCancha 2\tCEMENTO\tReservar")],
    "LADRILLO": [FakeRow("19:00\tCancha 5\tLADRILLO\tReservar")],
}, court=2)
case("sweep finds a cemento court", (found, label), (True, "CEMENTO"))
case("sweep stops at the first hit", len(states), 1)

# The court exists but its Reserva link is hidden: taken, not missing.
found, label, states, booked = sweep({
    "CEMENTO": [FakeRow("19:00\tCancha 1\tCEMENTO")],
    "LADRILLO": [FakeRow("19:00\tCancha 5\tLADRILLO", visible=False)],
})
case("taken court with nothing else free on its surface reports full",
     (found, states[-1]), (False, "full"))

# Nobody has that court at that hour.
found, label, states, booked = sweep({
    "CEMENTO": [FakeRow("19:00\tCancha 1\tCEMENTO")],
    "LADRILLO": [FakeRow("19:00\tCancha 6\tLADRILLO")],
})
case("court on no surface is missing everywhere", (found, set(states)), (False, {"missing"}))

# An empty grid is a markup problem, reported separately from a busy court.
found, label, states, booked = sweep({"CEMENTO": [], "LADRILLO": []})
case("empty grid reports no-grid", set(states), {"no-grid"})

# The target date never shows up.
found, label, states, booked = sweep({"CEMENTO": [FakeRow("19:00\tCancha 5")]}, reachable=False)
case("unreachable date reports no-date", set(states), {"no-date"})

# A 15:00 row must not satisfy a request for court 5 at 19:00 end to end.
found, label, states, booked = sweep({
    "CEMENTO": [FakeRow("15:00\tCancha 3\tCEMENTO\tReservar")],
    "LADRILLO": [FakeRow("15:00\tCancha 3\tLADRILLO\tReservar")],
})
case("no false hit from the 5 in 15:00", found, False)


# ── Fallback to another court on the same surface ───────────────────────────────

# Court 5 is taken; 4 and 7 are free on ladrillo, cemento's court 2 must not be used.
found, label, states, booked = sweep({
    "CEMENTO": [FakeRow("19:00\tCancha 2\tCEMENTO\tReservar")],
    "LADRILLO": [FakeRow("19:00\tCancha 5\tLADRILLO", visible=False),
                 FakeRow("19:00\tCancha 7\tLADRILLO\tReservar"),
                 FakeRow("19:00\tCancha 4\tLADRILLO\tReservar")],
})
case("taken court falls back on its own surface", (found, label, booked), (True, "LADRILLO", 4))
case("fallback is reported as a substitute", states[-1], "substitute")

# The site hides the booked 19:00 slot entirely; the 20:00 row shows court 5 is ladrillo.
found, label, states, booked = sweep({
    "CEMENTO": [FakeRow("19:00\tCancha 2\tCEMENTO\tReservar")],
    "LADRILLO": [FakeRow("20:00\tCancha 5\tLADRILLO\tReservar"),
                 FakeRow("19:00\tCancha 6\tLADRILLO\tReservar")],
})
case("hidden slot still resolves the court's surface", (found, label, booked), (True, "LADRILLO", 6))

# Other courts are free only at other hours, or taken: nothing to fall back on.
found, label, states, booked = sweep({
    "LADRILLO": [FakeRow("19:00\tCancha 5\tLADRILLO", visible=False),
                 FakeRow("19:00\tCancha 6\tLADRILLO", visible=False),
                 FakeRow("20:00\tCancha 7\tLADRILLO\tReservar")],
})
case("no fallback at a different hour", (found, booked, states[-1]), (False, None, "full"))

# The requested court is free: never substitute.
found, label, states, booked = sweep({
    "LADRILLO": [FakeRow("19:00\tCancha 4\tLADRILLO\tReservar"),
                 FakeRow("19:00\tCancha 5\tLADRILLO\tReservar")],
})
case("free requested court is booked as asked", booked, 5)

case("closest number wins, lower on a tie",
     reserve.pick_substitute(5, [(7, "r7"), (3, "r3"), (6, "r6"), (4, "r4")]), (4, "r4"))


if __name__ == "__main__":
    failed = 0
    for desc, got, want in CASES:
        if got == want:
            print(f"[PASS] {desc}")
        else:
            failed += 1
            print(f"[FAIL] {desc}\n       got={got!r} want={want!r}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    raise SystemExit(1 if failed else 0)
