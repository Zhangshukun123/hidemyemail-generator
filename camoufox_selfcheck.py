import os

from camoufox import utils
from camoufox.pkgman import get_path
from camoufox.sync_api import NewBrowser
from hidemyemail_generator.openai_browser_bridge import (
    prepare_writable_camoufox_fontconfig,
)
from playwright.sync_api import sync_playwright


runtime = prepare_writable_camoufox_fontconfig()
print("xdg_cache_home=" + str(os.environ.get("XDG_CACHE_HOME")))
print("generated=" + utils._generate_fontconfig(get_path("fontconfig/linux")))
with sync_playwright() as playwright:
    browser = NewBrowser(
        playwright,
        headless=True,
        os="linux",
        humanize=False,
        geoip=False,
        enable_cache=False,
        timeout=60000,
    )
    context = browser.new_context()
    page = context.new_page()
    page.goto("about:blank")
    print("camoufox_launch=ok")
    context.close()
    browser.close()
if runtime is not None:
    runtime.cleanup()
