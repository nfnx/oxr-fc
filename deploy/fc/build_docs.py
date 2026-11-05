"""Generate the public HTML and OpenAPI contract from local sources.

--private additionally substitutes the FC client token into an ACL-protected,
Git-ignored HTML copy. Never use a cloud AccessKey as OXR_FC_TOKEN.
"""
import argparse
import getpass
import html
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def build(private=False):
    import markdown
    spec=importlib.util.spec_from_file_location("oxr_fc_openapi_export",ROOT/"deploy/fc/gateway.py")
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    schema=module.app.openapi()
    schema["servers"]=[{"url":"https://rethos-oxr-demo-neqzwwuitq.cn-hangzhou.fcapp.run",
                        "description":"Target endpoint; see release receipt for deployed version"}]
    (ROOT/"deploy/fc/openapi.json").write_text(json.dumps(schema,ensure_ascii=False,indent=2),encoding="utf-8")
    source=(ROOT/"docs/FC\u63a5\u53e3\u6587\u6863.md").read_text(encoding="utf-8")
    body=markdown.markdown(source,extensions=["tables","fenced_code"])
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OXR FC API</title><style>body{max-width:1100px;margin:40px auto;padding:0 24px;font:16px/1.75 system-ui;color:#172033}table{border-collapse:collapse;width:100%;display:block;overflow:auto}td,th{border:1px solid #d8dfe8;padding:9px;text-align:left}th{background:#f0f4fa}pre{background:#101c30;color:#e4efff;padding:18px;overflow:auto;border-radius:8px}code{overflow-wrap:anywhere}h1,h2,h3{line-height:1.35;margin-top:1.7em}a{color:#1465c0}</style><body>'+body+'</body></html>'
    (ROOT/"\u63a5\u53e3\u6587\u6863.html").write_text(page,encoding="utf-8")
    if private:
        token=os.environ.get("OXR_FC_TOKEN")
        if not token:raise RuntimeError("OXR_FC_TOKEN required for private delivery")
        path=ROOT/"\u63a5\u53e3\u6587\u6863.private.html"
        if not path.exists():
            with path.open("x",encoding="utf-8"):pass
        if os.name=="nt":
            identity=os.environ.get("USERDOMAIN","")+"\\"+getpass.getuser()
            executor=subprocess.check_output(["whoami"],text=True).strip()
            subprocess.run(["icacls",str(path),"/inheritance:r","/grant:r",identity+":(F)",executor+":(F)","SYSTEM:(F)"],
                           check=True,capture_output=True)
        else:path.chmod(0o600)
        path.write_text(page.replace("&lt;FC_TOKEN&gt;",html.escape(token)),encoding="utf-8")
    print(json.dumps({"openapi_paths":len(schema["paths"]),"private_generated":private}))


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--private",action="store_true")
    build(parser.parse_args().private)
