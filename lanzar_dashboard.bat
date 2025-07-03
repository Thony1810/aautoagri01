@echo off
echo Iniciando dashboard de fertilización...
cd /d "C:\Users\AAUTOAGRI01\OneDrive - Compania Agricola Industrial Santa Ana, S. A\Escritorio\SQL\venv"
call venv\Scripts\activate.bat
start /B cmd /C "streamlit run appdashboardfertiv1.py"
