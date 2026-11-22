# Install Jobfeed on your own server

This page is for a coding agent (Claude Code, Codex, a hosted agent, or similar)
that a user asked to install Jobfeed on their own always-on machine: a Linux
VPS or cloud instance, or a Mac mini. Follow it in order. A person can follow it
too.

It installs **Jobfeed** as a background service: it scans job sites, filters
and ranks the jobs against the user's résumé with AI, and shows them on a
dashboard. As options, it also reads replies to applications from Gmail
(confirmations, interviews, rejections, offers) and applies to the jobs the
user picks with Claude Appliers, which wait for the user to confirm every
Submit.

The user only talks to you, in this chat. Assume they are not technical and
have no terminal on the server. Everything that needs their input (a choice, a
sign-in link, a code, their résumé) goes through you.

## How to talk to the user

The user should always know where they are, what they will get, and what you
need from them. Nothing else.

- **No filler.** No greetings, praise, apologies, "Sure!", "Let me…", or
  repeating what they said. Report results, not your process. Never explain a
  command or paste one, nor JSON, logs, or error output.
- **One shape for every message:** a status line, then at most one question.

  ```
  ✅ Step 3/9 · Jobfeed now runs in the background and restarts by itself.
  ```

  Use ⏳ before something slow ("⏳ Step 2/9 · Installing, about 3 minutes."),
  ⚠️ for something the user should know, and ❌ when a step failed and you need
  them. Keep a message under eight lines, except the first message and the
  hand-over.
- **Questions are numbered choices** with the recommended one first, so the
  user can reply with a number:

  ```
  How should Jobfeed use AI?
  1. Your Claude subscription (recommended, no extra cost)
  2. Your ChatGPT subscription
  3. An API key (billed per use)
  ```

- **Plain words.** Say "keeps running in the background" and "starts again
  after a restart", not "systemd unit" or "linger".
- **Fix failures yourself.** Tell the user only what a failure means for them,
  in one sentence, and what you are doing about it.

### The first message

After the server check (step 1), and before you change anything, send one
message in this shape:

```
**Jobfeed setup on <hostname>**

What you get
• A job dashboard that runs on your server around the clock
• New jobs from LinkedIn, Indeed and company career pages, ranked against your résumé by AI
• A scan every day by itself, or only when you start one: your choice
• Optional extras: Handshake and signed-in LinkedIn, a private link for your phone,
  replies to your applications from Gmail, applying to the jobs you pick (you confirm every Submit)

What I need from you (about 10 minutes)
1. Your résumé: a PDF, Word file or pasted text
2. A Claude or ChatGPT sign-in, which you approve in your browser
3. A few choices, each one a number

Steps
1 Check server ✅ · 2 Install · 3 Background service · 4 AI · 5 Résumé and profile
6 Searches, schedule and limits · 7 Extras · 8 Test · 9 Hand-over

Server: ✅ <memory> memory, <disk> free disk, internet OK
Start the install? (yes)
```

Then post one status line per step as you finish it.

## Rules for the agent

- Run every command yourself. When the user must act (open a link, approve a
  sign-in), tell them exactly what to click and what they will see.
- Never print, log, or commit API keys or tokens.
- Do not start a scan or an evaluation on your own. The only one during setup
  is the first scan that step 8 offers, after the user says yes.
- Never start an application, press Submit, or turn on auto submit or
  autopilot. Only the user does those (see "Applying for them").
- Jobfeed's dashboard has no accounts. It refuses to bind anything except
  loopback. The only ways to reach it from outside the server are the private
  link (step 7), which asks for an access code, and an SSH tunnel. Do not open
  a public port, a `0.0.0.0` forward, or any other proxy to it.
- Use `sudo` only for the steps that say so, and ask first ("I need
  administrator rights to … OK?"). If `sudo` asks for a password, explain that
  it is the password they use to log in to the server, run the command in tmux,
  and have them send it to you only if they cannot type it into the server
  themselves.
- Every step is safe to run again. If a step fails, fix the cause and rerun it;
  do not delete the checkout or `jobfeed/data/` to start over.
- Do not edit `config.toml` or the files in `data/` by hand. A failing
  `jobfeed onboard` command names the missing step.

## 1. Find the server and check it

Work out where Jobfeed should go:

- **You run on the server.** Run the commands below directly.
- **You run elsewhere** (the user's laptop, a cloud or hosted agent). Use
  whatever you have that runs commands on the server: a remote-shell or machine
  tool you were given (for example, a connector that controls the user's cloud
  computer), or `ssh <host>`. If it is not obvious which machine the user means,
  ask. Run every command below there, not on your own machine.

If you are a cloud agent, the machine your own shell runs on is a temporary
sandbox that disappears after this session. Never install Jobfeed there, even if
it looks like a normal Linux server. If you have no tool that reaches the user's
server, say so and ask how to reach it.

Some steps need an interactive terminal (a sign-in that waits for a code). If
your way of running commands is not interactive, run those in `tmux` on the
server (install it if missing): `tmux new -d -s jf '<command>'`, read the output
with `tmux capture-pane -p -t jf`, and type into it with
`tmux send-keys -t jf '<text>' Enter`.

Jobfeed needs:

| | Minimum | Recommended |
|---|---|---|
| System | Linux with systemd, or macOS | |
| Processor | 64-bit (`x86_64`, `aarch64` or `arm64`) | |
| Memory (total) | 1 GB, with swap | 2 GB or more |
| Free disk in the home folder | 4 GB | 6 GB |
| Internet | can reach github.com, astral.sh and pypi.org | |

The disk goes to Jobfeed's Python environment (about 1.5 GB), a private
Python, download caches, a small scoring model and the Claude Code or Codex CLI.
Model calls run at the provider, not on the server.

```sh
hostname; whoami; uname -sm
if [ "$(uname -s)" = Darwin ]; then
    echo "mem_mb=$(( $(sysctl -n hw.memsize) / 1048576 ))"
else
    echo "mem_mb=$(free -m | awk '/^Mem:/ {print $2}') available_mb=$(free -m | awk '/^Mem:/ {print $7}') swap_mb=$(free -m | awk '/^Swap:/ {print $2}')"
    command -v systemctl >/dev/null && echo systemd-ok
fi
echo "disk_free_mb=$(df -Pm "$HOME" | awk 'NR==2 {print $4}')"
for url in https://github.com https://astral.sh https://pypi.org/simple/; do
    curl -fsSI -m 10 "$url" >/dev/null && echo "reach-ok $url" || echo "reach-FAIL $url"
done
command -v git curl tmux
```

Remember `available_mb`: step 6 uses it. Then:

- **Everything meets the recommended column:** send the first message.
- **32-bit processor, no systemd on Linux, under 1 GB of memory, or under 4 GB
  of free disk:** stop. Send ❌ with what is short, by how much, and what would
  fix it, for example: "Your server has 512 MB of memory; Jobfeed needs at
  least 1 GB, and 2 GB is better. Most cloud providers offer a 2 GB plan for a
  few dollars a month." Do not install.
- **1–2 GB of memory and less than 1 GB of swap (Linux):** in the first message,
  add ⚠️ that the server is small and ask to let it borrow 2 GB of disk as
  extra memory. With their OK:

  ```sh
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  ```

  If `swapon` fails (some containers forbid it), say Jobfeed may be slow or
  stop during big scans, and continue only if they accept that.
- **Plenty of total memory but under 700 MB available:** add ⚠️ that other
  programs are using most of it, so scans will be slow.
- **A site is unreachable:** ❌ the server cannot reach the sites Jobfeed
  downloads from; ask whether it is behind a firewall or proxy.
- **Git, `curl` or `tmux` missing:** install them after asking
  (`sudo apt-get install -y git curl tmux` on Debian and Ubuntu;
  `brew install git tmux` on macOS). Python is not required up front.

## 2. Install

Use `~/jobfeed` without asking.

```sh
JOBFEED_HOME="${JOBFEED_HOME:-$HOME/jobfeed}"
if [ -d "$JOBFEED_HOME/.git" ]; then
    git -C "$JOBFEED_HOME" pull --ff-only
else
    git clone https://github.com/V1ctor2182/jobfeed.git "$JOBFEED_HOME"
fi
cd "$JOBFEED_HOME/jobfeed"
JOBFEED_SETUP_NO_OPEN=1 ./setup.sh
curl -fsS http://127.0.0.1:7654/api/health
kill "$(cat "$JOBFEED_HOME/jobfeed/data/jobfeed-serve.pid")" 2>/dev/null || true
rm -f "$JOBFEED_HOME/jobfeed/data/jobfeed-serve.pid"
JF="$JOBFEED_HOME/jobfeed/.venv/bin/jobfeed"
```

`setup.sh` installs `uv` and a private Python 3.12, builds `jobfeed/.venv`,
creates the database, and starts a temporary server, which the last lines stop
so the service in step 3 owns the port. If `git pull` refuses because of local
changes, stop and show the user `git -C "$JOBFEED_HOME" status --short`. Do not
discard their changes.

## 3. Background service

Services start with a minimal `PATH`. Jobfeed can use a signed-in `claude` or
`codex` CLI as its model provider, so the service must be able to find them.
Build `SERVICE_PATH` from the directories that actually hold them, followed by
the system defaults. Rebuild it, rewrite the service and restart it whenever you
install one of those CLIs later (step 4).

```sh
SERVICE_PATH=""
for tool in claude codex; do
    dir="$(dirname "$(command -v "$tool" 2>/dev/null)" 2>/dev/null)" || continue
    [ -n "$dir" ] && [ "$dir" != "." ] && case ":$SERVICE_PATH:" in *":$dir:"*) ;; *) SERVICE_PATH="$SERVICE_PATH$dir:" ;; esac
done
SERVICE_PATH="${SERVICE_PATH}$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
echo "$SERVICE_PATH"
```

### Linux (systemd)

Write `~/.config/systemd/user/jobfeed-dashboard.service`, replacing
`JOBFEED_HOME` with the absolute checkout path and `SERVICE_PATH` with the
value printed above:

```ini
[Unit]
Description=Jobfeed dashboard on 127.0.0.1:7654

[Service]
WorkingDirectory=JOBFEED_HOME/jobfeed
Environment=PATH=SERVICE_PATH
ExecStart=JOBFEED_HOME/jobfeed/.venv/bin/jobfeed serve --host 127.0.0.1 --port 7654
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now jobfeed-dashboard.service
```

So the service keeps running after the user logs out and starts at boot, enable
lingering. This needs `sudo` on most systems; ask first:

```sh
loginctl show-user "$USER" -p Linger | grep -q yes || sudo loginctl enable-linger "$USER"
```

### macOS (launchd)

Write `~/Library/LaunchAgents/com.jobfeed.dashboard.plist`, replacing
`JOBFEED_HOME` and `SERVICE_PATH`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobfeed.dashboard</string>
  <key>WorkingDirectory</key><string>JOBFEED_HOME/jobfeed</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>SERVICE_PATH</string></dict>
  <key>ProgramArguments</key>
  <array>
    <string>JOBFEED_HOME/jobfeed/.venv/bin/jobfeed</string>
    <string>serve</string><string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>7654</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>JOBFEED_HOME/jobfeed/data/jobfeed-serve.log</string>
  <key>StandardErrorPath</key><string>JOBFEED_HOME/jobfeed/data/jobfeed-serve.log</string>
</dict>
</plist>
```

```sh
launchctl bootout "gui/$(id -u)/com.jobfeed.dashboard" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.jobfeed.dashboard.plist
```

On a headless Mac mini, the user must stay logged in (or enable automatic
login) for a LaunchAgent to run.

Wait up to 30 seconds for `curl -fsS http://127.0.0.1:7654/api/health` to
succeed. If it does not, read the log (`journalctl --user -u
jobfeed-dashboard -n 50` on Linux, `jobfeed/data/jobfeed-serve.log` on macOS)
and fix the cause.

## 4. AI provider

From here on, setup runs through `"$JF" onboard`, which calls the running
dashboard and prints JSON for you. `"$JF" onboard status` shows `next_step`;
setup can stop and resume at any point. Every choice is the user's: ask, then
wait.

Ask with the numbered choice shown above. Recommend the subscription that
matches the CLI you are running in.

- **Subscription (`claude_cli` or `codex_cli`).** The CLI must be installed and
  signed in **on the server**, as the same user the service runs as. If you
  run elsewhere, that is a separate sign-in from yours.
  1. Run `"$JF" onboard provider claude_cli` (or `codex_cli`).
  2. If `connected` is false: install the CLI if missing, start
     `claude auth login` (or `codex login --device-auth`) in tmux, and send the
     user only the link and code it prints:

     ```
     ⏳ Step 4/9 · Sign in to Claude for Jobfeed
     1. Open: <link>
     2. Approve the sign-in (code: <code>)
     3. Reply "done", or paste the code the page shows
     ```

     Paste back any code they give you. If you installed the CLI just now,
     rebuild `SERVICE_PATH`, rewrite the service and restart it (step 3). Then
     run the provider command again.
  3. Do not ask about models. From the list it prints, pick a fast, cheap model
     for Quick and a stronger one for Detailed, save them with
     `"$JF" onboard provider claude_cli --quick <id> --detailed <id>`, and
     report them in the status line ("quick checks with …, careful reviews
     with …").
- **API key (`anthropic_api` or `openai_api`).** If they can open a terminal on
  the server, have them run `"$JF" onboard provider anthropic_api
  --api-key-stdin` there; it prompts without echoing. If they can't, warn ⚠️
  that a key pasted into chat stays in this conversation's history, and use it
  only if they still want to: pass it on stdin to `--api-key-stdin`, never as
  an argument or in a file, and never repeat it. Then continue with
  `--quick`/`--detailed`.

## 5. Résumé and job profile

1. **Résumé** (`next_step: resume`). Ask for a PDF, Word, Markdown or text
   file, or pasted text, and say in the same message that its text is sent to
   the AI provider to suggest a job profile. A file on your machine or attached
   to the chat must be copied to the server first (into
   `$JOBFEED_HOME/jobfeed/data/`); pasted text goes in
   `$JOBFEED_HOME/jobfeed/data/resume-from-chat.md`. `data/` is git-ignored.
   Then `"$JF" onboard resume <file>` (about a minute: send ⏳ first).
2. **Job profile** (`profile_confirmed`). Show the suggested profile as a short
   list and ask what to change:

   ```
   ✅ Step 5/9 · Résumé read. Suggested job profile:
   • Titles: …
   • Level: …
   • Where: …
   • Work authorization: …
   • Excluded: …
   • Postings from the last … days
   Anything to change? (reply "ok" to keep it)
   ```

   Searches are built from `desired_titles`, so add any extra titles there.
   Write the full corrected profile JSON to
   `$JOBFEED_HOME/jobfeed/data/profile-confirmed.json` and run
   `"$JF" onboard profile --confirm <that file>`.

## 6. Searches, schedule and limits

1. **Searches** (`searches`). `"$JF" onboard searches` lists one LinkedIn and
   one Indeed search per title, all off. Ask as a numbered list of titles with
   "all" first, then `"$JF" onboard searches --enable "<title>" ...` (or
   `--enable-all`).
2. **Limits** (`finished`). Run `"$JF" onboard finish --dry-run` and show the
   defaults as a short list: new jobs the AI reads per run, AI calls per day,
   and, for API keys, the most dollars per day. With a subscription, add that it
   uses their plan's allowance, not extra money. Ask "Keep these? (yes)". Apply
   them, or their changes, with
   `"$JF" onboard finish --jobs-per-run N --daily-calls N [--daily-cost USD] --parallel N`.
   Choose `--parallel` from `available_mb` (step 1): `1` under 1500 MB, `2`
   under 3000 MB, otherwise `4`. On a small server, add one line that scans run
   one or two jobs at a time so the server does not run out of memory.
3. **Schedule.** Ask:

   ```
   When should Jobfeed look for new jobs?
   1. Every morning at 8:00 your time (recommended)
   2. Every day at another time
   3. Only when I start a scan myself
   ```

   For 3, set nothing up. In the status line, say how to start a scan: on the
   dashboard's **Scanner runs** page, **Start scan**, or ask any coding agent
   "Run a Jobfeed scan".

`"$JF" onboard status` now reports `next_step: done`.

### Daily scan

For 1 or 2, ask for their city or time zone if you don't know it, and for 2 the
hour. Convert the hour to the server's time zone (`date +%Z`; cloud servers
often run on UTC). Each run scans every enabled source, then evaluates new
jobs. With an API key, say it typically costs $1–2 a day.

**Linux.** `~/.config/systemd/user/jobfeed-daily-scan.service`:

```ini
[Unit]
Description=Jobfeed daily scan and evaluation
After=jobfeed-dashboard.service
Requires=jobfeed-dashboard.service

[Service]
Type=oneshot
WorkingDirectory=JOBFEED_HOME/jobfeed
Environment=JOBFEED_API_URL=http://127.0.0.1:7654
ExecStart=JOBFEED_HOME/jobfeed/.venv/bin/python JOBFEED_HOME/jobfeed/scripts/jobfeed_scheduled_cycle.py
TimeoutStartSec=5h
```

`~/.config/systemd/user/jobfeed-daily-scan.timer`, with the server-time hour:

```ini
[Unit]
Description=Daily Jobfeed scan

[Timer]
OnCalendar=*-*-* 08:00:00
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now jobfeed-daily-scan.timer
```

Do not run `jobfeed-daily-scan.service` by hand.

**macOS.** `~/Library/LaunchAgents/com.jobfeed.daily-scan.plist`, with `Hour`
set to the server-time hour:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobfeed.daily-scan</string>
  <key>WorkingDirectory</key><string>JOBFEED_HOME/jobfeed</string>
  <key>EnvironmentVariables</key>
  <dict><key>JOBFEED_API_URL</key><string>http://127.0.0.1:7654</string></dict>
  <key>ProgramArguments</key>
  <array>
    <string>JOBFEED_HOME/jobfeed/.venv/bin/python</string>
    <string>JOBFEED_HOME/jobfeed/scripts/jobfeed_scheduled_cycle.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>JOBFEED_HOME/jobfeed/data/daily-scan.log</string>
  <key>StandardErrorPath</key><string>JOBFEED_HOME/jobfeed/data/daily-scan.log</string>
</dict>
</plist>
```

```sh
launchctl bootout "gui/$(id -u)/com.jobfeed.daily-scan" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.jobfeed.daily-scan.plist
```


## 7. Extras

Ask once, as one list. The user can pick several or none:

```
✅ Step 6/9 · Searches, schedule and limits saved.

Optional extras (reply with numbers, or "none"):
1. Handshake, signed-in LinkedIn or Jobright: you sign in once on the server's screen
2. Private link: open the dashboard from your phone or laptop
3. Company career pages: I suggest companies that fit your profile
4. Application email: track replies to your applications (confirmations, interviews, rejections) from Gmail. Read-only
5. Apply for me: Claude fills in applications for the jobs you pick, asks you on Discord, and waits for your OK before each Submit
```

When the confirmed profile is a student, new graduate or intern, write 1 as
"Handshake, where most student jobs are, and signed-in LinkedIn (recommended)",
since Handshake shows nothing without a sign-in. Leave out 1 and 5 when the
user cannot see the server's screen (see "Show the user the server's screen" below);
offer the laptop route only if they ask. Set up what they chose in the order
below, and ask only the follow-up questions each one needs.

### Handshake, signed-in LinkedIn, Jobright

These sites show jobs only to signed-in people. Jobfeed reads them through its
Chrome extension, in the server's own Chrome, where the user signs in. Ask
which of the three they want. In the status line, say that Chrome must stay
open (scans skip these sites while it is closed and still read the others) and
that a scan opens a search tab in it for a few minutes. Never ask for, type, or
store their passwords.

**Show the user the server's screen.** They sign in by typing on the server's
own screen, so they must be able to see it and type into it. Use the first that
fits:

- Your tool for the server can show its live screen (a cloud computer's live
  view or "show screen" tool, made for sign-ins, two-step codes and CAPTCHAs):
  open it and send the user its link.
- A Mac mini: macOS Screen Sharing (System Settings › General › Sharing). Tell
  them how to connect from their computer.
- Neither: the user cannot sign in on the server. Say so in one line and skip
  this extra, unless they want the laptop route below.

If you can control the server's desktop (click, type, take screenshots), do
items 1 to 3 yourself. Otherwise tell the user exactly what to click.

1. Install Chrome if it is missing (`google-chrome --version`; on Debian and
   Ubuntu, with their OK, download
   `https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb`
   and `sudo apt-get install -y ./google-chrome-stable_current_amd64.deb`).
2. Load the extension. Open `chrome://extensions`, turn on **Developer mode**
   (top right), click **Load unpacked**, and choose
   `$JOBFEED_HOME/jobfeed/extensions/jobright-source` (in the file dialog,
   press Ctrl+L and type the path). **Jobfeed Jobright Source** appears.
3. Point it at the dashboard. Open the extension's popup (puzzle-piece icon,
   then **Jobfeed Jobright Source**, or `chrome-extension://<id>/popup.html`
   with the ID from `chrome://extensions`). Set the Jobfeed URL to exactly
   `http://localhost:7654` and click **Save and reconnect**. Use `localhost`,
   not `127.0.0.1`: the extension replaces `127.0.0.1:7654` with another
   address. `"$JF" onboard extension status` must show `"connected": true`.
4. Open the sites they chose in that Chrome (`https://app.joinhandshake.com`,
   `https://www.linkedin.com/login`, `https://jobright.ai`), one tab each, and
   send:

   ```
   ⏳ Step 7/9 · Sign in to Handshake on your server
   1. Open your server's screen: <link>
   2. In the Chrome window there, sign in to Handshake, with any code your school sends
   3. Reply "done" when you see your Handshake home page
   ```

   Never ask for, type, or store their password. Leave the tabs open.
5. **Check that the sign-in worked** before you turn a site on. Look at each
   tab yourself (a screenshot, or the tab's address):
   - Handshake is signed in when the address is on `app.joinhandshake.com` but
     not `/login`, and the page shows their name or job search.
   - LinkedIn is signed in on `/feed` or `/jobs`; not on `/login`,
     `/checkpoint` or `/authwall`.
   - Jobright is signed in when it shows their recommended jobs, not its
     sign-in page.

   If a tab is still on a sign-in or code page, send ❌ with what you see ("the
   Handshake tab still asks for your school email") and ask them to finish. If
   you cannot see the screen, ask them what the page shows. Turn on only the
   sites that passed.
6. Turn each signed-in site on: `"$JF" onboard extension enable handshake`
   (`linkedin`, `jobright`). Handshake and LinkedIn search the titles from step
   6; pass `--query "<text>"` (repeatable) for different searches. If a command
   says the extension is not connected, redo item 3.
7. **Open Chrome again after a restart.** Chrome stays signed in, but nothing
   reopens it when the server restarts, and the signed-in sites are skipped
   until it runs. Skip this when the server's desktop already starts Chrome by
   itself: a desktop service that launches it, or an entry for it in
   `~/.config/autostart/` or `/etc/xdg/autostart/`.
   - **Linux desktop.** Find the command Chrome runs with now:
     `ps -eo args= | grep -E '(chrome|chromium)' | grep -v -e '--type=' -e grep | head -1`.
     Keep the program path and, if present, its `--user-data-dir=` and
     `--profile-directory=` options; drop the rest. Write
     `~/.config/autostart/jobfeed-chrome.desktop`:

     ```ini
     [Desktop Entry]
     Type=Application
     Name=Chrome for Jobfeed
     Exec=<program path> <kept options>
     X-GNOME-Autostart-enabled=true
     ```

   - **Mac mini.** Add Chrome to the login items:
     `osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/Google Chrome.app", hidden:true}'`.

   Say in the status line that Chrome opens again by itself after a restart.

The first scan proves the sign-in end to end: `jobfeed doctor` then shows
"Source: Handshake: … found", or ❌ with the reason.

To turn a site off later: `"$JF" onboard extension disable <site>`.

**Signing in again.** Sites sign people out after a while; scans then skip that
site and `jobfeed doctor` shows ❌ "signed out of Handshake". When the user asks
to sign in again: show them the server's screen (above), open the site in the
server's Chrome, have them sign in, check the tab (item 5), and confirm that
`"$JF" onboard extension status` shows `"connected": true`. The next scan
clears the ❌.

**Without a desktop**, only if the user insists: the extension runs in Chrome
on their own computer, which must be on with Chrome open and the SSH tunnel
(step 9) running at every scan. They download
https://github.com/V1ctor2182/jobfeed/archive/refs/heads/main.zip, unzip it,
load `jobfeed-main/jobfeed/extensions/jobright-source` as in item 2, start the
tunnel, set the URL as in item 3, and sign in. You run the check in item 3 and
item 6 on the server.

### Private link

The dashboard asks for an access code when a request comes through a tunnel,
and refuses every tunnel request while the code is off. Requests made on the
server itself never need it. Say in the status line that anyone with both the
link and the code could see their résumé and job list.

1. Create the code:

   ```sh
   cd "$JOBFEED_HOME/jobfeed" && "$JF" remote-access on
   ```

   It prints the code once. Send it in its own message and ask them to save it
   in their password manager or notes. Do not write it anywhere else. Running
   the command again makes a new code and signs out every browser.
2. Install `cloudflared`, Cloudflare's free tunnel program (no account, no
   `sudo`). Linux:

   ```sh
   case "$(uname -m)" in x86_64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; esac
   mkdir -p ~/.local/bin
   curl -fsSL -o ~/.local/bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
   chmod +x ~/.local/bin/cloudflared
   ```

   macOS: `brew install cloudflared`.
3. Run it in the background. Linux, `~/.config/systemd/user/jobfeed-tunnel.service`:

   ```ini
   [Unit]
   Description=Jobfeed private link
   After=jobfeed-dashboard.service network-online.target

   [Service]
   ExecStart=%h/.local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:7654 --metrics 127.0.0.1:7655
   Restart=on-failure
   RestartSec=10

   [Install]
   WantedBy=default.target
   ```

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now jobfeed-tunnel.service
   ```

   macOS: `~/Library/LaunchAgents/com.jobfeed.tunnel.plist` with label
   `com.jobfeed.tunnel`, `RunAtLoad` and `KeepAlive` true, and
   `ProgramArguments` `$(brew --prefix)/bin/cloudflared tunnel --no-autoupdate
   --url http://127.0.0.1:7654 --metrics 127.0.0.1:7655`, loaded with
   `launchctl bootstrap` like the dashboard.
4. About 10 seconds later, `cd "$JOBFEED_HOME/jobfeed" && "$JF" remote-access
   status` prints the link as `tunnel_link`. If it stays `null`, read
   `journalctl --user -u jobfeed-tunnel -n 20`. "network is unreachable" or
   repeated failures mean the server's network blocks Cloudflare (some cloud
   sandboxes allow web traffic only through their own proxy). Then turn the
   tunnel and the code off (below), send ⚠️ that the server's network does not
   allow a private link, and offer the other ways in step 9.

To turn the link off: `systemctl --user disable --now jobfeed-tunnel.service`,
then `cd "$JOBFEED_HOME/jobfeed" && "$JF" remote-access off`.

### Company career pages

`"$JF" onboard companies recommend` makes one model call and lists suggested
companies with their job-board vendor; only rows with a vendor can be tracked.
Show them as a numbered list with "all" first, then
`"$JF" onboard companies add <slug> ...`. For students, also offer the broad
public new-grad and internship list: `"$JF" onboard companies catalog` counts
it, and `--add` tracks all of it.

### Application email

Jobfeed reads Gmail through the Gmail connector of the user's Claude account,
one small `claude -p` call at a time, so the server keeps no Google password or
token. It searches for application mail and saves each message's sender,
subject and a short preview; it reads a whole email only when the user opens
it on the Tracker page. A small AI sorts each one: received, needs action,
interview, rejected, offer. It never sends, deletes or changes email. Say that
in one or two lines.

It needs Claude Code signed in **on the server** with the user's Claude
account. With the Claude subscription from step 4 that is already done. With
an API key or ChatGPT, sign Claude Code in as in step 4 first, or skip this
extra.

1. Have them connect Gmail to that Claude account:

   ```
   ⏳ Step 7/9 · Connect Gmail to your Claude account
   1. On claude.ai, signed in with the same account, open Settings › Connectors
   2. Next to Gmail, click Connect, choose your Google account and allow access
   3. Reply "done"
   ```

2. Check it, with one small read-only search that saves nothing:

   ```sh
   cd "$JOBFEED_HOME/jobfeed" && "$JF" email check
   ```

   Pass: `"connected": true`. Otherwise it prints `reason` and `fix`: send
   them in one plain line each, and run it again after they say "done".
3. Read the last 45 days once (a few minutes; send ⏳ first):
   `cd "$JOBFEED_HOME/jobfeed" && "$JF" email sync`. It prints how many
   messages it saved. This reads email only; it is not a job scan.
4. Check for new mail every 30 minutes. Linux,
   `~/.config/systemd/user/jobfeed-mail-sync.service`, with the same
   `JOBFEED_HOME` and `SERVICE_PATH` as the dashboard service (the sync runs
   `claude`):

   ```ini
   [Unit]
   Description=Jobfeed application email sync (reads Gmail, sends nothing)

   [Service]
   Type=oneshot
   WorkingDirectory=JOBFEED_HOME/jobfeed
   Environment=PATH=SERVICE_PATH
   ExecStart=JOBFEED_HOME/jobfeed/.venv/bin/jobfeed email sync
   TimeoutStartSec=15min
   ```

   `~/.config/systemd/user/jobfeed-mail-sync.timer`:

   ```ini
   [Unit]
   Description=Check for new application email every 30 minutes

   [Timer]
   OnCalendar=*:0/30
   RandomizedDelaySec=5min
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now jobfeed-mail-sync.timer
   ```

   macOS: `~/Library/LaunchAgents/com.jobfeed.mail-sync.plist` with label
   `com.jobfeed.mail-sync`, `WorkingDirectory` `JOBFEED_HOME/jobfeed`,
   `EnvironmentVariables` `PATH` set to `SERVICE_PATH`, `ProgramArguments`
   `JOBFEED_HOME/jobfeed/.venv/bin/jobfeed email sync`, and `StartInterval`
   1800, loaded with `launchctl bootstrap` like the dashboard.
5. Tell them the dashboard's **Tracker** page shows the replies: an inbox, and
   a board of where each application stands.

If Gmail later stops answering (the connector was removed, or the Claude
sign-in expired), `jobfeed doctor` shows ❌ "Application email" with the fix:
reconnect Gmail on claude.ai, then run `jobfeed email check`.

### Applying for them

Claude Appliers fill in application forms in a browser on the server. The user
picks the jobs and presses **Start with Claude** on the dashboard; each job gets
one Applier, which picks one of their PDF résumés, fills the form, and asks them
in Discord when it needs a fact. Nothing is sent to an employer until they
confirm Submit for that job, on the dashboard or by replying `submit` in its
Discord thread. Say that in two or three lines, and that setting it up takes
about 20 minutes with them.

It needs all of these. If one is missing, say which and skip this extra:

- Claude Code signed in **on the server** with their Claude account (step 4,
  or sign it in as there). Appliers run on their Claude plan.
- A server screen they can see and type into, as for Handshake above: they sign
  in to Claude in a browser there. Without one, skip this extra.
- A Discord account. They make a free Discord server and bot in item 8.
- Application email (above) is strongly recommended: Appliers read sign-up
  codes and links from Gmail. Set it up first if they agree.
- About 1.5 GB of memory free (`available_mb` from step 1, read again now). With
  less, say applying will be slow and run one job at a time.

**Rules.** You never press **Start with Claude**, never reply `submit` or
anything else in their Discord, never press Submit, and never run
`pipeline.cli auto-submit on` or `autopilot on`: only the user starts
applications and confirms each Submit. Setting up and testing apply to no job.

Run the commands from `$JOBFEED_HOME/resume-application-pipeline`. On Linux,
read the desktop's display first; every browser command below uses it:

```sh
cd "$JOBFEED_HOME/resume-application-pipeline"
systemctl --user show-environment | grep -E '^(DISPLAY|XAUTHORITY)='
```

If `DISPLAY` is missing there, take it from a process running on the desktop
(`tr '\0' '\n' < /proc/$(pgrep -u "$USER" -n -f 'chrome|xfce|gnome-shell|plasma')/environ | grep -E '^(DISPLAY|XAUTHORITY)='`).

1. **Python environment.** The application programs share Jobfeed's:

   ```sh
   ln -sfn ../jobfeed/.venv .venv
   .venv/bin/python -m pipeline.cli --help >/dev/null && echo pipeline-ok
   ```

2. **Dashboard service.** Add three lines to the `[Service]` section of the
   dashboard service from step 3, then reload and restart it:

   ```ini
   Environment=JOBFEED_API_URL=http://127.0.0.1:7654
   Environment=JOBFEED_APPLIERS=N
   KillMode=process
   ```

   `N` is how many Appliers work at once: `1` under 2500 MB available memory,
   `2` under 5000 MB, otherwise `3`. `KillMode=process` keeps running
   applications alive when the dashboard restarts. On macOS, add
   `JOBFEED_API_URL` and `JOBFEED_APPLIERS` to the plist's
   `EnvironmentVariables` and reload it.
3. **Trust the folder.** Claude Code asks once whether it may work in a folder.
   Answer it for the checkout: start `claude` in tmux in `$JOBFEED_HOME` and
   read the screen after each key. A first interactive run asks setup
   questions first (text style, security notes): press Enter for each. At the
   trust question the selected answer is **No, exit**: press Down until
   **Yes, I trust this folder** is selected, then Enter, then send `/exit`.
   Check:
   `python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude.json')))['projects']['$JOBFEED_HOME'].get('hasTrustDialogAccepted'))"`
   must print `True`.
4. **My Profile.** Appliers fill forms from it. Read it with
   `curl -s http://127.0.0.1:7654/api/my-profile`. `application_resumes` must
   list at least one résumé with `"available": true`: the PDF from step 5. If
   their résumé was Word or pasted text, ask for a PDF and upload it on the
   dashboard's **My Profile** page (or `"$JF" onboard resume <pdf>`). Then fill
   `personal_details` from the résumé and ask only for what it lacks, as one
   list:

   ```
   ⏳ Step 7/9 · Facts that application forms ask for
   • Name: … · Email: … · Phone: …
   • Address (city and state is enough): …
   • Allowed to work in: … · Needs visa sponsorship: …
   • Willing to relocate: … · Can start: …
   • LinkedIn / GitHub / website: …
   Correct, or fill in the blanks?
   ```

   Save the confirmed facts as one JSON object (all thirteen fields of
   `personal_details`, empty strings for unknowns, one line each) with the
   `revision` you read:

   ```sh
   curl -fsS -X PUT http://127.0.0.1:7654/api/my-profile/personal \
     -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:7654' \
     --data @"$JOBFEED_HOME/jobfeed/data/my-profile.json"
   ```

   The file holds `{"personal_details": {...}, "revision": "..."}`. Delete it
   afterwards. Write only what they confirmed; never guess work authorization.
5. **Applying browser.** Appliers work in a Chrome of their own, with its own
   profile, so the user's everyday browser stays out of it. Install Chrome as
   in the Handshake extra if it is missing. Linux,
   `~/.config/systemd/user/jobfeed-apply-chrome.service`, with the display
   values read above (leave out `XAUTHORITY` if there was none):

   ```ini
   [Unit]
   Description=Jobfeed applying browser
   After=graphical-session.target

   [Service]
   Environment=DISPLAY=:0
   Environment=XAUTHORITY=/path/from/above
   ExecStart=/usr/bin/google-chrome --user-data-dir=%h/.config/jobfeed-apply-chrome --remote-debugging-port=9333 --password-store=basic --no-first-run --no-default-browser-check
   Restart=on-failure
   RestartSec=10

   [Install]
   WantedBy=default.target
   ```

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now jobfeed-apply-chrome.service
   curl -fsS http://127.0.0.1:9333/json/version >/dev/null && echo browser-ok
   ```

   macOS: `~/Library/LaunchAgents/com.jobfeed.apply-chrome.plist` with label
   `com.jobfeed.apply-chrome`, `RunAtLoad` and `KeepAlive` true, and
   `ProgramArguments` `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
   with the same four options (`--user-data-dir=$HOME/Library/Application Support/jobfeed-apply-chrome`),
   loaded with `launchctl bootstrap` like the dashboard.
6. **Claude in Chrome.** Appliers drive that browser through Claude's own
   browser extension.
   1. List the browsers already connected to their Claude account (their
      laptop's, for example). Claude in Chrome connects through the account,
      so browsers on other computers show up too. Keep the output:

      ```sh
      claude -p --chrome --model haiku --allowedTools 'mcp__claude-in-chrome__list_connected_browsers' -- "Call list_connected_browsers once and reply with its raw result only."
      ```
   2. Open the extension's page in the applying browser:
      `DISPLAY=… /usr/bin/google-chrome --user-data-dir=$HOME/.config/jobfeed-apply-chrome https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn`
      (it opens in the running window), and send:

      ```
      ⏳ Step 7/9 · Connect Claude to the applying browser
      1. Open your server's screen: <link>
      2. In the Chrome window with the Claude extension page, click Add to Chrome, then Add extension
      3. A Claude log-in page opens by itself: log in with the same Claude account. Skip "Sign in to Chrome"
      4. Reply "done"
      ```

      If you can control the server's desktop, do 2 yourself (maximize the
      window first if the button is off screen); the log-in is theirs.
   3. Run the command from 1 again. The browser that is new in the list, with
      `osPlatform` Linux (or macOS on a Mac mini), is the applying browser.
      Save its deviceId:
      `echo '{"device_id": "<deviceId>"}' > .pipeline/apply-browser.json`.
      If no browser is new, the extension is not signed in yet, or signed in
      to another Claude account: ask them to check, then try again. If more
      than one is new, ask which.

   Every Applier selects that deviceId before it touches a page. Other browsers
   on their Claude account, such as their laptop's, are never used.
7. **Job sites that need an account.** Workday sites make applicants create an
   account. A program on the server does that with one password the user
   chooses, so the Applier never sees it. Linux,
   `~/.config/systemd/user/jobfeed-browser-broker.service`, with the same
   `JOBFEED_HOME` and `SERVICE_PATH` as the dashboard service (it reads
   verification emails with `claude`):

   ```ini
   [Unit]
   Description=Jobfeed job-site account program
   After=jobfeed-dashboard.service jobfeed-apply-chrome.service
   Wants=jobfeed-apply-chrome.service

   [Service]
   WorkingDirectory=JOBFEED_HOME/resume-application-pipeline
   Environment=PATH=SERVICE_PATH
   ExecStart=JOBFEED_HOME/resume-application-pipeline/.venv/bin/python -m pipeline.browser_broker serve --root .
   Restart=on-failure
   RestartSec=60

   [Install]
   WantedBy=default.target
   ```

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now jobfeed-browser-broker.service
   ```

   macOS: a LaunchAgent `com.jobfeed.browser-broker` with the same working
   directory, `PATH` and program, `RunAtLoad` and `KeepAlive` true.
   Then have them set the password on the dashboard, on the server's own
   screen, so it never passes through the chat. Open the page there with
   `DISPLAY=… google-chrome http://127.0.0.1:7654/profile` (their everyday
   Chrome profile, not the applying browser) and send:

   ```
   ⏳ Step 7/9 · Choose a password for job-site accounts
   On your server's screen, on the My Profile page, under Login: type a new password and click Save password.
   At least 8 characters, with a capital letter, a small letter, a digit and a symbol. Save it in your password manager.
   Reply "done"
   ```
8. **Discord.** Appliers ask questions there, and the user can reply `submit`
   there. Walk them through it, one message per part, and wait for "done":

   ```
   ⏳ Step 7/9 · Make a Discord server for your applications
   1. In Discord, click + (Add a Server) on the left, then Create My Own, then For me and my friends
   2. Name it, for example "Job applications", and click Create
   Reply "done"
   ```

   ```
   ⏳ Step 7/9 · Make the bot that writes to you
   1. Open https://discord.com/developers/applications and click New Application. Name it "Jobfeed Appliers", accept and click Create
   2. On the left, click Bot. Under Privileged Gateway Intents, turn on Message Content Intent, then Save Changes
   3. On the same page, click Reset Token, confirm, and Copy. Keep it to yourself for now
   Reply "done"
   ```

   Then the token. If they can type on the server's screen, open a terminal
   window there running the setup, so the token goes straight to the server.
   Linux, with `JOBFEED_HOME` replaced by the absolute checkout path:

   ```sh
   DISPLAY=… x-terminal-emulator -e bash -c 'cd JOBFEED_HOME/resume-application-pipeline && .venv/bin/python -m pipeline.discord_setup; read -rp "Press Enter to close this window."' &
   ```

   (macOS: `open -a Terminal` and type the same `cd … && …` line there.) Send:

   ```
   ⏳ Step 7/9 · Give the bot's token to your server
   On your server's screen, a black window asks for the token. Click it, paste the token (Ctrl+Shift+V) and press Enter.
   Nothing shows while you paste; that is normal. Reply "done"
   ```
 Otherwise warn ⚠️ that a token pasted
   into chat stays in this conversation's history, and use it only if they
   still want to: pass it to `.venv/bin/python -m pipeline.discord_setup` on
   stdin, never as an argument or in a file, and never repeat it.

   It prints one JSON result and also leaves it, without the token, in
   `~/.config/jobfeed-applier/discord-setup.json`; read that file when it ran
   in their terminal window. The first time, expect
   `"problem": "not_in_server"`: the bot is not in their server yet. Send its
   `invite_url`:

   ```
   ⏳ Step 7/9 · Add the bot to your server
   1. Open: <invite_url>
   2. Choose your applications server and click Authorize
   Reply "done"
   ```

   Then run `.venv/bin/python -m pipeline.discord_setup --again`, which reuses
   the token Discord already accepted, so they never paste it twice. Each
   other `problem` comes with a `fix`: send it in one plain line, then run
   `--again` (after `bad_token`, they paste a new token as before). When it
   passes, a "Jobfeed Appliers are connected" message and a test thread appear
   in their server. Only the server's owner, which is them, can answer the
   Appliers.
9. **Check.** Read-only; it starts nothing and posts nothing:

   ```sh
   .venv/bin/python -m pipeline.applier_check
   ```

   `"ready": true` passes. Fix each check with `"ok": false` from its `fix`.

In the status line, say how they apply: open a job they like, **Add to
application queue**, then **Runs › Application queue**, select it and **Start
with Claude**. Questions arrive in Discord; each Submit waits for them.

To turn applying off later: `systemctl --user disable --now
jobfeed-apply-chrome.service jobfeed-browser-broker.service`.

## 8. Test the setup

Do not hand over until the test passes. Send "⏳ Step 8/9 · Testing
everything." first.

1. **Restart test.** Restart the service and wait for it, so you know it comes
   back on its own:

   ```sh
   systemctl --user restart jobfeed-dashboard.service   # macOS: launchctl kickstart -k "gui/$(id -u)/com.jobfeed.dashboard"
   for i in $(seq 30); do curl -fsS http://127.0.0.1:7654/api/health >/dev/null 2>&1 && break; sleep 1; done
   ```

2. **Doctor.** Run the full check. It is read-only and starts no scan:

   ```sh
   cd "$JOBFEED_HOME/jobfeed" && "$JF" doctor --json
   ```

   It checks the dashboard, the background service (running, starts after a
   reboot, keeps running after logout), every setup step, the AI provider and
   models, the résumé, the job sources, the scan API, one tiny AI request made
   with the service's own `PATH`, the last scan and evaluation, the daily scan
   (scheduled, last run not failed), every job source that is on (Indeed,
   LinkedIn, company pages, Handshake…: jobs found in its latest scan; a
   failure or a sign-out is ❌, three empty scans in a row are ⚠️), the Chrome
   extension when a signed-in site is on, application email (the last Gmail
   read worked and ran on schedule), the private link (must answer 401 without
   the code), free disk, and
   that the database, résumé and settings stay out of git. Each check is `ok`,
   `warn`, `fail` or `off` (not set up), with a `fix` hint. It exits 1 when any
   check fails.

   Fix every `fail` and run it again. Right after the restart, the Chrome
   extension needs up to 30 seconds to reconnect, so if only that check fails,
   wait and rerun. If something cannot be fixed, say so in the hand-over with
   what it means for them. Pass on each `warn` in one plain sentence.
3. **Applying** (only with that extra). Run
   `cd "$JOBFEED_HOME/resume-application-pipeline" && .venv/bin/python -m pipeline.applier_check`
   and fix each check with `"ok": false`. Report it as one line, "✅ Applying:
   profile, résumé, browser, Discord ready".

Report the result as one message, one line per check that is not `off`, in
plain words, and offer the first scan in the same message:

```
✅ Step 8/9 · All checks passed
✅ Keeps running and starts again by itself
✅ Setup complete: Claude (quick: …, detailed: …), résumé, 6 searches
✅ AI answers from the background service
✅ Daily scan: next run Fri 08:00 (Shanghai time)
✅ 21 GB free disk
✅ Your résumé and data stay private

Run a first scan now? It proves every job site works and gives you ranked jobs
today. About 10–30 minutes.
1. Yes (recommended)
2. No, later
```

With an API key, add the cost to the question (typically $1–2). With "no",
go to step 9.

4. **First scan.** With their yes, send "⏳ Step 8/9 · First scan running,
   about 10–30 minutes." and run one scan of every enabled site, then the AI
   ranking of the new jobs, the same way the daily scan does. It can outlast
   your command time limit, so run it in tmux and check every few minutes:

   ```sh
   tmux new -d -s jfscan "cd '$JOBFEED_HOME/jobfeed' && JOBFEED_API_URL=http://127.0.0.1:7654 .venv/bin/python scripts/jobfeed_scheduled_cycle.py; echo exit=\$? > data/first-scan.exit"
   tmux has-session -t jfscan 2>/dev/null && echo running || cat "$JOBFEED_HOME/jobfeed/data/first-scan.exit"
   ```

   When it finishes (`exit=0`; the log is `data/scheduler/cycle.log`), run
   `"$JF" doctor --json` again. Its "Source: …" lines now say how many jobs
   each site found. Fix each ❌ that setup can fix (a signed-out site: sign in
   again as in step 7), then report one line per site and the number of jobs
   ranked:

   ```
   ✅ First scan: 212 new jobs, 38 ranked as a good fit
   ✅ LinkedIn 120 · Indeed 74 · Handshake 18
   ```

## 9. Hand-over

Choose how the user opens the dashboard, the first that fits:

- **The server has a desktop the user can see** (a cloud computer with a
  desktop, a Mac mini): `http://127.0.0.1:7654` in a browser there. If you can
  control that desktop, open it there for them.
- **Private link** (step 7): the link and the code.
- **They log in to the server with SSH:** on their own computer they open
  Terminal (Mac: Cmd+Space, type "Terminal"; Windows: "PowerShell"), paste the
  line below with the real user and address, and leave the window open while
  they use the dashboard. It is normal that the window shows nothing:

  ```sh
  ssh -N -L 7654:127.0.0.1:7654 <user>@<server>
  ```

  Then they open `http://127.0.0.1:7654`.
- **They already use Tailscale on both machines:** turn on the access code
  (`remote-access on`), run `tailscale serve --bg 7654` on the server, and give
  them the address it prints and the code.

Then send the hand-over in this shape, with only the lines that apply:

```
**✅ Jobfeed is ready**

Open it: <how, in one or two lines>
Finds jobs: every day at 08:00 (<their time zone>) · LinkedIn, Indeed, <n> companies, Handshake
AI: Claude subscription · up to <N> new jobs per run
Replies: Gmail checked every 30 minutes, on the Tracker page
Applies: when you press Start with Claude · questions on Discord · every Submit waits for you
Tests: all passed

Next: <one line, below>

Later, ask any coding agent:
• "Check my Jobfeed"
• "Update Jobfeed"
• "Change my Jobfeed searches"
• "What's my Jobfeed link?"
```

When Handshake, LinkedIn or Jobright is on, add:

```
⚠️ Keep Chrome open on the server (it opens again by itself after a restart). If
Handshake signs you out, scans skip it; ask any coding agent "sign me in to
Handshake again".
```

For "Next": after a first scan, "open the dashboard and look at today's ranked
jobs"; otherwise "open the dashboard, go to **Scanner runs** and press **Start
scan**", adding "or wait for the daily scan" when there is one. With scans only
on request, write "Finds jobs: when you start a scan · …" in the "Finds jobs"
line.

Leave out the "Replies" line when application email is off, and the
"Applies" line when applying is off; then add "Not included: applying to jobs".
With applying on, add one line on how to start: open a job they like, **Add to
application queue**, then **Runs › Application queue**, select it and **Start
with Claude**. With a private link, add a line that the link changes when the
server restarts. Without applying, the dashboard has no Review or Answer Bank
pages, and its Tracker page stays empty without application email; don't
mention them unless asked.

Finish with one short **Technical details** block for whoever helps them later:
the checkout path, the service names, and these commands (Linux):

```sh
# check that everything works (read-only)
cd "$JOBFEED_HOME/jobfeed" && .venv/bin/jobfeed doctor
# update
git -C "$JOBFEED_HOME" pull --ff-only && (cd "$JOBFEED_HOME/jobfeed" && ~/.local/bin/uv sync --locked --no-dev --python 3.12) && systemctl --user restart jobfeed-dashboard
# stop the background services (keeps all data)
systemctl --user disable --now jobfeed-dashboard.service jobfeed-daily-scan.timer jobfeed-tunnel.service jobfeed-mail-sync.timer
# signed out of Handshake or LinkedIn: see "Signing in again" in docs/server-install.md
# application email: check Gmail, or read new mail now
cd "$JOBFEED_HOME/jobfeed" && .venv/bin/jobfeed email check
cd "$JOBFEED_HOME/jobfeed" && .venv/bin/jobfeed email sync
# current private link
cd "$JOBFEED_HOME/jobfeed" && .venv/bin/jobfeed remote-access status
# applying: check it (read-only), or reconnect Discord
cd "$JOBFEED_HOME/resume-application-pipeline" && .venv/bin/python -m pipeline.applier_check
cd "$JOBFEED_HOME/resume-application-pipeline" && .venv/bin/python -m pipeline.discord_setup
```

With applying on, add `jobfeed-apply-chrome.service jobfeed-browser-broker.service`
to the stop line and `jobfeed-browser-broker` to the restart in the update line.

If the Chrome extension is installed, add: after an update, click the reload
arrow on **Jobfeed Jobright Source** in `chrome://extensions`. On macOS, replace
the `systemctl` parts with `launchctl kickstart -k
"gui/$(id -u)/com.jobfeed.dashboard"` to restart and `launchctl bootout
"gui/$(id -u)/com.jobfeed.dashboard"` (and `com.jobfeed.daily-scan`,
`com.jobfeed.tunnel`) to stop.
