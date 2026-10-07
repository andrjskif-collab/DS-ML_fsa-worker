#!/usr/bin/env python3
"""Еженедельная сверка статусов ДС с реестром ФСА (pub.fsa.gov.ru).

Работает на хостинге с российским IP (Bothost): ФСА не отвечает на зарубежные адреса.
Номера ДС берёт из Google-реестра через приёмник Apps Script и туда же пишет результат:
колонки «Статус ФСА» и «Проверено в ФСА» + ячейка с датой последней сверки.

Переменные окружения (или файл config.json рядом):
    RECEIVER_URL     — адрес приёмника (…/exec)
    RECEIVER_SECRET  — секрет приёмника (API_SECRET)
    CHECK_WEEKDAY    — день недели проверки, 0 = понедельник (по умолчанию 0)
    CHECK_HOUR       — час проверки по Москве (по умолчанию 10)

Запуск: python main.py           — работать по расписанию (сразу проверит, если давно не проверял)
        python main.py --once    — одна проверка и выход
        python main.py --diag    — диагностика: IP, доступ к ФСА, сырой ответ по одной ДС
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
STATE_FILE = DATA_DIR / "state.json"
MSK = dt.timezone(dt.timedelta(hours=3))

FSA = "https://pub.fsa.gov.ru"
# Учётка, под которой сайт pub.fsa.gov.ru сам ходит в свой API для анонимных посетителей
FSA_LOGIN = {"username": "anonymous", "password": "hrgesf7HDR67Bd"}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def log(*args):
    print(dt.datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def config():
    cfg = {}
    if (HERE / "config.json").exists():
        cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    return {
        "url": os.environ.get("RECEIVER_URL", cfg.get("url", "")),
        "secret": os.environ.get("RECEIVER_SECRET", cfg.get("secret", "")),
        "weekday": int(os.environ.get("CHECK_WEEKDAY", cfg.get("weekday", 0))),
        "hour": int(os.environ.get("CHECK_HOUR", cfg.get("hour", 10))),
    }


# ---------- HTTP ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def http(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")


def receiver(cfg, action, **params):
    """Запрос к приёмнику Apps Script (он отвечает редиректом 302 на страницу с результатом)."""
    body = json.dumps({"secret": cfg["secret"], "action": action, **params}).encode("utf-8")
    req = urllib.request.Request(cfg["url"], data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "curl/8.7.1"})
    try:
        with NO_REDIRECT.open(req, timeout=300) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        if err.code not in (301, 302, 303):
            raise
        with urllib.request.urlopen(urllib.request.Request(err.headers["Location"],
                                                           headers={"User-Agent": "curl/8.7.1"}), timeout=300) as resp:
            text = resp.read().decode("utf-8")
    result = json.loads(text)
    if not result.get("ok"):
        raise RuntimeError("Приёмник: " + result.get("error", text[:300]))
    return result


# ---------- ФСА ----------

class Fsa:
    def __init__(self):
        self.token = ""
        self.status_names = {}

    def login(self):
        _, headers, _ = http("POST", FSA + "/login", FSA_LOGIN)
        token = headers.get("Authorization") or headers.get("authorization") or ""
        if not token:
            raise RuntimeError("ФСА не выдал токен")
        self.token = token
        self.status_names = self._load_statuses()

    def _api(self, method, path, body=None):
        try:
            return json.loads(http(method, FSA + path, body, {"Authorization": self.token})[2])
        except urllib.error.HTTPError as err:
            if err.code == 401:  # токен истёк — перелогиниваемся один раз
                self.login()
                return json.loads(http(method, FSA + path, body, {"Authorization": self.token})[2])
            raise

    def _load_statuses(self):
        """Справочник статусов {id: название}. Формат справочника ищем гибко."""
        names = {}
        try:
            data = json.loads(http("GET", FSA + "/api/v1/rds/common/identifiers", None,
                                   {"Authorization": self.token})[2])
            node = data.get("status") or data.get("statuses") or {}
            items = node.values() if isinstance(node, dict) else node
            for item in items:
                if isinstance(item, dict) and "id" in item and "name" in item:
                    names[int(item["id"])] = item["name"]
        except Exception as err:  # справочник не критичен: покажем хотя бы номер статуса
            log("Справочник статусов недоступен:", err)
        return names

    def search(self, number):
        body = {
            "size": 10, "page": 0,
            "filter": {"columnsSearch": [{"name": "number", "search": number, "type": 9, "translated": False}]},
            "columnsSort": [{"column": "declDate", "sort": "DESC"}],
        }
        return self._api("POST", "/api/v1/rds/common/declarations/get", body)

    def status_of(self, number):
        """→ (статус текстом, найдено ли)."""
        items = self.search(number).get("items") or []
        key = norm(number)
        exact = [it for it in items if norm(it.get("number", "")) == key]
        if not exact:
            return "Не найдена в ФСА", False
        sid = exact[0].get("idStatus")
        name = self.status_names.get(int(sid)) if sid is not None else None
        return name or ("статус " + str(sid)), True


def norm(number):
    lookalike = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")
    return "".join(str(number).upper().split()).replace("№", "").translate(lookalike)


# ---------- Сверка ----------

def run_check(cfg):
    rows = receiver(cfg, "fsa_list")["rows"]
    log(f"Проверяю {len(rows)} ДС…")
    fsa = Fsa()
    fsa.login()
    results = []
    for r in rows:
        try:
            status, found = fsa.status_of(r["number"])
            results.append({"row": r["row"], "number": r["number"], "status": status, "found": found})
        except Exception as err:
            log("Ошибка по", r["number"], err)
            results.append({"row": r["row"], "number": r["number"], "error": str(err)[:200]})
        time.sleep(1.5)  # не нагружаем ФСА
    res = receiver(cfg, "fsa_update", results=results)
    log(res.get("message", ""))
    save_state({"last_check": dt.datetime.now(MSK).isoformat()})


def diag(cfg):
    try:
        log("Мой IP:", http("GET", "https://ipinfo.io/json")[2].replace("\n", " "))
    except Exception as err:
        log("ipinfo недоступен:", err)
    fsa = Fsa()
    try:
        fsa.login()
        log("ФСА: вход выполнен, статусов в справочнике:", len(fsa.status_names), fsa.status_names)
    except Exception as err:
        log("ФСА недоступен:", repr(err))
        return
    number = "ЕАЭС N RU Д-RU.РА08.В.78299/26"
    if cfg["url"]:
        try:
            rows = receiver(cfg, "fsa_list")["rows"]
            log("Приёмник: ДС в реестре:", len(rows))
            number = rows[-1]["number"] if rows else number
        except Exception as err:
            log("Приёмник недоступен:", err)
    raw = fsa.search(number)
    log("Сырой ответ ФСА по", number, ":", json.dumps(raw, ensure_ascii=False)[:3000])
    log("Итог:", fsa.status_of(number))


# ---------- Расписание ----------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def next_run(cfg, now):
    target = now.replace(hour=cfg["hour"], minute=0, second=0, microsecond=0)
    target += dt.timedelta(days=(cfg["weekday"] - now.weekday()) % 7)
    return target if target > now else target + dt.timedelta(days=7)


def main():
    cfg = config()
    if "--diag" in sys.argv:
        return diag(cfg)
    if not cfg["url"] or not cfg["secret"]:
        sys.exit("Не заданы RECEIVER_URL и RECEIVER_SECRET")
    if "--once" in sys.argv:
        return run_check(cfg)

    diag(cfg)
    last = load_state().get("last_check")
    if not last or dt.datetime.now(MSK) - dt.datetime.fromisoformat(last) > dt.timedelta(days=7):
        try:
            run_check(cfg)
        except Exception as err:
            log("Проверка не удалась:", repr(err))
    while True:
        when = next_run(cfg, dt.datetime.now(MSK))
        log("Следующая проверка:", when.strftime("%d.%m.%Y %H:%M"))
        while dt.datetime.now(MSK) < when:
            time.sleep(min(3600, max(1, (when - dt.datetime.now(MSK)).total_seconds())))
        for attempt in range(3):
            try:
                run_check(cfg)
                break
            except Exception as err:
                log(f"Проверка не удалась (попытка {attempt + 1}):", repr(err))
                time.sleep(1800)


if __name__ == "__main__":
    main()
