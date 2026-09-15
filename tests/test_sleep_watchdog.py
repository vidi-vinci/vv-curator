"""The server survives the laptop sleeping (server.py's client watchdog).

Run: python test_sleep_watchdog.py

The watchdog stops the process when its window is gone, on two wall-clock rules: a beacon arms a
15s countdown, and hearing nothing for 180s means the browser died without saying goodbye.

A SLEEPING LAPTOP SUSPENDS THIS PROCESS TOO. On wake `time.time()` has jumped by however long the
lid was shut, so `now - seen` is hours, the staleness rule fires on the first tick, and the app is
dead before the browser -- also just waking, and throttled by Chromium besides -- can check in.
The author reported it as the app dying after sleep, and guessed the close-on-window-gone feature. It was
the backstop behind that feature reading a suspend as a dead client.

The fix is that the tick knows how long its own sleep ACTUALLY took. A 2-second sleep that took
three hours is a suspend, not a slow loop, and every timestamp it holds is then stale for reasons
that say nothing about the browser. What this pins:

  1. Sleep does NOT kill the server, at any duration.
  2. The wake re-arms rather than cancels: a window closed just before the lid still shuts the
     server down, it just gets its grace measured from the wake -- the only moment a page could
     answer from.
  3. A browser that really did die during the sleep is still caught, on the ordinary rules, three
     minutes after the wake.
  4. Nothing about the normal cases changed -- a live client, a beacon, a real staleness.

Pure: the tick takes `now` and `slept` as arguments, so the clock jump is passed in rather than
waited for. No sleeping, no server, no threads.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                              # noqa: E402

TICK = server.WATCHDOG_TICK_S
GRACE = server.CLIENT_GRACE_S
STALE = server.CLIENT_STALE_S

failures = []


def check(name, cond, detail=''):
    if cond:
        print('  ok    %s' % name)
    else:
        print('  FAIL  %s%s' % (name, ('\n          ' + str(detail)) if detail else ''))
        failures.append(name)


def live(seen, gone_at=None, ever=True):
    server._LIVE.update(seen=seen, gone_at=gone_at, ever=ever)


def tick(now, slept=TICK):
    return server._watchdog_tick(now, slept)


print('\nThe watchdog and a sleeping laptop\n')

# --- the normal rules, unchanged ---------------------------------------------------------------
live(seen=1000.0)
check('a client that just checked in is fine', tick(1002.0) is False)

live(seen=1000.0)
check('silence past the staleness limit stops it', tick(1000.0 + STALE + 1) is True)

live(seen=1000.0, gone_at=1000.0)
check('a beacon inside its grace does not stop it', tick(1000.0 + GRACE - 1) is False)
check('  and past its grace does', tick(1000.0 + GRACE + 1) is True)

live(seen=0.0, gone_at=None, ever=False)
check('nothing happens before a window has ever appeared',
      tick(999999.0) is False, 'exited before the browser ever opened')

# --- the sleep --------------------------------------------------------------------------------
print('')
for hours in (0.5, 3, 24):
    gap = hours * 3600
    live(seen=1000.0)
    woke = 1000.0 + gap
    check('a %g-hour sleep does not kill the server' % hours,
          tick(woke, slept=TICK + gap) is False, 'exited on wake')
    check('  and the clock is reset to the wake', server._LIVE['seen'] == woke,
          server._LIVE['seen'])

# The tick right after the wake is an ORDINARY one — the client has had no time to check in yet,
# and the reset is what has to carry it. This is the exact moment the bug killed the app.
live(seen=1000.0)
woke = 1000.0 + 7200
tick(woke, slept=TICK + 7200)
check('the ordinary tick straight after a wake is also fine',
      tick(woke + TICK) is False, 'exited two seconds after waking')

# A browser that really did die while the lid was shut is still caught, on the normal rule.
live(seen=1000.0)
tick(woke, slept=TICK + 7200)
check('a browser that died during the sleep is still caught',
      tick(woke + STALE + 1) is True, 'a dead client now runs forever')

# --- a window closed just before the lid --------------------------------------------------------
print('')
live(seen=1000.0, gone_at=999.0)
woke = 1000.0 + 7200
check('a beacon armed before the sleep does not fire ON the wake',
      tick(woke, slept=TICK + 7200) is False, 'no chance for a reloading page to answer')
check('  the countdown is re-armed from the wake, not cancelled',
      server._LIVE['gone_at'] == woke, server._LIVE['gone_at'])
check('  and it still stops once that grace runs out',
      tick(woke + GRACE + 1) is True, 'a closed window left the server running')

# A suspend must not be inferred from a merely slow tick — that would hand a genuinely dead client
# an extra grace period every time the machine was busy.
live(seen=1000.0)
check('a slightly slow tick is NOT treated as a suspend',
      tick(1000.0 + STALE + 1, slept=TICK + 1) is True, 'jitter now looks like a sleep')

# THE LOOP MUST LEAVE NO UNWATCHED GAP. `slept` is measured from the PREVIOUS tick, not from just
# before this sleep — otherwise a suspend landing between the two is invisible, and the tick after
# it sees an ordinary sleep beside a pre-sleep `seen` and exits. A millisecond-wide window, but it
# is the original bug, so the source is held to the shape that closes it.
import inspect                                             # noqa: E402
src = inspect.getsource(server._client_watchdog)
check('the loop carries its timestamp forward between ticks',
      'last = time.time()' in src and 'slept, last = now - last, now' in src,
      'timing only the sleep leaves a gap a suspend can hide in')
check('  and nothing re-reads the clock inside the loop before sleeping',
      src.count('time.sleep(') == 1 and 'before = time.time()' not in src)

print('\n%s\n' % ('%d FAILED' % len(failures) if failures else 'all passed'))
sys.exit(1 if failures else 0)
