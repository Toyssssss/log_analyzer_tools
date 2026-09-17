@echo off
REM Build the Log Analyzer executables for Windows.
REM PyInstaller cannot cross-compile: run this on Windows for Windows binaries.
setlocal
cd /d "%~dp0.."

python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo PyInstaller is not installed. Run:
    echo     python -m pip install -r requirements-build.txt
    exit /b 1
)

python -c "import tkinter" 2>nul
if errorlevel 1 (
    echo ERROR: tkinter is missing, the GUI build will fail.
    echo Reinstall Python with the tcl/tk option enabled.
    exit /b 1
)

echo ==^> running tests
python -m unittest discover -s tests -t .
if errorlevel 1 (
    echo tests failed; refusing to build
    exit /b 1
)

echo ==^> building GUI ^(onedir, windowed^)
python -m PyInstaller --clean --noconfirm packaging\log-analyzer.spec
if errorlevel 1 exit /b 1

echo ==^> building CLI
python -m PyInstaller --clean --noconfirm packaging\log-analyzer-cli.spec
if errorlevel 1 exit /b 1

echo ==^> packaging dist archives
python packaging\make_zips.py
if errorlevel 1 exit /b 1

echo.
echo Done. Artifacts in dist\
echo The GUI bundle is dist\Cluster-log-analyzer\ ^(run Cluster-log-analyzer.exe inside it^).
echo Distribute the whole folder, not just the executable.
endlocal
