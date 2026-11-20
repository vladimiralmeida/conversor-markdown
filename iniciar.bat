@echo off
title Conversor para Markdown (servidor local - feche esta janela para encerrar)
cd /d "%~dp0"
python conversor_md.py
if errorlevel 1 pause
