"""Собирает feed/avito.xml для Автозагрузки Авито.

Список объявлений: feed/ads.json (id, direction, city, необязательно title и price).
Параметры направлений: feed/directions.json.
Текст объявления: ads/<direction>/text.md, фото: ads/<direction>/photos/ (по имени, не больше 10).

ВАЖНО: в режиме загрузки по ссылке Авито снимает объявление, которого нет в фиде.
Удалять строку из ads.json можно только если объявление действительно нужно снять.
Id объявления менять нельзя, иначе Авито создаст новое.
"""
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = "https://raw.githubusercontent.com/kerico-0548/avito-feed/main/"
MAX_DESC = 7500


def md_to_html(text):
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    paras, cur = [], []
    for line in lines:
        line = line.rstrip()
        if line.strip() in ("", "---"):
            if cur:
                paras.append(cur)
                cur = []
            continue
        cur.append(line)
    if cur:
        paras.append(cur)

    def fmt(s):
        s = html.escape(s.strip().lstrip("#").strip())
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    return "".join("<p>" + "<br>".join(fmt(x) for x in p) + "</p>" for p in paras)


def main():
    directions = json.loads((ROOT / "feed/directions.json").read_text(encoding="utf-8"))
    ads = json.loads((ROOT / "feed/ads.json").read_text(encoding="utf-8"))
    errors, seen, out = [], set(), []

    for ad in ads:
        aid, d = ad["id"], ad["direction"]
        if aid in seen:
            errors.append(f"{aid}: повтор Id")
        seen.add(aid)
        if d not in directions:
            errors.append(f"{aid}: нет направления {d}")
            continue
        cfg = directions[d]
        text_path = ROOT / "ads" / d / "text.md"
        if not text_path.exists():
            errors.append(f"{aid}: нет {text_path.relative_to(ROOT)}")
            continue
        desc = md_to_html(text_path.read_text(encoding="utf-8"))
        if len(desc) > MAX_DESC:
            errors.append(f"{aid}: описание {len(desc)} символов, лимит {MAX_DESC}")
        photos = sorted(p for p in (ROOT / "ads" / d / "photos").glob("*")
                        if p.suffix.lower() in (".png", ".jpg", ".jpeg"))[:10]
        if not photos:
            errors.append(f"{aid}: нет фото")

        fields = {"Id": aid, **cfg["fields"],
                  "Title": ad.get("title", cfg["title"]),
                  "Price": str(ad.get("price", cfg["price"])),
                  "Address": ad["city"],
                  "ContactMethod": "По телефону и в сообщениях"}
        x = "  <Ad>\n"
        for k, v in fields.items():
            x += f"    <{k}>{html.escape(str(v))}</{k}>\n"
        x += f"    <Description><![CDATA[{desc}]]></Description>\n    <Images>\n"
        for p in photos:
            x += f'      <Image url="{RAW}{p.relative_to(ROOT).as_posix()}"/>\n'
        x += "    </Images>\n  </Ad>\n"
        out.append(x)

    if errors:
        sys.exit("Ошибки:\n" + "\n".join(errors))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<Ads formatVersion="3" target="Avito.ru">\n'
           + "".join(out) + "</Ads>\n")
    (ROOT / "feed/avito.xml").write_text(xml, encoding="utf-8")
    print(f"Готово: {len(out)} объявлений в feed/avito.xml")


if __name__ == "__main__":
    main()
