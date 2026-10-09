#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
textile-expert / stop_check.py  —  Stop-хук.

Перед завершенням відповіді перевіряє два «професійні» мінімуми:
  1) правові/митні/податкові твердження мають дату актуальності («станом на ДД.ММ.РРРР») і
     вказівку звірити з первинним джерелом — норми для текстилю та імпорту в Україні змінюються
     (воєнний стан, ЗВТ з Туреччиною, перелік товарів, що не підлягають поверненню тощо);
  2) розрахунок собівартості/ціни показує припущення (курс, мито, ПДВ, логістика).
Спрацьовує не більше одного разу за відповідь (stop_hook_active) і лише для текстильних тем.

Безпека: лише stdlib, офлайн, нічого не логує; fail-open.
Тест без хука:  echo '{"last_assistant_message":"..."}' | python3 -I stop_check.py
"""
from __future__ import print_function

import json
import os
import re
import stat
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
DATA_DIR = os.path.join(SKILL_DIR, "data")

TEXTILE_RE = re.compile(
    r"тканин|текстил|постіл|штор|рушник|бяз|ранфорс|поплін|сатин|перкаль|махр|фланел|жакард|велюр|блекаут|тюл|"
    r"бавовн|льон|лляни|шовк|вовн|поліестер|textile|fabric|ковдр|плед|простир|підодіяльн|наволоч", re.I)
LEGAL_PATTERNS = [
    r"\bмит[оау]\b|митн", r"\bпдв\b", r"укт[зс]ед", r"повернен|обмін\b|обмен",
    r"\bзакон|постанов|наказ\s{1,3}(?:№|мінекономіки)", r"маркуван", r"декларац",
    r"\bзвт\b|вільн\w{1,12}\s{1,3}торгівл|free\s{1,3}trade|\bfta\b", r"сертифікац|сертификац",
    r"дпсс|держпродспоживслужб", r"воєнн\w{1,10}\s{1,3}стан", r"\bфоп\b|єдин\w{1,10}\s{1,3}податок",
    r"eur\.?\s{0,2}1|pan-?euro|походженн", r"ліценз",
]
LEGAL_RE = [re.compile(p, re.I) for p in LEGAL_PATTERNS]
DATE_RE = re.compile(r"станом\s{1,3}на|as\s{1,3}of|актуальн\w{1,10}\s{1,3}на|на\s{1,3}дату", re.I)
VERIFY_RE = re.compile(r"перевір|звір|уточн|підтверд|verify|check", re.I)
COST_RE = re.compile(r"собівартіст|landed|кінцева\s{1,3}ціна|повна\s{1,3}вартість|розрахунок\s{1,3}(?:ціни|собівартості)", re.I)
ASSUMP_RE = re.compile(r"курс|мито|ставк\w{1,10}\s{1,3}мита|пдв|логіст|доставк|брак|\bусадк", re.I)


DEFAULT_CONFIG = {"stop_check_enabled": True, "legal_as_of": ""}


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


def load_config(project_dir=None):
    raw = {}
    raw.update(_load_json(os.path.join(DATA_DIR, "config.json"), {}))
    if project_dir:
        raw.update(_load_json(os.path.join(project_dir, ".claude", "textile-expert.json"), {}))
    d = dict(DEFAULT_CONFIG)
    if isinstance(raw.get("stop_check_enabled"), bool):
        d["stop_check_enabled"] = raw["stop_check_enabled"]
    v = raw.get("legal_as_of")
    if isinstance(v, str) and re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", v):  # лише дата, жодного довільного тексту
        d["legal_as_of"] = v
    return d


def evaluate(msg):
    """Повертає список зауважень (порожній — все гаразд)."""
    msg = msg[:200000]
    if len(msg) < 350 or not TEXTILE_RE.search(msg):
        return []
    problems = []
    legal_hits = sum(1 for rx in LEGAL_RE if rx.search(msg))
    if legal_hits >= 3 and not (DATE_RE.search(msg) and VERIFY_RE.search(msg)):
        problems.append("legal")
    if COST_RE.search(msg) and re.search(r"\d", msg) and len(ASSUMP_RE.findall(msg)) < 2:
        problems.append("cost")
    return problems


def hook_main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")  # завжди UTF-8, а не локаль процесу
    if data.get("stop_hook_active"):
        return 0
    msg = data.get("last_assistant_message")
    if not isinstance(msg, str) or not msg:
        return 0
    cfg = load_config(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
    if not cfg.get("stop_check_enabled", True):
        return 0
    problems = evaluate(msg)
    if not problems:
        return 0
    today = cfg.get("legal_as_of") or time.strftime("%d.%m.%Y")
    parts = []
    if "legal" in problems:
        parts.append(
            "У відповіді є правові/митні/податкові твердження без дати актуальності. Додай коротку примітку: «станом на %s» "
            "+ що ставки, терміни й переліки варто звірити з первинним джерелом (zakon.rada.gov.ua, customs.gov.ua, dpss.gov.ua) "
            "або з брокером/юристом. Не знаю напевно — так і скажи." % today)
    if "cost" in problems:
        parts.append(
            "У відповіді є розрахунок собівартості/ціни, але не названо припущення. Додай рядок із вихідними: курс, "
            "ставка мита, ПДВ (чи платник), логістика, % браку/усадки. Без них цифри не відтворити.")
    out = {"decision": "block",
           "reason": "textile-expert (перевірка перед завершенням). " + " ".join(parts) +
                     " Додай лише примітку, не переписуй відповідь."}
    sys.stdout.write(json.dumps(out))
    return 0


def main():
    try:
        return hook_main()
    except Exception:
        if os.environ.get("TEXTILE_EXPERT_DEBUG"):
            traceback.print_exc()
        return 0  # fail-open


if __name__ == "__main__":
    sys.exit(main())
