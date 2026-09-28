"""
Script to fetch GastroPlanner shifts and export as iCal.
"""
import argparse
import collections
import datetime
import hashlib
import logging
import re
import tomllib
import uuid

import requests
from bs4 import BeautifulSoup


Shift = collections.namedtuple("Shift", ["name", "shift_name", "start", "end"])


#
# Shift formatting utils
#


FORMAT_VCALENDAR_START = (
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//github.com/paalbra//NONSGML gastrounplanner//EN",
)
FORMAT_VEVENT = (
    "BEGIN:VEVENT",
    "UID:{uid}",
    "SUMMARY:{shift_name}",
    "DTSTAMP:{start}",
    "DTSTART:{start}",
    "DTEND:{end}",
    "END:VEVENT",
)
FORMAT_VCALENDAR_END = (
    "END:VCALENDAR",
)


def _format_dt(dt):
    return dt.strftime("%Y%m%dT%H%M%S")


def _format_ical_shift(shift):
    ident = f"{shift.shift_name}{shift.start}{shift.end}"
    md5 = hashlib.md5(ident.encode("utf-8"))
    uid = str(uuid.UUID(md5.hexdigest()))
    vevent_data = {
        'uid': uid,
        'shift_name': shift.shift_name,
        'start': _format_dt(shift.start),
        'end': _format_dt(shift.end),
    }
    for line in FORMAT_VEVENT:
        yield line.format(**vevent_data)


def _generate_ical(shifts):
    for line in FORMAT_VCALENDAR_START:
        yield line

    for shift in shifts:
        yield from _format_ical_shift(shift)

    for line in FORMAT_VCALENDAR_END:
        yield line

    yield ""


def format_ical_shifts(shifts):
    """
    Format a list of shifts into a RFC-5545 iCalendar VCALENDAR object.

    :param datetime.date date: the base date
    :param int days_before: start date, in days relative to *date*
    :param int days_after: end date, in days relative to *date* (not inclusive)

    :rtype: iterator[datetime.date]
    """
    return "\r\n".join(_generate_ical(shifts))


#
# Shift parsing
#


END_OF_DAY = datetime.time.max.replace(microsecond=0)


def parse_timespan(time_range, at_date, truncate=False):
    """
    Parse a time range string into a datetime range.

    :param str time_range: time range, e.g. "19:00-23:00"
    :param date at_date: start date for the time range
    :param bool truncate:
        Truncate range to END_OF_DAY if it wraps to the next day

    :rtype: tuple[datetime.datetime, datetime.datetime]
    """
    logging.debug("Parsing time range=%s (at date=%s, truncate=%r)",
                  repr(time_range), at_date.isoformat(), repr(truncate))
    start_date = end_date = at_date

    start_time_str, end_time_str = time_range.split("-")

    start_time = datetime.time.strptime(start_time_str, "%H:%M")
    end_time = datetime.time.strptime(end_time_str, "%H:%M")

    if end_time <= start_time:
        # Timestamp passes midnight - we either truncate to end of day, or
        # push the end-date to next day
        if truncate:
            end_time = END_OF_DAY
        else:
            # Adjust end day if we are passing midnight, like "21:00-01:00".
            end_date += datetime.timedelta(days=1)

    return (
        datetime.datetime.combine(start_date, start_time),
        datetime.datetime.combine(end_date, end_time),
    )


def parse_shifts(content, date, truncate=False):
    """
    Parse day shifts into Shift tuples.

    :param str content: shifts page content
    :param datetime.date date: the date these shifts are for
    :param bool truncate: truncate shifts to END_OF_DAY if they pass midnight

    :rtype: iterator[Shift]
    """
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
        start_time, end_time = parse_timespan(shift_timespan, date, truncate)

        yield Shift(name, shift_name, start_time, end_time)


class GastroUnplanner():

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/") + "/"
        self.session = requests.session()
        self.logged_in = False
        self.truncate = True

    @property
    def login_url(self):
        """ login url (login POST target). """
        return self.base_url + "index.php?controller=TimeSheet"

    @property
    def login_redir_url(self):
        """ login redirect url (expected login redirect). """
        return self.base_url + "index.php?controller=TimeSheet&action=welcome"

    @property
    def shifts_url(self):
        """ shifts url (list shifts at a given date). """
        return (self.base_url +
                "index.php?controller=TimeSheet&action=getPersonalList")

    def login(self, login_email, login_password):
        """ Perform a login for the current session. """
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
        """
        Get shifts at a given date.

        :type date: datetime.date

        :rtype: iterator[Shift]
        """
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
        return parse_shifts(response.text, date, truncate=self.truncate)

    def get_shifts(self, days_since, days_until):
        """
        Get all shifts in a given date range.

        :param int days_since:
            start date, in days relative to today

        :param int days_until:
            end date, non-inclusive, in days relative to today

        :rtype: iterator[Shift]
        """
        if not self.logged_in:
            return
        today = datetime.date.today()
        dates = (
            today + datetime.timedelta(days=d)
            for d in range(days_since, days_until)
        )
        for date in dates:
            yield from self.get_shifts_at(date)


#
# Script
#


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
