#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import re
from typing import Any

from playwright.sync_api import sync_playwright


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def validate_viewer(urls_path: pathlib.Path, output: pathlib.Path, screenshot_dir: pathlib.Path) -> None:
    data = json.loads(urls_path.read_text(encoding="utf-8"))
    urls = {
        "health": str(data.get("health_viewer_url") or "").strip(),
        "execution": str(data.get("execution_profile_viewer_url") or "").strip(),
        "allocation": str(data.get("allocation_profile_viewer_url") or "").strip(),
    }
    missing = [name for name, url in urls.items() if not url]
    if missing:
        raise RuntimeError(f"missing viewer URL(s): {missing}")

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"status": "running", "urls": urls, "pages": {}}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1200})
        try:
            for kind, url in urls.items():
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=90000)
                if response is None or response.status >= 400:
                    status = None if response is None else response.status
                    raise RuntimeError(f"{kind} viewer navigation failed: HTTP {status} {url}")

                page.wait_for_selector("body", timeout=30000)
                if kind == "health":
                    page.wait_for_function(
                        """() => [...document.querySelectorAll('div.metric h1')].some(
                            el => (el.textContent || '').includes('Memory') &&
                                  (el.textContent || '').includes('(alloc)'))""",
                        timeout=90000,
                    )
                    allocation_card = page.evaluate(
                        """() => {
                            const cards = [...document.querySelectorAll('div.metric')];
                            const card = cards.find(el => {
                                const heading = el.querySelector('h1')?.textContent || '';
                                return heading.includes('Memory') && heading.includes('(alloc)');
                            });
                            if (!card) return null;
                            return {
                                heading: card.querySelector('h1')?.textContent || '',
                                text: card.textContent || '',
                                svgCount: card.querySelectorAll('svg').length,
                                pathCount: card.querySelectorAll('path').length,
                            };
                        }"""
                    )
                    if not allocation_card:
                        raise RuntimeError("health viewer did not render the Memory (alloc) metric card")
                    heading = normalized(str(allocation_card["heading"]))
                    card_text = normalized(str(allocation_card["text"]))
                    if "Memory" not in heading or "(alloc)" not in heading:
                        raise RuntimeError(f"unexpected allocation metric heading: {heading!r}")
                    if "Bytes/sec" not in card_text:
                        raise RuntimeError(f"allocation metric card has no Bytes/sec legend: {card_text!r}")
                    if int(allocation_card["svgCount"]) < 1 or int(allocation_card["pathCount"]) < 1:
                        raise RuntimeError(f"allocation metric graph was not rendered: {allocation_card}")
                    result["pages"][kind] = {
                        "http_status": response.status,
                        "allocation_heading": heading,
                        "allocation_card_text": card_text,
                        "svg_count": int(allocation_card["svgCount"]),
                        "path_count": int(allocation_card["pathCount"]),
                    }
                else:
                    page.wait_for_timeout(2500)
                    body = normalized(page.locator("body").inner_text())
                    lowered = body.casefold()
                    fatal_markers = (
                        "failed to fetch spark data",
                        "unable to load spark data",
                        "report not found",
                        "404 - page not found",
                        "internal server error",
                    )
                    if len(body) < 80 or any(marker in lowered for marker in fatal_markers):
                        raise RuntimeError(f"{kind} viewer did not render a usable report: {body[:500]!r}")
                    result["pages"][kind] = {
                        "http_status": response.status,
                        "body_excerpt": body[:600],
                    }

                screenshot = screenshot_dir / f"viewer-{kind}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                result["pages"][kind]["screenshot"] = str(screenshot)
                page.close()
        finally:
            browser.close()

    result["status"] = "PASS"
    output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", default="allocation-rate-evidence.json")
    parser.add_argument("--output", default="viewer-allocation-evidence.json")
    parser.add_argument("--screenshots", default="viewer-screenshots")
    args = parser.parse_args()
    validate_viewer(pathlib.Path(args.evidence), pathlib.Path(args.output), pathlib.Path(args.screenshots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
