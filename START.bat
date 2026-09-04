@echo off
setlocal
cd /d "%~dp0"
if not defined OLLAMA_FLASH_ATTENTION set "OLLAMA_FLASH_ATTENTION=1"
rem Hand off to the hidden Matrix Core launcher. The temporary cmd wrapper exits now.
start "" /b wscript.exe "%~dp0launch.vbs"
endlocal
exit /b 0
