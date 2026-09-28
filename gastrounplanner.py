"""
Script to fetch GastroPlanner shifts and export as iCal.
"""
import argparse
import collections
import datetime
import hashlib
import logging
import re
import textwrap
import tomllib
import uuid

import requests
from bs4 import BeautifulSoup


Shift = collections.namedtuple("Shift", ["name", "shift_name", "start", "end"])


def parse_timespan(time_range, date):
    # Parses a simple time range string, like "19:00-23:00", to an end and
    # start time.  It needs the date for the start time.
    logging.debug("Parsing time range, %s, of day %s",
                  time_range, date.isoformat())

    date_str = datetime.datetime.strftime(date, "%Y-%m-%d")
    start_time_str, end_time_str = time_range.split("-")

    start_time = datetime.datetime.strptime(
        f"{date_str} {start_time_str}",
        "%Y-%m-%d %H:%M",
    )
    end_time = datetime.datetime.strptime(
        f"{date_str} {end_time_str}",
        "%Y-%m-%d %H:%M",
    )

    if end_time <= start_time:
        # Adjust end day if we are passing midnight, like "21:00-01:00".
        end_time += datetime.timedelta(days=1)

    return start_time, end_time


def parse_shifts(content, date, truncate=False):
    # The shift data itself only knows what timespan the shifts are.
    # We therefore have to know which day it is through "date".
    # "truncate" will make sure no shift passes midnight
    # (end them 23:59:59 same day).

    datetime.datetime.strftime(date, "%Y-%m-%d")

    soup = BeautifulSoup(content, "html.parser")

    for tr in soup.find_all("tr", {"class": "timetracker_row_expand"}):
        # Each tr is a single shift.
        # The children tds contain data about the shift.

        logging.debug("Parsing shift/html table row: %s", tr)

        tds = tr.find_all("td")

        # Some shifts have some extra weird values after the third one
        name, _, shift_name, *_ = [element.text for element in tds[0].children]

        if shift_name == "":
            logging.warning("Skipping empty shift?: %s", repr(tr))
            continue

        shift_timespan = tds[1].text
        start_time, end_time = parse_timespan(shift_timespan, date)

        if truncate:
            start_time, end_time = truncate_to_day(start_time, end_time)

        yield Shift(name, shift_name, start_time, end_time)


FORMAT_VCALENDAR_START = (
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//github.com/paalbra//NONSGML gastrounplanner//EN",
)
FORMAT_VCALENDAR_END = (
    "END:VCALENDAR",
)
FORMAT_VEVENT = textwrap.dedent(
    """
    BEGIN:VEVENT
    UID:{uid}
    SUMMARY:{name}
    DTSTAMP:{start}
    DTSTART:{start}
    DTEND:{end}
    END:VEVENT
    """
).lstrip()


def _format_dt(dt):
    return dt.strftime("%Y%m%dT%H%M%S")


def _format_ical_shift(shift):
    ident = f"{shift.shift_name}{shift.start}{shift.end}"
    md5 = hashlib.md5(ident.encode("utf-8"))
    uid = str(uuid.UUID(md5.hexdigest()))
    text = FORMAT_VEVENT.format(
        uid=uid,
        name=shift.shift_name,
        start=_format_dt(shift.start),
        end=_format_dt(shift.end),
    )
    return text.strip().split("\n")


def _generate_ical(shifts):
    for line in FORMAT_VCALENDAR_START:
        yield line
    for shift in shifts:
        for line in _format_ical_shift(shift):
            yield line
    for line in FORMAT_VCALENDAR_END:
        yield line
    yield ""


def format_ical_shifts(shifts):
    return "\r\n".join(_generate_ical(shifts))


def truncate_to_day(start_time, end_time):
    # Truncate end times to 23:59:59 of start time if they pass midnight.
    if end_time.date() > start_time.date():
        end_time = datetime.datetime.combine(
            start_time.date(),
            datetime.datetime.max.time().replace(microsecond=0),
        )
    return start_time, end_time


def generate_date_range(date, days_before, days_after):
    for days in range(days_before, days_after):
        yield date + datetime.timedelta(days=days)


class GastroUnplanner():

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/") + "/"
        self.session = requests.session()
        self.logged_in = False
        self.truncate = True

    @property
    def login_url(self):
        return self.base_url + "index.php?controller=TimeSheet"

    @property
    def login_redir_url(self):
        return self.base_url + "index.php?controller=TimeSheet&action=welcome"

    @property
    def shifts_url(self):
        return (self.base_url +
                "index.php?controller=TimeSheet&action=getPersonalList")

    def login(self, login_email, login_password):
        response = self.session.post(
            self.login_url,
            data={
                "login_email": login_email,
                "login_password": login_password,
                "login_user": 1,
            },
            allow_redirects=False,
        )
        if (response.status_code == 303
                and response.headers["Location"] == self.login_redir_url):
            self.logged_in = True
        else:
            raise Exception("Unable to login to: %s", repr(self.login_url))

    def get_shifts_at(self, date):
        response = self.session.post(
            self.shifts_url,
            data={
                "year": date.year,
                "month": date.month,
                "day": date.day,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        # returns a generator
        return parse_shifts(response.text, date, truncate=self.truncate)

    def get_shifts(self, days_since, days_until):
        if not self.logged_in:
            return
        today = datetime.datetime.today().date()
        for date in generate_date_range(today, days_since, days_until):
            yield from self.get_shifts_at(date)


parser = argparse.ArgumentParser(
    description="Creates an ical from a https://gastroplanner.eu/ instance."
)
parser.add_argument("config")
parser.add_argument("--since", type=int, default=-7)
parser.add_argument("--until", type=int, default=30)


def main(argv=None):
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)

    with open(args.config, "rb") as f:
        config = tomllib.load(f)

    gu = GastroUnplanner(config["url"])
    gu.login(config["email"], config["password"])
    # Get shifts. Since 7 days ago and until 30 days forward, by default.
    shifts = gu.get_shifts(args.since, args.until)

    for export in config["exports"]:
        export_shifts = [
            shift
            for shift in shifts
            if re.search(export["name_filter"], shift.name)
        ]
        ical = format_ical_shifts(export_shifts)

        with open(export["file_path"], "w") as f:
            f.write(ical)


if __name__ == "__main__":
    main()
