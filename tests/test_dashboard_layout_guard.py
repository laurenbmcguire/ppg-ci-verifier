"""Missing deterministic rows never become invented zero-reject row analytics."""
import pytest
from hosting import dashboard

@pytest.mark.parametrize('summary,wording',[
    ({'scans':12,'inspected':12,'rejected':2,'rejected_unit':'rows'},'saved summary has counts'),
    ({'scans':None,'inspected':None,'rejected':None},'quantities are unknown'),
    ({'scans':0,'inspected':0,'rejected':0},'explicitly records zero'),
    ({'scans':0,'inspected':10,'rejected':0},'saved summary has counts'),
])
def test_missing_rows_are_distinguished_and_never_zeroed(monkeypatch,summary,wording):
    monkeypatch.setattr(dashboard,'summarize',lambda *a,**kw:summary)
    monkeypatch.setattr('hosting.spreadsheets.report_records',lambda *a,**kw:[])
    with pytest.raises(ValueError,match=wording):dashboard.data_for({'filename':'SYNTHETIC.csv'},b'')

def test_partial_local_rows_are_not_presented_as_complete_analytics(monkeypatch):
    monkeypatch.setattr(dashboard,'summarize',lambda *a,**kw:{'scans':2})
    monkeypatch.setattr('hosting.spreadsheets.report_records',lambda *a,**kw:[{'table_kind':'detail'}])
    with pytest.raises(ValueError,match='do not match'):dashboard.data_for({'filename':'SYNTHETIC.csv','mapping_mode':'adaptive','record_count':2},b'')
