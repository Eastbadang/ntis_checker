#!/usr/bin/env python3
"""수요조사서 중복성 점검 (PySide6 · LGPL, PyQt6 대체).

사용 흐름
  1) 수요조사서 파일을 창에 끌어다 놓기 (HWP·HWPX·DOCX·PDF·TXT, 여러 건은 엑셀·CSV 일괄 양식)
  2) [점검하기] → NTIS 자동 검색 + KISTI 기준DB 비교
  3) 판정 카드 확인 → [결과 저장] (경로·파일명 지정, Word .docx 와 .md 함께 저장)

실행:  python survey_checker_gui.py      설치:  pip install -r requirements.txt
"""
from __future__ import annotations

import copy
import html
import re
import sys
from pathlib import Path

try:
    from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
    from PySide6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
        QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
        QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QTextEdit,
        QVBoxLayout, QWidget,
    )
except ImportError:
    print("PySide6가 필요합니다. 명령 프롬프트에서 'pip install PySide6' 를 실행하세요.")
    sys.exit(1)

import app_config as cfg
import ntis_fetch
import survey_core as core

APP_TITLE = "수요조사서 중복성 점검"
SUPPORTED = {".hwp", ".hwpx", ".docx", ".pdf", ".txt", ".xlsx", ".xlsm", ".csv"}
OPEN_FILTER = "수요조사서 (*.hwp *.hwpx *.docx *.pdf *.txt *.xlsx *.xlsm *.csv);;모든 파일 (*)"
VERDICT_COLORS = {"유지": "#2e7d32", "개선": "#ef6c00", "불가": "#c62828", "판단 보류": "#616161"}
HEADLINES = {
    "유지": "중복 가능성이 낮습니다. 현재 방향을 유지하세요.",
    "개선": "비슷한 과제가 있습니다. 차별성을 보강해 주세요.",
    "불가": "기존 과제와 사실상 겹칩니다. 다시 기획해 주세요.",
    "판단 보류": "비교할 자료가 없어 판정하지 못했습니다.",
}
MODE_LABELS = {"project": "과제", "paper": "논문", "patent": "특허", "report": "연구보고서"}
TEMPLATE_HEADERS = ["번호", "기술명", "키워드", "개요(필요성)", "연구목표", "연구내용", "기대효과"]
TEMPLATE_EXAMPLE = ["1", "예시) 스마트 양식장 수질 예측 플랫폼", "스마트양식, 수질예측, 용존산소",
                    "양식장 집단 폐사가 반복됨", "수질 예측 정확도 90% 이상", "센서 데이터 수집, 예측 모델 개발",
                    "폐사 피해 감소"]
PRIVACY_NOTE = ("※ 회사 기밀·개인정보(연구책임자 성명 등)는 비식별화한 뒤 넣으세요. "
                "NTIS 검색에는 기술명·키워드만 쓰며, 수요조사서 본문은 외부로 보내지 않습니다.")


def _safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text or "수요조사")[:60].strip("_") or "수요조사"


class Worker(QObject):
    """오래 걸리는 점검을 별도 스레드에서 실행 (화면 위젯은 건드리지 않음)."""
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, job):
        super().__init__()
        self._job = job

    @Slot()
    def run(self):
        try:
            self.finished.emit(self._job(self.progress.emit))
        except Exception as e:  # 화면에 오류 내용을 보여 주기 위해 모든 예외를 받음
            self.failed.emit(f"{type(e).__name__}: {e}")


# ====================================================================== 보조 창
class EditDialog(QDialog):
    """자동으로 나눈 수요조사서 내용 확인·수정."""

    def __init__(self, survey: core.Survey, parent=None):
        super().__init__(parent)
        self.setWindowTitle("수요조사서 내용 확인·수정")
        self.resize(760, 680)
        form = QFormLayout(self)
        self.title = QLineEdit(survey.title)
        self.keywords = QLineEdit(survey.keywords)
        self.keywords.setPlaceholderText("쉼표로 구분. 비워 두면 기술명에서 검색어를 만듭니다.")
        self.boxes = {}
        form.addRow("기술명", self.title)
        form.addRow("키워드", self.keywords)
        for fld in ("overview", "goal", "content", "effect"):
            box = QPlainTextEdit(getattr(survey, fld))
            self.boxes[fld] = box
            form.addRow(core.FIELD_LABELS[fld], box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def apply_to(self, survey: core.Survey) -> None:
        survey.title = self.title.text().strip()
        survey.keywords = self.keywords.text().strip()
        for fld, box in self.boxes.items():
            setattr(survey, fld, box.toPlainText().strip())


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, key_status: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.resize(720, 420)
        self.settings = copy.deepcopy(settings)
        v = QVBoxLayout(self)

        kbox = QGroupBox("KISTI 2019~2024 기준DB")
        kf = QFormLayout(kbox)
        self.use_kisti = QCheckBox("기준DB의 과제도 함께 비교 (처음 한 번 읽을 때 수십 초 걸림)")
        self.use_kisti.setChecked(bool(settings.get("use_kisti", True)))
        self.kisti = QLineEdit(settings.get("kisti_db_path", ""))
        self.kisti.setPlaceholderText(f"비워 두면 프로그램 폴더 주변의 '{cfg.KISTI_FILE_NAME}'을 자동으로 찾습니다.")
        found = cfg.find_kisti_db(settings)
        self.kisti_state = QLabel(f"현재 사용할 파일: {found}" if found else "현재 사용할 파일: 찾지 못함")
        self.kisti_state.setWordWrap(True)
        kf.addRow(self.use_kisti)
        kf.addRow("파일 경로", self._with_browse(self.kisti, self._browse_kisti))
        kf.addRow(self.kisti_state)
        v.addWidget(kbox)

        nbox = QGroupBox("NTIS 자동 검색")
        nf = QFormLayout(nbox)
        row = QHBoxLayout()
        self.modes = {}
        for mode, label in MODE_LABELS.items():
            cb = QCheckBox(label)
            cb.setChecked(mode in settings.get("ntis_modes", []))
            self.modes[mode] = cb
            row.addWidget(cb)
        row.addStretch(1)
        nf.addRow("검색 대상", row)
        key_lbl = QLabel(key_status)
        key_lbl.setWordWrap(True)
        nf.addRow("인증키", key_lbl)
        v.addWidget(nbox)

        obox = QGroupBox("저장")
        of = QFormLayout(obox)
        self.outdir = QLineEdit(settings.get("output_dir", ""))
        self.outdir.setPlaceholderText("결과 저장 창이 처음 열릴 폴더 (비우면 문서 폴더)")
        of.addRow("기본 저장 폴더", self._with_browse(self.outdir, self._browse_outdir))
        v.addWidget(obox)

        info = QLabel(f"판정 기준값(불가·개선 기준 등)은 화면에 두지 않았습니다. 바꾸려면 메모장으로 "
                      f"{cfg.SETTINGS_FILE} 의 \"criteria\" 값을 고치세요.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#555;")
        v.addWidget(info)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    @staticmethod
    def _with_browse(edit: QLineEdit, slot) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QPushButton("찾아보기…")
        b.clicked.connect(slot)
        h.addWidget(b)
        return w

    def _browse_kisti(self):
        path, _ = QFileDialog.getOpenFileName(self, "KISTI 기준DB 선택", self.kisti.text(), "CSV (*.csv)")
        if path:
            self.kisti.setText(path)
            self.kisti_state.setText(f"현재 사용할 파일: {path}")

    def _browse_outdir(self):
        path = QFileDialog.getExistingDirectory(self, "기본 저장 폴더 선택", self.outdir.text())
        if path:
            self.outdir.setText(path)

    def values(self) -> dict:
        s = self.settings
        s["use_kisti"] = self.use_kisti.isChecked()
        s["kisti_db_path"] = self.kisti.text().strip()
        s["ntis_modes"] = [m for m, cb in self.modes.items() if cb.isChecked()]
        s["output_dir"] = self.outdir.text().strip()
        return s


# ====================================================================== 메인 창
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(980, 820)
        self.setAcceptDrops(True)
        self.settings = cfg.load_settings()
        self.entries: list[dict] = []          # {"survey": Survey, "result": Result|None}
        self.current = -1
        self.last_batch: list | None = None    # 마지막 '전체 점검' 결과
        self.kisti: core.KistiIndex | None = None
        self.kisti_path = ""
        self._thread: QThread | None = None
        self._worker: Worker | None = None
        self._batch_run = False
        self._build_ui()
        self._refresh()

    # ------------------------------------------------------------ 화면
    def _build_ui(self):
        central = QWidget()
        v = QVBoxLayout(central)

        top = QHBoxLayout()
        title = QLabel(APP_TITLE)
        title.setStyleSheet("font-size:16pt; font-weight:bold;")
        top.addWidget(title)
        top.addStretch(1)
        self.btn_template = QPushButton("일괄 양식 받기")
        self.btn_template.clicked.connect(self._save_template)
        self.btn_settings = QPushButton("설정")
        self.btn_settings.clicked.connect(self._open_settings)
        top.addWidget(self.btn_template)
        top.addWidget(self.btn_settings)
        v.addLayout(top)

        self.drop = QLabel("수요조사서 파일을 여기에 끌어다 놓으세요\n\n"
                           "HWP · HWPX · DOCX · PDF · TXT  /  여러 건은 엑셀·CSV 일괄 양식")
        self.drop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop.setMinimumHeight(120)
        self.drop.setStyleSheet("border:2px dashed #90a4ae; border-radius:10px; color:#455a64; "
                                "font-size:13pt; background:#f5f7f8;")
        v.addWidget(self.drop)

        row = QHBoxLayout()
        self.btn_open = QPushButton("파일 선택…")
        self.btn_open.clicked.connect(self._open_files)
        row.addWidget(self.btn_open)
        self.cb_items = QComboBox()
        self.cb_items.currentIndexChanged.connect(self._on_pick)
        row.addWidget(self.cb_items, 1)
        self.btn_edit = QPushButton("내용 확인·수정")
        self.btn_edit.clicked.connect(self._edit)
        row.addWidget(self.btn_edit)
        v.addLayout(row)
        self.lbl_loaded = QLabel("")
        self.lbl_loaded.setWordWrap(True)
        self.lbl_loaded.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.lbl_loaded)

        act = QHBoxLayout()
        self.btn_check = QPushButton("점검하기")
        self.btn_check.setMinimumHeight(46)
        self.btn_check.setStyleSheet("font-size:14pt; font-weight:bold;")
        self.btn_check.clicked.connect(self._check_current)
        self.btn_all = QPushButton("전체 점검")
        self.btn_all.setMinimumHeight(46)
        self.btn_all.clicked.connect(self._check_all)
        act.addWidget(self.btn_check, 3)
        act.addWidget(self.btn_all, 1)
        v.addLayout(act)
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color:#555;")
        v.addWidget(self.lbl_status)

        # 판정 카드 (한 장 요약)
        self.card = QFrame()
        self.card.setFrameShape(QFrame.Shape.StyledPanel)
        cv = QVBoxLayout(self.card)
        self.lbl_verdict = QLabel("")
        self.lbl_headline = QLabel("")
        self.lbl_headline.setWordWrap(True)
        self.lbl_headline.setStyleSheet("font-size:12pt;")
        self.lbl_body = QLabel("")
        self.lbl_body.setWordWrap(True)
        self.lbl_body.setTextFormat(Qt.TextFormat.RichText)
        cv.addWidget(self.lbl_verdict)
        cv.addWidget(self.lbl_headline)
        cv.addWidget(self.lbl_body)
        crow = QHBoxLayout()
        self.btn_save = QPushButton("결과 저장… (Word·md)")
        self.btn_save.clicked.connect(self._save)
        self.btn_detail = QPushButton("자세히 보기 ▼")
        self.btn_detail.clicked.connect(self._toggle_detail)
        crow.addWidget(self.btn_save)
        crow.addStretch(1)
        crow.addWidget(self.btn_detail)
        cv.addLayout(crow)
        v.addWidget(self.card)

        # 상세 (접힘)
        self.detail = QWidget()
        dv = QVBoxLayout(self.detail)
        dv.setContentsMargins(0, 0, 0, 0)
        self.tbl = QTableWidget(0, 6)
        self.tbl.setHorizontalHeaderLabels(["순위", "출처", "구분", "제목", "연도", "유사도"])
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hdr = self.tbl.horizontalHeader()
        for col in range(6):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.txt = QTextEdit()
        self.txt.setReadOnly(True)
        dv.addWidget(self.tbl, 2)
        dv.addWidget(self.txt, 3)
        v.addWidget(self.detail, 1)

        note = QLabel(PRIVACY_NOTE)
        note.setWordWrap(True)
        note.setStyleSheet("color:#777; font-size:9pt;")
        v.addWidget(note)
        self.setCentralWidget(central)

    # ------------------------------------------------------------ 상태 갱신
    def _refresh(self):
        has = bool(self.entries)
        many = len(self.entries) > 1
        self.cb_items.setVisible(many)
        self.btn_edit.setEnabled(has and self._thread is None)
        self.btn_check.setEnabled(has and self._thread is None)
        self.btn_all.setVisible(many)
        self.btn_all.setEnabled(many and self._thread is None)
        if not has:
            self.lbl_loaded.setText("")
            self._show_result(None)
            return
        s = self.entries[self.current]["survey"]
        kw = html.escape(s.keywords) if s.keywords else "(없음 · 기술명에서 검색어를 만듭니다)"
        filled = [core.FIELD_LABELS[f] for f in ("overview", "goal", "content", "effect") if getattr(s, f)]
        self.lbl_loaded.setText(
            f"<b>{html.escape(s.display_name())}</b><br>키워드: {kw}<br>"
            f"<span style='color:#666'>인식한 항목: {', '.join(filled) or '없음'} · 출처: {html.escape(s.source)}</span>")
        self._show_result(self.entries[self.current].get("result"))

    def _fill_combo(self):
        self.cb_items.blockSignals(True)
        self.cb_items.clear()
        for e in self.entries:
            res = e.get("result")
            self.cb_items.addItem((f"[{res.verdict}] " if res else "") + e["survey"].display_name())
        self.cb_items.setCurrentIndex(max(self.current, 0))
        self.cb_items.blockSignals(False)

    @Slot(int)
    def _on_pick(self, index: int):
        if 0 <= index < len(self.entries):
            self.current = index
            self._refresh()

    # ------------------------------------------------------------ 불러오기
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        self._load_paths(paths)

    def _open_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "수요조사서 선택", "", OPEN_FILTER)
        self._load_paths(paths)

    def _load_paths(self, paths: list[str]):
        if not paths or self._thread is not None:
            return
        loaded, errors = [], []
        for p in paths:
            path = Path(p)
            if path.suffix.lower() not in SUPPORTED:
                errors.append(f"{path.name}: 지원하지 않는 형식입니다.")
                continue
            try:
                loaded += core.load_survey_file(path)
            except Exception as e:
                errors.append(f"{path.name}: {e}")
        if loaded:
            self.entries = [{"survey": s, "result": None} for s in loaded]
            self.current = 0
            self.last_batch = None
            self._fill_combo()
            self._refresh()
            self.lbl_status.setText(f"{len(loaded)}건을 불러왔습니다. [점검하기]를 누르세요.")
        if errors:
            QMessageBox.warning(self, APP_TITLE, "다음 파일을 읽지 못했습니다.\n\n" + "\n".join(errors))

    def _edit(self):
        if not self.entries:
            return
        s = self.entries[self.current]["survey"]
        dlg = EditDialog(s, self)
        if dlg.exec():                       # 확인 = 1, 취소 = 0
            dlg.apply_to(s)
            self.entries[self.current]["result"] = None
            self.last_batch = None               # 내용이 바뀌었으므로 이전 전체 점검 결과는 저장하지 않음
            self._fill_combo()
            self._refresh()

    def _save_template(self):
        start = self.settings.get("output_dir") or str(Path.home() / "Documents")
        path, _ = QFileDialog.getSaveFileName(self, "일괄 양식 저장", str(Path(start) / "수요조사_일괄양식.csv"),
                                              "CSV (엑셀에서 열림) (*.csv)")
        if path:
            core.write_csv([dict(zip(TEMPLATE_HEADERS, TEMPLATE_EXAMPLE))], Path(path))
            QMessageBox.information(self, APP_TITLE, f"양식을 저장했습니다.\n엑셀로 열어 한 행에 한 건씩 채운 뒤 "
                                                     f"이 창에 끌어다 놓으세요.\n\n{path}")

    # ------------------------------------------------------------ 설정
    def _key_status(self) -> str:
        try:
            key, src = ntis_fetch.load_key(cfg.APP_DIR)
        except RuntimeError as e:
            return str(e)
        if key:
            return f"확인됨 (읽은 위치: {src})"
        return (f"없음. 프로그램 폴더({cfg.APP_DIR})의 .env 파일에 'NTIS_APPRV_KEY=발급키' 한 줄을 넣거나 "
                f"환경변수 NTIS_APPRV_KEY 를 설정하세요. 키가 없으면 KISTI 기준DB로만 비교합니다.")

    def _open_settings(self):
        dlg = SettingsDialog(self.settings, self._key_status(), self)
        if dlg.exec():
            self.settings = dlg.values()
            self._save_settings()

    def _save_settings(self):
        try:
            cfg.save_settings(self.settings)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, f"설정을 저장하지 못했습니다.\n{e}")

    # ------------------------------------------------------------ 점검
    def _prepare(self):
        """점검 전에 인증키·기준DB를 확인. 비교 자료가 하나도 없으면 None."""
        try:
            key, _ = ntis_fetch.load_key(cfg.APP_DIR)
        except RuntimeError as e:
            QMessageBox.warning(self, APP_TITLE, str(e))
            return None
        modes = [m for m in self.settings.get("ntis_modes", []) if m in ntis_fetch.SERVICES]
        use_kisti = bool(self.settings.get("use_kisti", True))
        kisti_path = cfg.find_kisti_db(self.settings) if use_kisti else ""
        if not (key and modes) and not kisti_path:
            QMessageBox.warning(self, APP_TITLE, "비교할 자료가 없습니다.\n\n" + self._key_status() +
                                "\n\n또는 [설정]에서 KISTI 기준DB 파일 경로를 지정하세요.")
            return None
        return {"key": key, "modes": modes, "use_kisti": use_kisti, "kisti_path": kisti_path,
                "criteria": cfg.criteria_from(self.settings),
                "count": int(self.settings.get("ntis_count", 10)), "ktop": int(self.settings.get("kisti_top", 20))}

    def _make_job(self, targets: list, p: dict):
        def job(progress):
            kisti = self._get_kisti(p["kisti_path"], progress) if p["kisti_path"] else None
            out = []
            for n, (entry, snap) in enumerate(targets, 1):
                if len(targets) > 1:
                    progress(f"{n}/{len(targets)} 점검 중: {snap.display_name()}")
                items, notes = [], []
                if p["key"] and p["modes"]:
                    items, notes = ntis_fetch.search_survey(snap, p["key"], p["modes"], p["count"], progress)
                else:
                    notes.append("NTIS 인증키가 없어 NTIS 검색을 건너뛰었습니다.")
                if kisti is not None:
                    notes.append(f"KISTI 2019~2024 기준DB에서 후보 {p['ktop']}건 비교")
                elif p["use_kisti"]:
                    notes.append("KISTI 기준DB 파일을 찾지 못해 비교하지 않았습니다.")
                progress("판정 중...")
                res = core.analyze(snap, items, p["criteria"], kisti, p["ktop"])
                res.notes = notes
                out.append((entry, res))
            return out
        return job

    def _check_current(self):
        if not self.entries:
            return
        entry = self.entries[self.current]
        if entry["survey"].is_empty():
            QMessageBox.warning(self, APP_TITLE, "수요조사서 내용이 비어 있습니다. [내용 확인·수정]에서 확인하세요.")
            return
        p = self._prepare()
        if p:
            self._start(self._make_job([(entry, copy.copy(entry["survey"]))], p), batch=False)

    def _check_all(self):
        targets = [(e, copy.copy(e["survey"])) for e in self.entries if not e["survey"].is_empty()]
        if not targets:
            QMessageBox.information(self, APP_TITLE, "점검할 내용이 있는 수요조사서가 없습니다.")
            return
        p = self._prepare()
        if p:
            self._start(self._make_job(targets, p), batch=True)

    def _get_kisti(self, path: str, progress) -> core.KistiIndex:
        """작업 스레드에서 호출. 같은 파일이면 한 번만 읽음."""
        if self.kisti is None or self.kisti_path != path:
            progress("KISTI 기준DB를 읽는 중입니다 (처음 한 번만)...")
            self.kisti = core.KistiIndex.load(Path(path), progress)
            self.kisti_path = path
        return self.kisti

    def _start(self, job, batch: bool):
        if self._thread is not None:
            return
        self._batch_run = batch
        self._set_busy(True)
        thread = QThread(self)
        worker = Worker(job)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_done)
        self._thread, self._worker = thread, worker
        thread.start()

    def _set_busy(self, busy: bool):
        for b in (self.btn_check, self.btn_all, self.btn_open, self.btn_settings, self.btn_edit, self.btn_template):
            b.setEnabled(not busy)
        self.cb_items.setEnabled(not busy)
        self.setAcceptDrops(not busy)

    @Slot(str)
    def _on_progress(self, msg: str):
        self.lbl_status.setText(msg)

    @Slot(object)
    def _on_finished(self, results):
        for entry, res in results:
            if any(e is entry for e in self.entries):     # 점검 중 목록이 바뀌었으면 건너뜀
                entry["result"] = res
        self.last_batch = [r for _, r in results] if self._batch_run else None
        self._fill_combo()
        self._refresh()
        if self._batch_run:
            counts: dict[str, int] = {}
            for _, r in results:
                counts[r.verdict] = counts.get(r.verdict, 0) + 1
            self.lbl_status.setText("전체 점검 완료: " + ", ".join(f"{k} {v}건" for k, v in counts.items())
                                    + " · [결과 저장]을 누르면 전체 결과를 한 파일로 저장합니다.")
        elif results:
            self.lbl_status.setText("점검을 마쳤습니다.")

    @Slot(str)
    def _on_failed(self, msg: str):
        self.lbl_status.setText("점검 중 오류가 발생했습니다.")
        QMessageBox.critical(self, APP_TITLE, f"점검 중 오류가 발생했습니다.\n\n{msg}")

    @Slot()
    def _on_thread_done(self):
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread, self._worker = None, None
        self._set_busy(False)
        self._refresh()

    # ------------------------------------------------------------ 결과
    def _show_result(self, res):
        self.card.setVisible(res is not None)
        if res is None:
            self.detail.setVisible(False)
            self.btn_detail.setText("자세히 보기 ▼")
            return
        color = VERDICT_COLORS.get(res.verdict, "#000")
        self.lbl_verdict.setText(f"<span style='font-size:26pt; font-weight:bold; color:{color}'>{res.verdict}</span>"
                                 f"&nbsp;&nbsp;<span style='color:#555'>중복성 {res.degree} · "
                                 f"최고 유사도 {res.top_sim:.2f}</span>")
        self.lbl_verdict.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_headline.setText(HEADLINES.get(res.verdict, ""))
        parts = []
        if res.items:
            it = res.items[0]
            extra = ", ".join(x for x in (it.source, it.kind, it.year) if x)
            parts.append(f"<b>가장 비슷한 항목</b>: {html.escape(it.title)} ({html.escape(extra)}) · 유사도 {it.sim:.2f}")
        actions = [a for a in res.actions if not a.startswith("중복 가능성이 낮습니다")][:3]
        if actions:
            parts.append("<b>고칠 점</b><ol>" + "".join(f"<li>{html.escape(a)}</li>" for a in actions) + "</ol>")
        self.lbl_body.setText("<br>".join(parts))
        self.tbl.setRowCount(len(res.items))
        for row, it in enumerate(res.items):
            for col, val in enumerate([str(row + 1), it.source, it.kind, it.title, it.year, f"{it.sim:.2f}"]):
                self.tbl.setItem(row, col, QTableWidgetItem(val))
        self.txt.setMarkdown(core.to_markdown(res))

    def _toggle_detail(self):
        show = not self.detail.isVisible()
        self.detail.setVisible(show)
        self.btn_detail.setText("접기 ▲" if show else "자세히 보기 ▼")

    def _save(self):
        batch = self.last_batch
        res = self.entries[self.current].get("result") if self.entries else None
        if batch and len(batch) > 1:
            default = "수요조사_전체점검결과"
        elif res is not None:
            batch, default = None, f"{_safe_name(res.survey.display_name())}_중복성점검"
        else:
            QMessageBox.information(self, APP_TITLE, "먼저 [점검하기]를 누르세요.")
            return
        start = self.settings.get("output_dir") or str(Path.home() / "Documents")
        path, _ = QFileDialog.getSaveFileName(self, "결과 저장 (Word 파일과 .md 파일을 함께 저장합니다)",
                                              str(Path(start) / f"{default}.docx"), "Word 문서 (*.docx)")
        if not path:
            return
        target = Path(path)
        base = target.with_suffix("") if target.suffix.lower() in (".docx", ".md") else target
        md_path, docx_path = Path(f"{base}.md"), Path(f"{base}.docx")
        saved, problems = [], []
        try:
            md_path.write_text(core.to_markdown_many(batch) if batch else core.to_markdown(res), encoding="utf-8")
            saved.append(str(md_path))
        except OSError as e:
            problems.append(f".md 저장 실패: {e}")
        try:
            core.to_docx(batch if batch else res, docx_path)
            saved.append(str(docx_path))
        except Exception as e:                   # python-docx 내부 오류까지 받아 .md 저장 결과는 유지
            problems.append(f"Word 저장 실패: {e}")
        if batch:
            csv_path = Path(f"{base}_요약.csv")
            try:
                core.write_csv([core.summary_row(r) for r in batch], csv_path)
                saved.append(str(csv_path))
            except OSError as e:
                problems.append(f"요약 CSV 저장 실패: {e}")
        self.settings["output_dir"] = str(base.parent)
        self._save_settings()
        msg = ("저장했습니다.\n\n" + "\n".join(saved)) if saved else "저장하지 못했습니다."
        if problems:
            msg += "\n\n" + "\n".join(problems) + "\n(파일이 다른 프로그램에서 열려 있으면 닫고 다시 저장하세요.)"
        (QMessageBox.warning if problems else QMessageBox.information)(self, APP_TITLE, msg)

    def closeEvent(self, event):
        if self._thread is not None:   # 점검 작업은 중간에 멈출 수 없으므로 끝날 때까지 닫지 않음
            QMessageBox.information(self, APP_TITLE, "점검이 진행 중입니다. 끝난 뒤에 닫아 주세요.")
            event.ignore()
            return
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
