@echo off
rem 直播插件正式启动器：由任务计划程序在正常用户环境运行（不在任何沙箱里），
rem 这样 ncm-cli 播放器 daemon、mpv 都能正常工作。
cd /d "%~dp0.."
"%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -u -B -m liveplugin.web --port 8765
