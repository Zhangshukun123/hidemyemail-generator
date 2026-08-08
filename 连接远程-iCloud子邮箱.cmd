@echo off
chcp 65001 >nul
title 远程 iCloud 验证码 - SSH 安全隧道

echo 正在连接远程服务器 cac...
echo 连接期间请保留此窗口。关闭窗口会断开远程网页。
echo 本地访问地址：http://127.0.0.1:18765/
echo.

start "" /B powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:18765/'"
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 18765:127.0.0.1:18767 cac

echo.
echo SSH 隧道已断开。
pause
