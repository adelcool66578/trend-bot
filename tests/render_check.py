"""Open the built dashboard in headless Chrome, visit every tab, collect JS errors,
and save screenshots (desktop + phone). Usage: python tests/render_check.py <docs_dir> <out_dir>"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

docs, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
tabs = ["overview", "positions", "trades", "signals", "activity", "about"]
errors = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    for label, vp, scheme in [("desktop", {"width": 1280, "height": 900}, "light"),
                              ("dark", {"width": 1280, "height": 900}, "dark"),
                              ("phone", {"width": 390, "height": 844}, "light")]:
        pg = b.new_page(viewport=vp, color_scheme=scheme)
        pg.on("pageerror", lambda e: errors.append(f"{label}: {e}"))
        pg.on("console", lambda m: errors.append(f"{label} console: {m.text}") if m.type == "error" else None)
        for t in tabs if label != "dark" else ["overview", "signals"]:
            pg.goto(f"file://{docs}/index.html#{t}")
            pg.wait_for_timeout(250)
            visible = pg.eval_on_selector(f"section#{t}", "e => getComputedStyle(e).display")
            if visible != "block":
                errors.append(f"{label}: tab {t} not visible")
            pg.screenshot(path=str(out / f"{label}_{t}.png"), full_page=True)
        if label == "desktop":
            pg.goto(f"file://{docs}/index.html#overview"); pg.wait_for_timeout(200)
            box = pg.query_selector("#chart svg").bounding_box()
            pg.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + 100); pg.wait_for_timeout(150)
            if not pg.is_visible("#tip"):
                errors.append("tooltip did not show")
            pg.screenshot(path=str(out / "desktop_tooltip.png"))
            pg.click("text=Hold Bitcoin"); pg.click("#range button[data-r='30']"); pg.wait_for_timeout(150)
            pg.screenshot(path=str(out / "desktop_toggles.png"))
            pg.goto(f"file://{docs}/index.html#trades"); pg.select_option("#f-side", "sell"); pg.wait_for_timeout(100)
            n = pg.eval_on_selector_all("#tradetable tr", "r => r.length")
            sells = pg.eval_on_selector_all("#tradetable .b-sell", "r => r.length")
            print(f"trade filter: {n-1} rows shown, {sells} sell badges")
            pg.click("text=Signals"); pg.wait_for_timeout(100)
            if "#signals" not in pg.url:
                errors.append("nav click did not route")
        pg.close()
    b.close()
print("ERRORS:", errors if errors else "none")
