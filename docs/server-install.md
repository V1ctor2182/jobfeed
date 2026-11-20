# Install Jobfeed on your own server

This page is written for a coding agent (Claude Code, Codex, a hosted agent, or
similar) that a user asked to install Jobfeed on their own always-on machine: a
Linux VPS or cloud instance, or a Mac mini. Follow it in order. A person can
follow it too.

The user only talks to you, in this chat. Assume they have no terminal on the
server: everything that needs their input (a choice, a sign-in link, a code,
their résumé) goes through you.

It installs **Jobfeed** (scan, filter, score, dashboard) as a background service
and an optional daily scan. It does not install the Claude Applier; that needs a
desktop browser and Discord and is covered in the main README.

## Rules for the agent

- Assume the user is not technical. Talk in plain words: say "keep running in
  the background" and "start automatically after a restart", not "systemd unit"
  or "linger". Ask one question at a time, and always offer a sensible default
  they can accept with "yes".
- Run every command yourself. Never ask the user to type a command when you can
  run it; when they must act (open a link, approve a sign-in), tell them exactly
  what to click and what they will see.
- When something fails, fix it yourself if you can. Tell the user only what it
  means for them, in one sentence, not the error output.
- Tell the user your plan in a few lines before you change anything.
- Never print, log, or commit API keys or tokens.
- Do not start a scan or an evaluation. Scans start only after the user finishes
  setup (step 5) and asks for one.
- Jobfeed's dashboard has no login. It refuses to bind anything except loopback.
  Do not put it behind a public port, reverse proxy, or `0.0.0.0` forward.
- Use `sudo` only for the steps that say so, and ask the user first, in plain
  words ("I need administrator rights to … — OK?"). If `sudo` asks for a
  password, explain that it is the password they use to log in to the server,
  run the command in tmux, and have them send it to you only if they cannot
  type it into the server themselves.
- Every step is safe to run again. If a step fails, fix the cause and rerun it;
  do not delete the checkout or `jobfeed/data/` to start over.

## 0. Find the target machine

Work out where you are running and where Jobfeed should go:

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

Check `uname -s`, `hostname` and `whoami` on the server and tell the user which
machine you are about to change.

Some steps need an interactive terminal (a sign-in that waits for a code). If
your way of running commands is not interactive, run those in `tmux` on the
server (install it if missing): `tmux new -d -s jf '<command>'`, read the output
with `tmux capture-pane -p -t jf`, and type into it with
`tmux send-keys -t jf '<text>' Enter`.

### Check that the server can run Jobfeed

Do this before you change anything. Jobfeed needs:

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
uname -sm
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
```

Then tell the user the result in one or two plain sentences:

- **Everything meets the recommended column:** say the server is big enough and
  go on.
- **32-bit processor, no systemd on Linux, under 1 GB of memory, or under 4 GB
  of free disk:** stop. Say what is short, by how much, and what would fix it,
  for example: "Your server has 512 MB of memory; Jobfeed needs at least 1 GB,
  and 2 GB is better. Most cloud providers offer a 2 GB plan for a few dollars a
  month." Do not install.
- **1–2 GB of memory and less than 1 GB of swap (Linux):** explain that the
  server is small and that you can let it borrow 2 GB of disk as extra memory.
  With their OK:

  ```sh
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  ```

  If `swapon` fails (some containers forbid it), say Jobfeed may be slow or stop
  during big scans, and continue only if they accept that.
- **Plenty of total memory but under 700 MB available:** other programs are
  using it. Mention that scans may be slow and continue.

Remember `available_mb` for step 5: each model call during a scan runs its own
CLI process of about 250 MB, so small servers must run fewer at once.
- **A site is unreachable:** say the server cannot reach the internet sites
  Jobfeed downloads from, and ask whether it is behind a firewall or proxy.

If Git, `curl` or `tmux` is missing, install it with the system's package
manager after asking (`sudo apt-get install -y git curl tmux` on Debian and
Ubuntu; `brew install git tmux` on macOS). Python is not required up front:
setup installs `uv` and a private Python 3.12.

Use `~/jobfeed` for the checkout without asking. Ask the user only this before
installing:

- Whether Jobfeed should look for new jobs automatically once a day (step 4),
  and at what time in *their* time zone. Suggest yes, at 8 am. Ask which city or
  time zone they are in, and convert the hour to the server's time zone
  (`date +%Z`; cloud servers often run on UTC).

Model provider, résumé, searches, and companies come later, in step 5, where
you walk the user through setup in chat.

## 1. Get the code

```sh
JOBFEED_HOME="${JOBFEED_HOME:-$HOME/jobfeed}"
if [ -d "$JOBFEED_HOME/.git" ]; then
    git -C "$JOBFEED_HOME" pull --ff-only
else
    git clone https://github.com/V1ctor2182/jobfeed.git "$JOBFEED_HOME"
fi
```

If `git pull` refuses because of local changes, stop and show the user
`git -C "$JOBFEED_HOME" status --short`. Do not discard their changes.

## 2. Install the runtime

```sh
cd "$JOBFEED_HOME/jobfeed"
JOBFEED_SETUP_NO_OPEN=1 ./setup.sh
```

This installs `uv` if missing, builds `jobfeed/.venv`, creates
`jobfeed/data/jobfeed.sqlite`, and starts a temporary server on
`127.0.0.1:7654`. Check it:

```sh
curl -fsS http://127.0.0.1:7654/api/health
```

Then stop the temporary server so the service in step 3 owns the port:

```sh
kill "$(cat "$JOBFEED_HOME/jobfeed/data/jobfeed-serve.pid")" 2>/dev/null || true
rm -f "$JOBFEED_HOME/jobfeed/data/jobfeed-serve.pid"
```

## 3. Run the dashboard as a service

Services start with a minimal `PATH`. Jobfeed can use a signed-in `claude` or
`codex` CLI as its model provider, so the service must be able to find them.
Build `SERVICE_PATH` from the directories that actually hold them, followed by
the system defaults:

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

### Check

Wait up to 30 seconds for `curl -fsS http://127.0.0.1:7654/api/health` to
succeed. If it does not, read the log (`journalctl --user -u
jobfeed-dashboard -n 50` on Linux, `jobfeed/data/jobfeed-serve.log` on macOS)
and fix the cause.

## 4. Optional: daily scan

Only if the user asked for it. It calls the running dashboard to scan every
enabled source, then evaluates new jobs. Evaluation spends model credits,
typically $1–2 a day.

Write the files below now, but **turn the schedule on only at the end of step
5**, after `onboard finish` succeeds. A schedule that is on too early can fire
in the middle of setup, for example when a sleeping server wakes after the
scan hour and catches up on the missed run.

### Linux

`~/.config/systemd/user/jobfeed-daily-scan.service`:

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

`~/.config/systemd/user/jobfeed-daily-scan.timer` (replace `08:00` with the
user's hour):

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
```

Do not enable the timer yet, and do not run `jobfeed-daily-scan.service` by
hand.

### macOS

`~/Library/LaunchAgents/com.jobfeed.daily-scan.plist`, with `Hour` set to the
user's hour:

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

Do not load it with `launchctl` yet.

## 5. Set up Jobfeed with the user, in chat

The dashboard has a setup screen, but on a server the user would need a tunnel
first. Instead, walk them through the same steps here with `jobfeed onboard`,
which calls the running dashboard and prints JSON. Use the full path:

```sh
JF="$JOBFEED_HOME/jobfeed/.venv/bin/jobfeed"
"$JF" onboard status
```

`next_step` says where to continue; setup can stop and resume at any point.
Keep each message to the user short and in plain words; never paste raw JSON at
them. Every choice below is the user's. Suggest, then wait for their answer.

1. **Model provider** (`next_step: provider`). Recommend the signed-in CLI you
   are running in: `"$JF" onboard provider claude_cli` (or `codex_cli`). It uses
   the user's existing subscription and needs no API key. The CLI must be
   installed and signed in **on the server**, as the same user the service runs
   as. If you run elsewhere, that is a separate sign-in from yours.
   If `connected` is false, show `detail`, then sign that CLI in on the server
   for the user: install it if missing, start `claude auth login` (or
   `codex login --device-auth`) in tmux, send the user the link (and code) it
   prints, wait for them to approve it in their browser, and paste back any code
   they give you. If you installed the CLI just now, rebuild `SERVICE_PATH`
   (step 3), rewrite the dashboard service with it and restart the service, so
   the dashboard can find the CLI. Then run the provider command again.
   Do not quiz the user about models. From the list it prints, pick a fast,
   cheap model for Quick and a stronger one for Detailed, tell them in one line
   ("quick checks with …, careful reviews with …"), and save it unless they
   object:
   `"$JF" onboard provider claude_cli --quick <id> --detailed <id>`.
   If they prefer an API key: if they can open a terminal on the server, have
   them run `"$JF" onboard provider anthropic_api --api-key-stdin` (or
   `openai_api`) there; it prompts without echoing. If they can't, warn that a
   key pasted into chat stays in this conversation's history, and use it only if
   they still want to: pass it on stdin to `--api-key-stdin`, never as an
   argument or in a file, and never repeat it. Then continue with
   `--quick`/`--detailed`.
2. **Résumé** (`resume`). Ask for the résumé: a PDF, DOCX, Markdown or text
   file, or text pasted into chat. A file on your machine or attached to the
   chat must be copied to the server first (into `$JOBFEED_HOME/jobfeed/data/`);
   pasted text goes in `$JOBFEED_HOME/jobfeed/data/resume-from-chat.md`.
   `data/` is git-ignored.
   Before uploading, tell them the résumé text is sent to the provider they
   chose to suggest a job profile, and get their OK. Then
   `"$JF" onboard resume <file>`.
3. **Job profile** (`profile_confirmed`). Summarize the suggested profile in a
   few lines: target titles, seniority, countries and locations, work modes,
   work authorization, exclusions, and how recent postings must be. Ask what to
   change. Searches are built from `desired_titles`, so add any extra titles
   there. Write the full corrected profile JSON to
   `$JOBFEED_HOME/jobfeed/data/profile-confirmed.json` and run
   `"$JF" onboard profile --confirm <that file>`.
4. **Searches** (`searches`). `"$JF" onboard searches` lists one LinkedIn and
   one Indeed search per title, all off. Ask which titles to search, then
   `"$JF" onboard searches --enable "<title>" ...` (or `--enable-all`).
5. **Companies** (optional; not a `next_step`). Offer to also scan company
   career pages. `"$JF" onboard companies recommend` makes one model call and
   lists suggestions with their job-board vendor; only rows with a vendor can be
   tracked. Add the ones the user picks with
   `"$JF" onboard companies add <slug> ...`. `companies catalog` counts a broad
   public new-grad and internship list; `companies catalog --add` tracks all of
   it. Skipping is fine.
6. **Limits and finish** (`finished`). Run `"$JF" onboard finish --dry-run`.
   Explain the defaults in one or two plain sentences: how many new jobs the AI
   reads per run, and the most AI calls (and, for API keys, dollars) it may use
   per day. With a CLI provider, say that this uses their subscription's usage
   allowance, not extra money. Suggest keeping the defaults, and apply them, or
   their changes, with
   `"$JF" onboard finish --jobs-per-run N --daily-calls N [--daily-cost USD] --parallel N`.
   Choose `--parallel` from the `available_mb` you measured before installing:
   `1` under 1500 MB, `2` under 3000 MB, otherwise `4`. On a small server, tell
   the user in one sentence that scans run one or two jobs at a time so the
   server does not run out of memory.
7. **Signed-in job sites** (optional; see below). Ask before you turn on the
   daily scan, so its first run already includes them.
8. **Turn on the daily scan** (only if the user wanted it in step 0).
   Linux:

   ```sh
   systemctl --user enable --now jobfeed-daily-scan.timer
   systemctl --user list-timers jobfeed-daily-scan.timer
   ```

   macOS:

   ```sh
   launchctl bootout "gui/$(id -u)/com.jobfeed.daily-scan" 2>/dev/null || true
   launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.jobfeed.daily-scan.plist
   ```

   Tell the user the time it will run, in their time zone.

`"$JF" onboard status` should now report `next_step: done`. Do not start a scan
unless the user asks for one; their first scan can also be started from the
dashboard's **Runs** page.

If a command fails, its message names the missing step. Fix that step and
continue; do not edit `config.toml` or the files in `data/` by hand.

### Signed-in job sites: Handshake, LinkedIn, Jobright

Handshake, and the full LinkedIn and Jobright listings, show jobs only to
signed-in people. Jobfeed reads them through its Chrome extension, in a Chrome
where the user is signed in. Ask, in plain words, for example: "Do you use
Handshake, or want me to also read LinkedIn while you're signed in? It finds
more jobs than the public search. You'd sign in once, on the server's screen."
If they say no, skip this section and tell them they can add it later.

Two things must stay true afterwards, so say them in one sentence each:

- That Chrome must stay open. When it is closed, scans skip these sites and
  still read the others.
- A scan opens a search tab in that Chrome for a few minutes.

Never ask for, type, or store their passwords. They sign in themselves.

**Which Chrome.** Use the server's own Chrome if the server has a desktop the
user can see (a cloud computer with a desktop, a Mac mini). That Chrome stays
on with the server. If the server has no desktop, explain that the extension
would have to run in Chrome on their own computer, which would have to be on,
with Chrome open and the SSH tunnel from step 6 running, at every scan. Suggest
skipping, and continue only if they still want it (see "No desktop" below).

**On the server's desktop.** If you can control the server's desktop (click,
type, take screenshots), do steps 1 and 2 yourself. Otherwise tell the user
exactly what to click.

1. Install Chrome if it is missing (`google-chrome --version`; on Debian and
   Ubuntu, with their OK, download
   `https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb`
   and `sudo apt-get install -y ./google-chrome-stable_current_amd64.deb`).
2. Load the extension. In Chrome, open `chrome://extensions`, turn on
   **Developer mode** (top right), click **Load unpacked**, and choose the folder
   `$JOBFEED_HOME/jobfeed/extensions/jobright-source` (in the file dialog, press
   Ctrl+L and type the path). **Jobfeed Jobright Source** appears in the list.
3. Point it at the dashboard. Click the puzzle-piece icon in Chrome's toolbar,
   then **Jobfeed Jobright Source**. Set the Jobfeed URL to exactly
   `http://localhost:7654` and click **Save**. Use `localhost`, not
   `127.0.0.1`: the extension replaces `127.0.0.1:7654` with a different
   address. The popup should say **Connected to Jobfeed**. If the popup is hard
   to reach, open `chrome-extension://<id>/popup.html` in a tab instead, with
   the ID shown on `chrome://extensions`.
4. Check: `"$JF" onboard extension status` shows `"connected": true`.
5. Have the user sign in. Open the sites they chose in that Chrome:
   `https://app.joinhandshake.com`, `https://www.linkedin.com/login`,
   `https://jobright.ai`. Ask them to sign in on the server's screen, including
   any code their school or LinkedIn sends, and to tell you when they see their
   home page. Leave those tabs open.
6. Turn the sites on, one command each:

   ```sh
   "$JF" onboard extension enable handshake
   "$JF" onboard extension enable linkedin
   "$JF" onboard extension enable jobright
   ```

   Handshake and LinkedIn search for the titles they already chose. Say so in
   one line, and pass `--query "<text>"` (repeatable) if they want different
   searches. If a command says the extension is not connected, redo step 3.

**No desktop.** On the user's own computer:

1. Download https://github.com/V1ctor2182/jobfeed/archive/refs/heads/main.zip
   and unzip it.
2. Load the folder `jobfeed-main/jobfeed/extensions/jobright-source` as in
   step 2 above.
3. Start the SSH tunnel from step 6, then set the extension's URL to
   `http://localhost:7654` as in step 3.
4. Sign in to the sites in that Chrome.
5. Run steps 4 and 6 above from the server.

Tell them that daily scans include these sites only while their computer is on
with Chrome open and the tunnel running.

To turn a site off later: `"$JF" onboard extension disable <site>`.

## 6. Verify and hand over

Run these and report each result to the user:

```sh
curl -fsS http://127.0.0.1:7654/api/health
"$JOBFEED_HOME/jobfeed/.venv/bin/jobfeed" --help >/dev/null && echo cli-ok
git -C "$JOBFEED_HOME" check-ignore -q jobfeed/data/jobfeed.sqlite && echo data-ignored
```

Then help the user open the dashboard, where they will read their job matches.
It only listens inside the server, on purpose: it has no password. Pick the
first way that fits, and give step-by-step instructions for their computer:

- **The server has a screen or a cloud desktop the user can see** (a Mac mini,
  a cloud computer with a desktop): open `http://127.0.0.1:7654` in a browser
  there.
- **They log in to the server with SSH** (most VPS and cloud servers): on their
  own computer, open Terminal (Mac: press Cmd+Space, type "Terminal"; Windows:
  open "PowerShell"), paste the line below with the real user and address
  filled in, and leave that window open while they use the dashboard:

  ```sh
  ssh -N -L 7654:127.0.0.1:7654 <user>@<server>
  ```

  Then open `http://127.0.0.1:7654` in their browser. Tell them it is normal
  that the Terminal window shows nothing after they log in.
- **They already use Tailscale on both machines:** run
  `tailscale serve --bg 7654` on the server and give them the address it prints.

Never make the dashboard reachable from the public internet.

Nothing scans until they start a run from the dashboard's **Runs** page or the
daily scan fires.

Finish with a short summary in plain words: Jobfeed is installed and keeps
running in the background; whether it looks for jobs every day and at what time
(in their time zone); which job sites it reads, including whether Handshake,
signed-in LinkedIn and Jobright are on and which Chrome must stay open for
them; how to open the dashboard; and that they can come back to
any coding agent and say "update Jobfeed" or "change my Jobfeed searches". Then,
under a "For later" heading, list the checkout path, the service names, and
these commands:

```sh
# update (Linux)
git -C "$JOBFEED_HOME" pull --ff-only && (cd "$JOBFEED_HOME/jobfeed" && ~/.local/bin/uv sync --locked --no-dev --python 3.12) && systemctl --user restart jobfeed-dashboard
# stop the background services (keeps all data; Linux)
systemctl --user disable --now jobfeed-dashboard.service jobfeed-daily-scan.timer
```

If the Chrome extension is installed, also say: after an update, click the
reload arrow on **Jobfeed Jobright Source** in `chrome://extensions`.

On macOS, replace the `systemctl` parts with
`launchctl kickstart -k "gui/$(id -u)/com.jobfeed.dashboard"` to restart, and
`launchctl bootout "gui/$(id -u)/com.jobfeed.dashboard"` (and
`com.jobfeed.daily-scan`) to stop.
