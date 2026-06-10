#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Долгоживущий процесс для GitHub Actions:
  • периодические авто-проверки (checkege ~15 мин, gia.orb.ru ~5 мин) — через подпроцессы;
  • слушает Telegram: на команду /check отвечает кратким сводом по всем предметам
    (ЕГЭ + ОГЭ), баллы — под спойлером.

Запуск:  python3 bot_serve.py --deadline <epoch>   (без deadline — ~5,5 ч)
"""
import argparse
import html
import json
import os
import subprocess
import sys
import time

import requests
import checker

HERE = os.path.dirname(os.path.abspath(__file__))
EGE_INTERVAL = int(os.environ.get("EGE_INTERVAL_SEC", "900"))  # 15 мин
API = "https://api.telegram.org/bot{}/{}"


def tg(method, **kw):
    tok = TOKEN
    return requests.post(API.format(tok, method), timeout=kw.pop("_timeout", 25),
                         verify=checker.build_ca_bundle(), **kw).json()


def tg_send(chat_id, text_html):
    try:
        tg("sendMessage", json={"chat_id": chat_id, "text": text_html,
                                "parse_mode": "HTML", "disable_web_page_preview": True})
    except Exception as e:
        checker.log(f"[serve] не смог отправить: {e}")


def drain_backlog():
    """Пропустить старые апдейты — отвечаем только на новые /check."""
    try:
        r = requests.get(API.format(TOKEN, "getUpdates"), params={"timeout": 0},
                         timeout=20, verify=checker.build_ca_bundle()).json()
        ups = r.get("result", [])
        return ups[-1]["update_id"] + 1 if ups else 0
    except Exception:
        return 0


def get_updates(offset, timeout=30):
    try:
        r = requests.get(API.format(TOKEN, "getUpdates"),
                         params={"offset": offset, "timeout": timeout},
                         timeout=timeout + 10, verify=checker.build_ca_bundle()).json()
        return r.get("result", [])
    except Exception as e:
        checker.log(f"[serve] getUpdates: {e}")
        time.sleep(5)
        return []


# нас интересуют только эти предметы ЕГЭ
WANT = ("литератур", "русск", "математ")


def build_summary(cfg):
    icon = {"scored": "✅", "hidden": "🔒", "none": "⏳"}
    try:
        rows = [r for r in checker.fetch_rows(cfg) if any(w in r[1].lower() for w in WANT)]
    except Exception as e:
        return f"Не удалось проверить: {html.escape(str(e))}"
    if not rows:
        return "Результатов пока нет (Литература / Русский язык / Математика)."
    lines = ["<b>Результаты ЕГЭ:</b>"]
    for _id, subj, date, st, txt in rows:
        if st == "scored":
            lines.append(f"{icon[st]} {html.escape(subj)}: <tg-spoiler>{html.escape(txt)}</tg-spoiler>")
        else:
            lines.append(f"{icon[st]} {html.escape(subj)}: {html.escape(txt)}")
    return "\n".join(lines)


def is_check_command(update):
    msg = update.get("message") or update.get("channel_post") or {}
    text = (msg.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0] if text else ""
    chat_id = (msg.get("chat") or {}).get("id")
    return cmd == "/check", chat_id


def commit_state():
    """Зафиксировать state.json/orb_state.json в репозитории, если изменились."""
    try:
        changed = subprocess.run(["git", "diff", "--quiet", "--", "state.json"],
                                 cwd=HERE).returncode != 0
        if changed:
            subprocess.run(["git", "commit", "-m", "обновление состояния", "--", "state.json"],
                           cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "push"], cwd=HERE,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        checker.log(f"[serve] commit: {e}")


def run(script):
    subprocess.run([sys.executable, os.path.join(HERE, script), "--once"], cwd=HERE)
    commit_state()


def main():
    global TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--deadline", type=int, default=0)
    args = ap.parse_args()
    deadline = args.deadline or int(time.time() + 20100)

    cfg = checker.load_config()
    TOKEN = cfg.get("notify", {}).get("telegram_bot_token", "")
    allowed_chat = str(cfg.get("notify", {}).get("telegram_chat_id", ""))

    checker.log(f"[serve] старт. /check {'включён' if TOKEN else 'выключен'}; авто-ЕГЭ/{EGE_INTERVAL}с")

    offset = drain_backlog() if TOKEN else 0
    last_ege = 0.0

    while time.time() < deadline:
        now = time.time()
        if now - last_ege >= EGE_INTERVAL:
            run("checker.py")          # авто-проверка ЕГЭ (~15 мин), уведомляет о новых результатах
            last_ege = now

        if not TOKEN:
            time.sleep(30)
            continue

        for u in get_updates(offset, timeout=30):
            offset = u["update_id"] + 1
            ischeck, chat_id = is_check_command(u)
            if ischeck and (not allowed_chat or str(chat_id) == allowed_chat):
                checker.log(f"[serve] /check от {chat_id}")
                tg_send(chat_id, "⏳ Проверяю результаты ЕГЭ…")
                tg_send(chat_id, build_summary(cfg))


TOKEN = ""

if __name__ == "__main__":
    main()
