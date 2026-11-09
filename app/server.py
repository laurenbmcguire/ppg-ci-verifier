import argparse, json, secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from model import Review, Conflict
from report_intake import ReportInbox, IntakeError, IntakeConflict

ROOT=Path(__file__).resolve().parent
class Handler(BaseHTTPRequestHandler):
    def review(self):
        mode=parse_qs(urlsplit(self.path).query).get('mode',['synthetic'])[0]
        if mode not in self.server.reviews: raise Conflict('Unknown dataset mode.')
        return self.server.reviews[mode]
    def log_message(self,*args): pass
    def send(self,code,data,ctype='application/json',extra=None):
        self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k,v in (extra or {}).items(): self.send_header(k,v)
        # Rejections can leave POST bytes unread. Frame the response explicitly so
        # clients do not wait for EOF (which can arrive as a TCP reset on close).
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Connection','close')
        self.close_connection=True
        self.end_headers(); self.wfile.write(data)
    def valid_host(self): return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')
    def do_GET(self):
        if not self.valid_host(): return self.send(403,b'{}')
        try:
            route=urlsplit(self.path).path
            if route=='/api/reports': return self.send(200,json.dumps(self.server.inbox.state(),allow_nan=False).encode())
            if route=='/api/report-sample':
                name=parse_qs(urlsplit(self.path).query).get('name',[''])[0]
                samples={'match':'matched-report.json','errors':'mismatched-report.json'}
                if name not in samples: return self.send(404,b'{"error":"Unknown synthetic example"}')
                return self.send(200,(ROOT.parent/'samples'/samples[name]).read_bytes())
            if route=='/api/state': return self.send(200,json.dumps(dict(state=self.review().view(),token=self.server.token)).encode())
            files={'/assets/ppg-client-intelligence.png':'assets/ppg-client-intelligence.png','/':'index.html','/ui.js':'ui.js','/report-ui.js':'report-ui.js','/style.css':'style.css'}
            if route in files:
                name=files[route]; return self.send(200,(ROOT/name).read_bytes(),{'html':'text/html; charset=utf-8','js':'text/javascript','css':'text/css','png':'image/png'}[name.split('.')[-1]])
            self.send(404,b'{}')
        except Exception as e: self.send(409,json.dumps({'error':str(e),'fail_closed':True}).encode())
    def do_POST(self):
        if not self.valid_host() or self.headers.get('X-Review-Token')!=self.server.token: return self.send(403,b'{"error":"Local session token required"}')
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'): return self.send(403,b'{}')
        try:
            size=int(self.headers.get('Content-Length',0))
            route=urlsplit(self.path).path
            limit=1024*1024 if route=='/api/reports/import' else 10000
            if not 0<size<=limit: raise Conflict('Invalid request size.')
            if route=='/api/reports/import':
                item=self.server.inbox.ingest(self.rfile.read(size))
                return self.send(200,json.dumps({'item':item,'state':self.server.inbox.state()},allow_nan=False).encode())
            p=json.loads(self.rfile.read(size)); action=urlsplit(self.path).path.removeprefix('/api/'); review=self.review()
            if action=='download': return self.send(200,review.download(p['revision']),'text/csv; charset=utf-8',{'Content-Disposition':f'attachment; filename="UNAPPROVED_{review.prefix}_private-copy.csv"'})
            review.act(action,p['revision'],p)
            self.send(200,json.dumps(review.view()).encode())
        except Exception as e: self.send(409,json.dumps({'error':str(e),'fail_closed':True}).encode())
def serve(root, port):
    server=HTTPServer(('127.0.0.1',port),Handler); server.reviews={'synthetic':Review(root)}; server.token=secrets.token_urlsafe(32); server.inbox=ReportInbox(Path(root)/'report-intake')
    print(f'LOCAL REVIEW PROTOTYPE http://127.0.0.1:{server.server_port}',flush=True); server.serve_forever()
if __name__ == '__main__':
    raise SystemExit('Legacy server is disabled in LOCAL SQLITE COPY. Run python hosting/sqlite_server.py instead.')
