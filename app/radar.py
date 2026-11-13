# /// script
# requires-python = ">=3.11"
# dependencies = ["feedparser>=6.0", "pyyaml>=6.0"]
# ///
"""
NewsRadar: 출처를 모아 후보를 만들고(collect), Claude가 쓴 정리(digest)로 페이지를 만든다(render).

    uv run radar.py collect              새 글 모으기 → <data>/candidates.json (LLM 안 씀)
    uv run radar.py collect --baseline   지금 있는 글을 '본 것'으로만 기록 (처음 한 번)
    uv run radar.py check                <data>/digest.json이 후보와 맞는지 점검
    uv run radar.py render               digest + 후보 → <data>/archive/<날짜>.json, <data>/site/index.html
    uv run radar.py week                 최근 7일 → <data>/week-input.json, Claude가 <data>/weekly.json 작성 (매일)
    uv run radar.py check --week         weekly.json 점검
    uv run radar.py where                데이터 폴더 위치 보기

사용자 데이터 폴더(<data>)는 코드와 떨어져 있다. 환경변수 NEWSRADAR_DATA가 있으면 그곳,
없으면 Windows는 %LOCALAPPDATA%\\NewsRadar\\data, Mac은 ~/Library/Application Support/NewsRadar/data.
<data>/profile.yaml(분야·하는 일·분류·거르는 단어)과 <data>/sources.yaml(출처)을 읽는다.
"""

import argparse
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen

import feedparser
import yaml


def default_data() -> Path:
    if os.environ.get("NEWSRADAR_DATA"):
        return Path(os.environ["NEWSRADAR_DATA"]).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "NewsRadar" / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "NewsRadar" / "data"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "NewsRadar" / "data"


APP = Path(__file__).resolve().parent
DATA = default_data()
PROFILE = DATA / "profile.yaml"
SOURCES = DATA / "sources.yaml"
SEEN = DATA / "seen.json"
CANDIDATES = DATA / "candidates.json"
DIGEST = DATA / "digest.json"
ARCHIVE = DATA / "archive"
SITE = DATA / "site" / "index.html"
TEMPLATE = APP / "template.html"
CATALOG = APP / "catalog"
WEEK_INPUT = DATA / "week-input.json"
WEEKLY = DATA / "weekly.json"
HISTORY_DAYS = 14

KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; newsradar/0.1)"  # 짧게 유지: 긴 브라우저 UA를 거절하는 사이트가 있다
MAX_CANDIDATES = 100
FEW_CANDIDATES = 10  # 이보다 적은 날은 흐름 1~2개도 허용
MAX_CATEGORIES = 8   # 페이지 색이 8가지
SNIPPET = 220
SEEN_KEEP_DAYS = 60
KINDS = ("feeds", "pages", "news")
# profile에 exclude가 없을 때 쓰는 기본값: 주가·광고성 제목
DEFAULT_EXCLUDE = r"\b(stock price|price target|buy rating|dividend|sponsored)\b|주가|목표가|매수 의견|\[광고\]|\(광고\)|협찬"


def now_kst() -> datetime:
    return datetime.now(KST)


def fetch(url: str, timeout: int = 20) -> bytes:
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm_url(url: str) -> str:
    url = re.sub(r"[?#].*$", "", url.strip()) if "news.google.com" not in url else url.strip()
    return url.rstrip("/")


def title_key(title: str) -> set[str]:
    words = re.findall(r"[0-9a-z]+|[가-힣]+", title.lower())
    return {w for w in words if len(w) > 1}


def similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.6


# ── 설정 읽기 ──

def read_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        sys.exit(f"설정 파일을 읽지 못했습니다: {path}\n  {e}")


def load_profile() -> dict:
    if not PROFILE.exists():
        sys.exit(f"설정이 아직 없습니다. 설정 화면에서 분야를 먼저 정해 주세요. (찾은 곳: {PROFILE})")
    p = read_yaml(PROFILE)
    cats = p.get("categories") or []
    if not 1 <= len(cats) <= MAX_CATEGORIES:
        sys.exit(f"설정의 분류는 1~{MAX_CATEGORIES}개여야 합니다 (지금 {len(cats)}개)")
    return p


def load_catalog() -> dict[str, tuple[str, dict]]:
    """app/catalog/*.yaml의 출처를 id로 찾을 수 있게. {id: (종류, 출처)}"""
    out: dict[str, tuple[str, dict]] = {}
    for f in sorted(CATALOG.glob("*.yaml")):
        for kind in KINDS:
            for s in read_yaml(f).get(kind) or []:
                out[s["id"]] = (kind, s)
    return out


def load_sources() -> tuple[dict[str, list[dict]], list[str]]:
    """사용자 sources.yaml = catalog에서 고른 id + 직접 넣은 출처. (종류별 출처, 찾지 못한 catalog id)"""
    if not SOURCES.exists():
        sys.exit(f"출처 목록이 아직 없습니다. 설정 화면에서 출처를 먼저 정해 주세요. (찾은 곳: {SOURCES})")
    user = read_yaml(SOURCES)
    catalog = load_catalog()
    out: dict[str, list[dict]] = {k: [] for k in KINDS}
    missing = []
    for cid in user.get("catalog") or []:
        if cid in catalog:
            kind, s = catalog[cid]
            out[kind].append(s)
        else:
            missing.append(cid)
    for kind in KINDS:
        out[kind] += user.get(kind) or []
    return out, missing


def regex(pattern: str | None) -> re.Pattern | None:
    return re.compile(pattern, re.I) if pattern else None


# ── 수집 ──

def from_feed(src: dict, since: datetime, kw: re.Pattern | None) -> list[dict]:
    parsed = feedparser.parse(fetch(src["url"]))
    out = []
    for e in parsed.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        when = datetime(*t[:6], tzinfo=timezone.utc) if t else None
        if when and when < since:
            continue
        title = clean(e.get("title", ""))
        snippet = clean(e.get("summary", ""))[:SNIPPET]
        if src.get("filter") and kw and not kw.search(f"{title} {snippet}"):
            continue
        if not title or not e.get("link"):
            continue
        out.append(item(src, title, e.link, when, snippet))
        if len(out) >= src.get("max", 8):
            break
    return out


def news_url(q: str, lang: str | None, period: str = "1d") -> str:
    hl = "hl=ko&gl=KR&ceid=KR:ko" if lang == "ko" else "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={quote_plus(q + ' when:' + period)}&{hl}"


def news_must(src: dict) -> re.Pattern:
    return re.compile(src.get("must") or re.escape(src["q"].split()[0].strip('"')), re.I)


def from_news(src: dict, since: datetime) -> list[dict]:
    parsed = feedparser.parse(fetch(news_url(src["q"], src.get("lang"))))
    must = news_must(src)
    out = []
    for e in parsed.entries:
        if not must.search(e.get("title", "")):
            continue  # 검색어와 상관없는 기사가 섞여 나온다
        t = e.get("published_parsed")
        when = datetime(*t[:6], tzinfo=timezone.utc) if t else None
        if when and when < since:
            continue
        title = clean(e.get("title", ""))
        publisher = clean(e.get("source", {}).get("title", "")) if e.get("source") else ""
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(publisher) - 3]
        s = dict(src, name=f"구글 뉴스 · {publisher or '언론'}")
        out.append(item(s, title, e.link, when, ""))
        if len(out) >= src.get("max", 3):
            break
    return out


def from_page(src: dict, seen: dict) -> tuple[list[dict], list[str]]:
    """목록 페이지에서 처음 보는 글 링크를 찾는다. (새 글, 페이지의 모든 글 주소)"""
    body = fetch(src["url"]).decode("utf-8", "replace")
    pat = re.compile(src["link"])
    links = []
    for href in re.findall(r'href="([^"]+)"', body):
        if pat.search(href):
            full = norm_url(urljoin(src["url"], href))
            if full not in links:
                links.append(full)
    fresh = [u for u in links if u not in seen][: src.get("max", 5)]
    out = []
    for u in fresh:
        title, snippet = page_title(u, src)
        out.append(item(src, title, u, None, snippet))
    return out, links


def meta_content(body: str, *names: str) -> str | None:
    for n in names:
        m = re.search(rf'<meta[^>]+(?:property|name)="{re.escape(n)}"[^>]+content="([^"]*)"', body) \
            or re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(n)}"', body)
        if m:
            return clean(m.group(1))
    return None


def page_title(url: str, src: dict) -> tuple[str, str]:
    try:
        body = fetch(url, timeout=15).decode("utf-8", "replace")
    except Exception:
        return slug_title(url), ""
    title = meta_content(body, "og:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S)
        title = clean(m.group(1)) if m else slug_title(url)
    # 제목 끝의 사이트 이름 꼬리 지우기 ("글 제목 | 회사 블로그")
    for name in filter(None, [meta_content(body, "og:site_name"), src.get("org"), src.get("name")]):
        title = re.sub(rf"\s*[|\-–—:·]\s*{re.escape(name)}\s*$", "", title, flags=re.I)
    desc = meta_content(body, "og:description", "description") or ""
    return title, desc[:SNIPPET]


def slug_title(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].replace(".html", "").replace("-", " ")


def item(src: dict, title: str, url: str, when: datetime | None, snippet: str) -> dict:
    return {
        "title": title,
        "url": url,
        "source": src["name"],
        "org": src.get("org", ""),
        "tier": src.get("tier", 2),
        "date": (when.astimezone(KST) if when else now_kst()).strftime("%Y-%m-%d"),
        "snippet": snippet,
    }


def safe(fn, src, *args):
    label = src.get("name") or src.get("q")
    try:
        return label, fn(src, *args), None
    except Exception as e:  # 한 곳이 실패해도 나머지는 계속
        return label, [], f"{type(e).__name__}: {e}"[:160]


def merge(items: list[dict]) -> list[dict]:
    """같은 글(주소)이나 거의 같은 제목을 하나로 묶고, 몇 곳에서 나왔는지 센다."""
    groups: list[dict] = []
    for it in items:
        key, u = title_key(it["title"]), norm_url(it["url"])
        for g in groups:
            if u in g["urls"] or similar(key, g["key"]):
                g["urls"].add(u)
                g["also"].append(it["source"])
                g["tier"] = min(g["tier"], it["tier"])
                g["org"] = g["org"] or it["org"]
                if not g["snippet"] and it["snippet"]:
                    g["snippet"] = it["snippet"]
                break
        else:
            groups.append(dict(it, key=key, urls={u}, also=[]))
    return groups


def score(g: dict) -> float:
    s = {1: 3.0, 2: 2.0, 3: 1.0}.get(g["tier"], 1.0)
    s += 2.0 * len(set(g["also"]) - {g["source"]})  # 여러 곳에서 다루면 중요
    s += 0.5 if g["org"] else 0
    s += 0.5 if g["snippet"] else 0
    return s


def collect(days: int, baseline: bool) -> None:
    profile = load_profile()
    cfg, missing = load_sources()
    kw = regex(profile.get("keywords"))
    drop = regex(profile["exclude"] if "exclude" in profile else DEFAULT_EXCLUDE) or re.compile(r"(?!)")
    seen: dict = json.loads(SEEN.read_text(encoding="utf-8")) if SEEN.exists() else {}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    today = now_kst().strftime("%Y-%m-%d")

    jobs = [] if baseline else [(from_feed, s, since, kw) for s in cfg["feeds"]] + [(from_news, s, since) for s in cfg["news"]]
    results, errors = [], {f"목록에 없는 출처 {cid}": "app/catalog에서 찾지 못함" for cid in missing}
    with ThreadPoolExecutor(8) as pool:
        futs = [pool.submit(safe, fn, s, *a) for fn, s, *a in jobs]
        futs += [pool.submit(safe, from_page, s, seen) for s in cfg["pages"]]
        for f in futs:
            label, got, err = f.result()
            if err:
                errors[label] = err
                continue
            if isinstance(got, tuple):  # 목록 페이지
                fresh, links = got
                for u in links:
                    seen.setdefault(u, today)
                got = [] if baseline else fresh
            results.extend(it for it in got if not drop.search(it["title"]))

    fresh = [it for it in results if norm_url(it["url"]) not in seen or seen[norm_url(it["url"])] == today]
    for it in results:
        seen.setdefault(norm_url(it["url"]), today)
    cutoff = (now_kst() - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y-%m-%d")
    seen = {u: d for u, d in seen.items() if d >= cutoff}
    DATA.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")
    total = sum(len(v) for v in cfg.values())
    if baseline:
        print(f"기준 기록: 본 글 {len(seen)}개 저장. 출처 {total}곳 중 실패 {len(errors)}곳")
        for k, v in errors.items():
            print(f"  실패 {k}: {v}")
        return

    groups = sorted(merge(fresh), key=lambda g: (score(g), g["date"]), reverse=True)[:MAX_CANDIDATES]
    out = []
    for i, g in enumerate(groups, 1):
        out.append({
            "id": f"c{i:03d}",
            "title": g["title"],
            "snippet": g["snippet"],
            "org": g["org"],
            "source": g["source"],
            "also": sorted(set(g["also"]) - {g["source"]}),
            "date": g["date"],
            "url": g["url"],
        })
    CANDIDATES.write_text(json.dumps({"collected_at": now_kst().isoformat(timespec="minutes"), "sources": total, "errors": errors, "items": out}, ensure_ascii=False, indent=1), encoding="utf-8")
    chars = sum(len(c["title"]) + len(c["snippet"]) + 40 for c in out)
    print(f"후보 {len(out)}건 (새 글 {len(fresh)}건에서 묶고 추림) → {CANDIDATES}")
    print(f"Claude가 읽을 양: 약 {chars:,}자 ≈ {chars // 3:,} 토큰 (어림)")
    print(f"출처 {total}곳 중 실패 {len(errors)}곳")
    for k, v in errors.items():
        print(f"  실패 {k}: {v}")
    if not out:
        sys.exit(2)  # 정리할 것이 없다 → 실행 스크립트가 실패 알림을 보낸다


# ── 점검·페이지 ──

def load_digest() -> tuple[dict, dict]:
    cands = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    digest = json.loads(DIGEST.read_text(encoding="utf-8"))
    return cands, digest


def theme_range(n_candidates: int) -> tuple[int, int]:
    return (1, 6) if n_candidates < FEW_CANDIDATES else (3, 6)


def problems(cands: dict, digest: dict, cats: list[str]) -> list[str]:
    ids = {c["id"] for c in cands["items"]}
    errs = []
    themes = digest.get("themes", [])
    lo, hi = theme_range(len(ids))
    if not lo <= len(themes) <= hi:
        errs.append(f"흐름은 {lo}~{hi}개여야 함 (지금 {len(themes)}개, 후보 {len(ids)}건)")
    for t in themes:
        for k in ("name", "line", "use", "ids"):
            if not t.get(k):
                errs.append(f"흐름 '{t.get('name', '?')}'에 {k} 없음")
        for i in t.get("ids", []):
            if i not in ids:
                errs.append(f"흐름 '{t.get('name')}'의 {i}가 후보에 없음")
            if i not in digest.get("items", {}):
                errs.append(f"{i}의 한 줄 제목(items)이 없음")
    for i, v in digest.get("items", {}).items():
        if i not in ids:
            errs.append(f"items의 {i}가 후보에 없음")
        if not v.get("h"):
            errs.append(f"{i}의 한 줄 제목(h)이 비어 있음")
        if v.get("cat") not in cats:
            errs.append(f"{i}의 분류 '{v.get('cat')}'는 목록에 없음 ({', '.join(cats)})")
    return errs


def render() -> None:
    profile = load_profile()
    cats = profile["categories"]
    cands, digest = load_digest()
    errs = problems(cands, digest, cats)
    if errs:
        sys.exit("정리에 문제가 있어 페이지를 만들지 않음:\n  " + "\n  ".join(errs))
    by_id = {c["id"]: c for c in cands["items"]}

    def entry(i: str) -> dict:
        c, d = by_id[i], digest["items"][i]
        return {"h": d["h"], "cat": d["cat"], "org": c["org"], "src": c["source"], "n": 1 + len(c["also"]), "date": c["date"], "url": c["url"]}

    in_theme = {i for t in digest["themes"] for i in t["ids"]}
    page = {
        "date": digest.get("date") or now_kst().strftime("%Y-%m-%d"),
        "collected": cands["collected_at"],
        "total": len(cands["items"]),
        "themes": [{"name": t["name"], "line": t["line"], "use": t["use"], "items": [entry(i) for i in t["ids"]]} for t in digest["themes"]],
        "others": [entry(i) for i in digest["items"] if i not in in_theme],
        "cats": cats,
        "errors": len(cands.get("errors", {})),
        "sources": cands.get("sources"),
    }
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    (ARCHIVE / f"{page['date']}.json").write_text(json.dumps(page, ensure_ascii=False, indent=1), encoding="utf-8")
    write_site(profile)
    print(f"페이지 생성: 흐름 {len(page['themes'])}개, 그 밖의 소식 {len(page['others'])}건 → {SITE}")


def archives(limit: int) -> list[dict]:
    """보관된 날짜별 페이지, 최근 것부터."""
    files = sorted(ARCHIVE.glob("*.json"), reverse=True)[:limit]
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def write_site(profile: dict) -> None:
    history = archives(HISTORY_DAYS)
    data = {
        "history": history,
        "weekly": weekly_page(history[0]["date"] if history else None),
        "search": profile.get("search") or profile.get("field") or "",
    }
    text = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    SITE.parent.mkdir(parents=True, exist_ok=True)
    SITE.write_text(TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", text), encoding="utf-8")


# ── 최근 7일 요약 (매일, profile.weekly가 true일 때) ──

def week() -> None:
    """최근 7일 보관분을 Claude가 읽을 짧은 목록으로 → week-input.json"""
    days = archives(7)
    if not days:
        sys.exit("보관된 날짜가 없음")
    items, out_days = {}, []
    for p in reversed(days):
        mmdd = p["date"][5:].replace("-", "")
        k = 0
        def add(x):
            nonlocal k
            k += 1
            i = f"{mmdd}-{k}"
            items[i] = {"h": x["h"], "cat": x["cat"], "org": x["org"], "n": x["n"], "url": x["url"], "src": x["src"], "date": x["date"]}
            return i
        out_days.append({
            "date": p["date"],
            "themes": [{"name": t["name"], "line": t["line"], "ids": [add(x) for x in t["items"]]} for t in p["themes"]],
            "others": [add(x) for x in p["others"]],
        })
    WEEK_INPUT.write_text(json.dumps({"start": days[-1]["date"], "end": days[0]["date"], "days": out_days, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    chars = sum(len(v["h"]) + 20 for v in items.values()) + sum(len(t["name"]) + len(t["line"]) for d in out_days for t in d["themes"])
    print(f"{len(out_days)}일, 소식 {len(items)}건 → {WEEK_INPUT} (Claude가 읽을 양 약 {chars // 3:,} 토큰)")


def week_problems() -> list[str]:
    if not WEEKLY.exists():
        return ["weekly.json이 없음"]
    w = json.loads(WEEKLY.read_text(encoding="utf-8"))
    inp = json.loads(WEEK_INPUT.read_text(encoding="utf-8"))
    items = inp["items"]
    errs = [f"{k} 없음" for k in ("start", "end", "headline", "points") if not w.get(k)]
    if (w.get("start"), w.get("end")) != (inp["start"], inp["end"]):
        errs.append(f"start·end는 week-input 값 그대로여야 함 ({inp['start']} ~ {inp['end']})")
    lo = 1 if len(inp["days"]) < 3 else 3  # 쌓인 날이 적을 때는 요점 1개부터
    if not lo <= len(w.get("points", [])) <= 5:
        errs.append(f"요점은 {lo}~5개여야 함 (지금 {len(w.get('points', []))}개, 쌓인 날 {len(inp['days'])}일)")
    for p in w.get("points", []):
        errs += [f"요점 '{p.get('name', '?')}'에 {k} 없음" for k in ("name", "line", "use", "ids") if not p.get(k)]
        errs += [f"요점 '{p.get('name')}'의 {i}가 week-input에 없음" for i in p.get("ids", []) if i not in items]
    return errs


def weekly_page(today: str | None) -> dict | None:
    if not (WEEKLY.exists() and WEEK_INPUT.exists() and today):
        return None
    w = json.loads(WEEKLY.read_text(encoding="utf-8"))
    if w.get("end", "") < (datetime.fromisoformat(today) - timedelta(days=6)).strftime("%Y-%m-%d"):
        return None  # 오래된 요약은 보여 주지 않는다
    items = json.loads(WEEK_INPUT.read_text(encoding="utf-8"))["items"]
    pts = [{"name": p["name"], "line": p["line"], "use": p["use"], "items": [items[i] for i in p["ids"] if i in items]} for p in w["points"]]
    return {"start": w["start"], "end": w["end"], "headline": w["headline"], "points": pts}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # 실패 메시지(sys.exit)도 한글이 깨지지 않게
    p = argparse.ArgumentParser(description="NewsRadar")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--days", type=int, default=2, help="며칠 전 글까지 볼지 (기본 2)")
    c.add_argument("--baseline", action="store_true")
    ck = sub.add_parser("check")
    ck.add_argument("--week", action="store_true", help="최근 7일 요약(weekly.json) 점검")
    sub.add_parser("render")
    sub.add_parser("week")
    sub.add_parser("where")
    a = p.parse_args()
    t0 = time.time()
    if a.cmd == "where":
        print(DATA)
        return
    if a.cmd == "collect":
        collect(a.days, a.baseline)
    elif a.cmd == "check":
        errs = week_problems() if a.week else problems(*load_digest(), load_profile()["categories"])
        print("문제 없음" if not errs else "\n".join(errs))
        sys.exit(1 if errs else 0)
    elif a.cmd == "week":
        week()
    else:
        render()
    print(f"({time.time() - t0:.1f}초)")


if __name__ == "__main__":
    main()
