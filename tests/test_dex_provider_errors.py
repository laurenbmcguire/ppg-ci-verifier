"""Fixed synthetic transport doubles only; no provider connection."""
import io
import json
import urllib.error
import pytest
from hosting import ai_review as ar
from hosting.ai_proposals import failure_metadata

@pytest.mark.parametrize('kind,status,category',[('http',401,'provider_http'),('http',403,'provider_http'),('http',503,'provider_http'),('timeout',None,'provider_timeout'),('transport',None,'provider_transport'),('invalid',None,'response')])
def test_real_send_classifies_fake_transport(monkeypatch,kind,status,category):
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic-not-a-real-key')
    def opener(request,timeout):
        if kind=='http':raise urllib.error.HTTPError(request.full_url,status,'synthetic',{},io.BytesIO(json.dumps({'error':{'message':'secret test body'}}).encode()))
        if kind=='timeout':raise TimeoutError()
        if kind=='transport':raise urllib.error.URLError('synthetic connection error')
        return io.BytesIO(b'not-json')
    with pytest.raises(ar.AIReviewError) as caught:ar._send({},opener=opener)
    meta=failure_metadata(caught.value)
    assert meta['category']==category and meta['http_status']==status
    assert 'secret' not in meta['reason']

def test_missing_config_is_not_outage(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY',raising=False)
    with pytest.raises(ar.AIReviewNotConfigured) as caught:ar._send({},opener=lambda *a,**k:pytest.fail('Unexpected provider call'))
    assert failure_metadata(caught.value)['category']=='configuration'
