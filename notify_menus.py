"""Preview or send new MSU menu candidates via authenticated TLS SMTP."""

import argparse
import base64
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
import hashlib
import html
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
import uuid
from zoneinfo import ZoneInfo

KINDS = ("cheesecake_candidate", "related_dessert_review")
STATE_BRANCH = "codex/notification-state"
STATE_PATH = ".notifications/state.json"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class LocalState:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"version": 1, "recipients": {}}

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class GitHubState:
    """Durable state on a separate branch; SHA checks refuse concurrent overwrites."""
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("Invalid GITHUB_REPOSITORY")
        self.token = os.environ["GITHUB_TOKEN"]
        self.sha = None
        self.ready = False

    def api(self, path, data=None, method=None):
        req = Request(f"https://api.github.com/repos/{self.repo}{path}",
            data=None if data is None else json.dumps(data).encode(), method=method,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "User-Agent": "MSU-Menu-Notifier", "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404:
                return None
            raise RuntimeError(f"Notification state request failed (HTTP {error.code})") from None

    def read(self):
        response = self.api(f"/contents/{STATE_PATH}?ref={quote(STATE_BRANCH, safe='')}")
        if response is None:
            return {"version": 1, "recipients": {}}
        self.sha = response["sha"]
        return json.loads(base64.b64decode(response["content"]).decode("utf-8"))

    def save(self, state):
        if not self.ready:
            ref = self.api(f"/git/ref/heads/{STATE_BRANCH}")
            if ref is None:
                commit = os.environ.get("GITHUB_SHA")
                if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
                    raise ValueError("Missing workflow commit SHA for state branch initialization")
                if not self.api("/git/refs", {"ref": "refs/heads/" + STATE_BRANCH, "sha": commit}):
                    raise RuntimeError("Could not create notification state branch")
            self.ready = True
        payload = {"message": "Record menu notification delivery state", "branch": STATE_BRANCH,
                   "content": base64.b64encode(json.dumps(state, sort_keys=True).encode()).decode()}
        if self.sha:
            payload["sha"] = self.sha
        response = self.api(f"/contents/{STATE_PATH}", payload, method="PUT")
        if not response:
            raise RuntimeError("Could not persist notification state")
        self.sha = response["content"]["sha"]


def smtp_config():
    required = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_TO"]
    missing = [key for key in required if not os.environ.get(key, "").strip()]
    if missing:
        raise ValueError("Missing email settings: " + ", ".join(missing))
    sender = os.environ.get("EMAIL_FROM") or os.environ["SMTP_USERNAME"]
    recipients = sorted(set(address.strip().lower() for address in os.environ["EMAIL_TO"].split(",") if address.strip()))
    for key, addresses in (("SMTP_USERNAME", [os.environ["SMTP_USERNAME"]]),
                           ("EMAIL_FROM", [sender]), ("EMAIL_TO", recipients)):
        if any(not re.fullmatch(r"[^\s<>@,]+@[^\s<>@,]+\.[^\s<>@,]+", address) for address in addresses):
            raise ValueError(f"{key} must contain plain email addresses without line breaks")
    security = os.environ.get("SMTP_SECURITY", "starttls").lower()
    if security not in ("starttls", "ssl"):
        raise ValueError("SMTP_SECURITY must be starttls or ssl")
    return {"host": os.environ["SMTP_HOST"], "port": int(os.environ.get("SMTP_PORT", "587")),
            "username": os.environ["SMTP_USERNAME"], "password": os.environ["SMTP_PASSWORD"],
            "sender": sender, "recipients": recipients, "security": security}


def send_email(config, subject, text, markup):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["sender"]
    message["To"] = ", ".join(config["recipients"])
    message.set_content(text)
    message.add_alternative(markup, subtype="html")
    context = ssl.create_default_context()
    if config["security"] == "ssl":
        client = smtplib.SMTP_SSL(config["host"], config["port"], timeout=30, context=context)
    else:
        client = smtplib.SMTP(config["host"], config["port"], timeout=30)
    # Once SMTP starts, a timeout can leave delivery uncertain. Never retry automatically.
    with client:
        if config["security"] == "starttls":
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(config["username"], config["password"])
        refused = client.send_message(message)
        if refused:
            raise RuntimeError("Some recipients were refused; delivery requires manual review")


def select_updates(report, sent, today):
    if report.get("errors") != 0 or not report.get("coverage"):
        raise ValueError("Menu retrieval was incomplete; refusing to send partial results")
    if any(row.get("status") != "ok" for row in report["coverage"]):
        raise ValueError("Menu source failed; refusing to send partial results")
    selected = {}
    for row in report["matches"]:
        if row["kind"] not in KINDS or row["meal_slug"].lower() != "lunch" or not today.isoformat() <= row["date"] <= report["end"]:
            continue
        key = digest([row["date"], row["location_slug"], row["meal_slug"], row["food_id"], row.get("station")])
        fingerprint = digest([row["name"], row["kind"], row["menu_url"]])
        if sent.get(key, {}).get("fingerprint") != fingerprint:
            selected[key] = {"row": row, "fingerprint": fingerprint, "date": row["date"]}
    return dict(sorted(selected.items(), key=lambda item: (item[1]["row"]["kind"], item[1]["date"], item[1]["row"]["location"], item[1]["row"]["meal"])))


def render_email(updates, report):
    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    start, end = date.fromisoformat(report["start"]), date.fromisoformat(report["end"])
    subject = f"[cheesecake] {start.month}/{start.day} - {end.month}/{end.day}"
    text = [f"Menu week: {start.isoformat()} ({weekdays[start.weekday()]}) to {end.isoformat()} ({weekdays[end.weekday()]}) (MSU local dates)", "", "New or changed menu items are listed below. Categories are based on item names and have not been verified in person. Items may change or sell out."]
    body = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head><body style='font-family:Arial,sans-serif;color:#183d32;line-height:1.6'>", "<h1>MSU cheesecake menu update</h1>", f"<p>{html.escape(text[0])}</p><p>{html.escape(text[2])}</p>"]
    for kind, label in [(KINDS[0], "Cheesecake candidates"), (KINDS[1], "Related desserts to check separately (such as ice cream)")]:
        rows = [entry["row"] for entry in updates.values() if entry["row"]["kind"] == kind]
        if not rows:
            continue
        text += ["", label]
        body += [f"<h2>{label}</h2><table cellpadding='8' style='border-collapse:collapse' border='1'><tr><th>Date</th><th>Dining location</th><th>Meal</th><th>Station</th><th>Item</th></tr>"]
        for row in rows:
            url = row["menu_url"]
            if urlparse(url).scheme != "https" or urlparse(url).netloc != "msu.nutrislice.com":
                raise ValueError("Unexpected menu link domain")
            menu_date = date.fromisoformat(row["date"])
            values = [f"{menu_date.isoformat()} ({weekdays[menu_date.weekday()]})", row["location"], row["meal"], row.get("station") or "", row["name"]]
            text += [" | ".join(values), url]
            body.append("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in values[:4]) + f"<td><a href='{html.escape(url, quote=True)}'>{html.escape(values[4])}</a></td></tr>")
        body.append("</table>")
    text += ["", "Other possible matches are in the report but are not all included in this email. A missing item or empty menu does not confirm that it will not be served."]
    body += [f"<p>{html.escape(text[-1])}</p></body></html>"]
    return subject, "\n".join(text), "\n".join(body)


def deliver(store, state, recipient_key, updates, config, content, today, sender=send_email):
    recipient = state["recipients"].setdefault(recipient_key, {"sent": {}})
    if recipient.get("pending"):
        raise RuntimeError("Previous delivery is uncertain. Check the inbox, then manually retry-pending if needed")
    # Persist BEFORE SMTP. A crash after acceptance cannot silently send duplicates on the next run.
    recipient["pending"] = {"id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
        "items": {key: {"date": value["date"], "fingerprint": value["fingerprint"]} for key, value in updates.items()}}
    store.save(state)
    sender(config, *content)
    keep_after = (today - timedelta(days=7)).isoformat()
    recipient["sent"] = {key: value for key, value in recipient["sent"].items() if value["date"] >= keep_after}
    for key, value in updates.items():
        recipient["sent"][key] = {"date": value["date"], "fingerprint": value["fingerprint"]}
    recipient.pop("pending")
    store.save(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("probe-output/results.json"))
    parser.add_argument("--state", type=Path, default=Path("probe-output/notification-state.json"))
    parser.add_argument("--state-backend", choices=["local", "github"], default="local")
    parser.add_argument("--mode", choices=["preview", "send", "test-email", "retry-pending", "acknowledge-pending", "failure"], default="preview")
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("America/Detroit")).date()
    config = smtp_config() if args.mode != "preview" else None
    if args.mode in ("test-email", "failure"):
        subject = "[MSU] Email notification test" if args.mode == "test-email" else "[MSU] Menu check failed: action needed"
        text = ("This is a test email. If you received it, the email service is connected." if args.mode == "test-email" else "The menu check or notification did not finish, so cheesecake availability is unknown. Check the GitHub Actions run for details.")
        url = os.environ.get("GITHUB_SERVER_URL", "https://github.com") + "/" + os.environ.get("GITHUB_REPOSITORY", "") + "/actions/runs/" + os.environ.get("GITHUB_RUN_ID", "")
        if os.environ.get("GITHUB_RUN_ID"):
            text += "\n" + url
        send_email(config, subject, text, f"<p>{html.escape(text)}</p>")
        print("Email accepted by SMTP server; inbox delivery has not been independently verified.")
        return 0
    if args.mode == "acknowledge-pending":
        store = GitHubState() if args.state_backend == "github" else LocalState(args.state)
        state = store.read()
        recipient = state["recipients"].get(digest(config["recipients"]))
        if recipient and recipient.get("pending"):
            recipient["sent"].update(recipient.pop("pending")["items"])
            store.save(state)
        print("Acknowledged prior delivery; no email sent.")
        return 0
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if args.mode != "preview":
        if report.get("data_mode") != "refresh":
            raise ValueError("Sending requires a freshly downloaded report (--refresh)")
        generated = datetime.fromisoformat(report["generated_at_utc"])
        age = datetime.now(timezone.utc) - generated
        if age < timedelta(minutes=-5) or age > timedelta(hours=6):
            raise ValueError("Report is too old or has an invalid timestamp; refresh before sending")
        if not report["start"] <= today.isoformat() <= report["end"]:
            raise ValueError("Report does not cover today's MSU date")
    store = GitHubState() if args.state_backend == "github" else LocalState(args.state)
    state = store.read()
    if state.get("version") != 1 or not isinstance(state.get("recipients"), dict):
        raise ValueError("Invalid notification state; refusing to discard delivery history")
    recipient_key = digest(config["recipients"]) if config else "preview"
    recipient = state["recipients"].get(recipient_key, {"sent": {}})
    if args.mode == "retry-pending":
        recipient.pop("pending", None)
        state["recipients"][recipient_key] = recipient
        store.save(state)
    if recipient.get("pending"):
        raise RuntimeError("Previous delivery is uncertain. Check inbox and use retry-pending only if needed")
    updates = select_updates(report, recipient["sent"], today)
    content = render_email(updates, report)
    output = args.report.parent
    (output / "email-preview.txt").write_text(content[0] + "\n\n" + content[1], encoding="utf-8")
    (output / "email-preview.html").write_text(content[2], encoding="utf-8")
    if args.mode == "preview":
        print(f"Preview only: {len(updates)} candidates; no email sent and no state changed.")
    elif not updates:
        print("No new or changed future menu candidates; no email sent.")
    else:
        deliver(store, state, recipient_key, updates, config, content, today)
        print(f"SMTP accepted a notification containing {len(updates)} candidates; state saved.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # SMTP/server exceptions can contain addresses or server text; never print credentials.
        if isinstance(error, (ValueError, RuntimeError, FileNotFoundError)):
            print(f"Notification stopped: {error}")
        else:
            print(f"Notification stopped ({type(error).__name__}); check configuration and delivery state.")
        raise SystemExit(1)
