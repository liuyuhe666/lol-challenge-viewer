import csv
import json
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CHALLENGE_ID = "101301"
CHALLENGE_NAME = "全随机全英雄"
DD_VERSION_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DD_CHAMPION_URL = (
    "https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_CN/champion.json"
)

try:
    import psutil
except ImportError:
    psutil = None

try:
    from io import BytesIO  # noqa

    from PIL import Image, ImageTk  # noqa

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


def get_lcu_credentials():
    """从运行中的 LOL 客户端进程命令行读取端口和 token。"""
    if psutil is None:
        raise RuntimeError("缺少 psutil。请先运行：pip install psutil")

    candidates = []
    for proc in psutil.process_iter(["name", "cmdline", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in ("leagueclientux.exe", "leagueclient.exe"):
                continue
            cmd = proc.info.get("cmdline") or []
            joined = " ".join(cmd)
            port = re.search(r"--app-port=(\d+)", joined)
            token = re.search(r"--remoting-auth-token=([^\s\"]+)", joined)
            if port and token:
                candidates.append((port.group(1), token.group(1)))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if candidates:
        return candidates[0]

    raise RuntimeError(
        "没有找到 LCU 端口和 Token。\n\n"
        "请先启动国服 LOL 客户端并登录，保持客户端主界面打开，"
        "再点击「刷新」。\n"
        "如果仍失败，尝试以管理员身份运行本程序。"
    )


def request_lcu(path):
    port, token = get_lcu_credentials()
    url = f"https://127.0.0.1:{port}{path}"
    response = requests.get(url, auth=("riot", token), verify=False, timeout=12)
    if not response.ok:
        raise RuntimeError(
            f"LCU 请求失败：HTTP {response.status_code}\n{response.text[:1000]}"
        )
    return response.json()


def find_challenge_object(payload):
    """兼容按 challengeId 为键的对象，以及直接返回挑战对象的情况。"""
    if isinstance(payload, dict):
        if CHALLENGE_ID in payload and isinstance(payload[CHALLENGE_ID], dict):
            return payload[CHALLENGE_ID]
        if str(payload.get("id")) == CHALLENGE_ID:
            return payload
        # 有些响应会把挑战列表放在某个字段中
        for value in payload.values():
            if isinstance(value, dict):
                found = find_challenge_object(value)
                if found:
                    return found
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        found = find_challenge_object(item)
                        if found:
                            return found
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                found = find_challenge_object(item)
                if found:
                    return found
    return None


def load_champions():
    versions = requests.get(DD_VERSION_URL, timeout=15).json()
    version = versions[0]
    url = DD_CHAMPION_URL.format(version=version)
    data = requests.get(url, timeout=20).json()["data"]
    # key 是 Riot champion ID，例如 1、4、5；name 是简体中文名称
    champs = {}
    for champ in data.values():
        champs[int(champ["key"])] = {
            "name": champ["name"],
            "image": champ.get("image", {}).get("full", ""),
        }
    return version, champs


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("LOL 全随机全英雄成就查看器")
        self.root.geometry("820x650")
        self.root.minsize(700, 500)

        self.all_rows = []
        self.images = []
        self.status = tk.StringVar(value="等待连接 LOL 客户端")
        self.search = tk.StringVar()
        self.filter_mode = tk.StringVar(value="全部")
        self.version_text = tk.StringVar(value="Data Dragon：未加载")
        self.progress_text = tk.StringVar(value="进度：--")

        self.build_ui()
        self.root.after(400, self.refresh)

    def build_ui(self):
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill="x")

        ttk.Label(
            top, text=CHALLENGE_NAME, font=("Microsoft YaHei UI", 17, "bold")
        ).pack(anchor="w")
        ttk.Label(top, text="挑战 ID：101301  |  按 completedIds 判断已完成英雄").pack(
            anchor="w", pady=(3, 8)
        )

        stats = ttk.Frame(top)
        stats.pack(fill="x", pady=4)
        self.total_label = ttk.Label(stats, text="英雄总数：--")
        self.total_label.pack(side="left", padx=(0, 18))
        self.done_label = ttk.Label(stats, text="已完成：--")
        self.done_label.pack(side="left", padx=(0, 18))
        self.todo_label = ttk.Label(stats, text="未完成：--")
        self.todo_label.pack(side="left")

        self.progress = ttk.Progressbar(top, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(6, 2))
        ttk.Label(top, textvariable=self.progress_text).pack(anchor="e")

        controls = ttk.Frame(self.root, padding=(12, 4))
        controls.pack(fill="x")
        ttk.Button(controls, text="刷新 LCU 数据", command=self.refresh).pack(
            side="left"
        )
        ttk.Button(controls, text="导出 CSV", command=self.export_csv).pack(
            side="left", padx=8
        )
        ttk.Label(controls, text="筛选：").pack(side="left", padx=(16, 3))
        filter_box = ttk.Combobox(
            controls,
            textvariable=self.filter_mode,
            values=["全部", "已完成", "未完成"],
            state="readonly",
            width=10,
        )
        filter_box.pack(side="left")
        filter_box.bind("<<ComboboxSelected>>", lambda e: self.render_rows())
        ttk.Label(controls, text="搜索：").pack(side="left", padx=(12, 3))
        search_entry = ttk.Entry(controls, textvariable=self.search, width=20)
        search_entry.pack(side="left")
        search_entry.bind("<KeyRelease>", lambda e: self.render_rows())

        table_frame = ttk.Frame(self.root, padding=(12, 4))
        table_frame.pack(fill="both", expand=True)
        columns = ("status", "id", "name")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        self.tree.heading("status", text="状态")
        self.tree.heading("id", text="英雄 ID")
        self.tree.heading("name", text="英雄名称")
        self.tree.column("status", width=100, anchor="center")
        self.tree.column("id", width=100, anchor="center")
        self.tree.column("name", width=240, anchor="w")
        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        bottom = ttk.Frame(self.root, padding=12)
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status).pack(anchor="w")
        ttk.Label(bottom, textvariable=self.version_text).pack(anchor="w", pady=(3, 0))

    def refresh(self):
        self.status.set("正在连接 LOL 客户端并读取挑战数据……")
        self.root.update_idletasks()
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            payload = request_lcu("/lol-challenges/v1/challenges/local-player")
            challenge = find_challenge_object(payload)
            if not challenge:
                sample = json.dumps(payload, ensure_ascii=False)[:1200]
                raise RuntimeError(
                    f"接口已连接，但响应中找不到挑战 {CHALLENGE_ID}。\n"
                    f"响应片段：\n{sample}"
                )

            completed_ids = set()
            for value in challenge.get("completedIds", []):
                try:
                    completed_ids.add(int(value))
                except (TypeError, ValueError):
                    pass

            version, champions = load_champions()
            rows = []
            missing_ids = []
            for champion_id, info in champions.items():
                done = champion_id in completed_ids
                rows.append(
                    {
                        "status": "已完成" if done else "未完成",
                        "id": champion_id,
                        "name": info["name"],
                    }
                )

            # 记录挑战数据中存在、但当前 Data Dragon 列表没有的 ID
            missing_ids = sorted(completed_ids - set(champions.keys()))
            rows.sort(key=lambda x: x["name"])

            self.root.after(
                0,
                lambda: self.update_ui(
                    rows, version, challenge, completed_ids, missing_ids
                ),
            )
        except Exception as e:  # noqa
            error_msg = str(e)
            self.root.after(0, lambda: self.status.set(f"读取失败：{error_msg}"))
            self.root.after(0, lambda: messagebox.showerror("读取失败", error_msg))

    def update_ui(self, rows, version, challenge, completed_ids, missing_ids):
        self.all_rows = rows
        total = len(rows)
        done = sum(1 for row in rows if row["status"] == "已完成")
        todo = total - done
        rate = (done / total * 100) if total else 0

        self.total_label.config(text=f"英雄总数：{total}")
        self.done_label.config(text=f"已完成：{done}")
        self.todo_label.config(text=f"未完成：{todo}")
        self.progress["value"] = rate
        self.progress_text.set(
            f"完成率：{rate:.1f}%  |  挑战等级：{challenge.get('currentLevel', '未知')}  |  当前值：{challenge.get('currentValue', '--')}"
        )
        self.version_text.set(f"Data Dragon：{version}")
        extra = (
            f"；有 {len(missing_ids)} 个 completedIds 不在当前英雄表中：{missing_ids}"
            if missing_ids
            else ""
        )
        self.status.set(f"读取成功。挑战已完成英雄 ID 数：{len(completed_ids)}{extra}")
        self.render_rows()

    def render_rows(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        keyword = self.search.get().strip().lower()
        mode = self.filter_mode.get()

        for row in self.all_rows:
            if mode != "全部" and row["status"] != mode:
                continue
            if (
                keyword
                and keyword not in row["name"].lower()
                and keyword not in str(row["id"])
            ):
                continue
            self.tree.insert(
                "",
                "end",
                values=(row["status"], row["id"], row["name"]),
                tags=(row["status"],),
            )

        self.tree.tag_configure("已完成", foreground="#16803c")
        self.tree.tag_configure("未完成", foreground="#b45309")

    def export_csv(self):
        if not self.all_rows:
            messagebox.showinfo("提示", "请先成功读取数据。")
            return
        path = filedialog.asksaveasfilename(
            title="保存英雄成就清单",
            defaultextension=".csv",
            initialfile="LOL全随机全英雄成就清单.csv",
            filetypes=[("CSV 文件", "*.csv")],
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["状态", "英雄ID", "英雄名称"])
            for row in self.all_rows:
                writer.writerow([row["status"], row["id"], row["name"]])
        messagebox.showinfo("导出成功", f"已保存到：\n{path}")


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
