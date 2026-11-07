#!/usr/bin/env python3
"""수요조사서 중복성 점검 핵심 로직 (GUI와 분리되어 단위 테스트가 가능).

입력
  - 수요조사서: HWP/HWPX/DOCX/PDF/TXT 파일, 엑셀(xlsx)·CSV 일괄 양식, 화면 직접 입력
  - NTIS 분석결과: 붙여 넣은 텍스트 (CSV·탭 표, ntis_*_결과.csv 내용, 줄 단위 목록 모두 허용)
  - (선택) KISTI 2019~2024 기준DB CSV
출력
  - 중복성 정도(낮음/보통/높음), 판정(유지/개선/불가), 근거, 수정 방향, 유사 항목 목록, Markdown 보고서

모든 계산은 이 PC 안에서만 이뤄지며 외부로 데이터를 보내지 않습니다.
필수 패키지 없음. 파일 형식별 선택 패키지: HWP(olefile), PDF(pypdf), 엑셀(openpyxl).
"""
from __future__ import annotations

import csv
import io
import math
import re
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

csv.field_size_limit(10**9)
BOM = chr(0xFEFF)

# =============================================================== 수요조사서 모델
FIELD_LABELS = {
    "title": "기술명", "overview": "개요(필요성)", "goal": "목표",
    "content": "내용", "effect": "기대효과", "keywords": "키워드",
}
FIELD_ALIASES = {
    "title": ("기술명", "과제명", "연구개발과제명", "기술(과제)명", "제안기술명", "과제제목"),
    "overview": ("개요", "기술개요", "필요성", "추진배경", "추진필요성", "기술의필요성", "배경"),
    "goal": ("목표", "연구목표", "개발목표", "최종목표", "기술개발목표"),
    "content": ("내용", "연구내용", "개발내용", "주요내용", "연구개발내용", "기술개발내용"),
    "effect": ("기대효과", "활용방안", "파급효과", "기대성과", "활용분야"),
    "keywords": ("키워드", "한글키워드", "핵심어", "핵심키워드", "keyword", "keywords"),
}
ID_ALIASES = ("기준id", "id", "번호", "연번", "관리번호", "접수번호")
SECTION_FIELDS = ("overview", "goal", "content", "effect")


@dataclass
class Survey:
    sid: str = ""
    title: str = ""
    overview: str = ""
    goal: str = ""
    content: str = ""
    effect: str = ""
    keywords: str = ""
    source: str = ""

    def text_of(self, *names: str) -> str:
        return "\n".join(getattr(self, n) for n in names if getattr(self, n))

    def full_text(self) -> str:
        return self.text_of("title", "keywords", "overview", "goal", "content", "effect")

    def core_text(self) -> str:
        return self.text_of("title", "keywords", "goal")

    def display_name(self) -> str:
        return self.title or (Path(self.source).name if self.source else "") or self.sid or "(제목 없음)"

    def is_empty(self) -> bool:
        return not self.full_text().strip()


def _normh(text: str) -> str:
    """제목·헤더 비교용 정규화 (공백·기호 제거, 소문자)."""
    return re.sub(r"[\s\W_]+", "", text or "").lower()


# =============================================================== 파일에서 텍스트 추출
def read_text_any(path: Path) -> str:
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _local(tag) -> str:
    return tag.split("}")[-1] if isinstance(tag, str) else ""


def _walk_text(root: ET.Element) -> str:
    """문단(p)마다 줄을 바꾸고 텍스트(t)를 이어 붙임 (DOCX·HWPX 공통)."""
    out: list[str] = []

    def visit(el: ET.Element) -> None:
        name = _local(el.tag)
        if name == "p":
            out.append("\n")
        if name == "t":
            out.append("".join(el.itertext()))
            return
        if name == "tab":
            out.append("\t")
        elif name in ("br", "lineBreak"):
            out.append("\n")
        for child in el:
            visit(child)

    visit(root)
    return "".join(out)


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    return _walk_text(root)


def _hwpx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n, re.I)]
        names.sort(key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1)))
        if not names:
            raise RuntimeError("HWPX 안에서 본문(section)을 찾지 못했습니다.")
        return "\n".join(_walk_text(ET.fromstring(z.read(n))) for n in names)


HWPTAG_PARA_TEXT = 67  # HWPTAG_BEGIN(16) + 51


def _hwp_para_text(payload: bytes) -> str:
    """HWP 문단 텍스트 레코드 → 문자열 (제어문자 처리)."""
    n = len(payload) // 2
    units = struct.unpack_from(f"<{n}H", payload, 0) if n else ()
    chars: list[str] = []
    i = 0
    while i < n:
        c = units[i]
        if c < 32:
            if c in (0, 10, 13) or 24 <= c <= 31:      # 1글자 제어문자
                if c in (10, 13):
                    chars.append("\n")
                elif c in (30, 31):
                    chars.append(" ")
                i += 1
            else:                                        # 인라인·확장 제어문자 (8글자 분량)
                if c == 9:
                    chars.append("\t")
                i += 8
        else:
            chars.append(chr(c))
            i += 1
    text = "".join(chars)
    # 서로게이트 쌍(이모지 등) 복원
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _hwp_records_text(data: bytes) -> str:
    out: list[str] = []
    pos, n = 0, len(data)
    while pos + 4 <= n:
        header = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        tag = header & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > n:
                break
            size = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        payload = data[pos:pos + size]
        pos += size
        if tag == HWPTAG_PARA_TEXT:
            out.append(_hwp_para_text(payload))
    return "\n".join(out)


def _hwp_text(path: Path) -> str:
    try:
        import olefile
    except ImportError:
        raise RuntimeError("HWP 파일을 읽으려면 'pip install olefile'이 필요합니다. "
                           "또는 한글에서 HWPX나 PDF로 저장해 불러오세요.") from None
    ole = olefile.OleFileIO(str(path))
    try:
        header = ole.openstream("FileHeader").read()
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & 0x2 or flags & 0x4:
            raise RuntimeError("암호가 걸렸거나 배포용인 HWP는 읽을 수 없습니다. HWPX나 PDF로 저장해 주세요.")
        compressed = bool(flags & 0x1)
        sections = [e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText" and e[1].startswith("Section")]
        sections.sort(key=lambda e: int(e[1][7:] or 0))
        texts = []
        for entry in sections:
            data = ole.openstream(entry).read()
            if compressed:
                data = zlib.decompress(data, -15)
            texts.append(_hwp_records_text(data))
        return "\n".join(texts)
    finally:
        ole.close()


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("PDF 파일을 읽으려면 'pip install pypdf'가 필요합니다.") from None
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_text(path: Path) -> str:
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".hwpx":
        return _hwpx_text(path)
    if ext == ".hwp":
        return _hwp_text(path)
    if ext == ".docx":
        return _docx_text(path)
    if ext == ".pdf":
        return _pdf_text(path)
    if ext in (".txt", ".md"):
        return read_text_any(path)
    raise ValueError(f"지원하지 않는 형식입니다: {ext}")


# =============================================================== 수요조사서 항목 분리
_HEAD_PREFIX = r"^[\s\d.\)\(\[\]①-⑳□■○●◦▶▷►\-–·•*ㅇ]*"


def _alias_pattern(alias: str) -> str:
    # "연구목표" 가 "연구 목표" 로 적혀도 인식하도록 글자 사이 공백 허용
    return r"\s*".join(re.escape(ch) for ch in alias if not ch.isspace())


def _build_heading():
    pairs = [(a, f) for f, names in FIELD_ALIASES.items() for a in names]
    pairs.sort(key=lambda p: -len(p[0]))
    # 항목 이름 뒤 괄호 설명 허용: "개요(필요성)", "목표 (정량)" 등
    pattern = (_HEAD_PREFIX + "(" + "|".join(_alias_pattern(a) for a, _ in pairs) + ")"
               + r"\s*(?:[(（][^)）]*[)）])?\s*(?:[:：]\s*(.*)|$)")
    return re.compile(pattern, re.IGNORECASE), {_normh(a): f for a, f in pairs}


_HEADING_RE, _HEADING_MAP = _build_heading()


def parse_survey_text(text: str, source: str = "") -> Survey:
    """'기술명:', '□ 연구목표', 표 형태의 '내용' 칸 등 항목 제목을 찾아 내용을 나눔."""
    s = Survey(source=source)
    buckets: dict[str, list[str]] = defaultdict(list)
    pre: list[str] = []
    current = None
    found = False
    for raw in (text or "").lstrip(BOM).splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HEADING_RE.match(line)
        if m:
            fld = _HEADING_MAP.get(_normh(m.group(1)))
            if fld:
                current, found = fld, True
                rest = (m.group(2) or "").strip()
                if rest:
                    buckets[fld].append(rest)
                continue
        (buckets[current] if current else pre).append(line)

    if not found:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        if lines and len(lines[0]) <= 80:
            s.title, s.content = lines[0], "\n".join(lines[1:])
        else:
            s.content = "\n".join(lines)
        return s

    for fld, lines in buckets.items():
        setattr(s, fld, "\n".join(lines))
    if s.title and "\n" in s.title:                       # 기술명 칸 아래 줄은 개요로
        first, rest = s.title.split("\n", 1)
        s.title = first
        s.overview = (rest + "\n" + s.overview).strip()
    if pre:
        if not s.title and len(pre[0]) <= 80:
            s.title, pre = pre[0], pre[1:]
        if pre:
            s.overview = ("\n".join(pre) + "\n" + s.overview).strip()
    return s


def _map_columns(row: list[str]) -> dict[str, int]:
    """헤더 행 → {필드: 열번호}. 정확히 일치하는 이름을 먼저, 그다음 포함 관계로 연결."""
    heads = [_normh(c) for c in row]
    colmap: dict[str, int] = {}
    used: set[int] = set()
    for i, h in enumerate(heads):
        if h in ID_ALIASES and "sid" not in colmap:
            colmap["sid"] = i
            used.add(i)
    for exact in (True, False):
        for fld, names in FIELD_ALIASES.items():
            if fld in colmap:
                continue
            for i, h in enumerate(heads):
                if i in used or not h:
                    continue
                if any((h == _normh(n)) if exact else (_normh(n) in h) for n in names):
                    colmap[fld] = i
                    used.add(i)
                    break
    return colmap


def _cell(row, i) -> str:
    if i is None or i >= len(row) or row[i] is None:
        return ""
    return str(row[i]).strip()


def surveys_from_rows(rows: list[list[str]], source: str = "") -> list[Survey]:
    header_idx, colmap = None, {}
    for i, row in enumerate(rows[:10]):
        cm = _map_columns(row)
        if len([k for k in cm if k != "sid"]) >= 2:
            header_idx, colmap = i, cm
            break
    if header_idx is None:
        raise ValueError("일괄 양식에서 '기술명', '목표', '내용' 같은 제목 행을 찾지 못했습니다.")
    out: list[Survey] = []
    for j, row in enumerate(rows[header_idx + 1:], 1):
        s = Survey(source=f"{source} {j}행".strip())
        for fld, ci in colmap.items():
            setattr(s, fld, _cell(row, ci))
        if s.is_empty():
            continue
        if not s.sid:
            s.sid = str(j)
        out.append(s)
    return out


def _xlsx_rows(path: Path) -> list[list[str]]:
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("엑셀 파일을 읽으려면 'pip install openpyxl'이 필요합니다.") from None
    wb = openpyxl.load_workbook(str(path), data_only=True)
    try:
        ws = wb.worksheets[0]
        return [["" if v is None else str(v).strip() for v in r] for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def load_survey_file(path: Path) -> list[Survey]:
    """파일 1개 → 수요조사서 목록 (일괄 양식이면 여러 건)."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return surveys_from_rows(_xlsx_rows(path), source=path.name)
    if ext == ".csv":
        rows = list(csv.reader(io.StringIO(read_text_any(path))))
        return surveys_from_rows(rows, source=path.name)
    survey = parse_survey_text(extract_text(path), source=path.name)
    if survey.is_empty():
        raise RuntimeError("파일에서 글자를 찾지 못했습니다. 스캔 이미지 PDF라면 텍스트가 있는 파일로 저장해 주세요.")
    return [survey]


# =============================================================== 텍스트 유사도
TOKEN_RE = re.compile(r"[가-힣]+|[a-z]+|\d+")
STOPWORDS = ("개발", "기술", "연구", "시스템", "플랫폼", "기반", "및", "위한", "관한", "구축", "활용",
             "통한", "적용", "사업", "방법", "장치", "고도화", "실증", "대한", "이용", "있는", "등")


def _strip_affixes(tok: str) -> str:
    changed = True
    while changed and tok:
        changed = False
        for sw in STOPWORDS:
            if tok == sw:
                return ""
            if len(tok) > len(sw) and tok.endswith(sw):
                tok = tok[:-len(sw)]
                changed = True
    return tok


def tokens(text: str) -> list[str]:
    out = []
    for t in TOKEN_RE.findall((text or "").lower()):
        t = _strip_affixes(t)
        if len(t) >= 2:
            out.append(t)
    return out


def bigrams(text: str) -> Counter:
    c: Counter = Counter()
    for t in tokens(text):
        for i in range(len(t) - 1):
            c[t[i:i + 2]] += 1
    return c


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


# =============================================================== NTIS 결과 해석
KIND_MAP = {"project": "과제", "paper": "논문", "rpaper": "논문", "patent": "특허", "rpatent": "특허",
            "researchreport": "보고서", "researchpdf": "보고서", "rresearchpdf": "보고서",
            "requip": "장비", "equip": "장비"}
TITLE_KEYS = ("과제명", "결과명", "제목", "projecttitlekorean", "resulttitlekorean", "resulttitle",
              "korpjtnm", "논문명", "특허명", "발명의명칭", "보고서명", "연구과제명", "비교과제명", "title")
SCORE_KEYS = ("유사도", "similarityscore", "similarity", "차별성점수", "차별성", "점수", "score")
ID_KEYS = ("결과id", "과제고유번호", "projectnumber", "resultid", "termsn", "rstid", "pjtid", "과제번호")
YEAR_KEYS = ("등록연도", "등록일", "연도", "projectyear", "pubyear", "publicationyear", "과제수행연도", "기준년도")
KIND_KEYS = ("컬렉션", "구분", "유형", "collection", "kind")
TEXT_KEY_PARTS = ("goal", "abstract", "effect", "keyword", "요약", "키워드", "목표", "내용", "기대효과")
BASE_KEYS = ("기준id", "기준과제id")
SCORE_MODES = ("auto", "유사도", "차별성", "무시")


@dataclass
class RefItem:
    title: str
    kind: str = ""
    ref_id: str = ""
    year: str = ""
    source: str = "NTIS"
    raw_score: float | None = None     # 입력된 원 점수
    given_sim: float | None = None     # 원 점수를 0~1 유사도로 환산 (높을수록 유사)
    text: str = ""                     # 비교용 요약·키워드
    local_sim: float = 0.0             # 이 프로그램이 계산한 문장 유사도

    @property
    def sim(self) -> float:
        """판정에 쓰는 종합 유사도: NTIS 점수가 있으면 그것을, 없으면 계산 유사도."""
        return self.given_sim if self.given_sim is not None else self.local_sim


def normalize_score(raw: float | None, mode: str) -> float | None:
    if raw is None or mode == "무시":
        return None
    v = raw / 100.0 if raw > 1.0 else raw
    v = min(max(v, 0.0), 1.0)
    return round(1.0 - v, 4) if mode == "차별성" else round(v, 4)


def _to_float(text: str) -> float | None:
    t = (text or "").replace(",", "").replace("%", "").replace("점", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _year(text: str) -> str:
    m = re.search(r"(?:19|20)\d{2}", text or "")
    return m.group(0) if m else ""


def _find_col(heads: list[str], keys) -> int | None:
    for k in keys:                                   # 정확히 일치
        for i, h in enumerate(heads):
            if h == k and not h.startswith("기준"):
                return i
    for k in keys:                                   # 포함 관계 ('기준과제명' 같은 기준 열은 제외)
        for i, h in enumerate(heads):
            if k in h and not h.startswith("기준"):
                return i
    return None


LINE_SCORE_RE = re.compile(r"(?P<sep>\t|\s{2,}|\s*[|:,]\s*|\s)(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>%|점)?\s*$")
ENUM_RE = re.compile(r"^\s*(?:\d+[.)]|\(\d+\)|[-*•·○●□■▶])\s*")


def _parse_line(line: str, mode: str) -> RefItem | None:
    s = line.strip()
    raw = None
    eff_mode = "유사도" if mode == "auto" else mode
    if "\t" in s:   # 엑셀·웹 표를 제목 행 없이 복사한 경우: 가장 긴 글자 칸 = 제목, 마지막 점수 칸 = 점수
        cells = [c.strip() for c in s.split("\t") if c.strip()]
        texts = [c for c in cells if _to_float(c) is None]
        nums = []
        for pos, c in enumerate(cells):
            v = _to_float(c)
            if v is None:
                continue
            if pos == 0 and re.fullmatch(r"\d{1,3}", c):   # 맨 앞 순번 칸은 점수가 아님
                continue
            if 0 <= v <= 100:
                nums.append(v)
        title = max(texts, key=len) if texts else ""
        if len(_normh(title)) < 4:
            return None
        raw = nums[-1] if nums else None
        return RefItem(title=title, raw_score=raw, given_sim=normalize_score(raw, eff_mode))
    m = LINE_SCORE_RE.search(s)
    if m and m.start() > 0:
        num_text, unit, sep = m.group("num"), m.group("unit"), m.group("sep")
        num = float(num_text)
        explicit_sep = "\t" in sep or len(sep) >= 2 or sep.strip() in ("|", ":", ",")
        if num <= 100 and (unit or ("." in num_text and num <= 1.0) or explicit_sep):
            raw = num
            s = s[:m.start()]
    s = ENUM_RE.sub("", s).strip(" \t-:|,;")
    if len(_normh(s)) < 4:
        return None
    return RefItem(title=s, raw_score=raw, given_sim=normalize_score(raw, eff_mode))


def _dedup(items: list[RefItem]) -> list[RefItem]:
    best: dict[tuple, RefItem] = {}
    order: list[tuple] = []
    for it in items:
        key = (_normh(it.title), it.kind)
        if key not in best:
            best[key] = it
            order.append(key)
        else:
            new = -1.0 if it.given_sim is None else it.given_sim
            old = -1.0 if best[key].given_sim is None else best[key].given_sim
            if new > old:
                best[key] = it
    return [best[k] for k in order]


def parse_ntis_text(text: str, score_mode: str = "auto", base_id: str = "") -> list[RefItem]:
    """붙여 넣은 NTIS 분석결과 → 비교 항목 목록.
    표(CSV·탭)면 제목/점수/구분 열을 찾아 쓰고, 아니면 한 줄에 한 건으로 읽음."""
    text = (text or "").strip().lstrip(BOM)
    if not text:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    first = lines[0]
    delim = "\t" if "\t" in first else ("," if "," in first else None)
    if delim:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
        heads = [_normh(h) for h in rows[0]]
        ti = _find_col(heads, TITLE_KEYS)
        if ti is not None:
            si = _find_col(heads, SCORE_KEYS)
            ii = _find_col(heads, ID_KEYS)
            yi = _find_col(heads, YEAR_KEYS)
            ki = _find_col(heads, KIND_KEYS)
            bi = next((i for i, h in enumerate(heads) if h in BASE_KEYS), None)
            used = {x for x in (ti, si, ii, yi, ki, bi) if x is not None}
            text_cols = [i for i, h in enumerate(heads)
                         if i not in used and any(p in h for p in TEXT_KEY_PARTS)]
            mode = score_mode
            if mode == "auto":
                mode = "차별성" if si is not None and "차별" in heads[si] else "유사도"
            body = rows[1:]
            if bi is not None and base_id and any(_cell(r, bi) == base_id for r in body):
                body = [r for r in body if _cell(r, bi) == base_id]
            items = []
            for r in body:
                title = _cell(r, ti)
                if not title:
                    continue
                raw = _to_float(_cell(r, si)) if si is not None else None
                kind_raw = _cell(r, ki)
                items.append(RefItem(
                    title=title,
                    kind=KIND_MAP.get(kind_raw.lower(), kind_raw),
                    ref_id=_cell(r, ii),
                    year=_year(_cell(r, yi)),
                    raw_score=raw,
                    given_sim=normalize_score(raw, mode),
                    text=" ".join(_cell(r, c) for c in text_cols)))
            return _dedup(items)
    items = [it for it in (_parse_line(ln, score_mode) for ln in lines) if it]
    return _dedup(items)


# =============================================================== KISTI 기준DB (선택)
class KistiIndex:
    """KISTI 2019~2024 기준DB(과제명+한글키워드) TF-IDF 검색. 후보를 찾는 용도."""

    def __init__(self, docs: list[dict]):
        self.docs = docs
        df: Counter = Counter()
        for d in docs:
            df.update(set(d["tokens"]))
        n = len(docs)
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for i, d in enumerate(docs):
            tf = Counter(d["tokens"])
            w = {t: (1 + math.log(c)) * self.idf[t] for t, c in tf.items()}
            norm = math.sqrt(sum(v * v for v in w.values())) or 1.0
            for t, v in w.items():
                self.postings[t].append((i, v / norm))

    @classmethod
    def load(cls, path: Path, progress=None) -> "KistiIndex":
        docs = []
        with Path(path).open(encoding="utf-8-sig", newline="") as f:
            for k, r in enumerate(csv.DictReader(f)):
                title = r.get("과제명", "") or ""
                kw = r.get("한글키워드", "") or ""
                docs.append({"year": r.get("과제수행연도", ""), "id": r.get("과제고유번호", ""),
                             "title": title, "kw": kw, "tokens": tokens(title + " " + kw.replace(",", " "))})
                if progress and k and k % 50000 == 0:
                    progress(f"KISTI 기준DB 읽는 중... {k:,}건")
        if progress:
            progress(f"KISTI 기준DB 색인 중... {len(docs):,}건")
        return cls(docs)

    def search(self, text: str, top: int = 20) -> list[RefItem]:
        tf = Counter(t for t in tokens(text) if t in self.idf)
        if not tf:
            return []
        q = {t: (1 + math.log(c)) * self.idf[t] for t, c in tf.items()}
        qn = math.sqrt(sum(v * v for v in q.values())) or 1.0
        scores: dict[int, float] = defaultdict(float)
        for t, qv in q.items():
            for i, dv in self.postings[t]:
                scores[i] += qv / qn * dv
        out: list[RefItem] = []
        seen: dict[str, RefItem] = {}
        for i, sc in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            d = self.docs[i]
            key = _normh(d["title"])
            if key in seen:                     # 연차별로 반복되는 같은 과제는 한 건으로
                if d["year"] and d["year"] not in seen[key].year:
                    seen[key].year = ",".join(sorted(set(seen[key].year.split(",") + [d["year"]]) - {""}))
                continue
            item = RefItem(title=d["title"], kind="과제", ref_id=d["id"], year=d["year"], source="KISTI",
                           raw_score=round(sc, 4), text=d["kw"])
            seen[key] = item
            out.append(item)
            if len(out) >= top:
                break
        return out


# =============================================================== 판정
@dataclass
class Criteria:
    block: float = 0.85        # 이 값 이상 유사 항목이 있으면 '불가'
    improve: float = 0.65      # 이 값 이상이면 '개선'
    crowd_sim: float = 0.50    # 유사 주제 밀집 판단 기준 유사도
    crowd_count: int = 5       # 위 기준 이상이 이 건수 이상이면 '개선'
    top_n: int = 10            # 보고서에 보여 줄 유사 항목 수


@dataclass
class Result:
    survey: Survey
    verdict: str
    degree: str
    top_sim: float
    n_block: int
    n_improve: int
    n_crowd: int
    items: list[RefItem]
    common_keywords: list[str]
    section_scores: dict[str, float]
    reasons: list[str]
    actions: list[str]
    criteria: Criteria
    checked_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    notes: list[str] = field(default_factory=list)      # 비교 자료 수집 기록 (NTIS 검색어·건수 등)


QUERY_STRIP_RE = re.compile(r"[|!\"()\[\]{}^*?~]")


def build_queries(survey: Survey, max_terms: int = 3) -> list[str]:
    """NTIS 검색어 후보. 키워드(부족하면 기술명 단어)로 만들고, 결과가 없을 때 쓰도록 점점 줄인 검색어도 함께 반환.
    NTIS 검색은 띄어쓰기가 AND 이므로 단어가 많을수록 결과가 적어집니다."""
    terms: list[str] = []
    for kw in re.split(r"[,;/·\n]+", survey.keywords or ""):
        kw = re.sub(r"\s+", " ", QUERY_STRIP_RE.sub(" ", kw)).strip()
        if len(kw) >= 2 and kw not in terms:
            terms.append(kw)
    for tok in tokens(survey.title):
        if len(terms) >= max_terms:
            break
        if tok not in terms:
            terms.append(tok)
    terms = terms[:max_terms]
    queries: list[str] = []
    for n in range(len(terms), 0, -1):
        q = " ".join(terms[:n])
        if q not in queries:
            queries.append(q)
    return queries


def compute_local_sims(survey: Survey, items: list[RefItem]) -> None:
    """기술명끼리, 기술명+키워드 대 제목, 핵심 내용 대 제목+요약 중 가장 높은 값을 계산 유사도로 씀."""
    s_t = bigrams(survey.title)
    s_title = bigrams(survey.title + " " + survey.keywords)
    s_core = bigrams(survey.core_text() + "\n" + survey.content)
    for it in items:
        it_t = bigrams(it.title)
        it.local_sim = round(max(cosine(s_t, it_t), cosine(s_title, it_t),
                                 cosine(s_core, bigrams(it.title + " " + it.text))), 4)


def common_keywords(survey: Survey, items: list[RefItem], top_k: int = 10) -> list[str]:
    counts: Counter = Counter()
    texts = [(it.title + " " + it.text).lower() for it in items]
    for kw in re.split(r"[,;/·\n]+", survey.keywords or ""):
        kw = kw.strip()
        if len(kw) >= 2:
            n = sum(1 for t in texts if kw.lower() in t)
            if n:
                counts[kw] += n
    mine = set(tokens(survey.core_text() + " " + survey.content))
    lowered = {k.lower() for k in counts}
    for t in texts:
        for tok in set(tokens(t)) & mine:
            if tok not in lowered:
                counts[tok] += 1
    return [k for k, _ in counts.most_common(top_k)]


def section_scores(survey: Survey, items: list[RefItem], k: int = 3) -> dict[str, float]:
    refs = [bigrams(it.title + " " + it.text) for it in items[:max(k, 1)]]
    out = {}
    for fld in SECTION_FIELDS:
        txt = getattr(survey, fld)
        if not txt.strip() or not refs:
            continue
        b = bigrams(txt)
        out[FIELD_LABELS[fld]] = round(sum(cosine(b, r) for r in refs) / len(refs), 4)
    return out


def _describe(it: RefItem) -> str:
    extra = ", ".join(x for x in (it.kind, it.year, it.source if it.source != "NTIS" else "") if x)
    return f"「{it.title}」" + (f"({extra})" if extra else "")


def _build_text(verdict: str, top: list[RefItem], kws: list[str], sec: dict[str, float],
                n_improve: int, n_crowd: int, c: Criteria, top_sim: float) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    actions: list[str] = []
    names = ", ".join(_describe(it) for it in top[:3])
    worst = max(sec, key=sec.get) if sec else ""
    kw_text = ", ".join(kws[:6])
    if top:
        reasons.append(f"가장 유사한 항목: {_describe(top[0])}, 종합 유사도 {top_sim:.2f}")
    reasons.append(f"유사도 {c.improve:.2f} 이상 {n_improve}건, "
                   f"{c.crowd_sim:.2f} 이상 {n_crowd}건 (기준: 불가 {c.block:.2f}, 개선 {c.improve:.2f})")
    if worst:
        reasons.append(f"유사 항목과 가장 많이 겹치는 수요조사서 항목: {worst} ({sec[worst]:.2f})")
    if kw_text:
        reasons.append(f"공통 키워드: {kw_text}")

    if verdict == "불가":
        actions += [
            "현재 내용으로는 제출하지 않는 것을 권합니다. 기수행·수행 중 과제와 주제가 사실상 겹칩니다.",
            f"유사 과제 {names}의 목표와 성과를 확인한 뒤, 그 성과를 활용하는 후속·고도화 과제로 다시 정의하세요.",
            "적용 대상(산업·현장), 기술 접근 방식, 최종 결과물 중 최소 한 가지를 바꿔 과제 범위를 재설정하세요.",
        ]
        if kw_text:
            actions.append(f"공통 키워드({kw_text}) 위주의 표현을 줄이고, 새로 제안하는 핵심 기술을 키워드로 앞세우세요.")
        actions.append("수정 후 이 도구와 NTIS 차별성검토 서비스로 다시 점검하세요.")
    elif verdict == "개선":
        if top_sim < c.improve and n_crowd >= c.crowd_count:
            actions.append(f"유사도 {c.crowd_sim:.2f} 이상 과제가 {n_crowd}건으로 같은 주제가 밀집해 있습니다. "
                           "기존 과제와의 경쟁 또는 상호보완 관계를 설명하는 문단을 추가하세요.")
        if worst:
            actions.append(f"가장 많이 겹치는 '{worst}' 항목부터 고쳐 쓰세요.")
        actions += [
            "목표를 정량 지표(성능 수치, 기술성숙도(TRL), 적용 규모 등)로 구체화해 기존 과제와의 차이를 드러내세요.",
            f"기존 과제 {names} 대비 차별점(대상·방법·결과물)을 '차별성' 문단으로 명시하세요.",
        ]
        if kw_text:
            actions.append(f"공통 키워드({kw_text}) 외에 이 과제만의 고유 키워드를 보강하세요.")
    elif verdict == "유지":
        actions += [
            "중복 가능성이 낮습니다. 현재 방향을 유지합니다.",
            "유사도가 높은 상위 항목을 선행연구로 인용해 차별성 근거를 함께 제시하면 좋습니다.",
            "제출 전 NTIS 차별성검토 서비스로 공식 점검을 받으세요.",
        ]
    else:
        actions += [
            "비교할 자료가 없어 판정하지 않았습니다. NTIS 분석결과를 붙여 넣거나 KISTI 기준DB를 켜고 다시 점검하세요.",
        ]
    return reasons, actions


def analyze(survey: Survey, items: list[RefItem], criteria: Criteria | None = None,
            kisti: KistiIndex | None = None, kisti_top: int = 20) -> Result:
    c = criteria or Criteria()
    pool = [RefItem(**vars(it)) for it in items]          # 입력 목록은 바꾸지 않음
    if kisti is not None:
        pool += kisti.search(survey.core_text() + " " + survey.content[:1000], kisti_top)
    compute_local_sims(survey, pool)
    pool.sort(key=lambda x: x.sim, reverse=True)
    sims = [it.sim for it in pool]
    top_sim = sims[0] if sims else 0.0
    n_block = sum(1 for s in sims if s >= c.block)
    n_improve = sum(1 for s in sims if s >= c.improve)
    n_crowd = sum(1 for s in sims if s >= c.crowd_sim)
    if not pool:
        verdict, degree = "판단 보류", "-"
    elif top_sim >= c.block:
        verdict, degree = "불가", "높음"
    elif top_sim >= c.improve or n_crowd >= c.crowd_count:
        verdict, degree = "개선", "보통"
    else:
        verdict, degree = "유지", "낮음"
    top = pool[:c.top_n]
    kws = common_keywords(survey, top[:5])
    sec = section_scores(survey, top)
    reasons, actions = _build_text(verdict, top, kws, sec, n_improve, n_crowd, c, top_sim)
    return Result(survey, verdict, degree, round(top_sim, 4), n_block, n_improve, n_crowd,
                  top, kws, sec, reasons, actions, c)


# =============================================================== 출력
DISCLAIMER = ("이 결과는 텍스트 비교에 따른 1차 참고자료입니다. 최종 차별성·중복 여부는 "
              "발주기관의 연구심의위원회 등에서 결정합니다.")


def _md_cell(text: str) -> str:
    return (text or "").replace("|", "/").replace("\n", " ").strip()


def to_markdown(r: Result) -> str:
    s = r.survey
    c = r.criteria
    out = [
        "# 수요조사서 중복성 점검 결과",
        "",
        f"- 점검일시: {r.checked_at}",
        f"- 수요조사서: {s.display_name()}" + (f" (ID {s.sid})" if s.sid else ""),
        f"- **판정: {r.verdict}** (중복성 정도: {r.degree}, 최고 유사도 {r.top_sim:.2f})",
        f"- 적용 기준: 불가 {c.block:.2f} 이상 / 개선 {c.improve:.2f} 이상 / "
        f"밀집 {c.crowd_sim:.2f} 이상 {c.crowd_count}건",
        "",
        "## 판정 근거",
        "",
    ]
    out += [f"- {x}" for x in r.reasons]
    out += ["", "## 수정 방향", ""]
    out += [f"{i}. {x}" for i, x in enumerate(r.actions, 1)]
    if r.items:
        out += ["", f"## 유사 항목 (상위 {len(r.items)}건)", "",
                "| 순위 | 출처 | 구분 | 제목 | 연도 | 입력 점수 | 계산 유사도 | 종합 |",
                "|---|---|---|---|---|---|---|---|"]
        for i, it in enumerate(r.items, 1):
            raw = "" if it.raw_score is None else f"{it.raw_score:g}"
            out.append(f"| {i} | {it.source} | {_md_cell(it.kind)} | {_md_cell(it.title)} | {_md_cell(it.year)} "
                       f"| {raw} | {it.local_sim:.2f} | {it.sim:.2f} |")
    if r.section_scores:
        out += ["", "## 항목별 겹침 정도", "", "| 수요조사서 항목 | 평균 유사도 |", "|---|---|"]
        out += [f"| {k} | {v:.2f} |" for k, v in sorted(r.section_scores.items(), key=lambda x: -x[1])]
    if r.notes:
        out += ["", "## 비교 자료", ""] + [f"- {n}" for n in r.notes]
    out += ["", "## 유의사항", "", f"- {DISCLAIMER}",
            "- 입력 점수가 있으면 그 점수를, 없으면 이 프로그램이 계산한 문장 유사도를 종합 유사도로 씁니다.", ""]
    return "\n".join(out)


def to_markdown_many(results: list[Result]) -> str:
    """일괄 점검: 요약 표 + 건별 결과."""
    out = ["# 수요조사서 중복성 일괄 점검 결과", "", "| ID | 기술명 | 판정 | 최고 유사도 |", "|---|---|---|---|"]
    out += [f"| {_md_cell(r.survey.sid)} | {_md_cell(r.survey.display_name())} | {r.verdict} | {r.top_sim:.2f} |"
            for r in results]
    for r in results:
        out += ["", "---", "", to_markdown(r).replace("# 수요조사서 중복성 점검 결과", f"# {r.survey.display_name()}", 1)]
    return "\n".join(out)


VERDICT_RGB = {"유지": (0x2E, 0x7D, 0x32), "개선": (0xEF, 0x6C, 0x00), "불가": (0xC6, 0x28, 0x28),
               "판단 보류": (0x61, 0x61, 0x61)}


def to_docx(results, path: Path) -> None:
    """Word 보고서 저장 (한글에서도 열림). results: Result 1개 또는 목록."""
    try:
        from docx import Document
        from docx.enum.text import WD_BREAK
        from docx.oxml.ns import qn
        from docx.shared import Pt, RGBColor
    except ImportError:
        raise RuntimeError("워드 보고서를 만들려면 'pip install python-docx'가 필요합니다.") from None
    if isinstance(results, Result):
        results = [results]
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "맑은 고딕"
    normal.font.size = Pt(10)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "맑은 고딕")

    def table(headers, rows):
        t = doc.add_table(rows=1, cols=len(headers))
        t.style = "Table Grid"
        for cell, h in zip(t.rows[0].cells, headers):
            cell.text = h
        for row in rows:
            for cell, v in zip(t.add_row().cells, row):
                cell.text = str(v)

    if len(results) > 1:
        doc.add_heading("수요조사서 중복성 일괄 점검 결과", level=1)
        table(["ID", "기술명", "판정", "최고 유사도"],
              [[r.survey.sid, r.survey.display_name(), r.verdict, f"{r.top_sim:.2f}"] for r in results])
    for n, r in enumerate(results):
        if len(results) > 1:
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        c = r.criteria
        doc.add_heading(r.survey.display_name() if len(results) > 1 else "수요조사서 중복성 점검 결과", level=1)
        p = doc.add_paragraph()
        run = p.add_run(f"판정: {r.verdict}")
        run.bold = True
        run.font.size = Pt(18)
        run.font.color.rgb = RGBColor(*VERDICT_RGB.get(r.verdict, (0, 0, 0)))
        p.add_run(f"    중복성 정도 {r.degree} · 최고 유사도 {r.top_sim:.2f}")
        table(["항목", "내용"], [
            ["점검일시", r.checked_at],
            ["수요조사서", r.survey.display_name() + (f" (ID {r.survey.sid})" if r.survey.sid else "")],
            ["적용 기준", f"불가 {c.block:.2f} 이상 / 개선 {c.improve:.2f} 이상 / 밀집 {c.crowd_sim:.2f} 이상 {c.crowd_count}건"],
        ])
        doc.add_heading("고칠 점", level=2)
        for a in r.actions:
            doc.add_paragraph(a, style="List Number")
        doc.add_heading("판정 근거", level=2)
        for x in r.reasons:
            doc.add_paragraph(x, style="List Bullet")
        if r.items:
            doc.add_heading(f"유사 항목 (상위 {len(r.items)}건)", level=2)
            table(["순위", "출처", "구분", "제목", "연도", "유사도"],
                  [[i, it.source, it.kind, it.title, it.year, f"{it.sim:.2f}"] for i, it in enumerate(r.items, 1)])
        if r.notes:
            doc.add_heading("비교 자료", level=2)
            for x in r.notes:
                doc.add_paragraph(x, style="List Bullet")
        doc.add_heading("유의사항", level=2)
        doc.add_paragraph(DISCLAIMER)
    doc.save(str(path))


def items_to_rows(r: Result) -> list[dict]:
    return [{"순위": i, "출처": it.source, "구분": it.kind, "제목": it.title, "ID": it.ref_id, "연도": it.year,
             "입력점수": "" if it.raw_score is None else it.raw_score, "계산유사도": it.local_sim,
             "종합유사도": it.sim} for i, it in enumerate(r.items, 1)]


def summary_row(r: Result) -> dict:
    top = r.items[0] if r.items else None
    return {"ID": r.survey.sid, "기술명": r.survey.display_name(), "판정": r.verdict, "중복성정도": r.degree,
            "최고유사도": r.top_sim, "가장유사한항목": top.title if top else "",
            "개선기준이상건수": r.n_improve, "밀집기준이상건수": r.n_crowd,
            "공통키워드": ", ".join(r.common_keywords[:6]), "점검일시": r.checked_at}


def write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        return
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
