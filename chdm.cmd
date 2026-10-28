@echo off
rem Command-line launcher: waits for CHD Manager and passes its exit code on. Usage: chdm game.chd --to pbp -o D:\out   (chdm --help lists everything)
start "" /wait /b "%~dp0CHD Manager.exe" cli %*
exit /b %errorlevel%
