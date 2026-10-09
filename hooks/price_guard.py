#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
textile-expert / price_guard.py  —  PreToolUse-хук.

Захищає від типових цінових помилок ПЕРЕД тим, як вони потраплять у магазин:
  * масові імпорти цін у магазин через MCP (Horoshop: catalog_import, product_set_import,
    catalog_import_residues);
  * запис і редагування CSV/TSV із колонкою ціни (Write, Edit, MultiEdit — для Edit/MultiEdit
    перевіряється файл після застосування правки);
  * ціна < 0 або не число (NaN/inf), ціна нижча за собівартість (якщо вона є в даних),
    маржа нижча за поріг (config: min_gross_margin_pct), викид відносно медіани партії
    (config: price_outlier_ratio — «зайвий/пропущений нуль»), «знижка» ≥ 50 % від старої ціни,
    нечитабельне значення ціни.

Рішення: deny — безумовні помилки; ask — підозріле, питаємо людину; нічого не виводимо — якщо все гаразд.

Безпека: локально, офлайн, лише stdlib; вміст нікуди не пишеться; fail-open при внутрішній помилці
(крім випадку «CSV з колонкою ціни не вдалося розібрати» — тоді ask).
CLI для перевірки CSV без хука:  python3 -I price_guard.py --csv prices.csv
"""
from __future__ import print_function

import csv
import io
import json
import math
import os
import re
import stat
import sys
import traceback
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
DATA_DIR = os.path.join(SKILL_DIR, "data")

MAX_FILE = 5000000
MAX_RECORDS = 200000
MAX_ROWS_SCANNED = 1000000  # рядків CSV, які взагалі переглядаються (порожні рядки теж рахуються)

DEFAULT_CONFIG = {
    "min_gross_margin_pct": 20,
    "price_outlier_ratio": 30,
    "lint_block_on_error": True,
    "stop_check_enabled": True,
    "legal_as_of": "",
    "confidential_terms": [],
}

PRICE_KEYS = ("price", "price_uah", "retail_price", "ціна", "цена")
OLD_KEYS = ("price_old", "old_price", "oldprice", "стара_ціна")
COST_KEYS = ("cost", "cost_price", "purchase_price", "price_purchase", "собівартість", "себестоимость", "закупка")
ID_KEYS = ("article", "sku", "артикул", "code", "id", "title", "name")

PRICE_NAMES = {"price", "price uah", "ціна", "ціна грн", "цена", "цена грн", "роздрібна ціна", "retail price", "retail",
               "verkaufspreis", "vk preis", "vk", "preço de venda", "pvp"}
OLD_NAMES = {"price old", "old price", "oldprice", "стара ціна", "старая цена", "price old uah"}
COST_NAMES = {"cost", "cost price", "purchase price", "price purchase", "собівартість", "себестоимость", "закупка",
              "закупівельна ціна", "закупочная цена", "einkaufspreis", "ek preis", "ek", "preço de custo", "custo",
              "preço de compra"}
ID_NAMES = {"article", "sku", "артикул", "код", "id", "artikel", "artikelnummer", "referência", "ref"}


# ---------------------------------------------------------------- конфіг

def _load_json(path, default, limit=65536):
    """Безпечне читання JSON: лише звичайний файл і не більше `limit` байт, інакше — default.
    Конфіг проєкту приходить із чужого репозиторію: FIFO чи пристрій (/dev/zero) не повинні ні зависати, ні їсти пам'ять."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return default
        with os.fdopen(fd, "rb") as f:
            fd = None
            raw = f.read(limit + 1)
        if len(raw) > limit:
            return default
        v = json.loads(raw.decode("utf-8-sig"))
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default
    finally:
        if fd is not None:
            os.close(fd)


def sanitize_config(cfg):
    d = dict(DEFAULT_CONFIG)

    def num(k, lo, hi):
        v = cfg.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi:
            d[k] = float(v)

    num("min_gross_margin_pct", 0, 99.9)
    num("price_outlier_ratio", 2, 100000)
    return d


def load_config(project_dir=None):
    raw = {}
    raw.update(_load_json(os.path.join(DATA_DIR, "config.json"), {}))
    if project_dir:
        raw.update(_load_json(os.path.join(project_dir, ".claude", "textile-expert.json"), {}))
    return sanitize_config(raw)


def _clean(s, n):
    """Значення з файлу/запиту потрапляє у відповідь хука: керуючі й форматні символи -> пробіл, довжина <= n."""
    s = "".join(" " if unicodedata.category(c)[0] == "C" or c in u"\u2028\u2029" else c for c in s)
    return re.sub(r"\s+", " ", s).strip()[:n]


# ---------------------------------------------------------------- числа

def parse_num(v):
    """float або None, якщо значення не можна надійно розібрати (в т.ч. NaN/inf, наукова нотація)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        s = v.replace(" ", " ").replace(" ", " ").replace("−", "-").replace("–", "-").replace("—", "-").strip()
        if not s or re.search(r"\d[eE][+\-]?\d", s):
            return None
        s = re.sub(r"(?<=\d) (?=\d{3}(?:\D|$))", "", s)
        s = re.sub(r"[^\d,.\-]", "", s).replace(",", ".")
        if s.count(".") > 1:
            head, _, tail = s.rpartition(".")
            s = head.replace(".", "") + "." + tail
        if not re.match(r"^-?\d+(\.\d+)?$|^-?\.\d+$", s):
            return None
        try:
            x = float(s)
        except ValueError:
            return None
        return x if math.isfinite(x) else None
    return None


def _empty(v):
    return v is None or (isinstance(v, str) and not v.strip())


def walk(obj, records, depth=0):
    """Збирає словники з полем ціни з будь-якої вкладеності (depth ≤ 8)."""
    if depth > 8 or len(records) >= MAX_RECORDS:
        return
    if isinstance(obj, str):
        s = obj.strip()
        if s[:1] in ("[", "{") and len(s) < MAX_FILE:
            try:
                walk(json.loads(s), records, depth + 1)
            except Exception:
                pass
        return
    if isinstance(obj, list):
        for x in obj:
            walk(x, records, depth + 1)
        return
    if isinstance(obj, dict):
        low = {str(k).lower(): v for k, v in obj.items()}
        old = next((parse_num(low[k]) for k in OLD_KEYS if k in low and not _empty(low[k])), None)
        cost = next((parse_num(low[k]) for k in COST_KEYS if k in low and not _empty(low[k])), None)
        ident = ""
        for k in ID_KEYS:
            if k in low and isinstance(low[k], (str, int)) and not isinstance(low[k], bool):
                ident = _clean(str(low[k]), 40)
                break
        for k in PRICE_KEYS:
            if k in low and not _empty(low[k]):
                raw = low[k]
                val = parse_num(raw)
                rec = {"price": val, "old": old, "cost": cost, "id": ident, "bad": None}
                if val is None:
                    rec["bad"] = "nonfinite" if isinstance(raw, float) else _clean(str(raw), 30)
                records.append(rec)
        for v in obj.values():
            walk(v, records, depth + 1)


# ---------------------------------------------------------------- CSV

def _norm_h(h):
    h = re.sub(r"\([^()]*\)", "", h.replace("﻿", "")).strip().strip("\"'").lower()
    return re.sub(r"[\s_,\-]+", " ", h).strip()


def records_from_csv(content):
    """Повертає список записів; None — якщо колонка ціни є, але CSV не вдалося розібрати."""
    content = content.lstrip("﻿").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    head = content.split("\n", 1)[0]
    delim = max((";", ",", "\t"), key=lambda d: head.count(d))
    try:
        csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    except Exception:
        pass
    hdr_norm = [_norm_h(h) for h in head.split(delim)]
    if not any(h in PRICE_NAMES for h in hdr_norm):
        return []
    reader = csv.reader(io.StringIO(content), delimiter=delim)
    try:
        first = next(reader, None)
    except csv.Error:
        return None
    if first is None:
        return []
    hdr = [_norm_h(h) for h in first]
    pi = next((i for i, h in enumerate(hdr) if h in PRICE_NAMES), None)
    if pi is None:
        return []
    oi = next((i for i, h in enumerate(hdr) if h in OLD_NAMES), None)
    ci = next((i for i, h in enumerate(hdr) if h in COST_NAMES), None)
    ii = next((i for i, h in enumerate(hdr) if h in ID_NAMES), None)
    recs = []
    scanned = 0
    while scanned < MAX_ROWS_SCANNED:
        try:
            r = next(reader, None)
        except csv.Error:
            return None
        if r is None:
            break
        scanned += 1
        if len(r) <= pi or _empty(r[pi]):
            continue
        val = parse_num(r[pi])
        rec = {
            "price": val,
            "old": parse_num(r[oi]) if oi is not None and len(r) > oi else None,
            "cost": parse_num(r[ci]) if ci is not None and len(r) > ci else None,
            "id": (_clean(r[ii], 40) if ii is not None and len(r) > ii else ""),
            "bad": None if val is not None else _clean(r[pi], 30),
        }
        recs.append(rec)
        if len(recs) >= MAX_RECORDS:
            break
    return recs


def apply_edits(content, edits):
    for e in edits:
        if not isinstance(e, dict):
            continue
        old, new = e.get("old_string"), e.get("new_string")
        if isinstance(old, str) and isinstance(new, str) and old and old in content:
            content = content.replace(old, new) if e.get("replace_all") else content.replace(old, new, 1)
    return content


# ---------------------------------------------------------------- оцінка

def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def evaluate(recs, cfg):
    deny, ask = [], []
    min_margin = float(cfg.get("min_gross_margin_pct", 20))
    ratio = float(cfg.get("price_outlier_ratio", 30))

    good = []
    for r in recs:
        tag = " [%s]" % r["id"] if r["id"] else ""
        p = r["price"]
        if p is None:
            if r.get("bad") == "nonfinite":
                deny.append("ціна не є скінченним числом (NaN/inf)%s" % tag)
            else:
                ask.append("нечитабельне значення ціни «%s»%s" % (r.get("bad"), tag))
            continue
        good.append(r)
        if p < 0:
            deny.append("від’ємна ціна %g%s" % (p, tag))
            continue
        if p == 0:
            ask.append("ціна 0%s — це «безкоштовно»; так задумано?" % tag)
        if r.get("cost") and r["cost"] > 0 and p > 0:
            if p < r["cost"]:
                deny.append("ціна %g нижча за собівартість %g%s" % (p, r["cost"], tag))
            else:
                m = (p - r["cost"]) / p * 100.0
                if m < min_margin:
                    ask.append("валова маржа %.1f %% < порогу %g %% (ціна %g, собівартість %g)%s" % (m, min_margin, p, r["cost"], tag))
        if r.get("old") and r["old"] > 0 and p > 0 and p <= r["old"] * 0.5:
            ask.append("«знижка» %.0f %% (стара %g → нова %g)%s: підтвердіть, що товар справді продавався за старою ціною" % (
                (1 - p / r["old"]) * 100, r["old"], p, tag))

    prices = [r["price"] for r in good if r["price"] > 0]
    if len(prices) >= 5:
        m = median(prices)
        odd = [r for r in good if r["price"] > 0 and (r["price"] < m / ratio or r["price"] > m * ratio)]
        for r in odd[:3]:
            ask.append("ціна %g відрізняється від медіани партії (%g) у >%gх — можлива помилка в нулях/десятковому розділювачі%s" % (
                r["price"], m, ratio, (" [%s]" % r["id"] if r["id"] else "")))
        if len(odd) > 3:
            ask.append("…ще %d цін-викидів" % (len(odd) - 3))
    return deny, ask


def decision(kind, items, total):
    body = "\n".join("• " + x for x in items[:8])
    if len(items) > 8:
        body += "\n• …і ще %d" % (len(items) - 8)
    head = {
        "deny": "textile-expert: цінова помилка, запис заблоковано (перевірено %d поз.):" % total,
        "ask": "textile-expert: цінові сигнали, потрібне підтвердження (перевірено %d поз.):" % total,
    }[kind]
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": kind,
        "permissionDecisionReason": head + "\n" + body}}


# ---------------------------------------------------------------- хук

def _read_csv_file(path, cwd):
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    if not os.path.isfile(full):
        return ""
    with open(full, "rb") as f:
        return f.read(MAX_FILE).decode("utf-8", errors="replace")


def hook_main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")  # завжди UTF-8, а не локаль процесу
    tool = str(data.get("tool_name") or "")
    ti = data.get("tool_input") or {}
    if not isinstance(ti, dict):
        return 0
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else os.getcwd()
    cfg = load_config(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)

    recs = []
    if tool in ("Write", "Edit", "MultiEdit"):
        fp = str(ti.get("file_path") or "")
        if os.path.splitext(fp)[1].lower() not in (".csv", ".tsv"):
            return 0
        if tool == "Write":
            content = str(ti.get("content") or "")[:MAX_FILE]
        else:
            edits = ti.get("edits") if tool == "MultiEdit" else [ti]
            content = apply_edits(_read_csv_file(fp, cwd), edits if isinstance(edits, list) else [])
        recs = records_from_csv(content)
        if recs is None:
            sys.stdout.write(json.dumps(decision(
                "ask", ["CSV із колонкою ціни не вдалося розібрати (некоректні лапки/розмір поля) — перевірте файл вручну"], 0)))
            return 0
    elif tool.startswith("mcp__"):
        walk(ti, recs)
    if not recs:
        return 0
    deny, ask = evaluate(recs, cfg)
    if deny:
        sys.stdout.write(json.dumps(decision("deny", deny, len(recs))))
    elif ask:
        sys.stdout.write(json.dumps(decision("ask", ask, len(recs))))
    return 0


def cli_main(argv):
    if len(argv) != 2 or argv[0] != "--csv":
        print("usage: price_guard.py --csv FILE")
        return 2
    with open(argv[1], "rb") as f:
        content = f.read(MAX_FILE).decode("utf-8", errors="replace")
    recs = records_from_csv(content)
    if recs is None:
        print("ASK:  CSV не вдалося розібрати")
        return 1
    deny, ask = evaluate(recs, load_config(os.getcwd()))
    for x in deny:
        print("DENY: " + x)
    for x in ask:
        print("ASK:  " + x)
    print("перевірено позицій: %d" % len(recs))
    return 1 if deny else 0


def main():
    if len(sys.argv) > 1:
        return cli_main(sys.argv[1:])
    try:
        return hook_main()
    except Exception:
        if os.environ.get("TEXTILE_EXPERT_DEBUG"):
            traceback.print_exc()
        return 0  # fail-open


if __name__ == "__main__":
    sys.exit(main())
