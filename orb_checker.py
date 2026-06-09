#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка результатов на gia.orb.ru (региональный портал ГИА, Оренбургская обл.).
Логинится фамилией + кодом, читает счётчик «Найденные результаты» и список,
и уведомляет в Telegram, когда появляется НОВЫЙ результат. Капчи здесь нет.

Запуск:  python3 orb_checker.py --once
Данные берутся из переменных окружения (ORB_SURNAME|EGE_SURNAME, ORB_PASSWORD,
TELEGRAM_*) или из config.json (surname, orb_password, notify).
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys

import requests
import checker  # переиспользуем notify / build_ca_bundle / log / UA / CONFIG_PATH

ORB = "https://gia.orb.ru"
HERE = os.path.dirname(os.path.abspath(__file__))
ORB_STATE = os.path.join(HERE, "orb_state.json")


def load_cfg():
    surname = os.environ.get("ORB_SURNAME") or os.environ.get("EGE_SURNAME") or ""
    password = os.environ.get("ORB_PASSWORD") or ""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if os.path.exists(checker.CONFIG_PATH):
        c = json.load(open(checker.CONFIG_PATH, encoding="utf-8"))
        surname = surname or c.get("surname", "")
        password = password or c.get("orb_password", "") or c.get("doc_number", "")
        n = c.get("notify", {})
        tok = tok or n.get("telegram_bot_token", "")
        chat = chat or n.get("telegram_chat_id", "")
    cfg = {"notify": {"telegram_bot_token": tok, "telegram_chat_id": chat,
                      "macos_notification": True, "sound": True}}
    return surname, password, cfg


def login(session, surname, password):
    V = checker.build_ca_bundle()
    r = session.get(f"{ORB}/users/sign_in", verify=V, timeout=30)
    m = re.search(r'name="authenticity_token"\s+value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("не найден CSRF-токен на странице входа")
    session.post(f"{ORB}/users/sign_in", verify=V, timeout=30, data={
        "authenticity_token": html.unescape(m.group(1)),
        "user[first_name]": surname,
        "user[password]": password,
        "commit": "Войти",
    })
    page = session.get(f"{ORB}/", verify=V, timeout=30).text
    if "sign_out" not in page and "Выйти" not in page:
        raise RuntimeError("вход не выполнен — проверьте фамилию и код")
    return page


def parse(page):
    m = re.search(r"Найденные результаты\s*(?:<[^>]+>\s*)*(\d+)", page)
    count = int(m.group(1)) if m else None
    results = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(c))).strip() for c in cells]
        if len(cells) >= 5 and re.match(r"\d{4}-\d{2}-\d{2}$", cells[0]):
            results.append({"date": cells[0], "subject": cells[1],
                            "score": cells[3], "status": cells[4]})
    if count is None:
        count = len(results)
    return count, results


def key_hash(r):
    # в state храним только хэш (без предмета/балла) — чтобы публичный репозиторий не светил результаты
    return hashlib.md5(f"{r['date']}|{r['subject']}".encode("utf-8")).hexdigest()[:10]


def load_state():
    if os.path.exists(ORB_STATE):
        try:
            return json.load(open(ORB_STATE, encoding="utf-8"))
        except Exception:
            pass
    return {"seen": [], "count": None}


def save_state(state):
    try:
        json.dump(state, open(ORB_STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        checker.log(f"[orb] не удалось сохранить orb_state.json: {e}")


def main():
    ap = argparse.ArgumentParser(description="Проверка результатов на gia.orb.ru")
    ap.add_argument("--once", action="store_true", help="одна проверка и выход")
    ap.parse_args()
    surname, password, cfg = load_cfg()
    if not surname or not password:
        sys.exit("Нет данных для gia.orb.ru (ORB_SURNAME/ORB_PASSWORD или config.json).")

    session = requests.Session()
    session.headers.update({"User-Agent": checker.UA})
    try:
        page = login(session, surname, password)
    except Exception as e:
        checker.log(f"[orb] ошибка входа: {e}")
        return

    count, results = parse(page)
    checker.log(f"[orb] Найденные результаты: {count}")
    for r in results:
        checker.log(f"[orb]   {r['date']} {r['subject']}: {r['score']} ({r['status']})")

    state = load_state()
    seen = set(state.get("seen", []))
    new = [r for r in results if key_hash(r) not in seen]

    state["seen"] = sorted({key_hash(r) for r in results})
    state["count"] = count
    save_state(state)

    if new:
        plain = "\n".join(f"📊 {r['subject']}: {r['score']} балл(ов)" for r in new)
        tg = "\n".join(f"📊 {html.escape(r['subject'])}: "
                       f"<tg-spoiler>{html.escape(r['score'])} балл(ов)</tg-spoiler>" for r in new)
        checker.log("[orb] 🔔 НОВЫЙ РЕЗУЛЬТАТ: " + "; ".join(f"{r['subject']}:{r['score']}" for r in new))
        checker.notify(cfg, f"gia.orb.ru: новый результат (найдено {count})",
                       plain, loud=True, tg_html=tg)


if __name__ == "__main__":
    main()
