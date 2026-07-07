from __future__ import annotations

import json
import math
import sys
import uuid
from pathlib import Path


DATA_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
INVENTORY_FILE = DATA_DIR / "thread_inventory.json"


def normalize_hex(value: str) -> str:
    text = value.strip().lower()
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("Thread color must be a hex value like #222222.")
    return f"#{text}"


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    color = normalize_hex(value)
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def rgb_distance(a: str, b: str) -> float:
    ar, ag, ab = hex_to_rgb(a)
    br, bg, bb = hex_to_rgb(b)
    return math.sqrt((ar - br) ** 2 + (ag - bg) ** 2 + (ab - bb) ** 2)


def load_inventory(path: Path = INVENTORY_FILE) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            color = normalize_hex(str(item.get("color", "")))
        except ValueError:
            continue
        quantity = item.get("quantity", 1)
        try:
            quantity = max(0, int(quantity))
        except (TypeError, ValueError):
            quantity = 1
        items.append(
            {
                "id": str(item.get("id") or uuid.uuid4().hex),
                "brand": str(item.get("brand", "")).strip(),
                "name": str(item.get("name", "")).strip(),
                "color": color,
                "quantity": quantity,
            }
        )
    return items


def save_inventory(items: list[dict], path: Path = INVENTORY_FILE) -> None:
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")


def add_inventory_item(
    *,
    brand: str,
    name: str,
    color: str,
    quantity: int,
    path: Path = INVENTORY_FILE,
) -> dict:
    items = load_inventory(path)
    item = {
        "id": uuid.uuid4().hex,
        "brand": brand.strip(),
        "name": name.strip(),
        "color": normalize_hex(color),
        "quantity": max(0, int(quantity)),
    }
    items.append(item)
    save_inventory(items, path)
    return item


def delete_inventory_item(item_id: str, path: Path = INVENTORY_FILE) -> None:
    items = [item for item in load_inventory(path) if item["id"] != item_id]
    save_inventory(items, path)


def closest_inventory_match(color: str, inventory: list[dict]) -> dict | None:
    if not inventory:
        return None
    return min(
        inventory,
        key=lambda item: rgb_distance(color, item["color"]),
    )
