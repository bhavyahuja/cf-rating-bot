import os
import smtplib
from email.mime.text import MIMEText

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

HANDLE_COLUMN = "Put in your cf handle"
EMAIL_COLUMN = "Put in the email address you'd like to receive notifications on"
LAST_NOTIFIED_COLUMN = "Last Notified Time"
LAST_RATING_UPDATE_COLUMN = "Last Rating Update Time"
CURRENT_RATING_COLUMN = "Current Rating"
LAST_CONTEST_COLUMN = "Last Contest Name"
MANAGED_COLUMNS = [
    LAST_NOTIFIED_COLUMN,
    LAST_RATING_UPDATE_COLUMN,
    CURRENT_RATING_COLUMN,
    LAST_CONTEST_COLUMN,
]


def require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(
            f"Missing required environment variable `{name}`. "
            "Please set it in your GitHub Actions secrets."
        )
    return value


def load_config():
    smtp_port = require_env("SMTP_PORT")
    try:
        smtp_port = int(smtp_port)
    except ValueError as error:
        raise ValueError("`SMTP_PORT` must be a valid integer.") from error

    return {
        "smtp_server": require_env("SMTP_SERVER"),
        "smtp_port": smtp_port,
        "smtp_user": require_env("SMTP_USER"),
        "smtp_password": require_env("SMTP_PASSWORD"),
    }


def load_users_from_google_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "service_account.json", scope
    )
    client = gspread.authorize(creds)
    sheet = client.open("Codeforces Rating Bot (Responses)").sheet1
    ensure_managed_columns(sheet)
    records = sheet.get_all_records(default_blank="")
    print(f"Loaded {len(records)} users from Google Sheet.")
    return records, sheet


def ensure_managed_columns(sheet):
    headers = sheet.row_values(1)
    updated = False

    for column_name in MANAGED_COLUMNS:
        if column_name not in headers:
            headers.append(column_name)
            updated = True

    if updated:
        sheet.update("1:1", [headers])
        print(f"Added managed sheet columns: {', '.join(MANAGED_COLUMNS)}")


def get_rating(handle):
    url = f"https://codeforces.com/api/user.rating?handle={handle}"
    response = requests.get(url, timeout=10)
    response.raise_for_status()

    data = response.json()
    if data["status"] != "OK":
        raise Exception(
            f"Codeforces API error for handle {handle}: {data.get('comment')}"
        )

    return data["result"]


def send_email_notification(smtp_info, recipient_email, contest_name, old_rating, new_rating):
    rating_change = new_rating - old_rating
    color = "green" if rating_change >= 0 else "red"
    sign = "+" if rating_change >= 0 else ""

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2 style="color: #333;">Contest Update: <span style="color:#0077cc;">{contest_name}</span></h2>
        <p><b>Old Rating:</b> {old_rating}</p>
        <p><b>New Rating:</b> {new_rating}</p>
        <p><b>Rating Change:</b> <span style="color: {color}; font-weight: bold;">{sign}{rating_change}</span></p>
    </body>
    </html>
    """

    msg = MIMEText(html, "html")
    msg["Subject"] = "Contest Rating Update!"
    msg["From"] = smtp_info["smtp_user"]
    msg["To"] = recipient_email

    with smtplib.SMTP(
        smtp_info["smtp_server"], smtp_info["smtp_port"], timeout=30
    ) as server:
        server.starttls()
        server.login(smtp_info["smtp_user"], smtp_info["smtp_password"])
        server.send_message(msg)


def to_int(value, default=0):
    if value is None:
        return default

    if isinstance(value, (int, float)):
        return int(value)

    value = str(value).strip()
    if not value:
        return default

    try:
        return int(float(value))
    except ValueError:
        return default


def get_latest_rating_info(ratings):
    if not ratings:
        return None

    latest_rating = ratings[-1]
    return {
        "rating_update_time": int(latest_rating["ratingUpdateTimeSeconds"]),
        "old_rating": int(latest_rating["oldRating"]),
        "new_rating": int(latest_rating["newRating"]),
        "contest_name": latest_rating["contestName"],
    }


def update_user_row(sheet, row_number, headers, values):
    updates = []
    for key, value in values.items():
        column_number = headers.index(key) + 1
        cell = gspread.utils.rowcol_to_a1(row_number, column_number)
        updates.append({"range": cell, "values": [[value]]})

    if updates:
        sheet.batch_update(updates)


def main():
    try:
        smtp_info = load_config()
    except Exception as error:
        print(f"Failed to load SMTP configuration: {error}")
        return

    try:
        users, sheet = load_users_from_google_sheet()
        headers = sheet.row_values(1)
    except Exception as error:
        print(f"Failed to load users from Google Sheet: {error}")
        return

    for idx, user in enumerate(users, start=2):
        handle = str(user.get(HANDLE_COLUMN, "")).strip()
        email = str(user.get(EMAIL_COLUMN, "")).strip()

        if not handle or not email:
            print(f"Skipping row {idx}: missing handle or email.")
            continue

        try:
            print(f"Fetching ratings for {handle}...")
            ratings = get_rating(handle)
            latest_rating = get_latest_rating_info(ratings)

            if latest_rating is None:
                print(f"No rating history for handle: {handle}")
                update_user_row(
                    sheet,
                    idx,
                    headers,
                    {
                        LAST_NOTIFIED_COLUMN: "",
                        CURRENT_RATING_COLUMN: "",
                        LAST_RATING_UPDATE_COLUMN: "",
                        LAST_CONTEST_COLUMN: "",
                    },
                )
                continue

            last_known_update_time = to_int(user.get(LAST_RATING_UPDATE_COLUMN))
            last_notified_time = to_int(user.get(LAST_NOTIFIED_COLUMN))
            current_update_time = latest_rating["rating_update_time"]

            if last_known_update_time == 0:
                print(
                    f"Seeding state for {handle} at {current_update_time}. "
                    "No email will be sent on first observation."
                )
                update_user_row(
                    sheet,
                    idx,
                    headers,
                    {
                        LAST_NOTIFIED_COLUMN: current_update_time,
                        LAST_RATING_UPDATE_COLUMN: current_update_time,
                        CURRENT_RATING_COLUMN: latest_rating["new_rating"],
                        LAST_CONTEST_COLUMN: latest_rating["contest_name"],
                    },
                )
                continue

            if current_update_time > last_known_update_time:
                print(f"New rating for {handle}. Sending notification to {email}.")
                send_email_notification(
                    smtp_info,
                    email,
                    latest_rating["contest_name"],
                    latest_rating["old_rating"],
                    latest_rating["new_rating"],
                )
                update_user_row(
                    sheet,
                    idx,
                    headers,
                    {
                        LAST_NOTIFIED_COLUMN: current_update_time,
                        LAST_RATING_UPDATE_COLUMN: current_update_time,
                        CURRENT_RATING_COLUMN: latest_rating["new_rating"],
                        LAST_CONTEST_COLUMN: latest_rating["contest_name"],
                    },
                )
                print(f"Updated stored rating state for {handle}.")
            else:
                update_user_row(
                    sheet,
                    idx,
                    headers,
                    {
                        LAST_NOTIFIED_COLUMN: last_notified_time,
                        LAST_RATING_UPDATE_COLUMN: current_update_time,
                        CURRENT_RATING_COLUMN: latest_rating["new_rating"],
                        LAST_CONTEST_COLUMN: latest_rating["contest_name"],
                    },
                )
                print(
                    f"No new rating update for {handle}. "
                    f"Stored time: {last_known_update_time}, current time: {current_update_time}"
                )

        except Exception as error:
            print(f"An error occurred for user {handle}: {error}")
            continue


if __name__ == "__main__":
    main()
