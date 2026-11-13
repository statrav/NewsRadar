"""NewsRadar 설정 화면 서버. radar.py setup이 부른다.

- 이 PC 안(127.0.0.1)에서만 열리고, 빈 포트를 쓴다
- 주소에 무작위 열쇠(?k=…)를 붙인다. API는 같은 열쇠를 X-Token 머리글로 받아야 동작한다
- 화면 흐름: ① Claude 연결 확인 → ② 분야 → (Claude 추천 + 출처 시험) → ③ 확인 → [시작하기]
  → 저장 · 첫 기록 · 만든 사람에게 전달(구글 폼) · 첫 정리 → 페이지 열기 → 서버 종료
"""

import json
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import radar

TOKEN = secrets.token_urlsafe(16)
PAGE = Path(__file__).resolve().parent / "setup.html"
IDLE_LIMIT = 60 * 60  # 한 시간 동안 아무 요청이 없으면 닫는다

# 진행 중인 일 하나 (추천 또는 시작). 화면은 /api/progress로 0.7초마다 묻는다
JOB = {"kind": None, "stage": "idle", "done": 0, "total": 0, "message": "", "error": "", "result": None}
LOCK = threading.Lock()
LAST_SEEN = [time.time()]
SERVER: list = []


def set_job(**kw) -> None:
    with LOCK:
        JOB.update(kw)


def now_date() -> str:
    return radar.now_kst().strftime("%Y-%m-%d")


# ── 화면에 보여 줄 형태로 ──

def view_sources(result: dict) -> list[dict]:
    """통과한 출처를 화면용 이름·설명으로 (사이트·블로그 먼저, 뉴스 검색 나중)."""
    catalog = radar.load_catalog()
    sites, news = [], []
    for cid in result["passed"]["catalog"]:
        kind, s = catalog[cid]
        if kind == "news":
            news.append({"name": f"뉴스: {s['q'].strip(chr(34))}", "desc": s.get("desc", "")})
        else:
            sites.append({"name": s["name"], "desc": s.get("desc", "")})
    for s in result["passed"]["feeds"]:
        sites.append({"name": s["name"], "desc": s.get("why", "")})
    for s in result["passed"]["news"]:
        news.append({"name": f"뉴스: {s['q'].strip(chr(34))}", "desc": f"지난 1주 기사 {s['week']}건"})
    return sites + news


def current_state() -> dict:
    profile = radar.read_yaml(radar.PROFILE) if radar.PROFILE.exists() else {}
    info = radar.load_install()
    return {"profile": {k: profile.get(k) for k in ("field", "job", "time")}, "minutes": info.get("minutes"),
            "installed": bool(info.get("id")) and radar.SOURCES.exists(), "choices": radar.MINUTES_CHOICES, "version": radar.VERSION}


def save_time(at: str) -> bool:
    """이미 설정한 사람이 받을 시각만 바꿀 때: profile의 time과 예약만 바꾼다 (Claude 안 씀)."""
    profile = radar.read_yaml(radar.PROFILE)
    profile["time"] = at
    radar.PROFILE.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    radar.schedule(at)
    return True


# ── 오래 걸리는 일 (따로 돌린다) ──

def do_connect() -> None:
    """NewsRadar 전용 설정 폴더로 claude auth login. 브라우저가 열리고, 로그인하면 끝난다.
    브라우저 연결이 안 되는 PC에서는 코드를 붙여 넣으라고 하므로 창을 보이게 띄운다 (Windows: 새 콘솔)."""
    import subprocess
    set_job(kind="connect", stage="working", message="브라우저에서 Claude에 로그인해 주세요", error="")
    exe = radar.claude_exe()
    if not exe:
        return set_job(stage="error", error="Claude Code가 설치되어 있지 않아요. 설치 파일을 다시 실행해 주세요.")
    radar.CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    proc = subprocess.Popen([exe, "auth", "login"], env=radar.claude_env(), creationflags=flags)
    deadline = time.time() + 15 * 60
    while time.time() < deadline:
        time.sleep(3)
        if radar.auth_status().get("loggedIn"):
            if proc.poll() is None:
                proc.terminate()
            return set_job(stage="done", message="연결됐어요")
        if proc.poll() is not None and not radar.auth_status().get("loggedIn"):
            return set_job(stage="error", error="로그인이 끝나지 않았어요. 다시 해 주세요.")
    proc.terminate()
    set_job(stage="error", error="15분 안에 로그인이 끝나지 않았어요. 다시 해 주세요.")


def do_recommend(form: dict) -> None:
    set_job(kind="recommend", stage="working", done=0, total=0, message="Claude가 볼 만한 곳을 고르고 있어요", error="", result=None)
    try:
        profile = radar.read_yaml(radar.PROFILE) if radar.PROFILE.exists() else {}
        profile.update({
            "field": form["field"].strip(),
            "job": (form.get("job") or "").strip(),
            "follow": [],
            "regions": ["ko", "en"],
            "time": form.get("time") or "09:30",
            "weekly": True,
            "model": profile.get("model") or "claude-sonnet-5-5",
        })
        profile.setdefault("categories", ["기타"])  # 추천 결과로 바뀐다
        radar.DATA.mkdir(parents=True, exist_ok=True)
        radar.PROFILE.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")

        def progress(done, total, label, ok):
            if label == "claude":
                set_job(done=0, total=0, message="Claude가 볼 만한 곳을 고르고 있어요")
            else:
                set_job(done=done, total=total, message=f"하나씩 받아 보는 중 ({done}/{total})")

        result = radar.recommend(progress)
        n = result["summary"]["passed"]
        set_job(stage="done", message=f"{n}곳", result={
            "count": n, "sources": view_sources(result),
            "categories": result["profile"].get("categories") or profile["categories"],
            "minutes": form.get("minutes"),
        })
    except Exception as e:
        set_job(stage="error", error="출처를 고르지 못했어요. 잠시 뒤 다시 해 주세요.", message=str(e)[-300:])


def do_start(minutes: str) -> None:
    set_job(kind="start", stage="working", done=0, total=0, message="저장하고 있어요", error="")
    try:
        result = json.loads(radar.TEST_RESULT.read_text(encoding="utf-8"))
        radar.save_sources(result)
        info = radar.load_install()
        first = not info.get("id")
        if first:  # 처음 답한 '하루 소식 시간'은 Before라서 나중에 설정을 바꿔도 덮어쓰지 않는다
            info = {"id": uuid.uuid4().hex[:12], "installed": now_date(), "minutes": minutes}
        info.update(version=radar.VERSION, updated=now_date())
        radar.save_install(info)

        profile = radar.load_profile()
        set_job(message="매일 정리하도록 예약하고 있어요")
        radar.schedule(str(profile.get("time") or "09:30"))
        set_job(message="지금 있는 글을 '이미 본 글'로 기록하고 있어요")
        try:
            radar.collect(2, True)
        except SystemExit:
            pass
        if first:
            profile = radar.load_profile()
            radar.submit_form({"id": info["id"], "stage": "설치", "field": profile.get("field", ""), "minutes": minutes,
                               "sources": result["summary"]["passed"], "days": 0, "date": now_date(), "version": radar.VERSION})

        set_job(stage="running", message="첫 정리를 만들고 있어요")
        code = radar.run_daily()
        if code == 0 and radar.SITE.exists():
            webbrowser.open(radar.SITE.as_uri())
            set_job(stage="done", message="첫 정리를 열었어요")
        else:
            set_job(stage="error", error="첫 정리를 만들지 못했어요. 내일 정한 시간에 다시 해 볼게요.", message="")
    except Exception as e:
        set_job(stage="error", error="저장하지 못했어요.", message=str(e)[-300:])
    finally:
        threading.Timer(120, shutdown).start()  # 완료 화면을 볼 시간을 두고 닫는다


def shutdown() -> None:
    if SERVER:
        threading.Thread(target=SERVER[0].shutdown, daemon=True).start()


# ── HTTP ──

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 콘솔을 조용하게
        pass

    def allowed_host(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")  # 다른 이름으로 들어오는 요청(DNS 리바인딩)은 막는다

    def send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json(self, data: dict, code: int = 200) -> None:
        self.send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        LAST_SEEN[0] = time.time()
        if not self.allowed_host():
            return self.send(403, b"forbidden", "text/plain")
        if self.path.split("?")[0] == "/":
            if f"k={TOKEN}" not in self.path:
                return self.send(403, "바탕화면의 'NewsRadar 설정'으로 다시 열어 주세요.".encode("utf-8"), "text/plain; charset=utf-8")
            html = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            return self.send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if self.headers.get("X-Token") != TOKEN:
            return self.send(403, b"forbidden", "text/plain")
        if self.path == "/api/state":
            return self.json(current_state())
        if self.path == "/api/auth":
            a = radar.auth_status()
            return self.json({"loggedIn": bool(a.get("loggedIn")), "missing": bool(a.get("missing"))})
        if self.path == "/api/progress":
            with LOCK:
                return self.json(dict(JOB))
        self.send(404, b"not found", "text/plain")

    def do_POST(self):
        LAST_SEEN[0] = time.time()
        if not self.allowed_host() or self.headers.get("X-Token") != TOKEN:
            return self.send(403, b"forbidden", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length) or b"{}") if length else {}
        with LOCK:
            busy = JOB["stage"] in ("working", "running")
        if busy:
            return self.json({"error": "하던 일이 아직 끝나지 않았어요"}, 409)
        if self.path == "/api/save-time":
            at = data.get("time") or ""
            if not (radar.PROFILE.exists() and len(at) == 5 and at[2] == ":"):
                return self.json({"error": "받을 시각을 다시 골라 주세요"}, 400)
            save_time(at)
            return self.json({"ok": True, "time": at})
        if self.path == "/api/connect":
            threading.Thread(target=do_connect, daemon=True).start()
        elif self.path == "/api/recommend":
            if not (data.get("field") or "").strip() or data.get("minutes") not in radar.MINUTES_CHOICES:
                return self.json({"error": "분야와 하루 소식 시간을 골라 주세요"}, 400)
            threading.Thread(target=do_recommend, args=(data,), daemon=True).start()
        elif self.path == "/api/start":
            if not radar.TEST_RESULT.exists():
                return self.json({"error": "추천을 먼저 받아 주세요"}, 400)
            threading.Thread(target=do_start, args=(data.get("minutes") or "",), daemon=True).start()
        else:
            return self.send(404, b"not found", "text/plain")
        self.json({"ok": True})


def watch_idle() -> None:
    while SERVER:
        time.sleep(30)
        with LOCK:
            busy = JOB["stage"] in ("working", "running")
        if not busy and time.time() - LAST_SEEN[0] > IDLE_LIMIT:
            shutdown()
            return


def serve(open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    SERVER.append(server)
    url = f"http://127.0.0.1:{server.server_address[1]}/?k={TOKEN}"
    print(f"설정 화면: {url}", flush=True)
    print("(이 창을 닫으면 설정 화면도 닫혀요)", flush=True)
    threading.Thread(target=watch_idle, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        SERVER.clear()
        server.server_close()
