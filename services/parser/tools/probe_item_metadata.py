"""Read public item metadata, storing only photos and publication evidence (no session data)."""

import json
import re
import sys
import urllib.request
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser

root = Path(__file__).resolve().parents[1]
url = (
    sys.argv[1] if len(sys.argv) > 1 else "https://www.vinted.fr/items/10001853847-nike-air-max-90"
)
identity = re.search(r"/items/(\d+)", url)
identity = identity[1] if identity else ""
request = urllib.request.Request(
    "http://127.0.0.1:8191/v1",
    data=json.dumps(
        {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": 45000,
            "returnOnlyCookies": False,
            "blockMedia": True,
        }
    ).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=60) as response:
    payload = json.load(response)
solution = payload.get("solution", {})
tree = LexborHTMLParser(solution.get("response", ""))
chunks = []
for node in tree.css("script"):
    raw = node.text()
    if raw.startswith("self.__next_f.push("):
        try:
            value = json.loads(
                raw[len("self.__next_f.push(") :].removesuffix(")").removesuffix(";")
            )
            if len(value) > 1 and isinstance(value[1], str):
                chunks.append(value[1])
        except ValueError:
            pass
records = []


def walk(value):
    if isinstance(value, dict):
        if identity and str(value.get("id", value.get("item_id", ""))) == identity:
            records.append(
                {
                    "keys": list(value),
                    "metadata": {
                        k: v
                        for k, v in value.items()
                        if k in {"id", "item_id", "photos"}
                        or re.search("created|updated|upload|publish|date|time", k)
                    },
                }
            )
        # Source upload labels may occur outside the item object.
        if any(
            isinstance(v, str) and re.search(r"Uploaded|Ajouté|upload_date|created_at", v)
            for v in value.values()
        ):
            records.append(
                {"labels": {k: v for k, v in value.items() if isinstance(v, (str, int))}}
            )
        for v in value.values():
            walk(v)
    elif isinstance(value, list):
        for v in value:
            walk(v)


for line in "".join(chunks).splitlines():
    try:
        walk(json.loads(line.partition(":")[2]))
    except ValueError:
        pass
nodes = [
    n.html
    for n in tree.css("[data-testid]")
    if re.search("upload|created|date", n.attrs.get("data-testid", ""), re.I)
]
links = [n.attrs.get("href") for n in tree.css('a[data-testid$="--overlay-link"]')][:5]
sort_evidence = list(dict.fromkeys(re.findall(r".{0,60}newest_first.{0,100}", "".join(chunks))))[
    :10
]
report = {
    "url": url,
    "status": solution.get("status"),
    "records": records,
    "nodes": nodes,
    "links": links,
    "sort_evidence": sort_evidence,
}
destination = root / ".artifacts/gallery-probe/item-metadata.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in report.items() if k != "records"}, ensure_ascii=True))
