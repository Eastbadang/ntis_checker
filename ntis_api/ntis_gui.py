"""
NTIS 국가R&D OpenAPI GUI 클라이언트 v2.0 (PySide6)
- 결과 파일 저장 경로(Output Directory) 자유 지정
- 참조 파일(작업디렉토리, .env, kisti_lookup.csv, input_pjt_ids.csv, 검색어CSV) 경로 지정
- 과제번호 기반 정밀 조사 (Deep-Dive: 과제스펙 -> 연관특허/보고서/논문 -> KISTI 상세보강)
- 키워드 및 다중 쿼리 검색
"""

import os
import sys
import shutil
import subprocess
import pandas as pd
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, Slot, QSettings
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QPushButton, QTextEdit, QTableWidget,
    QTableWidgetItem, QTabWidget, QComboBox, QCheckBox, QSpinBox,
    QRadioButton, QButtonGroup, QFileDialog, QGroupBox, QSplitter,
    QProgressBar, QMessageBox, QHeaderView, QFrame, QToolBox
)
from PySide6.QtGui import QFont, QTextCursor, QColor, QIcon


# ----------------------------------------------------------------------
# 1. 백그라운드 워커 스레드 (비동기 서브프로세스 제어 & 실시간 로깅)
# ----------------------------------------------------------------------
class WorkerThread(QThread):
    log_signal = Signal(str)
    progress_signal = Signal(int)
    finished_signal = Signal(bool, str, str)  # success, message, result_csv_path

    def __init__(self, task_type: str, params: dict, paths: dict):
        super().__init__()
        self.task_type = task_type
        self.params = params
        self.paths = paths
        self.is_cancelled = False
        self.current_process = None

    def cancel(self):
        self.is_cancelled = True
        if self.current_process and self.current_process.poll() is None:
            try:
                self.current_process.terminate()
            except Exception:
                pass

    def run_subcommand(self, cmd: list, env_vars: dict = None) -> int:
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        self.log_signal.emit(f"[실행] {cmd_str}")

        work_dir = Path(self.paths["work_dir"])
        env = os.environ.copy()
        if env_vars:
            env.update(env_vars)

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self.current_process = subprocess.Popen(
                cmd,
                cwd=str(work_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                creationflags=creationflags,
                env=env
            )
        except Exception as e:
            self.log_signal.emit(f"[프로세스 시작 실패] {str(e)}")
            return -1

        while True:
            if self.is_cancelled:
                self.log_signal.emit("[사용자 요청으로 프로세스 중단 중...]")
                try:
                    self.current_process.terminate()
                except Exception:
                    pass
                return -1

            line_bytes = self.current_process.stdout.readline()
            if not line_bytes:
                break

            try:
                line_str = line_bytes.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    line_str = line_bytes.decode('cp949')
                except Exception:
                    line_str = line_bytes.decode('latin1', errors='replace')

            self.log_signal.emit(line_str.rstrip('\r\n'))

        self.current_process.stdout.close()
        return self.current_process.wait()

    def run(self):
        py_exec = sys.executable
        work_dir = Path(self.paths["work_dir"])
        out_dir = Path(self.paths["out_dir"])
        env_file = Path(self.paths["env_file"]) if self.paths.get("env_file") else None
        kisti_file = Path(self.paths["kisti_file"]) if self.paths.get("kisti_file") else None

        # 출력 폴더 생성
        out_dir.mkdir(parents=True, exist_ok=True)

        # .env 파일에서 인증키 로드
        custom_env = {}
        if env_file and env_file.exists():
            try:
                with open(env_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("NTIS_APPRV_KEY=") and len(line.split("=", 1)) == 2:
                            custom_env["NTIS_APPRV_KEY"] = line.split("=", 1)[1].strip()
                        elif line.startswith("NTIS_SEARCH_PROFILE=") and len(line.split("=", 1)) == 2:
                            custom_env["NTIS_SEARCH_PROFILE"] = line.split("=", 1)[1].strip()
                if "NTIS_APPRV_KEY" in custom_env:
                    self.log_signal.emit(f"[인증키] 지정된 .env에서 로드 완료: {env_file}")
            except Exception as e:
                self.log_signal.emit(f"[경고] .env 로드 오류: {str(e)}")

        # 외부 kisti_lookup.csv가 지정되어 있고 작업 폴더와 다른 경우 자동 동기화/보장
        if kisti_file and kisti_file.exists():
            target_kisti = work_dir / "kisti_lookup.csv"
            if kisti_file.resolve() != target_kisti.resolve():
                try:
                    self.log_signal.emit(f"[참조파일] KISTI 조견표 연결: {kisti_file} -> {target_kisti}")
                    shutil.copy2(kisti_file, target_kisti)
                except Exception as e:
                    self.log_signal.emit(f"[경고] kisti_lookup 동기화 오류: {str(e)}")

        profile = self.params.get("profile", "sample")

        try:
            # ----------------------------------------------------------
            # 1. API 통신 진단
            # ----------------------------------------------------------
            if self.task_type == "probe":
                cmd = [py_exec, "ntis_search_client.py", "--mode", "all", "--probe"]
                ret = self.run_subcommand(cmd, custom_env)
                default_probe_csv = work_dir / "search_out" / "진단결과.csv"
                dest_probe_csv = out_dir / "진단결과.csv"

                if default_probe_csv.exists() and default_probe_csv.resolve() != dest_probe_csv.resolve():
                    shutil.copy2(default_probe_csv, dest_probe_csv)

                final_target = dest_probe_csv if dest_probe_csv.exists() else default_probe_csv
                if ret == 0:
                    self.finished_signal.emit(True, f"진단 완료! 결과 저장: {final_target}", str(final_target))
                else:
                    self.finished_signal.emit(False, "진단 중 오류가 발생했습니다.", "")

            # ----------------------------------------------------------
            # 2. 키워드 / 배치 검색 (ntis_search_client.py)
            # ----------------------------------------------------------
            elif self.task_type == "search":
                mode = self.params["mode"]
                cmd = [py_exec, "ntis_search_client.py", "--mode", mode, "--profile", profile]

                if self.params.get("is_query_file", False):
                    q_file = self.params["query_file"]
                    cmd.extend(["--queries", q_file])
                else:
                    cmd.extend(["--text", self.params["text"]])

                cmd.extend(["--count", str(self.params["count"])])
                cmd.extend(["--max-pages", str(self.params["max_pages"])])

                if self.params.get("keep_personal", False):
                    cmd.append("--keep-personal")

                ret = self.run_subcommand(cmd, custom_env)

                # 결과 파일을 지정된 출력 저장 경로(out_dir)로 복사/저장
                default_res_csv = work_dir / "search_out" / f"ntis_{mode}_결과.csv"
                dest_res_csv = out_dir / f"ntis_{mode}_결과.csv"

                if default_res_csv.exists() and default_res_csv.resolve() != dest_res_csv.resolve():
                    try:
                        shutil.copy2(default_res_csv, dest_res_csv)
                        # raw 폴더도 복사
                        src_raw = work_dir / "search_out" / "raw"
                        dest_raw = out_dir / "raw"
                        if src_raw.exists():
                            shutil.copytree(src_raw, dest_raw, dirs_exist_ok=True)
                        self.log_signal.emit(f"[파일 저장] 지정된 결과 폴더로 저장 완료: {dest_res_csv}")
                    except Exception as e:
                        self.log_signal.emit(f"[파일 복사 경고] {str(e)}")

                final_csv = dest_res_csv if dest_res_csv.exists() else default_res_csv
                if ret == 0 and final_csv.exists():
                    self.finished_signal.emit(True, f"{mode} 검색 완료! 저장: {final_csv}", str(final_csv))
                elif ret == 0:
                    self.finished_signal.emit(True, f"{mode} 검색이 완료되었습니다 (검색 결과 0건).", "")
                else:
                    self.finished_signal.emit(False, "검색 중 오류가 발생했습니다.", "")

            # ----------------------------------------------------------
            # 3. 과제번호 기반 정밀 조사 (Deep-Dive Pipeline)
            # ----------------------------------------------------------
            elif self.task_type == "deep_dive":
                pjt_ids = self.params["pjt_ids"]
                do_search_detail = self.params.get("do_search_detail", True)
                do_related_client = self.params.get("do_related_client", True)
                do_enrich = self.params.get("do_enrich", True)
                collections = self.params.get("collections", ["project", "paper", "patent", "researchreport"])

                self.log_signal.emit(f"=== 과제번호 정밀 조사 시작: 대상 과제 {len(pjt_ids)}건 ===")
                self.log_signal.emit(f"대상 목록: {', '.join(pjt_ids)}")
                self.log_signal.emit(f"지정 결과 저장 경로: {out_dir}")

                last_target_csv = ""

                # Step 1: 과제 직접 검색
                if do_search_detail and not self.is_cancelled:
                    self.log_signal.emit("\n[Step 1/3] 과제 기본정보 및 상세 스펙 수집 (--pjt-id)")
                    for pjt_id in pjt_ids:
                        cmd = [py_exec, "ntis_search_client.py", "--mode", "project", "--pjt-id", pjt_id, "--profile", profile]
                        self.run_subcommand(cmd, custom_env)

                    default_pjt_csv = work_dir / "search_out" / "ntis_project_결과.csv"
                    dest_pjt_csv = out_dir / "ntis_project_결과.csv"
                    if default_pjt_csv.exists() and default_pjt_csv.resolve() != dest_pjt_csv.resolve():
                        shutil.copy2(default_pjt_csv, dest_pjt_csv)
                    if dest_pjt_csv.exists():
                        last_target_csv = str(dest_pjt_csv)

                # Step 2: 연관 컨텐츠 정밀 추적 (ntis_client.py)
                if do_related_client and not self.is_cancelled:
                    self.log_signal.emit("\n[Step 2/3] 연관 컨텐츠(유사과제/특허/보고서/논문) 정밀 추적")
                    input_pjt_csv = Path(self.paths.get("input_pjt_file") or (work_dir / "input_pjt_ids.csv"))
                    pd.DataFrame({"pjtId": pjt_ids}).to_csv(input_pjt_csv, index=False, encoding="utf-8-sig")

                    # 작업 디렉토리의 기본 input_pjt_ids.csv와 다른 경우 복사 보장
                    work_input = work_dir / "input_pjt_ids.csv"
                    if input_pjt_csv.resolve() != work_input.resolve():
                        shutil.copy2(input_pjt_csv, work_input)

                    cmd = [py_exec, "ntis_client.py", "--collections"] + collections
                    self.run_subcommand(cmd, custom_env)

                    # raw jsonl 저장 폴더 복사
                    raw_jsonl = work_dir / "ntis_results_raw.jsonl"
                    dest_jsonl = out_dir / "ntis_results_raw.jsonl"
                    if raw_jsonl.exists() and raw_jsonl.resolve() != dest_jsonl.resolve():
                        shutil.copy2(raw_jsonl, dest_jsonl)

                # Step 3: KISTI 상세정보 자동 보강 (ntis_enrich.py)
                if do_enrich and not self.is_cancelled:
                    self.log_signal.emit("\n[Step 3/3] KISTI 상세정보 자동 보강 조인 (enrich)")
                    cmd = [py_exec, "ntis_enrich.py"]
                    self.run_subcommand(cmd, custom_env)

                    default_enriched = work_dir / "ntis_results_enriched.csv"
                    dest_enriched = out_dir / "ntis_results_enriched.csv"
                    if default_enriched.exists() and default_enriched.resolve() != dest_enriched.resolve():
                        shutil.copy2(default_enriched, dest_enriched)
                    if dest_enriched.exists():
                        last_target_csv = str(dest_enriched)

                if self.is_cancelled:
                    self.finished_signal.emit(False, "정밀 조사가 사용자에 의해 취소되었습니다.", "")
                else:
                    self.finished_signal.emit(True, f"과제번호 정밀 조사 완료! 저장 경로: {out_dir}", last_target_csv)

        except Exception as e:
            self.log_signal.emit(f"[치명적 오류] {str(e)}")
            self.finished_signal.emit(False, str(e), "")


# ----------------------------------------------------------------------
# 2. 메인 GUI 윈도우
# ----------------------------------------------------------------------
class NTISSearchGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NTIS 국가R&D 심층 분석 및 중복성 검토 시스템 v2.0 (PySide6)")
        self.resize(1260, 920)

        # 설정 관리 (QSettings)
        self.settings = QSettings("NTIS_Analyzer", "ConfigPaths")

        self.setup_ui()
        self.apply_styles()
        self.load_saved_paths()
        self.check_env_status()

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(15, 12, 15, 15)
        main_layout.setSpacing(10)

        # ==============================================================
        # 1. 최상단 상태 & 프로필 바
        # ==============================================================
        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 8, 12, 8)

        self.lbl_env_status = QLabel("인증키(.env): 확인 중...")
        self.lbl_env_status.setFont(QFont("Malgun Gothic", 9, QFont.Bold))
        top_layout.addWidget(self.lbl_env_status)

        top_layout.addSpacing(20)
        top_layout.addWidget(QLabel("요청 프로필:"))
        self.combo_profile = QComboBox()
        self.combo_profile.addItems(["sample", "sample_nocmbn", "spec", "spec_cmbn"])
        self.combo_profile.setToolTip("NTIS 요청 방식 프로필 (기본값: sample)")
        top_layout.addWidget(self.combo_profile)

        top_layout.addStretch()

        self.btn_probe = QPushButton("⚡ API 통신 진단 (--probe)")
        self.btn_probe.clicked.connect(self.on_run_probe)
        top_layout.addWidget(self.btn_probe)

        main_layout.addWidget(top_bar)

        # ==============================================================
        # 2. 경로 및 참조 파일 설정 패널 (핵심 신규 기능)
        # ==============================================================
        path_group = QGroupBox("📁 파일 저장 경로 및 참조 파일(입력/조회표/.env) 설정")
        path_layout = QGridLayout(path_group)
        path_layout.setContentsMargins(12, 12, 12, 10)
        path_layout.setHorizontalSpacing(10)
        path_layout.setVerticalSpacing(8)

        # 1) 결과 파일 저장 경로 (Output Directory)
        lbl_out = QLabel("결과 저장 경로 (Output Dir):")
        lbl_out.setStyleSheet("font-weight: bold; color: #27ae60;")
        path_layout.addWidget(lbl_out, 0, 0)
        self.txt_out_dir = QLineEdit()
        self.txt_out_dir.setPlaceholderText("검색 및 정밀조사 결과 CSV/Raw 파일이 저장될 폴더")
        path_layout.addWidget(self.txt_out_dir, 0, 1)
        self.btn_browse_out = QPushButton("폴더 선택...")
        self.btn_browse_out.clicked.connect(self.on_browse_out_dir)
        path_layout.addWidget(self.btn_browse_out, 0, 2)

        # 2) 파이썬 스크립트 작업 폴더 (Working Dir)
        path_layout.addWidget(QLabel("스크립트 작업 경로 (Work Dir):"), 1, 0)
        self.txt_work_dir = QLineEdit()
        self.txt_work_dir.setPlaceholderText("ntis_search_client.py, ntis_client.py 등이 위치한 폴더")
        path_layout.addWidget(self.txt_work_dir, 1, 1)
        self.btn_browse_work = QPushButton("폴더 선택...")
        self.btn_browse_work.clicked.connect(self.on_browse_work_dir)
        path_layout.addWidget(self.btn_browse_work, 1, 2)

        # 3) 참조 파일: 인증키 파일 (.env)
        path_layout.addWidget(QLabel("참조 인증키 파일 (.env):"), 2, 0)
        self.txt_env_file = QLineEdit()
        self.txt_env_file.setPlaceholderText("NTIS_APPRV_KEY가 저장된 .env 파일 경로")
        path_layout.addWidget(self.txt_env_file, 2, 1)
        self.btn_browse_env = QPushButton("파일 선택...")
        self.btn_browse_env.clicked.connect(self.on_browse_env_file)
        path_layout.addWidget(self.btn_browse_env, 2, 2)

        # 4) 참조 파일: KISTI 과제 상세 조견표 (kisti_lookup.csv)
        path_layout.addWidget(QLabel("참조 KISTI 조견표 (kisti_lookup.csv):"), 3, 0)
        self.txt_kisti_file = QLineEdit()
        self.txt_kisti_file.setPlaceholderText("KISTI 과제 상세정보 매핑용 CSV 파일 경로")
        path_layout.addWidget(self.txt_kisti_file, 3, 1)
        self.btn_browse_kisti = QPushButton("파일 선택...")
        self.btn_browse_kisti.clicked.connect(self.on_browse_kisti_file)
        path_layout.addWidget(self.btn_browse_kisti, 3, 2)

        # 5) 경로 저장 및 초기화 버튼 바
        path_btn_box = QHBoxLayout()
        self.btn_save_paths = QPushButton("💾 현재 경로 설정 저장")
        self.btn_save_paths.clicked.connect(self.save_paths_settings)
        self.btn_reset_paths = QPushButton("🔄 기본값(현재 폴더)으로 초기화")
        self.btn_reset_paths.clicked.connect(self.reset_default_paths)
        path_btn_box.addWidget(self.btn_save_paths)
        path_btn_box.addWidget(self.btn_reset_paths)
        path_btn_box.addStretch()
        path_layout.addLayout(path_btn_box, 4, 1, 1, 2)

        main_layout.addWidget(path_group)

        # ==============================================================
        # 3. 중앙 탭 영역 (정밀 조사 탭 / 키워드 검색 탭)
        # ==============================================================
        self.tabs = QTabWidget()
        self.tab_deep_dive = QWidget()
        self.tab_search = QWidget()

        self.setup_deep_dive_tab()
        self.setup_search_tab()

        self.tabs.addTab(self.tab_deep_dive, "🎯 과제번호 기반 정밀 조사 (Deep-Dive)")
        self.tabs.addTab(self.tab_search, "🔍 키워드 및 다중 쿼리 검색")
        main_layout.addWidget(self.tabs)

        # ==============================================================
        # 4. 하단 분할 영역 (실시간 로그 & 결과 데이터 미리보기 테이블)
        # ==============================================================
        splitter = QSplitter(Qt.Vertical)

        # 1) 로그 영역
        log_group = QGroupBox("실시간 실행 로그")
        log_layout = QVBoxLayout(log_group)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.txt_log)

        action_bar = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        action_bar.addWidget(self.progress_bar)

        self.btn_cancel = QPushButton("⏹ 작업 중단")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.on_cancel_task)
        action_bar.addWidget(self.btn_cancel)

        self.btn_clear_log = QPushButton("로그 지우기")
        self.btn_clear_log.clicked.connect(self.txt_log.clear)
        action_bar.addWidget(self.btn_clear_log)

        log_layout.addLayout(action_bar)
        splitter.addWidget(log_group)

        # 2) 결과 테이블 영역
        result_group = QGroupBox("결과 데이터 미리보기 (Data Table)")
        result_layout = QVBoxLayout(result_group)

        table_ctrl = QHBoxLayout()
        table_ctrl.addWidget(QLabel("결과 내 필터 검색:"))
        self.txt_table_filter = QLineEdit()
        self.txt_table_filter.setPlaceholderText("표시된 결과 내 실시간 텍스트 필터...")
        self.txt_table_filter.textChanged.connect(self.filter_table)
        table_ctrl.addWidget(self.txt_table_filter)

        self.btn_open_excel = QPushButton("📊 엑셀/기본 앱으로 열기")
        self.btn_open_excel.clicked.connect(self.on_open_in_excel)
        table_ctrl.addWidget(self.btn_open_excel)

        self.btn_open_dir = QPushButton("📂 결과 저장 폴더 열기")
        self.btn_open_dir.clicked.connect(self.on_open_out_dir)
        table_ctrl.addWidget(self.btn_open_dir)

        result_layout.addLayout(table_ctrl)

        self.table_result = QTableWidget()
        self.table_result.setAlternatingRowColors(True)
        self.table_result.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        result_layout.addWidget(self.table_result)

        splitter.addWidget(result_group)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)

        main_layout.addWidget(splitter)
        self.current_result_csv = ""

    # ------------------------------------------------------------------
    # 탭 1: 과제번호 정밀 조사 탭
    # ------------------------------------------------------------------
    def setup_deep_dive_tab(self):
        layout = QVBoxLayout(self.tab_deep_dive)
        layout.setSpacing(10)

        info_lbl = QLabel(
            "선정된 핵심 선행과제 번호를 입력하면 **[과제 스펙 수집]** ➔ **[연관 특허/보고서/논문 추적]** ➔ "
            "**[KISTI 상세정보 보강]**을 순차 실행하여 지정한 저장 폴더에 최종 CSV 및 Raw 데이터를 저장합니다."
        )
        info_lbl.setWordWrap(True)
        layout.addWidget(info_lbl)

        # 과제번호 입력 및 참조 CSV 선택
        input_group = QGroupBox("조사 대상 과제번호 입력")
        grid = QGridLayout(input_group)

        grid.addWidget(QLabel("과제번호 직접 입력:"), 0, 0)
        self.txt_pjt_ids = QLineEdit()
        self.txt_pjt_ids.setPlaceholderText("예: 2410014068, 2410004369, 1415180036 (쉼표 또는 공백 구분)")
        grid.addWidget(self.txt_pjt_ids, 0, 1)

        grid.addWidget(QLabel("참조 과제번호 파일 (선택):"), 1, 0)
        self.txt_custom_pjt_file = QLineEdit()
        self.txt_custom_pjt_file.setPlaceholderText("input_pjt_ids.csv 등 과제번호 목록 CSV 파일 경로")
        grid.addWidget(self.txt_custom_pjt_file, 1, 1)

        self.btn_browse_pjt_csv = QPushButton("CSV 파일 선택...")
        self.btn_browse_pjt_csv.clicked.connect(self.on_browse_pjt_csv)
        grid.addWidget(self.btn_browse_pjt_csv, 1, 2)

        layout.addWidget(input_group)

        # 파이프라인 단계 선택
        opt_group = QGroupBox("정밀 조사 실행 단계 선택")
        opt_layout = QHBoxLayout(opt_group)

        self.chk_step1 = QCheckBox("1단계: 과제 상세 스펙 조회 (--pjt-id)")
        self.chk_step1.setChecked(True)
        opt_layout.addWidget(self.chk_step1)

        self.chk_step2 = QCheckBox("2단계: 연관 컨텐츠(특허/보고서/논문) 수집")
        self.chk_step2.setChecked(True)
        opt_layout.addWidget(self.chk_step2)

        self.chk_step3 = QCheckBox("3단계: KISTI 상세정보 자동 보강 (enrich)")
        self.chk_step3.setChecked(True)
        opt_layout.addWidget(self.chk_step3)

        layout.addWidget(opt_group)

        # 연관 컨텐츠 수집 대상 컬렉션
        col_group = QGroupBox("연관 컨텐츠 수집 대상 컬렉션")
        col_layout = QHBoxLayout(col_group)
        self.chk_col_pjt = QCheckBox("유사 과제 (project)")
        self.chk_col_pjt.setChecked(True)
        self.chk_col_patent = QCheckBox("출원 특허 (patent)")
        self.chk_col_patent.setChecked(True)
        self.chk_col_report = QCheckBox("연구보고서 (researchreport)")
        self.chk_col_report.setChecked(True)
        self.chk_col_paper = QCheckBox("학술 논문 (paper)")
        self.chk_col_paper.setChecked(True)

        col_layout.addWidget(self.chk_col_pjt)
        col_layout.addWidget(self.chk_col_patent)
        col_layout.addWidget(self.chk_col_report)
        col_layout.addWidget(self.chk_col_paper)
        layout.addWidget(col_group)

        self.btn_start_deep_dive = QPushButton("🚀 과제번호 정밀 조사 시작 (Deep-Dive Pipeline)")
        self.btn_start_deep_dive.setObjectName("btnPrimary")
        self.btn_start_deep_dive.setFixedHeight(40)
        self.btn_start_deep_dive.clicked.connect(self.on_run_deep_dive)
        layout.addWidget(self.btn_start_deep_dive)

        layout.addStretch()

    # ------------------------------------------------------------------
    # 탭 2: 일반 키워드 및 다중 쿼리 검색 탭
    # ------------------------------------------------------------------
    def setup_search_tab(self):
        layout = QVBoxLayout(self.tab_search)
        layout.setSpacing(10)

        mode_box = QGroupBox("검색 방식 선택")
        mode_layout = QHBoxLayout(mode_box)
        self.rb_single_query = QRadioButton("단일 키워드 검색")
        self.rb_single_query.setChecked(True)
        self.rb_file_queries = QRadioButton("검색어 CSV 파일 일괄 검색 (--queries)")
        mode_layout.addWidget(self.rb_single_query)
        mode_layout.addWidget(self.rb_file_queries)
        layout.addWidget(mode_box)

        query_box = QGroupBox("검색어 설정")
        query_grid = QGridLayout(query_box)

        query_grid.addWidget(QLabel("단일 검색어:"), 0, 0)
        self.txt_single_query = QLineEdit()
        self.txt_single_query.setPlaceholderText("예: 수소상용차 전력분배 (공백은 묵시적 AND)")
        query_grid.addWidget(self.txt_single_query, 0, 1, 1, 2)

        query_grid.addWidget(QLabel("참조 검색어 CSV 파일:"), 1, 0)
        self.txt_query_file = QLineEdit()
        self.txt_query_file.setPlaceholderText("search_queries_additional.csv 경로")
        self.txt_query_file.setEnabled(False)
        query_grid.addWidget(self.txt_query_file, 1, 1)

        self.btn_browse_query_file = QPushButton("파일 선택...")
        self.btn_browse_query_file.setEnabled(False)
        self.btn_browse_query_file.clicked.connect(self.on_browse_query_file)
        query_grid.addWidget(self.btn_browse_query_file, 1, 2)

        self.rb_single_query.toggled.connect(self.on_query_mode_toggled)
        layout.addWidget(query_box)

        param_box = QGroupBox("수집 파라미터")
        param_grid = QGridLayout(param_box)

        param_grid.addWidget(QLabel("검색 대상(Mode):"), 0, 0)
        self.combo_mode = QComboBox()
        for text, data in [
            ("과제검색 (project)", "project"),
            ("학술논문 (paper)", "paper"),
            ("특허검색 (patent)", "patent"),
            ("시설장비 (equip)", "equip"),
            ("연구보고서 (report)", "report")
        ]:
            self.combo_mode.addItem(text, data)
        param_grid.addWidget(self.combo_mode, 0, 1)

        param_grid.addWidget(QLabel("페이지당 건수:"), 0, 2)
        self.spin_count = QSpinBox()
        self.spin_count.setRange(1, 100)
        self.spin_count.setValue(50)
        param_grid.addWidget(self.spin_count, 0, 3)

        param_grid.addWidget(QLabel("최대 페이지:"), 1, 0)
        self.spin_max_pages = QSpinBox()
        self.spin_max_pages.setRange(1, 20)
        self.spin_max_pages.setValue(2)
        param_grid.addWidget(self.spin_max_pages, 1, 1)

        self.chk_keep_personal = QCheckBox("개인정보(연구원 성명 등) 포함")
        param_grid.addWidget(self.chk_keep_personal, 1, 2, 1, 2)

        layout.addWidget(param_box)

        self.btn_start_search = QPushButton("🔍 키워드 검색 실행")
        self.btn_start_search.setObjectName("btnPrimary")
        self.btn_start_search.setFixedHeight(40)
        self.btn_start_search.clicked.connect(self.on_run_search)
        layout.addWidget(self.btn_start_search)

        layout.addStretch()

    # ------------------------------------------------------------------
    # UI 스타일시트
    # ------------------------------------------------------------------
    def apply_styles(self):
        style = """
        QWidget {
            font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
            font-size: 10pt;
            color: #2c3e50;
        }
        #topBar {
            background-color: #ecf0f1;
            border-radius: 6px;
            border: 1px solid #bdc3c7;
        }
        QGroupBox {
            font-weight: bold;
            border: 1px solid #dcdde1;
            border-radius: 6px;
            margin-top: 10px;
            padding-top: 12px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px;
            color: #2980b9;
        }
        QLineEdit, QComboBox, QSpinBox {
            padding: 5px 8px;
            border: 1px solid #bdc3c7;
            border-radius: 4px;
            background: #ffffff;
        }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
            border: 1px solid #3498db;
        }
        QPushButton {
            padding: 6px 14px;
            border-radius: 4px;
            border: 1px solid #bdc3c7;
            background-color: #f8f9fa;
        }
        QPushButton:hover {
            background-color: #e9ecef;
            border-color: #adb5bd;
        }
        QPushButton#btnPrimary {
            background-color: #2980b9;
            color: white;
            font-weight: bold;
            border: none;
        }
        QPushButton#btnPrimary:hover {
            background-color: #3498db;
        }
        QTableWidget {
            gridline-color: #e2e8f0;
            background-color: #ffffff;
            alternate-background-color: #f8fafc;
        }
        QHeaderView::section {
            background-color: #edf2f7;
            padding: 5px;
            font-weight: bold;
            border: 1px solid #cbd5e0;
        }
        QTextEdit {
            background-color: #1e293b;
            color: #f8fafc;
            border-radius: 4px;
        }
        """
        self.setStyleSheet(style)

    # ------------------------------------------------------------------
    # 경로 설정 관리 (QSettings 기반 불러오기 / 저장 / 초기화)
    # ------------------------------------------------------------------
    def load_saved_paths(self):
        curr_dir = Path.cwd()

        # 저장된 값이 없으면 기본 경로 할당
        saved_work = self.settings.value("work_dir", str(curr_dir))
        saved_out = self.settings.value("out_dir", str(curr_dir / "search_out"))
        saved_env = self.settings.value("env_file", str(curr_dir / ".env"))
        saved_kisti = self.settings.value("kisti_file", str(curr_dir / "kisti_lookup.csv"))
        saved_pjt = self.settings.value("input_pjt_file", str(curr_dir / "input_pjt_ids.csv"))

        self.txt_work_dir.setText(saved_work)
        self.txt_out_dir.setText(saved_out)
        self.txt_env_file.setText(saved_env)
        self.txt_kisti_file.setText(saved_kisti)
        self.txt_custom_pjt_file.setText(saved_pjt)

    def save_paths_settings(self):
        self.settings.setValue("work_dir", self.txt_work_dir.text().strip())
        self.settings.setValue("out_dir", self.txt_out_dir.text().strip())
        self.settings.setValue("env_file", self.txt_env_file.text().strip())
        self.settings.setValue("kisti_file", self.txt_kisti_file.text().strip())
        self.settings.setValue("input_pjt_file", self.txt_custom_pjt_file.text().strip())

        self.check_env_status()
        QMessageBox.information(self, "설정 저장", "지정한 파일 저장 경로 및 참조 파일 경로가 성공적으로 저장되었습니다.")
        self.append_log("[설정] 지정된 저장 경로 및 참조 파일 경로 저장 완료")

    def reset_default_paths(self):
        curr = Path.cwd()
        self.txt_work_dir.setText(str(curr))
        self.txt_out_dir.setText(str(curr / "search_out"))
        self.txt_env_file.setText(str(curr / ".env"))
        self.txt_kisti_file.setText(str(curr / "kisti_lookup.csv"))
        self.txt_custom_pjt_file.setText(str(curr / "input_pjt_ids.csv"))
        self.save_paths_settings()

    def get_current_paths(self) -> dict:
        return {
            "work_dir": self.txt_work_dir.text().strip() or str(Path.cwd()),
            "out_dir": self.txt_out_dir.text().strip() or str(Path.cwd() / "search_out"),
            "env_file": self.txt_env_file.text().strip(),
            "kisti_file": self.txt_kisti_file.text().strip(),
            "input_pjt_file": self.txt_custom_pjt_file.text().strip()
        }

    # ------------------------------------------------------------------
    # 파일/폴더 선택 다이얼로그들
    # ------------------------------------------------------------------
    def on_browse_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "결과 파일 저장 폴더 선택", self.txt_out_dir.text() or str(Path.cwd()))
        if d:
            self.txt_out_dir.setText(d)
            self.settings.setValue("out_dir", d)

    def on_browse_work_dir(self):
        d = QFileDialog.getExistingDirectory(self, "스크립트 작업 폴더(ntis_api) 선택", self.txt_work_dir.text() or str(Path.cwd()))
        if d:
            self.txt_work_dir.setText(d)
            self.settings.setValue("work_dir", d)
            self.check_env_status()

    def on_browse_env_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "참조 .env 파일 선택", self.txt_env_file.text() or str(Path.cwd()), "Env Files (*.env);;All Files (*)")
        if f:
            self.txt_env_file.setText(f)
            self.settings.setValue("env_file", f)
            self.check_env_status()

    def on_browse_kisti_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "참조 kisti_lookup.csv 선택", self.txt_kisti_file.text() or str(Path.cwd()), "CSV Files (*.csv)")
        if f:
            self.txt_kisti_file.setText(f)
            self.settings.setValue("kisti_file", f)

    def on_browse_pjt_csv(self):
        f, _ = QFileDialog.getOpenFileName(self, "과제번호 CSV 파일 선택", self.txt_custom_pjt_file.text() or str(Path.cwd()), "CSV Files (*.csv)")
        if f:
            self.txt_custom_pjt_file.setText(f)
            self.settings.setValue("input_pjt_file", f)
            try:
                df = pd.read_csv(f)
                pjt_col = None
                for col in df.columns:
                    if col.lower() in ["pjtid", "projectnumber", "과제번호", "기준id"]:
                        pjt_col = col
                        break
                if pjt_col is None:
                    pjt_col = df.columns[0]
                ids = df[pjt_col].dropna().astype(str).str.strip().tolist()
                self.txt_pjt_ids.setText(", ".join(ids))
                self.append_log(f"과제번호 CSV에서 {len(ids)}건 로드 완료: {f}")
            except Exception as e:
                QMessageBox.warning(self, "파일 오류", f"과제번호 CSV 로드 중 오류: {str(e)}")

    def on_browse_query_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "검색어 CSV 파일 선택", str(Path.cwd()), "CSV Files (*.csv)")
        if f:
            self.txt_query_file.setText(f)

    # ------------------------------------------------------------------
    # 기타 로직 및 워커 연동
    # ------------------------------------------------------------------
    def check_env_status(self):
        env_path = Path(self.txt_env_file.text().strip())
        if not env_path.exists():
            env_path = Path(self.txt_work_dir.text().strip()) / ".env"

        has_key = False
        if env_path.exists():
            try:
                with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip().startswith("NTIS_APPRV_KEY=") and len(line.strip().split("=", 1)[1]) > 5:
                            has_key = True
                            break
            except Exception:
                pass

        if has_key:
            self.lbl_env_status.setText(f"인증키: 🟢 로드됨 ({env_path.name})")
            self.lbl_env_status.setStyleSheet("color: #27ae60;")
        else:
            self.lbl_env_status.setText("인증키: 🔴 미설정 (키 필요)")
            self.lbl_env_status.setStyleSheet("color: #e74c3c;")

    def on_query_mode_toggled(self):
        is_single = self.rb_single_query.isChecked()
        self.txt_single_query.setEnabled(is_single)
        self.txt_query_file.setEnabled(not is_single)
        self.btn_browse_query_file.setEnabled(not is_single)

    def start_worker(self, task_type: str, params: dict):
        paths = self.get_current_paths()
        self.txt_log.append(f"\n[{pd.Timestamp.now().strftime('%H:%M:%S')}] 작업 시작: {task_type}")
        self.txt_log.append(f"결과 저장 위치: {paths['out_dir']}")
        self.progress_bar.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self.btn_start_search.setEnabled(False)
        self.btn_start_deep_dive.setEnabled(False)
        self.btn_probe.setEnabled(False)

        self.current_worker = WorkerThread(task_type, params, paths)
        self.current_worker.log_signal.connect(self.append_log)
        self.current_worker.finished_signal.connect(self.on_worker_finished)
        self.current_worker.start()

    def append_log(self, text: str):
        self.txt_log.append(text)
        self.txt_log.moveCursor(QTextCursor.End)

    def on_cancel_task(self):
        if self.current_worker and self.current_worker.isRunning():
            self.current_worker.cancel()
            self.btn_cancel.setEnabled(False)

    def on_worker_finished(self, success: bool, message: str, result_csv: str):
        self.progress_bar.setVisible(False)
        self.btn_cancel.setEnabled(False)
        self.btn_start_search.setEnabled(True)
        self.btn_start_deep_dive.setEnabled(True)
        self.btn_probe.setEnabled(True)

        if success:
            self.append_log(f"[성공] {message}")
            if result_csv and Path(result_csv).exists():
                self.load_csv_to_table(result_csv)
        else:
            self.append_log(f"[실패/중단] {message}")

    def on_run_probe(self):
        params = {"profile": self.combo_profile.currentText()}
        self.start_worker("probe", params)

    def on_run_search(self):
        mode = self.combo_mode.currentData()
        profile = self.combo_profile.currentText()
        is_query_file = self.rb_file_queries.isChecked()

        params = {
            "mode": mode,
            "profile": profile,
            "is_query_file": is_query_file,
            "count": self.spin_count.value(),
            "max_pages": self.spin_max_pages.value(),
            "keep_personal": self.chk_keep_personal.isChecked()
        }

        if is_query_file:
            q_file = self.txt_query_file.text().strip()
            if not q_file or not Path(q_file).exists():
                QMessageBox.warning(self, "입력 오류", "유효한 참조 검색어 CSV 파일을 선택해주세요.")
                return
            params["query_file"] = q_file
        else:
            text = self.txt_single_query.text().strip()
            if not text:
                QMessageBox.warning(self, "입력 오류", "검색어를 입력해주세요.")
                return
            params["text"] = text

        self.start_worker("search", params)

    def on_run_deep_dive(self):
        raw_ids = self.txt_pjt_ids.text().replace("\n", ",").replace(";", ",")
        pjt_ids = [x.strip() for x in raw_ids.split(",") if x.strip()]

        if not pjt_ids:
            # 직접 입력이 없으면 참조 CSV 파일에서 읽기 시도
            custom_pjt = Path(self.txt_custom_pjt_file.text().strip())
            if custom_pjt.exists():
                try:
                    df = pd.read_csv(custom_pjt)
                    pjt_col = [c for c in df.columns if c.lower() in ["pjtid", "projectnumber", "과제번호"]]
                    col = pjt_col[0] if pjt_col else df.columns[0]
                    pjt_ids = df[col].dropna().astype(str).str.strip().tolist()
                except Exception:
                    pass

        if not pjt_ids:
            QMessageBox.warning(self, "입력 오류", "조사할 과제번호를 입력하거나 과제번호 CSV 파일을 지정해주세요.")
            return

        collections = []
        if self.chk_col_pjt.isChecked(): collections.append("project")
        if self.chk_col_patent.isChecked(): collections.append("patent")
        if self.chk_col_report.isChecked(): collections.append("researchreport")
        if self.chk_col_paper.isChecked(): collections.append("paper")

        params = {
            "pjt_ids": pjt_ids,
            "profile": self.combo_profile.currentText(),
            "do_search_detail": self.chk_step1.isChecked(),
            "do_related_client": self.chk_step2.isChecked(),
            "do_enrich": self.chk_step3.isChecked(),
            "collections": collections
        }

        self.start_worker("deep_dive", params)

    def load_csv_to_table(self, csv_path: str):
        try:
            self.current_result_csv = csv_path
            df = pd.read_csv(csv_path)

            preferred_cols = [
                "기준ID", "기준과제번호", "ProjectNumber", "과제번호", "컬렉션",
                "ProjectTitle_Korean", "과제명", "제목", "주요내용",
                "ResearchAgency_Name", "연구수행기관", "ProjectYear", "과제년도",
                "TotalFunds", "총연구비", "Abstract_Full"
            ]
            cols = [c for c in preferred_cols if c in df.columns]
            if len(cols) < 3:
                cols = df.columns.tolist()[:10]

            self.table_result.setRowCount(0)
            self.table_result.setColumnCount(len(cols))
            self.table_result.setHorizontalHeaderLabels(cols)

            max_rows = min(300, len(df))
            self.table_result.setRowCount(max_rows)

            for r in range(max_rows):
                for c, col_name in enumerate(cols):
                    val = str(df.iloc[r][col_name])
                    if val == "nan": val = ""
                    self.table_result.setItem(r, c, QTableWidgetItem(val))

            self.append_log(f"테이블 미리보기 갱신 완료: {csv_path} (총 {len(df)}행)")
        except Exception as e:
            self.append_log(f"[테이블 로드 오류] {str(e)}")

    def filter_table(self, filter_text: str):
        filter_text = filter_text.strip().lower()
        for row in range(self.table_result.rowCount()):
            row_match = False
            for col in range(self.table_result.columnCount()):
                item = self.table_result.item(row, col)
                if item and filter_text in item.text().lower():
                    row_match = True
                    break
            self.table_result.setRowHidden(row, not row_match)

    def on_open_in_excel(self):
        if not self.current_result_csv or not Path(self.current_result_csv).exists():
            out_dir = Path(self.txt_out_dir.text().strip())
            csvs = list(out_dir.glob("*.csv"))
            if csvs:
                self.current_result_csv = str(csvs[0])
            else:
                QMessageBox.information(self, "안내", "지정된 결과 저장 폴더에 CSV 파일이 없습니다.")
                return

        os.startfile(self.current_result_csv) if sys.platform == "win32" else subprocess.run(["open", self.current_result_csv])

    def on_open_out_dir(self):
        out_dir = Path(self.txt_out_dir.text().strip())
        out_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(out_dir)) if sys.platform == "win32" else subprocess.run(["open", str(out_dir)])


# ----------------------------------------------------------------------
# 프로그램 진입점
# ----------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    window = NTISSearchGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
