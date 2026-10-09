"""
TikTok Live Manager — MAGNATA
Várias contas do TikTok entram juntas numa live.

• Tela: ui.html aberta numa janela própria do Edge/Chrome (modo app)
• Motor: Playwright (Edge/Chrome do PC, em segundo plano)
• Banco: Supabase (contas, chaves de acesso, travas, histórico)
"""

import asyncio
import json
import os
import platform
import random
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

APP_NAME = "MAGNATA Live Manager"

# ── Banco (Supabase) — a chave pública só chama as funções tlm_*, que exigem a chave de acesso
SUPA_URL = "https://rfhyvazvezsmdtfdaebn.supabase.co"
SUPA_ANON = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJmaHl2YXp2ZXpzbWR0ZmRh"
    "ZWJuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTAyMjU4OTAsImV4cCI6MjEwNTgwMTg5MH0.EaNThyB1TuHlT28Hlhu1D"
    "Y9Yt5l9G4s9QZdYQxRNdUw"
)

HEARTBEAT_EVERY = 30  # segundos (a trava no banco vence em 90s sem sinal)


# ╔═══════════════════════════════════════════════════════════════╗
# ║                    CAMINHOS E UTILIDADES                      ║
# ╚═══════════════════════════════════════════════════════════════╝

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """Arquivo empacotado junto (ui.html)."""
    base = Path(getattr(sys, "_MEIPASS", get_base_dir()))
    p = base / name
    return p if p.exists() else get_base_dir() / name


def get_data_dir() -> Path:
    """Windows: pasta tiktok_data ao lado do .exe.
    Mac: ~/Library/Application Support (o .app é só leitura quando vem baixado)."""
    if platform.system() == "Darwin" and getattr(sys, "frozen", False):
        d = Path.home() / "Library" / "Application Support" / "MagnataLiveManager"
    else:
        d = get_base_dir() / "tiktok_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def browsers_dir() -> Path:
    """Onde fica o Chromium reserva (só baixa se não tiver Chrome/Edge)."""
    return get_data_dir() / "navegadores"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _write_log(text: str):
    """Grava erro no erro_log.txt ao lado do exe (nunca derruba o app)."""
    try:
        folder = get_data_dir() if platform.system() == "Darwin" else get_base_dir()
        with open(folder / "erro_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()}  {text}\n")
    except Exception:
        pass


def _show_error(msg: str):
    _write_log(msg)
    try:
        if platform.system() == "Windows":
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, msg, f"{APP_NAME} — Erro", 0x10)
            return
    except Exception:
        pass
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass


# ╔═══════════════════════════════════════════════════════════════╗
# ║                        LOG DO APP                             ║
# ╚═══════════════════════════════════════════════════════════════╝

class LogBuffer:
    """Log em memória que a tela vai lendo aos poucos."""

    def __init__(self, keep: int = 300):
        self._items: List[dict] = []
        self._seq = 0
        self._keep = keep
        self._lock = threading.Lock()

    def add(self, msg: str, kind: str = "info"):
        with self._lock:
            self._seq += 1
            self._items.append({"id": self._seq, "t": ts(), "msg": msg, "kind": kind})
            if len(self._items) > self._keep:
                self._items = self._items[-self._keep:]

    def since(self, last_id: int) -> List[dict]:
        with self._lock:
            return [i for i in self._items if i["id"] > last_id]


LOG = LogBuffer()


def log(msg: str):
    kind = "info"
    if msg.startswith(("❌", "⛔")):
        kind = "err"
    elif msg.startswith(("⚠", "🔄")):
        kind = "warn"
    elif msg.startswith(("✅", "🏁", "🚀")):
        kind = "ok"
    LOG.add(msg, kind)


# ╔═══════════════════════════════════════════════════════════════╗
# ║                       BANCO (SUPABASE)                        ║
# ╚═══════════════════════════════════════════════════════════════╝

ERROS = {
    "CHAVE_INVALIDA": "Chave inválida ou bloqueada.",
    "SEM_PERMISSAO": "Só o admin pode fazer isso.",
    "NOME_VAZIO": "Coloca um nome.",
    "LIMITE_CONTAS": "Limite de 50 contas atingido.",
    "NAO_ENCONTRADO": "Não encontrado (talvez já foi removido).",
    "NAO_PODE_SE_BLOQUEAR": "Você não pode bloquear a si mesmo.",
    "NAO_PODE_SE_APAGAR": "Você não pode apagar a si mesmo.",
}


class ApiError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(ERROS.get(code, code))


def rpc(fn: str, **params):
    """Chama uma função tlm_* no Supabase."""
    req = urllib.request.Request(
        f"{SUPA_URL}/rest/v1/rpc/{fn}",
        data=json.dumps(params).encode("utf-8"),
        method="POST",
        headers={
            "apikey": SUPA_ANON,
            "Authorization": f"Bearer {SUPA_ANON}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
            raise ApiError(body.get("message") or str(e))
        except ApiError:
            raise
        except Exception:
            raise ApiError(f"Erro do servidor ({e.code})")
    except urllib.error.URLError:
        raise ApiError("Sem conexão com a internet / servidor.")


# ╔═══════════════════════════════════════════════════════════════╗
# ║                 NAVEGADOR DAS LIVES (PLAYWRIGHT)              ║
# ╚═══════════════════════════════════════════════════════════════╝

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR','pt','en-US','en']});
window.chrome = window.chrome || { runtime: {} };
"""

# procura a tela de "LIVE encerrada" — ignora o chat/comentários
_LIVE_END_JS = r"""
() => {
  const re = /(live\s+(has\s+)?ended|live\s+(terminou|encerrou|encerrada|finalizada|finalizou)|(esta|essa|a)\s+live\s+(terminou|foi\s+encerrada|acabou)|transmiss[aã]o\s+(ao\s+vivo\s+)?(terminou|encerrada))/i;
  const skip = /(chat|comment|message|danmu|gift)/i;
  const els = document.querySelectorAll('div,span,p,h1,h2,h3');
  for (const el of els) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 80 || el.children.length > 3) continue;
    if (!re.test(t)) continue;
    let n = el, inChat = false;
    while (n && n !== document.body) {
      const c = (typeof n.className === 'string' ? n.className : '') + ' ' + (n.id || '');
      if (skip.test(c)) { inChat = true; break; }
      n = n.parentElement;
    }
    if (!inChat) return true;
  }
  return false;
}
"""


class BrowserEngine:
    """Sessões de live (em segundo plano) + login visível do admin."""

    CHECK_EVERY = 15        # segundos entre checagens
    END_CONFIRMS = 2        # precisa ver "live encerrada" 2x seguidas
    MAX_RECONNECTS = 5      # por conta

    def __init__(self, log_fn, on_live_end=None):
        self._log = log_fn
        self._on_live_end = on_live_end or (lambda: None)
        self._pw = None
        self._browser = None
        self.browser_name = ""
        self._ctxs: Dict[str, object] = {}
        self._pages: Dict[str, object] = {}
        self._states: Dict[str, Optional[dict]] = {}
        self._names: Dict[str, str] = {}
        self._reconnects: Dict[str, int] = {}
        self.url: str = ""
        self._monitor_task = None
        self._end_hits = 0
        self._lgn_browser = None
        self._lgn_ctx = None
        self.login_open_for: Optional[str] = None

    # ── lifecycle ──

    async def _ensure_pw(self):
        if not self._pw:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()

    async def _launch(self, headless: bool, extra_args: list):
        """Edge → Chrome do PC (tocam a live e parecem navegador normal) → Chromium."""
        args = ["--disable-blink-features=AutomationControlled"] + extra_args
        for channel in ("msedge", "chrome", None):
            try:
                kw = {"headless": headless, "args": args}
                if channel:
                    kw["channel"] = channel
                br = await self._pw.chromium.launch(**kw)
                return br, (channel or "chromium")
            except Exception:
                continue
        raise RuntimeError("Nenhum navegador abriu (Edge/Chrome/Chromium).")

    async def _ensure_browser(self):
        await self._ensure_pw()
        if self._browser and not self._browser.is_connected():
            self._browser = None
        if not self._browser:
            self._browser, self.browser_name = await self._launch(
                True,
                ["--no-sandbox", "--disable-dev-shm-usage", "--mute-audio",
                 "--autoplay-policy=no-user-gesture-required"],
            )
            self._log(f"Navegador das lives: {self.browser_name}")

    async def shutdown(self):
        await self.leave_all()
        await self.cancel_login()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    # ── login (admin, navegador visível) ──

    async def open_login(self, acc_id: str, state: Optional[dict]):
        await self._ensure_pw()
        await self.cancel_login()
        self._lgn_browser, _ = await self._launch(False, ["--start-maximized"])
        opts: dict = {"user_agent": random.choice(_USER_AGENTS), "locale": "pt-BR", "no_viewport": True}
        if state:
            opts["storage_state"] = state
        self._lgn_ctx = await self._lgn_browser.new_context(**opts)
        page = await self._lgn_ctx.new_page()
        await page.add_init_script(_STEALTH_JS)
        self.login_open_for = acc_id
        await page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded", timeout=40_000)

    @staticmethod
    def state_has_session(state: dict) -> bool:
        for c in (state or {}).get("cookies", []):
            if c.get("name") in ("sessionid", "sessionid_ss") and c.get("value") \
                    and "tiktok" in c.get("domain", ""):
                return True
        return False

    async def confirm_login(self) -> Optional[dict]:
        """Devolve o login (cookies) se a conta tá logada de verdade; senão None."""
        if not self._lgn_ctx:
            return None
        state = await self._lgn_ctx.storage_state()
        if not self.state_has_session(state):
            return None
        await self.cancel_login()
        return state

    async def cancel_login(self):
        for attr in ("_lgn_ctx", "_lgn_browser"):
            obj = getattr(self, attr)
            if obj:
                try:
                    await obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        self.login_open_for = None

    # ── live ──

    async def _open_page(self, acc_id: str):
        opts: dict = {
            "user_agent": random.choice(_USER_AGENTS),
            "viewport": {"width": 1280 + random.randint(-80, 80), "height": 720 + random.randint(-40, 40)},
            "locale": "pt-BR",
        }
        if self._states.get(acc_id):
            opts["storage_state"] = self._states[acc_id]
        ctx = await self._browser.new_context(**opts)
        page = await ctx.new_page()
        await page.add_init_script(_STEALTH_JS)
        self._ctxs[acc_id] = ctx
        self._pages[acc_id] = page
        await page.goto(self.url, wait_until="domcontentloaded", timeout=40_000)

    async def join_live(self, acc_id: str, state: Optional[dict], url: str, name: str = "") -> bool:
        await self._ensure_browser()
        if self.url != url:
            self._end_hits = 0
        self.url = url
        self._states[acc_id] = state
        self._names[acc_id] = name or acc_id
        self._reconnects[acc_id] = 0
        try:
            await self._open_page(acc_id)
            self._start_monitor()
            return True
        except Exception as exc:
            self._log(f"❌ {name}: {str(exc).splitlines()[0][:120]}")
            await self.leave_live(acc_id)
            return False

    async def leave_live(self, acc_id: str):
        pg = self._pages.pop(acc_id, None)
        ctx = self._ctxs.pop(acc_id, None)
        for obj in (pg, ctx):
            if obj:
                try:
                    await obj.close()
                except Exception:
                    pass

    async def leave_all(self):
        t = self._monitor_task
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()
        self._monitor_task = None
        for aid in list(self._pages.keys()):
            await self.leave_live(aid)
        self._end_hits = 0

    def watching_ids(self) -> List[str]:
        return list(self._pages.keys())

    # ── vigia: fim da live + reconexão ──

    def _start_monitor(self):
        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.ensure_future(self._monitor())

    async def _monitor(self):
        try:
            while self._pages:
                await asyncio.sleep(self.CHECK_EVERY)
                if not self._pages:
                    break

                if self._browser and not self._browser.is_connected():
                    self._log("⚠ Navegador caiu — reabrindo as contas...")
                    self._browser = None
                    for aid in list(self._pages.keys()):
                        self._ctxs.pop(aid, None)
                        self._pages[aid] = None
                    await self._ensure_browser()

                for aid in list(self._pages.keys()):
                    pg = self._pages.get(aid)
                    if pg is not None and not pg.is_closed():
                        continue
                    nm = self._names.get(aid, aid)
                    self._reconnects[aid] = self._reconnects.get(aid, 0) + 1
                    if self._reconnects[aid] > self.MAX_RECONNECTS:
                        self._log(f"❌ {nm} caiu muitas vezes — saiu da live")
                        await self.leave_live(aid)
                        continue
                    self._log(f"🔄 {nm} caiu — reconectando...")
                    try:
                        await self.leave_live(aid)
                        await self._open_page(aid)
                        self._log(f"✅ {nm} voltou pra live")
                    except Exception as exc:
                        self._log(f"⚠ {nm} não reconectou: {str(exc)[:100]}")

                ended = False
                for pg in list(self._pages.values()):
                    if pg is None or pg.is_closed():
                        continue
                    try:
                        ended = await pg.evaluate(_LIVE_END_JS)
                    except Exception:
                        continue
                    break
                self._end_hits = self._end_hits + 1 if ended else 0
                if self._end_hits >= self.END_CONFIRMS:
                    self._log("🏁 A live terminou — todas as contas saíram")
                    await self.leave_all()
                    self._on_live_end()
                    return
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log(f"⚠ Vigia da live parou: {exc}")


# ── loop async numa thread separada ──

_loop: Optional[asyncio.AbstractEventLoop] = None


def _run_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def fire(coro):
    if _loop is None:
        raise RuntimeError("Loop async não iniciado")
    return asyncio.run_coroutine_threadsafe(coro, _loop)


# ╔═══════════════════════════════════════════════════════════════╗
# ║                     CÉREBRO DO APP                            ║
# ╚═══════════════════════════════════════════════════════════════╝

class AppCore:
    def __init__(self):
        self.key: Optional[str] = None
        self.user: Optional[dict] = None
        self.engine = BrowserEngine(log, on_live_end=self._on_live_end)
        self.claimed: Dict[str, str] = {}      # id -> nome (travadas por mim)
        self.launching = False
        self._stop_flag = False
        self._hb_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.kicked = False                     # chave bloqueada no meio do uso

    # ── chave salva ──

    @property
    def _key_file(self) -> Path:
        return get_data_dir() / "chave.txt"

    def saved_key(self) -> Optional[str]:
        try:
            k = self._key_file.read_text(encoding="utf-8").strip()
            return k or None
        except Exception:
            return None

    # ── helpers ──

    def _need_login(self):
        if not self.key:
            raise ApiError("CHAVE_INVALIDA")

    def _need_admin(self):
        self._need_login()
        if (self.user or {}).get("role") != "admin":
            raise ApiError("SEM_PERMISSAO")

    def is_live(self) -> bool:
        return bool(self.engine.watching_ids()) or self.launching

    # ── ações ──

    def act_boot(self, p):
        k = self.saved_key()
        if not k:
            return {"user": None}
        try:
            return self.act_login({"key": k, "remember": True})
        except ApiError as e:
            if e.code == "CHAVE_INVALIDA":
                self._forget_key()
            return {"user": None, "error": str(e)}

    def act_login(self, p):
        key = (p.get("key") or "").strip().upper()
        user = rpc("tlm_login", p_key=key)
        self.key, self.user, self.kicked = key, user, False
        if p.get("remember"):
            try:
                self._key_file.write_text(key, encoding="utf-8")
            except Exception:
                pass
        log(f"Bem-vindo, {user['name']}!")
        return {"user": user}

    def _forget_key(self):
        try:
            self._key_file.unlink()
        except Exception:
            pass

    def act_logout(self, p):
        self.act_stop({})
        self.key = self.user = None
        self._forget_key()
        return {}

    def act_status(self, p):
        return {
            "user": self.user,
            "kicked": self.kicked,
            "watching": self.engine.watching_ids(),
            "claimed": list(self.claimed.keys()),
            "live_url": self.engine.url if self.is_live() else "",
            "launching": self.launching,
            "browser": self.engine.browser_name,
            "login_open_for": self.engine.login_open_for,
            "logs": LOG.since(int(p.get("since") or 0)),
        }

    def act_accounts(self, p):
        self._need_login()
        return {"accounts": rpc("tlm_list_accounts", p_key=self.key)}

    # ── lançar na live ──

    def act_launch(self, p):
        self._need_login()
        url = (p.get("url") or "").strip()
        u = urlparse(url if "://" in url else "https://" + url)
        if "tiktok.com" not in (u.netloc or "") and "vm.tiktok" not in url:
            raise ApiError("Esse link não é do TikTok.")
        url = u.geturl()
        if self.is_live():
            raise ApiError("Já tem uma live rodando. Clica PARAR primeiro.")

        accs = rpc("tlm_claim", p_key=self.key, p_url=url) or []
        if not accs:
            raise ApiError("Nenhuma conta livre agora (sem login ou em uso por outra pessoa).")
        with self._lock:
            self.claimed = {a["id"]: a["name"] for a in accs}
        self.launching = True
        self._stop_flag = False
        log(f"🚀 Lançando {len(accs)} conta(s) na live...")

        def _work():
            try:
                for i, acc in enumerate(accs):
                    if self._stop_flag:
                        break
                    fut = fire(self.engine.join_live(acc["id"], acc.get("state"), url, acc["name"]))
                    try:
                        ok = fut.result(timeout=60)
                        log(f"✅ {acc['name']} entrou" if ok else f"❌ {acc['name']} não conseguiu entrar")
                    except Exception as exc:
                        log(f"❌ {acc['name']}: {exc}")
                    if i < len(accs) - 1 and not self._stop_flag:
                        time.sleep(random.uniform(1.5, 3.5))
            finally:
                self.launching = False
                n = len(self.engine.watching_ids())
                if not self._stop_flag:
                    log(f"✅ Pronto — {n} conta(s) na live")
                self._sync_locks()
            self._ensure_heartbeat()

        threading.Thread(target=_work, daemon=True).start()
        self._ensure_heartbeat()
        return {"count": len(accs)}

    def act_stop(self, p):
        self._stop_flag = True
        try:
            fire(self.engine.leave_all()).result(timeout=30)
        except Exception:
            pass
        self._release_all()
        if p is not None and p.get("_quiet") is None:
            log("Todas as contas saíram da live")
        return {}

    def _on_live_end(self):
        self._release_all()

    def _release_all(self):
        with self._lock:
            had = bool(self.claimed)
            self.claimed = {}
        if had and self.key:
            try:
                rpc("tlm_release", p_key=self.key)
            except Exception:
                pass

    def _sync_locks(self):
        """Solta no banco as contas travadas que não estão mais na live."""
        if self.launching:
            return
        watching = set(self.engine.watching_ids())
        with self._lock:
            gone = [i for i in self.claimed if i not in watching]
            for i in gone:
                self.claimed.pop(i, None)
        if gone and self.key:
            try:
                rpc("tlm_release", p_key=self.key, p_ids=gone)
            except Exception:
                pass

    def _ensure_heartbeat(self):
        if self._hb_thread and self._hb_thread.is_alive():
            return

        def _hb():
            while self.claimed or self.launching:
                time.sleep(HEARTBEAT_EVERY)
                if not self.key:
                    break
                self._sync_locks()
                if not self.claimed:
                    break
                try:
                    rpc("tlm_heartbeat", p_key=self.key)
                except ApiError as e:
                    if e.code == "CHAVE_INVALIDA":
                        self.kick()
                        break
                except Exception:
                    pass

        self._hb_thread = threading.Thread(target=_hb, daemon=True)
        self._hb_thread.start()

    def kick(self):
        """Chave bloqueada/apagada no meio do uso: tira tudo e volta pra tela de chave."""
        if not self.key:
            return
        log("⛔ Seu acesso foi bloqueado pelo admin")
        self.kicked = True
        self.act_stop({"_quiet": 1})
        self.key = self.user = None
        self._forget_key()

    # ── ADMIN: contas ──

    def act_admin_add_account(self, p):
        self._need_admin()
        rpc("tlm_admin_add_account", p_key=self.key, p_name=p.get("name", ""), p_gmail=p.get("gmail", ""))
        log(f"Conta adicionada: {p.get('name')}")
        return {}

    def act_admin_remove_account(self, p):
        self._need_admin()
        aid = p["id"]
        if aid in self.engine.watching_ids():
            fire(self.engine.leave_live(aid)).result(timeout=15)
        rpc("tlm_admin_remove_account", p_key=self.key, p_id=aid)
        log("Conta removida")
        return {}

    def act_admin_free_account(self, p):
        self._need_admin()
        rpc("tlm_admin_free_account", p_key=self.key, p_id=p["id"])
        return {}

    def act_admin_login_open(self, p):
        self._need_admin()
        aid = p["id"]
        state = rpc("tlm_admin_get_state", p_key=self.key, p_id=aid)
        fire(self.engine.open_login(aid, state)).result(timeout=60)
        log(f"Navegador de login aberto: {p.get('name', '')}")
        return {}

    def act_admin_login_confirm(self, p):
        self._need_admin()
        aid = self.engine.login_open_for
        if not aid:
            raise ApiError("O navegador de login não tá aberto.")
        state = fire(self.engine.confirm_login()).result(timeout=30)
        if not state:
            return {"saved": False}
        rpc("tlm_admin_save_state", p_key=self.key, p_id=aid, p_state=state)
        log(f"✅ Login salvo: {p.get('name', '')}")
        return {"saved": True}

    def act_admin_login_cancel(self, p):
        fire(self.engine.cancel_login()).result(timeout=15)
        return {}

    # ── ADMIN: usuários ──

    def act_admin_users(self, p):
        self._need_admin()
        return {"users": rpc("tlm_admin_list_users", p_key=self.key)}

    def act_admin_create_user(self, p):
        self._need_admin()
        k = rpc("tlm_admin_create_user", p_key=self.key, p_name=p.get("name", ""),
                p_role="admin" if p.get("role") == "admin" else "user")
        log(f"Acesso criado: {p.get('name')}")
        return {"key": k}

    def act_admin_reset_key(self, p):
        self._need_admin()
        return {"key": rpc("tlm_admin_reset_key", p_key=self.key, p_user=p["id"])}

    def act_admin_set_active(self, p):
        self._need_admin()
        rpc("tlm_admin_set_active", p_key=self.key, p_user=p["id"], p_active=bool(p.get("active")))
        return {}

    def act_admin_delete_user(self, p):
        self._need_admin()
        rpc("tlm_admin_delete_user", p_key=self.key, p_user=p["id"])
        return {}

    def act_admin_activity(self, p):
        self._need_admin()
        return {"items": rpc("tlm_admin_activity", p_key=self.key)}

    # ── fechar ──

    def shutdown(self):
        try:
            self.act_stop({"_quiet": 1})
        except Exception:
            pass
        try:
            fire(self.engine.shutdown()).result(timeout=10)
        except Exception:
            pass


# ╔═══════════════════════════════════════════════════════════════╗
# ║                SERVIDOR LOCAL (tela ↔ motor)                  ║
# ╚═══════════════════════════════════════════════════════════════╝

TOKEN = secrets.token_urlsafe(24)
CORE: Optional[AppCore] = None
_last_ping = time.time()
_seen_ping = False
_bye_at = 0.0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            try:
                html = resource_path("ui.html").read_text(encoding="utf-8")
            except Exception:
                html = "<h1>ui.html não encontrado — coloque ao lado do app.py</h1>"
            html = html.replace("__TOKEN__", TOKEN)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(404, b"", "text/plain")

    def do_POST(self):
        global _last_ping, _seen_ping, _bye_at
        if self.path.startswith("/api/bye") and f"t={TOKEN}" in self.path:
            _bye_at = time.time()
            self._send(204, b"", "text/plain")
            return
        if self.headers.get("X-Token") != TOKEN or not self.path.startswith("/api/"):
            self._send(403, b"{}", "application/json")
            return
        _last_ping = time.time()
        _seen_ping = True
        action = self.path[5:].split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
            fn = getattr(CORE, f"act_{action}", None)
            if not fn:
                raise ApiError("Ação desconhecida")
            data = fn(payload) or {}
            out = {"ok": True, **data}
        except ApiError as e:
            if e.code == "CHAVE_INVALIDA" and action not in ("login", "boot") and CORE.key:
                CORE.kick()
                out = {"ok": False, "error": "Seu acesso foi bloqueado pelo admin.", "code": "BLOQUEADO"}
            else:
                out = {"ok": False, "error": str(e), "code": e.code}
        except Exception as e:
            _write_log(f"{action}: {traceback.format_exc()}")
            out = {"ok": False, "error": f"Erro: {str(e).splitlines()[0][:160] if str(e) else type(e).__name__}"}
        self._send(200, json.dumps(out, default=str).encode("utf-8"), "application/json")


# ╔═══════════════════════════════════════════════════════════════╗
# ║                     JANELA DO APP                             ║
# ╚═══════════════════════════════════════════════════════════════╝

def _find_browser_exe(chrome_edge_only: bool = False) -> Optional[str]:
    cands = []
    if platform.system() == "Windows":
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if root:
                cands += [
                    Path(root) / "Microsoft/Edge/Application/msedge.exe",
                    Path(root) / "Google/Chrome/Application/chrome.exe",
                ]
    elif platform.system() == "Darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            cands += [
                root / "Google Chrome.app/Contents/MacOS/Google Chrome",
                root / "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
            if not chrome_edge_only:
                cands.append(root / "Brave Browser.app/Contents/MacOS/Brave Browser")
    else:
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            w = shutil.which(name)
            if w:
                cands.append(Path(w))
    for c in cands:
        if c.exists():
            return str(c)
    return None


def open_window(url: str) -> Optional[subprocess.Popen]:
    exe = _find_browser_exe()
    if not exe:
        webbrowser.open(url)
        return None
    profile = get_data_dir() / "janela"
    args = [
        exe, f"--app={url}", f"--user-data-dir={profile}",
        "--window-size=1280,840", "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate,msEdgeSidebarV2,msHubApps,IntensiveWakeUpThrottling",
        "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
    ]
    flags = 0x08000000 if platform.system() == "Windows" else 0  # sem janela de console
    return subprocess.Popen(args, creationflags=flags)


def _need_bundled_chromium() -> bool:
    """Só baixa o Chromium se o PC não tiver Edge nem Chrome."""
    if _find_browser_exe(chrome_edge_only=True):
        return False
    try:
        base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or browsers_dir())
        if base.is_dir() and any(p.name.startswith("chromium") for p in base.iterdir()):
            return False
    except Exception:
        pass
    return True


def _install_chromium_bg():
    def _run():
        try:
            log("⚠ Edge/Chrome não encontrado — baixando navegador (só na 1ª vez)...")
            from playwright._impl._driver import compute_driver_executable, get_driver_env

            drv = compute_driver_executable()
            cmd = list(drv) if isinstance(drv, (tuple, list)) else [str(drv)]
            flags = 0x08000000 if platform.system() == "Windows" else 0
            r = subprocess.run(cmd + ["install", "chromium"], env=get_driver_env(),
                               capture_output=True, text=True, creationflags=flags)
            log("✅ Navegador baixado" if r.returncode == 0 else "❌ Falha ao baixar navegador")
        except Exception as exc:
            log(f"❌ Falha ao baixar navegador: {exc}")

    threading.Thread(target=_run, daemon=True).start()


def _wait_until_closed(proc: Optional[subprocess.Popen]):
    """Windows: a janela fecha → o processo do Edge termina.
    Mac: o Chrome continua aberto depois de fechar a janela, então a tela avisa
    ("bye") quando fecha; e se parar de dar sinal por 1 min, fecha também."""
    mac = platform.system() == "Darwin"
    idle_limit = 60 if mac else 180
    while True:
        time.sleep(1)
        if proc and proc.poll() is not None:
            return
        if _bye_at and time.time() - _bye_at > 6 and _last_ping < _bye_at:
            return  # janela fechada (ao recarregar, a tela volta a dar sinal antes de 6s)
        if (mac or not proc) and _seen_ping and time.time() - _last_ping > idle_limit:
            return


def _selftest() -> int:
    """Teste da compilação no GitHub: o motor abre um navegador de verdade?
    Resultado vai pro arquivo em TLM_SELFTEST (no Windows o app não tem console)."""
    lines: List[str] = []

    def say(m: str):
        lines.append(m)
        try:
            print(m, flush=True)
        except Exception:
            pass

    eng = BrowserEngine(say)

    async def _go():
        await eng._ensure_browser()
        ctx = await eng._browser.new_context()
        pg = await ctx.new_page()
        await pg.set_content("<h1>ok</h1>")
        txt = await pg.inner_text("h1")
        await eng.shutdown()
        return txt

    code = 1
    try:
        ok = fire(_go()).result(timeout=180) == "ok"
        ui = resource_path("ui.html").exists()
        say(f"SELFTEST navegador={eng.browser_name} motor={'OK' if ok else 'FALHOU'} "
            f"tela={'OK' if ui else 'FALTANDO'} dados={get_data_dir()}")
        code = 0 if ok and ui else 1
    except Exception as exc:
        say(f"SELFTEST FALHOU: {exc}")
    out = os.environ.get("TLM_SELFTEST", "")
    if out not in ("", "1"):
        try:
            Path(out).write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass
    return code


def main():
    global CORE
    if getattr(sys, "frozen", False):
        os.chdir(get_base_dir())

    threading.Thread(target=_run_loop, daemon=True).start()
    for _ in range(100):
        if _loop is not None:
            break
        time.sleep(0.02)

    if getattr(sys, "frozen", False):
        # Chromium reserva numa pasta gravável (no Mac o .app é só leitura)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir())

    if os.environ.get("TLM_SELFTEST"):
        return _selftest()

    CORE = AppCore()
    if _need_bundled_chromium():
        _install_chromium_bg()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/?t={TOKEN}"

    if os.environ.get("TLM_HEADLESS_TEST"):
        print(url, flush=True)
        while not os.path.exists(os.environ["TLM_HEADLESS_TEST"]):
            time.sleep(0.2)
    else:
        proc = open_window(url)
        _wait_until_closed(proc)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    CORE.shutdown()
    srv.shutdown()
    if _loop:
        _loop.call_soon_threadsafe(_loop.stop)


if __name__ == "__main__":
    try:
        code = main()
        if code:
            sys.exit(code)
    except Exception as exc:
        _write_log(traceback.format_exc())
        _show_error(f"Erro ao iniciar o app:\n\n{exc}")
        sys.exit(1)
