"""Guest checkout in the legacy shop. Renfe owns the forms, seats and payment-provider return."""

import re
from collections.abc import Callable
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

from . import legacy, shop
from .errors import ApiShapeError, UpstreamError, UsageError
from .stations import Station

if TYPE_CHECKING:
    from playwright.sync_api import Page, Route

PAYMENT = f"{legacy.BASE}/formasDePagoEnlaces.do"
RECEIPT = f"{legacy.BASE}/finalCompraEnlaceVXY.do"


class Method(StrEnum):
    apple_pay = "apple-pay"
    paypal = "paypal"
    card = "card"
    bizum = "bizum"
    google_pay = "google-pay"
    renfe_points = "renfe-points"
    aplazame = "aplazame"


# Renfe hides Apple Pay in Chrome. Only select a method actually offered on this trip.
_RADIOS = {
    Method.paypal: "paypal", Method.card: "tarjetaRedSys", Method.bizum: "bizum",
    Method.google_pay: "googlePay", Method.renfe_points: "tarjetaRenfe",
}


def _page_url(url: str) -> str:
    return url.partition("#")[0].partition("?")[0]


def _review_guard() -> Callable[["Route"], None]:
    """Once the payment page arrives, allow its static assets and read-only setup/price calls only."""
    at_payment = False
    reads = {
        "__System.generateId", "sesionManager.checkSession", "loginManager.obtenerUsuSesion",
        "buyEnlacesManager.actualizaObjetosSesion", "payEnlacesManager.recalcularPrecios",
    }
    price_urls = {f"{legacy.BASE}/dwr/call/plaincall/{name}.dwr" for name in reads}

    def guard(route: "Route") -> None:
        nonlocal at_payment
        req = route.request
        url = _page_url(req.url)
        if not at_payment and url == PAYMENT and req.is_navigation_request() and req.method == "GET":
            at_payment = True
            route.fallback()
        elif at_payment:
            asset = (req.method == "GET" and not req.is_navigation_request()
                     and req.resource_type in {"script", "stylesheet", "image", "font"})
            if (asset and url.startswith(legacy.BASE + "/")) or (req.method == "POST" and url in price_urls):
                route.fallback()
            else:
                route.abort()
        elif url == PAYMENT or url in {RECEIPT, f"{legacy.BASE}/conectarPayPal.do",
                                       f"{legacy.BASE}/respuestaPayPal.do"}:
            route.abort()
        else:
            route.fallback()

    return guard


def _expand_train(page: "Page", train: list[str]) -> None:
    """Match all legs in the results page's own list, then expand that row to show its fares."""
    from playwright.sync_api import TimeoutError as BrowserTimeout

    try:
        page.wait_for_function("() => Array.isArray(window.trenesIda?.listviajeViewEnlaceBean)", timeout=15000)
    except BrowserTimeout:
        raise UpstreamError("Renfe didn't display its trains. Check the search in `renfe open`.") from None
    ids = page.evaluate("""parts => window.trenesIda.listviajeViewEnlaceBean
        .filter(row => row.trayectos.map(leg => String(Number(leg.cdgoTren))).join('+') === parts.join('+'))
        .map(row => String(row.id))""", train)
    if len(ids) != 1 or not re.fullmatch(r"[0-9]+", ids[0]):
        raise UsageError("The selected train has no unique match on Renfe's results page.")
    page.locator(f"#tren_i_{ids[0]} .trenes").click()


def _select_method(page: "Page", method: Method) -> str:
    # Renfe binds its radio handlers in jQuery's DOM-ready callback.
    page.evaluate("() => window.jQuery && new Promise(done => window.jQuery(() => done(true)))")
    radio = page.locator(f"input[type=radio][name=radiobutton]#datosPago_cdgoFormaPago_{_RADIOS[method]}")
    if radio.count() != 1 or not radio.is_visible() or not radio.is_enabled():
        raise UsageError(f"Renfe doesn't offer {method.value} for this checkout.")
    radio.click()
    # The click handler updates these fields and synchronously recalculates the total.
    applied = page.locator("#datosPago_cdgoFormaPago")
    subcode = page.locator("#datosPago_subCdgoFormaPago")
    if (not radio.is_checked() or applied.input_value() != radio.input_value()
            or subcode.input_value() != (radio.get_attribute("subCdgoFormaPago") or "")
            or page.evaluate("window.errorPrecios") is not False):
        raise UpstreamError("Renfe did not confirm the payment method and total.")
    total = " ".join(page.locator("#totalTrayecto").inner_text().split())
    unit = "puntos" if method == Method.renfe_points else "(?:€|EUR)"
    if not re.fullmatch(rf"[0-9]+(?:[.,][0-9]+)* {unit}", total, re.IGNORECASE):
        raise ApiShapeError("Renfe's checkout returned an unrecognized total.")
    return total


def _whole_euros(total: str) -> int:
    """Renfe's threshold is in whole euros. Accept decimal commas or points and grouped thousands."""
    amount, unit = total.split()
    if unit in {"€", "EUR"}:
        for thousands, decimal in ((r"\.", ","), (",", r"\.")):
            match = re.fullmatch(rf"(?P<whole>[0-9]+|[1-9][0-9]{{0,2}}(?:{thousands}[0-9]{{3}})+)"
                                 rf"(?:{decimal}[0-9]{{1,2}})?", amount)
            if match:
                return int(re.sub(thousands, "", match["whole"]))
    raise ApiShapeError("Renfe's installment checkout returned an unrecognized euro total.")


def _prepare_payment(page: "Page", method: Method) -> dict[str, str]:
    if method != Method.aplazame:
        return {"selected_method": method.value, "total": _select_method(page, method)}

    # Renfe advertises financing on the card option. The buyer chooses Aplazame later at the gateway.
    offer = page.locator("#headingOne .radio-title").filter(has_text=re.compile(r"paga en 4 meses", re.I))
    if not offer.is_visible():
        raise UsageError("Renfe doesn't show the four-month card option for this checkout.")
    total = _select_method(page, Method.card)
    if _whole_euros(total) < 150:
        raise UsageError(f"Renfe requires a total of at least €150 for Aplazame. This checkout totals {total}.")
    return {"requested_method": method.value, "selected_method": Method.card.value, "total": total,
            "next": "At the card gateway, open 'Otras formas de pago', choose Aplazame and review the "
                    "financing terms. If Aplazame is absent, stop before authorizing a payment."}


def _purchase(page: "Page", results: str, method: Method, train: list[str] | None,
              review_only: bool, progress: Callable[[str], None]) -> dict:
    """The only automated clicks expand a train and select a payment method."""
    from playwright.sync_api import Error as BrowserError

    status = "unconfirmed"
    try:
        try:
            response = page.goto(results, referer=legacy.WWW + "/", wait_until="commit")
            if not response or not response.ok or _page_url(page.url) != legacy.RESULTS_URL:
                raise UpstreamError("Renfe didn't open its results page.")
            page.locator("#listaTrenesTBodyIda").wait_for(state="attached", timeout=30000)
        except BrowserError:
            if page.is_closed():
                return {"status": status}
            raise UpstreamError("Renfe accepted the search, but Chrome couldn't load the train list.") from None
        if train:
            _expand_train(page, train)
        progress("Choose your fare, enter the passenger details and review extras in Chrome.")
        page.wait_for_url(lambda u: _page_url(u) == PAYMENT, wait_until="domcontentloaded", timeout=0)
        payment = _prepare_payment(page, method)
        if review_only:
            return {"status": "reviewed", **payment}
        if "next" in payment:
            progress(payment["next"])
        progress(f"Selected {payment['selected_method']} ({payment['total']}). Review Renfe's total and terms, "
                 "then continue in Chrome. "
                 "Keep this window open through the return from the payment provider.")
        page.wait_for_url(lambda u: _page_url(u) == RECEIPT, wait_until="domcontentloaded", timeout=0)
        # The callback and final POST can also carry a cancellation or an error.
        locator = page.locator(".localizator span")
        if (locator.count() == 1 and locator.is_visible() and locator.evaluate("el => Boolean(el.textContent.trim())")
                and page.locator("#imprimirPDF").is_visible()):
            status = "confirmed"
            progress("Renfe confirmed the booking. Save your ticket, then close this checkout window.")
        else:
            progress("Renfe returned without a recognizable ticket confirmation. Check the page before retrying.")
        page.wait_for_event("close", timeout=0)
    except BrowserError:
        if not page.is_closed():
            raise
    return {"status": status}


def checkout(origin: Station, destination: Station, day: date, method: Method, train: list[str] | None,
             review_only: bool, progress: Callable[[str], None]) -> dict:
    if method == Method.apple_pay:
        if review_only:
            raise UsageError("Apple Pay opens Safari for manual checkout. Review-only pricing requires a "
                             "method supported in Chrome.")
        page = shop.link(origin, destination, day)
        if not shop.open_in_safari(page["url"]):
            raise UsageError("Apple Pay checkout needs Safari on macOS. Use `renfe open --no-browser` "
                             "to get the search link for another device.")
        next_step = page["next"]
        if train:
            next_step += f" Look for service {'+'.join(train)} in the results."
        return {"status": "handed_off", "requested_method": method.value, "browser": "Safari",
                "url": page["url"], "next": next_step + " Choose Apple Pay at payment and confirm in Safari."}

    try:
        from playwright.sync_api import Error as BrowserError, sync_playwright
    except ImportError:
        raise UsageError("Install the checkout extra with `uv sync --extra checkout` and use "
                         "`uv run --extra checkout renfe checkout`.") from None

    try:
        with sync_playwright() as playwright:
            try:
                # A temporary Chrome profile shares Renfe's cookies through the provider round trip.
                context = playwright.chromium.launch_persistent_context(
                    "", channel="chrome", chromium_sandbox=True, headless=False, no_viewport=True,
                    locale="es-ES", service_workers="block",
                )
            except BrowserError:
                raise UsageError("Checkout needs installed Google Chrome and a graphical desktop.") from None
            try:
                page = context.pages[0] if context.pages else context.new_page()
                # Keep user-initiated ticket downloads in Chrome's normal downloads directory.
                context.new_cdp_session(page).send("Browser.setDownloadBehavior", {"behavior": "default"})
                if review_only:
                    context.route("**/*", _review_guard())
                # The request context shares the browser cookie jar. Inspect the redirect before following it.
                response = context.request.post(legacy.SEARCH_URL, form=legacy.search_form(origin, destination, day),
                                                headers={"origin": legacy.WWW, "referer": legacy.WWW + "/"},
                                                max_redirects=0)
                results = legacy.results_redirect(response.status, response.headers.get("location", ""))
                return _purchase(page, results, method, train, review_only, progress)
            finally:
                context.close()
    except BrowserError:
        raise UpstreamError("Checkout was interrupted. If you submitted payment, check Renfe's confirmation "
                            "before trying again.") from None
