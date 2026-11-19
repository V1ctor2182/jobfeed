#!/usr/bin/env python3
"""Read-only HTTP link audit; no model calls and no form submissions."""
import argparse, concurrent.futures, datetime, json, re, ssl, time
import urllib.request, urllib.error, urllib.parse
from html.parser import HTMLParser
from pathlib import Path

class Page(HTMLParser):
    def __init__(self):
        super().__init__(); self.text=[]; self.links=[]; self.hidden=0; self.anchor=None
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag in ('script','style'): self.hidden+=1
        if tag=='a':self.anchor=[a.get('href',''),'']
        if tag=='input' and a.get('type') in ('submit','button'):self.text.append(a.get('value',''))
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
        if tag=='a' and self.anchor:self.links.append(self.anchor);self.anchor=None
    def handle_data(self,data):
        if not self.hidden:
            self.text.append(data)
            if self.anchor:self.anchor[1]+=' '+data

def classify(html, title, status, final_url):
    p=Page();p.feed(html);text=' '.join(' '.join(p.text).split());low=text.lower()
    words=lambda t:set(re.findall(r'[a-z0-9]+',t.lower()))-{'the','and','a','of','in','for'}
    wanted=words(title);overlap=len(wanted & words(text))/max(1,len(wanted))
    matched=overlap>=.85
    closure=re.search(r'no longer accepting applications|this (?:job|position|vacancy) (?:is|has been) (?:closed|filled)|job (?:is no longer available|has expired)|position has been filled',low)
    challenge=any(t in low for t in ['verify you are human','checking your browser','access denied','captcha challenge','enable javascript and cookies to continue'])
    apply_links=[{'text':label.strip()[:100],'url':urllib.parse.urljoin(final_url,url)} for url,label in p.links if re.search(r'\bapply\b|submit application',label,re.I) and url and not url.startswith(('#','javascript:'))]
    apply_text=bool(re.search(r'\bapply(?: now| for this| to this| online)?\b|submit application',low))
    if status in (401,403,429) or challenge: result='access_restricted'
    elif status in (404,410):result='url_not_found'
    elif status>=400:result='http_error'
    elif closure and matched:result='closed_message'
    elif matched and apply_links:result='job_page_with_apply_link'
    elif matched and apply_text:result='job_page_with_apply_text'
    elif matched:result='job_page_reachable'
    elif 'sign in' in low or 'log in' in low:result='login_or_redirect'
    else:result='needs_browser'
    return dict(result=result,title_match=matched,title_word_coverage=round(overlap,2),text_chars=len(text),
                apply_links=apply_links[:8],evidence=closure.group(0) if closure and matched else result,
                application_form_verified=False)

class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self):super().__init__();self.chain=[]
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append({'status':code,'from':req.full_url,'to':newurl})
        if urllib.parse.urlparse(newurl).scheme not in ('https','http'):raise ValueError('unsupported redirect scheme')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def check(row, timeout=20):
    started=time.monotonic();url=row.get('application_url') or row['url']
    out={k:row.get(k) for k in ('id','company','title','score')};out.update(input_url=url,checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    redirects=Redirects()
    try:
        if urllib.parse.urlparse(url).scheme not in ('http','https'):raise ValueError('unsupported URL scheme')
        opener=urllib.request.build_opener(redirects)
        req=urllib.request.Request(url,headers={'User-Agent':'ApplicationLinkChecker/1.0','Accept':'text/html,application/xhtml+xml'})
        try:response=opener.open(req,timeout=timeout)
        except urllib.error.HTTPError as e:response=e
        with response:
            data=response.read(2_000_001);status=response.code;final=response.geturl()
            html=data[:2_000_000].decode(response.headers.get_content_charset() or 'utf-8',errors='replace')
        out.update(http_status=status,final_url=final,redirects=redirects.chain,truncated=len(data)>2_000_000)
        out.update(classify(html,row['title'],status,final))
    except Exception as e:
        out.update(result='request_failed',error=f'{type(e).__name__}: {e}',redirects=redirects.chain,application_form_verified=False)
    out['elapsed_seconds']=round(time.monotonic()-started,2)
    return out

def main():
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--output',required=True);p.add_argument('--workers',type=int,default=4);p.add_argument('--timeout',type=int,default=20)
    a=p.parse_args();x=json.loads(Path(a.input).read_text());rows=x['candidates'] if isinstance(x,dict) else x
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True);results=[]
    with (out/'results.jsonl').open('w') as f, concurrent.futures.ThreadPoolExecutor(max_workers=min(max(a.workers,1),8)) as pool:
        tasks=[pool.submit(check,r,a.timeout) for r in rows]
        for future in concurrent.futures.as_completed(tasks):
            r=future.result();results.append(r);f.write(json.dumps(r,ensure_ascii=False)+'\n');f.flush()
            if len(results)%25==0:print(f'checked {len(results)}/{len(rows)}',flush=True)
    results.sort(key=lambda r:(-(r.get('score') or 0),r['id']))
    from collections import Counter
    counts=dict(Counter(r['result'] for r in results))
    (out/'results.json').write_text(json.dumps({'counts':counts,'total':len(results),'scope':'HTTP and HTML evidence only; not browser form validation','results':results},ensure_ascii=False,indent=2))
    lines=['# Application 链接 HTTP 检查','',f'共 {len(results)} 项；仅 HTTP/HTML 检查，不证明表单可提交。','',str(counts),'','| ID | 公司 | 岗位 | HTTP | 结果 | 最终链接 |','|---|---|---|---|---|---|']
    for r in results:lines.append('| '+' | '.join(str(v).replace('|','/').replace('\n',' ') for v in [r['id'],r['company'],r['title'],r.get('http_status','—'),r['result'],r.get('final_url',r['input_url'])])+' |')
    (out/'results.md').write_text('\n'.join(lines)+'\n');print(json.dumps(counts,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
