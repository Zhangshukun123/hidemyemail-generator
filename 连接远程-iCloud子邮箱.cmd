@echo off
chcp 65001 >nul
title 远程 iCloud 验证码 - SSH 安全隧道

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0连接远程-iCloud子邮箱.ps1"
exit /b %errorlevel%
