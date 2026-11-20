"""The browser checkout against synthetic pages. Every URL is intercepted and the context is offline."""

import os

import pytest

from renfe import checkout, legacy
from renfe.errors import ApiShapeError, UpstreamError, UsageError


@pytest.fixture(scope="module")
def browser():
    if os.getenv("RENFE_TEST_CHROME") != "1":
        pytest.skip("set RENFE_TEST_CHROME=1 to run the offline system Chrome tests")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", chromium_sandbox=True, headless=True)
        yield browser
        browser.close()


@pytest.fixture
def tab(browser):
    context = browser.new_context(offline=True, service_workers="block")
    page = context.new_page()
    page.set_default_timeout(2500)
    yield page
    context.close()


def results():
    return """
        <script>window.trenesIda = {listviajeViewEnlaceBean: [
          {id: 3, trayectos: [{cdgoTren: '00601'}, {cdgoTren: '00533'}]}
        ]}</script>
        <div id="listaTrenesTBodyIda"><div id="tren_i_3"><button class="trenes" onclick="this.textContent='Expanded'">Train</button></div></div>
    """


def payment(suffix="paypal", code="14", sub="", checked=False):
    # Like Renfe's method handler, the test page recalculates when its radio is clicked.
    total = "100 Puntos" if code == "11" else "42.50 €"
    return f"""
        <form method="post">
          <input type="radio" name="radiobutton" id="datosPago_cdgoFormaPago_{suffix}"
                 value="{code}" subCdgoFormaPago="{sub}" onclick="choose(this)" {"checked" if checked else ""}>
          <input type="hidden" id="datosPago_cdgoFormaPago">
          <input type="hidden" id="datosPago_subCdgoFormaPago">
          <span id="totalTrayecto">45,00 €</span><button>Pagar</button>
        </form>
        <script>
        function choose(radio) {{
          datosPago_cdgoFormaPago.value = radio.value;
          datosPago_subCdgoFormaPago.value = radio.getAttribute('subCdgoFormaPago');
          totalTrayecto.textContent = '{total}';
          window.errorPrecios = false;
        }}
        </script>
    """


def fixtures(tab, pages):
    requests = []

    def serve(route):
        key = route.request.method, checkout._page_url(route.request.url)
        requests.append(key)
        if key in pages:
            route.fulfill(content_type="text/html; charset=utf-8", body=pages[key])
        else:
            route.abort()

    tab.context.route("**/*", serve)
    return requests


@pytest.mark.parametrize("method,suffix,code,sub,checked", [
    (checkout.Method.paypal, "paypal", "14", "", False),
    (checkout.Method.paypal, "paypal", "14", "", True),
    (checkout.Method.bizum, "bizum", "02", "05", False),
    (checkout.Method.card, "tarjetaRedSys", "02", "02", False),
    (checkout.Method.google_pay, "googlePay", "02", "06", False),
    (checkout.Method.renfe_points, "tarjetaRenfe", "11", "", False),
])
def test_review_selects_only_requested_method(tab, method, suffix, code, sub, checked):
    requests = fixtures(tab, {("GET", legacy.RESULTS_URL): results(),
                              ("GET", checkout.PAYMENT): payment(suffix, code, sub, checked)})
    tab.context.route("**/*", checkout._review_guard())

    def user_chooses_fare(_):
        assert tab.locator(".trenes").inner_text() == "Expanded"
        tab.goto(checkout.PAYMENT + "?c=example")

    out = checkout._purchase(tab, legacy.RESULTS_URL, method, ["601", "533"], True, user_chooses_fare)
    total = "100 Puntos" if method == checkout.Method.renfe_points else "42.50 €"
    assert out == {"status": "reviewed", "selected_method": method.value, "total": total}
    assert tab.locator("input:checked").input_value() == code
    # Even if the buyer presses Pagar in the review window, the form POST never reaches the shop.
    with tab.expect_event("requestfailed", predicate=lambda req: req.method == "POST" and checkout._page_url(req.url) == checkout.PAYMENT):
        tab.get_by_text("Pagar").click()
    assert ("POST", checkout.PAYMENT) not in requests


@pytest.mark.parametrize("receipt,expected", [
    ('<div class="localizator"><span>ABC123</span></div><button id="imprimirPDF">PDF</button>', "confirmed"),
    ("<p>Payment cancelled</p>", "unconfirmed"),
])
def test_paypal_round_trip_requires_a_renfe_receipt(tab, receipt, expected):
    connector = f"{legacy.BASE}/conectarPayPal.do"
    callback = f"{legacy.BASE}/respuestaPayPal.do"
    wallet = "https://wallet.example.test/approve"
    requests = fixtures(tab, {
        ("GET", legacy.RESULTS_URL): results(), ("GET", checkout.PAYMENT): payment(),
        ("POST", checkout.PAYMENT): f'<script>location.assign("{connector}")</script>',
        ("GET", connector): f'<form method="post" action="{wallet}"></form><script>document.forms[0].submit()</script>',
        ("POST", wallet): f'<a href="{callback}?token=synthetic">Approve</a>',
        ("GET", callback): f'<form method="post" action="{checkout.RECEIPT}"></form>'
                           '<script>document.forms[0].submit()</script>',
        ("POST", checkout.RECEIPT): receipt,
    })

    def user(message):
        if message.startswith("Choose"):
            tab.goto(checkout.PAYMENT)
        elif message.startswith("Selected"):
            assert ("POST", checkout.PAYMENT) not in requests
            tab.get_by_text("Pagar").click()
            tab.get_by_text("Approve").click()
        else:
            tab.evaluate("setTimeout(() => console.log('test-finished'), 25)")

    tab.on("console", lambda msg: tab.close() if msg.text == "test-finished" else None)
    assert checkout._purchase(tab, legacy.RESULTS_URL, checkout.Method.paypal, None, False, user) == {"status": expected}
    assert requests.count(("POST", checkout.PAYMENT)) == requests.count(("POST", checkout.RECEIPT)) == 1


def test_method_unavailable_or_renfe_cannot_price(tab):
    tab.set_content(payment("bizum", "02", "05"))
    with pytest.raises(UsageError, match="doesn't offer"):
        checkout._select_method(tab, checkout.Method.paypal)
    tab.evaluate("window.choose = () => {window.errorPrecios = true}")
    with pytest.raises(UpstreamError, match="did not confirm"):
        checkout._select_method(tab, checkout.Method.bizum)


def test_closing_before_payment_is_unconfirmed(tab):
    fixtures(tab, {("GET", legacy.RESULTS_URL): results()})
    tab.on("console", lambda msg: tab.close() if msg.text == "leave" else None)
    out = checkout._purchase(tab, legacy.RESULTS_URL, checkout.Method.paypal, None, False,
                             lambda _: tab.evaluate("setTimeout(() => console.log('leave'), 25)"))
    assert out == {"status": "unconfirmed"}


@pytest.mark.parametrize("total,euros", [("149,99 €", 149), ("150.00 €", 150), ("150 EUR", 150),
                                         ("1.234,50 €", 1234), ("1,234.50 €", 1234)])
def test_aplazame_converts_displayed_totals_to_whole_euros(total, euros):
    assert checkout._whole_euros(total) == euros


def test_aplazame_rejects_unrecognized_grouping():
    with pytest.raises(ApiShapeError):
        checkout._whole_euros("1,2.50 €")


@pytest.mark.parametrize("quoted,offered,error", [("150.00 €", True, None), ("149.99 €", True, "at least €150"),
                                                  ("200.00 €", False, "four-month card option")])
def test_aplazame_review_uses_the_card_gateway_only_when_offered(tab, quoted, offered, error):
    heading = "Tarjeta / Paga en 4 meses sin intereses" if offered else "Tarjeta"
    markup = payment("tarjetaRedSys", "02", "02").replace("42.50 €", quoted)
    markup = markup.replace("<form method=", f'<div id="headingOne"><span class="radio-title">{heading}</span></div>'
                                             '<form method=')
    requests = fixtures(tab, {("GET", legacy.RESULTS_URL): results(), ("GET", checkout.PAYMENT): markup})
    tab.context.route("**/*", checkout._review_guard())

    def review():
        return checkout._purchase(tab, legacy.RESULTS_URL, checkout.Method.aplazame, None, True,
                                  lambda _: tab.goto(checkout.PAYMENT))

    if error:
        with pytest.raises(UsageError, match=error):
            review()
    else:
        out = review()
        assert out["status"] == "reviewed" and out["total"] == quoted
        assert out["requested_method"] == "aplazame" and out["selected_method"] == "card"
        assert "Otras formas de pago" in out["next"]
    assert ("POST", checkout.PAYMENT) not in requests
