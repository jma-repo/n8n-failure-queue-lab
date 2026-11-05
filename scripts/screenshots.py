"""Capture the README screenshots from the running stack (needs Microsoft Edge or Chrome installed; no browser download).

    uv run lab up
    uv run --group shots python scripts/screenshots.py --prepare

--prepare first plays a short story through the stack (a schema change, a held batch, an outage that outlasts the retries) so
Mailpit has alerts to show. The n8n login uses the test owner from .env.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

from lab import stack


def prepare(lab: stack.Lab) -> None:
    lab.reset(time_scale=0.005, jitter="none")
    lab.run_ingest()
    lab.scenario("schema_drift")
    lab.run_ingest()
    lab.scenario("jump", pct=0.10)
    lab.run_ingest()
    lab.scenario("http_500")
    lab.run_ingest()
    lab.drive_retries(lambda: lab.failures("dead") and any("http_5xx" in m["Subject"] for m in lab.mails()), timeout=90)
    lab.sim.delete("/admin/scenario")
    lab.trigger("monitor")


def sign_in(page, env: dict[str, str]) -> None:
    page.goto(f"{stack.N8N}/signin")
    page.fill('input[type="email"], input[name="emailOrLdapLoginId"]', env["N8N_OWNER_EMAIL"])
    page.fill('input[type="password"]', env["N8N_OWNER_PASSWORD"])
    page.keyboard.press("Enter")
    page.wait_for_url(lambda url: "/signin" not in url, timeout=30000)
    for _ in range(3):  # dismiss the welcome and survey dialogs if they show
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="docs/img")
    parser.add_argument("--channel", default="msedge", help="msedge or chrome")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    env = stack.read_env()
    if args.prepare:
        prepare(stack.Lab())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=args.channel, headless=True)
        page = browser.new_page(viewport={"width": 1800, "height": 500}, device_scale_factor=2)
        sign_in(page, env)
        for workflow, name, height in (("labIngest01", "n8n-ingest", 470), ("labRetry01", "n8n-retry", 880)):
            page.set_viewport_size({"width": 1800, "height": height})
            page.goto(f"{stack.N8N}/workflow/{workflow}")
            page.wait_for_selector(".vue-flow__node, [data-test-id='canvas-node']", timeout=30000)
            page.wait_for_timeout(2500)
            page.mouse.click(394, 105)  # close the "Production Checklist" card that n8n shows on a fresh browser profile
            page.wait_for_timeout(800)
            page.screenshot(path=str(out / f"{name}.png"))
        mail = browser.new_page(viewport={"width": 1300, "height": 520}, device_scale_factor=1)
        mail.goto(stack.MAIL)
        mail.wait_for_timeout(2000)
        mail.screenshot(path=str(out / "mailpit-alerts.png"))
        browser.close()
    print("wrote", ", ".join(sorted(p.name for p in out.glob("*.png"))))


if __name__ == "__main__":
    main()
