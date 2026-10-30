@echo off
rem 一键重启直播服务：先温和退出旧的，再启动新的（正常用户环境，点歌播放器可用）
curl -s -X POST http://127.0.0.1:8765/api/quit >nul 2>&1
timeout /t 3 /nobreak >nul
start "" /min cmd /c "%~dp0run_server.cmd"
timeout /t 5 /nobreak >nul
curl -s http://127.0.0.1:8765/api/state >nul 2>&1 && (echo [OK] 直播服务重启成功，打开 http://127.0.0.1:8765 & pause) || (echo [FAIL] 服务没起来，请双击 tools\run_server.cmd 查看报错 & pause)
