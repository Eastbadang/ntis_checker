#!/usr/bin/env python3
"""프로그램 설정 (settings.json). 판정 기준은 화면에 보이지 않고 이 파일에서만 바꿉니다.

인증키는 보안을 위해 이 파일에 저장하지 않습니다 (환경변수 NTIS_APPRV_KEY 또는 .env 사용).
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import survey_core as core

# exe 로 실행하면 exe 가 있는 폴더, 스크립트로 실행하면 이 파일이 있는 폴더
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "settings.json"
KISTI_FILE_NAME = "기준DB_2019_2024_연구과제.csv"

DEFAULTS = {
    "kisti_db_path": "",          # 비워 두면 프로그램 폴더 주변에서 자동으로 찾음
    "use_kisti": True,
    "output_dir": "",             # 마지막으로 저장한 폴더
    "ntis_modes": ["project", "paper", "patent", "report"],
    "ntis_count": 10,             # 서비스별 비교 건수
    "kisti_top": 20,
    "criteria": {"block": 0.85, "improve": 0.65, "crowd_sim": 0.50, "crowd_count": 5, "top_n": 10},
}


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    data = copy.deepcopy(DEFAULTS)
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return data
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k == "criteria" and isinstance(v, dict):
                data["criteria"].update({ck: cv for ck, cv in v.items() if ck in data["criteria"]})
            elif k in data:
                data[k] = v
    return data


def save_settings(data: dict, path: Path = SETTINGS_FILE) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def criteria_from(data: dict) -> core.Criteria:
    c = data.get("criteria", {})
    d = core.Criteria()
    try:
        crit = core.Criteria(block=float(c.get("block", d.block)), improve=float(c.get("improve", d.improve)),
                             crowd_sim=float(c.get("crowd_sim", d.crowd_sim)),
                             crowd_count=int(c.get("crowd_count", d.crowd_count)),
                             top_n=int(c.get("top_n", d.top_n)))
    except (TypeError, ValueError):
        return d
    return crit if 0 <= crit.improve < crit.block <= 1 else d     # 잘못된 값이면 기본값


def find_kisti_db(data: dict) -> str:
    """설정된 경로가 있으면 그것을, 없으면 프로그램 폴더 주변에서 기준DB 파일을 찾음."""
    configured = (data.get("kisti_db_path") or "").strip()
    if configured:
        return configured if Path(configured).exists() else ""
    for cand in (APP_DIR / KISTI_FILE_NAME, APP_DIR / "data" / KISTI_FILE_NAME,
                 APP_DIR.parent / "KISTI_중복성검토" / KISTI_FILE_NAME):
        if cand.exists():
            return str(cand)
    return ""
