"""Build Autofill data from the same CONTENT tuples used to render the resume."""
import ast
import json
import re
from pathlib import Path
MONTHS={m:i+1 for i,m in enumerate(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'])}
def plain(value):
 return re.sub(r'\[([^]]+)\]\([^)]+\)',r'\1',value).replace('**','')
def dates(value,education=False):
 match=re.fullmatch(r'\s*([A-Za-z]+) (\d{4})\s*[–—-]\s*(?:Expected\s+)?(?:(Present)|([A-Za-z]+) (\d{4}))\s*',value)
 if not match:raise ValueError('Unrecognized date range: '+value)
 m,y,present,endm,endy=match.groups()
 result={'start_month':str(MONTHS[m[:3]]),'start_year':y}
 if endy and (int(endy),MONTHS[endm[:3]])<(int(y),MONTHS[m[:3]]):raise ValueError('Date range is reversed')
 if education and present:raise ValueError('Education needs actual selected graduation variant')
 if education:result.update(grad_month=str(MONTHS[endm[:3]]),grad_year=endy)
 elif present:result['currently_working']=True
 else:result.update(end_month=str(MONTHS[endm[:3]]),end_year=endy,currently_working=False)
 return result
def split_skills(value):
 result=[]
 for item in re.split(r'[,;]\s*(?![^()]*\))',value):
  item=plain(item).strip();item=re.sub(r'^and\s+','',item)
  if item=='TypeScript/JavaScript':result.extend(['TypeScript','JavaScript'])
  elif item.startswith('AWS (') and item.endswith(')'):
   result.append('AWS');result.extend(x.strip() for x in item[5:-1].split(','))
  elif item=='Claude and OpenAI APIs':result.extend(['Claude APIs','OpenAI APIs'])
  elif item:result.append(item)
 return result
def convert_content(text,variant):
 tree=ast.parse(text);assign=next((n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='CONTENT' for t in n.targets)),None)
 if assign is None:raise ValueError('CONTENT assignment required')
 content=ast.literal_eval(assign.value)
 return convert_tuples(content,variant)
def convert_tuples(content,variant=None):
 if variant is None:
  degrees=[x[2] for x in content if x[0]=='degree']
  variant='May2027' if any('Expected May 2027' in d for d in degrees) else 'Dec2026' if any('Expected Dec 2026' in d for d in degrees) else 'content'
 if variant not in ('May2027','Dec2026','content'):raise ValueError('Unknown graduation variant')
 result={'schemaVersion':1,'variant':variant,'experience':[],'education':[],'skill':[],'websites':[],'projects':[]}
 section='';company=None;active=None
 for item in content:
  kind=item[0]
  if kind=='section':section=item[1].upper();company=active=None
  elif kind=='contact':
   emails=re.findall(r'mailto:([^)?]+)',item[1])
   if emails:result['email']=emails[0]
   for url in re.findall(r'\]\((https?://[^)]+)\)',item[1]):
    if not any(x['url']==url for x in result['websites']):result['websites'].append({'url':url})
  elif kind=='degree' and section=='EDUCATION':
   school,qualification=plain(item[1]).split(' — ',1)
   match=re.match(r'((?:B|M)\.(?:S|A)\.|Ph\.?D\.?)\s+(.+)',qualification)
   if not match:raise ValueError('Degree/major needs explicit mapping: '+qualification)
   row={'education':{'name':school},'degree':match[1],'major':{'name':match[2]},**dates(item[2],True)}
   expected={'Dec2026':('12','2026'),'May2027':('5','2027')}.get(variant)
   if expected and (row['grad_month'],row['grad_year'])!=expected:raise ValueError('Variant does not match selected CONTENT date')
   result['education'].append(row)
  elif kind=='skill':
   for name in split_skills(item[2]):
    if not any(x['skill']['name']==name for x in result['skill']):result['skill'].append({'skill':{'name':name}})
  elif kind=='company' and section=='WORK EXPERIENCE':company=(plain(item[1]),plain(item[2]));active=None
  elif kind in ('role','job') and section=='WORK EXPERIENCE':
   if kind=='job':
    location,date=map(str.strip,item[3].rsplit('|',1));company=(plain(item[1]),location);title=item[2]
   else:
    if company is None:raise ValueError('Role without company')
    title,date=item[1:3]
   active={'company':{'name':company[0]},'title':plain(title),'location':company[1],'description_stripped':'',**dates(date)}
   result['experience'].append(active)
  elif kind=='proj' and section=='PROJECTS':
   active={'name':plain(item[1]),'date':item[2],'description_stripped':''}
   result['projects'].append(active)
  elif kind=='bullet' and section in ('WORK EXPERIENCE','PROJECTS') and active is not None:
   active['description_stripped']+='\n' if active['description_stripped'] else ''
   active['description_stripped']+=plain(item[1])
 return result
def write_sidecar(text,variant,destination):
 data=convert_content(text,variant)
 Path(destination).write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n')
 return data
if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('content');p.add_argument('variant');p.add_argument('output');a=p.parse_args()
 write_sidecar(Path(a.content).read_text(),a.variant,a.output)
