#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc_monitor.py —— Claude Code 多会话监控(菜单栏 App)

混合架构的"消费端":
  - 主信源:cc_hook.py 写入的 ~/.cc-monitor/state.db(确定性事件,准)。
  - 兜底:对没装 hook 的会话,扫描 ~/.claude/projects 的 JSONL 日志启发式推断。
  - 唯一通知方:状态从 RUNNING → WAITING/NEEDS_INPUT 的**边沿**才弹一次,
    去重状态持久化在 DB,重启不丢、抖动不重复弹。

依赖:  pip3 install rumps   (仅菜单栏 UI;核心逻辑零依赖)
运行:  python3 cc_monitor.py
"""

import os
import json
import time
import glob
import sqlite3
import subprocess
from datetime import datetime

DB_DIR  = os.path.expanduser("~/.cc-monitor")
DB_PATH = os.path.join(DB_DIR, "state.db")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

REFRESH_SEC   = 2       # UI/兜底刷新间隔
IDLE_HIDE_SEC = 1800    # 超过该秒无活动的会话不显示
FALLBACK_RUNNING_GAP = 8    # 日志兜底:静默 < 此值视为运行中
FALLBACK_WAIT_GAP    = 30   # 日志兜底:静默 > 此值且最后是助手文本 → 等待

try:
    import rumps
    HAS_RUMPS = True
except ImportError:
    HAS_RUMPS = False


# ========================= 数据库 =========================

def connect():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY, cwd TEXT, project TEXT, status TEXT,
        last_event TEXT, last_event_ts REAL, turn_started_ts REAL,
        notify_pending INTEGER DEFAULT 0, notify_kind TEXT,
        transcript_path TEXT, source TEXT DEFAULT 'hook'
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, event TEXT, ts REAL
    );
    """)
    conn.commit()


# ========================= 日志兜底解析 =========================
# 仅用于"DB 里没有、但日志在动"的会话(没装 hook 的旧会话)。
# 修复点对照旧版:
#   1. 超大尾行:逐行 json.loads,坏行跳过,不会因一条巨型消息全盘失败。
#   2. 增量解析:记住每个文件 offset,只读新增部分,告别每秒重读 200KB。

_file_offsets = {}   # path -> (last_size, last_mtime, cached_tail_obj)

def parse_last_json_obj(path):
    """增量读取文件尾部,返回最后一条可解析的 JSON 对象。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    size, mtime = st.st_size, st.st_mtime
    cached = _file_offsets.get(path)
    if cached and cached[0] == size and cached[1] == mtime:
        return cached[2]   # 没变化,直接用缓存

    # 文件可能被 rotate/truncate(新 size < 旧 size)→ 从头读尾块
    read_from = 0
    if size > 200_000:
        read_from = size - 200_000
    last_obj = None
    try:
        with open(path, "rb") as fp:
            fp.seek(read_from)
            if read_from:
                fp.readline()  # 丢弃可能被切断的半行
            for line in fp:
                s = line.strip()
                if not s:
                    continue
                try:
                    last_obj = json.loads(s)
                except Exception:
                    continue   # 坏行/半行直接跳过,不影响整体
    except Exception:
        return None
    _file_offsets[path] = (size, mtime, last_obj)
    return last_obj


def infer_status_from_log(path):
    """从最后一条消息启发式推断状态(兜底用)。"""
    obj = parse_last_json_obj(path)
    if not obj:
        return None
    role = obj.get("role") or obj.get("type")
    msg = obj.get("message") or obj
    blocks = msg.get("content") if isinstance(msg, dict) else None
    last_kind = None
    if isinstance(blocks, list) and blocks:
        last_kind = blocks[-1].get("type")
    elif isinstance(blocks, str):
        last_kind = "text"

    try:
        idle = time.time() - os.path.getmtime(path)
    except OSError:
        idle = 9999

    # 助手在用工具/思考 → 运行中
    if role == "assistant" and last_kind in ("tool_use", "thinking"):
        return ("RUNNING", idle)
    # 用户的 tool_result → 助手马上要接着跑
    if last_kind == "tool_result":
        return ("RUNNING", idle)
    # 助手纯文本:近期=可能还在流式;久了=确实停下等你
    if role == "assistant" and last_kind in ("text", None):
        if idle < FALLBACK_RUNNING_GAP:
            return ("RUNNING", idle)
        if idle > FALLBACK_WAIT_GAP:
            return ("WAITING", idle)
        return ("RUNNING", idle)   # 灰色地带保守判为运行,避免误报
    return ("RUNNING", idle)


def merge_log_fallback(conn):
    """把 hook 没覆盖到的活跃会话,用日志补进 DB(source='log')。"""
    files = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    now = time.time()
    known = {r["session_id"] for r in conn.execute("SELECT session_id FROM sessions")}
    hooked = {r["session_id"] for r in
              conn.execute("SELECT session_id FROM sessions WHERE source='hook'")}
    for f in files:
        try:
            if now - os.path.getmtime(f) > IDLE_HIDE_SEC:
                continue
        except OSError:
            continue
        sid = os.path.splitext(os.path.basename(f))[0]
        if sid in hooked:
            continue   # 已有确定性 hook 数据,不用日志覆盖
        res = infer_status_from_log(f)
        if not res:
            continue
        status, idle = res
        project = os.path.basename(os.path.dirname(f))
        notify_pending = 1 if status == "WAITING" else 0
        conn.execute("""
            INSERT INTO sessions(session_id,cwd,project,status,last_event,
                last_event_ts,notify_pending,notify_kind,transcript_path,source)
            VALUES(?,?,?,?,?,?,?,?,?, 'log')
            ON CONFLICT(session_id) DO UPDATE SET
                project=excluded.project, status=excluded.status,
                last_event_ts=excluded.last_event_ts,
                notify_pending=MAX(sessions.notify_pending, excluded.notify_pending),
                notify_kind=CASE WHEN excluded.notify_pending=1 THEN 'DONE'
                                 ELSE sessions.notify_kind END,
                source='log'
            WHERE sessions.source!='hook'
        """, (sid, "", project, status, "log:"+status, now - idle,
              notify_pending, "DONE" if notify_pending else None, f))
    conn.commit()


# ========================= 通知(唯一出口) =========================

def macos_notify(title, subtitle, text):
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{text}" with title "{title}" subtitle "{subtitle}" sound name "Glass"'
        ], check=False)
    except Exception:
        pass


def drain_notifications(conn):
    """把所有 notify_pending=1 的会话弹一次,然后置 0(边沿触发,天然去重)。"""
    rows = conn.execute(
        "SELECT session_id,project,notify_kind FROM sessions WHERE notify_pending=1"
    ).fetchall()
    for r in rows:
        proj = r["project"] or r["session_id"][:8]
        if r["notify_kind"] == "NEEDS_INPUT":
            macos_notify("Claude Code 需要你", proj, "等待授权 / 输入")
        else:
            macos_notify("Claude Code 已完成", proj, "这一轮答完了 ✅")
    if rows:
        conn.execute("UPDATE sessions SET notify_pending=0 WHERE notify_pending=1")
        conn.commit()


# ========================= 汇总 =========================

def summarize(conn):
    now = time.time()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE last_event_ts > ? ORDER BY last_event_ts DESC",
        (now - IDLE_HIDE_SEC,)
    ).fetchall()
    counts = {"RUNNING": 0, "WAITING": 0, "NEEDS_INPUT": 0}
    items = []
    for r in rows:
        st = r["status"]
        counts[st] = counts.get(st, 0) + 1
        idle = int(now - r["last_event_ts"])
        icon = {"RUNNING": "🟢", "WAITING": "🟡", "NEEDS_INPUT": "🔴"}.get(st, "⚪")
        tag = "hook" if r["source"] == "hook" else "log"
        items.append(f'{icon} {r["project"]}  [{st} · {idle}s · {tag}]')
    return counts, items


# ========================= 菜单栏 App =========================
# 注意:类继承 rumps.App,必须放在 HAS_RUMPS 守卫内 —— 否则在没装 rumps 的
# 环境里,仅仅 import 本模块(如单元测试)就会因 class 定义求值 rumps.App 而崩。

def build_app():
    # 菜单栏模板图标:打包后在 .app 的 Resources 里,源码运行时在脚本同目录
    icon_path = None
    for cand in (
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "menubar_color.png"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "Resources", "menubar_color.png"),
    ):
        if os.path.exists(cand):
            icon_path = cand
            break

    class CCMonitor(rumps.App):
        def __init__(self):
            super().__init__("CC", icon=icon_path, template=False,
                             quit_button=None)   # 关掉自动退出键,改为手动维护
            self.conn = connect()
            ensure_schema(self.conn)
            self.timer = rumps.Timer(self.tick, REFRESH_SEC)
            self.timer.start()

        def cleanup_quit(self, _):
            """关闭数据库连接后再退出。"""
            try:
                self.conn.close()
            except Exception:
                pass
            rumps.quit_application(None)

        def tick(self, _):
            try:
                merge_log_fallback(self.conn)
                drain_notifications(self.conn)
                counts, items = summarize(self.conn)
            except Exception:
                self.title = " ⚠️"
                return
            r, w, n = counts["RUNNING"], counts["WAITING"], counts["NEEDS_INPUT"]
            # 有图标时只显示数字,无图标时回退到 "CC" 前缀
            prefix = "" if icon_path else "CC "
            self.title = f"{prefix}🟢{r} 🟡{w}" + (f" 🔴{n}" if n else "")
            menu = [f"运行中 {r} · 待处理 {w} · 需介入 {n}", None]
            menu += items if items else ["(暂无活跃会话)"]
            # 每次重建都手动补回「退出」,否则 clear() 会把它清掉
            menu += [None, rumps.MenuItem("退出 CC Monitor",
                                          callback=self.cleanup_quit)]
            self.menu.clear()
            self.menu = menu

    return CCMonitor()


# ========================= CLI 兜底(无 rumps 时) =========================

def run_cli():
    conn = connect(); ensure_schema(conn)
    print("无 rumps,降级为终端模式。Ctrl+C 退出。")
    try:
        while True:
            merge_log_fallback(conn)
            drain_notifications(conn)
            counts, items = summarize(conn)
            os.system("clear")
            print(f"[{datetime.now():%H:%M:%S}] "
                  f"运行中 {counts['RUNNING']} · 待处理 {counts['WAITING']} "
                  f"· 需介入 {counts['NEEDS_INPUT']}\n")
            for it in items:
                print(" ", it)
            time.sleep(REFRESH_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    if HAS_RUMPS:
        build_app().run()
    else:
        run_cli()
