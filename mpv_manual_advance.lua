-- 自动生成，勿手动编辑（由 telegram_mpv_player.py 的 write_advance_script() 写出）
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
