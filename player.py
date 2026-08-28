#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram → mpv 播放器（定稿版 v5 —— 菜单栏 + 按日期时间保留历史播放列表）
================================================================
开机自启动，图形界面除了"刷新播放列表"按钮，现在还有 File / Edit / About 菜单。

逻辑：
  开机 → 在 history 文件夹新建一份按"日期_时间"命名的播放列表文件
       （不再清空/覆盖旧文件，历史记录都留着）
       → 显示窗口
  点刷新 → 读取频道所有消息 → 提取 URL → 追加到本次的播放列表文件
         → 删除频道里这些消息（真正调用 deleteMessage，不是只推 offset）
         → mpv 未运行则启动（读取本次播放列表，暂停等待）
         → mpv 已运行则通过 IPC socket 把这次新读到的 URL 追加进去
              （不重启 mpv，不打断正在播放的歌）

字幕：直接播放视频画面（不加 --no-video），字幕就是视频本身烧录的那种，
      不做额外的字幕同步系统。

v4 变更（解决"第二首卡顿严重" + 停止点位置不对 + 第二首被跳过）：
  诊断：卡顿并非单纯因为分辨率，而是 mpv 从一首歌自动切到下一首的瞬间，
        yt-dlp 现场解析新视频的真实流地址 + 网络请求这两件事叠加发生，
        在老机器（4GB RAM）上会造成明显卡顿/掉帧。
  v3 的做法（停在上一首结尾，靠自定义空格键手动触发 playlist-next）
  有两个问题：①人按下播放键那一刻才开始加载下一首，卡顿其实没解决，
  只是往后挪了；②自定义的空格键逻辑和 mpv 自带的暂停/播放逻辑互相
  打架，会出现连续切两首、漏播一首的情况（"第二首没有了"）。
  v4 改为：
    1. 一首歌播完（eof-reached）后，脚本立刻自动在后台调用
       playlist-next 去加载下一首——加载/解析这件事是自动发生的，
       不用等人。
    2. 下一首真正加载完成（file-loaded 事件，此时 yt-dlp 解析、
       资源打开都已经做完）的瞬间，脚本自动把 pause 设为 true，
       画面定格在"下一首的第一帧"，而不是"上一首的最后一帧"。
    3. 播放/暂停完全交还给 mpv 自带的空格键，不再自定义按键逻辑，
       避免 v3 那种两套逻辑打架导致漏播的问题。操作员看到的画面
       就是下一首已经准备好、卡在开头，按一下空格直接播放，
       因为解析/加载已经在等待期间悄悄做完了，不会再卡。
    4. 保留之前的优化：强制不超过 1080p、优先选 H.264（avc1）而非
       VP9/AV1（老机器软解 H.264 比 VP9 轻松很多）、开启硬件解码、
       加大 demuxer 缓存。

v5 变更（加菜单栏 + 播放列表不再用完即弃）：
    1. 每次开机不再清空同一个文件，而是在 history 文件夹里新建一份
       "playlist_年月日_时分秒.m3u"——这样翻一下 history 文件夹，
       就知道上次唱过哪几首，不会像以前那样开机即焚、无迹可查。
    2. 加了标准菜单栏：
       - File：打开某一份历史播放列表（用它重新启动/替换当前播放）、
         打开 history 文件夹方便直接翻旧文件、退出程序。
       - Edit：清空当前这一份播放列表（历史文件不受影响）、
         用系统默认文本编辑器打开当前播放列表文件方便手改。
       - About：程序简介。
"""

import os
import re
import json
import time
import socket
import subprocess
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
import requests

from config import (
    BOT_TOKEN, CHANNEL_ID, PLAYLIST_FILE, LOG_FILE, MPV_SOCKET, MPV_EXTRA_ARGS
)

# 基准目录沿用 config.py 里 PLAYLIST_FILE 所在的文件夹；
# 真正每次开机使用的播放列表文件是 history 文件夹里按时间新建的那份。
BASE_DIR = Path(PLAYLIST_FILE).parent
HISTORY_DIR = BASE_DIR / "history"

# 本次开机实际使用的播放列表文件路径，在 init() 里赋值
CURRENT_PLAYLIST_FILE = None

# ============================================
#  初始化 & 日志
# ============================================

def new_session_playlist_path():
    """
    生成本次开机要用的播放列表文件名——按"日期_时间"命名，
    例如 playlist_20260827_143000.m3u，写入 history 文件夹。
    不覆盖、不清空旧文件，翻一下 history 就知道上次唱过什么。
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    return str(HISTORY_DIR / f"playlist_{ts}.m3u")


def init():
    global CURRENT_PLAYLIST_FILE
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_PLAYLIST_FILE = new_session_playlist_path()
    with open(CURRENT_PLAYLIST_FILE, 'w', encoding='utf-8') as f:
        f.write(f"# Telegram 播放列表 —— {time.strftime('%Y-%m-%d %H:%M:%S')} 本次开机新建\n")
    write_advance_script()
    log(f"✅ 初始化完成，本次播放列表: {CURRENT_PLAYLIST_FILE}")

def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')

# ============================================
#  自动预加载 + 停在下一首开头（写一个 mpv Lua 脚本到磁盘，启动 mpv 时加载它）
# ============================================

ADVANCE_SCRIPT_PATH = str(BASE_DIR / "mpv_manual_advance.lua")

def write_advance_script():
    """
    生成 mpv 的 Lua 脚本，配合启动参数 --keep-open=always：
      - 一首歌播完（eof-reached 变 true）时，脚本立刻自动调用
        playlist-next，让 mpv 在后台把下一首加载好（yt-dlp 解析、
        打开流这些耗资源的动作，在这一步悄悄完成）。
      - 下一首加载完成（file-loaded 事件）后，脚本把 pause 设为 true，
        画面停在"下一首的开头"，而不是停在"上一首的结尾"。
      - 播放/暂停完全用 mpv 自带的空格键，脚本不拦截任何按键，
        避免两套逻辑打架导致漏播。
    """
    lua = '''-- 自动生成，勿手动编辑（由 telegram_mpv_player.py 的 write_advance_script() 写出）
local advancing = false

-- 一首歌播完，立刻自动加载下一首（后台进行，不等人）
mp.observe_property("eof-reached", "bool", function(name, value)
    if value == true and not advancing then
        advancing = true
        mp.set_property_bool("pause", true)
        mp.commandv("playlist-next", "force")
    end
end)

-- 下一首加载完成（解析/打开流都已做完），定格在它的开头，等人按播放
mp.register_event("file-loaded", function()
    if advancing then
        advancing = false
        mp.set_property_bool("pause", true)
    end
end)
'''
    with open(ADVANCE_SCRIPT_PATH, 'w', encoding='utf-8') as f:
        f.write(lua)
    log(f"📝 自动预加载脚本已写入: {ADVANCE_SCRIPT_PATH}")

# ============================================
#  Telegram：读取消息
# ============================================

def get_channel_messages():
    """
    读取频道消息。返回 (messages, max_update_id)
    messages: [{'update_id', 'message_id', 'chat_id', 'text'}, ...]
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 10, "allowed_updates": '["channel_post"]'}

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if not data.get('ok'):
            log(f"⚠️ Telegram API 错误: {data}")
            return [], None

        updates = data.get('result', [])
        messages = []
        max_update_id = None

        for update in updates:
            max_update_id = update['update_id'] if max_update_id is None else max(max_update_id, update['update_id'])
            msg = update.get('channel_post')
            if msg and msg.get('text'):
                messages.append({
                    'update_id': update['update_id'],
                    'message_id': msg.get('message_id'),
                    'chat_id': msg.get('chat', {}).get('id', CHANNEL_ID),
                    'text': msg.get('text', '')
                })

        log(f"📥 从频道读取到 {len(messages)} 条含文本的消息（共 {len(updates)} 条更新）")
        return messages, max_update_id

    except Exception as e:
        log(f"⚠️ 读取频道失败: {e}")
        return [], None


def ack_updates(max_update_id):
    """告诉 Telegram：这些更新已处理，下次 getUpdates 不再返回（offset 机制）"""
    if max_update_id is None:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        requests.get(url, params={"offset": max_update_id + 1, "timeout": 1}, timeout=5)
    except Exception as e:
        log(f"⚠️ 确认 offset 失败: {e}")


def delete_channel_messages(messages):
    """
    真正删除频道里的消息（不是只推 offset）。
    需要 Bot 在频道里是管理员，且有删除消息权限，否则这里会失败——
    失败不影响播放列表功能，只是频道消息删不掉，日志里会看到原因。
    """
    if not messages:
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    ok_count = 0
    for msg in messages:
        try:
            resp = requests.post(url, data={
                "chat_id": msg['chat_id'],
                "message_id": msg['message_id']
            }, timeout=10)
            result = resp.json()
            if result.get('ok'):
                ok_count += 1
            else:
                log(f"  ⚠️ 删除消息 {msg['message_id']} 失败: {result.get('description')}")
        except Exception as e:
            log(f"  ⚠️ 删除消息 {msg['message_id']} 出错: {e}")

    log(f"🗑️ 频道消息删除完成：{ok_count}/{len(messages)} 条成功")


def extract_urls(text):
    pattern = r'https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s<>"\'，。、；：！？\n\r]+'
    urls = re.findall(pattern, text)
    cleaned = []
    for u in urls:
        while u and u[-1] in '.,;:!?)':
            u = u[:-1]
        cleaned.append(u)
    return cleaned


def fetch_and_append():
    """读取频道 → 提取 URL → 追加到播放列表 → 清空频道消息。返回新增的 URL 列表。"""
    log("🔄 开始刷新播放列表...")

    messages, max_update_id = get_channel_messages()
    if not messages:
        log("📭 频道没有新消息")
        return []

    all_urls = []
    for msg in messages:
        urls = extract_urls(msg['text'])
        if urls:
            all_urls.extend(urls)
            preview = msg['text'][:40].replace('\n', ' ')
            log(f"   📝 {preview}... → {len(urls)} 个链接")

    if not all_urls:
        log("❌ 未提取到有效链接")
        ack_updates(max_update_id)  # 没有链接的消息也确认已读，避免反复重复读到
        delete_channel_messages(messages)
        return []

    with open(CURRENT_PLAYLIST_FILE, 'a', encoding='utf-8') as f:
        for url in all_urls:
            f.write(url + '\n')
    log(f"🎵 共添加 {len(all_urls)} 首歌到播放列表: {CURRENT_PLAYLIST_FILE}")

    ack_updates(max_update_id)
    delete_channel_messages(messages)

    return all_urls

# ============================================
#  mpv 控制（IPC socket，不用信号那套）
# ============================================

mpv_process = None

def is_mpv_running():
    global mpv_process
    return mpv_process is not None and mpv_process.poll() is None


def start_mpv():
    global mpv_process
    if os.path.exists(MPV_SOCKET):
        try:
            os.remove(MPV_SOCKET)
        except Exception:
            pass

    cmd = [
        "mpv",
        f"--playlist={CURRENT_PLAYLIST_FILE}",
        "--pause",
        f"--input-ipc-server={MPV_SOCKET}",
        "--title=Telegram Player",
        # 强制不超过 1080p，且优先选 H.264（avc1）而非 VP9/AV1——
        # 老机器软解 H.264 比软解 VP9 轻松很多，这一步比单纯降分辨率更有效
        "--ytdl-format=bestvideo[height<=1080][vcodec^=avc1]+bestaudio/best[height<=1080]",
        # 能用硬件解码就用，减轻 CPU 压力（前提是这台机器的核显支持 VAAPI，
        # 可以先跑 `mpv --hwdec=help` 确认支持哪些后端）
        "--hwdec=auto-safe",
        "--cache=yes",
        "--demuxer-max-bytes=80MiB",
        "--demuxer-readahead-secs=15",
        # 核心改动：播完一首后自动在后台预加载下一首，加载好就暂停在它的开头
        "--keep-open=always",
        f"--script={ADVANCE_SCRIPT_PATH}",
    ] + MPV_EXTRA_ARGS

    try:
        mpv_process = subprocess.Popen(cmd)
        log("▶️ mpv 已启动（暂停状态，请在 mpv 窗口按播放）")
        return True
    except Exception as e:
        log(f"⚠️ 启动 mpv 失败: {e}")
        return False


def mpv_ipc_command(cmd_list, retries=5, delay=0.3):
    """发一条命令到 mpv 的 IPC socket；mpv 刚启动 socket 可能还没就绪，重试几次"""
    for attempt in range(retries):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(MPV_SOCKET)
                payload = json.dumps({"command": cmd_list}) + "\n"
                s.sendall(payload.encode("utf-8"))
            return True
        except Exception:
            time.sleep(delay)
    log(f"⚠️ mpv IPC 命令失败（socket 未就绪或 mpv 未运行）: {cmd_list}")
    return False


def append_to_mpv(urls):
    """播放列表运行中追加新链接，不重启、不打断当前播放"""
    ok = 0
    for url in urls:
        if mpv_ipc_command(["loadfile", url, "append-play"]):
            ok += 1
    log(f"🔄 已通过 IPC 追加 {ok}/{len(urls)} 首到正在运行的 mpv")


def refresh_mpv(new_urls):
    if not new_urls:
        return
    if is_mpv_running():
        append_to_mpv(new_urls)
    else:
        start_mpv()

# ============================================
#  图形界面
# ============================================

class PlayerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("🎵 Telegram 播放器")
        self.root.geometry("400x200")
        self.root.resizable(False, False)

        self.status = tk.StringVar(value="就绪")
        self.song_count = tk.StringVar(value="0 首歌")
        self.is_refreshing = False

        self.setup_menu()
        self.setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(500, self.auto_refresh_on_start)

    # ---------- 菜单栏 ----------

    def setup_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="打开历史播放列表...", command=self.open_history_playlist)
        file_menu.add_command(label="打开历史文件夹", command=self.open_history_folder)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="清空当前播放列表", command=self.clear_current_playlist)
        edit_menu.add_command(label="用文本编辑器打开当前播放列表", command=self.open_current_in_editor)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        menubar.add_command(label="About", command=self.show_about)

        self.root.config(menu=menubar)

    def open_history_playlist(self):
        """从 history 文件夹里挑一份旧的播放列表，加载进正在运行的 mpv
        （或者 mpv 没开着的话，直接用这份历史列表启动 mpv）。"""
        path = filedialog.askopenfilename(
            initialdir=str(HISTORY_DIR),
            title="选择历史播放列表",
            filetypes=[("播放列表", "*.m3u"), ("所有文件", "*.*")],
        )
        if not path:
            return
        log(f"📂 加载历史播放列表: {path}")
        if is_mpv_running():
            mpv_ipc_command(["loadlist", path, "replace"])
            self.status.set(f"已加载历史列表: {os.path.basename(path)}")
        else:
            global CURRENT_PLAYLIST_FILE
            CURRENT_PLAYLIST_FILE = path
            start_mpv()
            self.status.set(f"已用历史列表启动: {os.path.basename(path)}")

    def open_history_folder(self):
        try:
            subprocess.Popen(["xdg-open", str(HISTORY_DIR)])
        except Exception as e:
            log(f"⚠️ 打开历史文件夹失败: {e}")
            messagebox.showerror("错误", f"打不开文件夹：{e}")

    def clear_current_playlist(self):
        """只清空本次这一份播放列表文件，history 里其他旧文件不受影响。"""
        if not messagebox.askyesno("确认", "清空当前播放列表？\n（历史文件不受影响，只清空本次这一份）"):
            return
        try:
            with open(CURRENT_PLAYLIST_FILE, 'w', encoding='utf-8') as f:
                f.write(f"# Telegram 播放列表 —— {time.strftime('%Y-%m-%d %H:%M:%S')} 手动清空\n")
            if is_mpv_running():
                mpv_ipc_command(["playlist-clear"])
            log("🗑️ 当前播放列表已清空")
            self.status.set("当前播放列表已清空")
        except Exception as e:
            log(f"⚠️ 清空播放列表失败: {e}")
            messagebox.showerror("错误", f"清空失败：{e}")

    def open_current_in_editor(self):
        try:
            subprocess.Popen(["xdg-open", CURRENT_PLAYLIST_FILE])
        except Exception as e:
            log(f"⚠️ 打开播放列表文件失败: {e}")
            messagebox.showerror("错误", f"打不开文件：{e}")

    def show_about(self):
        messagebox.showinfo(
            "About",
            "🎵 Telegram 播放器\n\n"
            "从 Telegram 频道读取歌曲链接，自动加入 mpv 播放列表。\n"
            "每次开机会在 history 文件夹里新建一份按日期时间命名的\n"
            "播放列表，不会用完即弃——想看上次唱了哪些歌，\n"
            "File 菜单里打开历史播放列表就行。"
        )

    def on_close(self):
        log("👋 程序退出")
        self.root.destroy()

    # ---------- 主界面 ----------

    def setup_ui(self):
        tk.Label(self.root, text="🎵 Telegram 播放器", font=("Arial", 18, "bold")).pack(pady=15)

        self.btn = tk.Button(
            self.root, text="🔄 刷新播放列表", font=("Arial", 14),
            width=20, height=2, command=self.on_refresh,
            bg="#4CAF50", fg="white", relief=tk.RAISED, cursor="hand2"
        )
        self.btn.pack(pady=10)

        status_frame = tk.Frame(self.root)
        status_frame.pack(pady=10)
        tk.Label(status_frame, textvariable=self.status, font=("Arial", 10), fg="gray").pack(side=tk.LEFT)
        tk.Label(status_frame, textvariable=self.song_count, font=("Arial", 10), fg="blue").pack(side=tk.RIGHT, padx=20)

        tk.Label(
            self.root, text="💡 发歌到频道 → 点刷新 → mpv 里按播放（历史列表见 File 菜单）",
            font=("Arial", 9), fg="gray"
        ).pack(side=tk.BOTTOM, pady=10)

    def auto_refresh_on_start(self):
        log("🚀 启动后自动刷新一次...")
        self.on_refresh()

    def on_refresh(self):
        if self.is_refreshing:
            return
        self.is_refreshing = True
        self.btn.config(state=tk.DISABLED, text="⏳ 刷新中...")
        self.status.set("正在读取频道...")
        threading.Thread(target=self.do_refresh, daemon=True).start()

    def do_refresh(self):
        try:
            new_urls = fetch_and_append()
            self.root.after(0, lambda: self.update_ui(len(new_urls)))
            if new_urls:
                self.root.after(0, lambda: refresh_mpv(new_urls))
                self.root.after(0, lambda: self.status.set("▶️ 已更新，请在 mpv 窗口按播放" if not is_mpv_running() else "✅ 已追加到播放列表"))
        except Exception as e:
            log(f"⚠️ 刷新失败: {e}")
            self.root.after(0, lambda: self.status.set(f"❌ 错误: {e}"))
        finally:
            self.root.after(0, self.reset_button)

    def update_ui(self, count):
        if count > 0:
            self.song_count.set(f"+{count} 首新歌")
        else:
            self.song_count.set("没有新歌")
            self.status.set("📭 频道无新消息")

    def reset_button(self):
        self.is_refreshing = False
        self.btn.config(state=tk.NORMAL, text="🔄 刷新播放列表")

# ============================================
#  主程序入口
# ============================================

def main():
    init()
    root = tk.Tk()
    app = PlayerGUI(root)
    log("🚀 程序启动，等待用户操作...")
    root.mainloop()

if __name__ == "__main__":
    main()
