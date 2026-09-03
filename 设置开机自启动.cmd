@echo off
cd /d "%~dp0"
setlocal

if not exist ".venv\Scripts\nanobot.exe" goto :NotInstalled

rem 项目根目录（运行时探测，去掉结尾反斜杠）
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

rem 1) 把项目路径写入用户环境变量（一次性，登录时自动加载）
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Environment]::SetEnvironmentVariable('CATGIRL_MENTOR_HOME', '%ROOT%', 'User')"
if errorlevel 1 goto :Failed

rem 2) 在启动文件夹生成开机脚本，脚本内只引用环境变量，不写任何绝对路径
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if not exist "%STARTUP%" mkdir "%STARTUP%"
set "RUNNER=%STARTUP%\CatgirlMentor-自动启动.cmd"

>  "%RUNNER%" echo @echo off
>> "%RUNNER%" echo cd /d "%%CATGIRL_MENTOR_HOME%%"
>> "%RUNNER%" echo start "" /min ".venv\Scripts\nanobot.exe" gateway --background
if errorlevel 1 goto :Failed

echo.
echo 已设置开机自启动。
echo   启动项文件: %RUNNER%
echo   项目位置:   已记录到用户环境变量 CATGIRL_MENTOR_HOME
echo.
echo 从下次登录起，开机后会自动在后台运行网关。
echo 项目文件夹移动后，重新双击本文件即可更新，无需改动任何文件。
echo 取消开机自启：删除上面的启动项文件即可。
echo 提示：环境变量在下次登录时生效；想立即测试请注销后重新登录。
echo.
pause
exit /b 0

:Failed
echo 设置失败，请重试，或右键本文件选择“以管理员身份运行”。
pause
exit /b 1

:NotInstalled
echo 尚未安装。请先双击 install.cmd 完成安装后再运行本文件。
pause
exit /b 1