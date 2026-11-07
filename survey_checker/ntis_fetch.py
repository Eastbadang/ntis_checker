#!/usr/bin/env python3
"""NTIS 국가R&D 검색 OpenAPI 간이 클라이언트 (수요조사서 점검 프로그램용).

ntis_api/ntis_search_client.py 진단에서 실제 동작이 확인된 'sample' 요청 방식만 사용합니다.
  - 과제검색 public_project / 논문·특허 public_result / 연구보고서 rresearchpdf
  - 인증키 파라미터는 모두 apprvKey, cmbnApiYn=Y
인증키는 환경변수 NTIS_APPRV_KEY 또는 프로그램 폴더의 .env 에서 읽고, 화면·파일에는 남기지 않습니다.
"""
from __future__ import annotations

import http.client
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import survey_core as core

BASE = "https://www.ntis.go.kr/rndopen/openApi/"
TIMEOUT = 20
INTERVAL = 0.3                      # 초당 30건 한도보다 충분히 낮게
SERVICES = {                        # mode: (경로, 컬렉션, 화면 표시 이름)
    "project": ("public_project", "project", "과제"),
    "paper": ("public_result", "rpaper", "논문"),
    "patent": ("public_result", "rpatent", "특허"),
    "report": ("rresearchpdf", "rresearchpdf", "보고서"),
}
SPAN_RE = re.compile(r"</?span[^>]*>")
TAG_RE = re.compile(r"<[^>]+>")
AMP_RE = re.compile(r"&(?!(?:[A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);)")
LEAF_RE = re.compile(r"<([A-Za-z_][\w.-]*)(\s[^<>]*?)?(?<!/)>([^<]*)</([A-Za-z_][\w.-]*)>")
ERROR_TAGS = {"error", "errmsg", "errormsg", "error_msg", "errormessage"}
TITLE_FIELDS = ("ProjectTitle_Korean", "ResultTitle_Korean", "ResultTitle", "Title_Korean", "Title")
ID_FIELDS = ("ProjectNumber", "ResultID", "TermSn", "ApplicationNumber", "RegistrationNumber")
YEAR_FIELDS = ("ProjectYear", "PubYear", "PublicationYear", "PublicationYm", "ApplicationDate", "RegistrationDate")
TEXT_PARTS = ("Goal", "Abstract", "Effect", "Keyword", "Contents")


class NtisError(RuntimeError):
    """네트워크 연결 자체가 실패한 경우."""


# ---------------------------------------------------------------- 인증키
def load_key(app_dir: Path) -> tuple[str, str]:
    """(인증키, 읽은 위치). 없으면 ("", "없음"). 형식이 잘못되면 RuntimeError (값은 출력하지 않음)."""
    key, src = os.environ.get("NTIS_APPRV_KEY", "").strip(), "환경변수 NTIS_APPRV_KEY"
    if not key:
        env = Path(app_dir) / ".env"
        src = f".env 파일 ({env})"
        if env.exists():
            for line in env.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line.startswith("NTIS_APPRV_KEY=") and not line.startswith("#"):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        return "", "없음"
    if any(not ch.isascii() or ch.isspace() or ch in "*<>\"'" for ch in key):
        raise RuntimeError(f"NTIS 인증키 형식이 올바르지 않습니다 (길이 {len(key)}자). 읽은 위치: {src}")
    return key, src


def mask(text: str, key: str) -> str:
    return text.replace(key, "***") if key and text else text


# ---------------------------------------------------------------- 요청
def build_params(mode: str, key: str, query: str, count: int = 10, start: int = 1) -> list[tuple]:
    _, coll, _ = SERVICES[mode]
    if mode == "report":
        return [("apprvKey", key), ("query", query), ("userId", ""), ("collection", coll),
                ("searchField", ""), ("displayCount", count), ("startPosition", start), ("naviCount", 30),
                ("sortby", ""), ("boostquery", ""), ("addQuery", ""), ("cmbnApiYn", "Y")]
    return [("naviCount", 30), ("searchField", ""), ("addQuery", ""), ("query", query),
            ("displayCount", count), ("sortby", ""), ("collection", coll), ("boostquery", ""),
            ("userId", ""), ("startPosition", start), ("apprvKey", key), ("cmbnApiYn", "Y")]


def build_url(mode: str, params: list[tuple]) -> str:
    return BASE + SERVICES[mode][0] + "?" + urllib.parse.urlencode(params, encoding="utf-8")


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={
        "Accept": "application/xml, text/xml, */*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return getattr(res, "status", 200) or 200, res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        finally:
            e.close()
        return e.code, body
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", None) or e.__class__.__name__
        raise NtisError(f"NTIS에 연결하지 못했습니다: {reason}") from None


# ---------------------------------------------------------------- 응답 해석
def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", SPAN_RE.sub("", text or "")).strip()


def _repair(text: str) -> str:
    text = AMP_RE.sub("&amp;", text)

    def fix(m: re.Match) -> str:
        o, attrs, inner, c = m.groups()
        return m.group(0) if o == c else f"<{o}{attrs or ''}>{inner}</{o}>"

    return LEAF_RE.sub(fix, text)


def _flatten(el: ET.Element, prefix: str, out: dict) -> None:
    kids = list(el)
    if not kids:
        val = _clean(el.text)
        if val:
            out.setdefault(prefix, []).append(val)
        return
    for c in kids:
        _flatten(c, f"{prefix}_{c.tag}" if prefix else c.tag, out)


def _to_int(text: str | None) -> int:
    t = (text or "").strip()
    return int(t) if t.isdigit() else 0


def classify(status: int, body: str) -> tuple[str, int, list[dict], str]:
    """(판정, 전체건수, HIT 행 목록, 메시지). 판정: 정상/결과없음/오류응답/HTTP오류/XML아님"""
    text = SPAN_RE.sub("", (body or "").strip().lstrip(core.BOM).strip())
    try:
        root = ET.fromstring(text.encode("utf-8")) if text else None
    except ET.ParseError:
        try:
            root = ET.fromstring(_repair(text).encode("utf-8"))
        except ET.ParseError:
            root = None
    if root is not None and isinstance(root.tag, str) and root.tag.lower() == "html":
        root = None
    err = None
    if root is not None:
        for el in root.iter():
            if isinstance(el.tag, str) and el.tag.lower() in ERROR_TAGS:
                err = _clean("".join(el.itertext())) or el.tag
                break
    if status >= 400:
        return "HTTP오류", 0, [], f"HTTP {status}" + (f": {err or _clean(TAG_RE.sub(' ', text))[:150]}")
    if root is None:
        return "XML아님", 0, [], _clean(TAG_RE.sub(" ", text))[:150] or "(빈 응답)"
    if err:
        return "오류응답", 0, [], err[:200]
    total = 0
    for c in root.iter("COLCOUNT"):
        total = _to_int(c.text)
        break
    if total == 0:
        total = _to_int(root.findtext("TOTALHITS"))
    rows = []
    for hit in root.iter("HIT"):
        out: dict = {}
        _flatten(hit, "", out)
        rows.append({k.lstrip("_"): ";".join(v) for k, v in out.items()})
    return ("정상" if rows or total else "결과없음"), total, rows, ""


def hit_to_item(mode: str, row: dict) -> core.RefItem | None:
    title = next((row[k] for k in TITLE_FIELDS if row.get(k)), "")
    if not title:   # 서비스별 필드 이름이 다를 때: 'Title'로 끝나는 첫 필드 (논문의 지원과제명은 제외)
        title = next((v for k, v in row.items()
                      if k.endswith("Title") and not k.startswith("Project") and v), "")
    if not title:
        return None
    ref_id = next((row[k] for k in ID_FIELDS if row.get(k)), "")
    year = next((core._year(row[k]) for k in YEAR_FIELDS if row.get(k)), "")
    text = " ".join(v for k, v in row.items() if any(p in k for p in TEXT_PARTS))[:1500]
    return core.RefItem(title=title, kind=SERVICES[mode][2], ref_id=ref_id, year=year, source="NTIS", text=text)


# ---------------------------------------------------------------- 수요조사서 기준 검색
def search_survey(survey: core.Survey, key: str, modes=("project", "paper", "patent", "report"),
                  count: int = 10, progress=None) -> tuple[list[core.RefItem], list[str]]:
    """수요조사서 키워드로 서비스별 검색. 결과가 없으면 검색어를 줄여 다시 시도.
    반환: (비교 항목, 진행 기록). 인증·IP 오류가 나면 나머지 서비스는 건너뜀."""
    say = progress or (lambda _m: None)
    queries = core.build_queries(survey)
    if not queries:
        return [], ["NTIS: 기술명·키워드에서 검색어를 만들지 못했습니다."]
    items: list[core.RefItem] = []
    notes: list[str] = []
    for mode in modes:
        label = SERVICES[mode][2]
        found = False
        for q in queries:
            say(f"NTIS {label} 검색 중: {q}")
            try:
                status, body = fetch(build_url(mode, build_params(mode, key, q, count)))
            except NtisError as e:
                notes.append(str(e))
                return items, notes
            verdict, total, rows, msg = classify(status, mask(body, key))
            time.sleep(INTERVAL)
            if verdict == "오류응답":           # 인증키·IP 오류는 모든 서비스에 같으므로 중단
                notes.append(f"NTIS 검색 중단: {mask(msg, key)}")
                return items, notes
            if verdict in ("HTTP오류", "XML아님"):
                notes.append(f"NTIS {label}: 응답 오류 ({mask(msg, key)})")
                break
            if rows:
                got = [it for it in (hit_to_item(mode, r) for r in rows) if it]
                items += got
                notes.append(f"NTIS {label}: 검색어 '{q}' {total:,}건 중 상위 {len(got)}건 비교")
                found = True
                break
        else:
            if not found:
                notes.append(f"NTIS {label}: 검색 결과 없음")
    return items, notes
