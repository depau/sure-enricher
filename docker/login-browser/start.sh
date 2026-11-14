set -e
export TZ=Europe/Rome DISPLAY=:0
Xvfb :0 -screen 0 1366x900x24 -nolisten tcp &
sleep 1
# Chromium ignores --remote-debugging-address: expose its loopback CDP port through socat.
socat TCP-LISTEN:9222,fork,reuseaddr TCP:127.0.0.1:9221 &
# No popups or window.open (a new tab would escape the live view and its URL checks).
# Headful under Xvfb (headless is easier to tell apart). A Linux user agent got an empty Edenred voucher
# session; the owner's everyday macOS Chrome string works.
exec chromium --no-sandbox --disable-dev-shm-usage --no-first-run --no-default-browser-check \
  --renderer-process-limit=2 --disable-gpu --disable-extensions \
  --block-new-web-contents \
  --user-agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36" \
  --user-data-dir=/tmp/profile --remote-debugging-port=9221 --remote-allow-origins=* \
  --window-position=0,0 --window-size=1280,900 --lang=it-IT about:blank
