@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Conversor para Markdown - instalacao ===
python --version >nul 2>&1
if errorlevel 1 (
  echo Python nao encontrado. Instale o Python 3.10+ em https://www.python.org/downloads/ marcando "Add python.exe to PATH".
  pause & exit /b 1
)
echo Instalando bibliotecas...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 ( echo Falha ao instalar as bibliotecas. & pause & exit /b 1 )
echo Criando atalho na area de trabalho...
powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Conversor para Markdown.lnk')); $s.TargetPath='%~dp0iniciar.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0conversor_md.ico,0'; $s.WindowStyle=7; $s.Description='Conversor para Markdown'; $s.Save()"
echo.
echo Pronto. Para usar OCR, instale tambem Tesseract e Poppler (veja o README.md).
pause
