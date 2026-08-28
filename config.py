# -*- coding: utf-8 -*-
import os

# ============================================
#  Telegram 配置（⚠️ 改成你自己的）
# ============================================
BOT_TOKEN = "8878513679:AAFCbx8c9IHb5tOZ4TGaF-7fk71n5jTqbro"
CHANNEL_ID = "@list_trolcc"# ⚠️ Bot 必须被加进这个频道并设为管理员，且要有"删除消息"权限，
#    否则清空频道那一步会失败（程序会在日志里报错，不会中断运行）。

# ============================================
#  文件路径
# ============================================
HOME = os.path.expanduser("~")
BASE_DIR = os.path.join(HOME, "telegram_player")
PLAYLIST_FILE = os.path.join(BASE_DIR, "playlist.m3u")
LOG_FILE = os.path.join(BASE_DIR, "player.log")
MPV_SOCKET = os.path.join(BASE_DIR, "mpv_socket")

# ============================================
#  mpv 配置
# ============================================
# 不加 --no-video：播放视频画面，歌词就是视频里烧录的那种字幕
MPV_EXTRA_ARGS = [
    "--audio-device=alsa",
]
