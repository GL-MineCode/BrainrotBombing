@echo off
chcp 65001 >nul
rem ===========================================================================
rem  BrainrotBombing 静默轰炸
rem  一次性弹出 8 个视频窗口，每个视频最长播放 30 秒，弹窗在屏幕上乱飞；
rem  控制器窗口不显示。
rem  想退出：右键任务栏托盘里的炸弹图标，选「退出」（双击图标可以打开控制器）。
rem  注意：注释里别写 > 或 <，cmd 会把它当成重定向符照做。
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "COUNT=8"
set "DURATION=30"
rem 视频目录：默认是程序同级的 assets_light，想播放别的文件夹就改这个路径
set "ASSETS_DIR=%~dp0assets_light"
rem 不想要乱飞就去掉下面的 --fly
set "ARGS=--silent --count %COUNT% --duration %DURATION% --fly"

rem 同一文件夹里放了打包好的 exe 就优先用它
if exist "%~dp0BrainrotBombing.exe" (
    start "" "%~dp0BrainrotBombing.exe" %ARGS% --assets "%ASSETS_DIR%"
    goto :done
)

rem 源码版优先用 pythonw，免得额外弹出一个黑色控制台窗口
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0main.py" %ARGS% --assets "%ASSETS_DIR%"
    goto :done
)

where python >nul 2>nul
if not errorlevel 1 (
    start "" python "%~dp0main.py" %ARGS% --assets "%ASSETS_DIR%"
    goto :done
)

echo [BrainrotBombing] 没找到 BrainrotBombing.exe，也没找到 python / pythonw。
echo 请先安装 Python 3.10 以上版本，并执行：pip install -r requirements.txt
pause

:done
