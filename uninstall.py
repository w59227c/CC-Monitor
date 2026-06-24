#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
uninstall.py —— 一键卸载 CC Monitor

做三件事:
  1. 从 ~/.claude/settings.json 移除所有指向 cc_hook.py 的 hook(install_hooks 的逆操作)
  2. 删除状态库 ~/.cc-monitor/
  3. 提示用户手动删除 CCMonitor.app(如果装过)

幂等:重复运行无副作用。不会动你 settings.json 里的其它配置。
"""
import os, json, shutil, sys

SETTINGS = os.path.expanduser("~/.claude/settings.json")
STATE_DIR = os.path.expanduser("~/.cc-monitor")


def clean_hooks():
    if not os.path.exists(SETTINGS):
        print("· settings.json 不存在,跳过 hook 清理")
        return
    try:
        with open(SETTINGS) as f:
            cfg = json.load(f)
    except Exception:
        print("⚠️  settings.json 解析失败,请手动检查"); return

    hooks = cfg.get("hooks", {})
    removed = 0
    for ev in list(hooks.keys()):
        new_groups = []
        for g in hooks[ev]:
            kept = [h for h in g.get("hooks", [])
                    if "cc_hook.py" not in h.get("command", "")]
            removed += len(g.get("hooks", [])) - len(kept)
            if kept:
                g["hooks"] = kept
                new_groups.append(g)
        if new_groups:
            hooks[ev] = new_groups
        else:
            del hooks[ev]   # 该事件下已无任何 hook,清掉空键
    if not hooks:
        cfg.pop("hooks", None)

    with open(SETTINGS, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print(f"✅ 已从 settings.json 移除 {removed} 个 cc_hook hook")


def clean_state():
    if os.path.isdir(STATE_DIR):
        shutil.rmtree(STATE_DIR, ignore_errors=True)
        print(f"✅ 已删除状态库 {STATE_DIR}")
    else:
        print("· 状态库不存在,跳过")


def main():
    print("==> 卸载 CC Monitor")
    clean_hooks()
    clean_state()
    print("\n还需手动:")
    print("  · 退出菜单栏 App(菜单里「退出 CC Monitor」或  pkill -f cc_monitor)")
    print("  · 若装过 .app:把 /Applications/CCMonitor.app 拖进废纸篓")
    print("  · 重启正在运行的 Claude Code 会话,hook 即彻底失效")


if __name__ == "__main__":
    main()
