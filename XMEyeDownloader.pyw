"""GUI downloader for XMEye / Xiongmai DVR/NVR recordings (TCP 34567)."""

import json
import logging
import os
import queue
import shutil
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from xmeye import Cancelled, DVRClient, DVRError, to_mp4

APP_DIR = Path(__file__).resolve().parent
SETTINGS = APP_DIR / "settings.json"
FFMPEG = shutil.which("ffmpeg")
DEBUG_LOG = APP_DIR / "dvr_debug.log"
RETRIES = 3

DEFAULTS = {
    "ip": "0.0.0.0",
    "port": "34567",
    "user": "admin",
    "start": "14.09.2026 00:00",
    "end": "25.09.2026 23:59",
    "outdir": str(Path.home() / "Desktop" / "DVR"),
    "convert": bool(FFMPEG),
    "delete_raw": False,
    "threads": 4,
}


def parse_dt(text):
    text = text.strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError(f"не понял дату: {text!r} (формат ДД.ММ.ГГГГ ЧЧ:ММ)")


def fmt_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}"
        n /= 1024


def fmt_eta(sec):
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}ч {m:02d}м" if h else f"{m}м {sec % 60:02d}с"


def file_size(f):
    """FileLength from the DVR is hex, in kilobytes."""
    try:
        return int(f.get("FileLength", "0"), 16) * 1024
    except ValueError:
        return 0


def target_path(outdir, f):
    begin = f["BeginTime"]                    # 2026-09-14 00:00:00
    end = f["EndTime"]
    day = begin[:10]
    name = f"{begin[11:].replace(':', '.')}-{end[11:].replace(':', '.')}"
    ext = Path(f["FileName"]).suffix or ".h264"
    return Path(outdir) / f"канал{f['_ch'] + 1:02d}" / day / f"{day}_{name}{ext}"


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.files = []              # list of dicts from the DVR (+ _ch, _iid, _path)
        self.worker = None
        self.cancel = threading.Event()
        self.convert_q = queue.Queue()
        self.channel_vars = []

        cfg = dict(DEFAULTS)
        try:
            cfg.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

        root.title("XMEye Downloader")
        root.geometry("1000x720")
        root.minsize(820, 560)

        # --- connection ----------------------------------------------------
        top = ttk.LabelFrame(root, text="Регистратор")
        top.pack(fill="x", padx=18, pady=(18, 4))
        self.v_ip = tk.StringVar(value=cfg["ip"])
        self.v_port = tk.StringVar(value=cfg["port"])
        self.v_user = tk.StringVar(value=cfg["user"])
        self.v_pass = tk.StringVar()
        for col, (label, var, width, show) in enumerate([
            ("IP", self.v_ip, 16, None), ("Порт", self.v_port, 7, None),
            ("Логин", self.v_user, 12, None), ("Пароль", self.v_pass, 14, "•"),
        ]):
            ttk.Label(top, text=label).grid(row=0, column=col * 2, padx=(18, 2), pady=6, sticky="e")
            ttk.Entry(top, textvariable=var, width=width, show=show or "").grid(row=0, column=col * 2 + 1, pady=6)
        self.b_connect = ttk.Button(top, text="Подключиться", command=self.on_connect)
        self.b_connect.grid(row=0, column=8, padx=8)
        self.chan_frame = ttk.Frame(top)
        self.chan_frame.grid(row=1, column=0, columnspan=9, sticky="w", padx=8, pady=(0, 6))
        ttk.Label(self.chan_frame, text="Каналы: подключись, чтобы увидеть").pack(side="left")

        # --- period / output -------------------------------------------------
        mid = ttk.LabelFrame(root, text="Что качать")
        mid.pack(fill="x", padx=8, pady=4)
        self.v_start = tk.StringVar(value=cfg["start"])
        self.v_end = tk.StringVar(value=cfg["end"])
        self.v_out = tk.StringVar(value=cfg["outdir"])
        self.v_convert = tk.BooleanVar(value=cfg["convert"] and bool(FFMPEG))
        self.v_delete_raw = tk.BooleanVar(value=cfg["delete_raw"])
        ttk.Label(mid, text="С").grid(row=0, column=0, padx=(8, 2), pady=6)
        ttk.Entry(mid, textvariable=self.v_start, width=17).grid(row=0, column=1)
        ttk.Label(mid, text="По").grid(row=0, column=2, padx=(10, 2))
        ttk.Entry(mid, textvariable=self.v_end, width=17).grid(row=0, column=3)
        ttk.Label(mid, text="(ДД.ММ.ГГГГ ЧЧ:ММ)", foreground="gray").grid(row=0, column=4, padx=6)
        self.b_search = ttk.Button(mid, text="Найти записи", command=self.on_search, state="disabled")
        self.b_search.grid(row=0, column=5, padx=8)

        ttk.Label(mid, text="Папка").grid(row=1, column=0, padx=(8, 2), pady=(0, 6))
        ttk.Entry(mid, textvariable=self.v_out).grid(row=1, column=1, columnspan=4, sticky="we", pady=(0, 6))
        ttk.Button(mid, text="Обзор…", command=self.on_browse).grid(row=1, column=5, padx=8, pady=(0, 6))
        mid.columnconfigure(4, weight=1)

        conv = ttk.Frame(mid)
        conv.grid(row=2, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 6))
        cb = ttk.Checkbutton(conv, text="Конвертировать в MP4 (ffmpeg, без перекодирования)", variable=self.v_convert)
        cb.pack(side="left")
        ttk.Checkbutton(conv, text="удалять исходный .h264 после конвертации",
                        variable=self.v_delete_raw).pack(side="left", padx=12)
        if not FFMPEG:
            cb.state(["disabled"])
            ttk.Label(conv, text="ffmpeg не найден", foreground="gray").pack(side="left")

        # --- file list -------------------------------------------------------
        lst = ttk.Frame(root)
        lst.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("ch", "begin", "end", "size", "status")
        self.tree = ttk.Treeview(lst, columns=cols, show="headings", selectmode="extended")
        for c, title, w, anchor in [("ch", "Канал", 60, "center"), ("begin", "Начало", 150, "w"),
                                    ("end", "Конец", 150, "w"), ("size", "Размер", 90, "e"),
                                    ("status", "Статус", 260, "w")]:
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "status"))
        sb = ttk.Scrollbar(lst, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("done", foreground="#1a7f37")
        self.tree.tag_configure("error", foreground="#c62828")
        self.tree.tag_configure("active", background="#fff4c2")
        self.tree.tag_configure("skip", foreground="gray")

        # --- progress ----------------------------------------------------------
        prog = ttk.Frame(root)
        prog.pack(fill="x", padx=8, pady=4)
        self.l_file = ttk.Label(prog, text="Сейчас качается: —")
        self.l_file.grid(row=0, column=0, sticky="w")
        self.l_total = ttk.Label(prog, text="Всего: —")
        self.l_total.grid(row=2, column=0, sticky="w")
        self.pb_total = ttk.Progressbar(prog, maximum=1000)
        self.pb_total.grid(row=3, column=0, sticky="we", pady=2)
        prog.columnconfigure(0, weight=1)

        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=8, pady=4)
        self.b_download = ttk.Button(btns, text="Скачать всё", command=self.on_download, state="disabled")
        self.b_download.pack(side="left")
        self.b_download_sel = ttk.Button(btns, text="Скачать выделенное", command=lambda: self.on_download(True),
                                         state="disabled")
        self.b_download_sel.pack(side="left", padx=6)
        self.b_stop = ttk.Button(btns, text="Стоп", command=self.on_stop, state="disabled")
        self.b_stop.pack(side="left")
        ttk.Label(btns, text="Потоков:").pack(side="left", padx=(16, 4))
        self.v_threads = tk.IntVar(value=cfg["threads"])
        ttk.Spinbox(btns, from_=1, to=32, width=4, textvariable=self.v_threads, state="readonly").pack(side="left")
        ttk.Button(btns, text="Открыть папку", command=self.on_open_dir).pack(side="right")

        # --- log -----------------------------------------------------------------
        self.log_box = tk.Text(root, height=8, state="disabled", wrap="word", font=("Consolas", 9))
        self.log_box.pack(fill="x", padx=8, pady=(4, 8))

        threading.Thread(target=self.converter, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        self.log(f"ffmpeg: {FFMPEG or 'не найден (MP4 конвертация отключена)'}")

    # ---------------------------------------------------------------- helpers

    def log(self, text):
        self.q.put(("log", text))

    def ui(self, *msg):
        self.q.put(msg)

    def save_settings(self):
        cfg = {"ip": self.v_ip.get(), "port": self.v_port.get(), "user": self.v_user.get(),
               "start": self.v_start.get(), "end": self.v_end.get(), "outdir": self.v_out.get(),
               "convert": self.v_convert.get(), "delete_raw": self.v_delete_raw.get(), "threads": self.v_threads.get()}
        try:
            SETTINGS.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def make_client(self):
        # called from worker threads: uses the snapshot taken in run_worker, never Tk variables
        return DVRClient(*self.conn)

    def set_busy(self, busy):
        st = "disabled" if busy else "normal"
        self.b_connect.configure(state=st)
        self.b_search.configure(state="disabled" if busy or not self.channel_vars else "normal")
        has_files = bool(self.files)
        self.b_download.configure(state="disabled" if busy or not has_files else "normal")
        self.b_download_sel.configure(state="disabled" if busy or not has_files else "normal")
        self.b_stop.configure(state="normal" if busy else "disabled")

    def run_worker(self, target, *args):
        try:
            port = int(self.v_port.get() or 34567)
        except ValueError:
            messagebox.showerror("Порт", "Порт должен быть числом")
            return
        self.conn = (self.v_ip.get().strip(), self.v_user.get().strip(), self.v_pass.get(), port)
        self.cancel.clear()
        self.set_busy(True)
        self.save_settings()

        def wrapper():
            try:
                target(*args)
            except Cancelled:
                self.log("Остановлено.")
            except DVRError as e:
                self.log(f"ОШИБКА: {e}")
                self.ui("error", str(e))
            except Exception as e:  # noqa: BLE001 - show anything to the user
                self.log(f"ОШИБКА: {type(e).__name__}: {e}")
                self.ui("error", f"{type(e).__name__}: {e}")
            finally:
                self.ui("idle")

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    # ---------------------------------------------------------------- UI loop

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", time.strftime("%H:%M:%S ") + msg[1] + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "channels":
                    self.show_channels(msg[1])
                elif kind == "files":
                    self.show_files(msg[1])
                elif kind == "status":
                    _, iid, text, tag = msg
                    if self.tree.exists(iid):
                        self.tree.set(iid, "status", text)
                        self.tree.item(iid, tags=(tag,) if tag else ())
                        if tag == "active":
                            self.tree.see(iid)
                elif kind == "file_progress":
                    self.l_file.configure(text=msg[1])
                elif kind == "total_progress":
                    _, label, frac = msg
                    self.l_total.configure(text=label)
                    self.pb_total["value"] = frac * 1000
                elif kind == "error":
                    messagebox.showerror("Ошибка", msg[1])
                elif kind == "info":
                    messagebox.showinfo("Готово", msg[1])
                elif kind == "idle":
                    self.set_busy(False)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def show_channels(self, count):
        for w in self.chan_frame.winfo_children():
            w.destroy()
        ttk.Label(self.chan_frame, text="Каналы:").pack(side="left")
        self.channel_vars = []
        for i in range(count):
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(self.chan_frame, text=str(i + 1), variable=v).pack(side="left", padx=2)
            self.channel_vars.append(v)

    def show_files(self, files):
        self.tree.delete(*self.tree.get_children())
        self.files = files
        for i, f in enumerate(files):
            f["_iid"] = str(i)
            status, tag = "", ""
            if f["_path"].exists() or f["_path"].with_suffix(".mp4").exists():
                status, tag = "уже скачан", "skip"
            self.tree.insert("", "end", iid=str(i), tags=(tag,) if tag else (), values=(
                f["_ch"] + 1, f["BeginTime"], f["EndTime"], fmt_size(file_size(f)), status))

    # ---------------------------------------------------------------- actions

    def on_browse(self):
        d = filedialog.askdirectory(initialdir=self.v_out.get() or str(Path.home()))
        if d:
            self.v_out.set(d)

    def on_open_dir(self):
        d = Path(self.v_out.get())
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(d)

    def on_stop(self):
        self.cancel.set()
        self.log("Останавливаю…")

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Выход", "Идёт работа. Остановить и выйти?"):
                return
            self.cancel.set()
        self.save_settings()
        self.root.destroy()

    def on_connect(self):
        self.run_worker(self.do_connect)

    def do_connect(self):
        c = self.make_client()
        self.log(f"Подключаюсь к {c.ip}:{c.port}…")
        info = c.connect()
        try:
            count = c.channel_count() or int(info.get("ChannelNum", 0) or 0) or 1
            sysinfo = c.get_info("SystemInfo", 1020) or {}
        finally:
            c.close()
        self.log(f"Подключено. Модель: {sysinfo.get('HardWare', '?')}, "
                 f"прошивка: {sysinfo.get('SoftWareVersion', '?')}, каналов: {count}")
        self.ui("channels", count)

    def on_search(self):
        try:
            start, end = parse_dt(self.v_start.get()), parse_dt(self.v_end.get())
        except ValueError as e:
            messagebox.showerror("Дата", str(e))
            return
        if len(self.v_end.get().strip()) <= 10:      # only a date given -> whole day
            end = end.replace(hour=23, minute=59, second=59)
        if end <= start:
            messagebox.showerror("Дата", "Конец периода раньше начала")
            return
        channels = [i for i, v in enumerate(self.channel_vars) if v.get()]
        if not channels:
            messagebox.showerror("Каналы", "Выбери хотя бы один канал")
            return
        self.run_worker(self.do_search, start, end, channels, self.v_out.get())

    def do_search(self, start, end, channels, outdir):
        c = self.make_client()
        c.connect()
        found = []
        try:
            for ch in channels:
                # query day by day: DVRs cap the number of results per search
                day = start
                ch_files = []
                while day < end:
                    if self.cancel.is_set():
                        raise Cancelled()
                    day_end = min(datetime.combine(day.date(), datetime.max.time()).replace(microsecond=0), end)
                    self.ui("total_progress", f"Ищу: канал {ch + 1}, {day:%d.%m.%Y}", 0)
                    files = c.list_files(day.strftime("%Y-%m-%d %H:%M:%S"),
                                         day_end.strftime("%Y-%m-%d %H:%M:%S"), ch)
                    ch_files.extend(files)
                    day = datetime.combine(day.date() + timedelta(days=1), datetime.min.time())
                # a recording crossing midnight shows up in two day queries
                uniq = {f["FileName"]: f for f in ch_files}
                for f in uniq.values():
                    f["_ch"] = ch
                    f["_path"] = target_path(outdir, f)
                size = sum(file_size(f) for f in uniq.values())
                self.log(f"Канал {ch + 1}: {len(uniq)} файлов, {fmt_size(size)}")
                found.extend(uniq.values())
        finally:
            c.close()
        found.sort(key=lambda f: (f["_ch"], f["BeginTime"]))
        total = sum(file_size(f) for f in found)
        self.log(f"Итого: {len(found)} файлов, {fmt_size(total)}")
        self.ui("total_progress", f"Найдено: {len(found)} файлов, {fmt_size(total)}", 0)
        self.ui("files", found)

    def on_download(self, selected_only=False):
        if selected_only:
            sel = set(self.tree.selection())
            files = [f for f in self.files if f["_iid"] in sel]
            if not files:
                messagebox.showinfo("Выделение", "Выдели файлы в списке (Ctrl/Shift + клик)")
                return
        else:
            files = list(self.files)
        outdir = self.v_out.get()
        for f in files:                       # folder may have changed since the search
            f["_path"] = target_path(outdir, f)
        self.run_worker(self.do_download, files, self.v_convert.get(), self.v_delete_raw.get(),
                        self.v_threads.get())

    def do_download(self, files, convert, delete_raw, threads):
        todo = []
        for f in files:
            mp4 = f["_path"].with_suffix(".mp4")
            if convert and f["_path"].exists() and not mp4.exists():
                self.ui("status", f["_iid"], "уже скачан, ждёт MP4", "done")
                self.convert_q.put((f["_iid"], f["_path"], delete_raw, f["_path"].stat().st_size))
            elif f["_path"].exists() or mp4.exists():
                self.ui("status", f["_iid"], "уже скачан", "skip")
            else:
                todo.append(f)
        total_bytes = sum(file_size(f) for f in todo) or 1
        threads = max(1, min(threads, len(todo) or 1))
        self.log(f"К скачиванию: {len(todo)} файлов, {fmt_size(total_bytes)}, потоков: {threads}"
                 + (f" (пропущено уже скачанных: {len(files) - len(todo)})" if len(files) != len(todo) else ""))
        for f in todo:
            self.ui("status", f["_iid"], "в очереди", "")

        jobs = queue.Queue()
        for f in todo:
            jobs.put(f)
        lock = threading.Lock()
        st = {"done_exp": 0, "done_real": 0, "ok": 0, "failed": 0, "alive": threads, "active": {}}
        started = time.monotonic()

        def worker(wid):
            c = None
            completed = 0
            try:
                while True:
                    with lock:
                        if self.cancel.is_set():
                            st["alive"] -= 1
                            return
                        try:
                            f = jobs.get_nowait()
                        except queue.Empty:
                            st["alive"] -= 1
                            return
                    iid, path = f["_iid"], f["_path"]
                    expected = file_size(f) or 1
                    part = path.with_name(path.name + ".part")
                    path.parent.mkdir(parents=True, exist_ok=True)

                    for attempt in range(1, RETRIES + 1):
                        t0 = time.monotonic()

                        def progress(got):
                            with lock:
                                st["active"][iid] = got
                            speed = got / max(time.monotonic() - t0, 0.001)
                            self.ui("status", iid, f"[поток {wid}] {min(got / expected, 1):.0%}  "
                                                   f"{fmt_size(got)} / {fmt_size(expected)}  {fmt_size(speed)}/с",
                                    "active")

                        try:
                            self.ui("status", iid, f"[поток {wid}] подключаюсь…", "active")
                            if c is None or c.sock is None:
                                c = self.make_client()
                                c.connect()
                            else:
                                c.keepalive_if_needed()
                            with open(part, "wb") as out:
                                got = c.download(f, out, progress, self.cancel.is_set, file_size(f))
                            if got == 0:
                                raise DVRError("регистратор отдал пустой файл")
                            os.replace(part, path)
                            completed += 1
                            with lock:
                                st["active"].pop(iid, None)
                                st["done_exp"] += expected
                                st["done_real"] += got
                                st["ok"] += 1
                            if convert:
                                self.ui("status", iid, f"готово ({fmt_size(got)}), ждёт MP4", "done")
                                self.convert_q.put((iid, path, delete_raw, got))
                            else:
                                self.ui("status", iid, f"готово ({fmt_size(got)})", "done")
                            break
                        except Exception as e:  # noqa: BLE001 - retry, then move on to the next file
                            part.unlink(missing_ok=True)
                            if c:
                                c.close()
                            c = None
                            with lock:
                                st["active"].pop(iid, None)
                            if self.cancel.is_set() or isinstance(e, Cancelled):
                                self.ui("status", iid, "остановлено", "error")
                                with lock:
                                    st["alive"] -= 1
                                return
                            logging.getLogger("xmeye").exception("worker %d: %s", wid, f["FileName"])
                            if not isinstance(e, (DVRError, OSError)):
                                e = f"{type(e).__name__}: {e}"
                            if attempt < RETRIES:
                                self.log(f"Поток {wid}: сбой ({e}), повтор {attempt + 1}/{RETRIES} через 3 с…")
                                self.ui("status", iid, f"[поток {wid}] повтор {attempt + 1}/{RETRIES}…", "active")
                                time.sleep(3)
                                continue
                            with lock:
                                # a worker that never managed a single file most likely hit the DVR's
                                # limit on sessions/playbacks: give the file back and drop this worker
                                if completed == 0 and st["alive"] > 1:
                                    jobs.put(f)
                                    st["alive"] -= 1
                                    giving_up = True
                                else:
                                    st["failed"] += 1
                                    st["done_exp"] += expected
                                    giving_up = False
                            if giving_up:
                                self.log(f"Поток {wid} отключён: регистратор не тянет столько соединений ({e}). "
                                         f"Остальные потоки продолжают.")
                                self.ui("status", iid, "в очереди", "")
                                return
                            self.log(f"Не скачался {f['FileName']}: {e}")
                            self.ui("status", iid, f"ошибка: {e}", "error")
            finally:
                if c:
                    c.close()

        workers = []
        for wid in range(1, threads + 1):
            t = threading.Thread(target=worker, args=(wid,), daemon=True)
            t.start()
            workers.append(t)
            time.sleep(0.5)  # don't hit the DVR with all logins at once

        samples = []  # (time, real bytes) for a rolling speed estimate
        while any(t.is_alive() for t in workers):
            time.sleep(0.5)
            with lock:
                active = dict(st["active"])
                real = st["done_real"] + sum(active.values())
                prog = st["done_exp"] + sum(active.values())
                alive = st["alive"]
                done_n = st["ok"] + st["failed"]
            now = time.monotonic()
            samples.append((now, real))
            while len(samples) > 2 and now - samples[0][0] > 20:
                samples.pop(0)
            speed = (real - samples[0][1]) / max(now - samples[0][0], 0.001) if len(samples) > 1 else 0
            eta = (total_bytes - prog) / speed if speed > 0 else 0
            self.ui("file_progress", f"Сейчас качается: {len(active)} файл(ов), потоков: {alive}, "
                                     f"скорость {fmt_size(speed)}/с")
            self.ui("total_progress", f"Всего: {fmt_size(prog)} / {fmt_size(total_bytes)}   "
                                      f"файлов {done_n} из {len(todo)}   "
                                      f"осталось ~{fmt_eta(eta) if speed > 0 else '?'}",
                    min(prog / total_bytes, 1))

        if self.cancel.is_set():
            for f in todo:
                if not f["_path"].exists():
                    self.ui("status", f["_iid"], "", "")
            raise Cancelled()
        msg = f"Скачано: {st['ok']}, ошибок: {st['failed']}, за {fmt_eta(time.monotonic() - started)}"
        self.log(msg)
        self.ui("file_progress", "Сейчас качается: —")
        self.ui("total_progress", msg, 1)
        if st["failed"]:
            msg += "\nФайлы с ошибками можно докачать: нажми «Скачать всё» ещё раз - скачанные пропустятся."
        if convert and st["ok"]:
            msg += "\nКонвертация в MP4 идёт в фоне - смотри статус в списке."
        self.ui("info", msg)

    # ---------------------------------------------------------------- ffmpeg

    def converter(self):
        while True:
            iid, src, delete_raw, size = self.convert_q.get()
            dst = src.with_suffix(".mp4")
            tmp = dst.with_name(dst.stem + ".tmp.mp4")
            self.ui("status", iid, f"готово ({fmt_size(size)}), конвертирую в MP4…", "done")
            try:
                info = to_mp4(src, tmp, FFMPEG)
                os.replace(tmp, dst)
                logging.getLogger("xmeye").debug("mp4 %s: %s", dst, info)
                if delete_raw:
                    src.unlink(missing_ok=True)
                self.ui("status", iid, f"готово ({fmt_size(size)}) + MP4", "done")
            except Exception as e:  # noqa: BLE001
                tmp.unlink(missing_ok=True)
                self.log(f"MP4 не получился для {src.name}: {e} (исходник .h264 оставлен)")
                self.ui("status", iid, f"готово ({fmt_size(size)}), MP4 не вышел - открой .h264 в VLC", "done")


def main():
    logging.basicConfig(filename=DEBUG_LOG, filemode="w", encoding="utf-8", level=logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(message)s")
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
