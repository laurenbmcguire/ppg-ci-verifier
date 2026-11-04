"""Dispatch only recognized, explicitly mapped report/source schemas. No fallbacks."""
from hosting.branded_reports import reconcile_result as reconcile_ppg


def reconcile_result(report, source):
    if str(report.get('template_id', '')).startswith('pratt-kn-'):
        from hosting.pratt_reports import reconcile_pratt
        return reconcile_pratt(report, source)
    return reconcile_ppg(report, source)
