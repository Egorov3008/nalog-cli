#!/usr/bin/env python3
"""
CLI + локальный REST API для lknpd.nalog.ru — МОЙ НАЛОГ (самозанятые НПД).

Первый запуск — один раз, нужен открытый браузер с авторизацией:
  python3 nalog_cli.py auth init

После этого браузер не нужен:
  python3 nalog_cli.py login-status
  python3 nalog_cli.py receipts list
  python3 nalog_cli.py receipts add --name "Разработка" --amount 5000
  python3 nalog_cli.py receipts cancel --id <uuid>
  python3 nalog_cli.py serve --port 8080

HTTP API (после serve):
  GET  /api/login-status
  GET  /api/receipts?from=01.01.2026&to=31.12.2026&limit=10
  GET  /api/receipts/summary
  POST /api/receipts            {"name":"Услуга","amount":5000}
  POST /api/receipts/<uuid>/cancel   {"reason":"mistake"}
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

DAEMON_DEFAULT = "http://127.0.0.1:10086"
DAEMON_SESSION = "nalog"
SITE_URL = "https://lknpd.nalog.ru/"
API_BASE = "https://lknpd.nalog.ru"
MSK = timezone(timedelta(hours=3))
CONFIG_DIR = Path.home() / ".config" / "nalog-cli"
TOKENS_FILE = CONFIG_DIR / "tokens.json"
DEFAULT_PORT = 8080

# ── Output helpers ─────────────────────────────────────────────────────────────

def out(data):
    print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))

def fail(code, message, exit_code=1):
    print(json.dumps({"ok": False, "error": {"code": code, "message": message}},
                     ensure_ascii=False))
    sys.exit(exit_code)

# ── Time helpers ───────────────────────────────────────────────────────────────

def msk_now():
    return datetime.now(MSK)

def msk_fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+03:00")

def parse_date(s):
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=MSK)
        except ValueError:
            pass
    fail("invalid_date",
         f"Неверный формат даты: {s!r}. Используйте DD.MM.YYYY или YYYY-MM-DD")

def fmt_total(amount, quantity):
    total = amount * quantity
    return str(int(total)) if total == int(total) else f"{total:.2f}"

# ── NalogClient ────────────────────────────────────────────────────────────────

class NalogError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)

class NalogClient:
    """Прямые HTTP-запросы к API lknpd.nalog.ru с авторефрешем токена."""

    def __init__(self, tokens=None):
        self._tokens = tokens

    @classmethod
    def load(cls):
        if not TOKENS_FILE.exists():
            raise NalogError("not_initialized",
                             f"Токены не найдены. Запустите: python3 nalog_cli.py auth init")
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
        except Exception as e:
            raise NalogError("tokens_corrupt", f"Файл токенов повреждён: {e}")
        return cls(tokens)

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TOKENS_FILE.write_text(json.dumps(self._tokens, indent=2, ensure_ascii=False))
        os.chmod(TOKENS_FILE, 0o600)

    @property
    def _ua(self):
        return self._tokens.get(
            "user_agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        )

    def _request(self, method, path, body=None):
        url = API_BASE + path
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self._tokens['token']}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": self._ua,
                "Referer": SITE_URL,
                "Origin": "https://lknpd.nalog.ru",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body_resp = json.loads(e.read())
            except Exception:
                body_resp = {}
            return e.code, body_resp
        except urllib.error.URLError as e:
            raise NalogError("network_error",
                             f"Ошибка сети при обращении к nalog.ru: {e}")

    def _refresh(self):
        payload = {
            "refreshToken": self._tokens["refresh_token"],
            "deviceInfo": {
                "sourceDeviceId": self._tokens["device_id"],
                "sourceType": "WEB",
                "appVersion": "1.0.0",
                "metaDetails": {"userAgent": self._ua},
            }
        }
        req = urllib.request.Request(
            API_BASE + "/api/v1/auth/token",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self._ua,
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                t = json.loads(resp.read())
                self._tokens["token"] = t["token"]
                self._tokens["refresh_token"] = t["refreshToken"]
                self._tokens["token_expires"] = t.get("tokenExpireIn")
                self.save()
                return True
        except Exception:
            return False

    def call(self, method, path, body=None):
        """API-вызов с автоматическим обновлением токена при 401."""
        status, resp = self._request(method, path, body)
        if status == 401:
            if not self._refresh():
                raise NalogError("session_expired",
                                 "Сессия истекла, не удалось обновить токен. "
                                 "Запустите: python3 nalog_cli.py auth init")
            status, resp = self._request(method, path, body)
        return status, resp

# ── Kimi WebBridge (только для auth init) ─────────────────────────────────────

def daemon_post(daemon_url, action, args_dict):
    body = json.dumps({
        "action": action, "args": args_dict, "session": DAEMON_SESSION
    }).encode()
    req = urllib.request.Request(
        f"{daemon_url}/command", data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        fail("daemon_unreachable",
             f"Не удается подключиться к Kimi WebBridge ({daemon_url}): {e}. "
             "Убедитесь что демон запущен: ~/.kimi-webbridge/bin/kimi-webbridge start")

def ensure_nalog_tab(daemon_url):
    r = daemon_post(daemon_url, "find_tab", {"url": SITE_URL})
    if r.get("ok") and r["data"].get("success"):
        return
    r = daemon_post(daemon_url, "list_browser_tabs", {})
    if r.get("ok"):
        tab = next(
            (t for t in r["data"].get("tabs", [])
             if "lknpd.nalog.ru" in t.get("url", "")), None
        )
        if tab:
            r2 = daemon_post(daemon_url, "attach_tab", {"tabId": tab["tabId"]})
            if r2.get("ok") and r2["data"].get("success"):
                return
    daemon_post(daemon_url, "navigate",
                {"url": SITE_URL, "newTab": True, "group_title": "Мой налог"})
    fail("not_logged_in",
         f"Открыта новая вкладка {SITE_URL}. "
         "Войдите в аккаунт и повторите: python3 nalog_cli.py auth init")

# ── Business logic (переиспользуется CLI и HTTP сервером) ─────────────────────

def api_list_receipts(client, from_date=None, to_date=None,
                      limit=10, offset=0, sort="operation_time:desc"):
    now = msk_now()
    from_dt = (parse_date(from_date) if from_date
               else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    to_dt = (parse_date(to_date).replace(hour=23, minute=59, second=59)
             if to_date else now)
    params = urllib.parse.urlencode({
        "from": msk_fmt(from_dt),
        "to": msk_fmt(to_dt),
        "offset": offset,
        "sortBy": sort,
        "limit": limit,
    })
    return client.call("GET", f"/api/v1/incomes?{params}")

def api_add_receipt(client, name, amount, quantity=1, date=None,
                    client_type="individual", client_name=None,
                    client_inn=None, payment_type="cash"):
    now = msk_now()
    if date:
        d = parse_date(date)
        op_dt = d.replace(hour=now.hour, minute=now.minute, second=now.second)
    else:
        op_dt = now
    payload = {
        "operationTime": msk_fmt(op_dt),
        "requestTime": msk_fmt(now),
        "services": [{"name": name, "amount": amount, "quantity": quantity}],
        "totalAmount": fmt_total(amount, quantity),
        "client": {
            "contactPhone": None,
            "displayName": client_name,
            "inn": client_inn,
            "incomeType": "FROM_LEGAL_ENTITY" if client_type == "legal" else "FROM_INDIVIDUAL",
        },
        "paymentType": "ACCOUNT" if payment_type == "account" else "CASH",
        "ignoreMaxTotalIncomeRestriction": False,
    }
    return client.call("POST", "/api/v1/income", payload)

def api_cancel_receipt(client, receipt_uuid, reason="mistake"):
    now = msk_now()
    comments = {"mistake": "Чек сформирован ошибочно", "refund": "Возврат средств"}
    return client.call("POST", "/api/v1/cancel", {
        "operationTime": msk_fmt(now),
        "requestTime": msk_fmt(now),
        "comment": comments.get(reason, "Чек сформирован ошибочно"),
        "receiptUuid": receipt_uuid,
    })

# ── CLI commands ───────────────────────────────────────────────────────────────

def cmd_auth_init(args):
    """Извлечь токены из браузера один раз, сохранить для автономной работы."""
    ensure_nalog_tab(args.daemon_url)

    code = """(()=>{
        const t=sessionStorage.getItem('auth.token');
        const rt=sessionStorage.getItem('refresh.token');
        const di=localStorage.getItem('sourceDeviceId');
        if(!t||!rt||!di) return JSON.stringify({error:'no_tokens',has_token:!!t,has_refresh:!!rt,has_device:!!di});
        return JSON.stringify({token:t,refresh_token:rt,device_id:di,user_agent:navigator.userAgent});
    })()"""

    r = daemon_post(args.daemon_url, "evaluate", {"code": code})
    if not r.get("ok"):
        fail("evaluate_error", "Не удалось прочитать токены из браузера")

    data = json.loads(r["data"]["value"])
    if "error" in data:
        fail("not_logged_in",
             f"Токены не найдены в браузере ({data}). "
             f"Убедитесь что вы авторизованы на {SITE_URL}")

    # Проверяем токен прямым запросом к API
    client = NalogClient(data)
    status, user = client._request("GET", "/api/v1/user")
    if status == 401:
        # Пробуем обновить сразу
        if not client._refresh():
            fail("token_invalid", "Токен недействителен. Войдите заново и повторите auth init.")
        status, user = client._request("GET", "/api/v1/user")
    if status != 200:
        fail("auth_check_failed",
             f"API вернул статус {status}. Попробуйте войти заново.")

    client.save()
    out({
        "message": f"Готово. Токены сохранены в {TOKENS_FILE}",
        "user": user.get("displayName"),
        "inn": user.get("inn"),
        "device_id": data["device_id"],
        "note": "Браузер больше не нужен. Токены обновляются автоматически.",
    })

def cmd_login_status(args):
    try:
        client = NalogClient.load()
        status, body = client.call("GET", "/api/v1/user")
    except NalogError as e:
        fail(e.code, e.message)
    if status == 200:
        out({
            "logged_in": True,
            "user": body.get("displayName"),
            "inn": body.get("inn"),
            "user_status": body.get("status"),
            "email": body.get("email"),
            "phone": body.get("phone"),
        })
    else:
        fail("api_error", f"Статус {status}: {body}")

def cmd_receipts_list(args):
    try:
        client = NalogClient.load()
        status, body = api_list_receipts(
            client, args.from_date, args.to_date, args.limit, args.offset, args.sort
        )
    except NalogError as e:
        fail(e.code, e.message)
    if status == 200:
        out(body)
    else:
        fail("api_error", f"Статус {status}: {body}")

def cmd_receipts_add(args):
    try:
        client = NalogClient.load()
        status, resp = api_add_receipt(
            client, args.name, args.amount, args.quantity, args.date,
            args.client_type, args.client_name, args.client_inn, args.payment_type
        )
    except NalogError as e:
        fail(e.code, e.message)
    if status == 200:
        uuid = resp.get("approvedReceiptUuid")
        out({"receipt_uuid": uuid,
             "cancel_hint": f"python3 nalog_cli.py receipts cancel --id {uuid}"})
    else:
        fail("api_error", f"Статус {status}: {resp}")

def cmd_receipts_cancel(args):
    try:
        client = NalogClient.load()
        status, resp = api_cancel_receipt(client, args.id, args.reason)
    except NalogError as e:
        fail(e.code, e.message)
    if status == 200:
        out(resp)
    else:
        fail("api_error", f"Статус {status}: {resp}")

def cmd_receipts_summary(args):
    try:
        client = NalogClient.load()
        status, body = client.call("GET", "/api/v1/incomes/summary")
    except NalogError as e:
        fail(e.code, e.message)
    if status == 200:
        out(body)
    else:
        fail("api_error", f"Статус {status}: {body}")

# ── HTTP API server ────────────────────────────────────────────────────────────

class NalogHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}", file=sys.stderr)

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def get_client(self):
        try:
            return NalogClient.load()
        except NalogError as e:
            self.send_json(503, {"ok": False, "error": {"code": e.code, "message": e.message}})
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = dict(urllib.parse.parse_qsl(parsed.query))
        client = self.get_client()
        if not client:
            return
        try:
            if path == "/api/login-status":
                status, body = client.call("GET", "/api/v1/user")
                if status == 200:
                    self.send_json(200, {"ok": True, "data": {
                        "logged_in": True,
                        "user": body.get("displayName"),
                        "inn": body.get("inn"),
                        "user_status": body.get("status"),
                    }})
                else:
                    self.send_json(status, {"ok": False, "error": {"code": "api_error", "message": str(body)}})

            elif path == "/api/receipts":
                status, body = api_list_receipts(
                    client,
                    from_date=qs.get("from"),
                    to_date=qs.get("to"),
                    limit=int(qs.get("limit", 10)),
                    offset=int(qs.get("offset", 0)),
                    sort=qs.get("sort", "operation_time:desc"),
                )
                if status == 200:
                    self.send_json(200, {"ok": True, "data": body})
                else:
                    self.send_json(status, {"ok": False, "error": {"code": "api_error", "message": str(body)}})

            elif path == "/api/receipts/summary":
                status, body = client.call("GET", "/api/v1/incomes/summary")
                if status == 200:
                    self.send_json(200, {"ok": True, "data": body})
                else:
                    self.send_json(status, {"ok": False, "error": {"code": "api_error", "message": str(body)}})

            else:
                self.send_json(404, {"ok": False, "error": {
                    "code": "not_found", "message": f"Неизвестный маршрут: {path}"
                }})
        except NalogError as e:
            self.send_json(502, {"ok": False, "error": {"code": e.code, "message": e.message}})
        except Exception as e:
            self.send_json(500, {"ok": False, "error": {"code": "internal_error", "message": str(e)}})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self.read_body()
        client = self.get_client()
        if not client:
            return
        try:
            if path == "/api/receipts":
                name = body.get("name")
                amount = body.get("amount")
                if not name or amount is None:
                    self.send_json(400, {"ok": False, "error": {
                        "code": "missing_params",
                        "message": "Обязательные поля: name, amount"
                    }})
                    return
                status, resp = api_add_receipt(
                    client,
                    name=name,
                    amount=float(amount),
                    quantity=int(body.get("quantity", 1)),
                    date=body.get("date"),
                    client_type=body.get("client_type", "individual"),
                    client_name=body.get("client_name"),
                    client_inn=body.get("client_inn"),
                    payment_type=body.get("payment_type", "cash"),
                )
                if status == 200:
                    self.send_json(200, {"ok": True, "data": {
                        "receipt_uuid": resp.get("approvedReceiptUuid")
                    }})
                else:
                    self.send_json(status, {"ok": False, "error": {"code": "api_error", "message": str(resp)}})

            elif re.match(r"^/api/receipts/[^/]+/cancel$", path):
                receipt_uuid = path.split("/")[3]
                status, resp = api_cancel_receipt(
                    client, receipt_uuid, body.get("reason", "mistake")
                )
                if status == 200:
                    self.send_json(200, {"ok": True, "data": resp})
                else:
                    self.send_json(status, {"ok": False, "error": {"code": "api_error", "message": str(resp)}})

            else:
                self.send_json(404, {"ok": False, "error": {
                    "code": "not_found", "message": f"Неизвестный маршрут: {path}"
                }})
        except NalogError as e:
            self.send_json(502, {"ok": False, "error": {"code": e.code, "message": e.message}})
        except Exception as e:
            self.send_json(500, {"ok": False, "error": {"code": "internal_error", "message": str(e)}})

def cmd_serve(args):
    try:
        NalogClient.load()
    except NalogError as e:
        fail(e.code, e.message)
    port = args.port
    lines = [
        f"Сервер запущен на http://localhost:{port}",
        "",
        "Эндпоинты:",
        f"  GET  /api/login-status",
        f"  GET  /api/receipts?from=01.01.2026&to=31.12.2026&limit=10",
        f"  GET  /api/receipts/summary",
        f'  POST /api/receipts            {{"name":"Услуга","amount":5000}}',
        f'  POST /api/receipts/<uuid>/cancel  {{"reason":"mistake"}}',
        "",
        "Ctrl+C для остановки.",
    ]
    for line in lines:
        print(line, file=sys.stderr)
    try:
        HTTPServer(("", port), NalogHandler).serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.", file=sys.stderr)

# ── Argparse ───────────────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        prog="nalog-cli",
        description=(
            "CLI + REST API для lknpd.nalog.ru (МОЙ НАЛОГ — НПД самозанятого).\n"
            "Первый запуск (нужен браузер): python3 nalog_cli.py auth init\n"
            "Далее браузер не нужен."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--daemon-url", default=DAEMON_DEFAULT, metavar="URL",
        help=f"URL Kimi WebBridge (нужен только для auth init; default: {DAEMON_DEFAULT})",
    )

    sub = parser.add_subparsers(dest="command", required=True, title="команды")

    # auth
    auth_p = sub.add_parser("auth", help="Управление авторизацией")
    auth_sub = auth_p.add_subparsers(dest="auth_cmd", required=True)
    auth_sub.add_parser("init",
                        help="Извлечь токены из браузера и сохранить (одноразово)")

    # login-status
    sub.add_parser("login-status", help="Проверить авторизацию и данные пользователя")

    # serve
    serve_p = sub.add_parser("serve", help="Запустить локальный HTTP REST API сервер")
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT,
                         help=f"Порт (по умолч.: {DEFAULT_PORT})")

    # receipts
    receipts_p = sub.add_parser("receipts", help="Операции с чеками")
    rsub = receipts_p.add_subparsers(dest="receipts_cmd", required=True)

    rlist = rsub.add_parser("list", help="Список чеков за период")
    rlist.add_argument("--from", dest="from_date", metavar="ДАТА",
                       help="Начало периода (DD.MM.YYYY / YYYY-MM-DD; по умолч.: 1-е мес.)")
    rlist.add_argument("--to", dest="to_date", metavar="ДАТА",
                       help="Конец периода (по умолч.: сегодня)")
    rlist.add_argument("--limit", type=int, default=10, metavar="N")
    rlist.add_argument("--offset", type=int, default=0, metavar="N")
    rlist.add_argument("--sort", default="operation_time:desc", metavar="ПОЛЕ:ПОРЯДОК")

    radd = rsub.add_parser("add", help="Добавить доход / выдать чек")
    radd.add_argument("--name", required=True, help="Наименование товара/услуги")
    radd.add_argument("--amount", required=True, type=float, help="Сумма за единицу в рублях")
    radd.add_argument("--quantity", type=int, default=1)
    radd.add_argument("--date", metavar="ДАТА", help="Дата продажи (по умолч.: сегодня)")
    radd.add_argument("--client-type", dest="client_type",
                      choices=["individual", "legal"], default="individual")
    radd.add_argument("--client-name", dest="client_name", metavar="ИМЯ", default=None)
    radd.add_argument("--client-inn", dest="client_inn", metavar="ИНН", default=None)
    radd.add_argument("--payment-type", dest="payment_type",
                      choices=["cash", "account"], default="cash")

    rcancel = rsub.add_parser("cancel", help="Аннулировать чек")
    rcancel.add_argument("--id", required=True, metavar="UUID")
    rcancel.add_argument("--reason", choices=["mistake", "refund"], default="mistake")

    rsub.add_parser("summary", help="Сводка по доходам и налогам")

    return parser

def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "auth":
        {"init": cmd_auth_init}[args.auth_cmd](args)
    elif args.command == "login-status":
        cmd_login_status(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "receipts":
        {
            "list": cmd_receipts_list,
            "add": cmd_receipts_add,
            "cancel": cmd_receipts_cancel,
            "summary": cmd_receipts_summary,
        }[args.receipts_cmd](args)

if __name__ == "__main__":
    main()
