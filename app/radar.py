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
    uv run radar.py test-source [--save] Claude의 출처 제안(<data>/recommend.json)을 하나씩 시험 → recommend-result.json
    uv run radar.py setup                설정 화면 (127.0.0.1에서만 열림)
    uv run radar.py run                  매일 실행: 수집 → Claude 정리 → 페이지. 로그는 <data>/logs/

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
from concurrent.futures import ThreadPoolExecutor, as_completed
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


TITLE_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "with", "as", "by", "at", "is", "are", "its", "it",
              "from", "how", "what", "new", "your", "you", "into", "over", "after", "says", "say", "about", "now", "more"}
SIMILAR = 0.45  # M2 시험(4분야 후보)에서 이 값 이상은 모두 같은 소식, 바로 아래부터 다른 기사가 섞였다


def title_key(title: str) -> set[str]:
    """제목 비교용 조각: 한글은 두 글자씩(띄어쓰기·조사가 달라도 맞게), 영어·숫자는 낱말.
    '[게시판]' 같은 머리표와 ' - 언론사' 꼬리는 뺀다."""
    t = re.sub(r"\[[^\]]{1,12}\]", " ", title.lower())
    t = re.sub(r"\s+-\s+[^-]{1,20}$", "", t)
    key = set()
    for run in re.findall(r"[가-힣]+", t):
        key |= {run[i:i + 2] for i in range(len(run) - 1)}
    key |= {w for w in re.findall(r"[0-9a-z]+", t) if len(w) > 1 and w not in TITLE_STOP}
    return key


def similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    return 2 * len(a & b) / (len(a) + len(b)) >= SIMILAR


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
        "survey": survey_info(),
        "login_days": login_warning(),
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


# ── 출처 시험 (설정 때: Claude 제안 → 하나씩 실제로 받아 보기 → 통과한 것만) ──

PROPOSAL = DATA / "recommend.json"        # Claude가 쓴 제안
TEST_RESULT = DATA / "recommend-result.json"
FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml", "/feed/")
TEST_TIMEOUT = 15
SITE_RECENT_DAYS = 30
NEWS_TEST_DAYS = 7


def entry_time(e) -> datetime | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def as_feed(body: bytes):
    parsed = feedparser.parse(body)
    return parsed if parsed.entries and (parsed.get("version") or parsed.feed.get("title")) else None


def alt_feed_links(body: bytes, base: str) -> list[str]:
    """페이지의 <link rel="alternate" type="application/rss+xml" href="..."> 주소들"""
    out = []
    for tag in re.findall(r"<link\b[^>]*>", body.decode("utf-8", "replace"), re.I):
        if re.search(r'type=["\']application/(rss|atom)\+xml["\']', tag, re.I):
            m = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
            if m:
                out.append(urljoin(base, html.unescape(m.group(1))))
    return out


def judge_feed(parsed, now: datetime) -> tuple[bool, int, str]:
    """(통과, 최근 30일 글 수, 이유)"""
    times = [entry_time(e) for e in parsed.entries]
    recent = sum(1 for t in times if t and t >= now - timedelta(days=SITE_RECENT_DAYS))
    if recent:
        return True, recent, ""
    if not any(times):
        return True, len(parsed.entries), "글에 날짜가 없음 (처음 며칠은 옛 글이 섞일 수 있음)"
    return False, 0, f"최근 {SITE_RECENT_DAYS}일 동안 새 글이 없음"


def test_site(site: dict) -> dict:
    """주소가 RSS인지 → 페이지가 알려 주는 RSS → 흔한 RSS 주소 순서로 찾는다."""
    now = datetime.now(timezone.utc)
    url = site["url"]
    try:
        body = fetch(url, timeout=TEST_TIMEOUT)
    except Exception as e:
        return {"ok": False, "reason": f"사이트가 열리지 않음 ({type(e).__name__}: {e})"[:160]}
    tries = [(url, body)]
    seen = {url}
    for u in alt_feed_links(body, url) + [urljoin(url, p) for p in FEED_PATHS]:
        if u not in seen:
            seen.add(u)
            tries.append((u, None))
    last = "RSS를 찾지 못함"
    for u, b in tries:
        try:
            parsed = as_feed(b if b is not None else fetch(u, timeout=TEST_TIMEOUT))
        except Exception:
            continue
        if not parsed:
            continue
        ok, recent, note = judge_feed(parsed, now)
        if ok:
            return {"ok": True, "feed": u, "recent": recent, "note": note}
        last = note
    return {"ok": False, "reason": last}


def test_news(src: dict) -> dict:
    try:
        parsed = feedparser.parse(fetch(news_url(src["q"], src.get("lang"), f"{NEWS_TEST_DAYS}d"), timeout=TEST_TIMEOUT))
    except Exception as e:
        return {"ok": False, "reason": f"검색이 되지 않음 ({type(e).__name__})"}
    try:
        must = news_must(src)
    except re.error as e:
        return {"ok": False, "reason": f"제목 확인 단어(must)가 잘못됨: {e}"}
    hits = [e for e in parsed.entries if must.search(e.get("title", ""))]
    if not hits:
        why = "검색 결과가 없음" if not parsed.entries else f"검색 결과 {len(parsed.entries)}건 중 제목에 맞는 단어가 있는 기사가 없음"
        return {"ok": False, "reason": f"지난 {NEWS_TEST_DAYS}일 {why}"}
    return {"ok": True, "week": len(hits), "sample": clean(hits[0].get("title", ""))[:80]}


def test_sources(proposal_path: Path, save: bool, on_progress=None) -> dict:
    """제안을 하나씩 시험해 recommend-result.json을 쓰고 결과를 돌려준다.
    on_progress(done, total, 이름, 통과 여부)가 있으면 하나 끝날 때마다 부른다 (설정 화면 진행 표시)."""
    prop = json.loads(proposal_path.read_text(encoding="utf-8"))
    catalog = load_catalog()
    passed = {"catalog": [], "feeds": [], "news": []}
    failed = []
    for cid in prop.get("catalog") or []:
        if cid in catalog:
            passed["catalog"].append(cid)  # catalog는 이미 시험을 통과한 목록
        else:
            failed.append({"kind": "catalog", "name": cid, "reason": "목록(catalog)에 없는 id"})

    jobs = [("news", s) for s in prop.get("news") or []] + [("site", s) for s in prop.get("sites") or []]
    total = len(jobs)
    print(f"시험할 출처 {total}곳 (목록에서 고른 {len(passed['catalog'])}곳은 시험을 건너뜀)", flush=True)
    if on_progress:
        on_progress(0, total, "", True)
    with ThreadPoolExecutor(8) as pool:
        futs = {pool.submit(test_news if k == "news" else test_site, s): (k, s) for k, s in jobs}
        done = 0
        for f in as_completed(futs):
            k, s = futs[f]
            r = f.result()
            done += 1
            label = f"뉴스 검색 \"{s['q']}\"" if k == "news" else s["name"]
            print(f"  [{done}/{total}] {'통과' if r['ok'] else '안 됨'} {label}" + ("" if r["ok"] else f" — {r['reason']}"), flush=True)
            if on_progress:
                on_progress(done, total, label, r["ok"])
            if not r["ok"]:
                failed.append({"kind": k, "name": label, "url": s.get("url") or s.get("q"), "reason": r["reason"]})
            elif k == "news":
                keep = {x: s[x] for x in ("q", "lang", "must", "org", "tier", "max") if s.get(x)}
                passed["news"].append(dict(keep, tier=s.get("tier", 2), week=r["week"], sample=r["sample"]))
            else:
                passed["feeds"].append({"name": s["name"], "url": r["feed"], "org": s.get("org", ""), "tier": s.get("tier", 2),
                                        "filter": bool(s.get("filter")), "why": s.get("why", ""), "recent": r["recent"], "note": r["note"]})
    n_pass = len(passed["catalog"]) + len(passed["feeds"]) + len(passed["news"])
    profile_patch = {k: prop[k] for k in ("categories", "keywords", "exclude", "search") if prop.get(k)}
    result = {"tested_at": now_kst().isoformat(timespec="minutes"), "passed": passed, "failed": failed,
              "profile": profile_patch, "summary": {"passed": n_pass, "failed": len(failed)}}
    TEST_RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"통과 {n_pass}곳, 안 됨 {len(failed)}곳 → {TEST_RESULT}")
    if save:
        save_sources(result)
    return result


def save_sources(result: dict) -> None:
    """시험 결과 중 통과한 것만 sources.yaml로, 분류·거르는 단어는 profile.yaml에 합친다."""
    p = result["passed"]
    clean_feed = lambda s: {k: v for k, v in s.items() if k in ("name", "url", "org", "tier", "filter", "max") and v not in ("", None, False)}
    clean_news = lambda s: {k: v for k, v in s.items() if k in ("q", "lang", "must", "org", "tier", "max") and v not in ("", None)}
    sources = {"catalog": p["catalog"], "feeds": [clean_feed(s) for s in p["feeds"]], "pages": [], "news": [clean_news(s) for s in p["news"]]}
    SOURCES.write_text("# NewsRadar 출처 (설정 때 시험을 통과한 것만)\n" + yaml.safe_dump(sources, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    profile = read_yaml(PROFILE) if PROFILE.exists() else {}
    profile.update(result.get("profile") or {})
    PROFILE.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    print(f"저장: {SOURCES}, {PROFILE}")


# ── 설치 정보·만든 사람에게 전달 (구글 폼) ──
# 동료가 [시작하기]를 누를 때, 2주·한 달째 질문에 답할 때만 보낸다. 화면에 미리 알린다. 이름·메일·하는 일은 보내지 않는다.

VERSION = "0.1.0"
INSTALL = DATA / "install.json"
LOGS = DATA / "logs"
FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSfZVjXTg2wbH3S6rlvuVXuiFjMQd8nT62xwOkCa13t3qQ_JaQ/formResponse"
FORM_FIELDS = {"id": "entry.1998543362", "stage": "entry.2884039", "field": "entry.1116709699", "minutes": "entry.1089926017",
               "sources": "entry.1087978654", "days": "entry.423011386", "date": "entry.1233052819", "version": "entry.1240549935"}
MINUTES_CHOICES = ["거의 안 봐요", "10분 안쪽", "10~30분", "30분~1시간", "1시간 넘게"]
SURVEY_STAGES = (("한달", 30), ("2주", 14))  # (단계, 설치 뒤 며칠째부터)


def load_install() -> dict:
    return json.loads(INSTALL.read_text(encoding="utf-8")) if INSTALL.exists() else {}


def save_install(info: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    INSTALL.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")


def submit_form(values: dict) -> bool:
    """구글 폼에 한 줄 보낸다. 실패해도 설치·정리에는 영향 없음. 시험할 때는 NEWSRADAR_FORM_OFF=1로 끈다."""
    if os.environ.get("NEWSRADAR_FORM_OFF"):
        return False
    from urllib.parse import urlencode
    body = urlencode({FORM_FIELDS[k]: str(v) for k, v in values.items() if k in FORM_FIELDS}).encode("ascii")  # 한글은 UTF-8로
    try:
        req = Request(FORM_URL, data=body, headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
        with urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def digest_days(since: str) -> int:
    return sum(1 for f in ARCHIVE.glob("*.json") if f.stem >= since) if ARCHIVE.exists() else 0


def survey_info() -> dict | None:
    """정리 페이지 맨 위 질문(2주·한 달째)에 필요한 것. 페이지가 답했는지를 기억한다."""
    info = load_install()
    if not info.get("id"):
        return None
    days = (now_kst().date() - datetime.fromisoformat(info["installed"]).date()).days
    due = next((stage for stage, d in SURVEY_STAGES if days >= d), None)
    try:
        n_sources = sum(len(v) for v in load_sources()[0].values())
    except SystemExit:
        n_sources = 0
    profile = read_yaml(PROFILE) if PROFILE.exists() else {}
    return {"id": info["id"], "due": due, "field": profile.get("field", ""), "sources": n_sources,
            "days": digest_days(info["installed"]), "version": VERSION,
            "form": FORM_URL, "entries": FORM_FIELDS, "choices": MINUTES_CHOICES}


# ── Claude 실행 ──

def prompt_text(*names: str) -> str:
    radar = f'uv run "{APP / "radar.py"}"'
    text = "\n\n".join((APP / "prompts" / f"{n}.md").read_text(encoding="utf-8") for n in names)
    return text.replace("{RADAR}", radar).replace("{CATALOG}", str(CATALOG))


# NewsRadar 전용 Claude Code 설정 폴더. 로그인 정보도 여기 저장된다(claude auth login).
# 그 PC의 개인 CLAUDE.md·메모리·설정이 정리에 섞이지 않게 하려는 것 (--restricted로는 CLAUDE.md가 막히지 않았다, 2026-11-13 시험)
CLAUDE_HOME = DATA / "claude-home"


def claude_exe() -> str | None:
    import shutil
    return shutil.which("claude")


def claude_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")}  # API 요금 방지
    env["CLAUDE_CONFIG_DIR"] = str(CLAUDE_HOME)
    env["NEWSRADAR_DATA"] = str(DATA)
    return env


def auth_status() -> dict:
    """{'loggedIn': bool, 'loginExpiresAt': ...}. Claude Code가 없으면 {'missing': True}."""
    import subprocess
    exe = claude_exe()
    if not exe:
        return {"missing": True, "loggedIn": False}
    CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run([exe, "auth", "status", "--json"], env=claude_env(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return json.loads(p.stdout)
    except Exception:
        return {"loggedIn": False}


def run_claude(prompt: str, tools: str, timeout: int = 900) -> tuple[bool, str]:
    """claude -p를 데이터 폴더에서, 전용 설정 폴더로 실행한다. 도구는 tools로 제한."""
    import subprocess
    exe = claude_exe()
    if not exe:
        return False, "Claude Code가 설치되어 있지 않음"
    env = claude_env()
    model = (read_yaml(PROFILE) if PROFILE.exists() else {}).get("model") or "claude-sonnet-5-5"
    try:
        p = subprocess.run([exe, "-p", "--model", model, "--allowedTools", tools, "--no-session-persistence"],
                           input=prompt, cwd=DATA, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"Claude가 {timeout // 60}분 안에 끝내지 못함"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out.strip()[-2000:]


def recommend(on_progress=None, min_pass: int = 8) -> dict:
    """Claude가 출처를 제안 → 프로그램이 하나씩 시험. 통과가 적으면 한 번만 다시 제안받는다."""
    for f in (PROPOSAL, TEST_RESULT):
        f.unlink(missing_ok=True)
    result = {}
    for attempt in (1, 2):
        if on_progress:
            on_progress(0, 0, "claude", True)
        ok, out = run_claude(prompt_text("recommend"), "Read,Write,Glob", timeout=600)
        if not PROPOSAL.exists():
            raise RuntimeError(f"Claude가 추천을 쓰지 못함: {out[-300:]}")
        result = test_sources(PROPOSAL, False, on_progress)
        if result["summary"]["passed"] >= min_pass:
            break
    return result


# ── 매일 실행 (작업 스케줄러가 부르는 것: 수집 → Claude 정리 → 페이지) ──

def log(msg: str) -> None:
    line = f"[{now_kst().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / f"{now_kst().strftime('%Y-%m-%d')}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


AUTH_NOTE = DATA / "claude-auth.json"  # 마지막으로 확인한 로그인 상태 (페이지가 만료 전에 미리 알리는 데 씀)
LOGIN_WARN_DAYS = 5


def schedule(at: str) -> bool:
    """매일 at(HH:MM)에 정리하도록 OS 예약을 건다. 시험할 때는 NEWSRADAR_NO_SCHEDULE=1로 끈다."""
    import subprocess
    if os.environ.get("NEWSRADAR_NO_SCHEDULE"):
        return False
    if sys.platform == "win32":
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(APP / "tools" / "install-task.ps1"), "-At", at], capture_output=True, timeout=120)
        return p.returncode == 0
    return False  # Mac(launchd)은 M7


def login_warning() -> int | None:
    """로그인이 며칠 뒤 끝나는지 (5일 안쪽일 때만, 이미 풀렸으면 0)."""
    try:
        note = json.loads(AUTH_NOTE.read_text(encoding="utf-8"))
        if not note.get("loggedIn"):
            return 0
        exp = note.get("expires")
        days = (datetime.fromisoformat(exp.replace("Z", "+00:00")) - datetime.now(timezone.utc)).days
        return max(days, 0) if days < LOGIN_WARN_DAYS else None
    except Exception:
        return None


def run_daily() -> int:
    """성공 0, 실패 1, Claude 연결이 풀림 3. 오늘 보관분(archive/<오늘>.json)이 생겼는지로 판단한다."""
    today = now_kst().strftime("%Y-%m-%d")
    log(f"시작 (NewsRadar {VERSION}, 데이터 {DATA})")
    try:
        collect(2, False)
    except SystemExit as e:
        if e.code:
            log(f"수집 실패 또는 후보 없음: {e.code}")
            return 1
    auth = auth_status()
    AUTH_NOTE.write_text(json.dumps({"loggedIn": bool(auth.get("loggedIn")), "expires": auth.get("loginExpiresAt"),
                                     "checked": today}, ensure_ascii=False), encoding="utf-8")
    if not auth.get("loggedIn"):
        log("Claude 연결이 풀림 (설정 화면에서 다시 연결해야 함)" if not auth.get("missing") else "Claude Code가 설치되어 있지 않음")
        if SITE.exists():
            write_site(load_profile())  # 페이지 맨 위에 '다시 연결해 주세요'가 보이게
        return 3  # 실행 스크립트가 '다시 연결해 주세요' 알림을 보낸다
    profile = load_profile()
    names = ("daily", "weekly") if profile.get("weekly", True) else ("daily",)
    ok, out = run_claude(prompt_text(*names), "Bash,Read,Write,Edit,Glob,Grep")
    log(f"Claude 정리 {'끝' if ok else '실패'}: {out[-500:]}")
    done = (ARCHIVE / f"{today}.json").exists()
    log("성공" if done else "실패: 오늘 정리가 만들어지지 않음")
    return 0 if done else 1


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
    ts = sub.add_parser("test-source", help="Claude의 출처 제안(recommend.json)을 하나씩 시험")
    ts.add_argument("proposal", nargs="?", help=f"제안 파일 (기본 {PROPOSAL.name})")
    ts.add_argument("--save", action="store_true", help="통과한 것을 sources.yaml·profile.yaml에 저장")
    sub.add_parser("run", help="매일 실행: 수집 → Claude 정리 → 페이지 (작업 스케줄러가 부름)")
    sp = sub.add_parser("setup", help="설정 화면 열기 (이 PC 안에서만 열리는 화면)")
    sp.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않음 (시험용)")
    a = p.parse_args()
    t0 = time.time()
    if a.cmd == "where":
        print(DATA)
        return
    if a.cmd == "run":
        sys.exit(run_daily())
    if a.cmd == "setup":
        from setup_server import serve
        serve(open_browser=not a.no_browser)
        return
    if a.cmd == "test-source":
        test_sources(Path(a.proposal) if a.proposal else PROPOSAL, a.save)
    elif a.cmd == "collect":
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
