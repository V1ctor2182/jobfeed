const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
test('concurrent startup and alarm connect calls own only one socket',async()=>{
 const pending=[],sockets=[],sent=[],event={addListener(){}};
 class Socket {
  static OPEN=1;static CONNECTING=0;
  constructor(url){this.url=url;this.readyState=0;this.listeners={};sockets.push(this);}
  addEventListener(name,fn){this.listeners[name]=fn;}
  send(message){sent.push(JSON.parse(message));}close(){this.readyState=3;}
 }
 const c=vm.createContext({importScripts(){},URL,console,WebSocket:Socket,setTimeout,clearTimeout,setInterval:()=>1,clearInterval(){},chrome:{runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},storage:{local:{get:()=>new Promise(resolve=>pending.push(resolve))}}}});
 vm.runInContext(fs.readFileSync('extensions/jobright-source/service-worker.js','utf8'),c);
 const second=c.connect();
 for(const resolve of pending)resolve({});
 await second;await new Promise(resolve=>setImmediate(resolve));
 assert.equal(sockets.length,1);
 assert.equal(sockets[0].url,'wss://jobfeed.localhost:1355/api/sources/jobright/bridge');
 sockets[0].readyState=1;sockets[0].listeners.open();
 assert.ok(sent[0].sources.includes('application-resolution'));
 assert.ok(sent[0].sources.includes('application-resolution-10'));
 assert.equal(JSON.parse(fs.readFileSync('extensions/jobright-source/manifest.json','utf8')).version,'0.8.5');
 c.closeSocket();
 sockets[0].listeners.message({data:JSON.stringify({type:'ready',protocol:1})});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(vm.runInContext('connectionState',c),'disconnected');
});

test('an explicitly saved bridge address overrides the fresh-install default',async()=>{
 const sockets=[],event={addListener(){}};
 class Socket {
  static OPEN=1;static CONNECTING=0;
  constructor(url){this.url=url;this.readyState=0;sockets.push(this);}
  addEventListener(){}close(){this.readyState=3;}
 }
 const c=vm.createContext({importScripts(){},URL,console,WebSocket:Socket,setTimeout,clearTimeout,setInterval:()=>1,clearInterval(){},chrome:{runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},storage:{local:{get:async()=>({jobfeedBaseUrl:'http://127.0.0.1:8765'})}}}});
 vm.runInContext(fs.readFileSync('extensions/jobright-source/service-worker.js','utf8'),c);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(sockets.length,1);
 assert.equal(sockets[0].url,'ws://127.0.0.1:8765/api/sources/jobright/bridge');
 c.closeSocket();
});

test('reload migrates the old saved default and persists Portless for later reads',async()=>{
 const sockets=[],writes=[],event={addListener(){}};
 let saved='http://127.0.0.1:7654/';
 class Socket {
  static OPEN=1;static CONNECTING=0;
  constructor(url){this.url=url;this.readyState=0;sockets.push(this);}
  addEventListener(){}close(){this.readyState=3;}
 }
 const storage={get:async()=>({jobfeedBaseUrl:saved}),set:async(value)=>{saved=value.jobfeedBaseUrl;writes.push(saved);}};
 const c=vm.createContext({importScripts(){},URL,console,WebSocket:Socket,setTimeout,clearTimeout,setInterval:()=>1,clearInterval(){},chrome:{runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},storage:{local:storage}}});
 vm.runInContext(fs.readFileSync('extensions/jobright-source/service-worker.js','utf8'),c);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(sockets.length,1);
 assert.equal(sockets[0].url,'wss://jobfeed.localhost:1355/api/sources/jobright/bridge');
 assert.deepEqual(writes,['https://jobfeed.localhost:1355']);
 assert.equal(await c.getBaseUrl(),'https://jobfeed.localhost:1355');
 assert.equal(writes.length,1);
 c.closeSocket();
});
