"""PROPOSAL, NOT CONNECTED: each plant's confirmed reporting time zone, handled in the app. Nothing imports this module.

Today a list's date-only "Date" column is trusted only when the SharePoint site's own regional setting is Central. Two
plant sites (Lear_T1 and BriggsStratton_Auburn) are still on SharePoint's default, Pacific, so their dates are refused
although both plants are in Alabama. Changing those site settings would change what every user of the sites sees.

This keeps two different things apart instead:

  * the STORAGE zone: the zone SharePoint used when it stored a date-only value. It is the site's regional setting, read
    as evidence at capture. It is used for one thing only: to turn the stored instant back into the calendar date that
    was picked on the tablet.
  * the PLANT zone: where the plant is. It decides what "today" and "this business day" mean for that plant. It is a
    confirmed fact per plant site, listed here, never worked out from SharePoint.

The calendar date that was picked is the business date. It is never shifted from one zone into the other.

Nothing is guessed: a site with no confirmed plant zone, a regional setting that is not one of the exact ones known
here, a time that is not exactly midnight in the storage zone, or a value with no offset all give no date.
"""
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

# SharePoint regional time-zone settings as the site reports them. Only settings seen on PPG sites are listed.
SHAREPOINT_ZONES = {
    11: dict(zone='America/Chicago', Description='(UTC-06:00) Central Time (US and Canada)', Information=dict(Bias=360, StandardBias=0, DaylightBias=-60)),
    13: dict(zone='America/Los_Angeles', Description='(UTC-08:00) Pacific Time (US and Canada)', Information=dict(Bias=480, StandardBias=0, DaylightBias=-60)),
}
# Where each plant is. Confirmed 2026-11-20 from the client list (and, for Lear T1, the site's own "Tuscaloosa T1" files).
PLANT_ZONES = {
    'SLAlabama_AlexanderCity': dict(zone='America/Chicago', place='Alexander City, AL'),
    'Hyundai_Montgomery': dict(zone='America/Chicago', place='Montgomery, AL'),
    'Lear_T1': dict(zone='America/Chicago', place='Tuscaloosa, AL'),
    'BriggsStratton_Auburn': dict(zone='America/Chicago', place='Auburn, AL'),
}


def storage_zone(evidence):
    """The IANA zone for a site's regional setting, only when every part of the evidence is exactly a known setting."""
    if not isinstance(evidence, dict) or type(evidence.get('Id')) is not int:
        return None
    known = SHAREPOINT_ZONES.get(evidence['Id'])
    info = evidence.get('Information')
    if not known or evidence.get('Description') != known['Description'] or not isinstance(info, dict):
        return None
    if any(type(info.get(k)) is not int or info[k] != v for k, v in known['Information'].items()):
        return None
    return known['zone']


def plant_zone(site):
    entry = PLANT_ZONES.get(site)
    return entry['zone'] if entry else None


def _instant(value):
    if not isinstance(value, str) or re.search(r'T\d{2}:\d{2}:\d{2}[.,]\d*[1-9]', value):
        return None                                   # fractions of a second are never rounded into midnight
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def stored_date(value, evidence):
    """The calendar date a stored date-only value stands for: midnight in the site's storage zone, or nothing."""
    zone, instant = storage_zone(evidence), _instant(value)
    if not zone or instant is None:
        return None
    local = instant.astimezone(ZoneInfo(zone))
    return local.date().isoformat() if not (local.hour or local.minute or local.second or local.microsecond) else None


def business_date(value, site, evidence):
    """{date, plant_zone, storage_zone} for a record of a plant with a confirmed zone, or None. The date is not shifted."""
    plant, day = plant_zone(site), stored_date(value, evidence)
    if not plant or not day:
        return None
    return dict(date=day, plant_zone=plant, storage_zone=storage_zone(evidence), basis='date picked on the tablet; not shifted between zones')


def plant_today(now, site):
    """Today's date at the plant for an exact instant, or None when the plant's zone is not confirmed."""
    plant, instant = plant_zone(site), _instant(now)
    return instant.astimezone(ZoneInfo(plant)).date().isoformat() if plant and instant is not None else None


def plant_day(day, site):
    """The plant's calendar day as two exact instants (start inclusive, end exclusive). 23 or 25 hours on a clock change."""
    plant = plant_zone(site)
    if not plant or not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        return None
    try:
        start = datetime.fromisoformat(day)
    except ValueError:
        return None
    zone = ZoneInfo(plant)
    a = start.replace(tzinfo=zone).astimezone(timezone.utc)
    b = (start + timedelta(days=1)).replace(tzinfo=zone).astimezone(timezone.utc)
    return dict(start_utc=a.isoformat(), end_utc=b.isoformat(), hours=(b - a).total_seconds() / 3600, plant_zone=plant)
