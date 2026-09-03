@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\nanobot.exe" goto :NotInstalled

echo 正在启动常驻后台网关 gateway --background ...
echo 聊天频道与定时任务将在后台持续运行，关闭本窗口不影响后台。
echo.
".venv\Scripts\nanobot.exe" gateway --background
if errorlevel 1 goto :Failed

echo.
echo 后台网关已启动。
echo 查看状态: ".venv\Scripts\nanobot.exe" gateway status
echo 停止网关: ".venv\Scripts\nanobot.exe" gateway stop
echo 再次双击本文件可重复启动，会自动连接已有网关。
pause
exit /b 0

:Failed
echo.
echo 启动失败。若是首次使用，请先双击 install.cmd 完成安装配置。
pause
exit /b 1

:NotInstalled
echo 尚未安装。请先双击 install.cmd 完成安装后再运行本文件。
pause
exit /b 1
