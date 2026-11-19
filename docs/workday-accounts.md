# Workday accounts

Many Workday applications start with a Create Account or Sign In page. Claude in Chrome refuses to create accounts or type passwords, so a Claude Applier used to stop there. Now a program, the account program, does it instead. The model never sees the password.

The account program works on Workday sites only, meaning hosts ending in `.myworkdayjobs.com` and the shared `wdN.myworkdaysite.com` hosts. It runs whenever applying is authorized. There is no separate switch.

## How a sign-in works

1. The Applier reaches a Workday page that asks it to sign in or create an account. It calls `account_step` with the page's URL and types nothing.
2. The tool checks that the page is on the same Workday site as the job's registered tab, then asks the account program to act on that tab.
3. With no account on record for the site, the program creates one with the Primary email from My Profile and the shared password. With one on record, it signs in.
4. If Workday sends a verification email, the program reads Gmail, finds the newest email from `<tenant>@otp.workday.com` or `<tenant>@myworkday.com`, opens its verification link in a new background tab, closes that tab, and signs in on the Applier's tab.
5. The tool returns the outcome and what the Applier does next.

The verification email is untrusted. The program opens a link only when it is on the account's own Workday site and is the only verification link in the email. It reaches Gmail the way the recruiter sheet reaches Google, through `claude -p` and the claude.ai Gmail connector. It reads only the raw tool result, never the model's reply.

## Set the shared password

One password serves every site. Set it once:

```sh
cd resume-application-pipeline
.venv/bin/python -m pipeline.cli accounts set-password
```

It asks twice and saves nothing unless both match. Workday's usual rules apply: at least 8 characters with an uppercase letter, a lowercase letter, a digit and a special character, in printable ASCII with no spaces. The password goes to `.pipeline/account-password` with mode 0600. Only the account program reads it. No command, log, tool result or Discord message shows it, and the account records hold no password column.

`accounts list` shows each site's account: its email, state, the job that created it and the last error. `accounts forget SITE` deletes one record, so the next Applier creates or signs in again. Neither touches Workday.

## The applying browser

Appliers and the account program work in a Chromium of their own, the applying browser. It runs with its own profile in `~/snap/chromium/common/chromium-apply` and a fixed DevTools port, 9333, on 127.0.0.1 only. Chromium asks you to click **Allow** for every DevTools connection to your everyday profile. It never asks for a separate profile started with a port, so nothing waits on you. Your everyday browser stays out of applying.

The port has no prompt and no password, so any program running as you can drive the applying browser. Keep accounts you care about out of it, beyond the job sites you apply on.

Set it up once:

1. Install and start the services.

   ```sh
   cp deploy/systemd/jobfeed-apply-chromium.service deploy/systemd/jobfeed-browser-broker.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now jobfeed-apply-chromium jobfeed-browser-broker
   ```

2. In the applying browser's window, install Claude in Chrome and sign in to it. Sign in to the job sites Appliers use, such as LinkedIn.
3. Record the applying browser's Claude in Chrome deviceId in `resume-application-pipeline/.pipeline/apply-browser.json`, as `{"device_id": "<deviceId>"}`. `list_connected_browsers` shows it. Appliers launched after that call `select_browser` with it before they touch a page. Without the file they pick the browser on this computer, which is ambiguous once two are connected.

The account program reads the browser's DevTools address from `http://127.0.0.1:9333/json/version` on each new connection, so it reconnects by itself after the browser restarts. It listens on `.pipeline/browser-broker.sock`, mode 0600. To run it in a terminal instead, use `.venv/bin/python -m pipeline.browser_broker serve --root .` from `resume-application-pipeline`.

## Outcomes

| Outcome | Meaning | What the Applier does |
| --- | --- | --- |
| `signed_in` | The tab is signed in. | Continues filling the form. |
| `wrong_password` | An account for this email exists, and the shared password does not open it. | Reports a `user_only_check` blocker. Sign in yourself, or reset that site's password to the shared one, then reply. It never uses Forgot Password. |
| `needs_verification` | Workday wants the email verified, and no usable verification email arrived within about two minutes. | Reports a `user_only_check` blocker. Open the email's link, then reply. |
| `captcha` | The page shows a CAPTCHA. | Reports the usual `captcha` blocker. |
| `unknown_page` | The page did something the program does not recognize. The reason says what. | Reports a `user_only_check` blocker with the reason. |
| `tab_not_found` | No tab, or more than one, shows exactly the URL the Applier passed. | Calls `account_step` once more with the exact URL, then reports a blocker. |
| `not_connected` | The applying browser is not running. | Reports a `user_only_check` blocker. The job's Discord thread also gets an alert. Start `jobfeed-apply-chromium`, then reply. |
| `broker_unavailable` | The account program is not running. | Same, with an alert to start the service. |
| `no_password` | No shared password is set. | Same, with an alert to run `accounts set-password`. |

When you reply to one of these blockers, the Applier calls `account_step` again.

The program never solves a CAPTCHA and never changes the password of an existing account.

## What you see in review

When a job created an account, its review summary in Discord names the site and the email, for example ``**Account:** created a Workday account on `acme.wd5.myworkdayjobs.com` for you@example.com``.

## Limits

- The program clicks Workday's controls by their `data-automation-id` and reads the result from the page's text. A tenant with different wording can show as `unknown_page`.
- Requests run one at a time, so Appliers on Workday at the same moment wait for each other.
