#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc_hook.py —— Claude Code Hook 上报端(确定性事件源)

由 Claude Code 的 hooks 调用,从 stdin 读取事件 JSON,把"哪个会话、在干嘛"
确定性地写入共享 SQLite。它是一个**短命进程**:做完就退出,绝不阻塞 CC。

设计原则:
  - 零三方依赖(只用标准库),保证在任何 CC 环境都能跑起来。
  - 单一职责:只写库,不弹通知(通知由常驻的 cc_monitor.py 统一负责、统一去重)。
  - 并发安全:多个 CC 会话会同时调它 → WAL + busy_timeout + 重试。
  - 永不让 CC 卡住:任何异常都吞掉并 exit 0。

注册方式(写入 ~/.claude/settings.json,见文末 SETTINGS_SNIPPET):
  对 Stop / Notification / SessionStart / SessionEnd / UserPromptSubmit /
  PostToolUse 这几个事件各挂一条:
      python3 /绝对路径/cc_hook.py
"""

import sys
import os
import json
import time
import sqlite3

DB_DIR = os.path.expanduser("~/.cc-monitor")
DB_PATH = os.path.join(DB_DIR, "state.db")

# CC 事件 → 会话状态 的确定性映射
#   RUNNING     正在干活
#   WAITING     一轮答完,等你下一句(Stop)→ 触发"完成"通知
#   NEEDS_INPUT 需要你授权/补充输入(Notification)→ 触发"需介入"通知
#   ENDED       会话结束
EVENT_TO_STATUS = {
    "SessionStart":     "WAITING",     # 起会话,等首条 prompt
    "UserPromptSubmit": "RUNNING",     # 你发了一句,它开始干
    "PreToolUse":       "RUNNING",     # 心跳
    "PostToolUse":      "RUNNING",     # 心跳
    "Notification":     "NEEDS_INPUT", # 要授权 / 长时间等待
    "Stop":             "WAITING",     # ★ 一轮结束 → 关键通知信号
    "StopFailure":      "WAITING",     # 出错也算停了,需要你看一眼
    "SessionEnd":       "ENDED",
}

# 哪些状态是"需要弹通知的边沿"(由 App 读 notify_pending 决定是否真弹)
NOTIFY_STATES = {"WAITING", "NEEDS_INPUT"}


def connect():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        session_id      TEXT PRIMARY KEY,
        cwd             TEXT,
        project         TEXT,
        status          TEXT,
        last_event      TEXT,
        last_event_ts   REAL,
        turn_started_ts REAL,
        notify_pending  INTEGER DEFAULT 0,  -- 1=有待弹通知,App 弹完置 0
        notify_kind     TEXT,               -- DONE / NEEDS_INPUT
        transcript_path TEXT,
        source          TEXT DEFAULT 'hook'
    );
    CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT,
        event       TEXT,
        ts          REAL
    );
    """)


def upsert(conn, payload):
    sid   = payload.get("session_id") or "unknown"
    event = payload.get("hook_event_name") or "unknown"
    cwd   = payload.get("cwd") or ""
    tpath = payload.get("transcript_path") or ""
    project = os.path.basename(cwd.rstrip("/")) if cwd else "(unknown)"
    now = time.time()

    status = EVENT_TO_STATUS.get(event, "RUNNING")

    # 通知种类:Stop 类 → DONE;Notification → NEEDS_INPUT
    notify_kind = None
    notify_pending = 0
    if status == "WAITING":
        notify_kind, notify_pending = "DONE", 1
    elif status == "NEEDS_INPUT":
        notify_kind, notify_pending = "NEEDS_INPUT", 1

    # turn_started_ts:开始新一轮时记一次,用于算"这轮跑了多久"
    turn_started = now if event == "UserPromptSubmit" else None

    row = conn.execute(
        "SELECT turn_started_ts FROM sessions WHERE session_id=?", (sid,)
    ).fetchone()
    if row and turn_started is None:
        turn_started = row[0]

    conn.execute("""
        INSERT INTO sessions
            (session_id, cwd, project, status, last_event, last_event_ts,
             turn_started_ts, notify_pending, notify_kind, transcript_path, source)
        VALUES (?,?,?,?,?,?,?,?,?,?, 'hook')
        ON CONFLICT(session_id) DO UPDATE SET
            cwd=excluded.cwd,
            project=excluded.project,
            status=excluded.status,
            last_event=excluded.last_event,
            last_event_ts=excluded.last_event_ts,
            turn_started_ts=excluded.turn_started_ts,
            -- 通知是"取或":新事件要求弹,就置 1;不主动清(清由 App 负责)
            notify_pending=MAX(sessions.notify_pending, excluded.notify_pending),
            notify_kind=CASE WHEN excluded.notify_pending=1
                             THEN excluded.notify_kind ELSE sessions.notify_kind END,
            transcript_path=excluded.transcript_path,
            source='hook'
    """, (sid, cwd, project, status, event, now,
          turn_started, notify_pending, notify_kind, tpath))

    conn.execute("INSERT INTO events(session_id,event,ts) VALUES(?,?,?)",
                 (sid, event, now))
    conn.commit()


def main():
    raw = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    for attempt in range(3):
        conn = None
        try:
            conn = connect()
            ensure_schema(conn)
            upsert(conn, payload)
            break
        except sqlite3.OperationalError:
            time.sleep(0.2 * (attempt + 1))  # 锁竞争,退避重试
        except Exception:
            break  # 任何其它异常都不能影响 CC
        finally:
            if conn:
                conn.close()

    sys.exit(0)  # 永远成功退出,绝不阻断 Claude Code


if __name__ == "__main__":
    main()
