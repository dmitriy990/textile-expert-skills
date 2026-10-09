#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
textile-expert / scripts/landed_cost.py — калькулятор повної собівартості імпорту (landed cost) і ціни.

Локально, офлайн, лише stdlib. Нічого не пише на диск і не логує.
Результат — оцінка за вашими вхідними; ставки мита/ПДВ/курс беріть у брокера/бухгалтера на дату ввезення.

Приклад:
  python3 -I landed_cost.py --goods 2.8 --qty 5000 --freight 650 --fx 41.5 --duty-pct 10 \
      --broker-uah 9000 --inland-uah 6500 --loss-pct 3 --margin-pct 35 --vat-payer no

Формули:
  митна вартість       = товар + доставка до кордону України (у валюті) × курс
  мито                 = митна вартість × ставка
  ПДВ на ввезенні      = (митна вартість + мито) × 20 %  (витрата, якщо ви НЕ платник ПДВ)
  собівартість         = митна вартість + мито + (ПДВ, якщо не платник) + брокер + доставка складом
                         + банк/конвертація + контроль якості
  продається           = qty × (1 − втрати %)          (усадка, брак, залишки)
  ціна без ПДВ         = собівартість / одиницю / (1 − маржа %)       (маржа — від виручки)
  ціна роздрібна       = ціна без ПДВ × (1 + ПДВ), якщо ви платник ПДВ
"""
from __future__ import print_function

import argparse
import json
import math
import sys


def yes(v):
    return str(v).strip().lower() in ("y", "yes", "так", "true", "1", "да")


def num(lo=None, hi=None):
    """argparse-тип: скінченне число в межах [lo, hi]."""
    def conv(s):
        try:
            v = float(s)
        except ValueError:
            raise argparse.ArgumentTypeError("не число: %r" % s)
        if not math.isfinite(v):
            raise argparse.ArgumentTypeError("значення має бути скінченним числом: %r" % s)
        if lo is not None and v < lo:
            raise argparse.ArgumentTypeError("значення %g менше допустимого %g" % (v, lo))
        if hi is not None and v > hi:
            raise argparse.ArgumentTypeError("значення %g більше допустимого %g" % (v, hi))
        return v
    return conv


def yn(s):
    v = str(s).strip().lower()
    if v in ("y", "yes", "так", "true", "1", "да"):
        return "yes"
    if v in ("n", "no", "ні", "false", "0", "нет"):
        return "no"
    raise argparse.ArgumentTypeError("очікується yes або no, отримано %r" % s)


def compute(a):
    goods_cur = a.goods * a.qty
    customs_cur = goods_cur + a.freight
    customs_uah = customs_cur * a.fx
    duty = customs_uah * a.duty_pct / 100.0
    import_vat = (customs_uah + duty) * a.vat_pct / 100.0
    bank = goods_cur * a.fx * a.bank_pct / 100.0
    vat_payer = yes(a.vat_payer)
    vat_cost = 0.0 if vat_payer else import_vat
    total = customs_uah + duty + vat_cost + a.broker_uah + a.inland_uah + bank + a.qc_uah
    sellable = a.qty * (1 - a.loss_pct / 100.0)
    unit_cost = total / sellable
    r = {
        "goods_total_cur": goods_cur,
        "customs_value_uah": customs_uah,
        "duty_uah": duty,
        "import_vat_uah": import_vat,
        "import_vat_in_cost": not vat_payer,
        "bank_fee_uah": bank,
        "broker_uah": a.broker_uah,
        "inland_uah": a.inland_uah,
        "qc_uah": a.qc_uah,
        "total_cost_uah": total,
        "sellable_qty": sellable,
        "unit_cost_uah": unit_cost,
    }
    if a.margin_pct is not None:
        net = unit_cost / (1 - a.margin_pct / 100.0)
        r["target_margin_pct"] = a.margin_pct
        r["price_net_uah"] = net
        r["markup_pct"] = (net / unit_cost - 1) * 100.0
        r["price_retail_uah"] = net * (1 + a.vat_pct / 100.0) if vat_payer else net
    if a.retail_price is not None:
        net_p = a.retail_price / (1 + a.vat_pct / 100.0) if vat_payer else a.retail_price
        r["given_retail_price_uah"] = a.retail_price
        r["given_net_price_uah"] = net_p
        r["achieved_margin_pct"] = (net_p - unit_cost) / net_p * 100.0 if net_p else None
    return r


def report(a, r):
    cur = a.currency
    lines = [
        "Вхідні: товар %g %s × %g = %.2f %s; доставка до кордону %.2f %s; курс %g; мито %g %%; ПДВ %g %%; платник ПДВ: %s"
        % (a.goods, cur, a.qty, r["goods_total_cur"], cur, a.freight, cur, a.fx, a.duty_pct, a.vat_pct,
           "так" if yes(a.vat_payer) else "ні"),
        "Втрати/усадка/брак: %g %%; банк/конвертація: %g %%" % (a.loss_pct, a.bank_pct),
        "",
        "Митна вартість:        %12.2f грн" % r["customs_value_uah"],
        "Мито:                  %12.2f грн" % r["duty_uah"],
        "ПДВ на ввезенні:       %12.2f грн  (%s)" % (
            r["import_vat_uah"], "витрата" if r["import_vat_in_cost"] else "податковий кредит, не в собівартості"),
        "Брокер:                %12.2f грн" % r["broker_uah"],
        "Доставка по Україні:   %12.2f грн" % r["inland_uah"],
        "Банк/конвертація:      %12.2f грн" % r["bank_fee_uah"],
        "Контроль якості:       %12.2f грн" % r["qc_uah"],
        "-" * 44,
        "Повна собівартість:    %12.2f грн" % r["total_cost_uah"],
        "Придатно до продажу:   %12.2f од." % r["sellable_qty"],
        "Собівартість 1 од.:    %12.2f грн" % r["unit_cost_uah"],
    ]
    if "price_net_uah" in r:
        lines += [
            "",
            "Цільова маржа %g %% від виручки → ціна без ПДВ: %.2f грн (націнка %.1f %%)" % (
                r["target_margin_pct"], r["price_net_uah"], r["markup_pct"]),
            "Роздрібна ціна для покупця: %.2f грн" % r["price_retail_uah"],
        ]
    if "achieved_margin_pct" in r and r["achieved_margin_pct"] is not None:
        lines += ["", "При ціні %.2f грн (без ПДВ %.2f) валова маржа: %.1f %%" % (
            r["given_retail_price_uah"], r["given_net_price_uah"], r["achieved_margin_pct"])]
    lines += ["", "Це оцінка за вашими вхідними, не митна консультація: ставки, курс НБУ і режим ПДВ підтвердіть у брокера/бухгалтера."]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Landed cost і ціна для імпортного текстилю")
    ap.add_argument("--goods", type=num(0, 1e9), required=True, help="ціна товару за одиницю у валюті закупівлі")
    ap.add_argument("--qty", type=num(0, 1e12), required=True, help="кількість одиниць (м, шт, кг)")
    ap.add_argument("--currency", default="USD", help="валюта закупівлі (мітка)")
    ap.add_argument("--fx", type=num(0, 1e6), required=True, help="курс грн за 1 одиницю валюти (митний — курс НБУ на дату декларації)")
    ap.add_argument("--freight", type=num(0, 1e12), default=0.0, help="доставка+страхування до кордону України, у валюті закупівлі, усього")
    ap.add_argument("--duty-pct", type=num(0, 500), default=0.0, help="ставка мита, %% (беріть у брокера)")
    ap.add_argument("--vat-pct", type=num(0, 100), default=20.0)
    ap.add_argument("--vat-payer", type=yn, default="no", help="чи ви платник ПДВ: yes/no (впливає на включення ввізного ПДВ у собівартість)")
    ap.add_argument("--broker-uah", type=num(0, 1e12), default=0.0)
    ap.add_argument("--inland-uah", type=num(0, 1e12), default=0.0, help="доставка від кордону/порту до складу")
    ap.add_argument("--bank-pct", type=num(0, 100), default=1.0, help="комісії банку та конвертації, %% від суми платежу")
    ap.add_argument("--qc-uah", type=num(0, 1e12), default=0.0, help="інспекція/лабораторія")
    ap.add_argument("--loss-pct", type=num(0, 99.99), default=3.0, help="втрати: усадка, брак, залишки, %%")
    ap.add_argument("--margin-pct", type=num(0, 99.99), default=None, help="цільова валова маржа від виручки, %%")
    ap.add_argument("--retail-price", type=num(0, 1e12), default=None, help="перевірити маржу при заданій роздрібній ціні, грн")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.qty <= 0 or a.fx <= 0:
        ap.error("qty і fx мають бути > 0")
    if a.retail_price is not None and a.retail_price <= 0:
        ap.error("retail-price має бути > 0")

    r = compute(a)
    print(json.dumps(r, ensure_ascii=False, indent=2, allow_nan=False) if a.json else report(a, r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
