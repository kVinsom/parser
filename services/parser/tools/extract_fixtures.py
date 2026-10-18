"""Create small, redacted DOM fixtures from local gateway captures (no network)."""

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path

from selectolax.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]


def main():
    args = argparse.ArgumentParser()
    args.add_argument("capture_directory", type=Path)
    args = args.parse_args()
    output = ROOT / "tests" / "fixtures"
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for region in ("UK", "FR", "IT", "DE"):
        source = args.capture_directory / f"{region}.html.gz"
        original = source.read_bytes()
        tree = HTMLParser(gzip.decompress(original))
        cards = [
            n
            for n in tree.css('[data-testid^="product-item-id-"]')
            if re.fullmatch(r"product-item-id-\d+", n.attributes["data-testid"])
        ]
        selected, conditions = [], set()
        for i, card in enumerate(cards):
            description = card.css_first('a[data-testid$="--overlay-link"]').attributes["title"]
            condition = re.search(
                r"(?:Condition|État|Condizioni|Zustand): (.*?)"
                r"(?:, (?:Size|Taille|Taglia|Größe): |, \d+\.\d{2} [£€])",
                description,
            )
            value = condition[1] if condition else None
            if i < 8 or value not in conditions:
                selected.append(card)
                conditions.add(value)
        sections = []
        for i, card in enumerate(selected, 10001):
            old_id = card.attributes["data-testid"].removeprefix("product-item-id-")
            # Remove favorite counts, buttons, icons; retain observed extraction structure.
            for node in card.css("button, svg, script, style"):
                node.decompose()
            for node in card.traverse(include_text=False):
                for key in list(node.attributes):
                    if key not in ("data-testid", "href", "title", "src", "alt"):
                        del node.attrs[key]
                if node.attributes.get("href"):
                    node.attrs["href"] = f"/items/{i}-fixture-item?referrer=catalog"
                if node.attributes.get("src"):
                    node.attrs["src"] = f"https://images1.vinted.net/t/redacted/{i}.webp"
            sections.append(card.html.replace(old_id, str(i)))
        pagination = tree.css_first('[data-testid="catalog-pagination"]')
        for node in pagination.css("svg"):
            node.decompose()
        for node in pagination.traverse(include_text=False):
            for key in list(node.attributes):
                if key not in ("data-testid", "href", "aria-disabled", "aria-current"):
                    del node.attrs[key]
        sections.append(pagination.html)
        content = (
            "<!doctype html><html><head><title>Vinted</title></head><body>\n"
            + "\n".join(sections)
            + "\n</body></html>\n"
        )
        target = output / f"{region}.html"
        target.write_text(content, encoding="utf-8")
        manifest.append(
            {
                "region": region,
                "capture": source.parent.name + "/" + source.name,
                "capture_sha256": hashlib.sha256(original).hexdigest(),
                "fixture": target.name,
                "fixture_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "cards": len(selected),
            }
        )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
