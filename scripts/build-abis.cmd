@echo off
rem ===========================================================================
rem Keepsake local per-ABI build (Windows).
rem Build rules live in the build repo ONLY: Z:\dsh-restore\keepsake-build
rem
rem Usage:
rem   build-abis.cmd [source dir] [--skip-build]
rem
rem   --skip-build : do not run pub get / flutter build, just rename the existing
rem                  <src>\build\app\outputs\flutter-apk\app-release.apk into the
rem                  per-ABI names below and write .sha256 (dry run / re-pack).
rem
rem Output (under the source dir):
rem   build\app\outputs\flutter-apk\app-release.apk        (overwritten per build)
rem   build\abi\keepsake-<ver>-arm64.apk + .apk.sha256
rem   build\abi\keepsake-<ver>-arm.apk   + .apk.sha256
rem
rem NOTE: keep this file ASCII-only (cmd reads .cmd with the ANSI code page).
rem ===========================================================================
setlocal enabledelayedexpansion

set "JAVA_HOME=Z:\dsh-restore\tools\jdk\jdk-17.0.21+9"
set "ANDROID_HOME=Z:\dsh-restore\tools\android\sdk"
set "ANDROID_SDK_ROOT=%ANDROID_HOME%"
set "ANDROID_USER_HOME=Z:\dsh-restore\tools\android\user-home"
set "PATH=%JAVA_HOME%\bin;Z:\dsh-restore\tools\flutter\bin;%ANDROID_HOME%\platform-tools;%ANDROID_HOME%\emulator;%ANDROID_HOME%\cmdline-tools\latest\bin;C:\Users\Administrator\AppData\Local\Programs\MinGit\cmd;%PATH%"
set "PUB_HOSTED_URL=https://pub.flutter-io.cn"
set "FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn"
set "PUB_CACHE=Z:\dsh-restore\tools\pub-cache"
set "GRADLE_USER_HOME=Z:\dsh-restore\tools\gradle-home"

set "SRC="
set "SKIP_BUILD=0"
for %%a in (%*) do (
  if /i "%%~a"=="--skip-build" (
    set "SKIP_BUILD=1"
  ) else (
    set "SRC=%%~a"
  )
)
if "%SRC%"=="" set "SRC=Z:\dsh-restore\keepsake"
if not exist "%SRC%\pubspec.yaml" (
  echo ERROR: source project not found: %SRC%
  exit /b 1
)
cd /d "%SRC%" || exit /b 1
echo source: %CD%

if "%SKIP_BUILD%"=="1" (
  echo [1/3] flutter pub get  -- SKIPPED ^(--skip-build^)
) else (
  echo [1/3] flutter pub get
  call flutter pub get
  if errorlevel 1 (
    echo ERROR: flutter pub get failed
    exit /b 1
  )
)

rem version from pubspec.yaml: "version: 0.4.20+4200" -> 0.4.20
set "RAWVER="
for /f "tokens=2" %%v in ('findstr /b /c:"version:" pubspec.yaml') do set "RAWVER=%%v"
if "%RAWVER%"=="" (
  echo ERROR: no version line in pubspec.yaml
  exit /b 1
)
for /f "tokens=1 delims=+" %%v in ("%RAWVER%") do set "VER=%%v"
echo version: %VER%

if not exist "build\abi" mkdir "build\abi"

rem Two separate builds (--target-platform with a single ABI), so the product
rem really is -arm64 / -arm instead of one fat universal APK.
set /a STEP=1
for %%A in (arm64 arm) do (
  set /a STEP+=1
  if "!SKIP_BUILD!"=="1" (
    echo [!STEP!/3] flutter build apk --target-platform android-%%A  -- SKIPPED
  ) else (
    echo [!STEP!/3] flutter build apk --release --target-platform android-%%A abiFilters=%%A
    rem Only keep the target ABI: plugin .so files otherwise land in every ABI dir,
    rem and x86_64 devices pick that dir then crash on startup with no libflutter.so.
    set "ORG_GRADLE_PROJECT_keepsakeAbi=%%A"
    call flutter build apk --release --target-platform android-%%A
    if errorlevel 1 (
      echo ERROR: android-%%A build failed
      exit /b 1
    )
  )
  set "OUTNAME=keepsake-%VER%-%%A.apk"
  if not exist "build\app\outputs\flutter-apk\app-release.apk" (
    echo ERROR: build finished but app-release.apk is missing
    exit /b 1
  )
  copy /y "build\app\outputs\flutter-apk\app-release.apk" "build\abi\!OUTNAME!" >nul
  if errorlevel 1 (
    echo ERROR: copy of android-%%A product failed
    exit /b 1
  )
  call :write_sha256 "build\abi\!OUTNAME!" "build\abi\!OUTNAME!.sha256"
  if errorlevel 1 exit /b 1
  for %%S in ("build\abi\!OUTNAME!") do echo   product: !OUTNAME! ^(%%~zS bytes^)
)

echo.
echo done. products in %CD%\build\abi\ :
dir /b build\abi\keepsake-%VER%-*.apk
exit /b 0

rem ---------------------------------------------------------------------------
rem Subroutine: SHA256 via certutil, one sha256sum-compatible line
rem   <hash>  <file name>
rem ---------------------------------------------------------------------------
:write_sha256
set "HASH="
for /f "delims=" %%h in ('certutil -hashfile "%~1" SHA256 ^| findstr /r /c:"^[0-9A-Fa-f][0-9A-Fa-f]*$"') do set "HASH=%%h"
if not defined HASH (
  echo ERROR: certutil could not hash %~1
  exit /b 1
)
> "%~2" echo %HASH%  %~nx1
exit /b 0
