#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install_hooks.py —— 把 cc_hook.py 注册进 ~/.claude/settings.json

皮实设计(两道保险,确保永远不会因为这个 hook 卡住 Claude Code):
  1. 稳定位置:把 cc_hook.py 复制到固定的 ~/.cc-monitor/cc_hook.py 再注册。
     这样项目源码以后随便删/移/改名,CC 调用的都是这个固定副本,路径永不失效。
  2. 命令兜底:注册的命令以 `|| true` 结尾。即便固定副本哪天也被删了,
     命令整体仍返回 0,CC 的 UserPromptSubmit 也不会被拦住、发不出消息。
     (监控属于非关键 hook —— 宁可漏报一次,也绝不阻断 CC。)

幂等:重复运行不会重复添加(只要 command 里含 "cc_hook.py" 即视为已存在),
      并且每次都会用源码里最新的 cc_hook.py 覆盖固定副本。
"""
import os, json, sys, shutil

SETTINGS = os.path.expanduser("~/.claude/settings.json")

# 源码里的 cc_hook.py(随项目走,可能被移动/删除)
SRC_HOOK = os.path.abspath(os.path.join(os.path.dirname(__file__), "cc_hook.py"))
# 固定副本位置(不随项目走,CC 真正调用的就是它)
STABLE_DIR  = os.path.expanduser("~/.cc-monitor")
STABLE_HOOK = os.path.join(STABLE_DIR, "cc_hook.py")

# 命令带 `|| true` 兜底:文件即便丢了也不会让 CC 报错退出
CMD = f'python3 "{STABLE_HOOK}" || true'

# 这几个事件足够覆盖:开始/结束/答完/需介入/心跳
EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit",
          "Stop", "StopFailure", "Notification", "PostToolUse"]


def load():
    if os.path.exists(SETTINGS):
        try:
            with open(SETTINGS) as f:
                return json.load(f)
        except Exception:
            print("⚠️  settings.json 解析失败,请手动检查"); sys.exit(1)
    return {}


def stage_hook():
    """把源码里的 cc_hook.py 复制到固定位置,作为 CC 真正调用的副本。"""
    if not os.path.exists(SRC_HOOK):
        print(f"⚠️  找不到源 hook 脚本: {SRC_HOOK}"); sys.exit(1)
    os.makedirs(STABLE_DIR, exist_ok=True)
    shutil.copyfile(SRC_HOOK, STABLE_HOOK)
    print(f"✅ 已复制 hook 到固定位置: {STABLE_HOOK}")


def main():
    stage_hook()
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    cfg = load()
    hooks = cfg.setdefault("hooks", {})
    added = 0
    for ev in EVENTS:
        groups = hooks.setdefault(ev, [])
        # 去重:该事件下已存在指向 cc_hook.py 的 command 就跳过
        exists = any(
            "cc_hook.py" in h.get("command", "")
            for g in groups for h in g.get("hooks", [])
        )
        if exists:
            continue
        groups.append({"hooks": [{"type": "command", "command": CMD}]})
        added += 1
    with open(SETTINGS, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print(f"✅ 已注册 {added} 个事件 hook → {SETTINGS}")
    print(f"   实际命令: {CMD}")
    print("   重启 Claude Code 会话即可生效。")


if __name__ == "__main__":
    main()
