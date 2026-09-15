@echo off
setlocal
cd /d "%~dp0"

rem --- find a Python interpreter ---
rem THE STORE STUB IS NOT PYTHON, AND IT ANSWERS `where python`. Windows ships a zero-byte
rem python.exe under WindowsApps whose only job is to open the Microsoft Store. It satisfied the
rem old test, so on a machine with no Python at all -- the one case the message below exists for --
rem this script walked straight past the friendly error and failed later with something unreadable.
rem So we take the first match that is NOT that stub. The `py` launcher is never a stub, which is
rem why it is still tried first and needs no filtering.
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  for /f "delims=" %%I in ('where python 2^>nul') do (
    if not defined PY (
      echo "%%~fI" | find /i "\WindowsApps\" >nul || set "PY=%%~fI"
    )
  )
)

rem --- and is it really there, and new enough? ---
rem ASK THE INTERPRETER rather than trusting that a name resolved. This file used to claim 3.10+
rem while README.md said 3.8+ -- two numbers for one requirement, neither of them ever checked
rem against the thing the user actually has. Exit code 2 means "too old", anything else non-zero
rem means it did not run at all (a stub that slipped through, a broken install, no PATH entry).
rem Plain gotos, not blocks: %errorlevel% inside a parenthesised block expands at parse time, which
rem is how a check like this quietly stops checking anything.
rem `if errorlevel 2` would be WRONG here and was, until it was run: it means "2 or more", so the
rem 9009 that cmd returns for a command it cannot run -- the Store stub, a broken PATH entry -- came
rem out as "your Python is too old", followed by a version line printed by nothing. Exactly 2 is the
rem only code that means old; everything else non-zero means it did not run.
if not defined PY goto :nopython
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 2)" >nul 2>nul
if "%errorlevel%"=="2" goto :oldpython
if errorlevel 1 goto :nopython
goto :pyok

:nopython
echo.
echo Python was not found on your PATH.
echo Install Python 3.8 or newer from https://www.python.org/ and re-run this file.
echo (In the installer, tick "Add python.exe to PATH".)
echo.
pause
exit /b 1

:oldpython
echo.
echo VV Curator needs Python 3.8 or newer. The one on your PATH is older:
"%PY%" -V
echo.
echo Install a current version from https://www.python.org/ and re-run this file.
echo.
pause
exit /b 1

:pyok

rem --- ensure dependencies are available (Pillow for thumbnails, Send2Trash for deletes) ---
rem One combined import first, and the per-package checks only if that fails. Starting the Python
rem interpreter is the whole cost here (~65-175ms each, and it dwarfs the import itself), so the
rem steady state — everything already installed, i.e. every launch after the first — pays for ONE
rem interpreter instead of three. Measured: 297ms -> 200ms. The individual checks below are kept
rem verbatim rather than replaced, because they are what names the missing package in the install
rem message; a combined check alone could only say "something is missing".
rem PIP'S EXIT CODE IS CHECKED, and the imports are re-tested afterwards. None of this was looked
rem at before: a failed install -- no network, a proxy, a wheel that will not build -- printed its
rem own noise and then launched anyway, into a window that could never connect, with the console
rem closing behind it. That path only exists on a machine that has never run this, i.e. every new
rem user. The re-test at the end is the honest one: pip can exit 0 and still leave nothing
rem importable, and what matters is whether the server can start, not whether pip was happy.
"%PY%" -c "import PIL, send2trash, imageio_ffmpeg" >nul 2>nul
if errorlevel 1 (
  "%PY%" -c "import PIL" >nul 2>nul
  if errorlevel 1 (
    echo Installing Pillow ^(one-time^)...
    "%PY%" -m pip install --quiet Pillow
    if errorlevel 1 goto :pipfailed
  )
  "%PY%" -c "import send2trash" >nul 2>nul
  if errorlevel 1 (
    echo Installing Send2Trash ^(one-time^)...
    "%PY%" -m pip install --quiet Send2Trash
    if errorlevel 1 goto :pipfailed
  )
  "%PY%" -c "import imageio_ffmpeg" >nul 2>nul
  if errorlevel 1 (
    echo Installing imageio-ffmpeg for video thumbnails ^(one-time, ~50MB^)...
    "%PY%" -m pip install --quiet imageio-ffmpeg
    if errorlevel 1 goto :pipfailed
  )
  "%PY%" -c "import PIL, send2trash, imageio_ffmpeg" >nul 2>nul
  if errorlevel 1 goto :pipfailed
)
goto :deps_ok

:pipfailed
echo.
echo A one-time install did not finish, so VV Curator cannot start yet.
echo Check your internet connection and run this file again.
echo.
echo If it keeps failing, run this line to see what it says:
echo     %PY% -m pip install Pillow Send2Trash imageio-ffmpeg
echo.
pause
exit /b 1

:deps_ok

echo Starting VV Curator...

rem --- error log: the server runs hidden, so its stderr has nowhere to go unless we catch it.
rem     Keep the previous run's log too — a problem is often reported after a restart, and
rem     without this the evidence would be overwritten by the very restart used to look for it.
if not exist "%~dp0data" mkdir "%~dp0data"

rem --- kill, rotate, launch, open — one PowerShell pass, in that order ---
rem
rem ORDER MATTERS, and it used to be wrong. Rotating viewer.log -> viewer.prev.log happened up
rem here in cmd, BEFORE the kill below. A running server holds viewer.log open as its stderr, so
rem on the "closed the window without pressing the power button" path the `move` failed with
rem "The process cannot access the file because it is being used by another process" — and the
rem `>nul` on it only silenced stdout, so that landed on the console looking like a fatal error.
rem It never was: the kill/start/open below went on to work every time. Rotating AFTER the kill
rem kills the scare AND makes the rotation actually happen on that path — the one run whose log
rem you most want to keep was the one run that silently lost it.
rem
rem The kill itself: a previously-launched (hidden) server keeps holding the port; if we don't
rem clear it, the new server can't bind and the browser silently connects to the OLD one.
rem
rem The port is READ FROM port.txt, then config.json, falling back to 8770 — server.py resolves it
rem in exactly that order and the two must agree. Hardcoding it here meant that changing the port
rem moved the server but not the kill or the URL, so the script would clear the wrong port and then
rem open a browser on it. port.txt is checked SECOND so that it wins: it is the file a user is told
rem to create, and a stale `port` left in config.json must not outrank the one they just set.
rem
rem Reading config.json at all also lets master and the deployed copy run on different ports:
rem config.json is the user's and is never copied by update.bat, so master can carry a test port
rem while the deployed copy keeps 8770 and its localStorage (sidebar width, filters, toggles —
rem all per-origin, so moving the real app's port would silently reset them).
rem
rem The app window: any Chromium browser's `--app=` opens a window with no tab strip and no
rem address bar, and uses the page's favicon as its taskbar icon (hence app/icon.png). We ask
rem Windows which browser handles http and prefer that one — this machine's default is Brave, and
rem an earlier version of this line that only knew Chrome and Edge would have opened the app in
rem Edge instead, i.e. in a browser with none of the user's profile. Browsers are located via the
rem App Paths registry key, checked in HKCU before HKLM because per-user installs (Chrome's
rem default) only appear in HKCU. Anything with no `--app` support — Firefox — or no Chromium
rem browser at all falls back to opening a normal tab, exactly as this script always did.
rem
rem WAITING FOR THE SERVER: a readiness poll, not a fixed sleep. This used to be
rem `Start-Sleep -Seconds 2`, which was ~60% of the whole launch — the server answers in ~290ms on
rem an already-indexed library, so the window sat there waiting on nothing. Measured end to end:
rem ~3.3s -> ~1.2s.
rem
rem An earlier attempt at this was reverted (202fb42) for hanging, so the two traps are worth
rem naming, because both are easy to walk back into:
rem
rem   1. NEVER a BLOCKING TcpClient.Connect. Against a closed loopback port it takes ~2040ms to
rem      fail (measured, and the same for 'localhost' and '127.0.0.1' — it is the blocking connect,
rem      not name resolution). A poll built on it is SLOWER than the sleep it replaces. The fix is
rem      ConnectAsync(...).Wait(50): a refused connection fails in ~65ms. That distinction is the
rem      whole reason this is now viable.
rem   2. The loop MUST have two exits besides success, or a server that dies means a launcher that
rem      never returns — which is worse than a slow one, because you get no app at all. So it also
rem      stops when the process exits ($p.HasExited, measured to bail in ~100ms) and at a hard cap.
rem
rem The cap is deliberately generous (120s) rather than tight: server.py indexes BEFORE it binds,
rem so a first-ever index legitimately holds the port shut for a long time, and a tight cap would
rem open the window onto a dead port — the exact failure this poll otherwise fixes. Whatever
rem happens, the browser is still opened afterwards, so no path here can leave you without a window.
rem THE WINDOW IS OPENED AT http://localhost, NEVER AT A file:// SPLASH — and that is load-bearing,
rem not tidiness. Chromium remembers an --app window's position itself, keyed to an identity derived
rem from the launch URL. The splash (APP-1) opened the window at file:///…/splash.html and let it
rem navigate to http://localhost, so it SAVED its bounds under one identity and LOOKED for them under
rem another: the window came back full-height at the left of the screen, every session, and the user
rem reported it as "it used to work".
rem
rem Fixed by removing the cause rather than compensating for it. Passing --window-position was tried
rem and does not substitute: on a cold browser with a floating window, --window-size was honoured and
rem --window-position was ignored, which is a browser behaviour we do not control. Restoring the
rem original launch URL puts the position back in the hands of the thing that was doing it correctly
rem for months.
rem
rem THE COST, stated so nobody "optimises" it back: the window now appears after the readiness poll
rem instead of before it — roughly 250ms later. That is the entire benefit APP-1 bought, traded for a
rem window that opens where you left it. To get both, the server would have to bind its port BEFORE
rem indexing so it could serve the splash itself at the same origin; see the header of app\splash.html.
powershell -NoLogo -NoProfile -NonInteractive -Command "$port=8770; $cfg='%~dp0config.json'; if (Test-Path $cfg) { try { $j=Get-Content $cfg -Raw -EA Stop | ConvertFrom-Json; if ($j.port) { $port=[int]$j.port } } catch {} }; $pf='%~dp0port.txt'; if (Test-Path $pf) { try { $n=[int]((Get-Content $pf -Raw -EA Stop).Trim()); if ($n -ge 1 -and $n -le 65535) { $port=$n } } catch {} }; $url='http://localhost:'+$port; $prog=(Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice' -EA SilentlyContinue).ProgId; $first=$null; if ($prog -like 'Brave*') { $first='brave.exe' } elseif ($prog -like 'Chrome*') { $first='chrome.exe' } elseif ($prog -like 'MSEdge*') { $first='msedge.exe' } elseif ($prog -like 'Vivaldi*') { $first='vivaldi.exe' } elseif ($prog -like 'Opera*') { $first='opera.exe' }; $order=@(); if ($first) { $order+=$first }; $order+=@('brave.exe','chrome.exe','msedge.exe','vivaldi.exe'); $exe=$null; foreach ($n in $order) { foreach ($h in @('HKCU:','HKLM:')) { if (-not $exe) { $v=(Get-ItemProperty ($h+'\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\'+$n) -EA SilentlyContinue).'(default)'; if ($v -and (Test-Path $v)) { $exe=$v } } } }; $killed=$false; Get-NetTCPConnection -LocalPort $port -State Listen -EA SilentlyContinue | Select-Object -Expand OwningProcess -Unique | ForEach-Object { $pr=Get-CimInstance Win32_Process -Filter ('ProcessId='+$_) -EA SilentlyContinue; if ($pr -and $pr.CommandLine -match 'server\.py') { Stop-Process -Id $_ -Force -EA SilentlyContinue; $killed=$true } }; if ($killed) { Start-Sleep -Milliseconds 400 }; $data='%~dp0data'; $log=Join-Path $data 'viewer.log'; if (Test-Path $log) { Move-Item -Force $log (Join-Path $data 'viewer.prev.log') }; try { $p=Start-Process -FilePath '%PY%' -ArgumentList '-u','server.py' -WindowStyle Hidden -PassThru -RedirectStandardError $log } catch { $p=Start-Process -FilePath '%PY%' -ArgumentList '-u','server.py' -WindowStyle Hidden -PassThru }; if ($p) { $sw=[Diagnostics.Stopwatch]::StartNew(); while ($sw.ElapsedMilliseconds -lt 120000) { if ($p.HasExited) { break }; $ok=$false; $c=New-Object Net.Sockets.TcpClient; try { $ok=$c.ConnectAsync('127.0.0.1',$port).Wait(50) } catch {} finally { $c.Close() }; if ($ok) { break }; Start-Sleep -Milliseconds 40 }; if ($p.HasExited) { Write-Host ''; Write-Host ('The VV Curator server stopped as it started (exit code ' + $p.ExitCode + '). The end of its log:'); Write-Host ''; if (Test-Path $log) { Get-Content $log -Tail 12 | ForEach-Object { Write-Host ('   ' + $_) } } else { Write-Host '   (nothing reached data\viewer.log)' }; exit 1 }; if ($exe) { Start-Process -FilePath $exe -ArgumentList ('--app='+$url) } else { Start-Process $url } } else { Write-Host ''; Write-Host 'Python could not be started.'; exit 1 }"

rem A WINDOW ONTO NOTHING IS WORSE THAN A MESSAGE. If the server DIED -- the port taken by
rem something else, a half-installed dependency, any crash before it binds -- the browser used to
rem open anyway and show a connection error with no hint of the cause, while the one useful thing
rem (the traceback) sat unread in data\viewer.log and the console closed. So: process gone means
rem the log's tail is printed here and nothing is opened.
rem
rem THE TEST IS "HAS IT EXITED", NOT "DID THE POLL SUCCEED", and the difference is load-bearing. A
rem first index of a large or network library legitimately holds the port shut for longer than the
rem 120s cap, and that is a slow start, not a failure -- it still opens the window, exactly as it
rem always did.
if errorlevel 1 (
  echo.
  echo VV Curator did not start.
  echo.
  pause
  exit /b 1
)