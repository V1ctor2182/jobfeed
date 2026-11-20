# Install Jobfeed on your own server

This page is for a coding agent (Claude Code, Codex, a hosted agent, or similar)
that a user asked to install Jobfeed on their own always-on machine: a Linux
VPS or cloud instance, or a Mac mini. Follow it in order. A person can follow it
too.

It installs **Jobfeed** as a background service: it scans job sites, filters
and ranks the jobs against the user's résumé with AI, and shows them on a
dashboard. It does not apply to jobs or read application email; that is the
separate Claude Applier setup in the main README.

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
• Optional extras: a daily scan, Handshake and signed-in LinkedIn, a private link for your phone
• Not included: applying to jobs and reading application email

What I need from you (about 10 minutes)
1. Your résumé: a PDF, Word file or pasted text
2. A Claude or ChatGPT sign-in, which you approve in your browser
3. A few choices, each one a number

Steps
1 Check server ✅ · 2 Install · 3 Background service · 4 AI · 5 Résumé and profile
6 Searches and limits · 7 Extras · 8 Test · 9 Hand-over

Server: ✅ <memory> memory, <disk> free disk, internet OK
Start the install? (yes)
```

Then post one status line per step as you finish it.

## Rules for the agent

- Run every command yourself. When the user must act (open a link, approve a
  sign-in), tell them exactly what to click and what they will see.
- Never print, log, or commit API keys or tokens.
- Do not start a scan or an evaluation. Scans start only after setup is
  finished and the user asks for one.
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

## 6. Searches and limits

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

`"$JF" onboard status` now reports `next_step: done`.

## 7. Extras

Ask once, as one list. The user can pick several or none:

```
✅ Step 6/9 · Searches and limits saved.

Optional extras (reply with numbers, or "none"):
1. Daily scan: look for new jobs every morning (recommended)
2. Handshake, signed-in LinkedIn or Jobright: you sign in once on the server's screen
3. Private link: open the dashboard from your phone or laptop
4. Company career pages: I suggest companies that fit your profile
```

Leave out 2 when the server has no desktop the user can see (it would need
their own computer to stay on; offer it only if they ask). Set up what they
chose in the order below, and ask only the follow-up questions each one needs.

### Daily scan

Ask for their city or time zone, and the hour (suggest 8:00). Convert the hour
to the server's time zone (`date +%Z`; cloud servers often run on UTC). Each
run scans every enabled source, then evaluates new jobs. With an API key, say
it typically costs $1–2 a day.

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

### Handshake, signed-in LinkedIn, Jobright

These sites show jobs only to signed-in people. Jobfeed reads them through its
Chrome extension, in the server's own Chrome, where the user signs in. Ask
which of the three they want. In the status line, say that Chrome must stay
open (scans skip these sites while it is closed and still read the others) and
that a scan opens a search tab in it for a few minutes. Never ask for, type, or
store their passwords.

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
   `https://www.linkedin.com/login`, `https://jobright.ai`) and ask them to
   sign in on the server's screen, including any code their school or LinkedIn
   sends, and to reply "done" when they see their home page. Leave those tabs
   open.
5. Turn each chosen site on: `"$JF" onboard extension enable handshake`
   (`linkedin`, `jobright`). Handshake and LinkedIn search the titles from step
   6; pass `--query "<text>"` (repeatable) for different searches. If a command
   says the extension is not connected, redo item 3.

To turn a site off later: `"$JF" onboard extension disable <site>`.

**Without a desktop**, only if the user insists: the extension runs in Chrome
on their own computer, which must be on with Chrome open and the SSH tunnel
(step 9) running at every scan. They download
https://github.com/V1ctor2182/jobfeed/archive/refs/heads/main.zip, unzip it,
load `jobfeed-main/jobfeed/extensions/jobright-source` as in item 2, start the
tunnel, set the URL as in item 3, and sign in. You run the check in item 3 and
item 5 on the server.

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

## 8. Test the setup

Do not hand over until every check below passes. Send "⏳ Step 8/9 · Testing
everything." first. Fix each failure and run its check again. If something
cannot be fixed, say so in the hand-over with what it means for them.

1. **Survives a restart.** Restart the service and wait for it:

   ```sh
   systemctl --user restart jobfeed-dashboard.service   # macOS: launchctl kickstart -k "gui/$(id -u)/com.jobfeed.dashboard"
   for i in $(seq 30); do curl -fsS http://127.0.0.1:7654/api/health >/dev/null 2>&1 && break; sleep 1; done
   curl -fsS http://127.0.0.1:7654/api/health
   systemctl --user is-enabled jobfeed-dashboard.service
   loginctl show-user "$USER" -p Linger
   ```

   Pass: health is `ok`, the service is `enabled`, and `Linger=yes`.
2. **Setup is complete.** `"$JF" onboard check` tests the dashboard, every setup
   step, the saved AI provider and models, the résumé, the job sources, the scan
   API and, when a signed-in site is on, the Chrome extension. It exits 1 if any
   check fails. Right after the restart the extension needs up to 30 seconds to
   reconnect, so run it again once if only that check fails.
3. **The AI works from the service.** With a subscription, run one tiny request
   with the service's own `PATH`, so a missing CLI or an expired sign-in shows
   up now and not at the first scan:

   ```sh
   SERVICE_PATH="$(systemctl --user show jobfeed-dashboard.service -p Environment | sed -n 's/.*PATH=\([^ ]*\).*/\1/p')"
   env -i HOME="$HOME" PATH="$SERVICE_PATH" claude -p "Reply with OK" --model haiku   # codex: codex exec "Reply with OK"
   ```

   Pass: it prints OK. On macOS, use the `SERVICE_PATH` from the plist.
4. **The daily scan can run** (if it is on):

   ```sh
   cd "$JOBFEED_HOME/jobfeed" && JOBFEED_API_URL=http://127.0.0.1:7654 .venv/bin/python scripts/jobfeed_scheduled_cycle.py --check
   systemctl --user list-timers jobfeed-daily-scan.timer
   ```

   Pass: the first prints JSON with `"active"` and starts nothing, and the timer
   shows the next run at the right hour.
5. **The private link asks for the code** (if it is on): with the link from
   `remote-access status`,
   `curl -s -o /dev/null -w "%{http_code}\n" "<link>/api/health"` prints `401`.
   If it prints `200`, stop the tunnel at once and tell the user.
6. **Nothing private is tracked:**
   `git -C "$JOBFEED_HOME" status --short` prints nothing, and
   `git -C "$JOBFEED_HOME" check-ignore -q jobfeed/data/jobfeed.sqlite && echo ignored`
   prints `ignored`.

Report the results as one message:

```
✅ Step 8/9 · All 6 checks passed
✅ Keeps running and restarts by itself
✅ Setup complete: Claude (quick: …, detailed: …), résumé, 6 searches
✅ AI answers from the background service
✅ Daily scan ready, next run Fri 08:00 (Shanghai time)
➖ Private link: not set up
✅ Your résumé and data stay private
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
Not included: applying to jobs and reading application email
Tests: 6/6 passed

Next: open the dashboard, go to **Scanner runs** and press **Start scan**, or wait for the daily scan.

Later, ask any coding agent:
• "Update Jobfeed"
• "Change my Jobfeed searches"
• "What's my Jobfeed link?"
```

Add a ⚠️ line when Chrome must stay open for Handshake or LinkedIn, and a line
that the private link changes when the server restarts. The dashboard has no
Review, Tracker or Answer Bank pages on this install; don't mention them unless
asked.

Finish with one short **Technical details** block for whoever helps them later:
the checkout path, the service names, and these commands (Linux):

```sh
# update
git -C "$JOBFEED_HOME" pull --ff-only && (cd "$JOBFEED_HOME/jobfeed" && ~/.local/bin/uv sync --locked --no-dev --python 3.12) && systemctl --user restart jobfeed-dashboard
# stop the background services (keeps all data)
systemctl --user disable --now jobfeed-dashboard.service jobfeed-daily-scan.timer jobfeed-tunnel.service
# current private link
cd "$JOBFEED_HOME/jobfeed" && .venv/bin/jobfeed remote-access status
```

If the Chrome extension is installed, add: after an update, click the reload
arrow on **Jobfeed Jobright Source** in `chrome://extensions`. On macOS, replace
the `systemctl` parts with `launchctl kickstart -k
"gui/$(id -u)/com.jobfeed.dashboard"` to restart and `launchctl bootout
"gui/$(id -u)/com.jobfeed.dashboard"` (and `com.jobfeed.daily-scan`,
`com.jobfeed.tunnel`) to stop.
