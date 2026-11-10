"""The plant-location timezone contract: a timestamp becomes a business day only for a listed site."""
from zoneinfo import ZoneInfo
from hosting.reconcile import item_day
from hosting.site_timezones import site_zone


def test_listed_site_uses_plant_local_day():
    zone = site_zone('https://ppgquality.sharepoint.com/sites/SLAlabama_AlexanderCity/Lists/Data%202200')
    assert zone == ZoneInfo('America/Chicago')
    # 03:30 UTC on Nov 7 is still Nov 6 in Alexander City
    assert item_day({'Date': '2026-11-07T03:30:00Z'}, {}, None, zone) == ('2026-11-06', 'business_date_with_time')
    assert item_day({'Date': '2026-11-07T03:30:00Z'}, {}, None, site_zone('BriggsStratton_Statesboro')) == ('2026-11-06', 'business_date_with_time')
    assert item_day({'Date': '2026-11-07T06:30:00Z'}, {}, None, site_zone('Lucid_CasaGrande')) == ('2026-11-06', 'business_date_with_time')


def test_unlisted_site_and_plain_dates_are_unchanged():
    assert site_zone('https://ppgquality.sharepoint.com/sites/Lear_T1/Lists/x') is None
    assert item_day({'Date': '2026-11-07T03:30:00Z'}, {}) == (None, 'timezone_unconfirmed')
    assert item_day({'Date': '2026-11-06'}, {}, None, ZoneInfo('America/Chicago')) == ('2026-11-06', 'business_date')
    assert item_day({'Date': '2026-11-07T03:30:00'}, {}, None, ZoneInfo('America/Chicago')) == (None, 'timezone_unconfirmed')   # no offset: not guessed
