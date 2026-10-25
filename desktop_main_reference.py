import csv
import json
import os
import re
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QFileDialog, QSpinBox, QDoubleSpinBox, QProgressBar, QTableWidget,
    QTableWidgetItem, QMessageBox, QGroupBox, QFormLayout, QHeaderView,
    QPlainTextEdit
)

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
PROFILE_DIR = APP_DIR / "chrome_profile"
RESULTS_DIR = APP_DIR / "results"
CDP_URL = "http://127.0.0.1:9222"
DEFAULT_URL = "https://supplier.meesho.com/"

DEFAULT_CONFIG = {
    "supplier_url": DEFAULT_URL,
    "change_button_selector": '[data-testid="change-front-image"]',
    "file_input_selector": "input[type='file']",
    "price_tab_selector": "#meesho_price",
    "shipping_selector": "",
    "price_selector": "",
    "wait_seconds": 0.6,
    "shipping_regex": r"(?i)shipping(?:\s*\(added separately\))?\s*(?:charge)?[^₹\n]{0,60}₹\s*([\d,]+(?:\.\d{1,2})?)",
    "fallback_shipping_regex": r"(?i)₹\s*([\d,]+(?:\.\d{1,2})?)\s*(?:shipping|delivery)",
    "price_regex": r"(?i)Meesho\s*price[^₹\n]{0,50}₹\s*([\d,]+(?:\.\d{1,2})?)",
}


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def find_chrome():
    candidates = []
    if os.name == "nt":
        for root in (os.environ.get("PROGRAMFILES", r"C:\\Program Files"),
                     os.environ.get("PROGRAMFILES(X86)", r"C:\\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            if root:
                candidates.extend([
                    Path(root) / "Google/Chrome/Application/chrome.exe",
                    Path(root) / "Chromium/Application/chrome.exe",
                ])
    else:
        for name in ("google-chrome", "chromium", "chromium-browser"):
            from shutil import which
            found = which(name)
            if found:
                return found
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def parse_shipping(text, cfg):
    patterns = [cfg.get("shipping_regex", ""), cfg.get("fallback_shipping_regex", "")]
    for pattern in patterns:
        if not pattern:
            continue
        try:
            match = re.search(pattern, text or "")
            if match:
                raw = match.group(1).replace(",", "").strip()
                return float(raw)
        except (re.error, ValueError, IndexError):
            continue
    return None


class BatchWorker(QThread):
    row_result = Signal(int, str, str, str, str)
    progress = Signal(int, int)
    log = Signal(str)
    finished_summary = Signal(str)

    def __init__(self, image_paths, cfg):
        super().__init__()
        self.image_paths = image_paths
        self.cfg = cfg
        self.results = []
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            self.finished_summary.emit("Playwright install nahi hai. README ke installation steps follow karein.")
            return

        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.connect_over_cdp(CDP_URL, timeout=5000)
                except Exception:
                    self.finished_summary.emit(
                        "Chrome connect nahi hua. Pehle 'Open Meesho Browser' button dabayein aur browser khula rehne dein."
                    )
                    return

                contexts = browser.contexts
                if not contexts:
                    self.finished_summary.emit("Chrome context nahi mila. Browser ko dobara open karein.")
                    return
                context = contexts[0]
                pages = context.pages
                page = pages[0] if pages else context.new_page()
                self.log.emit("Browser connected. Ensure karein ki aap Meesho supplier panel ke catalog form par hain.")

                for i, path in enumerate(self.image_paths):
                    if self._stop_requested:
                        self.log.emit("User ne processing stop ki.")
                        break
                    filename = Path(path).name
                    shipping_text = ""
                    price_text = ""
                    status = "Needs review"
                    shipping_amount = None
                    meesho_price = None
                    try:
                        # 1) Click CHANGE and prefer Playwright's file-chooser event.
                        # Some React forms create the hidden <input type=file> only after CHANGE is clicked,
                        # so we also inspect the page and its frames after the click.
                        change_selector = (self.cfg.get("change_button_selector") or "").strip()
                        changed = False
                        change_error = ""
                        if change_selector:
                            try:
                                change_button = page.locator(change_selector).first
                                if change_button.count() > 0 and change_button.is_visible():
                                    try:
                                        with page.expect_file_chooser(timeout=1500) as chooser_info:
                                            change_button.click(timeout=3000)
                                        chooser_info.value.set_files(path)
                                        changed = True
                                        self.log.emit(f"{filename}: CHANGE clicked; file chooser received image.")
                                    except Exception as chooser_error:
                                        change_error = str(chooser_error)
                                        self.log.emit(f"{filename}: chooser event not detected; checking hidden file inputs.")
                                else:
                                    change_error = f"CHANGE selector not found/visible: {change_selector}"
                            except Exception as e:
                                change_error = str(e)
                                self.log.emit(f"{filename}: CHANGE click failed; checking file inputs ({e}).")
                        else:
                            change_error = "CHANGE selector is blank"

                        # Fallback: search configured/default file-input selectors in the main page and frames.
                        # set_input_files works for hidden file inputs too; they do not need to be visible.
                        if not changed:
                            selectors = [
                                self.cfg.get("file_input_selector", ""),
                                "input[type='file']",
                                "input[type='file'][accept*='image']",
                                "input[accept*='image']",
                            ]
                            selectors = list(dict.fromkeys(s.strip() for s in selectors if s and s.strip()))
                            file_input = None
                            frame_diagnostics = []
                            for frame_index, frame in enumerate(page.frames):
                                try:
                                    frame_inputs = frame.locator("input[type='file']")
                                    frame_count = frame_inputs.count()
                                    frame_diagnostics.append(f"frame {frame_index}: {frame_count} file input(s)")
                                except Exception:
                                    frame_diagnostics.append(f"frame {frame_index}: unable to inspect")
                                for selector in selectors:
                                    try:
                                        locator = frame.locator(selector).first
                                        if locator.count() > 0:
                                            file_input = locator
                                            break
                                    except Exception:
                                        continue
                                if file_input is not None:
                                    break
                            if file_input is None:
                                raise RuntimeError(
                                    "Image upload input nahi mila. Pehle supplier page par Uploaded Images ke CHANGE button ko manually click karke dekhein; "
                                    "phir confirm karein ki catalog form khula hai. "
                                    f"CHANGE selector={change_selector!r}; file selector(s)={selectors!r}; "
                                    f"diagnostics={'; '.join(frame_diagnostics)}; click detail={change_error[:250]}"
                                )
                            file_input.set_input_files(path, timeout=15000)
                            changed = True
                            self.log.emit(f"{filename}: image selected using file-input fallback.")

                        wait_s = max(0.2, min(30.0, float(self.cfg.get("wait_seconds", 0.6))))
                        page.wait_for_timeout(wait_s * 1000)

                        # 2) Click Price tab so the updated price/settlement breakdown refreshes.
                        price_tab_selector = (self.cfg.get("price_tab_selector") or "").strip()
                        if price_tab_selector:
                            try:
                                price_tab = page.locator(price_tab_selector).first
                                if price_tab.count() > 0 and price_tab.is_visible():
                                    price_tab.click(timeout=5000)
                                    page.wait_for_timeout(wait_s * 1000)
                                    self.log.emit(f"{filename}: Price tab clicked; waiting for refreshed values.")
                                else:
                                    self.log.emit("Price tab selector not found/visible; checking current page text.")
                            except Exception as e:
                                self.log.emit(f"Price tab click failed; checking current page text ({e}).")

                        # 3) Read the shipping and Meesho price independently.
                        body_text = page.locator("body").inner_text(timeout=5000)
                        ship_selector = (self.cfg.get("shipping_selector") or "").strip()
                        if ship_selector:
                            try:
                                loc = page.locator(ship_selector).first
                                if loc.count() > 0:
                                    shipping_text = loc.inner_text(timeout=2500)
                            except Exception:
                                shipping_text = ""
                        if not shipping_text:
                            shipping_text = body_text
                        shipping_amount = parse_shipping(shipping_text, self.cfg)

                        price_selector = (self.cfg.get("price_selector") or "").strip()
                        if price_selector:
                            try:
                                loc = page.locator(price_selector).first
                                if loc.count() > 0:
                                    price_text = loc.inner_text(timeout=2500)
                            except Exception:
                                price_text = ""
                        if not price_text:
                            price_text = body_text
                        try:
                            price_match = re.search(self.cfg.get("price_regex", ""), price_text or "")
                            if price_match:
                                meesho_price = float(price_match.group(1).replace(",", "").strip())
                        except (re.error, ValueError, IndexError):
                            meesho_price = None

                        if shipping_amount is not None:
                            status = "Success" if meesho_price is not None else "Partial: shipping only"
                            self.log.emit(f"{filename}: shipping ₹{shipping_amount:g}; Meesho price {('₹'+format(meesho_price, 'g')) if meesho_price is not None else 'not found'}")
                        else:
                            status = "Needs review"
                            self.log.emit(f"{filename}: shipping amount text mein nahi mila. Price={meesho_price!r}; page/form state check karein.")
                    except Exception as e:
                        status = "Error"
                        self.log.emit(f"{filename}: {e}")

                    shipping_value = f"₹{shipping_amount:g}" if shipping_amount is not None else ""
                    price_value = f"₹{meesho_price:g}" if meesho_price is not None else ""
                    record = {
                        "image_name": filename,
                        "image_path": str(path),
                        "meesho_price_rs": meesho_price,
                        "shipping_rs": shipping_amount,
                        "status": status,
                        "checked_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    self.results.append(record)
                    self.row_result.emit(i, filename, price_value, shipping_value, status)
                    self.progress.emit(i + 1, len(self.image_paths))

                browser.close() if False else None  # Leave user's browser open.
                summary = self.export_results()
                self.finished_summary.emit(summary)
        except Exception as e:
            self.finished_summary.emit(f"Unexpected error: {e}")

    def export_results(self):
        RESULTS_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = RESULTS_DIR / f"shipping_report_{stamp}.csv"
        json_path = RESULTS_DIR / f"shipping_report_{stamp}.json"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["image_name", "image_path", "meesho_price_rs", "shipping_rs", "status", "checked_at"])
            writer.writeheader()
            writer.writerows(self.results)
        json_path.write_text(json.dumps(self.results, indent=2, ensure_ascii=False), encoding="utf-8")
        return f"Report save ho gayi:\nCSV: {csv_path}\nJSON: {json_path}\n\nSuccess: {sum(1 for r in self.results if r['status']=='Success')} | Review/Error: {sum(1 for r in self.results if r['status']!='Success')}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Meesho Shipping Charge Checker")
        self.resize(1050, 720)
        self.cfg = load_config()
        self.image_paths = []
        self.worker = None
        self.chrome_process = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setSpacing(10)

        heading = QLabel("Meesho Shipping Charge Checker")
        heading.setStyleSheet("font-size: 22px; font-weight: 700; color: #273c75;")
        sub = QLabel("Faster batch image upload • shipping text extraction • CSV + JSON export")
        sub.setStyleSheet("color: #555;")
        layout.addWidget(heading)
        layout.addWidget(sub)

        browser_group = QGroupBox("1. Supplier Browser")
        browser_layout = QHBoxLayout(browser_group)
        self.url_edit = QLineEdit(self.cfg.get("supplier_url", DEFAULT_URL))
        self.open_browser_btn = QPushButton("Open Meesho Browser")
        self.open_browser_btn.clicked.connect(self.open_browser)
        browser_layout.addWidget(QLabel("URL:"))
        browser_layout.addWidget(self.url_edit, 1)
        browser_layout.addWidget(self.open_browser_btn)
        layout.addWidget(browser_group)

        folder_group = QGroupBox("2. Image Batch")
        folder_layout = QHBoxLayout(folder_group)
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        browse_btn = QPushButton("Browse Images Folder")
        browse_btn.clicked.connect(self.choose_folder)
        self.wait_spin = QDoubleSpinBox()
        self.wait_spin.setRange(0.2, 30.0)
        self.wait_spin.setDecimals(1)
        self.wait_spin.setSingleStep(0.2)
        self.wait_spin.setSuffix(" sec")
        self.wait_spin.setValue(float(self.cfg.get("wait_seconds", 0.6)))
        folder_layout.addWidget(self.folder_edit, 1)
        folder_layout.addWidget(browse_btn)
        folder_layout.addWidget(QLabel("Wait:"))
        folder_layout.addWidget(self.wait_spin)
        layout.addWidget(folder_group)

        actions = QHBoxLayout()
        self.calibrate_btn = QPushButton("Calibration / Selectors")
        self.calibrate_btn.clicked.connect(self.open_calibration)
        self.start_btn = QPushButton("Process Images")
        self.start_btn.setStyleSheet("font-weight: bold; background: #18a558; color: white; padding: 8px 18px;")
        self.start_btn.clicked.connect(self.start_processing)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_processing)
        self.stop_btn.setEnabled(False)
        actions.addWidget(self.calibrate_btn)
        actions.addStretch(1)
        actions.addWidget(self.start_btn)
        actions.addWidget(self.stop_btn)
        layout.addLayout(actions)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Image Name", "Meesho Price", "Shipping (Rs)", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        log_group = QGroupBox("Activity Log")
        log_layout = QVBoxLayout(log_group)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(500)
        self.log_box.setMaximumHeight(120)
        log_layout.addWidget(self.log_box)
        layout.addWidget(log_group)

        note = QLabel("Note: Meesho page layout/eligibility ke hisaab se selectors aur charge text badal sakta hai. Unreadable amounts ko tool 'Needs review' mark karega.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #805500; padding: 4px;")
        layout.addWidget(note)

    def log(self, message):
        self.log_box.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def open_browser(self):
        self.cfg["supplier_url"] = self.url_edit.text().strip() or DEFAULT_URL
        save_config(self.cfg)
        chrome = find_chrome()
        PROFILE_DIR.mkdir(exist_ok=True)
        if not chrome:
            QMessageBox.warning(self, "Chrome not found", "Google Chrome nahi mila. Chrome install karein, phir app dobara kholein.")
            return
        try:
            # Dedicated Chrome profile prevents interference with a user's normal Chrome profile.
            args = [chrome, "--remote-debugging-port=9222", f"--user-data-dir={PROFILE_DIR}", "--no-first-run", self.cfg["supplier_url"]]
            self.chrome_process = subprocess.Popen(args)
            self.log("Dedicated Chrome launch kiya. Supplier panel mein manually login karein; CAPTCHA/OTP user khud complete kare.")
            QMessageBox.information(self, "Browser opened", "Chrome khul gaya hai. Meesho supplier panel mein login karein aur relevant catalog/image-upload form tak pahunch jaayein. Phir is app mein Process Images dabayein.")
        except Exception as e:
            QMessageBox.critical(self, "Browser error", str(e))

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder containing product images")
        if not folder:
            return
        exts = {".png", ".jpg", ".jpeg", ".webp"}
        self.image_paths = sorted(str(p) for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in exts)
        self.folder_edit.setText(folder)
        self.table.setRowCount(0)
        for path in self.image_paths:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(Path(path).name))
            self.table.setItem(row, 1, QTableWidgetItem(""))
            self.table.setItem(row, 2, QTableWidgetItem(""))
            self.table.setItem(row, 3, QTableWidgetItem("Queued"))
        self.progress.setValue(0)
        self.log(f"{len(self.image_paths)} images found in {folder}")
        if len(self.image_paths) > 100:
            self.log("Heads-up: selected folder has more than 100 images; consider splitting into batches.")

    def open_calibration(self):
        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle("Calibration / Selectors")
        dlg.resize(760, 420)
        form = QFormLayout(dlg)
        change_selector = QLineEdit(self.cfg.get("change_button_selector", '[data-testid="change-front-image"]'))
        file_selector = QLineEdit(self.cfg.get("file_input_selector", "input[type='file']"))
        price_tab_selector = QLineEdit(self.cfg.get("price_tab_selector", "#meesho_price"))
        ship_selector = QLineEdit(self.cfg.get("shipping_selector", ""))
        price_selector = QLineEdit(self.cfg.get("price_selector", ""))
        ship_regex = QLineEdit(self.cfg.get("shipping_regex", ""))
        fallback_regex = QLineEdit(self.cfg.get("fallback_shipping_regex", ""))
        price_regex = QLineEdit(self.cfg.get("price_regex", ""))
        help_label = QLabel("Flow: click CHANGE → select image → wait → click Price tab → capture Meesho price and shipping separately. Selectors must match the current Meesho page; verify results with a small test batch.")
        help_label.setWordWrap(True)
        form.addRow(help_label)
        form.addRow("CHANGE button CSS selector:", change_selector)
        form.addRow("Image file input CSS selector (fallback):", file_selector)
        form.addRow("Price tab CSS/text selector:", price_tab_selector)
        form.addRow("Shipping amount CSS selector (optional):", ship_selector)
        form.addRow("Meesho price CSS selector (optional):", price_selector)
        form.addRow("Primary shipping regex:", ship_regex)
        form.addRow("Fallback shipping regex:", fallback_regex)
        form.addRow("Meesho price regex:", price_regex)
        buttons = QHBoxLayout()
        save_btn = QPushButton("Save Calibration")
        close_btn = QPushButton("Close")
        buttons.addWidget(save_btn)
        buttons.addWidget(close_btn)
        form.addRow(buttons)

        def save():
            self.cfg["change_button_selector"] = change_selector.text().strip() or '[data-testid="change-front-image"]'
            self.cfg["file_input_selector"] = file_selector.text().strip() or "input[type='file']"
            self.cfg["price_tab_selector"] = price_tab_selector.text().strip()
            self.cfg["shipping_selector"] = ship_selector.text().strip()
            self.cfg["price_selector"] = price_selector.text().strip()
            self.cfg["shipping_regex"] = ship_regex.text().strip()
            self.cfg["fallback_shipping_regex"] = fallback_regex.text().strip()
            self.cfg["price_regex"] = price_regex.text().strip()
            self.cfg["wait_seconds"] = float(self.wait_spin.value())
            self.cfg["supplier_url"] = self.url_edit.text().strip() or DEFAULT_URL
            save_config(self.cfg)
            self.log("Calibration settings saved to config.json")
            QMessageBox.information(dlg, "Saved", "Calibration settings save ho gayi.")
        save_btn.clicked.connect(save)
        close_btn.clicked.connect(dlg.close)
        dlg.show()

    def start_processing(self):
        if not self.image_paths:
            QMessageBox.warning(self, "No images", "Pehle images wala folder select karein.")
            return
        if len(self.image_paths) > 100:
            answer = QMessageBox.question(self, "Large batch", f"{len(self.image_paths)} images selected hain. 50–100 images per batch recommended hai. Continue?", QMessageBox.Yes | QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        self.cfg["wait_seconds"] = float(self.wait_spin.value())
        self.cfg["supplier_url"] = self.url_edit.text().strip() or DEFAULT_URL
        save_config(self.cfg)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.setMaximum(len(self.image_paths))
        self.progress.setValue(0)
        self.worker = BatchWorker(self.image_paths, self.cfg)
        self.worker.row_result.connect(self.update_row)
        self.worker.progress.connect(self.update_progress)
        self.worker.log.connect(self.log)
        self.worker.finished_summary.connect(self.processing_finished)
        self.worker.start()
        self.log("Batch started.")

    def update_row(self, index, filename, price, shipping, status):
        if index >= self.table.rowCount():
            return
        self.table.setItem(index, 0, QTableWidgetItem(filename))
        self.table.setItem(index, 1, QTableWidgetItem(price))
        self.table.setItem(index, 2, QTableWidgetItem(shipping))
        status_item = QTableWidgetItem(status)
        if status == "Success":
            status_item.setForeground(Qt.darkGreen)
        elif status == "Error":
            status_item.setForeground(Qt.red)
        else:
            status_item.setForeground(Qt.darkYellow)
        self.table.setItem(index, 3, status_item)

    def update_progress(self, current, total):
        self.progress.setMaximum(total)
        self.progress.setValue(current)

    def stop_processing(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.log("Stop requested; current image will finish first.")
            self.stop_btn.setEnabled(False)

    def processing_finished(self, summary):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.log(summary.replace("\n", " | "))
        QMessageBox.information(self, "Processing finished", summary)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
