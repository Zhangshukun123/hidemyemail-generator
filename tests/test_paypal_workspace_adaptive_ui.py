from pathlib import Path

from hidemyemail_generator.web_ui import build_app_page


ROOT = Path(__file__).resolve().parents[1]
HOST_JS = ROOT / "src" / "hidemyemail_generator" / "web_ui" / "static" / "app.js"
HOST_CSS = ROOT / "src" / "hidemyemail_generator" / "web_ui" / "static" / "app.css"
PROTOCOL_ROOT = ROOT / "paypal-agreement-protocol" / "web_static"


def test_host_payment_view_uses_compact_accessible_workbench_shell() -> None:
    page = build_app_page()
    styles = HOST_CSS.read_text(encoding="utf-8")

    assert 'aria-label="协议支付工具栏"' in page
    assert 'class="pp-payment-kicker">PAYPAL AGREEMENT' in page
    assert 'class="pp-payment-empty-copy" role="status" aria-live="polite"' in page
    assert 'class="button pp-payment-retry"' in page
    assert ".pp-payment-view:not([hidden])" in styles
    assert "grid-template-rows: auto minmax(0, 1fr)" in styles
    assert ".pp-payment-frame-shell { height: auto; min-height: 0;" in styles
    assert ".pp-payment-frame-shell { height: calc(100vh - 230px); min-height: 620px" not in styles


def test_host_presenter_builds_embedded_theme_and_job_urls() -> None:
    script = HOST_JS.read_text(encoding="utf-8")

    assert "class PayPalWorkspacePresenter" in script
    assert 'url.searchParams.set("embedded", "1")' in script
    assert 'url.searchParams.set("theme", document.documentElement.dataset.theme || "dark")' in script
    assert 'url.searchParams.set("job", jobId)' in script
    assert "paypalWorkspacePresenter.frameUrl(baseUrl, jobId)" in script
    assert '(data.url || "/paypal-pay/") + "?job="' not in script
    assert len(script.splitlines()) <= 5000


def test_protocol_page_loads_adapter_last_and_uses_mvp_shell() -> None:
    markup = (PROTOCOL_ROOT / "index.html").read_text(encoding="utf-8")
    script = (PROTOCOL_ROOT / "app.js").read_text(encoding="utf-8")

    assert markup.index("protocol-workbench.css") > markup.index("protocol.css")
    assert "class PaymentShellModel" in script
    assert "class PaymentShellView" in script
    assert "class PaymentShellPresenter" in script
    assert "new MutationObserver" in script
    assert "event.origin !== location.origin" in script
    assert "window.self !== window.top" in script


def test_protocol_adapter_matches_host_tokens_and_removes_duplicate_chrome() -> None:
    styles = (PROTOCOL_ROOT / "protocol-workbench.css").read_text(encoding="utf-8")

    for token in ("#0d0d0d", "#181818", "#2b2b2b", "#f2f2f2", "#19c37d"):
        assert token in styles
    assert "html.embedded .topbar" in styles
    assert "html.embedded .protocol-hero" in styles
    assert "html.embedded footer" in styles
    assert "html.embedded .workspace-grid" in styles
    assert "border-radius: 4px" in styles
