import asyncio
import html
import os
import secrets
import subprocess
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import qrcode
import yaml
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from .config import load_accounts, load_settings
from .store import Store


SERVICE = "tg_lead_skill.service"
STAGES = {
    "primary_interest": "Первичный интерес",
    "needs_explanation": "Нужно объяснение",
    "qualification_needed": "Квалификация",
    "objection_without_commitment": "Возражение",
    "ready_to_test": "Готов к тесту",
    "meeting_agreed": "Встреча",
    "contact_or_later": "Контакт позже",
    "not_interested": "Не интересно",
    "negative_or_non_target": "Не целевой",
    "unknown": "Не определён",
    "без этапа": "Без этапа",
}


def service_active():
    try:
        return subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", SERVICE], timeout=5
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def service_action(action):
    if action not in {"start", "stop", "restart"}:
        raise ValueError("bad service action")
    subprocess.run(["systemctl", "--user", action, SERVICE], check=True, timeout=20)


def qr_svg(value):
    qr = qrcode.QRCode(border=2, box_size=7)
    qr.add_data(value)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    cells = "".join(
        f'<rect x="{x}" y="{y}" width="1" height="1"/>'
        for y, row in enumerate(matrix) for x, dark in enumerate(row) if dark
    )
    return f'<svg viewBox="0 0 {size} {size}" role="img" aria-label="QR Telegram">{cells}</svg>'


class Dashboard:
    def __init__(self):
        load_dotenv()
        self.settings = load_settings()
        self.store = Store(self.settings.db_path)
        self.csrf = secrets.token_urlsafe(24)
        self.token = os.getenv("DASHBOARD_TOKEN", "")
        if not self.token:
            raise RuntimeError("DASHBOARD_TOKEN is required")
        self.bootstrap_ips = {
            ip.strip() for ip in os.getenv("DASHBOARD_BOOTSTRAP_IPS", "127.0.0.1").split(",") if ip.strip()
        }
        self.qr = {"status": "idle", "url": "", "error": "", "name": ""}
        self.qr_lock = threading.Lock()
        self.password_ready = threading.Event()
        self.password = ""
        for account in load_accounts(self.settings):
            self.store.ensure_account(account.name)

    def accounts(self):
        return {account.name: account for account in load_accounts(self.settings)}

    def start_qr(self):
        with self.qr_lock:
            if self.qr["status"] in {"starting", "scanning", "password"}:
                return
            self.qr = {"status": "starting", "url": "", "error": "", "name": ""}
            self.password = ""
            self.password_ready.clear()
        threading.Thread(target=lambda: asyncio.run(self._qr_login()), daemon=True).start()

    def submit_password(self, password):
        self.password = password
        self.password_ready.set()

    async def _qr_login(self):
        client = None
        try:
            source = next(iter(self.accounts().values()))
            client = TelegramClient(StringSession(), source.api_id, source.api_hash, proxy=source.proxy)
            await client.connect()
            qr = await client.qr_login()
            with self.qr_lock:
                self.qr.update(status="scanning", url=qr.url)
            try:
                await qr.wait(timeout=120)
            except errors.SessionPasswordNeededError:
                with self.qr_lock:
                    self.qr.update(status="password", url="")
                if not await asyncio.to_thread(self.password_ready.wait, 180):
                    raise TimeoutError("Время ввода пароля истекло")
                await client.sign_in(password=self.password)
            me = await client.get_me()
            name = (me.username or f"account-{me.id}").lower()
            existing = self.accounts()
            if name in existing:
                name = f"{name}-{me.id}"
            self._save_account(name, source, client.session.save())
            self.store.ensure_account(name)
            service_action("restart")
            with self.qr_lock:
                self.qr = {"status": "done", "url": "", "error": "", "name": name}
        except Exception as exc:
            with self.qr_lock:
                self.qr = {"status": "error", "url": "", "error": str(exc)[:200], "name": ""}
        finally:
            self.password = ""
            if client:
                await client.disconnect()

    def _save_account(self, name, source, session):
        path = os.getenv("RUNTIME_ACCOUNTS_FILE", "runtime_accounts.yaml")
        data = {"accounts": []}
        if os.path.exists(path):
            with open(path) as file:
                data = yaml.safe_load(file) or data
        data.setdefault("accounts", []).append({
            "name": name,
            "api_id": source.api_id,
            "api_hash": source.api_hash,
            "session": session,
            "proxy": source.proxy_url,
            "manager_username": self.settings.manager_username,
            "cold_dm_daily_limit": self.settings.cold_dm_daily_limit,
        })
        directory = os.path.dirname(os.path.abspath(path))
        with tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as file:
            yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
            temp_path = file.name
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)

    def render(self):
        snapshot = self.store.dashboard_snapshot()
        configs = self.accounts()
        active = service_active()
        by_name = {row["account"]: row for row in snapshot["accounts"]}
        account_rows = []
        now = time.time()
        for name, cfg in configs.items():
            row = by_name.get(name, {})
            enabled = bool(row.get("enabled", 1))
            cooldown = max(0, int(float(row.get("cooldown_until") or 0) - now))
            if not enabled:
                state, tone = "выкл", "off"
            elif not active or not row.get("healthy"):
                state, tone = "нет связи", "bad"
            elif cooldown:
                state, tone = f"пауза {cooldown // 60 + 1}м", "wait"
            else:
                state, tone = "жив", "good"
            limit = row.get("daily_limit")
            if limit is None:
                limit = cfg.cold_dm_daily_limit
            account_rows.append(f"""
              <form class="account" method="post" action="/account">
                <input type="hidden" name="csrf" value="{self.csrf}">
                <input type="hidden" name="account" value="{html.escape(name)}">
                <span class="dot {tone}"></span><strong>{html.escape(name)}</strong><span class="state">{state}</span>
                <label>лимит <input name="limit" type="number" min="0" max="1000" value="{limit}"></label>
                <button name="enabled" value="{0 if enabled else 1}">{'Отключить' if enabled else 'Включить'}</button>
              </form>""")
        max_stage = max((row["count"] for row in snapshot["stages"]), default=1)
        bars = "".join(
            f'<div class="bar"><span>{html.escape(STAGES.get(row["name"], row["name"]))}</span>'
            f'<i style="--w:{row["count"] / max_stage:.3f}"></i><b>{row["count"]}</b></div>'
            for row in snapshot["stages"]
        ) or '<p class="empty">Пока пусто</p>'
        qr_status = self.qr.copy()
        qr_block = ""
        refresh = ""
        if qr_status["status"] in {"starting", "scanning"}:
            refresh = '<meta http-equiv="refresh" content="3">'
            qr_block = qr_svg(qr_status["url"]) if qr_status["url"] else '<p>Создаю QR…</p>'
        elif qr_status["status"] == "password":
            qr_block = f'''<form method="post" action="/qr/password" class="password">
              <input type="hidden" name="csrf" value="{self.csrf}"><input type="password" name="password" placeholder="Пароль 2FA" required autofocus><button>Продолжить</button></form>'''
        elif qr_status["status"] == "done":
            qr_block = f'<p class="success">Добавлен {html.escape(qr_status["name"])}</p>'
        elif qr_status["status"] == "error":
            qr_block = f'<p class="error">{html.escape(qr_status["error"])}</p>'
        return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh}
<title>Pulsar</title><style>
:root{{--ink:#17201d;--paper:#f3f1e8;--line:#d6d2c4;--green:#1d7a51;--red:#a63b32;--amber:#b27818}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px Georgia,serif}}main{{max-width:980px;margin:auto;padding:56px 24px 80px}}header{{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--ink);padding-bottom:18px}}h1{{font-size:30px;margin:0;letter-spacing:-1px}}button,input{{font:inherit}}button{{background:transparent;border:1px solid var(--ink);padding:8px 12px;cursor:pointer}}button:hover{{background:var(--ink);color:var(--paper)}}.switch button{{border-color:{'var(--red)' if active else 'var(--green)'};color:{'var(--red)' if active else 'var(--green)'}}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);margin:28px 0}}.metric{{background:var(--paper);padding:22px 18px}}.metric b{{display:block;font-size:36px;font-weight:normal}}.metric span{{font-size:13px;text-transform:uppercase;letter-spacing:.08em}}section{{margin-top:38px}}h2{{font-size:13px;text-transform:uppercase;letter-spacing:.12em;margin:0 0 16px}}.account{{display:grid;grid-template-columns:12px 1fr 100px 170px 105px;gap:12px;align-items:center;border-top:1px solid var(--line);padding:12px 0}}.account label{{font-size:13px}}.account input{{width:70px;border:0;border-bottom:1px solid var(--ink);background:transparent;padding:5px}}.dot{{width:8px;height:8px;border-radius:50%;background:var(--red)}}.good{{background:var(--green)}}.wait{{background:var(--amber)}}.off{{background:#777}}.state{{font-size:13px;color:#666}}.bar{{display:grid;grid-template-columns:180px 1fr 40px;gap:15px;align-items:center;margin:11px 0}}.bar span{{font-size:14px}}.bar i{{height:9px;background:var(--green);transform:scaleX(var(--w));transform-origin:left}}.bar b{{font-weight:normal;text-align:right}}.qrline{{display:flex;gap:18px;align-items:flex-start}}.qrline svg{{width:260px;height:260px;background:white;padding:8px}}.password{{display:flex;gap:8px}}.password input{{padding:9px;background:white;border:1px solid var(--ink)}}.error{{color:var(--red);max-width:520px}}.success{{color:var(--green)}}@media(max-width:680px){{main{{padding:28px 16px}}.metrics{{grid-template-columns:repeat(2,1fr)}}.account{{grid-template-columns:12px 1fr 85px}}.account label{{grid-column:2}}.account button{{grid-column:3;grid-row:1/3}}.bar{{grid-template-columns:130px 1fr 30px}}}}
</style></head><body><main><header><h1>Pulsar</h1><form class="switch" method="post" action="/agent"><input type="hidden" name="csrf" value="{self.csrf}"><button name="action" value="{'stop' if active else 'start'}">{'Остановить' if active else 'Запустить'}</button></form></header>
<div class="metrics"><div class="metric"><b>{snapshot['received']}</b><span>поступило</span></div><div class="metric"><b>{snapshot['pending']}</b><span>в очереди</span></div><div class="metric"><b>{snapshot['dropped']}</b><span>выпало</span></div><div class="metric"><b>{snapshot['warm']}</b><span>тёплые</span></div></div>
<section><h2>Аккаунты</h2>{''.join(account_rows)}</section><section><h2>Воронка</h2>{bars}</section>
<section><h2>Новый аккаунт</h2><div class="qrline"><form method="post" action="/qr/start"><input type="hidden" name="csrf" value="{self.csrf}"><button>Показать QR</button></form>{qr_block}</div></section>
</main></body></html>'''


class Handler(BaseHTTPRequestHandler):
    dashboard = None

    def authorized(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        value = cookie.get("hermes")
        return bool(value) and secrets.compare_digest(value.value, self.dashboard.token)

    def require_auth(self):
        if not self.authorized():
            self.send_error(401)
            return False
        return True

    def do_GET(self):
        if self.path == "/bootstrap":
            if self.client_address[0] not in self.dashboard.bootstrap_ips:
                self.send_error(403)
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"hermes={self.dashboard.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000",
            )
            self.end_headers()
            return
        if not self.require_auth():
            return
        if self.path != "/":
            self.send_error(404)
            return
        body = self.dashboard.render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.require_auth():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if not 0 <= length <= 4096:
            self.send_error(400)
            return
        form = {
            key: values[0]
            for key, values in parse_qs(
                self.rfile.read(length).decode(), max_num_fields=16
            ).items()
        }
        if not secrets.compare_digest(form.get("csrf", ""), self.dashboard.csrf):
            self.send_error(403)
            return
        try:
            if self.path == "/agent":
                service_action(form.get("action"))
            elif self.path == "/account":
                account = form.get("account", "")
                if account not in self.dashboard.accounts():
                    raise ValueError("unknown account")
                limit = int(form.get("limit", 0))
                if not 0 <= limit <= 1000:
                    raise ValueError("bad limit")
                self.dashboard.store.set_daily_limit(account, limit)
                self.dashboard.store.set_account_enabled(account, form.get("enabled") == "1")
            elif self.path == "/qr/start":
                self.dashboard.start_qr()
            elif self.path == "/qr/password":
                self.dashboard.submit_password(form.get("password", ""))
            else:
                raise ValueError("unknown action")
        except (ValueError, subprocess.SubprocessError) as exc:
            self.send_error(400, str(exc))
            return
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def run():
    dashboard = Dashboard()
    Handler.dashboard = dashboard
    server = ThreadingHTTPServer((os.getenv("DASHBOARD_HOST", "0.0.0.0"), int(os.getenv("DASHBOARD_PORT", "8080"))), Handler)
    server.serve_forever()


if __name__ == "__main__":
    run()
