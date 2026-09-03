@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\nanobot.exe" goto :NotInstalled

echo 正在启动 WebUI...
echo 启动完成后浏览器会自动打开 http://127.0.0.1:8765
echo 关闭本窗口即停止 WebUI
echo.
".venv\Scripts\nanobot.exe" webui
if errorlevel 1 goto :Failed
exit /b 0

:Failed
echo.
echo WebUI 启动失败。若是首次使用，请先双击 install.cmd 完成安装配置。
pause
exit /b 1

:NotInstalled
echo 尚未安装。请先双击 install.cmd 完成安装后再运行本文件。
pause
exit /b 1