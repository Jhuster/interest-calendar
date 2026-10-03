import hashlib, json, os, secrets
import socket
from pathlib import Path
from urllib.parse import urlparse
from dataclasses import dataclass

@dataclass
class Config:
    data_dir: Path
    base_url: str='http://127.0.0.1:8787'
    development: bool=True
    @classmethod
    def environment(cls):
        dev=os.getenv('CALENDAR_DEV','0')=='1'
        base=os.getenv('BASE_URL') or f'http://{detect_local_ip() if not dev else "127.0.0.1"}:8787'
        return cls(Path(os.getenv('CALENDAR_DATA_DIR','data')),base.rstrip('/'),dev)
    def validate(self):
        url=urlparse(self.base_url)
        if url.path or url.query or url.fragment or url.username or not url.hostname: raise ValueError('BASE_URL must be an origin')
        if self.development:
            if url.hostname not in ('localhost','127.0.0.1','::1'): raise ValueError('Development mode is loopback only')
        elif url.scheme not in ('http','https'): raise ValueError('BASE_URL must use HTTP or HTTPS')
    def credentials(self):
        return json.loads((self.data_dir/'credentials.json').read_text())

    def agent_token(self):
        return self.saved_token('agent')

    def listen_host(self):
        # One socket on every interface, including --dev. A loopback-only
        # socket cannot accept the LAN subscription URL shown to a phone.
        return '0.0.0.0'

    def allowed_hosts(self):
        hosts=[]
        for origin in (self.base_url, self._subscription_origin()):
            if not origin: continue
            host=urlparse(origin).hostname
            if host and host not in hosts: hosts.append(host)
        return hosts

    def accepted_origins(self):
        origins=[]
        for origin in (self.base_url, self._subscription_origin()):
            if origin and origin not in origins: origins.append(origin)
        return origins

    def subscription_url(self):
        return self.subscription_url_for(self.saved_token('calendar'))

    def subscription_url_for(self, token):
        origin=self._subscription_origin()
        if not token or not origin: return None
        return origin+'/c/'+token+'/calendar.ics'

    def _subscription_origin(self):
        url=urlparse(self.base_url)
        # A configured non-loopback origin is the address the operator chose.
        if not _loopback_host(url.hostname): return self.base_url
        if not self.development: return self.base_url
        detected=detect_local_ip()
        if _loopback_host(detected): return None
        return f'{url.scheme}://{detected}:{url.port or 8787}'

    def saved_token(self, role):
        credentials=self.credentials()
        token=credentials.get(role+'_token')
        if token and token_hash(token)==credentials[role]:
            return token
        # Older installations kept recoverable tokens only in the first-run file.
        path=self.data_dir/'first-run-credentials.txt'
        if path.exists():
            for line in path.read_text().splitlines():
                if line.startswith(role.upper()+'_TOKEN='):
                    token=line.split('=',1)[1]
                    if token_hash(token)==credentials[role]: return token
        return None

def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()

def _loopback_host(host):
    if not host: return True
    host=host.strip('[]').lower()
    return host in ('localhost','::1') or host.startswith('127.')

def detect_local_ip():
    """Best-effort LAN address discovery; never returns a loopback address."""
    try:
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.settimeout(.2)
        sock.connect(('192.0.2.1',80)); candidate=sock.getsockname()[0];sock.close()
        if not candidate.startswith('127.'): return candidate
    except OSError: pass
    try:
        for candidate in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not candidate.startswith('127.'): return candidate
    except OSError: pass
    return '127.0.0.1'
def initialize(config,rotate=None):
    config.data_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=config.data_dir/'credentials.json';existing=json.loads(path.read_text()) if path.exists() else {}
    tokens={}
    for role in ('admin','agent','calendar'):
        if role not in existing or rotate==role:
            tokens[role]=secrets.token_urlsafe(32);existing[role]=token_hash(tokens[role])
            existing[role+'_token']=tokens[role]
    if tokens:
        temporary=path.with_suffix('.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f: json.dump(existing,f)
        temporary.replace(path)
    return tokens
