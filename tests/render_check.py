"""Open the built app in headless Chrome, visit every section, collect JS errors,
exercise charts/shortcuts/filters, and save screenshots.
Usage: python tests/render_check.py <docs_dir> <out_dir>"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

docs, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
tabs = ["overview", "charts", "positions", "trades", "signals", "activity", "about"]
errors = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    for label, vp, scheme in [("mac", {"width": 1440, "height": 900}, "light"),
                              ("dark", {"width": 1440, "height": 900}, "dark"),
                              ("narrow", {"width": 700, "height": 900}, "light")]:
        pg = b.new_page(viewport=vp, color_scheme=scheme)
        pg.on("pageerror", lambda e, l=label: errors.append(f"{l}: {e}"))
        pg.on("console", lambda m, l=label: errors.append(f"{l} console: {m.text}") if m.type == "error" and "live.js" not in m.text else None)
        for t in tabs if label != "dark" else ["overview", "charts", "signals"]:
            pg.goto(f"file://{docs}/index.html#{t}")
            pg.wait_for_timeout(350)
            if pg.eval_on_selector(f"section#{t}", "e => getComputedStyle(e).display") != "block":
                errors.append(f"{label}: section {t} not visible")
            pg.screenshot(path=str(out / f"{label}_{t}.png"), full_page=(label == "narrow"))
        if label == "mac":
            pg.goto(f"file://{docs}/index.html#charts"); pg.wait_for_timeout(400)
            n_svg = pg.eval_on_selector_all("#charts .fig svg", "e => e.length")
            print("charts drawn:", n_svg)
            box = pg.query_selector("#cBTC svg").bounding_box()
            pg.mouse.move(box["x"] + box["width"] * .7, box["y"] + 80); pg.wait_for_timeout(150)
            if not pg.is_visible("#cBTC .tip"):
                errors.append("BTC tooltip did not show")
            pg.screenshot(path=str(out / "mac_tooltip.png"))
            pg.keyboard.press("Meta+4"); pg.wait_for_timeout(150)
            if not pg.url.endswith("#trades"):
                errors.append("Cmd+4 shortcut failed: " + pg.url)
            pg.click("#ov-range button[data-r='1']") if pg.is_visible("#ov-range") else None
            pg.goto(f"file://{docs}/index.html#overview"); pg.wait_for_timeout(200)
            pg.click("#ov-range button[data-r='1']"); pg.wait_for_timeout(200)
            pg.screenshot(path=str(out / "mac_overview_48h.png"))
            pg.click(".rng[data-c='BTC'] button[data-d='365']") if False else None
        pg.close()
    b.close()
print("ERRORS:", errors if errors else "none")
