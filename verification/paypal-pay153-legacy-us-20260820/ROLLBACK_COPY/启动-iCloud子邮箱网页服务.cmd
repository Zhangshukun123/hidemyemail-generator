@echo off
setlocal

cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python environment is missing. Run the normal launcher first.
  echo [错误] 缺少 Python 环境，请先运行普通启动器完成安装。
  pause
  exit /b 1
)

echo Starting local iCloud Hide My Email service...
echo 正在启动本地 iCloud 隐藏邮箱服务...
echo http://127.0.0.1:8765/

".venv\Scripts\python.exe" -m hidemyemail_generator.webapp --host 127.0.0.1 --port 8765 --region china --data-dir "%~dp0" --open-browser

endlocal
