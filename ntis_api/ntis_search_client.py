#!/usr/bin/env python3
"""NTIS 국가R&D 검색 OpenAPI 클라이언트 (과제검색 / 성과검색 / 연구보고서).

[요청 방식 프로필]
NTIS 매뉴얼은 같은 서비스에 대해 '요청명세 표'와 '요청 예시 URL'의 파라미터가 서로 다릅니다.
  sample        : 매뉴얼 요청 예시 URL 형식 (query, searchField, displayCount, naviCount, cmbnApiYn=Y)
                  연구보고서는 apprvKey, collection=rresearchpdf
  sample_nocmbn : sample 에서 cmbnApiYn 만 뺀 형식
  spec          : 요청명세 표 / NTIS TEST URL 화면 형식
                  과제검색은 SRWR, searchFd, displayCnt / 연구보고서는 userKey, collection=researchpdf
  spec_cmbn     : spec 에 cmbnApiYn=Y 를 더한 형식
어떤 형식이 실제로 동작하는지는 --probe 로 확인한 뒤,
.env 의 NTIS_SEARCH_PROFILE 또는 --profile 로 지정합니다. (기본: sample)

[사용 예]
  python ntis_search_client.py --mode all --probe            # 서비스별·프로필별 동작 진단 (먼저 실행)
  python ntis_search_client.py --mode project --text 나노
  python ntis_search_client.py --mode project --pjt-id 1425118980
  python ntis_search_client.py --mode paper --queries 검색어.csv --count 50 --max-pages 2

인증키는 코드에 넣지 않고 .env 또는 환경변수 NTIS_APPRV_KEY 에서 읽습니다.
화면 출력, 진단 결과, 원본 응답 파일에는 인증키를 *** 로 가려서 남깁니다.
검색어 CSV: '검색어' 열(없으면 첫 열). '기준ID' 열이 있으면 결과에 함께 기록합니다.
"""
from __future__ import annotations

import argparse
import csv
import http.client
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "https://www.ntis.go.kr/rndopen/openApi/"
TIMEOUT = 20
REQUEST_INTERVAL = 0.3   # 초당 최대 30tps 한도보다 충분히 낮게 유지
DEFAULT_COUNT = 10       # 매뉴얼에 1회 최대 건수가 명확하지 않아 보수적으로 설정 (--count 로 조정)
DEFAULT_PROFILE = "sample"
PROFILES = ("sample", "sample_nocmbn", "spec", "spec_cmbn")
PROBE_QUERY = "나노"      # 매뉴얼 예시·TEST URL 화면과 같은 검색어

SERVICES = {
    "project": {"path": "public_project", "collection": "project", "label": "과제검색"},
    "paper":   {"path": "public_result", "collection": "rpaper", "label": "논문검색"},
    "patent":  {"path": "public_result", "collection": "rpatent", "label": "특허검색"},
    "equip":   {"path": "public_result", "collection": "requip", "label": "연구시설장비검색"},
    "report":  {"path": "rresearchpdf", "collection": "researchpdf", "label": "연구보고서검색",
                "sample_collection": "rresearchpdf"},
}
SPAN_RE = re.compile(r"</?span[^>]*>")
TAG_RE = re.compile(r"<[^>]+>")
ERROR_TAGS = {"error", "errmsg", "errormsg", "error_msg", "errormessage"}
# NTIS 응답의 XML 형식 오류 보정용 (연구보고서: <DocUrl>…</NrmsUrl> 처럼 닫는 태그 불일치, URL 안의 & 등)
AMP_RE = re.compile(r"&(?!(?:[A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);)")
LEAF_RE = re.compile(r"<([A-Za-z_][\w.-]*)(\s[^<>]*?)?(?<!/)>([^<]*)</([A-Za-z_][\w.-]*)>")
BOM = chr(0xFEFF)
# 개인정보(연구책임자·참여연구원·저자·발명자 성명) 항목: 결과 CSV에서 기본 제외 (--keep-personal 로 포함)
PERSONAL_KEYS = ("Manager", "Researchers_Name", "Author", "Inventor")


def drop_personal(row: dict) -> dict:
    return {k: v for k, v in row.items() if not any(p in k for p in PERSONAL_KEYS)}


class NtisError(RuntimeError):
    """네트워크 수준에서 호출이 실패한 경우."""


# ---------------------------------------------------------------- 설정 / 보안
KEY_SOURCE = ""  # 인증키를 읽은 위치 (진단 표시용)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    # 메모장이 붙이는 BOM 이 있어도 첫 줄 이름을 읽도록 utf-8-sig 사용
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_api_key() -> str:
    global KEY_SOURCE
    preset = "NTIS_APPRV_KEY" in os.environ  # .env 보다 먼저 설정된 시스템/PyCharm 환경변수
    _load_dotenv(HERE / ".env")
    KEY_SOURCE = ("시스템 또는 PyCharm 환경변수 (.env 보다 우선 적용됨)" if preset
                  else f".env 파일 ({HERE / '.env'})")
    key = os.environ.get("NTIS_APPRV_KEY", "").strip()
    if not key:
        raise RuntimeError("NTIS_APPRV_KEY가 없습니다. .env 파일(또는 환경변수)을 확인하세요.")
    bad = sorted({ch for ch in key if not ch.isascii() or ch.isspace() or ch in "*<>\"'"})
    if bad:  # "삭제", "***", 자리표시자 문구처럼 키가 아닌 값
        raise RuntimeError(f"NTIS_APPRV_KEY 값이 인증키 형식이 아닙니다 (길이 {len(key)}자, "
                           f"{describe_chars(bad)} 포함). 읽은 위치: {KEY_SOURCE}")
    return key


def get_profile(cli_value: str | None) -> str:
    _load_dotenv(HERE / ".env")
    value = (cli_value or os.environ.get("NTIS_SEARCH_PROFILE", "") or DEFAULT_PROFILE).strip()
    if value not in PROFILES:
        raise SystemExit(f"알 수 없는 프로필: {value} (사용 가능: {', '.join(PROFILES)})")
    return value


def describe_chars(chars) -> str:
    """키에 섞인 비정상 문자의 '종류'만 설명 (키 값은 출력하지 않음)."""
    kinds = []
    for ch in chars:
        if "가" <= ch <= "힣" or "ㄱ" <= ch <= "ㅣ":
            kinds.append("한글")
        elif ch.isspace():
            kinds.append("공백")
        elif ch == "*":
            kinds.append("별표(*)")
        elif ch.isascii():
            kinds.append(f"기호({ch})")
        else:
            kinds.append("기타 비ASCII 문자")
    return ", ".join(dict.fromkeys(kinds))


def mask(text: str, secret: str) -> str:
    """출력·저장 전에 인증키를 *** 로 가림."""
    return text.replace(secret, "***") if secret and text else text


# ---------------------------------------------------------------- 요청 생성
def build_params(mode: str, profile: str, api_key: str, query: str, addquery: str = "",
                 count: int = DEFAULT_COUNT, start: int = 1, search_field: str = "") -> list[tuple]:
    """프로필별 요청 파라미터 (매뉴얼 순서 유지, 빈 값도 그대로 전송)."""
    s = SERVICES[mode]
    if profile.startswith("spec"):
        if mode == "report":
            # 매뉴얼 표는 userKey 이지만 NTIS 서버가 인식하지 않음(2026-10-30 진단: "인증키 정보가 없습니다").
            # 키 파라미터만 apprvKey 로 보내고, 나머지는 표 형식을 유지
            params = [("apprvKey", api_key), ("userId", ""), ("collection", s["collection"]),
                      ("query", query), ("searchField", search_field), ("addQuery", addquery),
                      ("startPosition", start), ("displayCount", count)]
        elif mode == "project":
            params = [("apprvKey", api_key), ("userId", ""), ("collection", s["collection"]),
                      ("SRWR", query), ("searchFd", search_field), ("addQuery", addquery),
                      ("searchRnkn", ""), ("startPosition", start), ("displayCnt", count)]
        else:
            params = [("apprvKey", api_key), ("userId", ""), ("collection", s["collection"]),
                      ("SRWR", query), ("searchField", search_field), ("addQuery", addquery),
                      ("startPosition", start), ("displayCount", count)]
        if profile == "spec_cmbn":
            params.append(("cmbnApiYn", "Y"))
    else:
        if mode == "report":
            params = [("apprvKey", api_key), ("query", query), ("userId", ""),
                      ("collection", s.get("sample_collection", s["collection"])),
                      ("searchField", search_field), ("displayCount", count),
                      ("startPosition", start), ("naviCount", 30), ("sortby", ""),
                      ("boostquery", ""), ("addQuery", addquery)]
        else:
            params = [("naviCount", 30), ("searchField", search_field), ("addQuery", addquery),
                      ("query", query), ("displayCount", count), ("sortby", ""),
                      ("collection", s["collection"]), ("boostquery", ""), ("userId", ""),
                      ("startPosition", start), ("apprvKey", api_key)]
        if profile == "sample":
            params.append(("cmbnApiYn", "Y"))
    return params


def build_url(mode: str, params: list[tuple]) -> str:
    return BASE + SERVICES[mode]["path"] + "?" + urllib.parse.urlencode(params, encoding="utf-8")


def masked_url(mode: str, params: list[tuple], api_key: str) -> str:
    """사람이 읽을 수 있는 요청 주소 (인증키는 ***)."""
    return mask(urllib.parse.unquote_plus(build_url(mode, params)), api_key)


def fetch(mode: str, params: list[tuple]) -> tuple[int, str]:
    """(HTTP 상태코드, 본문). HTTP 오류도 상태코드와 본문을 돌려줍니다."""
    req = urllib.request.Request(build_url(mode, params), headers={
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
            e.close()  # 오류 응답 연결도 닫음
        return e.code, body
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        # 연결 실패·시간 초과·연결 끊김 (URLError 외 TimeoutError 등 포함)
        reason = getattr(e, "reason", None) or e.__class__.__name__
        raise NtisError(f"네트워크 오류 ({SERVICES[mode]['label']}): {reason}") from None


# ---------------------------------------------------------------- 응답 해석
def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", SPAN_RE.sub("", text or "")).strip()


def _to_int(text: str | None) -> int:
    t = (text or "").strip()
    return int(t) if t.isdigit() else 0


def _flatten(el: ET.Element, prefix: str, out: dict) -> None:
    kids = list(el)
    if not kids:
        val = _clean(el.text)
        code = el.get("code")
        if val:
            out.setdefault(prefix, []).append(val)
        if code is not None and code != "":
            out.setdefault(prefix + "_코드", []).append(code)
        return
    for c in kids:
        _flatten(c, f"{prefix}_{c.tag}" if prefix else c.tag, out)


def find_error(root: ET.Element) -> str | None:
    """<error>…</error> 같은 NTIS 오류 응답이면 메시지를 돌려줌."""
    for el in root.iter():  # root 자신 포함
        if isinstance(el.tag, str) and el.tag.lower() in ERROR_TAGS:
            txt = _clean("".join(el.itertext()))
            return txt or el.tag
    return None


def parse_root(root: ET.Element) -> tuple[int, list[dict]]:
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
        row = {k.lstrip("_"): (v[0] if len(v) == 1 else ";".join(v)) for k, v in out.items()}
        row["HIT순번"] = hit.get("NO", "")
        rows.append(row)
    return total, rows


def parse_hits(xml_text: str) -> tuple[int, list[dict]]:
    """(전체 건수, HIT별 평탄화 dict 목록). XML이 아니면 ET.ParseError."""
    return parse_root(ET.fromstring(SPAN_RE.sub("", xml_text).encode("utf-8")))


def _repair_xml(text: str) -> str:
    """흔한 XML 형식 오류 보정: 이스케이프 안 된 &, 여는·닫는 태그 이름이 다른 단말 요소."""
    text = AMP_RE.sub("&amp;", text)

    def fix(m: re.Match) -> str:
        open_tag, attrs, inner, close_tag = m.groups()
        if open_tag == close_tag:
            return m.group(0)
        return f"<{open_tag}{attrs or ''}>{inner}</{open_tag}>"

    return LEAF_RE.sub(fix, text)


def classify(status: int, body: str) -> tuple[str, int, list[dict], str]:
    """응답 판정 → (판정, 전체건수, 결과행, 메시지).
    판정: 정상 / 결과없음 / 오류응답 / HTTP오류 / XML아님"""
    text = (body or "").strip().lstrip(BOM).strip()  # 앞쪽 공백·BOM 제거
    # 연구보고서는 검색어 강조를 실제 <span> 태그로 보내 문장이 쪼개지므로 해석 전에 제거
    text = SPAN_RE.sub("", text)
    note = ""
    try:
        root = ET.fromstring(text.encode("utf-8")) if text else None
    except ET.ParseError:
        try:  # NTIS 응답의 형식 오류를 보정해 한 번 더 시도
            root = ET.fromstring(_repair_xml(text).encode("utf-8"))
            note = "XML 형식 오류를 보정해 해석함"
        except ET.ParseError:
            root = None
    if root is not None and isinstance(root.tag, str) and root.tag.lower() == "html":
        root = None  # 형식상 XML이어도 HTML 페이지는 API 응답이 아님
    if status >= 400:
        err = find_error(root) if root is not None else None
        detail = err or _clean(TAG_RE.sub(" ", body))[:200]
        return "HTTP오류", 0, [], f"HTTP {status}" + (f": {detail}" if detail else "")
    if root is None:
        return "XML아님", 0, [], _clean(TAG_RE.sub(" ", body))[:200] or "(빈 응답)"
    err = find_error(root)
    if err:
        return "오류응답", 0, [], err[:200]
    total, rows = parse_root(root)
    return ("정상" if total or rows else "결과없음"), total, rows, note


# ---------------------------------------------------------------- 수집 / 진단
def run(mode: str, items: list[tuple[str, str]], addquery_fmt: str | None, count: int,
        max_pages: int, search_field: str, outdir: Path, profile: str | None = None,
        keep_personal: bool = False) -> list[dict]:
    api_key = get_api_key()
    profile = profile or get_profile(None)
    label = SERVICES[mode]["label"]
    raw_dir = outdir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{label}] 요청 방식 프로필: {profile}", flush=True)
    for key, query in items:
        addq = addquery_fmt.format(key=key) if addquery_fmt else ""
        page_total = None
        for page in range(max_pages):
            start = page * count + 1
            params = build_params(mode, profile, api_key, query, addq, count, start, search_field)
            try:
                status, body = fetch(mode, params)
            except NtisError as e:
                print(f"[경고] {e}")
                break
            body = mask(body, api_key)
            safe = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", f"{mode}_{key}_p{page+1}")[:120]
            (raw_dir / f"{safe}.xml").write_text(body, encoding="utf-8")
            verdict, total, rows, msg = classify(status, body)
            if verdict not in ("정상", "결과없음"):
                print(f"[경고] {label} {verdict} - {msg}")
                print(f"       요청주소: {masked_url(mode, params, api_key)}")
                print("       → 'python ntis_search_client.py --mode all --probe' 로 요청 방식을 진단하세요.")
                break
            page_total = total
            for r in rows:
                if not keep_personal:
                    r = drop_personal(r)
                r.update({"기준ID": key, "검색어": query, "컬렉션": SERVICES[mode]["collection"],
                          "조회일시": stamp})
                all_rows.append(r)
            print(f"[{mode}] 기준={key} 페이지{page+1}: {len(rows)}건 (전체 {total}건)", flush=True)
            if not rows or (page + 1) * count >= total:
                break
            time.sleep(REQUEST_INTERVAL)
        if page_total is not None and page_total > count * max_pages:
            print(f"[안내] 기준={key}: 전체 {page_total}건 중 상위 {count * max_pages}건만 수집")
        time.sleep(REQUEST_INTERVAL)
    return all_rows


def probe(modes: list[str], query: str, count: int, outdir: Path) -> list[dict]:
    """서비스별로 네 가지 요청 방식을 시험해 동작 여부를 표로 출력·저장."""
    api_key = get_api_key()
    raw_dir = outdir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    # 키 값은 보여 주지 않고 형식만 점검 (검색 서비스 매뉴얼 규격: apprvKey 최대 64자)
    odd = sorted({ch for ch in api_key if not (ch.isascii() and (ch.isalnum() or ch in "-_"))})
    print(f"[인증키 점검] 읽은 위치: {KEY_SOURCE}", flush=True)
    print(f"[인증키 점검] 길이 {len(api_key)}자"
          + (f", 영문·숫자 외 문자 포함: {describe_chars(odd)} (복사 오류 여부 확인)" if odd
             else ", 영문·숫자로만 구성"), flush=True)
    for mode in modes:
        label = SERVICES[mode]["label"]
        print(f"\n[진단] {label} (검색어={query}, 건수={count})", flush=True)
        for profile in PROFILES:
            params = build_params(mode, profile, api_key, query, "", count, 1, "")
            try:
                status, body = fetch(mode, params)
                body = mask(body, api_key)
                verdict, total, _, msg = classify(status, body)
            except NtisError as e:
                status, body, verdict, total, msg = 0, str(e), "네트워크오류", 0, str(e)
            (raw_dir / f"probe_{mode}_{profile}.txt").write_text(body, encoding="utf-8")
            results.append({"서비스": label, "mode": mode, "프로필": profile, "HTTP상태": status,
                            "판정": verdict, "전체건수": total, "메시지": msg,
                            "요청주소": masked_url(mode, params, api_key)})
            shown = (f"전체 {total}건" + (f" ({msg})" if msg else "")) if verdict == "정상" else msg[:90]
            print(f"  {profile:<14} HTTP {status:<4} {verdict:<6} {shown}", flush=True)
            time.sleep(REQUEST_INTERVAL)
        ok = [r["프로필"] for r in results if r["mode"] == mode and r["판정"] == "정상"]
        empty = [r["프로필"] for r in results if r["mode"] == mode and r["판정"] == "결과없음"]
        if ok:
            print(f"  → 동작하는 방식: {', '.join(ok)}  (권장: {ok[0]})")
        elif empty:
            print(f"  → 오류는 없지만 결과 0건: {', '.join(empty)}  (검색어 파라미터가 무시됐을 수 있음)")
        else:
            print("  → 동작하는 방식 없음: 진단결과.csv 를 NTIS에 전달해 문의하세요.")
    if not results:
        print("진단할 서비스가 없습니다.")
        return results
    path = outdir / "진단결과.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    print(f"\n진단 결과 저장: {path}  (인증키는 *** 로 가려져 있습니다)")
    return results


# ---------------------------------------------------------------- 입출력
def write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        print("저장할 결과가 없습니다.")
        return
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    lead = [f for f in ("기준ID", "검색어", "컬렉션", "HIT순번", "조회일시") if f in fields]
    fields = lead + [f for f in fields if f not in lead]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"저장: {path} ({len(rows)}행, {len(fields)}열)")


def load_items(path: Path) -> list[tuple[str, str]]:
    csv.field_size_limit(10**9)
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header = rows[0]
    if "검색어" in header:
        qcol = header.index("검색어")
        body = rows[1:]
    else:
        qcol, body = 0, rows
    kcol = header.index("기준ID") if "기준ID" in header else None
    out = []
    for r in body:
        if len(r) <= qcol or not r[qcol].strip():
            continue
        q = r[qcol].strip()
        k = r[kcol].strip() if kcol is not None and len(r) > kcol and r[kcol].strip() else q
        out.append((k, q))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="NTIS 국가R&D 검색 OpenAPI (과제/성과/연구보고서)")
    ap.add_argument("--mode", required=True, choices=list(SERVICES) + ["all"],
                    help="all 은 --probe 와 함께만 사용")
    ap.add_argument("--probe", action="store_true", help="요청 방식(프로필)별 동작 진단")
    ap.add_argument("--probe-query", default=PROBE_QUERY, help="진단에 쓸 검색어 (기본: 나노)")
    ap.add_argument("--profile", choices=PROFILES, default=None,
                    help="요청 방식 (기본: .env 의 NTIS_SEARCH_PROFILE, 없으면 sample)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--pjt-id", help="과제고유번호 1건 (addQuery=CN=과제번호 로 조회)")
    g.add_argument("--queries", type=Path, help="검색어 CSV")
    g.add_argument("--text", help="검색어 1건 (CSV 없이 바로 검색)")
    ap.add_argument("--search-field", default="", help="검색 필드 값 (예: TI 과제명, BI 전체)")
    ap.add_argument("--count", type=int, default=None, help=f"페이지당 건수 (기본 {DEFAULT_COUNT}, 진단은 1)")
    ap.add_argument("--max-pages", type=int, default=1, help="최대 페이지 수 (기본 1)")
    ap.add_argument("--keep-personal", action="store_true",
                    help="연구책임자·참여연구원·저자·발명자 성명을 결과 CSV에 포함 (기본: 제외)")
    ap.add_argument("--outdir", type=Path, default=HERE / "search_out")
    args = ap.parse_args()

    outdir = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        get_api_key()  # 호출 전에 키 누락·형식 오류를 먼저 알림
    except RuntimeError as e:
        raise SystemExit(f"[오류] {e}")

    if args.probe:
        modes = list(SERVICES) if args.mode == "all" else [args.mode]
        probe(modes, args.probe_query, args.count or 1, outdir)
        return
    if args.mode == "all":
        ap.error("--mode all 은 --probe 와 함께만 사용할 수 있습니다.")
    if not (args.pjt_id or args.queries or args.text):
        ap.error("--pjt-id, --queries, --text 중 하나가 필요합니다.")

    count = args.count or DEFAULT_COUNT
    profile = get_profile(args.profile)
    if args.pjt_id:
        items = [(args.pjt_id, args.pjt_id)]
        rows = run(args.mode, items, "CN={key}", count, args.max_pages, args.search_field, outdir, profile,
                   args.keep_personal)
    else:
        items = [(args.text, args.text)] if args.text else load_items(args.queries)
        if not items:
            raise SystemExit("검색어가 없습니다. CSV가 UTF-8로 저장됐는지, '검색어' 열이 있는지 확인하세요.")
        rows = run(args.mode, items, None, count, args.max_pages, args.search_field, outdir, profile,
                   args.keep_personal)
    write_csv(rows, outdir / f"ntis_{args.mode}_결과.csv")


if __name__ == "__main__":
    main()
