@echo off
chcp 65001 >nul
rem ===========================================================================
rem  BrainrotBombing 一键打包 exe（用 PyInstaller）
rem  直接双击本文件即可，产物在 dist 文件夹里。
rem  会自动检查并安装缺少的依赖（PyQt6 / PyInstaller），需要联网。
rem ===========================================================================
setlocal
cd /d "%~dp0"

rem ------------------------------- 可改配置 ---------------------------------
set "APP_NAME=BrainrotBombing"
rem 1 = 打包成单个 exe（好分享，启动稍慢）；0 = 打包成一个文件夹（启动更快）
set "ONEFILE=1"
rem 打包成功后自动打开 dist 文件夹：1 打开，0 不打开
set "OPEN_DIST=1"
rem 打包成功后把 exe 复制回本文件夹（方便直接配合「静默轰炸.cmd」使用）：1 复制，0 不复制
set "COPY_HERE=0"
rem --------------------------------------------------------------------------

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 python，请先安装 Python 3.10 以上版本，安装时记得勾选「Add python.exe to PATH」。
    goto :fail
)

echo [1/4] 检查运行依赖 PyQt6 ...
python -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo       缺少 PyQt6，正在安装 requirements.txt ...
    python -m pip install -r requirements.txt || goto :fail
) else (
    echo       PyQt6 已就绪
)

echo [2/4] 检查打包工具 PyInstaller ...
python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo       缺少 PyInstaller，正在安装 ...
    python -m pip install pyinstaller || goto :fail
) else (
    echo       PyInstaller 已就绪
)

echo [3/4] 清理上次的 build / dist ...
if exist build rmdir /s /q build
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"
if exist dist rmdir /s /q dist
mkdir build

if "%ONEFILE%"=="1" (set "MODE=--onefile") else (set "MODE=--onedir")
echo [4/4] 开始打包 %APP_NAME%.exe（%MODE%，第一次打包要几分钟，请耐心等）...
python -m PyInstaller --noconfirm --clean --windowed --noupx --name "%APP_NAME%" %MODE% --specpath build main.py
if errorlevel 1 goto :fail

if "%ONEFILE%"=="1" (set "TARGET=dist\%APP_NAME%.exe") else (set "TARGET=dist\%APP_NAME%\%APP_NAME%.exe")
if not exist "%TARGET%" (
    echo [错误] 打包结束但没找到 %TARGET%，请检查上面的输出。
    goto :fail
)

if "%COPY_HERE%"=="1" (
    if "%ONEFILE%"=="1" (
        copy /y "%TARGET%" "%APP_NAME%.exe" >nul
        echo 已复制到 %CD%\%APP_NAME%.exe
    ) else (
        echo [提示] 目录模式没法直接复制，请把整个 dist\%APP_NAME% 文件夹拷到需要的地方。
    )
)

echo.
echo ============================ 打包完成 ============================
echo 产物：%CD%\%TARGET%
echo 注意：exe 不自带视频，请把 assets_light（或 assets）文件夹和它放在一起；
echo       视频在别处就用启动参数指定，例如：%APP_NAME%.exe --assets D:\我的视频
echo       双击「静默轰炸.cmd」时会自动优先使用同目录下的 %APP_NAME%.exe。
if "%OPEN_DIST%"=="1" start "" explorer "%CD%\dist"
endlocal
exit /b 0

:fail
echo.
echo 打包失败，请根据上面的报错排查（常见原因：没联网、pip 太旧、Python 版本过低）。
pause
endlocal
exit /b 1
