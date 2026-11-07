#!/usr/bin/env python3
"""NTIS 결과(ntis_results.csv)에 KISTI 과제 상세정보를 붙여 ntis_results_enriched.csv 로 저장.

- NTIS API는 과제명·유사도·등록일 등 일부 필드만 제공합니다.
- project 결과는 과제고유번호(결과ID)로 KISTI 사업과제정보(2019~2024)와 맞춰
  수행기관, 연구기간, 연구비, 키워드, 표준분류, 사업명을 붙입니다.
- KISTI에 없는 결과(주로 2025년 이후 등록)는 '미수록'으로 표시합니다.
- paper / patent / researchreport 는 API 필드만 제공되므로 등록연도와 수록 여부만 계산합니다.

사용:
    python ntis_enrich.py                       # 같은 폴더의 ntis_results.csv 처리
    python ntis_enrich.py --input 다른파일.csv --output 결과.csv
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOOKUP = HERE / "kisti_lookup.csv"
CUTOFF_YEAR = 2024  # KISTI 기준 데이터의 마지막 연도

LOOKUP_COLS = ["과제수행연도", "사업명", "부처명", "과제고유번호", "과제명", "연구시작일", "연구종료일",
               "대표전문기관", "수행기관명", "연구수행주체", "과학기술표준분류1대", "한글키워드",
               "연구개발단계", "정부지원연구개발비", "총연구비합계"]
ADD_COLS = ["KISTI수록여부", "등록연도"] + [f"KISTI_{c}" for c in LOOKUP_COLS if c != "과제고유번호"]
MONEY_COLS = {"정부지원연구개발비", "총연구비합계"}


def fmt_money(v: str) -> str:
    """'3.64024E9' 같은 지수 표기를 원 단위 정수 문자열로 변환 (변환 불가 시 원문 유지)."""
    try:
        return f"{int(round(float(v))):,}" if v not in ("", None) else ""
    except (TypeError, ValueError):
        return v


def load_lookup(path: Path) -> dict[str, dict]:
    csv.field_size_limit(10**9)
    table = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            pid = r.get("과제고유번호", "").strip()
            if pid and pid not in table:  # 같은 번호가 여러 행이면 첫 행 사용
                table[pid] = r
    return table


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=HERE / "ntis_results.csv")
    ap.add_argument("--output", type=Path, default=HERE / "ntis_results_enriched.csv")
    ap.add_argument("--lookup", type=Path, default=LOOKUP)
    args = ap.parse_args()

    if not args.lookup.exists():
        raise SystemExit(f"KISTI 조회표가 없습니다: {args.lookup}")
    lookup = load_lookup(args.lookup)
    print(f"KISTI 조회표 {len(lookup)}건 로드")

    with args.input.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    base_fields = list(rows[0].keys()) if rows else []
    out_fields = base_fields + [c for c in ADD_COLS if c not in base_fields]

    matched = 0
    with args.output.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        for r in rows:
            year = (r.get("등록일") or "")[:4]
            r["등록연도"] = year
            if r.get("컬렉션") == "project":
                k = lookup.get(r.get("결과ID", "").strip())
            else:
                k = None
            if k:
                matched += 1
                r["KISTI수록여부"] = "수록"
                for c in LOOKUP_COLS:
                    if c != "과제고유번호":
                        r[f"KISTI_{c}"] = fmt_money(k.get(c, "")) if c.endswith("(원)") or c in MONEY_COLS else k.get(c, "")
            else:
                if r.get("컬렉션") == "project":
                    r["KISTI수록여부"] = "미수록(2025년 이후 등록 가능)" if year and int(year) > CUTOFF_YEAR else "미수록(확인 필요)"
                else:
                    r["KISTI수록여부"] = "해당없음(API 필드만 제공)"
            w.writerow(r)
    print(f"저장: {args.output} ({len(rows)}행, KISTI 매칭 {matched}건)")


if __name__ == "__main__":
    main()
