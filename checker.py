#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот проверки результатов ЕГЭ на checkege.rustest.ru.

Что делает:
  • сам заполняет данные участника из config.json;
  • получает капчу и решает её через CapMonster Cloud;
  • логинится в сервис ознакомления с результатами;
  • раз в N минут проверяет, появились ли результаты, и пишет об этом;
  • громко уведомляет (звук / уведомление macOS / Telegram), когда результаты опубликованы.

Запуск:
    python3 checker.py            # бесконечный цикл (раз в 5 минут по умолчанию)
    python3 checker.py --once     # одна проверка и выход
    python3 checker.py --balance  # показать баланс CapMonster и выйти

Перед первым запуском заполните config.json (см. config.example.json).
"""

import argparse
import json
import os
import re
import sys
import time
import html
import random
import hashlib
import tempfile
import subprocess
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Нужна библиотека requests. Установите:  pip3 install requests")

try:
    import certifi
    _CERTIFI = certifi.where()
except ImportError:
    _CERTIFI = None

# ──────────────────────────────────────────────────────────────────────────
# Константы сервиса (получены из разбора start.js / exams.js на checkege.rustest.ru)
# ──────────────────────────────────────────────────────────────────────────
BASE = "https://checkege.rustest.ru"
URL_CAPTCHA = BASE + "/api/captcha"
URL_LOGIN = BASE + "/api/participant/login"
URL_EXAM = BASE + "/api/exam"

CAPMONSTER = "https://api.capmonster.cloud"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
REGIONS_PATH = os.path.join(HERE, "regions.json")
STATE_PATH = os.path.join(HERE, "state.json")

# checkege.rustest.ru не присылает промежуточный сертификат GlobalSign, поэтому
# Python/OpenSSL не может достроить цепочку до корня (в отличие от curl, который
# подтягивает его по AIA). Кладём промежуточный сертификат рядом и собираем
# объединённый CA-бандл = certifi + промежуточный.
INTERMEDIATE_PEM = os.path.join(HERE, "globalsign-intermediate.pem")
INTERMEDIATE_AIA = "http://secure.globalsign.com/cacert/gsgccr3dvtlsca2020.crt"
CA_BUNDLE_PATH = os.path.join(HERE, "ca_bundle.pem")
_CA_BUNDLE = None


# ──────────────────────────────────────────────────────────────────────────
# Утилиты
# ──────────────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def simplify_fio(surname, name, patr):
    """Точная копия transformFio.simplify из start.js.
    Склеить ФИО → нижний регистр → убрать всё, кроме букв → ё→е, й→и."""
    t = (surname or "") + (name or "") + (patr or "")
    t = t.lower()
    t = re.sub(r"[^a-zA-Zа-яА-ЯЁё]+", "", t)
    t = t.replace("ё", "е").replace("й", "и")
    return t


def fio_hash(surname, name, patr):
    """md5(utf8(simplify(...))) — проверено: совпадает с реальным JS сайта."""
    return hashlib.md5(simplify_fio(surname, name, patr).encode("utf-8")).hexdigest()


def transform_doc_number(num):
    """transformPassNum из start.js: дополнить номер документа нулями слева до 12 цифр."""
    digits = re.sub(r"\D+", "", str(num))
    return digits.zfill(12)


def load_config():
    # 1) база — из config.json, если есть (для локального запуска)
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)

    # 2) поверх — переменные окружения (для бесплатного хостинга через секреты)
    env_map = {
        "surname": "EGE_SURNAME", "name": "EGE_NAME", "patronymic": "EGE_PATRONYMIC",
        "reg_code": "EGE_REG_CODE", "doc_number": "EGE_DOC_NUMBER", "region": "EGE_REGION",
        "capmonster_key": "CAPMONSTER_KEY",
    }
    for key, env in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg.setdefault("notify", {})
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["notify"]["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["notify"]["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("EGE_CAPTCHA_SOLVER"):
        cfg["captcha_solver"] = os.environ["EGE_CAPTCHA_SOLVER"]
    if os.environ.get("EGE_INTERVAL"):
        cfg["interval_seconds"] = int(os.environ["EGE_INTERVAL"])

    if not cfg.get("surname"):
        sys.exit("Нет данных участника. Заполните config.json (скопируйте из config.example.json) "
                 "или задайте переменные окружения EGE_SURNAME, EGE_NAME, ...")

    # --- проверки ---
    for key in ("surname", "name"):
        if not str(cfg.get(key, "")).strip():
            sys.exit(f"В config.json не заполнено поле «{key}».")

    code = re.sub(r"\D+", "", str(cfg.get("reg_code", "") or ""))
    doc = re.sub(r"\D+", "", str(cfg.get("doc_number", "") or ""))
    if bool(code) == bool(doc):
        sys.exit("Заполните РОВНО ОДНО из полей: reg_code (код регистрации) ИЛИ doc_number (номер документа).")

    cfg.setdefault("captcha_solver", "ocr")
    if cfg["captcha_solver"] == "capmonster" and not str(cfg.get("capmonster_key", "")).strip():
        sys.exit("captcha_solver=capmonster, но не задан capmonster_key.")

    cfg["_code"] = code
    cfg["_doc"] = doc
    cfg["_region_id"] = resolve_region(cfg.get("region", ""))
    cfg.setdefault("interval_seconds", 300)
    cfg.setdefault("notify", {})
    return cfg


def resolve_region(value):
    """Принимает либо номер региона, либо его название (см. regions.json)."""
    with open(REGIONS_PATH, encoding="utf-8") as f:
        regions = json.load(f)
    by_id = {r["id"]: r["name"] for r in regions}

    s = str(value).strip()
    if s.isdigit():
        rid = int(s)
        if rid not in by_id:
            sys.exit(f"Регион с номером {rid} не найден в regions.json.")
        return rid

    # поиск по названию (без учёта регистра, по подстроке)
    low = s.lower()
    exact = [r for r in regions if r["name"].lower() == low]
    part = [r for r in regions if low and low in r["name"].lower()]
    hits = exact or part
    if len(hits) == 1:
        log(f"Регион: {hits[0]['name']} (id={hits[0]['id']})")
        return hits[0]["id"]
    if not hits:
        sys.exit(f"Регион «{value}» не найден. Откройте regions.json и впишите точное название или номер.")
    sys.exit("Под «{}» подходит несколько регионов:\n  {}\nУточните название или укажите номер.".format(
        value, "\n  ".join(f'{h["id"]}: {h["name"]}' for h in hits)))


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"results_seen": []}


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Не удалось сохранить state.json: {e}")


# ──────────────────────────────────────────────────────────────────────────
# Решение капчи
# ──────────────────────────────────────────────────────────────────────────
# Капча сайта — 6 синих цифр на светлом зигзаге. Её надёжно (проверено) читает
# бесплатный Tesseract, если выделить синий канал. CapMonster оставлен запасным.

def ocr_solve(image_b64):
    """Бесплатное распознавание капчи через Tesseract. Возвращает строку цифр или ''."""
    import io
    import base64 as _b64
    from PIL import Image  # ставится как pillow

    im = Image.open(io.BytesIO(_b64.b64decode(image_b64))).convert("RGB")
    w, h = im.size
    big = im.resize((w * 4, h * 4))
    px = big.load()
    W, H = big.size
    for y in range(H):
        for x in range(W):
            r, g, b = px[x, y]
            # синие цифры: B заметно больше R; фон (серый/белый) -> белым
            px[x, y] = (0, 0, 0) if (b - r > 35 and b > 80) else (255, 255, 255)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp = tf.name
    try:
        big.save(tmp)
        res = subprocess.run(
            ["tesseract", tmp, "stdout", "--psm", "7",
             "-c", "tessedit_char_whitelist=0123456789"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        return re.sub(r"\D+", "", res.stdout.decode("utf-8", "ignore"))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def capmonster_balance(key):
    r = requests.post(f"{CAPMONSTER}/getBalance", json={"clientKey": key},
                      timeout=30, verify=build_ca_bundle())
    return r.json()


def capmonster_solve(key, image_b64, timeout=120):
    """Решить картинку-капчу через CapMonster ImageToTextTask. Возвращает (text, task_id)."""
    bundle = build_ca_bundle()
    create = requests.post(f"{CAPMONSTER}/createTask", json={
        "clientKey": key,
        "task": {
            "type": "ImageToTextTask",
            "body": image_b64,   # чистый base64 без префикса data:
            # ВАЖНО: numeric НЕ ставим — с ним CapMonster теряет точность на этой капче.
            # Лишние буквы потом отбрасываем и принимаем только ровно 6 цифр.
        },
    }, timeout=30, verify=bundle).json()

    if create.get("errorId"):
        raise RuntimeError(f"CapMonster createTask: {create.get('errorCode')} / {create.get('errorDescription')}")
    task_id = create["taskId"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        res = requests.post(f"{CAPMONSTER}/getTaskResult", json={
            "clientKey": key, "taskId": task_id,
        }, timeout=30, verify=bundle).json()
        if res.get("errorId"):
            raise RuntimeError(f"CapMonster getTaskResult: {res.get('errorCode')} / {res.get('errorDescription')}")
        if res.get("status") == "ready":
            return (res["solution"]["text"] or "").strip(), task_id
    raise TimeoutError("CapMonster не успел решить капчу за отведённое время")


def solve_captcha(cfg, image_b64):
    """Диспетчер. Возвращает (text, meta). meta нужно для отчёта о неверной капче."""
    solver = cfg.get("captcha_solver", "ocr")
    key = cfg.get("capmonster_key", "")

    if solver == "ocr":
        try:
            text = ocr_solve(image_b64)
            if text:
                return text, {"solver": "ocr"}
            log("OCR не распознал цифры.")
        except Exception as e:
            log(f"OCR недоступен ({e}).")
        # запасной вариант — CapMonster, если задан ключ
        if key:
            log("Пробую CapMonster как запасной вариант.")
            text, task_id = capmonster_solve(key, image_b64)
            return text, {"solver": "capmonster", "task_id": task_id}
        return "", {"solver": "ocr"}

    # solver == "capmonster"
    text, task_id = capmonster_solve(key, image_b64)
    return text, {"solver": "capmonster", "task_id": task_id}


def report_bad_captcha(cfg, meta):
    """Сообщить CapMonster о неверном решении (для возврата средств). Для OCR — ничего."""
    if meta.get("solver") != "capmonster":
        return
    try:
        requests.post(f"{CAPMONSTER}/reportIncorrectImageCaptcha",
                      json={"clientKey": cfg.get("capmonster_key", ""), "taskId": meta["task_id"]},
                      timeout=20, verify=build_ca_bundle())
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────
# checkege.rustest.ru
# ──────────────────────────────────────────────────────────────────────────
def build_ca_bundle():
    """Собрать CA-бандл = certifi + промежуточный сертификат GlobalSign.
    Если промежуточного нет рядом — пробуем скачать по AIA. Если совсем не
    получилось — возвращаем certifi (или None)."""
    global _CA_BUNDLE
    if _CA_BUNDLE:
        return _CA_BUNDLE

    inter = None
    if os.path.exists(INTERMEDIATE_PEM):
        with open(INTERMEDIATE_PEM, encoding="utf-8") as f:
            inter = f.read()
    else:
        try:
            import ssl
            import urllib.request
            raw = urllib.request.urlopen(INTERMEDIATE_AIA, timeout=20).read()
            inter = raw.decode() if raw.lstrip().startswith(b"-----BEGIN") else ssl.DER_cert_to_PEM_cert(raw)
            with open(INTERMEDIATE_PEM, "w", encoding="utf-8") as f:
                f.write(inter)
            log("Промежуточный сертификат GlobalSign скачан по AIA.")
        except Exception as e:
            log(f"Не удалось получить промежуточный сертификат: {e}")

    if not _CERTIFI:
        _CA_BUNDLE = INTERMEDIATE_PEM if inter else True
        return _CA_BUNDLE
    try:
        with open(CA_BUNDLE_PATH, "w", encoding="utf-8") as out, open(_CERTIFI, encoding="utf-8") as base:
            out.write(base.read())
            if inter:
                out.write("\n" + inter + "\n")
        _CA_BUNDLE = CA_BUNDLE_PATH
    except Exception:
        _CA_BUNDLE = _CERTIFI
    return _CA_BUNDLE


def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE + "/",
        "Origin": BASE,
    })
    s.verify = build_ca_bundle()
    return s


def get_captcha(session):
    r = session.get(URL_CAPTCHA, timeout=30)
    r.raise_for_status()
    d = r.json()
    return d["Image"], d["Token"]


def try_login(session, cfg, captcha_text, token):
    """Отправить форму логина. Возвращает (status, body_text)."""
    payload = {
        "Hash": fio_hash(cfg["surname"], cfg["name"], cfg.get("patronymic", "")),
        "Code": cfg["_code"],
        "Document": transform_doc_number(cfg["_doc"]) if cfg["_doc"] else "",
        "Region": cfg["_region_id"],
        "AgereeCheck": "true",          # да, в API поле называется именно так (опечатка сайта)
        "Captcha": captcha_text,
        "Token": token,
        "reCaptureToken": captcha_text,
    }
    r = session.post(URL_LOGIN, data=payload, timeout=30)
    try:
        parsed = r.json()
        body = parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False)
    except Exception:
        body = r.text
    return r.status_code, str(body)


def fetch_exams(session):
    r = session.get(URL_EXAM, timeout=30)
    r.raise_for_status()
    return r.json()


# ──────────────────────────────────────────────────────────────────────────
# Уведомления
# ──────────────────────────────────────────────────────────────────────────
def notify(cfg, title, message, loud=False, tg_html=None):
    """Уведомление. message — обычный текст (лог/macOS). Если задан tg_html —
    в Telegram уходит он (HTML), что позволяет прятать балл под спойлер."""
    n = cfg.get("notify", {})
    # macOS: всплывающее уведомление
    if n.get("macos_notification", True) and sys.platform == "darwin":
        try:
            t = title.replace('"', "'")
            m = message.replace('"', "'")
            subprocess.run(["osascript", "-e",
                            f'display notification "{m}" with title "{t}" sound name "Glass"'],
                           check=False)
        except Exception:
            pass
    # звук
    if loud and n.get("sound", True) and sys.platform == "darwin":
        try:
            for _ in range(3):
                subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)
        except Exception:
            pass
    # Telegram
    tok = n.get("telegram_bot_token", "")
    chat = n.get("telegram_chat_id", "")
    if tok and chat:
        if tg_html is not None:
            payload = {"chat_id": chat, "text": f"<b>{html.escape(title)}</b>\n{tg_html}",
                       "parse_mode": "HTML"}
        else:
            payload = {"chat_id": chat, "text": f"{title}\n{message}"}
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json=payload, timeout=20, verify=build_ca_bundle())
        except Exception as e:
            log(f"Telegram: {e}")


# ──────────────────────────────────────────────────────────────────────────
# Логика одной проверки
# ──────────────────────────────────────────────────────────────────────────
def describe_exams(data):
    """Из ответа /api/exam собрать список (exam_id, subject, date, state, text).
    state: 'scored' — есть балл; 'hidden' — «Результат скрыт» (пришёл, на утверждении в ГЭК);
           'none' — результата ещё нет."""
    exams = (data.get("Result") or {}).get("Exams") or []
    rows = []
    for e in exams:
        exam_id = e.get("ExamId")
        subject = e.get("Subject") or e.get("OralSubject") or "—"
        date = (e.get("ExamDate") or "")[:10]
        if e.get("IsHidden"):
            rows.append((exam_id, subject, date, "hidden", "Результат скрыт (на утверждении в ГЭК)"))
        elif e.get("HasResult"):
            if e.get("IsComposition"):
                txt = "зачёт" if e.get("Mark5") == 5 else "незачёт"
            else:
                minmark = e.get("MinMark")
                txt = f"{e.get('TestMark')} балл(ов)" + (f" (мин. {minmark})" if minmark is not None else "")
            rows.append((exam_id, subject, date, "scored", txt))
        else:
            status = e.get("StatusName") or ("Нет результата" if e.get("Status") == 0 else "ожидается")
            rows.append((exam_id, subject, date, "none", status))
    return rows


def fetch_rows(cfg, max_captcha_tries=8):
    """Войти и вернуть текущие строки экзаменов (для команды /check). Не уведомляет.
    Возвращает список rows; [] — если «участник не найден». Бросает при ошибке."""
    session = new_session()
    for _ in range(max_captcha_tries):
        image_b64, token = get_captcha(session)
        raw, meta = solve_captcha(cfg, image_b64)
        text = re.sub(r"\D+", "", raw or "")
        if len(text) != 6:
            report_bad_captcha(cfg, meta)
            continue
        status, body = try_login(session, cfg, text, token)
        if 200 <= status < 300:
            return describe_exams(fetch_exams(session))
        if status == 401:
            return []   # участник не найден / результатов пока нет
        if status == 400 and "картинк" in body:
            continue
        raise RuntimeError(f"вход не выполнен ({status})")
    raise RuntimeError("капча не решилась")


def one_cycle(session, cfg, state, max_captcha_tries=8):
    """Один полный проход: капча → логин → (если успех) результаты. Возвращает True, если нужно остановить бота."""
    for attempt in range(1, max_captcha_tries + 1):
        try:
            image_b64, token = get_captcha(session)
        except Exception as e:
            log(f"Не удалось получить капчу: {e}")
            return False

        try:
            raw_text, meta = solve_captcha(cfg, image_b64)
        except Exception as e:
            log(f"Ошибка решения капчи: {e}")
            # один раз предупредим в Telegram, чтобы простой не остался незамеченным
            if not state.get("captcha_error_notified"):
                hint = (" Похоже, закончился баланс CapMonster — пополни на capmonster.cloud."
                        if "ZERO_BALANCE" in str(e) else "")
                notify(cfg, "⚠️ ЕГЭ-бот не может проверять",
                       f"Капча не решается: {e}.{hint}", loud=True)
                state["captcha_error_notified"] = True
                save_state(state)
            return False

        captcha_text = re.sub(r"\D+", "", raw_text or "")   # капча сайта — ровно 6 цифр
        if len(captcha_text) != 6:
            log(f"Капча распознана как «{raw_text}» (не 6 цифр), беру новую (попытка {attempt}/{max_captcha_tries}).")
            report_bad_captcha(cfg, meta)
            continue

        # капча решилась — если раньше был сбой, сообщим о восстановлении
        if state.get("captcha_error_notified"):
            state["captcha_error_notified"] = False
            save_state(state)
            notify(cfg, "✅ ЕГЭ-бот снова работает", "Капча решается, проверки возобновлены.")
        log(f"Капча решена: {captcha_text} [{meta['solver']}] (попытка {attempt}/{max_captcha_tries})")
        status, body = try_login(session, cfg, captcha_text, token)

        # ── разбор ответа ──
        if 200 <= status < 300:   # 200/204 — участник найден, вход выполнен
            log("Вход выполнен — участник найден. Загружаю список экзаменов…")
            handle_results(session, cfg, state)
            return False

        if status == 400 and "картинк" in body:
            log("Капча не подошла, пробую заново.")
            report_bad_captcha(cfg, meta)
            continue

        if status == 400 and "данных" in body:
            log("ОШИБКА ДАННЫХ: сервис не принял введённые данные. Проверьте ФИО / код / номер документа / регион в config.json.")
            notify(cfg, "ЕГЭ-бот: ошибка данных", "Проверьте config.json — данные не приняты сервисом.", loud=True)
            return True  # остановиться, ждать бессмысленно

        if status == 401:  # "Участник не найден"
            log("Результатов пока нет («участник не найден»). Если данные верны — просто жду публикации.")
            # сбросим память о результатах, чтобы при появлении снова громко уведомить
            return False

        if status == 500:
            log("Сервер вернул 500 (внутренняя ошибка). Повторю в следующий раз.")
            return False

        log(f"Неожиданный ответ {status}: {body}")
        return False

    log("Капча не решилась за несколько попыток, повторю в следующий раз.")
    return False


def handle_results(session, cfg, state):
    try:
        data = fetch_exams(session)
    except Exception as e:
        log(f"Не удалось загрузить /api/exam: {e}")
        notify(cfg, "ЕГЭ-бот", "Вход выполнен, но список экзаменов не загрузился.", loud=True)
        return

    rows = describe_exams(data)
    if not rows:
        log("Вход выполнен, но список экзаменов пуст.")
        return

    icon = {"scored": "✅", "hidden": "🔒", "none": "⏳"}
    print("─" * 60)
    for _id, subject, date, st, txt in rows:
        print(f"  {icon.get(st, '⏳')} {date:<10} {subject:<35} {txt}")
    print("─" * 60)

    # В state храним только ID экзаменов (числа), без баллов — чтобы публичный
    # репозиторий не раскрывал результаты. Баллы уходят только в уведомление.
    scored_seen = set(state.get("results_seen", []))
    hidden_seen = set(state.get("hidden_seen", []))

    new_msgs = []   # для лога (балл виден)
    new_html = []   # для Telegram (балл под спойлером)
    for rid, subject, date, st, txt in rows:
        if rid is None:
            continue
        if st == "scored" and rid not in scored_seen:
            new_msgs.append(f"🎉 {subject}: {txt}")
            # сам балл прячем под спойлер — в группе будет заблюрен, тап открывает
            new_html.append(f"🎉 {html.escape(subject)}: <tg-spoiler>{html.escape(txt)}</tg-spoiler>")
        elif st == "hidden" and rid not in hidden_seen:
            msg = f"🔒 {subject}: результат пришёл, на утверждении в ГЭК — скоро балл"
            new_msgs.append(msg)
            new_html.append(f"🔒 {html.escape(subject)}: результат пришёл, на утверждении в ГЭК — скоро балл")

    # обновляем «виденное» (после сравнения)
    state["results_seen"] = sorted({rid for rid, _, _, st, _ in rows if st == "scored" and rid is not None})
    state["hidden_seen"] = sorted({rid for rid, _, _, st, _ in rows if st == "hidden" and rid is not None})
    save_state(state)

    if new_msgs:
        log("🔔 ИЗМЕНЕНИЯ: " + "; ".join(new_msgs))
        notify(cfg, "ЕГЭ: обновление результатов", "\n".join(new_msgs),
               loud=True, tg_html="\n".join(new_html))
    else:
        cur = "; ".join(f"{s}={st}" for _id, s, d, st, t in rows if st != "none")
        log("Без новых изменений" + (f" ({cur})" if cur else ""))


# ──────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Бот проверки результатов ЕГЭ (checkege.rustest.ru)")
    ap.add_argument("--once", action="store_true", help="одна проверка и выход")
    ap.add_argument("--balance", action="store_true", help="показать баланс CapMonster и выйти")
    args = ap.parse_args()

    cfg = load_config()

    if args.balance:
        print(json.dumps(capmonster_balance(cfg.get("capmonster_key", "")), ensure_ascii=False, indent=2))
        return

    log(f"Решение капчи: {cfg['captcha_solver']}" + (" (бесплатно, Tesseract)" if cfg["captcha_solver"] == "ocr" else ""))
    # проверка баланса CapMonster при старте (только если он используется)
    if cfg["captcha_solver"] == "capmonster":
        try:
            bal = capmonster_balance(cfg["capmonster_key"])
            if bal.get("errorId"):
                sys.exit(f"CapMonster: неверный ключ или ошибка — {bal.get('errorCode')} / {bal.get('errorDescription')}")
            log(f"CapMonster: ключ принят, баланс {bal.get('balance')}")
        except SystemExit:
            raise
        except Exception as e:
            log(f"Не удалось проверить баланс CapMonster: {e}")

    fio = f'{cfg["surname"]} {cfg["name"]} {cfg.get("patronymic","")}'.strip()
    ident = f'код {cfg["_code"]}' if cfg["_code"] else f'документ {cfg["_doc"]}'
    log(f"Участник: {fio} | {ident} | регион id={cfg['_region_id']}")

    interval = int(cfg["interval_seconds"])
    state = load_state()
    session = new_session()

    if args.once:
        one_cycle(session, cfg, state)
        return

    log(f"Старт. Проверяю каждые {interval} сек (~{interval//60} мин). Ctrl+C — остановить.")
    while True:
        try:
            stop = one_cycle(session, cfg, state)
            if stop:
                log("Останавливаюсь (нужно исправить config.json).")
                break
        except KeyboardInterrupt:
            log("Остановлено пользователем.")
            break
        except Exception as e:
            log(f"Непредвиденная ошибка цикла: {e}")
        # пауза с небольшим разбросом, чтобы не долбить сервис строго по таймеру
        sleep = interval + random.randint(0, 20)
        time.sleep(sleep)


if __name__ == "__main__":
    main()
