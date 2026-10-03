import argparse, os
import logging
from pathlib import Path
from app.config import Config, initialize
from app.services.maintenance import RestoreError, restore_database, stop_service
from app.storage.database import Store

class HideListenAddress(logging.Filter):
    def filter(self, record):
        return not record.getMessage().startswith('Uvicorn running on ')

def main():
    parser=argparse.ArgumentParser(description='兴趣日历本机网站')
    parser.add_argument('command',choices=['init','serve','backup','restore','rotate','stop'])
    parser.add_argument('--data-dir',default='data')
    parser.add_argument('--dev',action='store_true')
    parser.add_argument('--base-url',default='http://127.0.0.1:8787')
    parser.add_argument('--backup',help='restore 使用的备份文件')
    parser.add_argument('--apply',action='store_true',help='核对差异报告后执行恢复')
    parser.add_argument('--cert');parser.add_argument('--key');parser.add_argument('--role',choices=['admin','agent'])
    a=parser.parse_args();config=Config(Path(a.data_dir),a.base_url,a.dev)
    if a.command in ('init','rotate'):
        if a.command=='rotate' and not a.role: parser.error('rotate requires --role')
        tokens=initialize(config,a.role if a.command=='rotate' else None)
        Store(config.data_dir)
        for role,token in tokens.items(): print(f'{role.upper()}_TOKEN={token}')
        if not tokens: print('已初始化；如需新凭据，请使用 rotate --role admin 或 agent')
    elif a.command=='backup': print(Store(config.data_dir).backup(force=True))
    elif a.command=='restore':
        if not a.backup: parser.error('restore requires --backup')
        try: print(restore_database(config.data_dir,a.backup,apply=a.apply)['text'],end='')
        except RestoreError as exc: parser.exit(1,str(exc)+'\n')
    elif a.command=='stop':
        try: print(stop_service(config.data_dir))
        except RestoreError as exc: parser.exit(1,str(exc)+'\n')
    else:
        config.validate()
        import uvicorn
        from urllib.parse import urlparse
        from app.main import create_app
        logging.getLogger('uvicorn.error').addFilter(HideListenAddress())
        uvicorn.run(create_app(config),host='127.0.0.1' if a.dev else '0.0.0.0',port=urlparse(a.base_url).port or 8787,ssl_certfile=a.cert,ssl_keyfile=a.key,access_log=False,workers=1)
if __name__=='__main__': main()
